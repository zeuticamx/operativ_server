"""
Job de background: pausa las suscripciones que vencieron sin renovarse.

Hoy no hay cobro recurrente automático (el checkout es por preferencia
suelta, no por preapproval de Mercado Pago): "renovar" es que el dueño
vuelva a pagar desde /suscripcion. Este job no cobra nada ni reintenta un
cargo — solo refleja en `estado` lo que ya es cierto (que pasó la fecha de
renovación y nadie pagó de nuevo), para que services/acceso_pagos.py tenga
de dónde leer sin tener que recalcular la fecha en cada request.

Cuando el dueño sí vuelve a pagar, _procesar_pago_aprobado (routers/pagos.py)
hace el UPSERT que deja `estado = 'activa'` de nuevo — este job y ese código
nunca escriben al mismo tiempo la misma fila en direcciones opuestas: uno
pausa por vencimiento, el otro reactiva por pago aprobado.
"""

import logging

from session import execute

log = logging.getLogger("operativai.jobs.pagos")

JOB_ID = "pausar_suscripciones_vencidas"

_PAUSAR_VENCIDAS = """
    UPDATE tenant_subscriptions
       SET estado = 'pausada', updated_at = NOW()
     WHERE estado = 'activa'
       AND fecha_renovacion IS NOT NULL
       AND fecha_renovacion < NOW()
"""


async def job_pausar_suscripciones_vencidas() -> None:
    """Una pasada sobre todas las suscripciones activas. Idempotente: correrlo
    dos veces seguidas no hace nada la segunda vez (el WHERE ya no matchea)."""
    resultado = await execute(_PAUSAR_VENCIDAS)
    # asyncpg devuelve "UPDATE <n>"; se parsea para loguear cuántas cayeron
    # sin pedir otra consulta aparte.
    try:
        pausadas = int(resultado.split()[-1])
    except (ValueError, IndexError):
        pausadas = 0

    if pausadas:
        log.info("Suscripciones pausadas por vencimiento: %s", pausadas)
