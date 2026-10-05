"""
Customer Portal de Stripe: services/stripe_portal.py (lo que se manda a
Stripe) y POST /api/pagos/portal-cliente (quién puede y de qué Customer).
Sin red: se sustituye httpx.AsyncClient.
"""

from uuid import uuid4

import httpx
import pytest

from config import settings
from security import crear_access_token, hash_password
from services import stripe_portal
from session import execute


class _ClienteFalso:
    """httpx.AsyncClient de mentira: guarda la petición y devuelve `respuesta`."""

    peticiones: list[dict] = []
    respuesta: httpx.Response

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def _registrar(self, metodo, url, **kw):
        _ClienteFalso.peticiones.append({"metodo": metodo, "url": url, **kw})
        r = _ClienteFalso.respuesta
        r.request = httpx.Request(metodo, url)
        return r

    async def post(self, url, **kw):
        return await self._registrar("POST", url, **kw)

    async def get(self, url, **kw):
        return await self._registrar("GET", url, **kw)


@pytest.fixture
def stripe(monkeypatch):
    _ClienteFalso.peticiones = []
    _ClienteFalso.respuesta = httpx.Response(200, json={"url": "https://billing.stripe.com/p/s_1"})
    monkeypatch.setattr(stripe_portal.httpx, "AsyncClient", _ClienteFalso)
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(settings, "BASE_URL_FRONTEND", "https://portal.ejemplo.com")
    return _ClienteFalso


# ------------------------------------------------------------
# Servicio
# ------------------------------------------------------------
async def test_crea_la_sesion_con_customer_y_return_url(stripe):
    url = await stripe_portal.crear_sesion_portal("cus_1", "https://portal.ejemplo.com/suscripcion")

    assert url == "https://billing.stripe.com/p/s_1"
    p = stripe.peticiones[0]
    assert p["url"] == "https://api.stripe.com/v1/billing_portal/sessions"
    assert p["data"] == {"customer": "cus_1", "return_url": "https://portal.ejemplo.com/suscripcion"}
    assert p["headers"]["Authorization"] == "Bearer sk_test_x"
    # Form-encoded, como el resto de la API de Stripe.
    assert p["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


async def test_portal_sin_configurar_en_stripe_lanza_error_propio(stripe):
    stripe.respuesta = httpx.Response(400, json={"error": {"message": "No configuration provided"}})
    with pytest.raises(stripe_portal.StripeNoDisponible):
        await stripe_portal.crear_sesion_portal("cus_1", "https://x")


async def test_respuesta_sin_url_es_error(stripe):
    stripe.respuesta = httpx.Response(200, json={})
    with pytest.raises(stripe_portal.StripeNoDisponible):
        await stripe_portal.crear_sesion_portal("cus_1", "https://x")


async def test_sin_clave_de_stripe_no_llama(stripe, monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "")
    with pytest.raises(stripe_portal.StripeNoDisponible):
        await stripe_portal.crear_sesion_portal("cus_1", "https://x")
    assert stripe.peticiones == []


@pytest.mark.parametrize("data,esperado", [([{"id": "in_1"}], True), ([], False)])
async def test_facturas_abiertas(stripe, data, esperado):
    stripe.respuesta = httpx.Response(200, json={"data": data})
    assert await stripe_portal.tiene_facturas_abiertas("cus_1") is esperado
    p = stripe.peticiones[0]
    assert p["metodo"] == "GET"
    assert p["params"] == {"customer": "cus_1", "status": "open", "limit": "1"}


async def test_facturas_con_stripe_caido_lanza(stripe):
    stripe.respuesta = httpx.Response(500, text="boom")
    with pytest.raises(stripe_portal.StripeNoDisponible):
        await stripe_portal.tiene_facturas_abiertas("cus_1")


# ------------------------------------------------------------
# Endpoint (contra la base local)
# ------------------------------------------------------------
@pytest.fixture
async def negocio(db):
    tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Portal')", tid)
    usuarios = {}
    for rol in ("owner", "member", "superadmin"):
        uid = uuid4()
        await execute(
            "INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active) "
            "VALUES ($1, $2, $3, $4, $5, true)",
            uid, tid, f"{rol}-{uid}@ejemplo.test", hash_password("x" * 12), rol,
        )
        usuarios[rol] = {"Authorization": f"Bearer {crear_access_token(uid, tid, rol)}"}
    yield {"tenant_id": tid, **usuarios}
    await execute("DELETE FROM tenants WHERE id = $1", tid)


async def _con_customer(tenant_id, customer="cus_propio"):
    await execute(
        "INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly, "
        "stripe_customer_id) VALUES ($1, 'pro', 'activa', 99.99, $2)",
        tenant_id, customer,
    )


@pytest.mark.asyncio
async def test_el_dueno_recibe_la_url_de_su_propio_customer(http_client, negocio, stripe):
    await _con_customer(negocio["tenant_id"])

    r = await http_client.post(
        "/api/pagos/portal-cliente", headers=negocio["owner"], json={"customer": "cus_ajeno"}
    )

    assert r.status_code == 200
    assert r.json() == {"url": "https://billing.stripe.com/p/s_1"}
    # El Customer sale de la base, no del body.
    assert stripe.peticiones[0]["data"] == {
        "customer": "cus_propio",
        "return_url": "https://portal.ejemplo.com/suscripcion",
    }

    sub = (await http_client.get("/api/pagos/suscripcion", headers=negocio["owner"])).json()
    assert sub["portal_disponible"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("rol", ["member", "superadmin"])
async def test_otros_roles_no_abren_el_portal(http_client, negocio, stripe, rol):
    await _con_customer(negocio["tenant_id"])
    r = await http_client.post("/api/pagos/portal-cliente", headers=negocio[rol])
    assert r.status_code == 403
    assert stripe.peticiones == []


@pytest.mark.asyncio
async def test_sin_customer_de_stripe_409(http_client, negocio, stripe):
    r = await http_client.post("/api/pagos/portal-cliente", headers=negocio["owner"])
    assert r.status_code == 409
    assert stripe.peticiones == []

    sub = (await http_client.get("/api/pagos/suscripcion", headers=negocio["owner"])).json()
    assert sub["portal_disponible"] is False


@pytest.mark.asyncio
async def test_stripe_caido_502(http_client, negocio, stripe):
    await _con_customer(negocio["tenant_id"])
    stripe.respuesta = httpx.Response(500, text="boom")
    r = await http_client.post("/api/pagos/portal-cliente", headers=negocio["owner"])
    assert r.status_code == 502
