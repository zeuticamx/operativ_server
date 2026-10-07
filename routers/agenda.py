"""
Agenda de ventas: el calendario del portal (/vendedores/agenda para el
negocio, /mi-cartera para el vendedor).

Junta en una sola lectura dos módulos que se encienden por separado:

  tareas        CRM de campo (herramienta 'crm_campo')
  seguimientos  embudo de chat (herramienta 'vendedores' + el módulo
                encendido en tenant_servicios)

Uno apagado o fuera del plan no es un error de la agenda: solo no aporta
pendientes, y la respuesta dice cuál entró. Por eso este router no cuelga
`requiere_herramienta` a nivel de APIRouter como tareas.py — lo decide cada
endpoint con la fuente que toca.

Mover una TAREA de fecha sigue siendo PUT /api/tareas/{id} (ahí queda la
bitácora). Lo que vive acá es la escritura que no tenía casa: poner o mover
la fecha de seguimiento de un lead. Va aparte de /api/pipeline/* a
propósito: ese contrato lo consume la app de vendedores y no se toca.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from routers.vendedores import modulo_actual
from schemas import AgendaItemOut, AgendaOut, ReprogramacionOut, SeguimientoIn
from services import agenda
from services.acceso_plan import acceso_plan
from services.crm import AccesoCRM, acceso_crm
from services.pipeline import get_tenant_servicios
from services.pipeline_estados import ESTADOS_CERRADOS
from session import fetch_one, transaccion

router = APIRouter(prefix="/agenda", tags=["agenda"])

# Una vista de mes con sus semanas de borde son 42 días; esto deja margen
# para una vista de lista más larga sin abrir la puerta a pedir un año.
_RANGO_MAXIMO = timedelta(days=100)


def _con_zona(fecha: datetime) -> datetime:
    """Una fecha sin zona se toma como UTC, igual que la guarda asyncpg."""
    return fecha if fecha.tzinfo is not None else fecha.replace(tzinfo=timezone.utc)


@router.get("", response_model=AgendaOut)
async def listar(
    desde: datetime = Query(..., description="Inclusivo"),
    hasta: datetime = Query(..., description="Exclusivo"),
    vendedor_id: UUID | None = Query(None, description="Solo el negocio; un vendedor ve lo suyo"),
    acceso: AccesoCRM = Depends(acceso_crm),
):
    """
    Los pendientes del rango, de las dos fuentes, en orden de fecha.

    El vendedor siempre ve solo lo suyo (el `vendedor_id` del query se
    ignora), igual que en GET /tareas.
    """
    desde, hasta = _con_zona(desde), _con_zona(hasta)
    if hasta <= desde:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'hasta' tiene que ser posterior a 'desde'",
        )
    if hasta - desde > _RANGO_MAXIMO:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"El rango no puede pasar de {_RANGO_MAXIMO.days} días",
        )

    filtro_vendedor = acceso.vendedor_id if acceso.es_vendedor else vendedor_id

    plan = await acceso_plan(acceso.tenant_id)
    servicios = await get_tenant_servicios(acceso.tenant_id)
    con_tareas = plan.permite("crm_campo")
    con_seguimientos = plan.permite("vendedores") and servicios.gestion_vendedores_activo

    ahora = datetime.now(timezone.utc)
    items: list[AgendaItemOut] = []

    if con_tareas:
        filas = await agenda.tareas_en_rango(acceso.tenant_id, filtro_vendedor, desde, hasta)
        items.extend(agenda.item_de_tarea(f) for f in filas)

    if con_seguimientos:
        filas = await agenda.seguimientos_en_rango(
            acceso.tenant_id, filtro_vendedor, desde, hasta, list(ESTADOS_CERRADOS)
        )
        items.extend(agenda.item_de_seguimiento(f, ahora) for f in filas)

    return AgendaOut(
        tareas_disponibles=con_tareas,
        seguimientos_disponibles=con_seguimientos,
        items=agenda.ordenar(items),
    )


# ============================================================
# Seguimientos del embudo
# ============================================================
_SELECT_LEAD = """
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
"""


def _exigir_dueno(acceso: AccesoCRM, vendedor_id: UUID | None) -> None:
    """
    403 y no 404 cuando un vendedor toca el lead de un compañero: mismo
    criterio que _solo_lo_suyo en routers/vendedores.py.
    """
    if acceso.es_vendedor and vendedor_id != acceso.vendedor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Ese lead es de otro vendedor",
        )


@router.put("/seguimientos/{user_id}", response_model=AgendaItemOut | None)
async def agendar_seguimiento(
    user_id: UUID,
    datos: SeguimientoIn,
    _: UUID = Depends(modulo_actual),
    acceso: AccesoCRM = Depends(acceso_crm),
):
    """
    Pone, mueve o quita (`fecha: null` = hecho) el próximo seguimiento de
    un lead. Responde el pendiente tal como queda en la agenda, o null si
    se quitó.

    `nota` ausente conserva la que había; vacía la borra. Al quitar la
    fecha se va también la nota: era la de ese seguimiento.

    No toca `actualizado_en` del embudo: esa columna es "cuándo se movió de
    etapa" y de ella cuelgan la alerta de lead sin actividad y las métricas.
    Agendar una llamada no es mover al lead.
    """
    async with transaccion() as conn:
        # FOR UPDATE OF p: dos personas arrastrando el mismo lead a la vez
        # no pueden dejar la bitácora con una fecha_anterior que no existió.
        actual = await conn.fetchrow(
            f"{_SELECT_LEAD} WHERE p.tenant_id = $1 AND p.user_id = $2 FOR UPDATE OF p",
            acceso.tenant_id,
            user_id,
        )
        if actual is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Este cliente no está en el embudo",
            )
        _exigir_dueno(acceso, actual["vendedor_id"])

        if datos.fecha is not None and actual["estado"] in ESTADOS_CERRADOS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El lead ya está cerrado: no hay seguimiento que agendar",
            )

        if datos.fecha is None:
            nota = None
        elif "nota" in datos.model_fields_set:
            nota = (datos.nota or "").strip() or None
        else:
            nota = actual["seguimiento_nota"]

        await conn.execute(
            """
            UPDATE client_pipeline
               SET proximo_seguimiento = $3, seguimiento_nota = $4
             WHERE id = $1 AND tenant_id = $2
            """,
            actual["id"],
            acceso.tenant_id,
            datos.fecha,
            nota,
        )
        fila = await conn.fetchrow(f"{_SELECT_LEAD} WHERE p.id = $1", actual["id"])

        cambio = await agenda.registrar_reprogramacion(
            conn,
            tenant_id=acceso.tenant_id,
            client_pipeline_id=actual["id"],
            vendedor_id=actual["vendedor_id"],
            fecha_anterior=actual["proximo_seguimiento"],
            fecha_nueva=fila["proximo_seguimiento"],
            actor_portal_user_id=acceso.portal_user_id,
            actor_etiqueta=acceso.etiqueta,
        )

    item = (
        agenda.item_de_seguimiento(fila, datetime.now(timezone.utc))
        if fila["proximo_seguimiento"] is not None
        else None
    )

    if cambio:
        await agenda.avisar_reprogramacion(
            acceso.tenant_id,
            vendedor_id=actual["vendedor_id"],
            actor_portal_user_id=acceso.portal_user_id,
            actor_etiqueta=acceso.etiqueta,
            titulo=item.titulo if item else (actual["seguimiento_nota"] or "un seguimiento"),
            fecha_nueva=fila["proximo_seguimiento"],
            datos={"tipo": "seguimiento", "cliente_id": str(user_id)},
            # Primera vez que se agenda: para el vendedor es un pendiente nuevo.
            asignada=actual["proximo_seguimiento"] is None,
        )

    return item


@router.get("/seguimientos/{user_id}/reprogramaciones", response_model=list[ReprogramacionOut])
async def reprogramaciones_seguimiento(
    user_id: UUID,
    _: UUID = Depends(modulo_actual),
    acceso: AccesoCRM = Depends(acceso_crm),
):
    """Cada cambio de fecha del seguimiento de un lead, el más reciente primero."""
    lead = await fetch_one(
        "SELECT id, vendedor_id FROM client_pipeline WHERE tenant_id = $1 AND user_id = $2",
        acceso.tenant_id,
        user_id,
    )
    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Este cliente no está en el embudo",
        )
    _exigir_dueno(acceso, lead["vendedor_id"])

    filas = await agenda.reprogramaciones_de_seguimiento(acceso.tenant_id, lead["id"])
    return [ReprogramacionOut(**dict(f)) for f in filas]
