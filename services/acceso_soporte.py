"""
Acceso de soporte con escritura a la cuenta de un negocio
(sql/40_acceso_soporte_escritura.sql).

Una sesión de "ver como" es de solo lectura. Para que soporte pueda
corregir algo por el cliente, el dueño tiene que autorizarlo:

  1. `solicitar`: soporte pide permiso con un motivo. Se avisa al dueño por
     alerta personal en su portal y por correo (con enlace de un solo uso).
  2. `aprobar` / `rechazar`: el dueño responde, desde el portal o desde el
     enlace del correo. Al aprobar elige cuánto dura (15, 30 o 60 min).
  3. `revocar`: el dueño la corta cuando quiera, ya aprobada.

La concesión vive en BD y no en el token: `concesion_vigente` la consulta
deps._validar_impersonacion en cada petición, así que revocar o vencerse
surte efecto en la siguiente llamada.

Reglas:
  - Solo cargos de gerencia_users listados en ACCESO_SOPORTE_CARGOS pueden
    pedirla.
  - El negocio necesita un dueño o superadmin activo que pueda responder.
  - Una solicitud viva (pendiente o aprobada) por gerente y negocio.
  - Pendiente: vive ACCESO_SOPORTE_VIGENCIA_MINUTOS. Se compara con el reloj
    de Postgres, sin job que la limpie.
  - El token del correo es de un solo uso (la transición pendiente ->
    aprobada/rechazada es un UPDATE condicionado) y en BD solo está su hash.
  - Aprobar o rechazar una solicitud que ya no está pendiente devuelve el
    estado en que quedó, sin error ni efecto doble (idempotente).
"""

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

import asyncpg

from config import settings
from realtime import broadcast_alerta, emitir_datos
from services.correo import ErrorEnvioCorreo, enviar_solicitud_acceso_soporte
from services.gerencia import registrar_auditoria
from session import fetch_all, fetch_one, fetch_value, transaccion

log = logging.getLogger(__name__)

DURACIONES_PERMITIDAS = (15, 30, 60)

# Estado "efectivo": lo que se ve, considerando vencimientos que no
# escriben nada en BD.
Estado = Literal["pendiente", "aprobada", "rechazada", "revocada", "cancelada", "vencida", "terminada"]

# Rutas (prefijo) que NO se pueden tocar desde "ver como" ni con escritura
# autorizada: credenciales, identidad, equipo, dinero y borrado de cuenta.
# Se comparan contra request.url.path. Una lista de prohibidos es aceptable
# acá (a diferencia de la de solo lectura) porque lo que olvide quedar fuera
# ya requiere que el dueño haya autorizado escribir, y lo sensible conocido
# está cubierto; ver tests/test_acceso_soporte.py.
RUTAS_SENSIBLES = (
    "/api/auth",  # contraseña, correo, sesiones, invitaciones
    "/api/cuenta",  # eliminar la cuenta del negocio
    "/api/equipo",  # invitar/quitar gente y cambiar roles
    "/api/pagos",  # cobros, suscripción, portal de facturación
    "/api/canales",  # tokens y credenciales de Meta/WhatsApp
    "/api/perfil",  # datos personales del dueño
    "/api/onboarding",  # aplicarlo otorga un plan de prueba: lo decide el dueño
    # Responder solicitudes es del dueño de verdad: con esto una sesión de
    # ver-como no puede aprobarse a sí misma. Las dos rutas de soporte
    # (solicitar/cancelar) se permiten antes en deps._validar_impersonacion.
    "/api/acceso-soporte",
)


def ruta_sensible(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in RUTAS_SENSIBLES)


class SolicitudInvalida(Exception):
    """Regla de negocio incumplida; `codigo` es el HTTP status sugerido."""

    def __init__(self, codigo: int, detalle: str) -> None:
        super().__init__(detalle)
        self.codigo = codigo
        self.detalle = detalle


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def enlace(token: str) -> str:
    # En el fragmento (#t=): no viaja al servidor del portal ni queda en sus
    # logs de acceso. Mismo criterio que services/invitaciones.enlace.
    return f"{settings.BASE_URL_FRONTEND}/acceso-soporte#t={token}"


_SELECT = """
    SELECT s.id, s.tenant_id, t.name AS tenant_nombre, s.gerente_id, s.gerente_email,
           s.motivo, s.estado, s.creada_en, s.expira_en, s.resuelta_en,
           s.resuelta_por, s.canal, s.duracion_min, s.concede_hasta,
           (s.estado = 'pendiente' AND s.expira_en <= NOW()) AS vencida,
           (s.estado = 'aprobada' AND s.concede_hasta <= NOW()) AS terminada
    FROM acceso_soporte_solicitudes s
    JOIN tenants t ON t.id = s.tenant_id
"""


def estado_efectivo(fila: asyncpg.Record) -> Estado:
    if fila["vencida"]:
        return "vencida"
    if fila["terminada"]:
        return "terminada"
    return fila["estado"]


# ------------------------------------------------------------
# Lectura
# ------------------------------------------------------------
async def concesion_vigente(gerente_id: UUID, tenant_id: UUID) -> Optional[datetime]:
    """
    Hasta cuándo puede escribir este gerente en este negocio, o None si no
    puede. Es la consulta que corre en cada petición de una sesión de "ver
    como": una sola fila por índice parcial.
    """
    return await fetch_value(
        """
        SELECT concede_hasta
        FROM acceso_soporte_solicitudes
        WHERE tenant_id = $1 AND gerente_id = $2
          AND estado = 'aprobada' AND concede_hasta > NOW()
        """,
        tenant_id,
        gerente_id,
    )


async def ultima_de_gerente(gerente_id: UUID, tenant_id: UUID) -> Optional[asyncpg.Record]:
    return await fetch_one(
        f"{_SELECT} WHERE s.tenant_id = $1 AND s.gerente_id = $2 "
        "ORDER BY s.creada_en DESC LIMIT 1",
        tenant_id,
        gerente_id,
    )


async def listar_del_negocio(tenant_id: UUID, limite: int = 20) -> list[asyncpg.Record]:
    """Para el dueño: las solicitudes recientes de su negocio, nunca de otro."""
    return await fetch_all(
        f"{_SELECT} WHERE s.tenant_id = $1 ORDER BY s.creada_en DESC LIMIT $2",
        tenant_id,
        limite,
    )


async def leer_del_negocio(solicitud_id: UUID, tenant_id: UUID) -> Optional[asyncpg.Record]:
    return await fetch_one(f"{_SELECT} WHERE s.id = $1 AND s.tenant_id = $2", solicitud_id, tenant_id)


async def leer_por_token(token: str) -> Optional[asyncpg.Record]:
    return await fetch_one(f"{_SELECT} WHERE s.token_hash = $1", _hash(token))


async def _responsables(tenant_id: UUID) -> list[asyncpg.Record]:
    """Quienes pueden aprobar: dueño(s) y superadmin activos del negocio."""
    return await fetch_all(
        """
        SELECT id, email FROM portal_users
        WHERE tenant_id = $1 AND is_active AND role IN ('owner', 'superadmin')
        ORDER BY created_at
        """,
        tenant_id,
    )


async def gerente_puede_solicitar(email: str) -> bool:
    cargo = await fetch_value(
        "SELECT cargo FROM gerencia_users WHERE LOWER(email) = LOWER($1)", email
    )
    return cargo is not None and cargo.strip().lower() in settings.ACCESO_SOPORTE_CARGOS


# ------------------------------------------------------------
# Solicitar
# ------------------------------------------------------------
async def solicitar(
    tenant_id: UUID, gerente_id: UUID, gerente_email: str, motivo: str
) -> asyncpg.Record:
    if not await gerente_puede_solicitar(gerente_email):
        raise SolicitudInvalida(403, "Tu cargo no puede pedir permiso de edición")

    responsables = await _responsables(tenant_id)
    if not responsables:
        raise SolicitudInvalida(
            409, "El negocio no tiene un dueño activo que pueda autorizar la edición"
        )

    token = secrets.token_urlsafe(32)
    async with transaccion() as conn:
        # Las que ya no sirven (pendiente vencida, aprobada terminada) se
        # cierran para liberar el índice de "una viva por gerente".
        await conn.execute(
            """
            UPDATE acceso_soporte_solicitudes
               SET estado = 'cancelada', resuelta_en = COALESCE(resuelta_en, NOW()),
                   concede_hasta = NULL
             WHERE tenant_id = $1 AND gerente_id = $2
               AND ((estado = 'pendiente' AND expira_en <= NOW())
                 OR (estado = 'aprobada' AND concede_hasta <= NOW()))
            """,
            tenant_id,
            gerente_id,
        )
        try:
            solicitud_id = await conn.fetchval(
                """
                INSERT INTO acceso_soporte_solicitudes
                    (tenant_id, gerente_id, gerente_email, motivo, token_hash, expira_en)
                VALUES ($1, $2, $3, $4, $5, NOW() + make_interval(mins => $6))
                RETURNING id
                """,
                tenant_id,
                gerente_id,
                gerente_email,
                motivo.strip(),
                _hash(token),
                settings.ACCESO_SOPORTE_VIGENCIA_MINUTOS,
            )
        except asyncpg.UniqueViolationError:
            raise SolicitudInvalida(
                409, "Ya hay una solicitud pendiente o aprobada para este negocio"
            ) from None
        await registrar_auditoria(
            actor_email=gerente_email,
            actor_portal_user_id=gerente_id,
            accion="acceso_escritura_solicitado",
            tenant_id=tenant_id,
            detalle={"motivo": motivo.strip(), "solicitud_id": str(solicitud_id)},
            conn=conn,
        )

    fila = await fetch_one(f"{_SELECT} WHERE s.id = $1", solicitud_id)
    assert fila is not None
    await _avisar_a_responsables(fila, token, responsables)
    return fila


async def _avisar_a_responsables(
    fila: asyncpg.Record, token: str, responsables: list[asyncpg.Record]
) -> None:
    """
    Alerta personal en el portal y correo a cada responsable. Best-effort:
    la solicitud ya quedó creada, y la alerta del portal es la vía
    principal; un fallo del SMTP solo se registra.
    """
    tenant_id = fila["tenant_id"]
    for r in responsables:
        try:
            await broadcast_alerta(
                tenant_id,
                "solicitud_escritura",
                "🔐 Soporte pide permiso para editar tu cuenta",
                f"{fila['gerente_email']} quiere editar tu cuenta: {fila['motivo'][:300]}",
                {"solicitud_id": str(fila["id"])},
                portal_user_id=r["id"],
            )
        except Exception:
            log.exception("No se pudo crear la alerta de acceso de soporte (%s)", fila["id"])
        try:
            await enviar_solicitud_acceso_soporte(
                r["email"],
                gerente=fila["gerente_email"],
                negocio=fila["tenant_nombre"],
                motivo=fila["motivo"],
                enlace=enlace(token),
                minutos=settings.ACCESO_SOPORTE_VIGENCIA_MINUTOS,
            )
        except ErrorEnvioCorreo:
            log.warning("No salió el correo de acceso de soporte %s", fila["id"])
    await emitir_datos(tenant_id, "acceso_soporte", [r["id"] for r in responsables])


# ------------------------------------------------------------
# Responder (dueño)
# ------------------------------------------------------------
async def _responder(
    *,
    solicitud_id: Optional[UUID] = None,
    tenant_id: Optional[UUID] = None,
    token: Optional[str] = None,
    aprobar: bool,
    duracion_min: Optional[int],
    quien: str,
    canal: Literal["portal", "correo"],
    actor_id: Optional[UUID],
) -> Optional[asyncpg.Record]:
    """
    Transición pendiente -> aprobada/rechazada, atómica. La solicitud se
    identifica por (id + tenant) desde el portal o por el token del correo.
    Devuelve la fila como quedó (si ya no estaba pendiente, tal cual está:
    idempotente) o None si no existe.
    """
    if aprobar and duracion_min not in DURACIONES_PERMITIDAS:
        raise SolicitudInvalida(
            422, f"La duración debe ser una de {', '.join(map(str, DURACIONES_PERMITIDAS))} minutos"
        )

    if token is not None:
        filtro, clave = "s.token_hash = $6", (_hash(token),)
    else:
        filtro, clave = "s.id = $6 AND s.tenant_id = $7", (solicitud_id, tenant_id)

    async with transaccion() as conn:
        cambio = await conn.fetchrow(
            f"""
            UPDATE acceso_soporte_solicitudes s
               SET estado = $1::varchar,
                   resuelta_en = NOW(), resuelta_por = $2, canal = $3,
                   duracion_min = $4::int,
                   concede_hasta = CASE WHEN $5::boolean
                                        THEN NOW() + make_interval(mins => $4::int) END
             WHERE {filtro} AND s.estado = 'pendiente' AND s.expira_en > NOW()
            RETURNING s.id, s.tenant_id, s.gerente_id, s.gerente_email, s.motivo
            """,
            "aprobada" if aprobar else "rechazada",
            quien,
            canal,
            duracion_min if aprobar else None,
            aprobar,
            *clave,
        )
        if cambio is not None:
            await registrar_auditoria(
                actor_email=quien,
                actor_portal_user_id=actor_id,
                accion="acceso_escritura_aprobado" if aprobar else "acceso_escritura_rechazado",
                tenant_id=cambio["tenant_id"],
                detalle={
                    "solicitud_id": str(cambio["id"]),
                    "gerente": cambio["gerente_email"],
                    "canal": canal,
                    "minutos": duracion_min if aprobar else None,
                },
                conn=conn,
            )

    fila = await (
        leer_por_token(token) if token is not None else leer_del_negocio(solicitud_id, tenant_id)
    )
    if fila is not None and cambio is not None:
        await _avisar_respuesta(fila)
    return fila


async def aprobar(
    solicitud_id: UUID, tenant_id: UUID, duracion_min: int, quien: str, actor_id: UUID
) -> Optional[asyncpg.Record]:
    # El tenant va en el filtro: una solicitud de otro negocio es "no existe".
    if await leer_del_negocio(solicitud_id, tenant_id) is None:
        return None
    return await _responder(
        solicitud_id=solicitud_id,
        tenant_id=tenant_id,
        aprobar=True,
        duracion_min=duracion_min,
        quien=quien,
        canal="portal",
        actor_id=actor_id,
    )


async def rechazar(
    solicitud_id: UUID, tenant_id: UUID, quien: str, actor_id: UUID
) -> Optional[asyncpg.Record]:
    if await leer_del_negocio(solicitud_id, tenant_id) is None:
        return None
    return await _responder(
        solicitud_id=solicitud_id,
        tenant_id=tenant_id,
        aprobar=False,
        duracion_min=None,
        quien=quien,
        canal="portal",
        actor_id=actor_id,
    )


async def responder_por_token(
    token: str, *, aprobar_: bool, duracion_min: Optional[int]
) -> Optional[asyncpg.Record]:
    """Desde el enlace del correo. `quien` es el dueño del negocio de la solicitud."""
    fila = await leer_por_token(token)
    if fila is None:
        return None
    responsables = await _responsables(fila["tenant_id"])
    if not responsables:
        raise SolicitudInvalida(409, "El negocio ya no tiene un dueño activo")
    return await _responder(
        token=token,
        aprobar=aprobar_,
        duracion_min=duracion_min,
        quien=responsables[0]["email"],
        canal="correo",
        actor_id=responsables[0]["id"],
    )


async def revocar(
    solicitud_id: UUID, tenant_id: UUID, quien: str, actor_id: UUID
) -> Optional[asyncpg.Record]:
    """Corta una concesión aprobada. Si ya no estaba aprobada, no cambia nada."""
    async with transaccion() as conn:
        cambio = await conn.fetchrow(
            """
            UPDATE acceso_soporte_solicitudes
               SET estado = 'revocada', concede_hasta = NULL,
                   resuelta_en = NOW(), resuelta_por = $3
             WHERE id = $1 AND tenant_id = $2 AND estado = 'aprobada'
            RETURNING id, tenant_id, gerente_email
            """,
            solicitud_id,
            tenant_id,
            quien,
        )
        if cambio is not None:
            await registrar_auditoria(
                actor_email=quien,
                actor_portal_user_id=actor_id,
                accion="acceso_escritura_revocado",
                tenant_id=tenant_id,
                detalle={"solicitud_id": str(solicitud_id), "gerente": cambio["gerente_email"]},
                conn=conn,
            )
    fila = await leer_del_negocio(solicitud_id, tenant_id)
    if fila is not None and cambio is not None:
        await _avisar_respuesta(fila)
    return fila


# ------------------------------------------------------------
# Cancelar (soporte)
# ------------------------------------------------------------
async def cancelar(tenant_id: UUID, gerente_id: UUID) -> Optional[asyncpg.Record]:
    """Soporte retira su solicitud pendiente o suelta la edición aprobada."""
    cambio = await fetch_one(
        """
        UPDATE acceso_soporte_solicitudes
           SET estado = 'cancelada', concede_hasta = NULL, resuelta_en = NOW()
         WHERE tenant_id = $1 AND gerente_id = $2 AND estado IN ('pendiente', 'aprobada')
        RETURNING id
        """,
        tenant_id,
        gerente_id,
    )
    fila = await ultima_de_gerente(gerente_id, tenant_id)
    if fila is not None and cambio is not None:
        await _avisar_respuesta(fila)
    return fila


async def _avisar_respuesta(fila: asyncpg.Record) -> None:
    """Refresco en vivo: el dueño ve el cambio y el banner de soporte también."""
    responsables = await _responsables(fila["tenant_id"])
    await emitir_datos(fila["tenant_id"], "acceso_soporte", [r["id"] for r in responsables])


@dataclass(frozen=True)
class InfoEnlace:
    tenant_nombre: str
    gerente_email: str
    motivo: str
    estado: Estado
    expira_en: datetime


def info_enlace(fila: asyncpg.Record) -> InfoEnlace:
    return InfoEnlace(
        tenant_nombre=fila["tenant_nombre"],
        gerente_email=fila["gerente_email"],
        motivo=fila["motivo"],
        estado=estado_efectivo(fila),
        expira_en=fila["expira_en"],
    )
