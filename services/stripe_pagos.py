"""
Cliente de Stripe: crear el Checkout y validar la firma de sus webhooks.

Dos modos de Checkout:
  - `payment`      -> compra de créditos (cargo único, precio de la base).
  - `subscription` -> contratar un plan, sobre el Price recurrente del plan
                      (`planes.stripe_price_id`). Stripe cobra solo cada mes y
                      avisa por webhook (ver services/stripe_suscripciones.py).

Sin SDK, con httpx directo, igual que Meta y Mercado Pago en este mismo
proyecto. La API de Stripe es **form-encoded**, no JSON: los campos
anidados viajan con notación de corchetes
(`line_items[0][price_data][currency]`), y por eso el payload se arma como
un dict plano de strings en vez de con `json=`.
"""

import hashlib
import hmac
import logging
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException, status

from config import settings

log = logging.getLogger("operativai.pagos.stripe")

STRIPE_API = "https://api.stripe.com"
TIMEOUT = httpx.Timeout(15.0)

# Cuánto puede desfasarse el reloj del webhook antes de rechazarlo. Los 5
# minutos son los que recomienda Stripe: sin este control, una petición
# firmada capturada hoy se podría reenviar mañana tal cual y seguiría
# validando (replay).
TOLERANCIA_FIRMA_SEGUNDOS = 300

# Monedas sin decimales: el "monto en la unidad mínima" ES el monto, no
# hay centavos que multiplicar. Con MXN no aplica, pero STRIPE_CURRENCY es
# configurable y equivocarse acá cobra 100 veces de más.
MONEDAS_SIN_DECIMALES = frozenset(
    {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf",
     "ugx", "vnd", "vuv", "xaf", "xof", "xpf"}
)


def de_unidad_minima(monto: int, moneda: str) -> Decimal:
    """La inversa de a_unidad_minima: 9999 (centavos) -> Decimal("99.99")."""
    if moneda.lower() in MONEDAS_SIN_DECIMALES:
        return Decimal(monto)
    return (Decimal(monto) / 100).quantize(Decimal("0.01"))


def a_unidad_minima(monto: Decimal, moneda: str) -> int:
    """
    Convierte 99.99 -> 9999 (centavos), que es como Stripe cobra.

    Decimal y no float a propósito: `int(99.99 * 100)` da 9998 en binario
    flotante, o sea un centavo regalado en cada cobro.
    """
    if moneda.lower() in MONEDAS_SIN_DECIMALES:
        return int(monto.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return int((monto * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _payload_checkout(
    transaccion_id: UUID, concepto: str, monto: Decimal, email: str
) -> dict[str, str]:
    """
    Arma el form-encoded de POST /v1/checkout/sessions.

    `client_reference_id` es el hilo que une la sesión de Stripe con nuestra
    fila: el webhook no trae tenant_id, lo reconstruye desde acá. Va además
    en metadata porque algunos eventos (los de charge/refund) no incluyen
    client_reference_id.
    """
    moneda = settings.STRIPE_CURRENCY

    return {
        "mode": "payment",
        "client_reference_id": str(transaccion_id),
        "customer_email": email,
        # {CHECKOUT_SESSION_ID} es una plantilla literal de Stripe: la
        # sustituye al redirigir. No la reemplaces por el id real acá — al
        # crear la sesión todavía no existe.
        "success_url": f"{settings.BASE_URL_FRONTEND}/pagos/exito?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{settings.BASE_URL_FRONTEND}/pagos/error",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": moneda,
        "line_items[0][price_data][unit_amount]": str(a_unidad_minima(monto, moneda)),
        "line_items[0][price_data][product_data][name]": concepto,
        "metadata[transaccion_id]": str(transaccion_id),
        # El PaymentIntent es lo que sobrevive a la sesión: un reembolso
        # llega como evento de charge y solo trae el intent, así que la
        # referencia tiene que estar también ahí.
        "payment_intent_data[metadata][transaccion_id]": str(transaccion_id),
    }


def _payload_suscripcion(
    transaccion_id: UUID,
    tenant_id: UUID,
    plan: str,
    price_id: str,
    email: str,
    customer_id: str | None,
) -> dict[str, str]:
    """
    Arma el form-encoded de POST /v1/checkout/sessions en modo
    `subscription`.

    El monto no viaja: lo pone el Price recurrente del plan. La metadata va
    en `subscription_data` para que quede pegada a la Subscription y
    Stripe la repita en cada factura — así cada evento de renovación, fallo
    o cancelación sabe de qué tenant es sin depender de nuestra base.
    """
    payload = {
        "mode": "subscription",
        "client_reference_id": str(transaccion_id),
        # Ver _payload_checkout sobre {CHECKOUT_SESSION_ID}.
        "success_url": f"{settings.BASE_URL_FRONTEND}/pagos/exito?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{settings.BASE_URL_FRONTEND}/pagos/error",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "metadata[transaccion_id]": str(transaccion_id),
        "subscription_data[metadata][transaccion_id]": str(transaccion_id),
        "subscription_data[metadata][tenant_id]": str(tenant_id),
        "subscription_data[metadata][plan]": plan,
    }
    # Un tenant que ya fue cliente reusa su Customer (tarjetas guardadas,
    # historial de facturas en un solo lugar). La primera vez se manda el
    # correo y Stripe crea el Customer — en modo subscription siempre lo
    # crea. `customer` y `customer_email` no pueden ir juntos.
    if customer_id:
        payload["customer"] = customer_id
    else:
        payload["customer_email"] = email
    return payload


async def crear_checkout_session(
    transaccion_id: UUID, concepto: str, monto: Decimal, email: str
) -> dict[str, Any]:
    """Checkout de un cargo único (compra de créditos). Ver _post_checkout."""
    return await _post_checkout(
        transaccion_id, _payload_checkout(transaccion_id, concepto, monto, email)
    )


async def crear_checkout_suscripcion(
    transaccion_id: UUID,
    tenant_id: UUID,
    plan: str,
    price_id: str,
    email: str,
    customer_id: str | None,
) -> dict[str, Any]:
    """Checkout de una suscripción recurrente. Ver _post_checkout."""
    return await _post_checkout(
        transaccion_id,
        _payload_suscripcion(transaccion_id, tenant_id, plan, price_id, email, customer_id),
    )


async def _post_checkout(transaccion_id: UUID, payload: dict[str, str]) -> dict[str, Any]:
    """
    Crea la Checkout Session hospedada y devuelve el JSON de Stripe.

    Lanza 502 si Stripe rechaza o no contesta: quien llama se encarga de
    cerrar la transacción pendiente.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
            respuesta = await cliente.post(
                f"{STRIPE_API}/v1/checkout/sessions",
                headers={
                    "Authorization": f"Bearer {settings.STRIPE_SECRET_KEY}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    # Si el POST se reintenta (timeout de red, deploy a
                    # medias), Stripe devuelve la MISMA sesión en vez de
                    # crear una segunda para el mismo cobro.
                    "Idempotency-Key": str(transaccion_id),
                },
                data=payload,
            )
        respuesta.raise_for_status()
        return respuesta.json()
    except httpx.HTTPError as e:
        # `str(e)` de un HTTPStatusError no trae el body, y ahí es donde
        # Stripe dice la causa real ("amount must be at least 10 mxn", una
        # moneda no habilitada en la cuenta, etc.).
        cuerpo = e.response.text if isinstance(e, httpx.HTTPStatusError) else ""
        log.error(
            "Stripe rechazó el Checkout (%s): %s | body=%s", transaccion_id, e, cuerpo
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo iniciar el pago con Stripe. Inténtalo de nuevo.",
        )


def firma_valida(cabecera: str | None, cuerpo_crudo: bytes) -> bool:
    """
    Valida el HMAC que manda Stripe en `Stripe-Signature`.

    La cabecera viene como "t=1704908010,v1=abc123...". Lo que se firma es
    exactamente `{t}.{cuerpo crudo}` con STRIPE_WEBHOOK_SECRET (whsec_...),
    que no es la secret key de la API.

    El cuerpo tiene que ser el de la petición **tal cual llegó**: volver a
    serializar el JSON cambia espacios y orden, y el HMAC deja de dar.

    Sin esto, cualquiera que descubra la URL puede mandar un
    checkout.session.completed inventado y regalarse créditos.
    """
    if not cabecera:
        return False

    ts = ""
    firmas: list[str] = []
    for trozo in cabecera.split(","):
        clave, _, valor = trozo.partition("=")
        clave, valor = clave.strip(), valor.strip()
        if clave == "t":
            ts = valor
        elif clave == "v1":
            # Puede venir más de una durante una rotación de secreto:
            # alcanza con que una valide.
            firmas.append(valor)

    if not ts or not firmas:
        return False

    try:
        emitido_en = int(ts)
    except ValueError:
        return False

    if abs(time.time() - emitido_en) > TOLERANCIA_FIRMA_SEGUNDOS:
        log.warning("Webhook de Stripe fuera de la ventana de tolerancia (ts=%s)", ts)
        return False

    esperado = hmac.new(
        settings.STRIPE_WEBHOOK_SECRET.encode(),
        b"%s.%s" % (ts.encode(), cuerpo_crudo),
        hashlib.sha256,
    ).hexdigest()

    # compare_digest y no ==: comparación en tiempo constante, para que no
    # se pueda ir adivinando la firma midiendo cuánto tarda la respuesta.
    return any(hmac.compare_digest(esperado, f) for f in firmas)
