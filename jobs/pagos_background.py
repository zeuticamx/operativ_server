"""
Job de background: pausa las suscripciones que vencieron sin renovarse.

Con Stripe el plan es una suscripción recurrente: Stripe cobra solo y cada
cobro exitoso extiende `fecha_renovacion` (services/stripe_suscripciones.py).
Este job no cobra nada ni reintenta un cargo — solo refleja en `estado` lo
que ya es cierto (que pasó la fecha de renovación sin un cobro exitoso),
para que services/acceso_pagos.py tenga de dónde leer sin tener que
recalcular la fecha en cada request.

Es también el que aplica la pausa "diferida" de dos casos del webhook, que a
propósito no pausan en el momento para respetar los días ya pagados:
  - cobro de renovación fallido: sigue vigente hasta fecha_renovacion;
  - suscripción cancelada con días restantes: ídem.
En esos dos casos (y solo en esos: una prueba de gerencia que vence no es
noticia) se avisa a gerencia de la pausa.

Cuando el dueño sí paga (o un reintento de Stripe sale bien),
activar_suscripcion (services/pagos.py) hace el UPSERT que deja
`estado = 'activa'` de nuevo — este job y ese código nunca escriben al mismo
tiempo la misma fila en direcciones opuestas: uno pausa por vencimiento, el
otro reactiva por pago aprobado.
"""

import logging

from services.notificaciones_gerencia import EventoSuscripcion, notificar_evento_suscripcion
from session import fetch_all

log = logging.getLogger("operativai.jobs.pagos")

JOB_ID = "pausar_suscripciones_vencidas"

_PAUSAR_VENCIDAS = """
    UPDATE tenant_subscriptions
       SET estado = 'pausada', updated_at = NOW()
     WHERE estado = 'activa'
       AND fecha_renovacion IS NOT NULL
       AND fecha_renovacion < NOW()
    RETURNING tenant_id, plan, fecha_renovacion, cancela_al_vencer,
              intentos_fallidos, stripe_customer_id, stripe_subscription_id
"""


def _evento_de_pausa(f) -> EventoSuscripcion | None:
    """El aviso a gerencia de una fila recién pausada, o None si no aplica."""
    if f["cancela_al_vencer"]:
        motivo = "Venció la suscripción cancelada"
    elif f["intentos_fallidos"] and f["intentos_fallidos"] > 0:
        motivo = f"Venció sin que Stripe lograra cobrar ({f['intentos_fallidos']} intento(s) fallido(s))"
    else:
        return None
    return EventoSuscripcion(
        tipo="pausada",
        tenant_id=f["tenant_id"],
        plan=f["plan"],
        fecha_renovacion=f["fecha_renovacion"],
        motivo=motivo,
        accion=(
            "Se pausó: el agente y las herramientas del plan quedan bloqueados "
            "(salvo que tenga créditos) hasta que vuelva a pagar."
        ),
        stripe_customer_id=f["stripe_customer_id"],
        stripe_subscription_id=f["stripe_subscription_id"],
    )


async def job_pausar_suscripciones_vencidas() -> None:
    """Una pasada sobre todas las suscripciones activas. Idempotente: correrlo
    dos veces seguidas no hace nada la segunda vez (el WHERE ya no matchea),
    así que tampoco avisa dos veces."""
    pausadas = await fetch_all(_PAUSAR_VENCIDAS)

    if pausadas:
        log.info("Suscripciones pausadas por vencimiento: %s", len(pausadas))

    for fila in pausadas:
        evento = _evento_de_pausa(fila)
        if evento is not None:
            # notificar_evento_suscripcion no lanza: un SMTP caído no frena
            # el resto de las pausas.
            await notificar_evento_suscripcion(evento)
