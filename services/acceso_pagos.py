"""
Gate de acceso por pagos: agente de IA y CRM de vendedores se apagan
cuando al tenant le faltan las DOS cosas a la vez — crédito disponible y
suscripción vigente. Cualquiera de las dos alcanza para seguir operando
(ver el docstring de routers/pagos.py sobre el modelo híbrido).

Un tenant que nunca contrató nada (sin fila en tenant_subscriptions ni en
tenant_credits) NO se bloquea acá: exigir pago desde el día uno a todo el
que nunca pagó es una decisión de producto que nadie tomó (¿hay período de
prueba? ¿plan gratuito?), y bloquearlo de oficio rompería cualquier tenant
de desarrollo o de una demo que hoy funciona sin pasar por /api/pagos. Lo
que sí se bloquea es el caso pedido: alguien que TUVO una suscripción y se
le venció sin renovar, y además ya no le quedan créditos.

Quién pasa la suscripción de 'activa' a 'pausada' cuando vence es
jobs/pagos_background.py, no este módulo: acá solo se lee el estado que ya
quedó escrito.
"""

from dataclasses import dataclass
from uuid import UUID

from session import fetch_one


@dataclass
class AccesoPagos:
    permitido: bool
    suscripcion_activa: bool
    tiene_creditos: bool


async def acceso_pagos(tenant_id: UUID) -> AccesoPagos:
    fila = await fetch_one(
        """
        SELECT
            s.estado AS estado_suscripcion,
            c.creditos_disponibles
        FROM tenants t
        LEFT JOIN tenant_subscriptions s ON s.tenant_id = t.id
        LEFT JOIN tenant_credits      c ON c.tenant_id = t.id
        WHERE t.id = $1
        """,
        tenant_id,
    )

    # Tenant inexistente: no es este módulo el que tiene que decidir qué
    # hacer con eso — que falle donde corresponda (404 de tenant_actual, etc).
    if fila is None:
        return AccesoPagos(permitido=True, suscripcion_activa=False, tiene_creditos=False)

    suscripcion_activa = fila["estado_suscripcion"] == "activa"
    tiene_creditos = (fila["creditos_disponibles"] or 0) > 0

    # Ninguna fila en ninguna de las dos tablas: nunca pasó por pagos. No es
    # el caso de "se le venció y no renovó" que se pidió bloquear.
    tuvo_algo_alguna_vez = (
        fila["estado_suscripcion"] is not None or fila["creditos_disponibles"] is not None
    )

    permitido = (not tuvo_algo_alguna_vez) or suscripcion_activa or tiene_creditos

    return AccesoPagos(
        permitido=permitido,
        suscripcion_activa=suscripcion_activa,
        tiene_creditos=tiene_creditos,
    )
