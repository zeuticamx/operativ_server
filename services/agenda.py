"""
Agenda de ventas: tareas del CRM de campo + seguimientos del embudo.

Las dos fuentes siguen siendo de sus módulos (tareas.py, vendedores.py) y
cada una conserva sus permisos. Lo que vive acá es lo que comparten desde
que se ven en un mismo calendario:

  - las reglas de estado al cambiar una fecha (puras, se prueban sin BD)
  - el mapeo de cada fila a un AgendaItemOut
  - la bitácora de reprogramaciones (sql/39_agenda.sql)
  - el aviso personal al vendedor cuando otro le mueve un pendiente
"""

import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from realtime import broadcast_alerta, emitir_datos
from schemas import AgendaItemOut
from session import fetch_all, fetch_one

log = logging.getLogger("operativai.agenda")

_ZONA_POR_DEFECTO = "America/Mexico_City"

_DIAS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
_MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


# ============================================================
# Reglas puras
# ============================================================
def estado_tras_reprogramar(estado_actual: str, fecha_nueva: datetime, ahora: datetime) -> str:
    """
    El estado que le corresponde a una tarea cuando le cambian la fecha.

    Una completada sigue completada: cambiarle la fecha es corregir un dato,
    no reabrirla (para eso está POST /tareas/{id}/reabrir). Una abierta
    queda 'pendiente' si la nueva fecha es futura y 'vencida' si ya pasó —
    sin esto, arrastrar una vencida al jueves que viene la dejaba en rojo
    hasta que alguien la tocara a mano, y arrastrar una pendiente a ayer
    esperaba al job para pintarla.
    """
    if estado_actual == "completada":
        return "completada"
    return "pendiente" if fecha_nueva > ahora else "vencida"


def estado_seguimiento(fecha: datetime, ahora: datetime) -> str:
    """Un seguimiento no tiene columna de estado: se deduce de la fecha."""
    return "pendiente" if fecha > ahora else "vencida"


def fecha_legible(fecha: datetime, zona: str | None) -> str:
    """
    "jue 12 oct, 10:30" en la zona del negocio, para el texto de una alerta.

    Sin locale del sistema a propósito: el contenedor de producción no
    garantiza tener es_MX instalado, y un strftime("%a") en inglés en mitad
    de un aviso en español se ve peor que esta tablita.
    """
    try:
        tz = ZoneInfo(zona or _ZONA_POR_DEFECTO)
    except Exception:
        tz = ZoneInfo(_ZONA_POR_DEFECTO)
    local = fecha.astimezone(tz)
    return f"{_DIAS[local.weekday()]} {local.day} {_MESES[local.month - 1]}, {local:%H:%M}"


def item_de_tarea(fila: Mapping[str, Any]) -> AgendaItemOut:
    return AgendaItemOut(
        tipo="tarea",
        id=fila["id"],
        titulo=fila["titulo"],
        descripcion=fila["descripcion"],
        fecha=fila["fecha_programada"],
        estado=fila["estado"],
        vendedor_id=fila["vendedor_id"],
        vendedor_nombre=fila["vendedor_nombre"],
        cliente_id=fila["cliente_id"],
        cliente_nombre=fila["cliente_nombre_negocio"],
    )


def item_de_seguimiento(fila: Mapping[str, Any], ahora: datetime) -> AgendaItemOut:
    nombre = fila["cliente_nombre"] or fila["cliente_handle"]
    return AgendaItemOut(
        tipo="seguimiento",
        id=fila["id"],
        # La nota es el "qué hay que hacer"; sin nota, el título es el lead.
        titulo=fila["seguimiento_nota"] or f"Seguimiento a {nombre or 'cliente sin nombre'}",
        descripcion=None,
        fecha=fila["proximo_seguimiento"],
        estado=estado_seguimiento(fila["proximo_seguimiento"], ahora),
        vendedor_id=fila["vendedor_id"],
        vendedor_nombre=fila["vendedor_nombre"],
        cliente_id=fila["user_id"],
        cliente_nombre=nombre,
        etapa=fila["estado"],
    )


def ordenar(items: list[AgendaItemOut]) -> list[AgendaItemOut]:
    # El tipo como desempate: a la misma hora, una lista estable entre
    # recargas (el calendario no "baila" las tarjetas de lugar).
    return sorted(items, key=lambda i: (i.fecha, i.tipo, str(i.id)))


# ============================================================
# Lectura por rango
# ============================================================
# Un tope por fuente y no paginación: el calendario pide lo que ve (un mes
# a lo sumo), y un mes con más de mil pendientes ya no se puede leer en una
# grilla de todos modos.
LIMITE_POR_FUENTE = 1000


async def tareas_en_rango(
    tenant_id: UUID,
    vendedor_id: Optional[UUID],
    desde: datetime,
    hasta: datetime,
) -> list[asyncpg.Record]:
    return await fetch_all(
        """
        SELECT t.id, t.vendedor_id, t.cliente_id, t.titulo, t.descripcion,
               t.fecha_programada, t.estado,
               v.nombre AS vendedor_nombre,
               c.nombre_negocio AS cliente_nombre_negocio
        FROM tareas_seguimiento t
        JOIN vendedores v ON v.id = t.vendedor_id
        JOIN clientes   c ON c.id = t.cliente_id
        WHERE t.tenant_id = $1
          AND ($2::uuid IS NULL OR t.vendedor_id = $2)
          AND t.fecha_programada >= $3
          AND t.fecha_programada <  $4
        ORDER BY t.fecha_programada
        LIMIT $5
        """,
        tenant_id,
        vendedor_id,
        desde,
        hasta,
        LIMITE_POR_FUENTE,
    )


async def seguimientos_en_rango(
    tenant_id: UUID,
    vendedor_id: Optional[UUID],
    desde: datetime,
    hasta: datetime,
    estados_cerrados: list[str],
) -> list[asyncpg.Record]:
    """
    Solo leads abiertos: uno ganado o perdido ya no tiene a quién darle
    seguimiento, aunque la fecha se haya quedado guardada.
    """
    return await fetch_all(
        """
        SELECT p.id, p.user_id, p.vendedor_id, p.estado,
               p.proximo_seguimiento, p.seguimiento_nota,
               NULLIF(NULLIF(TRIM(u.display_name), ''), 'null') AS cliente_nombre,
               COALESCE(
                   NULLIF(NULLIF(TRIM(u.whatsapp_id), ''), 'null'),
                   NULLIF(NULLIF(TRIM(u.instagram_id), ''), 'null'),
                   NULLIF(NULLIF(TRIM(u.facebook_id), ''), 'null')
               ) AS cliente_handle,
               v.nombre AS vendedor_nombre
        FROM client_pipeline p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN vendedores v ON v.id = p.vendedor_id
        WHERE p.tenant_id = $1
          AND ($2::uuid IS NULL OR p.vendedor_id = $2)
          AND p.proximo_seguimiento >= $3
          AND p.proximo_seguimiento <  $4
          AND p.estado <> ALL($5::text[])
        ORDER BY p.proximo_seguimiento
        LIMIT $6
        """,
        tenant_id,
        vendedor_id,
        desde,
        hasta,
        estados_cerrados,
        LIMITE_POR_FUENTE,
    )


# ============================================================
# Bitácora
# ============================================================
async def registrar_reprogramacion(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    vendedor_id: Optional[UUID],
    fecha_anterior: Optional[datetime],
    fecha_nueva: Optional[datetime],
    actor_portal_user_id: Optional[UUID],
    actor_etiqueta: str,
    tarea_id: Optional[UUID] = None,
    client_pipeline_id: Optional[UUID] = None,
) -> bool:
    """
    Anota un cambio de fecha. Va dentro de la misma transacción que el
    UPDATE: no puede quedar una fecha movida sin su renglón.

    Devuelve False (y no escribe) si la fecha no cambió: un PUT que solo
    edita el título manda la misma fecha de vuelta, y eso no es una
    reprogramación.
    """
    if fecha_anterior == fecha_nueva:
        return False

    await conn.execute(
        """
        INSERT INTO agenda_reprogramaciones
            (tenant_id, tarea_id, client_pipeline_id, vendedor_id,
             fecha_anterior, fecha_nueva, actor_portal_user_id, actor_etiqueta)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        tenant_id,
        tarea_id,
        client_pipeline_id,
        vendedor_id,
        fecha_anterior,
        fecha_nueva,
        actor_portal_user_id,
        actor_etiqueta,
    )
    return True


_SELECT_REPROGRAMACIONES = """
    SELECT r.id, r.fecha_anterior, r.fecha_nueva, r.actor_etiqueta,
           v.nombre AS vendedor_nombre, r.creado_en
    FROM agenda_reprogramaciones r
    LEFT JOIN vendedores v ON v.id = r.vendedor_id
    WHERE r.tenant_id = $1 AND {filtro} = $2
    ORDER BY r.creado_en DESC
    LIMIT 100
"""


async def reprogramaciones_de_tarea(tenant_id: UUID, tarea_id: UUID) -> list[asyncpg.Record]:
    return await fetch_all(
        _SELECT_REPROGRAMACIONES.format(filtro="r.tarea_id"), tenant_id, tarea_id
    )


async def reprogramaciones_de_seguimiento(
    tenant_id: UUID, client_pipeline_id: UUID
) -> list[asyncpg.Record]:
    return await fetch_all(
        _SELECT_REPROGRAMACIONES.format(filtro="r.client_pipeline_id"),
        tenant_id,
        client_pipeline_id,
    )


# ============================================================
# Aviso al vendedor
# ============================================================
async def avisar_cambio_vendedores(
    tenant_id: UUID,
    vendedor_ids: list[Optional[UUID]],
    recurso: str = "cartera",
) -> None:
    """
    Evento de UI en vivo (sin alerta) para que el portal de cada vendedor
    afectado vuelva a pedir su cartera/agenda. Vendedores sin cuenta del
    portal se ignoran. Best-effort: se llama después del commit.
    """
    try:
        ids = [v for v in vendedor_ids if v is not None]
        filas = (
            await fetch_all(
                """
                SELECT portal_user_id FROM vendedores
                WHERE tenant_id = $1 AND id = ANY($2::uuid[]) AND portal_user_id IS NOT NULL
                """,
                tenant_id,
                ids,
            )
            if ids
            else []
        )
        await emitir_datos(tenant_id, recurso, [f["portal_user_id"] for f in filas])
    except Exception:
        log.exception("No se pudo avisar el cambio a los vendedores (tenant=%s)", tenant_id)


async def avisar_reprogramacion(
    tenant_id: UUID,
    *,
    vendedor_id: Optional[UUID],
    actor_portal_user_id: Optional[UUID],
    actor_etiqueta: str,
    titulo: str,
    fecha_nueva: Optional[datetime],
    datos: dict[str, Any],
    asignada: bool = False,
) -> None:
    """
    Alerta PERSONAL al vendedor dueño del pendiente cuando otro se lo movió
    o se lo acaba de asignar (`asignada`: tarea nueva o reasignada a él).

    Si se lo movió él mismo no hay nada que avisar. Tampoco si el vendedor
    no tiene cuenta del portal (o está desactivada): la alerta personal va
    a una room de usuario, y sin usuario no hay a quién.

    Best-effort: se llama después del commit y un fallo acá no deshace la
    reprogramación, que es lo que importa.
    """
    if vendedor_id is None:
        return
    try:
        fila = await fetch_one(
            """
            SELECT v.portal_user_id, ts.zona_horaria
            FROM vendedores v
            JOIN portal_users pu ON pu.id = v.portal_user_id
            LEFT JOIN tenant_servicios ts ON ts.tenant_id = v.tenant_id
            WHERE v.id = $1 AND v.tenant_id = $2
              AND v.activo AND pu.is_active
            """,
            vendedor_id,
            tenant_id,
        )
        if fila is None or fila["portal_user_id"] == actor_portal_user_id:
            return
        await emitir_datos(tenant_id, "agenda", [fila["portal_user_id"]])

        cuando = fecha_legible(fecha_nueva, fila["zona_horaria"]) if fecha_nueva else None
        if asignada:
            titulo_alerta = "📅 Tienes un pendiente nuevo"
            mensaje = f"{actor_etiqueta} te asignó «{titulo}»" + (f" para el {cuando}" if cuando else "")
        elif cuando is None:
            titulo_alerta = "📅 Te movieron un pendiente"
            mensaje = f"{actor_etiqueta} marcó como hecho «{titulo}»"
        else:
            titulo_alerta = "📅 Te movieron un pendiente"
            mensaje = f"{actor_etiqueta} movió «{titulo}» al {cuando}"

        await broadcast_alerta(
            tenant_id,
            tipo="agenda_reprogramada",
            titulo=titulo_alerta,
            mensaje=mensaje[:1000],
            datos={
                **datos,
                "fecha_nueva": fecha_nueva.isoformat() if fecha_nueva else None,
            },
            portal_user_id=fila["portal_user_id"],
        )
    except Exception:
        log.exception(
            "No se pudo avisar la reprogramación al vendedor %s (tenant=%s)",
            vendedor_id,
            tenant_id,
        )


# ============================================================
# Job: tareas vencidas
# ============================================================
async def marcar_tareas_vencidas() -> int:
    """
    Pasa a 'vencida' toda tarea 'pendiente' cuya fecha ya pasó. Devuelve
    cuántas marcó.

    El estado 'vencida' existía desde 07_crm_campo.sql pero nadie lo
    escribía: la app de vendedores y el portal mostraban como pendiente una
    tarea de hace un mes. Un solo UPDATE, sin bitácora: no es una
    reprogramación, la fecha no se movió.
    """
    filas = await fetch_all(
        """
        UPDATE tareas_seguimiento
           SET estado = 'vencida'
         WHERE estado = 'pendiente'
           AND fecha_programada < $1
        RETURNING id
        """,
        datetime.now(timezone.utc),
    )
    return len(filas)
