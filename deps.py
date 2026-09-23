"""Dependencias compartidas: quién está autenticado y a qué tenant pertenece."""

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import settings
from security import decodificar_token
from session import fetch_one

bearer = HTTPBearer(auto_error=False)

# Quién puede cambiar la configuración del negocio, no solo mirarla.
# 'member' queda fuera a propósito: ve el pipeline, no apaga servicios.
ROLES_GERENCIA = frozenset({"owner", "superadmin"})

# Rol de la app de vendedores en campo. Es un valor más de
# portal_users.role (owner / member / superadmin / vendedor), no un
# sistema de permisos aparte: la columna es VARCHAR sin CHECK, así que
# admitirlo no necesitó migración.
ROL_VENDEDOR = "vendedor"

# Lo único que puede hacer una sesión de "ver como" (impersonación de
# plataforma). Todo lo demás es una escritura sobre los datos de un cliente
# hecha por alguien que no es el cliente.
METODOS_SOLO_LECTURA = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass
class UsuarioActual:
    id: UUID
    tenant_id: UUID | None
    email: str
    role: str
    es_gerencia_plataforma: bool = False
    # Solo en sesiones de "ver como": el correo del gerente que está
    # mirando y cuándo vence el token. None en una sesión normal.
    impersonado_por: str | None = None
    impersonacion_expira: datetime | None = None

    @property
    def es_superadmin(self) -> bool:
        return self.role == "superadmin"


async def _validar_impersonacion(payload: dict, request: Request) -> str:
    """
    Revalida una sesión de "ver como" y devuelve el correo del gerente.

    Dos controles, en este orden:

    1. El gerente sigue siendo gerente. El token dura poco, pero si a
       alguien lo sacan de gerencia_users a mitad de una impersonación, la
       sesión tiene que morir en la siguiente petición — mismo criterio que
       usuario_actual aplica a todo lo demás.
    2. Solo lectura. Se bloquea acá, en la dependencia que usan todos los
       endpoints del portal, y no endpoint por endpoint: una lista de
       permitidos que alguien olvida actualizar es una escritura que se
       cuela. 403 y no 401: el token es válido, lo que falta es el permiso,
       y un 401 haría que el portal intentara refrescar en vano.
    """
    gerente = await fetch_one(
        """
        SELECT pu.email
        FROM portal_users pu
        JOIN gerencia_users gu ON LOWER(gu.email) = LOWER(pu.email)
        WHERE pu.id = $1 AND pu.is_active
        """,
        UUID(payload["imp"]),
    )
    if gerente is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="La sesión de 'ver como' ya no es válida",
        )

    if request.method not in METODOS_SOLO_LECTURA:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Modo solo lectura: estás viendo el portal como este negocio",
        )

    return gerente["email"]


async def usuario_actual(
    request: Request,
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> UsuarioActual:
    if cred is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta el token de autenticación",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decodificar_token(cred.credentials, "access")
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="El token expiró",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Se relee de BD en vez de confiar solo en el JWT: si al usuario lo
    # desactivaron o lo movieron de tenant, el token viejo no debe seguir
    # sirviendo hasta que expire. El LEFT JOIN contra gerencia_users hace
    # lo mismo para el nivel gerencia de plataforma: si a alguien lo sacan
    # de esa lista, pierde el nivel en la siguiente petición, no cuando
    # expire el token.
    fila = await fetch_one(
        """
        SELECT pu.id, pu.tenant_id, pu.email, pu.role, pu.is_active,
               (gu.id IS NOT NULL) AS es_gerencia_plataforma
        FROM portal_users pu
        LEFT JOIN gerencia_users gu ON LOWER(gu.email) = LOWER(pu.email)
        WHERE pu.id = $1
        """,
        UUID(payload["sub"]),
    )

    if fila is None or not fila["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario no encontrado o inactivo",
        )

    if "imp" in payload:
        impersonado_por = await _validar_impersonacion(payload, request)
        return UsuarioActual(
            id=fila["id"],
            tenant_id=fila["tenant_id"],
            email=fila["email"],
            role=fila["role"],
            # Nunca, aunque el dueño que se está mirando fuera gerencia: la
            # sesión es la del negocio, no la de la plataforma. Sin esto,
            # desde el "ver como" se podría volver a entrar a /gerencia.
            es_gerencia_plataforma=False,
            impersonado_por=impersonado_por,
            impersonacion_expira=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
        )

    return UsuarioActual(
        id=fila["id"],
        tenant_id=fila["tenant_id"],
        email=fila["email"],
        role=fila["role"],
        es_gerencia_plataforma=fila["es_gerencia_plataforma"],
    )


async def tenant_actual(
    usuario: UsuarioActual = Depends(usuario_actual),
) -> UUID:
    """
    Para endpoints que operan sobre datos de un tenant.
    Falla si el usuario todavía no tiene tenant asignado.
    """
    if usuario.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El usuario no tiene un negocio asociado todavía",
        )
    return usuario.tenant_id


def verificar_acceso_tenant(usuario: UsuarioActual, tenant_id: UUID) -> UUID:
    """
    Confirma que este usuario puede operar sobre `tenant_id`.

    Hace falta cuando el tenant llega por la URL o por el body en vez de
    salir del JWT: sin esto, cambiar el UUID de la ruta serviría para leer
    el pipeline de otro negocio con un token perfectamente válido.

    404 y no 403 cuando no coincide: un 403 confirmaría que ese tenant
    existe, que es justo lo que un token ajeno no debería poder averiguar.
    """
    if usuario.es_superadmin:
        return tenant_id

    if usuario.tenant_id is None or usuario.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Negocio no encontrado",
        )

    return tenant_id


async def tenant_en_ruta(
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(usuario_actual),
) -> UUID:
    """Para los endpoints /tenants/{tenant_id}/..."""
    return verificar_acceso_tenant(usuario, tenant_id)


async def gerencia_actual(
    usuario: UsuarioActual = Depends(usuario_actual),
) -> UsuarioActual:
    """Endpoints que cambian la configuración del negocio."""
    if usuario.role not in ROLES_GERENCIA:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Hace falta ser dueño o administrador del negocio",
        )
    return usuario


async def gerencia_plataforma_actual(
    usuario: UsuarioActual = Depends(usuario_actual),
) -> UsuarioActual:
    """
    Para endpoints reservados al nivel gerencia de plataforma (tabla
    gerencia_users), no al rol dentro de un tenant. No confundir con
    `gerencia_actual`: ese dependency es sobre portal_users.role
    (owner/superadmin) de UN negocio; este es un nivel aparte, sin tenant,
    para quienes administran la plataforma completa (el equipo de
    OperativAI). Sin endpoints propios todavía — queda listo para que las
    próximas features de este tipo lo usen.
    """
    if not usuario.es_gerencia_plataforma:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta acción requiere nivel gerencia",
        )
    return usuario


@dataclass
class VendedorActual:
    """El vendedor detrás del portal_user que está llamando."""

    id: UUID
    tenant_id: UUID
    nombre: str
    portal_user_id: UUID
    email: str


async def vendedor_actual(
    usuario: UsuarioActual = Depends(usuario_actual),
) -> VendedorActual:
    """
    Resuelve el vendedor de la petición.

    El JWT no lleva ni el rol ni el vendedor_id: solo la identidad (`sub`).
    Todo lo demás se relee de la base en cada petición, igual que hace
    `usuario_actual`. Así, si gerencia desactiva al vendedor o le quita el
    rol, deja de pasar en la siguiente petición y no cuando expire el
    token — que con ACCESS_TOKEN_MINUTES=60 podría ser una hora entera de
    check-ins de alguien que ya no trabaja ahí.

    403 y no 401 en los dos rechazos: el token es válido y la sesión sirve,
    lo que falta es el permiso. Un 401 haría que el cliente intentara
    refrescar en vano y terminara mandando al usuario al login.
    """
    if usuario.role != ROL_VENDEDOR:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta sección es solo para vendedores",
        )

    fila = await fetch_one(
        """
        SELECT id, tenant_id, nombre, activo
        FROM vendedores
        WHERE portal_user_id = $1
        """,
        usuario.id,
    )

    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tu cuenta tiene el rol de vendedor pero no está ligada a una ficha de vendedor",
        )

    if not fila["activo"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tu cuenta de vendedor está desactivada",
        )

    return VendedorActual(
        id=fila["id"],
        tenant_id=fila["tenant_id"],
        nombre=fila["nombre"],
        portal_user_id=usuario.id,
        email=usuario.email,
    )


async def llamada_interna(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """
    Autentica a n8n en /api/eventos/*.

    n8n no tiene sesión de portal, así que el JWT de portal_users no aplica
    acá: se usa un secreto compartido.

    Sin N8N_INTERNAL_TOKEN configurado el endpoint no atiende a nadie (503).
    Es deliberado: si esto fuera "sin token no se valida", olvidar la
    variable dejaría abierto un endpoint que crea leads y que, probando
    UUIDs, dice cuáles existen.
    """
    if not settings.N8N_INTERNAL_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El endpoint interno no está configurado (falta N8N_INTERNAL_TOKEN)",
        )

    # compare_digest y no ==: comparar tiempo constante para que el largo del
    # prefijo acertado no se pueda medir por lo que tarda la respuesta.
    if x_internal_token is None or not secrets.compare_digest(
        x_internal_token, settings.N8N_INTERNAL_TOKEN
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token interno inválido",
        )
