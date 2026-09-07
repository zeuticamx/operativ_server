"""
Punto de integración con el agente de IA que corre en n8n.

Este endpoint NO decide si el agente contesta. Solo gestiona el embudo y
devuelve los flags del tenant; con `agente_ia_activo` en la mano, el propio
workflow de n8n elige si sigue hacia el nodo del agente. La decisión vive
allá para no partir el control del flujo en dos lugares.

Los workflows existentes (canal-entrada-universal, escalar-humano,
ejecutar-herramienta-tenant) no cambian: esto se llama antes, justo después
de que n8n resuelve el tenant.
"""

import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends

from asignacion import asignar_vendedor_automatico
from deps import llamada_interna
from notificaciones import notificar_vendedor_nuevo_lead
from pipeline import asignar_vendedor, get_or_create_pipeline, get_tenant_servicios
from schemas import MensajeEntranteIn, MensajeEntranteOut
from session import transaccion

log = logging.getLogger("operativai.eventos")

# Todo el router exige la cabecera X-Internal-Token: lo llama n8n, que no
# tiene sesión de portal y por lo tanto no puede mandar el JWT de
# portal_users. Ver deps.llamada_interna.
router = APIRouter(
    prefix="/eventos",
    tags=["eventos"],
    dependencies=[Depends(llamada_interna)],
)


async def on_mensaje_entrante(
    tenant_id: UUID,
    user_id: UUID,
    mensaje: dict[str, Any],
) -> MensajeEntranteOut:
    """
    Qué hace el módulo de vendedores cuando entra un mensaje.

    Con el módulo apagado no toca nada y solo informa los flags: un tenant
    que únicamente usa el agente no paga ni una escritura por esto.

    Con el módulo encendido, asegura que el cliente esté en el embudo y le
    busca vendedor si todavía no tiene. Que no haya vendedores activos no es
    un error: el lead queda registrado con vendedor_id NULL y aparece en el
    panel de gerencia como pendiente de asignar.

    El aviso al vendedor sale solo si el agente está apagado. Con el agente
    encendido el cliente ya está siendo atendido, y el vendedor entra
    después mirando su cartera, no con un empujón por cada mensaje.

    Nunca se reasigna un lead que ya tiene dueño: un cliente que vuelve a
    escribir sigue siendo del mismo vendedor.
    """
    servicios = await get_tenant_servicios(tenant_id)

    if not servicios.gestion_vendedores_activo:
        return MensajeEntranteOut(
            gestion_vendedores_activo=False,
            agente_ia_activo=servicios.agente_ia_activo,
        )

    asignado_ahora = False
    vendedor_id: Optional[UUID] = None

    async with transaccion() as conn:
        embudo = await get_or_create_pipeline(tenant_id, user_id, conn)
        vendedor_id = embudo.vendedor_id

        if embudo.vendedor_id is None:
            elegido = await asignar_vendedor_automatico(tenant_id, conn=conn)
            if elegido is not None:
                await asignar_vendedor(
                    embudo.id,
                    elegido,
                    conn,
                    nota="Asignación automática por mensaje entrante",
                )
                vendedor_id = elegido
                asignado_ahora = True

    # El aviso va FUERA de la transacción: es una llamada de red y no debe
    # tener abierta una transacción de Postgres mientras espera. Si falla, el
    # lead ya quedó asignado igual.
    if asignado_ahora and vendedor_id is not None and not servicios.agente_ia_activo:
        try:
            await notificar_vendedor_nuevo_lead(
                vendedor_id, user_id, mensaje, tenant_id=tenant_id
            )
        except Exception:
            # Un aviso que no sale no puede tumbar el mensaje entrante del
            # cliente: n8n necesita su respuesta para seguir el workflow.
            log.exception("Falló el aviso de lead nuevo al vendedor %s", vendedor_id)

    return MensajeEntranteOut(
        gestion_vendedores_activo=True,
        agente_ia_activo=servicios.agente_ia_activo,
        pipeline_id=embudo.id,
        vendedor_id=vendedor_id,
        asignado_ahora=asignado_ahora,
    )


@router.post("/mensaje-entrante", response_model=MensajeEntranteOut)
async def mensaje_entrante(datos: MensajeEntranteIn):
    return await on_mensaje_entrante(datos.tenant_id, datos.user_id, datos.mensaje)
