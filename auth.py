"""Registro con verificación de correo, login y refresh de tokens."""

import math
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

import asyncpg
import jwt
from fastapi import APIRouter, Depends, HTTPException, status

from config import settings
from correo import ErrorEnvioCorreo, enviar_codigo_verificacion
from deps import UsuarioActual, usuario_actual
from security import (
    crear_access_token,
    crear_refresh_token,
    decodificar_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from session import execute, fetch_one, get_pool
from schemas import (
    LoginIn,
    RefreshIn,
    RegistroIn,
    ReenviarCodigoIn,
    TokenOut,
    UsuarioOut,
    VerificacionPendienteOut,
    VerificarCodigoIn,
)

router = APIRouter(prefix="/auth", tags=["auth"])


PROMPT_INICIAL = """Eres el asistente virtual de {negocio}.

# TONO
Cercano, profesional y resolutivo. Español neutro.

# FORMATO
- Máximo 2 o 3 líneas por respuesta. Es un chat, no un correo.
- Ve directo a resolver, sin fórmulas de cortesía largas.

# REGLAS
- Si no sabes algo, dilo. No inventes datos ni precios.
- Usa el historial: no vuelvas a preguntar lo que el cliente ya te dijo.
- Si preguntan si eres una IA, confírmalo con naturalidad."""


# ============================================================
# ALTA EN DOS PASOS
# ============================================================
# 1. POST /registro         guarda el alta en email_verifications y manda
#                           un código de 6 dígitos al correo. No crea nada
#                           en tenants ni portal_users.
# 2. POST /verificar        si el código coincide, recién ahí se crean el
#                           tenant, su config de agente y el usuario dueño.
#
# El correo no se da por bueno hasta que alguien prueba que lo lee. Ver
# 04_verificacion_email.sql para por qué el alta pendiente vive en su
# propia tabla y no como un `email_verified` dentro de portal_users.
# ============================================================


def _generar_codigo() -> str:
    """6 dígitos con RNG criptográfico. `random` acá sería adivinable."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _vencimiento() -> datetime:
    return datetime.now(timezone.utc) + timedelta(
        minutes=settings.CODIGO_VIGENCIA_MINUTOS
    )


def _espera_de_reenvio(sent_at: datetime) -> int:
    """Segundos que faltan para poder pedir otro código. 0 si ya se puede."""
    transcurrido = (datetime.now(timezone.utc) - sent_at).total_seconds()
    return max(0, math.ceil(settings.CODIGO_REENVIO_SEGUNDOS - transcurrido))


def _pendiente(email: str) -> VerificacionPendienteOut:
    return VerificacionPendienteOut(
        email=email,
        expira_en_minutos=settings.CODIGO_VIGENCIA_MINUTOS,
        reenviar_en_segundos=settings.CODIGO_REENVIO_SEGUNDOS,
    )


async def _mandar_codigo(email: str, codigo: str, negocio: str) -> None:
    """Traduce un fallo de SMTP a un 502, sin filtrar el detalle del servidor."""
    try:
        await enviar_codigo_verificacion(email, codigo, negocio)
    except ErrorEnvioCorreo:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo enviar el correo con el código. Inténtalo de nuevo.",
        )


async def _crear_cuenta(
    conn: asyncpg.Connection,
    email: str,
    password_hash: str,
    full_name: str | None,
    nombre_negocio: str,
) -> tuple[UUID, UUID]:
    """
    Crea el negocio (tenant), su configuración de agente por defecto y el
    usuario dueño. Quien llama abre la transacción: si algo falla, no queda
    un tenant huérfano sin usuario.
    """
    tenant_id = await conn.fetchval(
        "INSERT INTO tenants (name) VALUES ($1) RETURNING id",
        nombre_negocio,
    )

    await conn.execute(
        """
        INSERT INTO tenant_agent_config
            (tenant_id, agent_name, system_prompt, temperature, history_window)
        VALUES ($1, $2, $3, 0.7, 30)
        """,
        tenant_id,
        "Asistente",
        PROMPT_INICIAL.format(negocio=nombre_negocio),
    )

    user_id = await conn.fetchval(
        """
        INSERT INTO portal_users (tenant_id, email, password_hash, full_name, role)
        VALUES ($1, $2, $3, $4, 'owner')
        RETURNING id
        """,
        tenant_id,
        email,
        password_hash,
        full_name,
    )

    return user_id, tenant_id


@router.post(
    "/registro", response_model=VerificacionPendienteOut, status_code=202
)
async def registro(datos: RegistroIn):
    """
    Paso 1: guarda el alta como pendiente y manda el código al correo.

    Devuelve 202 y ningún token — la cuenta todavía no existe. Para
    terminar, POST /auth/verificar con el código.
    """
    email = datos.email.lower()
    # La contraseña se hashea acá y en claro no se guarda nunca, ni
    # siquiera mientras el alta está pendiente de confirmar.
    pwd_hash = hash_password(datos.password)
    codigo = _generar_codigo()

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            # Limpieza de vencidos aprovechando el viaje. Es barata y evita
            # depender de un cron para que la tabla no crezca sola.
            await conn.execute(
                "DELETE FROM email_verifications WHERE expires_at < NOW()"
            )

            existe = await conn.fetchval(
                "SELECT 1 FROM portal_users WHERE LOWER(email) = LOWER($1)",
                email,
            )
            if existe:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Ya existe una cuenta con ese correo",
                )

            # El mismo tope de reenvío que /reenviar-codigo: si no, bastaría
            # repetir /registro para inundar una casilla ajena de códigos.
            previo = await conn.fetchrow(
                "SELECT sent_at FROM email_verifications WHERE email = $1 FOR UPDATE",
                email,
            )
            if previo is not None:
                espera = _espera_de_reenvio(previo["sent_at"])
                if espera > 0:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail=f"Ya se envió un código. Espera {espera} segundos.",
                        headers={"Retry-After": str(espera)},
                    )

            # Registrarse otra vez con el mismo correo pisa el intento
            # anterior en vez de acumular filas, y de paso invalida el
            # código viejo y devuelve los intentos a cero.
            await conn.execute(
                """
                INSERT INTO email_verifications
                    (email, code_hash, password_hash, full_name, nombre_negocio,
                     attempts, expires_at, sent_at)
                VALUES ($1, $2, $3, $4, $5, 0, $6, NOW())
                ON CONFLICT (email) DO UPDATE SET
                    code_hash      = EXCLUDED.code_hash,
                    password_hash  = EXCLUDED.password_hash,
                    full_name      = EXCLUDED.full_name,
                    nombre_negocio = EXCLUDED.nombre_negocio,
                    attempts       = 0,
                    expires_at     = EXCLUDED.expires_at,
                    sent_at        = NOW()
                """,
                email,
                hash_password(codigo),
                pwd_hash,
                datos.full_name,
                datos.nombre_negocio,
                _vencimiento(),
            )

            # Dentro de la transacción a propósito: si el SMTP falla, el
            # alta pendiente no llega a escribirse y el usuario reintenta
            # limpio, sin toparse con el freno de reenvío de un código que
            # nunca salió.
            await _mandar_codigo(email, codigo, datos.nombre_negocio)

    return _pendiente(email)


@router.post("/verificar", response_model=TokenOut, status_code=201)
async def verificar(datos: VerificarCodigoIn):
    """Paso 2: comprueba el código y crea la cuenta."""
    email = datos.email.lower()

    # UPDATE ... RETURNING en una sola sentencia: comprueba vigencia y tope
    # de intentos y suma el intento de forma atómica. Con un SELECT y un
    # UPDATE por separado, dos peticiones a la vez gastarían un solo intento
    # y el tope se podría estirar con concurrencia.
    fila = await fetch_one(
        """
        UPDATE email_verifications
           SET attempts = attempts + 1
         WHERE email = $1
           AND expires_at > NOW()
           AND attempts < $2
        RETURNING code_hash, password_hash, full_name, nombre_negocio
        """,
        email,
        settings.CODIGO_MAX_INTENTOS,
    )

    if fila is None:
        # No hay fila, venció, o se agotaron los intentos. En los dos
        # últimos casos el alta ya no sirve para nada: se descarta.
        await execute("DELETE FROM email_verifications WHERE email = $1", email)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El código venció o se agotaron los intentos. Vuelve a registrarte.",
        )

    if not verify_password(datos.codigo, fila["code_hash"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Código incorrecto",
        )

    try:
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                user_id, tenant_id = await _crear_cuenta(
                    conn,
                    email,
                    fila["password_hash"],
                    fila["full_name"],
                    fila["nombre_negocio"],
                )
                await conn.execute(
                    "DELETE FROM email_verifications WHERE email = $1", email
                )
    except asyncpg.UniqueViolationError:
        # Alguien registró ese correo entre el /registro y el /verificar.
        # Lo atrapa el UNIQUE de portal_users.email, no una comprobación
        # previa, que en carrera no serviría de nada.
        await execute("DELETE FROM email_verifications WHERE email = $1", email)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe una cuenta con ese correo",
        )

    return TokenOut(
        access_token=crear_access_token(user_id, tenant_id, "owner"),
        refresh_token=crear_refresh_token(user_id),
    )


@router.post(
    "/reenviar-codigo", response_model=VerificacionPendienteOut, status_code=202
)
async def reenviar_codigo(datos: ReenviarCodigoIn):
    """Manda un código nuevo para un alta que sigue pendiente."""
    email = datos.email.lower()
    codigo = _generar_codigo()

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            fila = await conn.fetchrow(
                """
                SELECT nombre_negocio, sent_at
                FROM email_verifications
                WHERE email = $1 AND expires_at > NOW()
                FOR UPDATE
                """,
                email,
            )
            if fila is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No hay un registro pendiente para ese correo. Empieza de nuevo.",
                )

            espera = _espera_de_reenvio(fila["sent_at"])
            if espera > 0:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Espera {espera} segundos antes de pedir otro código.",
                    headers={"Retry-After": str(espera)},
                )

            # Código nuevo, reloj nuevo e intentos a cero: el anterior deja
            # de servir en cuanto se manda este.
            await conn.execute(
                """
                UPDATE email_verifications
                   SET code_hash  = $2,
                       attempts   = 0,
                       expires_at = $3,
                       sent_at    = NOW()
                 WHERE email = $1
                """,
                email,
                hash_password(codigo),
                _vencimiento(),
            )

            await _mandar_codigo(email, codigo, fila["nombre_negocio"])

    return _pendiente(email)


@router.post("/login", response_model=TokenOut)
async def login(datos: LoginIn):
    fila = await fetch_one(
        """
        SELECT id, tenant_id, password_hash, role, is_active
        FROM portal_users
        WHERE LOWER(email) = LOWER($1)
        """,
        datos.email,
    )

    # Mismo mensaje y mismo costo aproximado para usuario inexistente y
    # contraseña incorrecta: no queremos que se pueda enumerar correos
    # midiendo qué respuesta llega.
    if fila is None:
        hash_password(datos.password)  # gasta el tiempo igual
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Correo o contraseña incorrectos",
        )

    if not verify_password(datos.password, fila["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Correo o contraseña incorrectos",
        )

    if not fila["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="La cuenta está desactivada",
        )

    # Si el hash quedó con parámetros viejos, se actualiza aprovechando
    # que en este momento tenemos la contraseña en claro.
    if needs_rehash(fila["password_hash"]):
        await execute(
            "UPDATE portal_users SET password_hash = $1 WHERE id = $2",
            hash_password(datos.password),
            fila["id"],
        )

    await execute(
        "UPDATE portal_users SET last_login_at = NOW() WHERE id = $1",
        fila["id"],
    )

    return TokenOut(
        access_token=crear_access_token(
            fila["id"], fila["tenant_id"], fila["role"]
        ),
        refresh_token=crear_refresh_token(fila["id"]),
    )


@router.post("/refresh", response_model=TokenOut)
async def refresh(datos: RefreshIn):
    try:
        payload = decodificar_token(datos.refresh_token, "refresh")
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token inválido o expirado",
        )

    fila = await fetch_one(
        "SELECT id, tenant_id, role, is_active FROM portal_users WHERE id = $1",
        UUID(payload["sub"]),
    )

    if fila is None or not fila["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario no encontrado o inactivo",
        )

    return TokenOut(
        access_token=crear_access_token(
            fila["id"], fila["tenant_id"], fila["role"]
        ),
        refresh_token=crear_refresh_token(fila["id"]),
    )


@router.get("/yo", response_model=UsuarioOut)
async def yo(usuario: UsuarioActual = Depends(usuario_actual)):
    nombre_negocio = None
    if usuario.tenant_id:
        nombre_negocio = await fetch_one(
            "SELECT name FROM tenants WHERE id = $1", usuario.tenant_id
        )
        nombre_negocio = nombre_negocio["name"] if nombre_negocio else None

    return UsuarioOut(
        id=usuario.id,
        email=usuario.email,
        full_name=None,
        role=usuario.role,
        tenant_id=usuario.tenant_id,
        nombre_negocio=nombre_negocio,
    )
