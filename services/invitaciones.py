"""
Invitaciones al equipo de un negocio (sql/36_invitaciones_equipo.sql).

Flujo:
  1. El dueño invita (routers/equipo.py) -> `crear`: una fila con el hash del
     token, y el enlace en claro va al correo del invitado.
  2. El invitado abre el enlace (portal /invitacion) -> `leer` para mostrar a
     qué negocio entra, y `aceptar` para crear la cuenta con SU contraseña.

Reglas:
  - El rol y el tenant salen de la invitación, nunca de lo que mande el
    invitado: con un enlace de 'vendedor' no hay forma de terminar 'owner'.
  - Un solo uso, y una sola invitación viva por correo y por ficha en cada
    negocio: invitar de nuevo revoca la anterior.
  - Vigencia de VIGENCIA_DIAS, comparada con el reloj de Postgres.
  - El token va en el fragmento del enlace (#t=...), no en la query: el
    fragmento no viaja al servidor del portal ni queda en sus logs de acceso.
"""

import hashlib
import logging
import secrets
from dataclasses import dataclass
from uuid import UUID

import asyncpg

from config import settings
from services.correo import ErrorEnvioCorreo, enviar_invitacion

log = logging.getLogger("operativai.invitaciones")

VIGENCIA_DIAS = 7

ETIQUETA_ROL = {"member": "colaborador", "vendedor": "vendedor", "proveedor": "proveedor de la agenda"}


class InvitacionInvalida(Exception):
    """No existe, ya se usó, se revocó o venció. Al invitado se le dice lo mismo en todos los casos."""


class CorreoYaRegistrado(Exception):
    """El correo ya tiene cuenta en OperativAI (portal_users.email es único global)."""


class FichaNoDisponible(Exception):
    """La ficha (de vendedor o de proveedor) se desactivó o ya quedó ligada a otra cuenta."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def enlace(token: str) -> str:
    # BASE_URL_FRONTEND es la URL pública explícita del portal; FRONTEND_ORIGINS
    # es una lista de CORS (puede traer localhost primero) y no sirve de enlace.
    base = settings.BASE_URL_FRONTEND
    return f"{base}/invitacion#t={token}"


_COLUMNAS = """
    i.id, i.email, i.role, i.vendedor_id, v.nombre AS vendedor_nombre,
    i.proveedor_id, p.nombre AS proveedor_nombre,
    i.creada_en, i.expira_en, (i.expira_en <= NOW()) AS vencida
"""

SELECT_INVITACION = f"""
    SELECT {_COLUMNAS}
    FROM invitaciones_equipo i
    LEFT JOIN vendedores  v ON v.id = i.vendedor_id
    LEFT JOIN proveedores p ON p.id = i.proveedor_id
"""


async def crear(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    email: str,
    role: str,
    vendedor_id: UUID | None,
    invitada_por: UUID,
    proveedor_id: UUID | None = None,
) -> tuple[asyncpg.Record, str]:
    """
    Crea la invitación y devuelve (fila, token en claro). Revoca antes la
    que siguiera viva para el mismo correo o la misma ficha: el enlace
    viejo deja de servir en ese momento.

    Las validaciones de negocio (ficha del tenant, correo libre) las hace
    el router antes, dentro de la misma transacción.
    """
    await conn.execute(
        """
        UPDATE invitaciones_equipo SET revocada_en = NOW()
         WHERE tenant_id = $1
           AND aceptada_en IS NULL AND revocada_en IS NULL
           AND (
                LOWER(email) = LOWER($2)
             OR ($3::uuid IS NOT NULL AND vendedor_id = $3)
             OR ($4::uuid IS NOT NULL AND proveedor_id = $4)
           )
        """,
        tenant_id,
        email,
        vendedor_id,
        proveedor_id,
    )

    token = secrets.token_urlsafe(32)
    invitacion_id = await conn.fetchval(
        """
        INSERT INTO invitaciones_equipo
            (tenant_id, email, role, vendedor_id, proveedor_id, token_hash, invitada_por, expira_en)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW() + make_interval(days => $8))
        RETURNING id
        """,
        tenant_id,
        email.lower(),
        role,
        vendedor_id,
        proveedor_id,
        _hash(token),
        invitada_por,
        VIGENCIA_DIAS,
    )
    fila = await conn.fetchrow(f"{SELECT_INVITACION} WHERE i.id = $1", invitacion_id)
    return fila, token


async def renovar(conn: asyncpg.Connection, invitacion_id: UUID, tenant_id: UUID) -> tuple[asyncpg.Record, str] | None:
    """
    Token y vigencia nuevos para una invitación viva (vencida incluida). El
    enlace anterior deja de servir. None si no existe en este negocio o ya
    se aceptó o revocó.
    """
    token = secrets.token_urlsafe(32)
    actualizada = await conn.fetchval(
        """
        UPDATE invitaciones_equipo
           SET token_hash = $3,
               creada_en  = NOW(),
               expira_en  = NOW() + make_interval(days => $4)
         WHERE id = $1 AND tenant_id = $2
           AND aceptada_en IS NULL AND revocada_en IS NULL
        RETURNING id
        """,
        invitacion_id,
        tenant_id,
        _hash(token),
        VIGENCIA_DIAS,
    )
    if actualizada is None:
        return None
    fila = await conn.fetchrow(f"{SELECT_INVITACION} WHERE i.id = $1", invitacion_id)
    return fila, token


async def mandar_correo(fila: asyncpg.Record, token: str, invitador: str, negocio: str) -> bool:
    """
    True si salió. Un fallo del SMTP no deshace la invitación: el dueño
    recibe el enlace en la respuesta y lo puede mandar por otro lado.
    """
    try:
        await enviar_invitacion(
            fila["email"],
            invitador=invitador,
            negocio=negocio,
            rol=ETIQUETA_ROL.get(fila["role"], fila["role"]),
            enlace=enlace(token),
            dias=VIGENCIA_DIAS,
        )
        return True
    except ErrorEnvioCorreo:
        log.warning("No salió el correo de la invitación %s", fila["id"])
        return False


@dataclass(frozen=True)
class InfoInvitacion:
    email: str
    role: str
    nombre_negocio: str
    vendedor_nombre: str | None
    proveedor_nombre: str | None
    expira_en: object


_SELECT_VIVA = """
    SELECT i.id, i.tenant_id, i.email, i.role, i.vendedor_id, i.proveedor_id, i.expira_en,
           t.name AS nombre_negocio, v.nombre AS vendedor_nombre, p.nombre AS proveedor_nombre
    FROM invitaciones_equipo i
    JOIN tenants t ON t.id = i.tenant_id
    LEFT JOIN vendedores  v ON v.id = i.vendedor_id
    LEFT JOIN proveedores p ON p.id = i.proveedor_id
    WHERE i.token_hash = $1
      AND i.aceptada_en IS NULL
      AND i.revocada_en IS NULL
      AND i.expira_en > NOW()
"""


async def leer(conn: asyncpg.Connection, token: str) -> InfoInvitacion:
    fila = await conn.fetchrow(_SELECT_VIVA, _hash(token))
    if fila is None:
        raise InvitacionInvalida()
    return InfoInvitacion(
        email=fila["email"],
        role=fila["role"],
        nombre_negocio=fila["nombre_negocio"],
        vendedor_nombre=fila["vendedor_nombre"],
        proveedor_nombre=fila["proveedor_nombre"],
        expira_en=fila["expira_en"],
    )


@dataclass(frozen=True)
class CuentaCreada:
    id: UUID
    tenant_id: UUID
    role: str


async def aceptar(
    conn: asyncpg.Connection,
    token: str,
    password_hash: str,
    terminos_version: str,
) -> CuentaCreada:
    """
    Crea la cuenta de la invitación. Tiene que correr dentro de una
    transacción: o quedan la cuenta, la ficha ligada y la invitación
    cerrada, o nada.
    """
    # FOR UPDATE: dos clics al mismo enlace no crean dos cuentas; el segundo
    # espera, y al seguir ya no la encuentra viva.
    fila = await conn.fetchrow(f"{_SELECT_VIVA} FOR UPDATE OF i", _hash(token))
    if fila is None:
        raise InvitacionInvalida()

    ocupado = await conn.fetchval(
        "SELECT 1 FROM portal_users WHERE LOWER(email) = LOWER($1)", fila["email"]
    )
    if ocupado:
        raise CorreoYaRegistrado()

    user_id = await conn.fetchval(
        """
        INSERT INTO portal_users
            (tenant_id, email, password_hash, full_name, role, is_active,
             terminos_aceptados_en, terminos_version, last_login_at)
        VALUES ($1, $2, $3, $4, $5, true, NOW(), $6, NOW())
        RETURNING id
        """,
        fila["tenant_id"],
        fila["email"],
        password_hash,
        fila["vendedor_nombre"] or fila["proveedor_nombre"],
        fila["role"],
        terminos_version,
    )

    if fila["vendedor_id"] is not None:
        # La ficha pudo cambiar desde que se invitó: desactivada, o ligada a
        # otra cuenta por otro camino. En ese caso no se crea nada.
        ligada = await conn.fetchval(
            """
            UPDATE vendedores SET portal_user_id = $1
             WHERE id = $2 AND tenant_id = $3 AND activo AND portal_user_id IS NULL
            RETURNING id
            """,
            user_id,
            fila["vendedor_id"],
            fila["tenant_id"],
        )
        if ligada is None:
            raise FichaNoDisponible()

    if fila["proveedor_id"] is not None:
        # Mismo criterio con la ficha del calendario.
        ligada = await conn.fetchval(
            """
            UPDATE proveedores SET portal_user_id = $1
             WHERE id = $2 AND tenant_id = $3 AND activo AND portal_user_id IS NULL
            RETURNING id
            """,
            user_id,
            fila["proveedor_id"],
            fila["tenant_id"],
        )
        if ligada is None:
            raise FichaNoDisponible()

    await conn.execute(
        "UPDATE invitaciones_equipo SET aceptada_en = NOW(), portal_user_id = $2 WHERE id = $1",
        fila["id"],
        user_id,
    )
    return CuentaCreada(id=user_id, tenant_id=fila["tenant_id"], role=fila["role"])
