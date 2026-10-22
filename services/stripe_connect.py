"""
Stripe Connect: cada tenant conecta su propia cuenta de Stripe (Express,
onboarding hospedado por Stripe) y cobra a SUS clientes con cargos directos
(la cuenta conectada es el comerciante, no la plataforma).

Sin SDK, con httpx y form-encoded, igual que services/stripe_pagos.py y
services/stripe_portal.py — reusa de ahí STRIPE_API, TIMEOUT y
a_unidad_minima/de_unidad_minima en vez de duplicarlos.

Mecanismo (nombres de parámetros verificados contra docs.stripe.com al
escribir este archivo — lo que importa conceptualmente es):

  1. POST /v2/core/accounts (dashboard=express, responsabilidades en Stripe,
     merchant + card_payments) -> crea la cuenta conectada. Accounts v2 porque
     v1 con `type` rechaza cuentas cuyas pérdidas asume la plataforma.
  2. POST /v2/core/account_links (use_case=account_onboarding, merchant) ->
     URL de onboarding hospedado, de un solo uso.
  3. Al completar onboarding, Stripe manda account.updated por webhook
     (charges_enabled, payouts_enabled, details_submitted,
     requirements.currently_due) -> sincroniza tenant_stripe_connect y
     enciende/apaga la tool del agente (herramientas_pagos_connect).
  4. Para cobrar: POST /v1/products, POST /v1/prices y
     POST /v1/payment_links, los tres con el header `Stripe-Account:
     acct_...` -- eso es lo que hace que el cargo sea DIRECTO (la cuenta
     conectada es el comerciante) y no "destination" ni "separate charges
     and transfers". `application_fee_amount` solo se manda si el fee está
     activo; `restrictions[completed_sessions][limit]=1` para que un link
     cobre una sola vez.
"""

import logging
from decimal import Decimal
from typing import Any, Awaitable, Callable
from uuid import UUID

import asyncpg
import httpx
from fastapi import HTTPException, status

from config import settings
from services import herramientas_pagos_connect
from services.stripe_pagos import STRIPE_API, TIMEOUT, a_unidad_minima, de_unidad_minima
from session import execute, fetch_one

log = logging.getLogger("operativai.pagos.stripe_connect")

# Accounts v2 (JSON) pide una versión de API explícita en cabecera. Es la
# versión de la especificación con la que se verificó el esquema; si Stripe
# la retira, cambiar solo esta constante.
STRIPE_VERSION_V2 = "2026-09-30.preview"

# País de las cuentas conectadas (identity.country en Accounts v2). El cobro
# por defecto es en MXN y acepta OXXO/SPEI, así que la plataforma es de México.
PAIS_CUENTAS_CONECTADAS = "MX"


class CuentaNoConectada(Exception):
    """El tenant no tiene Connect activo (sin cuenta, o sin charges_habilitado)."""


def _headers(cuenta_conectada: str | None = None, *, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {settings.STRIPE_SECRET_KEY}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if cuenta_conectada:
        # Opera SOBRE la cuenta del tenant: es lo que hace del cargo un
        # cargo directo en vez de uno sobre la cuenta de plataforma.
        headers["Stripe-Account"] = cuenta_conectada
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


async def _post(
    path: str,
    payload: dict[str, Any],
    *,
    cuenta_conectada: str | None = None,
    idempotency_key: str | None = None,
    v2: bool = False,
) -> dict[str, Any]:
    """
    v2=False: form-encoded, API v1 (productos, precios, links de cobro).
    v2=True: JSON con la versión de API v2 (cuentas y account links).
    """
    headers = _headers(cuenta_conectada, idempotency_key=idempotency_key)
    if v2:
        headers["Content-Type"] = "application/json"
        headers["Stripe-Version"] = STRIPE_VERSION_V2
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
            r = await cliente.post(
                f"{STRIPE_API}{path}",
                headers=headers,
                **({"json": payload} if v2 else {"data": payload}),
            )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        cuerpo = e.response.text if isinstance(e, httpx.HTTPStatusError) else ""
        log.error("Stripe Connect rechazó %s: %s | body=%s", path, e, cuerpo)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Stripe no pudo completar la operación. Inténtalo de nuevo.",
        )


async def _crear_cuenta_en_stripe() -> str:
    # Accounts v2: sin `type` (el v1 lo rechaza cuando la plataforma es la
    # que asume las pérdidas). `dashboard: express` mantiene el panel Express
    # que antes daba `type: express`; Stripe asume las pérdidas y las comisiones.
    # El vínculo con el tenant vive en tenant_stripe_connect, no en Stripe.
    datos = await _post(
        "/v2/core/accounts",
        {
            "dashboard": "express",
            "identity": {"country": PAIS_CUENTAS_CONECTADAS},
            "defaults": {
                "responsibilities": {
                    "fees_collector": "stripe",
                    "losses_collector": "stripe",
                },
            },
            "configuration": {
                "merchant": {"capabilities": {"card_payments": {"requested": True}}},
            },
        },
        v2=True,
    )
    cuenta_id = str(datos.get("id") or "")
    if not cuenta_id:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Stripe no devolvió una cuenta válida")
    return cuenta_id


async def crear_cuenta_express(tenant_id: UUID) -> str:
    """Idempotente: si el tenant ya tiene cuenta, la reusa en vez de crear otra."""
    existente = await fetch_one(
        "SELECT stripe_account_id FROM tenant_stripe_connect WHERE tenant_id = $1", tenant_id
    )
    if existente:
        return existente["stripe_account_id"]

    cuenta_id = await _crear_cuenta_en_stripe()
    await execute(
        """
        INSERT INTO tenant_stripe_connect (tenant_id, stripe_account_id)
        VALUES ($1, $2)
        ON CONFLICT (tenant_id) DO NOTHING
        """,
        tenant_id,
        cuenta_id,
    )
    return cuenta_id


async def crear_account_link(cuenta_id: str, *, refresh_url: str, return_url: str) -> str:
    # Account Links v2: el tipo de onboarding va dentro de `use_case`. No hay
    # lista de configuraciones: Stripe pide lo que la cuenta ya tiene (merchant).
    datos = await _post(
        "/v2/core/account_links",
        {
            "account": cuenta_id,
            "use_case": {
                "type": "account_onboarding",
                "account_onboarding": {
                    "refresh_url": refresh_url,
                    "return_url": return_url,
                },
            },
        },
        v2=True,
    )
    url = datos.get("url")
    if not url:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Stripe no devolvió la URL de onboarding")
    return url


async def iniciar_onboarding(tenant_id: UUID) -> str:
    """Crea la cuenta si falta y devuelve la URL de Account Link para redirigir ahí."""
    cuenta_id = await crear_cuenta_express(tenant_id)
    base = settings.BASE_URL_FRONTEND
    return await crear_account_link(
        cuenta_id,
        refresh_url=f"{base}/cobros/conectar?refresh=1",
        return_url=f"{base}/cobros/conectar?listo=1",
    )


async def consultar_cuenta(cuenta_id: str) -> dict[str, Any]:
    """GET /v1/accounts/{id}: estado fresco (lo usa el refresh manual del portal)."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
            r = await cliente.get(f"{STRIPE_API}/v1/accounts/{cuenta_id}", headers=_headers())
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        log.error("No se pudo consultar la cuenta %s: %s", cuenta_id, e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "No se pudo consultar Stripe")


async def obtener_estado(tenant_id: UUID) -> asyncpg.Record | None:
    return await fetch_one("SELECT * FROM tenant_stripe_connect WHERE tenant_id = $1", tenant_id)


async def _crear_payment_link_en_stripe(
    cuenta_id: str, descripcion: str, monto: Decimal, moneda: str, fee_bps: int, clave: str | None
) -> dict[str, Any]:
    producto = await _post(
        "/v1/products",
        {"name": descripcion},
        cuenta_conectada=cuenta_id,
        idempotency_key=f"{clave}:product" if clave else None,
    )
    monto_centavos = a_unidad_minima(monto, moneda)
    precio = await _post(
        "/v1/prices",
        {"product": producto["id"], "currency": moneda, "unit_amount": str(monto_centavos)},
        cuenta_conectada=cuenta_id,
        idempotency_key=f"{clave}:price" if clave else None,
    )
    payload = {
        "line_items[0][price]": precio["id"],
        "line_items[0][quantity]": "1",
        # Un link, un pago: así no se puede cobrar dos veces el mismo link.
        "restrictions[completed_sessions][limit]": "1",
    }
    fee_centavos = 0
    if fee_bps > 0:
        fee_centavos = monto_centavos * fee_bps // 10_000
        if fee_centavos > 0:
            payload["application_fee_amount"] = str(fee_centavos)
    link = await _post(
        "/v1/payment_links",
        payload,
        cuenta_conectada=cuenta_id,
        idempotency_key=f"{clave}:link" if clave else None,
    )
    return {
        "product_id": producto["id"],
        "price_id": precio["id"],
        "payment_link_id": link["id"],
        "url": link["url"],
        "fee_centavos": fee_centavos,
    }


async def crear_payment_link_cobro(
    tenant_id: UUID,
    descripcion: str,
    monto: Decimal,
    *,
    creado_por: UUID | None = None,
    conversation_id: UUID | None = None,
    idempotency_key: str | None = None,
) -> tuple[asyncpg.Record, bool]:
    """
    Crea un cobro nuevo (owner desde /cobros, o el agente desde una
    conversación). Devuelve (fila, duplicado). `duplicado=True` cuando
    `idempotency_key` ya existía: se devuelve el link anterior tal cual, sin
    tocar Stripe ni crear una fila nueva.

    Lanza `CuentaNoConectada` si el tenant no completó el onboarding.
    """
    if idempotency_key:
        existente = await fetch_one(
            "SELECT * FROM tenant_cobros_connect WHERE tenant_id = $1 AND idempotency_key = $2",
            tenant_id,
            idempotency_key,
        )
        if existente is not None:
            return existente, True

    estado = await obtener_estado(tenant_id)
    if estado is None or not estado["charges_habilitado"]:
        raise CuentaNoConectada()

    moneda = settings.STRIPE_CURRENCY
    fee_bps = settings.stripe_connect_fee_bps
    resultado = await _crear_payment_link_en_stripe(
        estado["stripe_account_id"], descripcion, monto, moneda, fee_bps, idempotency_key
    )
    fee_monto = de_unidad_minima(resultado["fee_centavos"], moneda) if resultado["fee_centavos"] else None

    fila = await fetch_one(
        """
        INSERT INTO tenant_cobros_connect
            (tenant_id, creado_por, conversation_id, descripcion, monto, moneda,
             stripe_product_id, stripe_price_id, stripe_payment_link_id, url_cobro,
             application_fee_bps, application_fee_monto, idempotency_key)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        RETURNING *
        """,
        tenant_id,
        creado_por,
        conversation_id,
        descripcion,
        monto,
        moneda,
        resultado["product_id"],
        resultado["price_id"],
        resultado["payment_link_id"],
        resultado["url"],
        fee_bps,
        fee_monto,
        idempotency_key,
    )
    return fila, False


async def desactivar_cobro(tenant_id: UUID, cobro_id: UUID) -> asyncpg.Record | None:
    """Desactiva el Payment Link en Stripe y lo marca localmente. 404 lo decide el caller."""
    fila = await fetch_one(
        "SELECT * FROM tenant_cobros_connect WHERE id = $1 AND tenant_id = $2", cobro_id, tenant_id
    )
    if fila is None or fila["estado"] != "activo":
        return fila

    estado = await obtener_estado(tenant_id)
    if estado is not None and fila["stripe_payment_link_id"]:
        await _post(
            f"/v1/payment_links/{fila['stripe_payment_link_id']}",
            {"active": "false"},
            cuenta_conectada=estado["stripe_account_id"],
        )
    return await fetch_one(
        """
        UPDATE tenant_cobros_connect
           SET estado = 'desactivado', actualizado_en = NOW()
         WHERE id = $1
        RETURNING *
        """,
        cobro_id,
    )


# ------------------------------------------------------------
# Webhook: eventos de una cuenta CONECTADA (evento["account"] presente).
# ------------------------------------------------------------
async def procesar_cuenta_actualizada(cuenta: dict[str, Any]) -> None:
    """account.updated: sincroniza tenant_stripe_connect y la tool del agente."""
    cuenta_id = str(cuenta.get("id") or "")
    if not cuenta_id:
        return

    requisitos = (cuenta.get("requirements") or {}).get("currently_due") or []
    charges_habilitado = bool(cuenta.get("charges_enabled"))

    fila = await fetch_one(
        """
        UPDATE tenant_stripe_connect
           SET charges_habilitado = $2,
               payouts_habilitado = $3,
               detalles_enviados  = $4,
               requisitos_pendientes = $5::jsonb,
               actualizado_en = NOW()
         WHERE stripe_account_id = $1
        RETURNING tenant_id, charges_habilitado
        """,
        cuenta_id,
        charges_habilitado,
        bool(cuenta.get("payouts_enabled")),
        bool(cuenta.get("details_submitted")),
        [str(r) for r in requisitos],
    )
    if fila is None:
        log.warning("account.updated de una cuenta que no es nuestra: %s", cuenta_id)
        return

    await herramientas_pagos_connect.sincronizar(fila["tenant_id"], fila["charges_habilitado"])


async def procesar_checkout_connect_completado(sesion: dict[str, Any]) -> None:
    """
    checkout.session.completed en una cuenta conectada: la Checkout Session
    que arma un Payment Link cuando el cliente paga. Se correlaciona por
    `payment_link` (Stripe lo manda en la sesión) contra
    tenant_cobros_connect.stripe_payment_link_id -- no por el id de la
    sesión, que no se conoce hasta este mismo evento.
    """
    if str(sesion.get("payment_status")) != "paid":
        return
    link_id = sesion.get("payment_link")
    if not link_id:
        return

    await execute(
        """
        UPDATE tenant_cobros_connect
           SET estado = 'pagado',
               stripe_checkout_session_id = COALESCE(stripe_checkout_session_id, $2),
               stripe_payment_intent_id   = COALESCE($3, stripe_payment_intent_id),
               pagado_en = NOW(),
               actualizado_en = NOW()
         WHERE stripe_payment_link_id = $1
           AND estado != 'pagado'
        """,
        str(link_id),
        str(sesion.get("id")) if sesion.get("id") else None,
        str(sesion["payment_intent"]) if sesion.get("payment_intent") else None,
    )


PROCESADORES: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {
    "account.updated": procesar_cuenta_actualizada,
    "checkout.session.completed": procesar_checkout_connect_completado,
}
