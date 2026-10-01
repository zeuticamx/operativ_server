"""
Recuperación de contraseña con un código de 6 dígitos por correo.

Flujo (routers/auth.py):
  1. POST /auth/recuperar/solicitar   -> `procesar_solicitud`, en background.
  2. POST /auth/recuperar/restablecer -> `restablecer`.

Reglas:
  - Vigencia ESTRICTA de 5 minutos (constante, no variable de entorno: que
    nadie la estire por configuración). Emisión y comprobación usan NOW() de
    Postgres, nunca el reloj de este proceso.
  - Un solo uso: el canje borra la fila en la misma transacción que cambia
    la contraseña.
  - Tope de intentos = settings.CODIGO_MAX_INTENTOS (el mismo del alta).
  - Anti-enumeración: la solicitud corre entera en background, así que la
    respuesta HTTP es idéntica (y tarda lo mismo) exista o no el correo. En
    el canje, "no hay código" y "código incorrecto" dan el mismo error y
    gastan el mismo argon2.
  - Hashing: security.hash_password/verify_password tal cual, para el
    código y para la contraseña nueva.

Ver sql/32_recuperacion_password.sql.
"""

import logging
import secrets
from uuid import UUID

from config import settings
from security import hash_password, verify_password
from services.correo import enviar_codigo_recuperacion
from session import execute, fetch_one, get_pool

log = logging.getLogger("operativai.recuperacion")

VIGENCIA_MINUTOS = 5

# Hash de un código que nadie tiene. Cuando no hay fila que comprobar se
# verifica contra este, para que esa respuesta tarde lo mismo que la de un
# código incorrecto y el tiempo no delate si el correo pidió un código.
_HASH_SEÑUELO = hash_password(secrets.token_hex(16))


class CodigoInvalido(Exception):
    """Código incorrecto, vencido, ya usado o con los intentos agotados."""


def generar_codigo() -> str:
    """6 dígitos con RNG criptográfico (`random` sería adivinable)."""
    return f"{secrets.randbelow(1_000_000):06d}"


async def procesar_solicitud(email: str) -> None:
    """
    Emite y manda un código si `email` es de una cuenta activa. Si no, no
    hace nada. Nunca lanza: corre como BackgroundTask, después de que la
    respuesta 202 ya salió, y cualquier fallo solo va al log.

    Las cuentas que entraron siempre con Google (password_hash NULL) también
    reciben código: probar que leen el correo basta para que se definan una
    contraseña.
    """
    try:
        await _emitir(email.lower())
    except Exception:
        log.exception("Fallo al procesar una solicitud de recuperación")


async def _emitir(email: str) -> None:
    codigo = generar_codigo()

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            # Limpieza de vencidos aprovechando el viaje, como /registro.
            await conn.execute("DELETE FROM password_resets WHERE expires_at < NOW()")

            usuario = await conn.fetchrow(
                "SELECT id FROM portal_users WHERE LOWER(email) = $1 AND is_active",
                email,
            )
            if usuario is None:
                return

            # Freno de reenvío: sin esto, repetir la solicitud serviría para
            # inundar de códigos una casilla ajena. En silencio: la
            # respuesta pública ya salió y es la misma de siempre.
            reciente = await conn.fetchval(
                """
                SELECT 1 FROM password_resets
                 WHERE portal_user_id = $1
                   AND sent_at > NOW() - make_interval(secs => $2)
                """,
                usuario["id"],
                settings.CODIGO_REENVIO_SEGUNDOS,
            )
            if reciente:
                log.info("Recuperación pedida de nuevo antes del freno; no se reenvía")
                return

            # Pisa el código anterior (si había): deja de servir ya, con
            # intentos en cero y reloj nuevo.
            await conn.execute(
                """
                INSERT INTO password_resets (portal_user_id, code_hash, attempts, expires_at, sent_at)
                VALUES ($1, $2, 0, NOW() + make_interval(mins => $3), NOW())
                ON CONFLICT (portal_user_id) DO UPDATE SET
                    code_hash  = EXCLUDED.code_hash,
                    attempts   = 0,
                    expires_at = EXCLUDED.expires_at,
                    sent_at    = EXCLUDED.sent_at
                """,
                usuario["id"],
                hash_password(codigo),
                VIGENCIA_MINUTOS,
            )

            # Dentro de la transacción, igual que /registro: si el SMTP falla
            # (ErrorEnvioCorreo) la fila se revierte, no queda un código que
            # nadie recibió frenando el reenvío, y procesar_solicitud lo
            # deja en el log.
            await enviar_codigo_recuperacion(email, codigo, VIGENCIA_MINUTOS)


async def restablecer(email: str, codigo: str, password_nueva: str) -> None:
    """
    Canjea el código y cambia la contraseña. Lanza CodigoInvalido si no se
    puede, sin decir por qué: quien llama no debe poder distinguir "no hay
    código para ese correo" de "código incorrecto".
    """
    email = email.lower()

    # Suma el intento y comprueba vigencia y tope en una sola sentencia: con
    # SELECT + UPDATE separados, dos peticiones a la vez gastarían un intento.
    fila = await fetch_one(
        """
        UPDATE password_resets pr
           SET attempts = pr.attempts + 1
          FROM portal_users pu
         WHERE pr.portal_user_id = pu.id
           AND LOWER(pu.email) = $1
           AND pu.is_active
           AND pr.expires_at > NOW()
           AND pr.attempts < $2
        RETURNING pr.portal_user_id, pr.code_hash
        """,
        email,
        settings.CODIGO_MAX_INTENTOS,
    )

    if fila is None:
        # Vencido o agotado: ya no sirve, se descarta. Un código vigente no
        # se toca (este DELETE no puede usarse para anular el de otro).
        await execute(
            """
            DELETE FROM password_resets pr
             USING portal_users pu
             WHERE pr.portal_user_id = pu.id
               AND LOWER(pu.email) = $1
               AND (pr.expires_at <= NOW() OR pr.attempts >= $2)
            """,
            email,
            settings.CODIGO_MAX_INTENTOS,
        )
        verify_password(codigo, _HASH_SEÑUELO)  # mismo costo que un código errado
        raise CodigoInvalido

    if not verify_password(codigo, fila["code_hash"]):
        raise CodigoInvalido

    await _aplicar_cambio(fila["portal_user_id"], fila["code_hash"], password_nueva)


async def _aplicar_cambio(user_id: UUID, code_hash: str, password_nueva: str) -> None:
    password_hash = hash_password(password_nueva)

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            # Se borra ANTES de cambiar la contraseña y solo si sigue siendo
            # el mismo código y sigue vigente:
            #  - dos canjes simultáneos del mismo código: solo uno borra la
            #    fila, el otro no encuentra nada y no cambia nada;
            #  - si venció mientras se verificaba el argon2, ya no vale;
            #  - si entre medio se pidió un código nuevo, este ya no vale.
            canjeado = await conn.fetchval(
                """
                DELETE FROM password_resets
                 WHERE portal_user_id = $1 AND code_hash = $2 AND expires_at > NOW()
                RETURNING 1
                """,
                user_id,
                code_hash,
            )
            if not canjeado:
                raise CodigoInvalido

            # credenciales_cambiadas_en invalida todos los JWT emitidos
            # antes (deps.usuario_actual y /auth/refresh): quien tuviera una
            # sesión abierta, legítima o robada, tiene que volver a entrar.
            await conn.execute(
                """
                UPDATE portal_users
                   SET password_hash = $2, credenciales_cambiadas_en = NOW()
                 WHERE id = $1
                """,
                user_id,
                password_hash,
            )

    log.info("Contraseña restablecida para el usuario %s", user_id)
