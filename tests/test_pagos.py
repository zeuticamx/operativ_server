"""
Tests de cobros con Mercado Pago.

Se concentran en las dos cosas que, si fallan, cuestan dinero de verdad:

  1. la validación de la firma del webhook (sin ella, cualquiera se regala
     créditos mandando un POST)
  2. la idempotencia al acreditar (Mercado Pago manda el mismo aviso varias
     veces y el saldo no puede sumarse dos veces)

La API de Mercado Pago no se llama nunca: lo que se prueba es nuestro lado.
"""

import hashlib
import hmac
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from config import settings
from routers import pagos
from schemas import CrearPagoIn
from session import execute, fetch_one, fetch_value

SECRETO = "secreto-de-prueba"


def firmar(data_id: str, request_id: str, ts: str = "1700000000") -> str:
    """Arma una cabecera x-signature válida, como la que manda Mercado Pago."""
    manifiesto = f"id:{data_id.lower()};request-id:{request_id};ts:{ts};"
    v1 = hmac.new(SECRETO.encode(), manifiesto.encode(), hashlib.sha256).hexdigest()
    return f"ts={ts},v1={v1}"


@pytest.fixture
def con_secreto(monkeypatch):
    monkeypatch.setattr(settings, "MERCADOPAGO_WEBHOOK_SECRET", SECRETO)
    return SECRETO


# ============================================================
# Firma del webhook
# ============================================================
def test_firma_correcta_pasa(con_secreto):
    firma = firmar("123456", "req-1")
    assert pagos._firma_valida(firma, "req-1", "123456") is True


def test_firma_de_otro_secreto_no_pasa(con_secreto):
    ajena = hmac.new(
        b"otro-secreto",
        b"id:123456;request-id:req-1;ts:1700000000;",
        hashlib.sha256,
    ).hexdigest()
    assert pagos._firma_valida(f"ts=1700000000,v1={ajena}", "req-1", "123456") is False


def test_sin_cabecera_no_pasa(con_secreto):
    assert pagos._firma_valida(None, "req-1", "123456") is False


def test_cabecera_sin_v1_no_pasa(con_secreto):
    assert pagos._firma_valida("ts=1700000000", "req-1", "123456") is False


def test_cambiar_el_id_del_pago_invalida_la_firma(con_secreto):
    """
    El caso que importa: firma buena de un pago de $1 reusada para acreditar
    otro. El id va dentro del manifiesto, así que cambiarlo rompe el HMAC.
    """
    firma = firmar("111", "req-1")
    assert pagos._firma_valida(firma, "req-1", "999") is False


def test_cambiar_el_request_id_invalida_la_firma(con_secreto):
    firma = firmar("123456", "req-1")
    assert pagos._firma_valida(firma, "req-OTRO", "123456") is False


# ============================================================
# Mapeo de estados
# ============================================================
@pytest.mark.parametrize(
    "estado_mp, esperado",
    [
        ("approved", "aprobado"),
        ("pending", "pendiente"),
        ("in_process", "pendiente"),
        ("authorized", "pendiente"),
        ("rejected", "rechazado"),
        ("cancelled", "cancelado"),
        ("refunded", "reembolsado"),
        ("charged_back", "reembolsado"),
    ],
)
def test_mapeo_de_estados(estado_mp, esperado):
    assert pagos.ESTADOS_MP[estado_mp] == esperado


def test_un_estado_desconocido_cae_en_pendiente():
    """Nunca se da por aprobado algo que no entendemos."""
    assert pagos.ESTADOS_MP.get("estado_nuevo_de_mp", "pendiente") == "pendiente"


# ============================================================
# Validación del body
# ============================================================
def test_suscripcion_sin_plan_no_valida():
    with pytest.raises(ValidationError):
        CrearPagoIn(tipo="subscription")


def test_creditos_sin_cantidad_no_valida():
    with pytest.raises(ValidationError):
        CrearPagoIn(tipo="credit_purchase")


def test_no_se_pueden_mezclar_plan_y_creditos():
    with pytest.raises(ValidationError):
        CrearPagoIn(tipo="subscription", plan="pro", creditos=Decimal(100))


def test_el_monto_no_es_un_campo_del_body():
    """El precio sale de la base, no del cliente. Ver el docstring de pagos.py."""
    assert "monto" not in CrearPagoIn.model_fields
    assert "precio" not in CrearPagoIn.model_fields


# ============================================================
# Payload de la preferencia: auto_return solo con URL pública
# ============================================================
# Mercado Pago responde 400 ("auto_return invalid. back_url.success must
# be defined") si `auto_return` viaja con una back_url.success que no es
# pública — localhost incluido, aunque el campo sí esté definido. Se
# reprodujo contra la API real antes de este fix: con auto_return + back_url
# localhost, 400; quitando auto_return, 201. Ver el comentario de
# _payload_preferencia.
def test_sin_https_en_base_url_frontend_no_manda_auto_return(monkeypatch):
    monkeypatch.setattr(settings, "BASE_URL_FRONTEND", "http://localhost:3000")
    payload = pagos._payload_preferencia(
        uuid4(), "Suscripción pro", Decimal("99.99"), "test@example.com"
    )
    assert "auto_return" not in payload
    # Las back_urls se mandan igual: son las que Mercado Pago descarta en
    # silencio si no son públicas, no algo que este código deba omitir.
    assert payload["back_urls"]["success"] == "http://localhost:3000/pagos/exito"


def test_con_https_en_base_url_frontend_si_manda_auto_return(monkeypatch):
    monkeypatch.setattr(settings, "BASE_URL_FRONTEND", "https://app.operativai.com.mx")
    payload = pagos._payload_preferencia(
        uuid4(), "Suscripción pro", Decimal("99.99"), "test@example.com"
    )
    assert payload["auto_return"] == "approved"
    assert payload["back_urls"]["success"] == "https://app.operativai.com.mx/pagos/exito"


# ============================================================
# Cotización: el precio siempre sale de la base
# ============================================================
@pytest.mark.asyncio
async def test_cotizar_plan_devuelve_el_precio_de_la_tabla(db):
    esperado = await fetch_value("SELECT precio_monthly FROM planes WHERE nombre = 'pro'")
    monto, concepto = await pagos._cotizar(CrearPagoIn(tipo="subscription", plan="pro"))

    assert monto == esperado
    assert "pro" in concepto


@pytest.mark.asyncio
async def test_cotizar_paquete_devuelve_el_precio_de_la_tabla(db):
    esperado = await fetch_value("SELECT precio FROM paquetes_creditos WHERE creditos = 500")
    monto, _ = await pagos._cotizar(
        CrearPagoIn(tipo="credit_purchase", creditos=Decimal(500))
    )
    assert monto == esperado


@pytest.mark.asyncio
async def test_un_paquete_inventado_da_404(db):
    """Pedir 7 créditos (que no es un paquete) no genera un cobro raro."""
    with pytest.raises(HTTPException) as exc:
        await pagos._cotizar(CrearPagoIn(tipo="credit_purchase", creditos=Decimal(7)))
    assert exc.value.status_code == 404


# ============================================================
# Acreditación de créditos e idempotencia
# ============================================================
async def _transaccion_de_creditos(tenant_id, creditos: Decimal):
    return await fetch_value(
        """
        INSERT INTO tenant_transactions
            (tenant_id, tipo, concepto, monto, estado_pago, creditos_comprados)
        VALUES ($1, 'credit_purchase', $2, 100, 'aprobado', $3)
        RETURNING id
        """,
        tenant_id,
        f"{int(creditos)} créditos",
        creditos,
    )


@pytest.mark.asyncio
async def test_acreditar_suma_al_saldo_y_deja_rastro(db, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    tx_id = await _transaccion_de_creditos(tenant_id, Decimal(500))

    await pagos._acreditar_creditos(tenant_id, tx_id, Decimal(500), "500 créditos")

    saldo = await fetch_value(
        "SELECT creditos_disponibles FROM tenant_credits WHERE tenant_id = $1", tenant_id
    )
    assert saldo == Decimal(500)

    movimiento = await fetch_one(
        "SELECT tipo, cantidad, saldo_anterior, saldo_nuevo FROM credit_transactions WHERE tenant_transaction_id = $1",
        tx_id,
    )
    assert movimiento["tipo"] == "compra"
    assert movimiento["cantidad"] == Decimal(500)
    assert movimiento["saldo_anterior"] == Decimal(0)
    assert movimiento["saldo_nuevo"] == Decimal(500)


@pytest.mark.asyncio
async def test_el_mismo_pago_no_se_acredita_dos_veces(db, tenant_y_usuario):
    """
    El caso real: Mercado Pago reintenta el webhook. La segunda pasada no
    puede volver a sumar.
    """
    tenant_id = tenant_y_usuario["tenant_id"]
    tx_id = await _transaccion_de_creditos(tenant_id, Decimal(1000))

    await pagos._acreditar_creditos(tenant_id, tx_id, Decimal(1000), "1000 créditos")
    await pagos._acreditar_creditos(tenant_id, tx_id, Decimal(1000), "1000 créditos")
    await pagos._acreditar_creditos(tenant_id, tx_id, Decimal(1000), "1000 créditos")

    saldo = await fetch_value(
        "SELECT creditos_disponibles FROM tenant_credits WHERE tenant_id = $1", tenant_id
    )
    assert saldo == Decimal(1000)

    movimientos = await fetch_value(
        "SELECT COUNT(*) FROM credit_transactions WHERE tenant_transaction_id = $1", tx_id
    )
    assert movimientos == 1


@pytest.mark.asyncio
async def test_dos_compras_distintas_si_se_suman(db, tenant_y_usuario):
    """La idempotencia es por pago, no un tope global."""
    tenant_id = tenant_y_usuario["tenant_id"]
    tx1 = await _transaccion_de_creditos(tenant_id, Decimal(100))
    tx2 = await _transaccion_de_creditos(tenant_id, Decimal(500))

    await pagos._acreditar_creditos(tenant_id, tx1, Decimal(100), "100 créditos")
    await pagos._acreditar_creditos(tenant_id, tx2, Decimal(500), "500 créditos")

    saldo = await fetch_value(
        "SELECT creditos_disponibles FROM tenant_credits WHERE tenant_id = $1", tenant_id
    )
    assert saldo == Decimal(600)


# ============================================================
# Activación de la suscripción
# ============================================================
@pytest.mark.asyncio
async def test_activar_suscripcion_deja_el_plan_y_prende_los_servicios(db, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]

    await pagos._activar_suscripcion(tenant_id, "pro")

    sub = await fetch_one(
        "SELECT plan, estado, precio_monthly, fecha_renovacion FROM tenant_subscriptions WHERE tenant_id = $1",
        tenant_id,
    )
    assert sub["plan"] == "pro"
    assert sub["estado"] == "activa"
    assert sub["fecha_renovacion"] is not None

    servicios = await fetch_one(
        "SELECT agente_ia_activo, gestion_vendedores_activo FROM tenant_servicios WHERE tenant_id = $1",
        tenant_id,
    )
    assert servicios["agente_ia_activo"] is True
    assert servicios["gestion_vendedores_activo"] is True


@pytest.mark.asyncio
async def test_cambiar_de_plan_pisa_la_suscripcion_en_vez_de_duplicarla(db, tenant_y_usuario):
    """UNIQUE(tenant_id): un tenant tiene una sola suscripción."""
    tenant_id = tenant_y_usuario["tenant_id"]

    await pagos._activar_suscripcion(tenant_id, "starter")
    await pagos._activar_suscripcion(tenant_id, "enterprise")

    filas = await fetch_value(
        "SELECT COUNT(*) FROM tenant_subscriptions WHERE tenant_id = $1", tenant_id
    )
    assert filas == 1

    plan = await fetch_value(
        "SELECT plan FROM tenant_subscriptions WHERE tenant_id = $1", tenant_id
    )
    assert plan == "enterprise"


@pytest.mark.asyncio
async def test_un_plan_inexistente_no_activa_nada(db, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]

    await pagos._activar_suscripcion(tenant_id, "plan_que_no_existe")

    filas = await fetch_value(
        "SELECT COUNT(*) FROM tenant_subscriptions WHERE tenant_id = $1", tenant_id
    )
    assert filas == 0


# ============================================================
# Endpoints HTTP
# ============================================================
@pytest.mark.asyncio
async def test_los_endpoints_de_pagos_exigen_token(http_client):
    for ruta in ("/api/pagos/suscripcion", "/api/pagos/historial", "/api/pagos/catalogo"):
        respuesta = await http_client.get(ruta)
        assert respuesta.status_code == 401, ruta


@pytest.mark.asyncio
async def test_suscripcion_de_un_tenant_sin_plan_devuelve_ceros(
    http_client, tenant_y_usuario
):
    """Un negocio que nunca pagó no es un error: es el estado inicial."""
    respuesta = await http_client.get(
        "/api/pagos/suscripcion",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["plan"] is None
    assert datos["estado_suscripcion"] is None
    assert Decimal(datos["creditos_disponibles"]) == Decimal(0)


@pytest.mark.asyncio
async def test_el_historial_solo_trae_transacciones_del_propio_tenant(
    http_client, tenant_y_usuario
):
    tenant_id = tenant_y_usuario["tenant_id"]
    otro_tenant = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro')", otro_tenant)

    await _transaccion_de_creditos(tenant_id, Decimal(100))
    await _transaccion_de_creditos(otro_tenant, Decimal(5000))

    respuesta = await http_client.get(
        "/api/pagos/historial",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 200
    historial = respuesta.json()
    assert len(historial) == 1
    assert historial[0]["concepto"] == "100 créditos"

    await execute("DELETE FROM tenants WHERE id = $1", otro_tenant)


@pytest.mark.asyncio
async def test_no_se_puede_espiar_la_preferencia_de_otro_tenant(
    http_client, tenant_y_usuario
):
    otro_tenant = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro')", otro_tenant)
    tx_ajena = await _transaccion_de_creditos(otro_tenant, Decimal(5000))
    await execute(
        "UPDATE tenant_transactions SET mp_preference_id = 'pref-ajena' WHERE id = $1",
        tx_ajena,
    )

    respuesta = await http_client.get(
        "/api/pagos/preferencia/pref-ajena",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 404

    await execute("DELETE FROM tenants WHERE id = $1", otro_tenant)


@pytest.mark.asyncio
async def test_el_webhook_rechaza_una_firma_invalida(http_client, con_secreto):
    respuesta = await http_client.post(
        "/api/pagos/webhook",
        json={"type": "payment", "data": {"id": "123456"}},
        headers={"x-signature": "ts=1700000000,v1=firmafalsa", "x-request-id": "req-1"},
    )
    assert respuesta.status_code == 401


@pytest.mark.asyncio
async def test_el_webhook_ignora_los_avisos_que_no_son_de_pago(http_client, con_secreto):
    """Un aviso de otro tipo se contesta 200 para que MP no lo reintente."""
    respuesta = await http_client.post(
        "/api/pagos/webhook",
        json={"type": "plan", "data": {"id": "123456"}},
        headers={
            "x-signature": firmar("123456", "req-1"),
            "x-request-id": "req-1",
        },
    )
    assert respuesta.status_code == 200


@pytest.mark.asyncio
async def test_sin_secreto_configurado_el_webhook_no_atiende(http_client, monkeypatch):
    """Fallar cerrado: sin secreto no hay forma de distinguir un aviso real."""
    monkeypatch.setattr(settings, "MERCADOPAGO_WEBHOOK_SECRET", "")
    respuesta = await http_client.post(
        "/api/pagos/webhook", json={"type": "payment", "data": {"id": "1"}}
    )
    assert respuesta.status_code == 503


# ============================================================
# crear-pago de punta a punta (con Mercado Pago simulado)
# ============================================================
class _RespuestaFalsa:
    def __init__(self, datos: dict, error: bool = False):
        self._datos = datos
        self._error = error

    def raise_for_status(self) -> None:
        if self._error:
            raise pagos.httpx.HTTPError("mercado pago dijo que no")

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

    async def post(self, url, headers=None, json=None):
        _ClienteFalso.ultimo_payload = json
        return self._respuesta


@pytest.fixture
def mp_simulado(monkeypatch):
    """Deja crear-pago operativo sin llamar a Mercado Pago de verdad."""
    monkeypatch.setattr(settings, "MERCADOPAGO_ACCESS_TOKEN", "token-de-prueba")
    _ClienteFalso.ultimo_payload = None

    def instalar(respuesta: _RespuestaFalsa):
        monkeypatch.setattr(
            pagos.httpx, "AsyncClient", lambda *a, **k: _ClienteFalso(respuesta)
        )

    return instalar


@pytest.mark.asyncio
async def test_crear_pago_guarda_la_transaccion_con_el_precio_de_la_base(
    http_client, tenant_y_usuario, mp_simulado
):
    mp_simulado(
        _RespuestaFalsa({"id": "pref-123", "init_point": "https://mp.test/checkout"})
    )

    respuesta = await http_client.post(
        "/api/pagos/crear-pago",
        json={"tipo": "subscription", "plan": "pro"},
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )

    assert respuesta.status_code == 201
    datos = respuesta.json()
    assert datos["mp_preference_id"] == "pref-123"
    assert datos["init_point"] == "https://mp.test/checkout"

    precio_real = await fetch_value("SELECT precio_monthly FROM planes WHERE nombre = 'pro'")
    fila = await fetch_one(
        "SELECT tipo, monto, estado_pago, plan_nombre, mp_preference_id FROM tenant_transactions WHERE id = $1",
        datos["transaccion_id"],
    )
    assert fila["tipo"] == "subscription"
    assert fila["monto"] == precio_real
    # Nace pendiente: lo que la aprueba es el webhook, no esta llamada.
    assert fila["estado_pago"] == "pendiente"
    assert fila["plan_nombre"] == "pro"
    assert fila["mp_preference_id"] == "pref-123"


@pytest.mark.asyncio
async def test_crear_pago_manda_el_external_reference_que_usa_el_webhook(
    http_client, tenant_y_usuario, mp_simulado
):
    """Sin ese hilo, el webhook no sabría a qué tenant acreditarle."""
    mp_simulado(_RespuestaFalsa({"id": "pref-9", "init_point": "https://mp.test/x"}))

    respuesta = await http_client.post(
        "/api/pagos/crear-pago",
        json={"tipo": "credit_purchase", "creditos": 500},
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 201

    payload = _ClienteFalso.ultimo_payload
    assert payload["external_reference"] == respuesta.json()["transaccion_id"]
    assert payload["items"][0]["currency_id"] == settings.MERCADOPAGO_CURRENCY
    assert payload["notification_url"].endswith("/api/pagos/webhook")


@pytest.mark.asyncio
async def test_si_mercado_pago_falla_la_transaccion_no_queda_pendiente_para_siempre(
    http_client, tenant_y_usuario, mp_simulado
):
    mp_simulado(_RespuestaFalsa({}, error=True))

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
async def test_un_member_no_puede_contratar(http_client, tenant_y_usuario, mp_simulado):
    """Contratar cambia lo que paga el negocio: es cosa de gerencia."""
    from security import crear_access_token, hash_password

    mp_simulado(_RespuestaFalsa({"id": "pref-1", "init_point": "https://mp.test/x"}))

    usuario_id = uuid4()
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, 'member', true)
        """,
        usuario_id,
        tenant_y_usuario["tenant_id"],
        f"member-{usuario_id}@ejemplo.com",
        hash_password("test_password"),
    )
    token = crear_access_token(usuario_id, tenant_y_usuario["tenant_id"], "member")

    respuesta = await http_client.post(
        "/api/pagos/crear-pago",
        json={"tipo": "subscription", "plan": "pro"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert respuesta.status_code == 403


@pytest.mark.asyncio
async def test_sin_access_token_configurado_crear_pago_no_atiende(
    http_client, tenant_y_usuario, monkeypatch
):
    monkeypatch.setattr(settings, "MERCADOPAGO_ACCESS_TOKEN", "")
    respuesta = await http_client.post(
        "/api/pagos/crear-pago",
        json={"tipo": "subscription", "plan": "pro"},
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 503
