"""
Tests del camino de Stripe.

Se concentran en lo que cuesta dinero si falla:

  1. la firma del webhook — sin ella, cualquiera que descubra la URL manda
     un checkout.session.completed inventado y se regala créditos
  2. la conversión a centavos — Stripe cobra en la unidad mínima, y un
     error de factor 100 es un cobro 100 veces mayor (o menor)
  3. que una sesión "completada" pero todavía no pagada (OXXO/SPEI, que es
     lo normal en MXN) NO entregue lo comprado

La entrega en sí (activar plan, acreditar créditos) no se prueba acá: vive
en services/pagos.py, no depende del proveedor y ya la cubre test_pagos.py.

La API de Stripe no se llama nunca: lo que se prueba es nuestro lado.
"""

import hashlib
import hmac
import json
import time
from decimal import Decimal
from uuid import uuid4

import pytest

from config import settings
from services import stripe_pagos
from services.stripe_pagos import a_unidad_minima, firma_valida
from session import execute, fetch_one, fetch_value

SECRETO = "whsec_secreto-de-prueba"


def firmar(cuerpo: bytes, secreto: str = SECRETO, ts: int | None = None) -> str:
    """Arma una cabecera Stripe-Signature válida, como la que manda Stripe."""
    ts = int(time.time()) if ts is None else ts
    firma = hmac.new(
        secreto.encode(), b"%d.%s" % (ts, cuerpo), hashlib.sha256
    ).hexdigest()
    return f"t={ts},v1={firma}"


@pytest.fixture
def con_secreto(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", SECRETO)
    return SECRETO


# ============================================================
# Firma del webhook
# ============================================================
def test_firma_correcta_pasa(con_secreto):
    cuerpo = b'{"type":"checkout.session.completed"}'
    assert firma_valida(firmar(cuerpo), cuerpo) is True


def test_firma_de_otro_secreto_no_pasa(con_secreto):
    cuerpo = b'{"type":"checkout.session.completed"}'
    assert firma_valida(firmar(cuerpo, secreto="whsec_otro"), cuerpo) is False


def test_sin_cabecera_no_pasa(con_secreto):
    assert firma_valida(None, b"{}") is False


def test_cabecera_sin_v1_no_pasa(con_secreto):
    assert firma_valida(f"t={int(time.time())}", b"{}") is False


def test_tocar_el_cuerpo_invalida_la_firma(con_secreto):
    """
    El caso que importa: firma buena de un pago de $1 reusada para acreditar
    $10.000. El cuerpo entero entra en el HMAC, así que editarlo lo rompe.
    """
    original = b'{"amount_total":100}'
    cabecera = firmar(original)
    assert firma_valida(cabecera, b'{"amount_total":1000000}') is False


def test_una_firma_vieja_no_pasa(con_secreto):
    """
    Sin ventana de tolerancia, una petición firmada capturada hoy se podría
    reenviar mañana tal cual (replay).
    """
    cuerpo = b"{}"
    vieja = firmar(cuerpo, ts=int(time.time()) - stripe_pagos.TOLERANCIA_FIRMA_SEGUNDOS - 60)
    assert firma_valida(vieja, cuerpo) is False


def test_una_firma_del_futuro_tampoco_pasa(con_secreto):
    cuerpo = b"{}"
    futura = firmar(cuerpo, ts=int(time.time()) + stripe_pagos.TOLERANCIA_FIRMA_SEGUNDOS + 60)
    assert firma_valida(futura, cuerpo) is False


def test_con_varias_firmas_alcanza_con_que_una_valide(con_secreto):
    """Stripe manda más de una v1 mientras se rota el secreto."""
    cuerpo = b"{}"
    ts = int(time.time())
    buena = hmac.new(SECRETO.encode(), b"%d.%s" % (ts, cuerpo), hashlib.sha256).hexdigest()
    assert firma_valida(f"t={ts},v1=firmavieja,v1={buena}", cuerpo) is True


# ============================================================
# Conversión a la unidad mínima
# ============================================================
@pytest.mark.parametrize(
    "monto, esperado",
    [
        (Decimal("99.99"), 9999),
        (Decimal("29.99"), 2999),
        (Decimal("299.99"), 29999),
        (Decimal("100"), 10000),
        (Decimal("0.01"), 1),
    ],
)
def test_pesos_a_centavos(monto, esperado):
    assert a_unidad_minima(monto, "mxn") == esperado


@pytest.mark.parametrize("monto", ["0.29", "0.57", "0.58", "1.13", "1.15", "2.01"])
def test_la_conversion_no_pierde_un_centavo_por_el_flotante(monto):
    """
    En binario flotante `int(1.15 * 100)` da 114: un centavo de menos. Pasa
    con ~4.600 de los primeros 100.000 montos posibles, así que no es un
    caso raro — por eso la cuenta se hace en Decimal y no con float.
    """
    esperado = int(Decimal(monto) * 100)
    assert a_unidad_minima(Decimal(monto), "mxn") == esperado
    # Lo que NO hay que hacer, documentado para que nadie lo "simplifique":
    assert int(float(monto) * 100) == esperado - 1


def test_una_moneda_sin_decimales_no_se_multiplica():
    """Con JPY, 500 son 500 yenes, no 50.000."""
    assert a_unidad_minima(Decimal("500"), "jpy") == 500
    assert a_unidad_minima(Decimal("500"), "JPY") == 500


# ============================================================
# Payload del Checkout
# ============================================================
def test_el_payload_lleva_la_referencia_que_usa_el_webhook(monkeypatch):
    """Sin ese hilo, el webhook no sabría a qué tenant acreditarle."""
    monkeypatch.setattr(settings, "BASE_URL_FRONTEND", "https://app.operativai.com.mx")
    tx = uuid4()

    payload = stripe_pagos._payload_checkout(
        tx, "Suscripción pro", Decimal("99.99"), "due@ejemplo.com"
    )

    assert payload["client_reference_id"] == str(tx)
    assert payload["metadata[transaccion_id]"] == str(tx)
    assert payload["payment_intent_data[metadata][transaccion_id]"] == str(tx)
    assert payload["mode"] == "payment"
    assert payload["line_items[0][price_data][unit_amount]"] == "9999"
    assert payload["line_items[0][price_data][product_data][name]"] == "Suscripción pro"


def test_el_success_url_conserva_la_plantilla_de_stripe(monkeypatch):
    """
    {CHECKOUT_SESSION_ID} lo sustituye Stripe al redirigir. Si alguien lo
    "arregla" poniendo el id real, la pantalla de retorno se queda sin saber
    qué pago consultar.
    """
    monkeypatch.setattr(settings, "BASE_URL_FRONTEND", "https://app.operativai.com.mx")
    payload = stripe_pagos._payload_checkout(
        uuid4(), "100 créditos", Decimal("29.99"), "due@ejemplo.com"
    )

    assert payload["success_url"] == (
        "https://app.operativai.com.mx/pagos/exito?session_id={CHECKOUT_SESSION_ID}"
    )
    assert payload["cancel_url"] == "https://app.operativai.com.mx/pagos/error"


# ============================================================
# Endpoints
# ============================================================
class _RespuestaFalsa:
    def __init__(self, datos: dict, error: bool = False):
        self._datos = datos
        self._error = error

    def raise_for_status(self) -> None:
        if self._error:
            raise stripe_pagos.httpx.HTTPError("stripe dijo que no")

    def json(self) -> dict:
        return self._datos


class _ClienteFalso:
    """Reemplaza httpx.AsyncClient: guarda el payload y devuelve lo pactado."""

    ultimo_payload: dict | None = None

    def __init__(self, respuesta: _RespuestaFalsa):
        self._respuesta = respuesta

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, url, headers=None, data=None):
        _ClienteFalso.ultimo_payload = data
        return self._respuesta


@pytest.fixture
def stripe_simulado(monkeypatch):
    """Deja crear-pago operativo sin llamar a Stripe de verdad."""
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "stripe")
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_de_prueba")
    _ClienteFalso.ultimo_payload = None

    def instalar(respuesta: _RespuestaFalsa):
        monkeypatch.setattr(
            stripe_pagos.httpx, "AsyncClient", lambda *a, **k: _ClienteFalso(respuesta)
        )

    return instalar


@pytest.mark.asyncio
async def test_crear_pago_guarda_la_transaccion_con_el_precio_de_la_base(
    http_client, tenant_y_usuario, stripe_simulado
):
    stripe_simulado(
        _RespuestaFalsa({"id": "cs_test_123", "url": "https://checkout.stripe.com/c/abc"})
    )

    respuesta = await http_client.post(
        "/api/pagos/crear-pago",
        json={"tipo": "subscription", "plan": "pro"},
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )

    assert respuesta.status_code == 201
    datos = respuesta.json()
    assert datos["proveedor"] == "stripe"
    assert datos["referencia"] == "cs_test_123"
    assert datos["checkout_url"] == "https://checkout.stripe.com/c/abc"

    precio_real = await fetch_value("SELECT precio_monthly FROM planes WHERE nombre = 'pro'")
    fila = await fetch_one(
        """
        SELECT tipo, monto, estado_pago, plan_nombre, stripe_session_id
        FROM tenant_transactions WHERE id = $1
        """,
        datos["transaccion_id"],
    )
    assert fila["tipo"] == "subscription"
    assert fila["monto"] == precio_real
    # Nace pendiente: lo que la aprueba es el webhook, no esta llamada.
    assert fila["estado_pago"] == "pendiente"
    assert fila["plan_nombre"] == "pro"
    assert fila["stripe_session_id"] == "cs_test_123"

    # El monto que se le manda a Stripe sale de la base, no del cliente.
    assert _ClienteFalso.ultimo_payload["line_items[0][price_data][unit_amount]"] == str(
        int(precio_real * 100)
    )


@pytest.mark.asyncio
async def test_si_stripe_falla_la_transaccion_no_queda_pendiente_para_siempre(
    http_client, tenant_y_usuario, stripe_simulado
):
    stripe_simulado(_RespuestaFalsa({}, error=True))

    respuesta = await http_client.post(
        "/api/pagos/crear-pago",
        json={"tipo": "subscription", "plan": "starter"},
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 502

    pendientes = await fetch_value(
        "SELECT COUNT(*) FROM tenant_transactions WHERE tenant_id = $1 AND estado_pago = 'pendiente'",
        tenant_y_usuario["tenant_id"],
    )
    assert pendientes == 0


@pytest.mark.asyncio
async def test_sin_secret_key_configurada_crear_pago_no_atiende(
    http_client, tenant_y_usuario, monkeypatch
):
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "stripe")
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "")

    respuesta = await http_client.post(
        "/api/pagos/crear-pago",
        json={"tipo": "subscription", "plan": "pro"},
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 503


@pytest.mark.asyncio
async def test_sin_secreto_configurado_el_webhook_no_atiende(http_client, monkeypatch):
    """Fallar cerrado: sin secreto no hay forma de distinguir un evento real."""
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "")
    respuesta = await http_client.post(
        "/api/pagos/stripe/webhook", json={"type": "checkout.session.completed"}
    )
    assert respuesta.status_code == 503


@pytest.mark.asyncio
async def test_el_webhook_rechaza_una_firma_invalida(http_client, con_secreto):
    respuesta = await http_client.post(
        "/api/pagos/stripe/webhook",
        json={"type": "checkout.session.completed"},
        headers={"stripe-signature": f"t={int(time.time())},v1=firmafalsa"},
    )
    assert respuesta.status_code == 401


@pytest.mark.asyncio
async def test_el_webhook_ignora_los_eventos_que_no_nos_interesan(
    http_client, con_secreto
):
    """Un evento de otro tipo se contesta 200 para que Stripe no lo reintente."""
    cuerpo = json.dumps({"type": "customer.created", "data": {"object": {}}}).encode()
    respuesta = await http_client.post(
        "/api/pagos/stripe/webhook",
        content=cuerpo,
        headers={
            "stripe-signature": firmar(cuerpo),
            "content-type": "application/json",
        },
    )
    assert respuesta.status_code == 200


async def _transaccion_pendiente(tenant_id, session_id: str) -> str:
    return await fetch_value(
        """
        INSERT INTO tenant_transactions
            (tenant_id, tipo, concepto, monto, estado_pago, creditos_comprados,
             stripe_session_id)
        VALUES ($1, 'credit_purchase', '500 créditos', 129.99, 'pendiente', 500, $2)
        RETURNING id
        """,
        tenant_id,
        session_id,
    )


def _evento_sesion(tipo: str, transaccion_id, session_id: str, **extra) -> bytes:
    objeto = {
        "id": session_id,
        "client_reference_id": str(transaccion_id),
        "payment_method_types": ["card"],
        **extra,
    }
    return json.dumps({"type": tipo, "data": {"object": objeto}}).encode()


@pytest.mark.asyncio
async def test_una_sesion_pagada_aprueba_la_transaccion(
    http_client, tenant_y_usuario, con_secreto
):
    tx = await _transaccion_pendiente(tenant_y_usuario["tenant_id"], "cs_pagada")
    cuerpo = _evento_sesion(
        "checkout.session.completed",
        tx,
        "cs_pagada",
        payment_status="paid",
        payment_intent="pi_123",
    )

    respuesta = await http_client.post(
        "/api/pagos/stripe/webhook",
        content=cuerpo,
        headers={"stripe-signature": firmar(cuerpo), "content-type": "application/json"},
    )
    assert respuesta.status_code == 200

    fila = await fetch_one(
        """
        SELECT estado_pago, stripe_payment_intent_id, metodo_pago
        FROM tenant_transactions WHERE id = $1
        """,
        tx,
    )
    assert fila["estado_pago"] == "aprobado"
    assert fila["stripe_payment_intent_id"] == "pi_123"
    assert fila["metodo_pago"] == "card"


@pytest.mark.asyncio
async def test_una_sesion_completada_pero_sin_pagar_no_aprueba_nada(
    http_client, tenant_y_usuario, con_secreto
):
    """
    OXXO y SPEI (lo normal en MXN): la sesión se completa cuando se genera
    el voucher, pero el dinero llega horas después. Dar esto por aprobado
    sería entregar créditos que nadie pagó todavía.
    """
    tx = await _transaccion_pendiente(tenant_y_usuario["tenant_id"], "cs_oxxo")
    cuerpo = _evento_sesion(
        "checkout.session.completed", tx, "cs_oxxo", payment_status="unpaid"
    )

    respuesta = await http_client.post(
        "/api/pagos/stripe/webhook",
        content=cuerpo,
        headers={"stripe-signature": firmar(cuerpo), "content-type": "application/json"},
    )
    assert respuesta.status_code == 200

    estado = await fetch_value(
        "SELECT estado_pago FROM tenant_transactions WHERE id = $1", tx
    )
    assert estado == "pendiente"

    saldo = await fetch_value(
        "SELECT COUNT(*) FROM credit_transactions WHERE tenant_transaction_id = $1", tx
    )
    assert saldo == 0


@pytest.mark.asyncio
async def test_el_pago_asincrono_que_si_llega_aprueba_la_transaccion(
    http_client, tenant_y_usuario, con_secreto
):
    """La otra mitad del caso OXXO: cuando el dinero entra, sí se acredita."""
    tx = await _transaccion_pendiente(tenant_y_usuario["tenant_id"], "cs_oxxo2")
    cuerpo = _evento_sesion(
        "checkout.session.async_payment_succeeded", tx, "cs_oxxo2", payment_intent="pi_9"
    )

    respuesta = await http_client.post(
        "/api/pagos/stripe/webhook",
        content=cuerpo,
        headers={"stripe-signature": firmar(cuerpo), "content-type": "application/json"},
    )
    assert respuesta.status_code == 200

    estado = await fetch_value(
        "SELECT estado_pago FROM tenant_transactions WHERE id = $1", tx
    )
    assert estado == "aprobado"


@pytest.mark.asyncio
async def test_una_sesion_vencida_cancela_la_transaccion(
    http_client, tenant_y_usuario, con_secreto
):
    tx = await _transaccion_pendiente(tenant_y_usuario["tenant_id"], "cs_vencida")
    cuerpo = _evento_sesion("checkout.session.expired", tx, "cs_vencida")

    respuesta = await http_client.post(
        "/api/pagos/stripe/webhook",
        content=cuerpo,
        headers={"stripe-signature": firmar(cuerpo), "content-type": "application/json"},
    )
    assert respuesta.status_code == 200

    estado = await fetch_value(
        "SELECT estado_pago FROM tenant_transactions WHERE id = $1", tx
    )
    assert estado == "cancelado"


@pytest.mark.asyncio
async def test_un_evento_tardio_no_hace_retroceder_un_pago_ya_aprobado(
    http_client, tenant_y_usuario, con_secreto
):
    """
    Stripe no garantiza el orden y reintenta lo que falla: el
    checkout.session.completed con payment_status=unpaid puede llegar
    después del async_payment_succeeded. Sin guarda, ese reintento dejaría
    como 'pendiente' un cobro ya entregado.
    """
    tx = await _transaccion_pendiente(tenant_y_usuario["tenant_id"], "cs_tardio")
    await execute(
        "UPDATE tenant_transactions SET estado_pago = 'aprobado' WHERE id = $1", tx
    )

    cuerpo = _evento_sesion(
        "checkout.session.completed", tx, "cs_tardio", payment_status="unpaid"
    )
    respuesta = await http_client.post(
        "/api/pagos/stripe/webhook",
        content=cuerpo,
        headers={"stripe-signature": firmar(cuerpo), "content-type": "application/json"},
    )
    assert respuesta.status_code == 200

    estado = await fetch_value(
        "SELECT estado_pago FROM tenant_transactions WHERE id = $1", tx
    )
    assert estado == "aprobado"


@pytest.mark.asyncio
async def test_un_evento_de_una_transaccion_ajena_no_rompe(
    http_client, tenant_y_usuario, con_secreto
):
    """Una sesión que no es nuestra se contesta 200 y no toca nada."""
    cuerpo = _evento_sesion(
        "checkout.session.completed", uuid4(), "cs_ajena", payment_status="paid"
    )

    respuesta = await http_client.post(
        "/api/pagos/stripe/webhook",
        content=cuerpo,
        headers={"stripe-signature": firmar(cuerpo), "content-type": "application/json"},
    )
    assert respuesta.status_code == 200


@pytest.mark.asyncio
async def test_un_reembolso_deja_la_transaccion_como_reembolsada(
    http_client, tenant_y_usuario, con_secreto
):
    tx = await _transaccion_pendiente(tenant_y_usuario["tenant_id"], "cs_reembolso")
    await execute(
        """
        UPDATE tenant_transactions
           SET estado_pago = 'aprobado', stripe_payment_intent_id = 'pi_reembolso'
         WHERE id = $1
        """,
        tx,
    )

    cuerpo = json.dumps(
        {"type": "charge.refunded", "data": {"object": {"payment_intent": "pi_reembolso"}}}
    ).encode()
    respuesta = await http_client.post(
        "/api/pagos/stripe/webhook",
        content=cuerpo,
        headers={"stripe-signature": firmar(cuerpo), "content-type": "application/json"},
    )
    assert respuesta.status_code == 200

    estado = await fetch_value(
        "SELECT estado_pago FROM tenant_transactions WHERE id = $1", tx
    )
    assert estado == "reembolsado"


@pytest.mark.asyncio
async def test_la_pantalla_de_retorno_consulta_por_session_id(
    http_client, tenant_y_usuario, con_secreto
):
    tx = await _transaccion_pendiente(tenant_y_usuario["tenant_id"], "cs_consulta")

    respuesta = await http_client.get(
        "/api/pagos/checkout/cs_consulta",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 200
    assert respuesta.json()["id"] == str(tx)
    assert respuesta.json()["estado"] == "pendiente"


@pytest.mark.asyncio
async def test_no_se_puede_espiar_el_checkout_de_otro_tenant(
    http_client, tenant_y_usuario
):
    otro_tenant = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro')", otro_tenant)
    await _transaccion_pendiente(otro_tenant, "cs_ajena_consulta")

    respuesta = await http_client.get(
        "/api/pagos/checkout/cs_ajena_consulta",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 404

    await execute("DELETE FROM tenants WHERE id = $1", otro_tenant)
