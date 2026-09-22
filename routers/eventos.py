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

from services.acceso_pagos import acceso_pagos
from services.asignacion import asignar_vendedor_automatico
from deps import llamada_interna
from services.gerencia import registrar_uso_tokens
from services.notificaciones import notificar_vendedor_nuevo_lead
from services.pipeline import asignar_vendedor, get_or_create_pipeline, get_tenant_servicios
from schemas import MensajeEntranteIn, MensajeEntranteOut, UsoTokensIn, UsoTokensOut
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

    `agente_ia_activo` no es solo el flag guardado en tenant_servicios: se
    apaga igual si al tenant se le venció la suscripción y ya no le quedan
    créditos (ver services/acceso_pagos.py). n8n no sabe nada de pagos —
    todo lo que tiene para decidir si llama al nodo del agente es este
    booleano, así que el bloqueo por falta de pago tiene que viajar disfrazado
    de "agente apagado".
    """
    servicios = await get_tenant_servicios(tenant_id)
    acceso = await acceso_pagos(tenant_id)
    agente_ia_activo = servicios.agente_ia_activo and acceso.permitido

    if not servicios.gestion_vendedores_activo:
        return MensajeEntranteOut(
            gestion_vendedores_activo=False,
            agente_ia_activo=agente_ia_activo,
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
    #
    # `agente_ia_activo` (el efectivo, no el guardado): si el agente está
    # bloqueado por falta de pago, nadie le va a contestar a este cliente, y
    # el vendedor tiene que enterarse igual que si el agente estuviera
    # apagado a propósito.
    if asignado_ahora and vendedor_id is not None and not agente_ia_activo:
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
        agente_ia_activo=agente_ia_activo,
        pipeline_id=embudo.id,
        vendedor_id=vendedor_id,
        asignado_ahora=asignado_ahora,
    )


@router.post("/mensaje-entrante", response_model=MensajeEntranteOut)
async def mensaje_entrante(datos: MensajeEntranteIn):
    return await on_mensaje_entrante(datos.tenant_id, datos.user_id, datos.mensaje)


@router.post("/uso-tokens", response_model=UsoTokensOut)
async def uso_tokens(datos: UsoTokensIn):
    """
    n8n reporta lo que gastó una llamada al modelo.

    Lo consume el panel de plataforma (/api/gerencia/consumo). Se llama
    DESPUÉS de que el modelo respondió, no antes: lo que se mide es el
    consumo real, no el estimado.

    Va suelto y no colgado de /mensaje-entrante a propósito. Aquel se llama
    una vez por mensaje entrante, antes de saber si el agente va a
    contestar; una respuesta puede terminar siendo varias llamadas al
    modelo (la del agente más las herramientas que use), y cada una tiene
    su propio consumo.

    Con `idempotency_key`, un nodo reintentado no cuenta dos veces: la
    respuesta trae `duplicado=true` y n8n puede seguir tranquilo.
    """
    registrado = await registrar_uso_tokens(
        datos.tenant_id,
        conversation_id=datos.conversation_id,
        origen=datos.origen,
        modelo=datos.modelo,
        tokens_entrada=datos.tokens_entrada,
        tokens_salida=datos.tokens_salida,
        costo_usd=datos.costo_usd,
        idempotency_key=datos.idempotency_key,
    )
    return UsoTokensOut(registrado=registrado, duplicado=not registrado)
