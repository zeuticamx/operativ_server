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
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from services.acceso_pagos import acceso_pagos
from services.asignacion import asignar_vendedor_automatico
from deps import llamada_interna
from realtime import broadcast_alerta
from services import calendario
from services.calendario import actor_desde_chat, verificar_calendario_activo
from services.gerencia import registrar_uso_tokens
from services.notificaciones import notificar_vendedor_nuevo_lead
from services.pipeline import asignar_vendedor, get_or_create_pipeline, get_tenant_servicios
from schemas import (
    CancelarReservaEventoIn,
    ConsultarDisponibilidadIn,
    ConsultarDisponibilidadOut,
    ConversacionTransferidaIn,
    ConversacionTransferidaOut,
    CrearReservaEventoIn,
    CrearReservaEventoOut,
    MensajeEntranteIn,
    MensajeEntranteOut,
    ProveedorOut,
    ReservaOut,
    ServicioOut,
    SlotDisponibleOut,
    UsoTokensIn,
    UsoTokensOut,
)
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


# ============================================================
# Handoff a humano
# ============================================================
@router.post("/conversacion-transferida", response_model=ConversacionTransferidaOut)
async def conversacion_transferida(datos: ConversacionTransferidaIn):
    """
    n8n llama esto justo después de que `escalar_humano` marca la
    conversación como 'transferred' en Postgres (workflow escalar-humano).

    Ese UPDATE por sí solo es invisible para gerencia: el WebSocket en vivo
    solo lo dispara este proceso corriendo (ver realtime.broadcast_alerta),
    así que sin esta llamada nadie se entera hasta que alguien entra al
    portal a filtrar conversaciones por estado a mano.
    """
    detalle = [f"{datos.cliente_nombre or 'Un cliente'} pidió hablar con alguien del equipo por {datos.canal}"]
    if datos.motivo:
        detalle.append(f"— {datos.motivo}")

    await broadcast_alerta(
        datos.tenant_id,
        "conversacion_transferida",
        "Cliente esperando atención humana",
        " ".join(detalle),
        datos={
            "conversation_id": str(datos.conversation_id),
            "canal": datos.canal,
        },
    )
    return ConversacionTransferidaOut(registrado=True)


# ============================================================
# Calendarios: catálogo, disponibilidad y reservas para el agente de n8n
# ============================================================
@router.get("/calendario/servicios", response_model=list[ServicioOut])
async def calendario_servicios(tenant_id: UUID = Query(...)):
    """
    Catálogo de servicios que el agente puede ofrecer por chat. Solo
    activos -- mismo criterio que /calendario/proveedores.
    """
    await verificar_calendario_activo(tenant_id)
    servicios = await calendario.servicios_activos(tenant_id)
    return [ServicioOut(**vars(s)) for s in servicios]


@router.get("/calendario/proveedores", response_model=list[ProveedorOut])
async def calendario_proveedores(tenant_id: UUID = Query(...)):
    """Proveedores (barberos/estilistas) activos, para que el agente ofrezca a cuál agendar."""
    await verificar_calendario_activo(tenant_id)
    proveedores = await calendario.proveedores_activos(tenant_id)
    return [ProveedorOut(**vars(p)) for p in proveedores]


@router.post("/calendario/disponibilidad", response_model=ConsultarDisponibilidadOut)
async def calendario_disponibilidad(datos: ConsultarDisponibilidadIn):
    """
    Slots libres para que n8n se los ofrezca al cliente por chat.

    Igual que el resto de este router, `tenant_id` viaja en el body (n8n no
    tiene sesión de portal para ponerlo en la ruta).
    """
    await verificar_calendario_activo(datos.tenant_id)
    slots = await calendario.consultar_disponibilidad(
        datos.tenant_id,
        datos.servicio_id,
        datos.proveedor_id,
        datos.fecha_desde,
        datos.fecha_hasta,
    )
    return ConsultarDisponibilidadOut(
        calendario_activo=True,
        slots=[SlotDisponibleOut(**s) for s in slots],
    )


@router.post("/calendario/reservas", response_model=CrearReservaEventoOut)
async def calendario_crear_reserva(datos: CrearReservaEventoIn):
    """
    Crea una cita desde el chat. Idempotente: un reintento de n8n con el
    mismo `idempotency_key` no crea una segunda reserva, devuelve
    `duplicado=true` con la reserva que ya existía.
    """
    await verificar_calendario_activo(datos.tenant_id)

    reserva, motivo = await calendario.crear_reserva(
        datos.tenant_id,
        datos.proveedor_id,
        datos.servicio_id,
        datos.hora_inicio,
        user_id=datos.user_id,
        cliente_nombre=datos.cliente_nombre,
        cliente_telefono=datos.cliente_telefono,
        notas=datos.notas,
        actor=actor_desde_chat(datos.user_id, datos.cliente_nombre),
        idempotency_key=datos.idempotency_key,
    )

    if reserva is None:
        return CrearReservaEventoOut(creado=False, motivo_rechazo=motivo)

    if motivo == "duplicado":
        return CrearReservaEventoOut(creado=True, duplicado=True, reserva=ReservaOut(**vars(reserva)))

    # Fuera de la conexión que usó crear_reserva, y solo para el caso
    # realmente nuevo: un reintento duplicado no debe generar una segunda
    # alerta ni un segundo aviso por WebSocket.
    #
    # Este es el único punto que dispara "reserva_creada": una reserva
    # creada por el propio negocio desde el portal (walk-in/teléfono,
    # routers/calendario.py::crear_reserva_manual) no notifica a gerencia
    # de sí misma -- la alerta es para avisar de reservas que llegaron
    # solas por el chat, no para confirmar lo que gerencia ya sabe que hizo.
    servicio = await calendario.servicio_del_tenant(datos.servicio_id, datos.tenant_id)
    tenant_servicios = await get_tenant_servicios(datos.tenant_id)
    hora_local = reserva.hora_inicio.astimezone(ZoneInfo(tenant_servicios.zona_horaria))

    detalle = [f"{reserva.cliente_nombre or 'Cliente'} reservó {reserva.servicio_nombre} con {reserva.proveedor_nombre}"]
    detalle.append(f"el {hora_local.strftime('%d/%m')} a las {hora_local.strftime('%H:%M')}")
    if reserva.cliente_telefono:
        detalle.append(f"({reserva.cliente_telefono})")
    if servicio and servicio.precio is not None:
        detalle.append(f"— ${servicio.precio:.2f}")

    await broadcast_alerta(
        datos.tenant_id,
        "reserva_creada",
        "Nueva reserva",
        " ".join(detalle),
        datos={
            "reserva_id": str(reserva.id),
            "proveedor_id": str(reserva.proveedor_id),
            "hora_inicio": reserva.hora_inicio.isoformat(),
        },
    )
    return CrearReservaEventoOut(creado=True, reserva=ReservaOut(**vars(reserva)))


@router.post("/calendario/reservas/cancelar", response_model=ReservaOut)
async def calendario_cancelar_reserva(datos: CancelarReservaEventoIn):
    await verificar_calendario_activo(datos.tenant_id)

    reserva = await calendario.cancelar_reserva(
        datos.reserva_id, datos.tenant_id, datos.motivo, actor_desde_chat(None, None)
    )
    if reserva is None:
        raise HTTPException(status_code=404, detail="Reserva no encontrada")

    await broadcast_alerta(
        datos.tenant_id,
        "reserva_cancelada",
        "Reserva cancelada",
        f"Se canceló la cita de {reserva.cliente_nombre or 'un cliente'} con {reserva.proveedor_nombre}",
        datos={"reserva_id": str(reserva.id)},
    )
    return ReservaOut(**vars(reserva))
