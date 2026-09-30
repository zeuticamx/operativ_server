"""
Tests de la suscripción recurrente de Stripe (services/stripe_suscripciones.py)
y de los avisos a gerencia (services/notificaciones_gerencia.py).

Lo que se cubre:

  1. El checkout: mode=subscription con el Price del plan, reuso del
     Customer y los 409 (plan sin Price, suscripción ya viva).
  2. Los lectores de Invoice/Subscription, en el formato viejo y en el de la
     API "basil" (2025-03-31+).
  3. El webhook, evento por evento: alta, renovación, cobro fallido,
     cancelación programada/revertida, baja con y sin días restantes — y
     que un evento repetido por Stripe no duplique ni el efecto ni el aviso.
  4. La pausa diferida del job (cobro fallido / baja con días restantes) y
     su aviso.
  5. Los avisos en sí: correo a cada gerencia_users, alerta abierta solo
     para lo que requiere acción, y que un SMTP caído no rompa nada.

En los tests del webhook `notificar_evento_suscripcion` se reemplaza por un
capturador: lo que se prueba ahí es QUÉ se avisa y cuándo. Cómo se avisa
(correo + alerta) se prueba aparte, al final.

La API de Stripe no se llama nunca.
"""

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

import jobs.pagos_background as pagos_background
import routers.pagos_stripe as pagos_stripe
import services.notificaciones_gerencia as notificaciones_gerencia
from config import settings
from services import stripe_pagos
from services.correo import ErrorEnvioCorreo
from services.notificaciones_gerencia import (
    EventoSuscripcion,
    armar_correo,
    notificar_evento_suscripcion,
)
from services.stripe_suscripciones import leer_factura, leer_suscripcion
from session import execute, fetch_all, fetch_one, fetch_value

SECRETO = "whsec_secreto-de-prueba"
AHORA = datetime.now(timezone.utc).replace(microsecond=0)


# ============================================================
# Helpers y fixtures
# ============================================================
def firmar(cuerpo: bytes) -> str:
    ts = int(time.time())
    firma = hmac.new(SECRETO.encode(), b"%d.%s" % (ts, cuerpo), hashlib.sha256).hexdigest()
    return f"t={ts},v1={firma}"


@pytest.fixture
def con_secreto(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", SECRETO)


@pytest.fixture
def avisos(monkeypatch):
    """Captura los avisos a gerencia del webhook y del job en vez de mandarlos."""
    capturados: list[EventoSuscripcion] = []

    async def capturar(evento: EventoSuscripcion) -> int:
        capturados.append(evento)
        return 1

    monkeypatch.setattr(pagos_stripe, "notificar_evento_suscripcion", capturar)
    monkeypatch.setattr(pagos_background, "notificar_evento_suscripcion", capturar)
    return capturados


async def enviar(http_client, tipo: str, objeto: dict) -> int:
    cuerpo = json.dumps(
        {"id": f"evt_{uuid4().hex[:12]}", "type": tipo, "data": {"object": objeto}}
    ).encode()
    respuesta = await http_client.post(
        "/api/pagos/stripe/webhook",
        content=cuerpo,
        headers={"stripe-signature": firmar(cuerpo), "content-type": "application/json"},
    )
    return respuesta.status_code


def factura(
    *,
    tenant_id,
    sub_id: str,
    invoice_id: str,
    billing_reason: str = "subscription_cycle",
    periodo_fin: datetime | None = None,
    centavos: int = 19900,
    plan: str = "pro",
    price_id: str | None = "price_test_pro",
    transaccion_id=None,
    intento: int = 0,
    proximo_intento: datetime | None = None,
    formato: str = "basil",
) -> dict:
    """Una Invoice como la manda Stripe, en el formato pedido."""
    periodo_fin = periodo_fin or AHORA + timedelta(days=30)
    metadata = {"tenant_id": str(tenant_id), "plan": plan}
    if transaccion_id:
        metadata["transaccion_id"] = str(transaccion_id)

    linea: dict = {"period": {"start": int(AHORA.timestamp()), "end": int(periodo_fin.timestamp())}}
    obj: dict = {
        "id": invoice_id,
        "object": "invoice",
        "customer": "cus_test_1",
        "currency": "mxn",
        "amount_paid": centavos,
        "amount_due": centavos,
        "billing_reason": billing_reason,
        "attempt_count": intento,
        "next_payment_attempt": int(proximo_intento.timestamp()) if proximo_intento else None,
    }
    if formato == "basil":
        if price_id:
            linea["pricing"] = {"price_details": {"price": price_id}}
        obj["parent"] = {
            "type": "subscription_details",
            "subscription_details": {"subscription": sub_id, "metadata": metadata},
        }
    else:
        if price_id:
            linea["price"] = {"id": price_id}
        obj["subscription"] = sub_id
        obj["subscription_details"] = {"metadata": metadata}
        obj["payment_intent"] = f"pi_{invoice_id}"
    obj["lines"] = {"data": [linea]}
    return obj


def suscripcion_stripe(sub_id: str, **extra) -> dict:
    return {"id": sub_id, "object": "subscription", "customer": "cus_test_1", **extra}


async def fila_suscripcion(
    tenant_id,
    *,
    sub_id: str | None = "sub_test_1",
    estado: str = "activa",
    fecha_renovacion: datetime | None = None,
    intentos: int = 0,
    plan: str = "pro",
    customer_id: str | None = "cus_test_1",
    cancelada_en: datetime | None = None,
) -> None:
    await execute(
        """
        INSERT INTO tenant_subscriptions
            (tenant_id, plan, estado, precio_monthly, fecha_renovacion,
             intentos_fallidos, stripe_customer_id, stripe_subscription_id,
             cancelada_en)
        VALUES ($1, $2, $3, 199.00, $4, $5, $6, $7, $8)
        """,
        tenant_id,
        plan,
        estado,
        fecha_renovacion or AHORA + timedelta(days=10),
        intentos,
        customer_id,
        sub_id,
        cancelada_en,
    )


async def sub_de(tenant_id):
    return await fetch_one("SELECT * FROM tenant_subscriptions WHERE tenant_id = $1", tenant_id)


# ============================================================
# 1. Checkout
# ============================================================
def test_payload_de_suscripcion_usa_el_price_y_deja_la_metadata_en_la_subscription(monkeypatch):
    monkeypatch.setattr(settings, "BASE_URL_FRONTEND", "https://app.operativai.com.mx")
    tx, tenant = uuid4(), uuid4()

    payload = stripe_pagos._payload_suscripcion(tx, tenant, "pro", "price_abc", "due@x.com", None)

    assert payload["mode"] == "subscription"
    assert payload["line_items[0][price]"] == "price_abc"
    assert payload["line_items[0][quantity]"] == "1"
    assert payload["client_reference_id"] == str(tx)
    # Stripe repite la metadata de la Subscription en cada factura: es lo que
    # le dice al webhook de qué tenant es una renovación.
    assert payload["subscription_data[metadata][tenant_id]"] == str(tenant)
    assert payload["subscription_data[metadata][plan]"] == "pro"
    assert payload["subscription_data[metadata][transaccion_id]"] == str(tx)
    assert payload["customer_email"] == "due@x.com"
    assert "customer" not in payload
    # Nada de monto: lo pone el Price.
    assert not any("price_data" in k for k in payload)
    assert not any("payment_intent_data" in k for k in payload)


def test_payload_de_suscripcion_reusa_el_customer_del_tenant():
    payload = stripe_pagos._payload_suscripcion(
        uuid4(), uuid4(), "pro", "price_abc", "due@x.com", "cus_existente"
    )
    assert payload["customer"] == "cus_existente"
    # Stripe rechaza customer y customer_email juntos.
    assert "customer_email" not in payload


class _Respuesta:
    def __init__(self, datos: dict):
        self._datos = datos

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._datos


class _Cliente:
    ultimo_payload: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, url, headers=None, data=None):
        _Cliente.ultimo_payload = data
        return _Respuesta({"id": f"cs_{uuid4().hex[:10]}", "url": "https://checkout.stripe.com/c/x"})


@pytest.fixture
def stripe_simulado(monkeypatch):
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "stripe")
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_de_prueba")
    _Cliente.ultimo_payload = None
    monkeypatch.setattr(stripe_pagos.httpx, "AsyncClient", lambda *a, **k: _Cliente())


async def _contratar(http_client, token: str, plan: str = "pro"):
    return await http_client.post(
        "/api/pagos/crear-pago",
        json={"tipo": "subscription", "plan": plan},
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.asyncio
async def test_un_plan_sin_price_de_stripe_da_409_y_no_deja_transaccion(
    http_client, tenant_y_usuario, stripe_simulado, precios_stripe
):
    await execute("UPDATE planes SET stripe_price_id = NULL WHERE nombre = 'pro'")

    respuesta = await _contratar(http_client, tenant_y_usuario["token"])

    assert respuesta.status_code == 409
    assert _Cliente.ultimo_payload is None
    assert await fetch_value(
        "SELECT COUNT(*) FROM tenant_transactions WHERE tenant_id = $1", tenant_y_usuario["tenant_id"]
    ) == 0


@pytest.mark.asyncio
async def test_con_una_suscripcion_recurrente_viva_no_se_contrata_otra(
    http_client, tenant_y_usuario, stripe_simulado, precios_stripe
):
    """Dos Subscriptions a la vez serían dos cobros cada mes."""
    await fila_suscripcion(tenant_y_usuario["tenant_id"])

    for plan in ("pro", "starter"):
        respuesta = await _contratar(http_client, tenant_y_usuario["token"], plan)
        assert respuesta.status_code == 409

    assert _Cliente.ultimo_payload is None


@pytest.mark.asyncio
async def test_despues_de_una_baja_se_puede_volver_a_contratar_con_el_mismo_customer(
    http_client, tenant_y_usuario, stripe_simulado, precios_stripe
):
    await fila_suscripcion(
        tenant_y_usuario["tenant_id"], estado="pausada", cancelada_en=AHORA - timedelta(days=3)
    )

    respuesta = await _contratar(http_client, tenant_y_usuario["token"])

    assert respuesta.status_code == 201
    assert _Cliente.ultimo_payload["customer"] == "cus_test_1"
    assert _Cliente.ultimo_payload["line_items[0][price]"] == precios_stripe["pro"]


@pytest.mark.asyncio
async def test_una_prueba_de_gerencia_no_impide_contratar(
    http_client, tenant_y_usuario, stripe_simulado, precios_stripe
):
    """La prueba no tiene Subscription de Stripe: no hay nada que duplicar."""
    await fila_suscripcion(tenant_y_usuario["tenant_id"], sub_id=None, customer_id=None)

    respuesta = await _contratar(http_client, tenant_y_usuario["token"])

    assert respuesta.status_code == 201
    assert _Cliente.ultimo_payload["customer_email"] == tenant_y_usuario["email"]


# ============================================================
# 2. Lectores
# ============================================================
@pytest.mark.parametrize("formato", ["basil", "viejo"])
def test_leer_factura_entiende_los_dos_formatos_de_la_api(formato):
    tenant = uuid4()
    fin = AHORA + timedelta(days=31)
    f = leer_factura(
        factura(
            tenant_id=tenant, sub_id="sub_x", invoice_id="in_x", periodo_fin=fin,
            centavos=29999, formato=formato,
        ),
        pagada=True,
    )

    assert f.id == "in_x"
    assert f.subscription_id == "sub_x"
    assert f.customer_id == "cus_test_1"
    assert f.metadata["tenant_id"] == str(tenant)
    assert f.price_id == "price_test_pro"
    assert f.periodo_fin == fin
    assert f.monto == Decimal("299.99")
    assert f.billing_reason == "subscription_cycle"


def test_leer_factura_toma_la_linea_del_periodo_mas_lejano():
    """Con prorrateo hay varias líneas; la del plan es la que termina último."""
    obj = factura(tenant_id=uuid4(), sub_id="sub_x", invoice_id="in_x")
    lejano = AHORA + timedelta(days=60)
    obj["lines"]["data"].insert(
        0, {"period": {"end": int((AHORA + timedelta(days=5)).timestamp())}}
    )
    obj["lines"]["data"].append({"period": {"end": int(lejano.timestamp())}})

    assert leer_factura(obj, pagada=True).periodo_fin == lejano


def test_leer_suscripcion_con_cancelacion_programada_y_motivo():
    s = leer_suscripcion(
        suscripcion_stripe(
            "sub_x",
            cancel_at_period_end=True,
            cancellation_details={"reason": "cancellation_requested", "feedback": "too_expensive"},
        )
    )
    assert s.cancela is True
    assert s.motivo == "cancellation_requested, too_expensive"


# ============================================================
# 3. Webhook
# ============================================================
async def _transaccion_pendiente(tenant_id, session_id: str):
    return await fetch_value(
        """
        INSERT INTO tenant_transactions
            (tenant_id, tipo, concepto, monto, estado_pago, plan_nombre, stripe_session_id)
        VALUES ($1, 'subscription', 'Suscripción pro', 199.00, 'pendiente', 'pro', $2)
        RETURNING id
        """,
        tenant_id,
        session_id,
    )


@pytest.mark.asyncio
async def test_alta_activa_el_plan_hasta_el_fin_del_periodo_cobrado_y_avisa(
    http_client, tenant_y_usuario, con_secreto, avisos, precios_stripe
):
    tenant = tenant_y_usuario["tenant_id"]
    tx = await _transaccion_pendiente(tenant, "cs_alta")
    fin = AHORA + timedelta(days=31)
    evento = factura(
        tenant_id=tenant, sub_id="sub_alta", invoice_id="in_alta",
        billing_reason="subscription_create", periodo_fin=fin, transaccion_id=tx,
    )

    assert await enviar(http_client, "invoice.payment_succeeded", evento) == 200

    s = await sub_de(tenant)
    assert (s["plan"], s["estado"], s["origen"]) == ("pro", "activa", "pago")
    # La fecha es la de Stripe, no "hoy + 30".
    assert s["fecha_renovacion"] == fin
    assert s["stripe_subscription_id"] == "sub_alta"
    assert s["stripe_customer_id"] == "cus_test_1"

    # Completa la fila que creó crear-pago en vez de crear otra.
    filas = await fetch_all(
        "SELECT id, estado_pago, stripe_invoice_id, monto FROM tenant_transactions WHERE tenant_id = $1",
        tenant,
    )
    assert len(filas) == 1
    assert filas[0]["id"] == tx
    assert (filas[0]["estado_pago"], filas[0]["stripe_invoice_id"]) == ("aprobado", "in_alta")
    assert filas[0]["monto"] == Decimal("199.00")

    assert [a.tipo for a in avisos] == ["alta"]
    assert avisos[0].tenant_id == tenant
    assert avisos[0].plan == "pro"
    assert avisos[0].stripe_invoice_id == "in_alta"


@pytest.mark.asyncio
async def test_la_misma_factura_reenviada_por_stripe_no_se_procesa_dos_veces(
    http_client, tenant_y_usuario, con_secreto, avisos, precios_stripe
):
    tenant = tenant_y_usuario["tenant_id"]
    await fila_suscripcion(tenant, sub_id="sub_dup")
    evento = factura(tenant_id=tenant, sub_id="sub_dup", invoice_id="in_dup")

    for _ in range(3):
        assert await enviar(http_client, "invoice.payment_succeeded", evento) == 200

    assert await fetch_value(
        "SELECT COUNT(*) FROM tenant_transactions WHERE stripe_invoice_id = 'in_dup'"
    ) == 1
    assert [a.tipo for a in avisos] == ["renovacion"]


@pytest.mark.asyncio
async def test_checkout_de_suscripcion_completado_no_activa_por_su_cuenta(
    http_client, tenant_y_usuario, con_secreto, avisos
):
    """
    En modo subscription el plan lo activa la factura (que trae hasta
    cuándo quedó pagado). La sesión solo confirma la transacción.
    """
    tenant = tenant_y_usuario["tenant_id"]
    tx = await _transaccion_pendiente(tenant, "cs_sub")

    estado = await enviar(
        http_client,
        "checkout.session.completed",
        {
            "id": "cs_sub",
            "mode": "subscription",
            "client_reference_id": str(tx),
            "payment_status": "paid",
            "payment_method_types": ["card"],
        },
    )

    assert estado == 200
    assert await fetch_value("SELECT estado_pago FROM tenant_transactions WHERE id = $1", tx) == "aprobado"
    assert await sub_de(tenant) is None
    assert avisos == []


@pytest.mark.asyncio
async def test_si_la_sesion_llega_antes_la_factura_igual_activa_el_plan(
    http_client, tenant_y_usuario, con_secreto, avisos, precios_stripe
):
    """Stripe no garantiza el orden: la sesión ya dejó la fila 'aprobado'."""
    tenant = tenant_y_usuario["tenant_id"]
    tx = await _transaccion_pendiente(tenant, "cs_orden")
    await execute("UPDATE tenant_transactions SET estado_pago = 'aprobado' WHERE id = $1", tx)

    evento = factura(
        tenant_id=tenant, sub_id="sub_orden", invoice_id="in_orden",
        billing_reason="subscription_create", transaccion_id=tx,
    )
    assert await enviar(http_client, "invoice.payment_succeeded", evento) == 200

    assert (await sub_de(tenant))["estado"] == "activa"
    assert [a.tipo for a in avisos] == ["alta"]


@pytest.mark.asyncio
async def test_renovacion_extiende_la_vigencia_y_registra_el_cobro(
    http_client, tenant_y_usuario, con_secreto, avisos, precios_stripe
):
    tenant = tenant_y_usuario["tenant_id"]
    await fila_suscripcion(tenant, sub_id="sub_ren", fecha_renovacion=AHORA)
    fin = AHORA + timedelta(days=30)

    evento = factura(
        tenant_id=tenant, sub_id="sub_ren", invoice_id="in_ren", periodo_fin=fin, formato="viejo"
    )
    assert await enviar(http_client, "invoice.payment_succeeded", evento) == 200

    s = await sub_de(tenant)
    assert s["estado"] == "activa"
    assert s["fecha_renovacion"] == fin

    tx = await fetch_one(
        "SELECT concepto, estado_pago, monto, stripe_payment_intent_id FROM tenant_transactions WHERE stripe_invoice_id = 'in_ren'"
    )
    assert tx["concepto"] == "Renovación pro"
    assert tx["estado_pago"] == "aprobado"
    assert tx["monto"] == Decimal("199.00")
    assert tx["stripe_payment_intent_id"] == "pi_in_ren"

    assert [a.tipo for a in avisos] == ["renovacion"]
    assert avisos[0].fecha_renovacion == fin


@pytest.mark.asyncio
async def test_el_price_cobrado_manda_sobre_la_metadata(
    http_client, tenant_y_usuario, con_secreto, avisos, precios_stripe
):
    """Si cambiaron el plan desde el panel de Stripe, la factura trae el Price nuevo."""
    tenant = tenant_y_usuario["tenant_id"]
    await fila_suscripcion(tenant, sub_id="sub_cambio")

    evento = factura(
        tenant_id=tenant, sub_id="sub_cambio", invoice_id="in_cambio",
        plan="pro", price_id=precios_stripe["enterprise"],
    )
    assert await enviar(http_client, "invoice.payment_succeeded", evento) == 200

    assert (await sub_de(tenant))["plan"] == "enterprise"


@pytest.mark.asyncio
async def test_cobro_fallido_respeta_los_dias_pagados_y_avisa_cada_intento_una_vez(
    http_client, tenant_y_usuario, con_secreto, avisos
):
    tenant = tenant_y_usuario["tenant_id"]
    vence = AHORA + timedelta(days=2)
    await fila_suscripcion(tenant, sub_id="sub_falla", fecha_renovacion=vence)
    reintento = AHORA + timedelta(days=3)

    intento_1 = factura(
        tenant_id=tenant, sub_id="sub_falla", invoice_id="in_falla",
        intento=1, proximo_intento=reintento,
    )
    assert await enviar(http_client, "invoice.payment_failed", intento_1) == 200
    # Stripe reenvía el mismo intento: no cuenta ni avisa de nuevo.
    assert await enviar(http_client, "invoice.payment_failed", intento_1) == 200

    s = await sub_de(tenant)
    # No se pausa: le quedan días pagados.
    assert s["estado"] == "activa"
    assert s["fecha_renovacion"] == vence
    assert s["intentos_fallidos"] == 1
    assert s["fecha_proximo_intento"] == reintento

    tx = await fetch_one(
        "SELECT estado_pago, concepto FROM tenant_transactions WHERE stripe_invoice_id = 'in_falla'"
    )
    assert tx["estado_pago"] == "rechazado"

    assert [a.tipo for a in avisos] == ["pago_fallido"]
    assert avisos[0].intento == 1
    assert avisos[0].proximo_intento == reintento
    assert "Sigue vigente" in avisos[0].accion

    # El segundo intento sí es noticia; la factura sigue siendo una sola fila.
    intento_2 = factura(
        tenant_id=tenant, sub_id="sub_falla", invoice_id="in_falla", intento=2, proximo_intento=None
    )
    assert await enviar(http_client, "invoice.payment_failed", intento_2) == 200

    assert (await sub_de(tenant))["intentos_fallidos"] == 2
    assert await fetch_value(
        "SELECT COUNT(*) FROM tenant_transactions WHERE stripe_invoice_id = 'in_falla'"
    ) == 1
    assert [a.tipo for a in avisos] == ["pago_fallido", "pago_fallido"]
    assert "no va a reintentar" in avisos[1].accion


@pytest.mark.asyncio
async def test_cobro_fallido_vencido_lo_pausa_el_job_y_un_reintento_exitoso_lo_reactiva(
    http_client, tenant_y_usuario, con_secreto, avisos, precios_stripe
):
    tenant = tenant_y_usuario["tenant_id"]
    await fila_suscripcion(tenant, sub_id="sub_ciclo", fecha_renovacion=AHORA + timedelta(hours=1))
    assert await enviar(
        http_client,
        "invoice.payment_failed",
        factura(tenant_id=tenant, sub_id="sub_ciclo", invoice_id="in_ciclo", intento=1,
                proximo_intento=AHORA + timedelta(days=3)),
    ) == 200

    # Pasa la fecha sin cobro: el job la pausa y avisa.
    await execute(
        "UPDATE tenant_subscriptions SET fecha_renovacion = NOW() - INTERVAL '1 minute' WHERE tenant_id = $1",
        tenant,
    )
    await pagos_background.job_pausar_suscripciones_vencidas()

    assert (await sub_de(tenant))["estado"] == "pausada"
    assert [a.tipo for a in avisos] == ["pago_fallido", "pausada"]
    assert "intento(s) fallido(s)" in avisos[1].motivo

    # Stripe reintenta la MISMA factura y esta vez cobra.
    fin = AHORA + timedelta(days=30)
    assert await enviar(
        http_client,
        "invoice.payment_succeeded",
        factura(tenant_id=tenant, sub_id="sub_ciclo", invoice_id="in_ciclo", periodo_fin=fin),
    ) == 200

    s = await sub_de(tenant)
    assert (s["estado"], s["intentos_fallidos"], s["fecha_renovacion"]) == ("activa", 0, fin)
    assert s["fecha_proximo_intento"] is None
    assert await fetch_value(
        "SELECT estado_pago FROM tenant_transactions WHERE stripe_invoice_id = 'in_ciclo'"
    ) == "aprobado"
    assert [a.tipo for a in avisos] == ["pago_fallido", "pausada", "renovacion"]


@pytest.mark.asyncio
async def test_el_primer_cobro_fallido_en_el_checkout_no_toca_nada(
    http_client, tenant_y_usuario, con_secreto, avisos
):
    """El dueño está en el checkout y puede probar otra tarjeta."""
    tenant = tenant_y_usuario["tenant_id"]
    tx = await _transaccion_pendiente(tenant, "cs_primer_falla")

    assert await enviar(
        http_client,
        "invoice.payment_failed",
        factura(tenant_id=tenant, sub_id="sub_nueva", invoice_id="in_nueva",
                billing_reason="subscription_create", transaccion_id=tx, intento=1),
    ) == 200

    assert await fetch_value("SELECT estado_pago FROM tenant_transactions WHERE id = $1", tx) == "pendiente"
    assert await sub_de(tenant) is None
    assert avisos == []


@pytest.mark.asyncio
async def test_cancelacion_programada_y_revertida(
    http_client, tenant_y_usuario, con_secreto, avisos
):
    tenant = tenant_y_usuario["tenant_id"]
    await fila_suscripcion(tenant, sub_id="sub_prog")

    programada = suscripcion_stripe(
        "sub_prog", cancel_at_period_end=True,
        cancellation_details={"reason": "cancellation_requested"},
    )
    assert await enviar(http_client, "customer.subscription.updated", programada) == 200
    assert await enviar(http_client, "customer.subscription.updated", programada) == 200

    s = await sub_de(tenant)
    assert s["cancela_al_vencer"] is True
    assert s["estado"] == "activa"
    assert [a.tipo for a in avisos] == ["cancelacion_programada"]
    assert avisos[0].motivo == "cancellation_requested"

    revertida = suscripcion_stripe("sub_prog", cancel_at_period_end=False)
    assert await enviar(http_client, "customer.subscription.updated", revertida) == 200

    assert (await sub_de(tenant))["cancela_al_vencer"] is False
    assert [a.tipo for a in avisos] == ["cancelacion_programada", "cancelacion_revertida"]


@pytest.mark.asyncio
async def test_baja_con_dias_restantes_sigue_vigente_y_se_pausa_al_vencer(
    http_client, tenant_y_usuario, con_secreto, avisos
):
    tenant = tenant_y_usuario["tenant_id"]
    vence = AHORA + timedelta(days=12)
    await fila_suscripcion(tenant, sub_id="sub_baja", fecha_renovacion=vence)

    baja = suscripcion_stripe("sub_baja", status="canceled")
    assert await enviar(http_client, "customer.subscription.deleted", baja) == 200
    assert await enviar(http_client, "customer.subscription.deleted", baja) == 200

    s = await sub_de(tenant)
    assert s["estado"] == "activa"
    assert s["fecha_renovacion"] == vence
    assert s["cancelada_en"] is not None
    assert [a.tipo for a in avisos] == ["cancelada"]
    assert "sigue vigente" in avisos[0].accion

    # Todavía no venció: el job no la toca.
    await pagos_background.job_pausar_suscripciones_vencidas()
    assert (await sub_de(tenant))["estado"] == "activa"

    # Vence: el job la pausa y avisa.
    await execute(
        "UPDATE tenant_subscriptions SET fecha_renovacion = NOW() - INTERVAL '1 minute' WHERE tenant_id = $1",
        tenant,
    )
    await pagos_background.job_pausar_suscripciones_vencidas()

    assert (await sub_de(tenant))["estado"] == "pausada"
    assert [a.tipo for a in avisos] == ["cancelada", "pausada"]
    assert avisos[1].motivo == "Venció la suscripción cancelada"


@pytest.mark.asyncio
async def test_baja_sin_dias_restantes_se_pausa_en_el_momento(
    http_client, tenant_y_usuario, con_secreto, avisos
):
    tenant = tenant_y_usuario["tenant_id"]
    await fila_suscripcion(tenant, sub_id="sub_ya", fecha_renovacion=AHORA - timedelta(hours=1))

    assert await enviar(
        http_client, "customer.subscription.deleted", suscripcion_stripe("sub_ya")
    ) == 200

    assert (await sub_de(tenant))["estado"] == "pausada"
    assert [a.tipo for a in avisos] == ["cancelada"]
    assert "se pausó de inmediato" in avisos[0].accion


@pytest.mark.asyncio
async def test_eventos_de_una_suscripcion_ajena_no_tocan_nada(
    http_client, con_secreto, avisos, db
):
    for tipo, obj in (
        ("customer.subscription.deleted", suscripcion_stripe("sub_de_nadie")),
        ("customer.subscription.updated", suscripcion_stripe("sub_de_nadie", cancel_at_period_end=True)),
        ("invoice.payment_failed", factura(tenant_id=uuid4(), sub_id="sub_de_nadie", invoice_id="in_nadie", intento=1)),
        ("invoice.payment_succeeded", factura(tenant_id=uuid4(), sub_id="sub_de_nadie", invoice_id="in_nadie2")),
    ):
        assert await enviar(http_client, tipo, obj) == 200

    assert avisos == []
    assert await fetch_value(
        "SELECT COUNT(*) FROM tenant_transactions WHERE stripe_invoice_id IN ('in_nadie', 'in_nadie2')"
    ) == 0


@pytest.mark.asyncio
async def test_el_portal_sabe_si_la_suscripcion_es_recurrente(
    http_client, tenant_y_usuario
):
    tenant = tenant_y_usuario["tenant_id"]
    headers = {"Authorization": f"Bearer {tenant_y_usuario['token']}"}
    await fila_suscripcion(tenant)

    datos = (await http_client.get("/api/pagos/suscripcion", headers=headers)).json()
    assert (datos["suscripcion_recurrente"], datos["cancela_al_vencer"]) == (True, False)

    await execute(
        "UPDATE tenant_subscriptions SET cancelada_en = NOW(), cancela_al_vencer = true WHERE tenant_id = $1",
        tenant,
    )
    datos = (await http_client.get("/api/pagos/suscripcion", headers=headers)).json()
    assert (datos["suscripcion_recurrente"], datos["cancela_al_vencer"]) == (False, True)


# ============================================================
# 4. Job: una prueba que vence no se avisa
# ============================================================
@pytest.mark.asyncio
async def test_el_job_no_avisa_de_una_prueba_que_vence(tenant_y_usuario, avisos):
    tenant = tenant_y_usuario["tenant_id"]
    await fila_suscripcion(
        tenant, sub_id=None, customer_id=None, fecha_renovacion=AHORA - timedelta(hours=1)
    )

    await pagos_background.job_pausar_suscripciones_vencidas()

    assert (await sub_de(tenant))["estado"] == "pausada"
    assert avisos == []


# ============================================================
# 5. Los avisos a gerencia
# ============================================================
@pytest.fixture
async def gerente_destino(db):
    email = f"gerencia-{uuid4().hex[:10]}@ejemplo.com"
    await execute(
        "INSERT INTO gerencia_users (email, full_name, cargo) VALUES ($1, 'Test', 'QA')", email
    )
    yield email
    await execute("DELETE FROM gerencia_users WHERE email = $1", email)


@pytest.fixture
def correos(monkeypatch):
    enviados: list[dict] = []

    async def enviar_correo(destino, asunto, texto, html):
        enviados.append({"destino": destino, "asunto": asunto, "texto": texto, "html": html})

    monkeypatch.setattr(notificaciones_gerencia, "enviar_correo", enviar_correo)
    return enviados


@pytest.fixture
async def limpiar_alertas(tenant_y_usuario):
    yield
    await execute("DELETE FROM gerencia_alertas WHERE tenant_id = $1", tenant_y_usuario["tenant_id"])


def _evento(tenant_id, tipo="pago_fallido", **extra) -> EventoSuscripcion:
    return EventoSuscripcion(
        tipo=tipo,
        tenant_id=tenant_id,
        plan="pro",
        monto=Decimal("199.00"),
        moneda="mxn",
        fecha_renovacion=AHORA + timedelta(days=2),
        intento=1,
        proximo_intento=AHORA + timedelta(days=3),
        accion="Sigue vigente hasta el X.",
        stripe_customer_id="cus_test_1",
        stripe_subscription_id="sub_test_1",
        stripe_invoice_id="in_test_1",
        **extra,
    )


@pytest.mark.asyncio
async def test_el_aviso_le_llega_a_cada_gerente_con_el_detalle_del_cliente(
    tenant_y_usuario, gerente_destino, correos, limpiar_alertas
):
    enviados = await notificar_evento_suscripcion(_evento(tenant_y_usuario["tenant_id"]))

    mio = [c for c in correos if c["destino"] == gerente_destino]
    assert len(mio) == 1
    assert enviados == len(correos)

    correo = mio[0]
    assert correo["asunto"] == "Cobro de suscripción fallido: Test Tenant - OperativAI"
    for dato in (
        "Test Tenant",
        tenant_y_usuario["email"],  # correo del dueño
        "pro",
        "199.00 MXN",
        "Intento de cobro: 1",
        "Sigue vigente hasta el X.",
        "cus_test_1",
        "sub_test_1",
        "in_test_1",
        f"/gerencia/tenants/{tenant_y_usuario['tenant_id']}",
    ):
        assert dato in correo["texto"], dato


@pytest.mark.asyncio
async def test_un_cobro_fallido_abre_una_sola_alerta_y_la_actualiza(
    tenant_y_usuario, correos, limpiar_alertas
):
    tenant = tenant_y_usuario["tenant_id"]
    await notificar_evento_suscripcion(_evento(tenant))
    segundo = _evento(tenant)
    segundo.intento = 2
    await notificar_evento_suscripcion(segundo)

    alertas = await fetch_all(
        "SELECT tipo, titulo, detalle FROM gerencia_alertas WHERE tenant_id = $1 AND revisada_en IS NULL",
        tenant,
    )
    assert len(alertas) == 1
    assert alertas[0]["tipo"] == "stripe_pago_fallido"
    assert alertas[0]["detalle"]["intento"] == 2
    assert alertas[0]["detalle"]["stripe_invoice_id"] == "in_test_1"


@pytest.mark.asyncio
async def test_altas_y_renovaciones_mandan_correo_pero_no_abren_alerta(
    tenant_y_usuario, gerente_destino, correos, limpiar_alertas
):
    tenant = tenant_y_usuario["tenant_id"]
    for tipo in ("alta", "renovacion", "cancelacion_revertida"):
        await notificar_evento_suscripcion(_evento(tenant, tipo=tipo))

    assert len([c for c in correos if c["destino"] == gerente_destino]) == 3
    assert await fetch_value(
        "SELECT COUNT(*) FROM gerencia_alertas WHERE tenant_id = $1", tenant
    ) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tipo, alerta",
    [
        ("cancelacion_programada", "stripe_cancelacion_programada"),
        ("cancelada", "stripe_suscripcion_cancelada"),
        ("pausada", "stripe_suscripcion_pausada"),
    ],
)
async def test_lo_que_pide_accion_abre_alerta(
    tenant_y_usuario, correos, limpiar_alertas, tipo, alerta
):
    await notificar_evento_suscripcion(_evento(tenant_y_usuario["tenant_id"], tipo=tipo))

    assert await fetch_value(
        "SELECT tipo FROM gerencia_alertas WHERE tenant_id = $1", tenant_y_usuario["tenant_id"]
    ) == alerta


@pytest.mark.asyncio
async def test_un_smtp_caido_no_rompe_el_aviso(
    tenant_y_usuario, gerente_destino, monkeypatch, limpiar_alertas
):
    async def falla(*_a, **_k):
        raise ErrorEnvioCorreo("smtp caído")

    monkeypatch.setattr(notificaciones_gerencia, "enviar_correo", falla)

    # No lanza, y la alerta queda igual: es lo que gerencia ve en el panel.
    assert await notificar_evento_suscripcion(_evento(tenant_y_usuario["tenant_id"])) == 0
    assert await fetch_value(
        "SELECT COUNT(*) FROM gerencia_alertas WHERE tenant_id = $1", tenant_y_usuario["tenant_id"]
    ) == 1


def test_el_html_del_aviso_escapa_los_datos_del_cliente():
    """El nombre del negocio lo escribe el cliente: no puede inyectar HTML."""
    _, _, cuerpo = armar_correo(
        _evento(uuid4(), tipo="alta"), "<script>alert(1)</script>", "d@x.com"
    )
    assert "<script>" not in cuerpo
    assert "&lt;script&gt;" in cuerpo
