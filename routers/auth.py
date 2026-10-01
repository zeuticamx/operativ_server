"""Registro con verificación de correo, login y refresh de tokens."""

import math
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

import asyncpg
import jwt
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from config import settings
from services.correo import ErrorEnvioCorreo, enviar_codigo_verificacion
from services.google_login import TokenGoogleInvalido, verificar_credential
from services import recuperacion_password
from routers.perfil import version_foto
from deps import UsuarioActual, usuario_actual
from security import (
    crear_access_token,
    crear_refresh_token,
    decodificar_token,
    emitido_antes_de,
    hash_password,
    needs_rehash,
    verify_password,
)
from session import execute, fetch_one, get_pool
from schemas import (
    GoogleLoginIn,
    LoginIn,
    RecuperacionSolicitadaOut,
    RefreshIn,
    RegistroIn,
    ReenviarCodigoIn,
    RestablecerPasswordIn,
    SolicitarRecuperacionIn,
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


def _exigir_terminos(nuevo: bool) -> HTTPException:
    """
    428 con un `codigo` que el portal reconoce para abrir el paso de
    aceptación. Ni tokens ni cuenta: lo que sigue es reenviar la misma
    petición con `acepta_terminos: true`.
    """
    return HTTPException(
        status_code=status.HTTP_428_PRECONDITION_REQUIRED,
        detail={
            "codigo": "terminos_requeridos",
            "mensaje": "Para continuar debes aceptar los Términos y Condiciones.",
            "cuenta_nueva": nuevo,
        },
    )


async def _registrar_aceptacion(user_id: UUID) -> None:
    """Deja la evidencia en una cuenta que ya existía. No pisa una previa."""
    await execute(
        """
        UPDATE portal_users
           SET terminos_aceptados_en = NOW(), terminos_version = $2
         WHERE id = $1 AND terminos_aceptados_en IS NULL
        """,
        user_id,
        settings.TERMINOS_VERSION,
    )


async def _crear_cuenta(
    conn: asyncpg.Connection,
    email: str,
    password_hash: str | None,
    full_name: str | None,
    nombre_negocio: str,
    terminos_aceptados_en: datetime,
    terminos_version: str,
    google_id: str | None = None,
) -> tuple[UUID, UUID]:
    """
    Crea el negocio (tenant), su configuración de agente por defecto y el
    usuario dueño. Quien llama abre la transacción: si algo falla, no queda
    un tenant huérfano sin usuario.

    `password_hash` es None para cuentas que entran solo por Google
    (POST /auth/google): no hay contraseña que hashear en ese camino.

    La aceptación de términos es un argumento obligatorio y no un default:
    así no puede existir un camino que cree una cuenta sin evidencia.
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
        INSERT INTO portal_users
            (tenant_id, email, password_hash, full_name, role, google_id,
             terminos_aceptados_en, terminos_version)
        VALUES ($1, $2, $3, $4, 'owner', $5, $6, $7)
        RETURNING id
        """,
        tenant_id,
        email,
        password_hash,
        full_name,
        google_id,
        terminos_aceptados_en,
        terminos_version,
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

    `acepta_terminos` tiene que ser true (lo exige el esquema, 422 si no):
    sin aceptación ni siquiera se guarda el alta pendiente.
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
                     attempts, expires_at, sent_at,
                     terminos_aceptados_en, terminos_version)
                VALUES ($1, $2, $3, $4, $5, 0, $6, NOW(), NOW(), $7)
                ON CONFLICT (email) DO UPDATE SET
                    code_hash      = EXCLUDED.code_hash,
                    password_hash  = EXCLUDED.password_hash,
                    full_name      = EXCLUDED.full_name,
                    nombre_negocio = EXCLUDED.nombre_negocio,
                    attempts       = 0,
                    expires_at     = EXCLUDED.expires_at,
                    sent_at        = NOW(),
                    terminos_aceptados_en = EXCLUDED.terminos_aceptados_en,
                    terminos_version      = EXCLUDED.terminos_version
                """,
                email,
                hash_password(codigo),
                pwd_hash,
                datos.full_name,
                datos.nombre_negocio,
                _vencimiento(),
                settings.TERMINOS_VERSION,
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
        RETURNING code_hash, password_hash, full_name, nombre_negocio,
                  terminos_aceptados_en, terminos_version
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

    # Un alta pendiente anterior a la migración de términos no tiene
    # aceptación guardada: no se crea cuenta sin evidencia, se vuelve a
    # registrar (ahora con la casilla).
    if fila["terminos_aceptados_en"] is None:
        await execute("DELETE FROM email_verifications WHERE email = $1", email)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Falta la aceptación de los Términos y Condiciones. Vuelve a registrarte.",
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
                    fila["terminos_aceptados_en"],
                    fila["terminos_version"],
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
        SELECT id, tenant_id, password_hash, role, is_active,
               terminos_aceptados_en
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

    # Cuentas anteriores a los términos: en su próximo ingreso tienen que
    # aceptarlos. Va DESPUÉS de comprobar la contraseña: pedir aceptación
    # no puede servir para confirmar que un correo existe.
    if fila["terminos_aceptados_en"] is None:
        if not datos.acepta_terminos:
            raise _exigir_terminos(nuevo=False)
        await _registrar_aceptacion(fila["id"])

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


@router.post("/google", response_model=TokenOut)
async def login_google(datos: GoogleLoginIn):
    """
    Login/alta con Google, alternativa opcional al correo+contraseña de
    arriba — no lo reemplaza, ambos caminos conviven.

    El frontend manda el ID token que entrega el botón "Sign in with
    Google" (Google Identity Services); acá se valida contra Google y,
    según el correo del token:
      - ya hay una cuenta con ese google_id       -> entra directo.
      - ya hay una cuenta con ese correo (alta con contraseña) -> se
        vincula el google_id a esa fila y entra, en vez de duplicar.
      - no hay ninguna  -> se da de alta un negocio nuevo, igual que
        /verificar pero sin password_hash.

    Términos y condiciones: una cuenta NUEVA solo se crea si viene
    `acepta_terminos: true`; si no, 428 y no se escribe nada (ni tenant ni
    usuario). Una cuenta existente que todavía no los aceptó también recibe
    428 hasta que los acepte, y entonces se le guarda la evidencia.
    """
    if not settings.google_login_configurado:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El login con Google no está configurado",
        )

    try:
        payload = await verificar_credential(datos.credential)
    except TokenGoogleInvalido:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de Google inválido",
        )

    # Sin el correo confirmado por Google no hay identidad en la que
    # confiar para entrar o vincular una cuenta existente.
    if not payload.get("email_verified", False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tu correo de Google no está verificado",
        )

    google_id = payload["sub"]
    email = payload["email"].lower()
    full_name = payload.get("name")

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            fila = await conn.fetchrow(
                """
                SELECT id, tenant_id, role, is_active, terminos_aceptados_en
                FROM portal_users
                WHERE google_id = $1
                """,
                google_id,
            )

            if fila is None:
                # ¿Cuenta ya dada de alta con correo+contraseña? Se vincula
                # en vez de duplicar; el UNIQUE de email tampoco dejaría
                # crear una fila nueva con el mismo correo.
                fila = await conn.fetchrow(
                    """
                    UPDATE portal_users SET google_id = $2
                    WHERE LOWER(email) = LOWER($1) AND google_id IS NULL
                    RETURNING id, tenant_id, role, is_active, terminos_aceptados_en
                    """,
                    email,
                    google_id,
                )

            if fila is None:
                # Sin aceptación explícita no se crea nada. El raise sale de
                # la transacción, así que tampoco queda el tenant a medias.
                if not datos.acepta_terminos:
                    raise _exigir_terminos(nuevo=True)

                # Cuenta nueva: negocio de arranque con el nombre que dio
                # Google, editable después desde el portal.
                user_id, tenant_id = await _crear_cuenta(
                    conn,
                    email,
                    None,
                    full_name,
                    (full_name or "").strip() or "Mi negocio",
                    datetime.now(timezone.utc),
                    settings.TERMINOS_VERSION,
                    google_id=google_id,
                )
                fila = {
                    "id": user_id,
                    "tenant_id": tenant_id,
                    "role": "owner",
                    "is_active": True,
                }
            elif fila["terminos_aceptados_en"] is None and fila["is_active"]:
                # Cuenta anterior a los términos: tiene que aceptar en este
                # ingreso. El raise revierte también la vinculación del
                # google_id de arriba.
                if not datos.acepta_terminos:
                    raise _exigir_terminos(nuevo=False)
                await conn.execute(
                    """
                    UPDATE portal_users
                       SET terminos_aceptados_en = NOW(), terminos_version = $2
                     WHERE id = $1 AND terminos_aceptados_en IS NULL
                    """,
                    fila["id"],
                    settings.TERMINOS_VERSION,
                )

    if not fila["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="La cuenta está desactivada",
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
        """
        SELECT id, tenant_id, role, is_active, credenciales_cambiadas_en
        FROM portal_users WHERE id = $1
        """,
        UUID(payload["sub"]),
    )

    if fila is None or not fila["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario no encontrado o inactivo",
        )

    # Un refresh token de antes de un cambio de contraseña ya no renueva
    # nada: sin esto, una sesión robada sobreviviría 30 días al cambio.
    if emitido_antes_de(payload, fila["credenciales_cambiadas_en"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="La sesión ya no es válida. Inicia sesión de nuevo.",
        )

    return TokenOut(
        access_token=crear_access_token(
            fila["id"], fila["tenant_id"], fila["role"]
        ),
        refresh_token=crear_refresh_token(fila["id"]),
    )


# ============================================================
# RECUPERACIÓN DE CONTRASEÑA (services/recuperacion_password.py)
# ============================================================
@router.post(
    "/recuperar/solicitar",
    response_model=RecuperacionSolicitadaOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def solicitar_recuperacion(
    datos: SolicitarRecuperacionIn, tareas: BackgroundTasks
) -> RecuperacionSolicitadaOut:
    """
    Manda un código de 6 dígitos (5 minutos, un solo uso) si el correo es
    de una cuenta activa.

    Siempre 202 con el mismo cuerpo, exista o no la cuenta, y todo el
    trabajo (buscar la cuenta, hashear el código, SMTP) va en background
    después de responder: ni el contenido ni el tiempo de la respuesta
    sirven para averiguar qué correos están registrados. Por lo mismo, un
    fallo de SMTP no llega acá como 502: queda en el log.
    """
    tareas.add_task(recuperacion_password.procesar_solicitud, datos.email)
    return RecuperacionSolicitadaOut(
        expira_en_minutos=recuperacion_password.VIGENCIA_MINUTOS,
        reenviar_en_segundos=settings.CODIGO_REENVIO_SEGUNDOS,
    )


@router.post("/recuperar/restablecer", status_code=status.HTTP_204_NO_CONTENT)
async def restablecer_password(datos: RestablecerPasswordIn) -> None:
    """
    Canjea el código y cambia la contraseña. Cierra todas las sesiones
    abiertas de la cuenta; no devuelve tokens: hay que entrar con la nueva.

    Un único 400 para código incorrecto, vencido, ya usado o con intentos
    agotados. Distinguirlos le diría a quien prueba un correo ajeno si ese
    correo tiene un código pendiente, o sea, que la cuenta existe. El portal
    sabe cuándo venció por su propia cuenta regresiva.
    """
    try:
        await recuperacion_password.restablecer(datos.email, datos.codigo, datos.password)
    except recuperacion_password.CodigoInvalido:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El código es incorrecto o ya venció. Solicita uno nuevo.",
        )


@router.get("/yo", response_model=UsuarioOut)
async def yo(usuario: UsuarioActual = Depends(usuario_actual)):
    nombre_negocio = None
    if usuario.tenant_id:
        nombre_negocio = await fetch_one(
            "SELECT name FROM tenants WHERE id = $1", usuario.tenant_id
        )
        nombre_negocio = nombre_negocio["name"] if nombre_negocio else None

    # Lo mínimo del perfil para el sidebar (nombre, foto, "faltan datos").
    # El perfil completo lo sirve GET /api/perfil.
    perfil = await fetch_one(
        """
        SELECT pu.nombres, pu.apellido_paterno, pu.perfil_completado_en,
               f.actualizada_en AS foto_actualizada_en
        FROM portal_users pu
        LEFT JOIN portal_user_fotos f ON f.portal_user_id = pu.id
        WHERE pu.id = $1
        """,
        usuario.id,
    )

    return UsuarioOut(
        id=usuario.id,
        email=usuario.email,
        full_name=None,
        role=usuario.role,
        tenant_id=usuario.tenant_id,
        nombre_negocio=nombre_negocio,
        es_gerencia_plataforma=usuario.es_gerencia_plataforma,
        impersonado_por=usuario.impersonado_por,
        impersonacion_expira=usuario.impersonacion_expira,
        nombres=perfil["nombres"] if perfil else None,
        apellido_paterno=perfil["apellido_paterno"] if perfil else None,
        perfil_completo=bool(perfil and perfil["perfil_completado_en"]),
        foto_version=version_foto(perfil["foto_actualizada_en"]) if perfil else None,
    )
