"""
Stripe del lado del cliente ya existente: Customer Portal y facturas abiertas.

  - `crear_sesion_portal`: sesión del Customer Portal hospedado por Stripe,
    donde el dueño cancela o gestiona su plan. La cancelación vuelve por el
    webhook de siempre (`customer.subscription.updated` -> cancela_al_vencer,
    services/stripe_suscripciones.py), así que acá no se toca la base.
  - `tiene_facturas_abiertas`: si el Customer debe algo. Lo usa el borrado de
    cuenta (services/eliminacion_cuenta.py) para no dejar adeudos sin negocio.

El portal se configura en el Dashboard de Stripe (Settings -> Billing ->
Customer portal): qué se puede hacer ahí (cancelar, cambiar tarjeta, cambiar
de plan) se decide allá, no en código.

Sin SDK, con httpx y form-encoded, igual que services/stripe_pagos.py.
"""

import logging

import httpx

from config import settings
from services.stripe_pagos import STRIPE_API, TIMEOUT

log = logging.getLogger("operativai.pagos.stripe_portal")


class StripeNoDisponible(Exception):
    """Stripe no contestó o rechazó la llamada; el detalle va al log."""


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.STRIPE_SECRET_KEY}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


async def crear_sesion_portal(customer_id: str, return_url: str) -> str:
    """URL de una sesión nueva del Customer Portal para `customer_id`."""
    if not settings.STRIPE_SECRET_KEY:
        raise StripeNoDisponible("STRIPE_SECRET_KEY sin configurar")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
            r = await cliente.post(
                f"{STRIPE_API}/v1/billing_portal/sessions",
                headers=_headers(),
                data={"customer": customer_id, "return_url": return_url},
            )
        r.raise_for_status()
        url = r.json().get("url")
    except (httpx.HTTPError, ValueError) as e:
        cuerpo = e.response.text if isinstance(e, httpx.HTTPStatusError) else ""
        # El caso típico de un 400 acá es el portal sin configurar en el
        # Dashboard ("No configuration provided...").
        log.error("Stripe rechazó la sesión del portal (%s): %s | body=%s", customer_id, e, cuerpo)
        raise StripeNoDisponible(str(e)) from e
    if not url:
        raise StripeNoDisponible("Stripe no devolvió la URL del portal")
    return url


async def tiene_facturas_abiertas(customer_id: str) -> bool:
    """
    True si el Customer tiene alguna factura 'open' (emitida y sin pagar):
    una renovación que falló y Stripe sigue reintentando, o que dejó de
    reintentar sin anularla. Lanza StripeNoDisponible si no se puede saber:
    quien llama decide fallar cerrado.
    """
    if not settings.STRIPE_SECRET_KEY:
        raise StripeNoDisponible("STRIPE_SECRET_KEY sin configurar")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
            r = await cliente.get(
                f"{STRIPE_API}/v1/invoices",
                headers=_headers(),
                params={"customer": customer_id, "status": "open", "limit": "1"},
            )
        r.raise_for_status()
        data = r.json().get("data")
    except (httpx.HTTPError, ValueError) as e:
        log.error("No se pudieron consultar las facturas de %s: %s", customer_id, e)
        raise StripeNoDisponible(str(e)) from e
    return bool(data)
