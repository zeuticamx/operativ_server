"""
Borrado de la cuenta por el propio dueño (routers/cuenta.py,
services/eliminacion_cuenta.py). Contra la base local, como
test_gerencia_eliminar_tenant.py; Stripe se sustituye.

Las reglas:

1. Solo el owner. Un member, un vendedor o el "ver como" -> 403.
2. Exige escribir "ELIMINAR" y la contraseña actual (o una credencial de
   Google del mismo google_id si la cuenta no tiene contraseña). Tras 5
   fallos se bloquea 15 minutos.
3. Bloquea lo que todavía puede cobrar (Stripe sin cancelar, Mercado Pago
   activa) y los adeudos (facturas abiertas en Stripe). Si Stripe no
   contesta, también bloquea: falla cerrado.
4. Una suscripción ya cancelada con días pagados NO bloquea: la revisión
   devuelve `pagado_hasta` para advertir que se pierden.
5. Si todo está bien, se borra el negocio entero, queda una entrada en la
   bitácora SIN correos, y la sesión deja de servir.

En cada rechazo se verifica que el negocio siga existiendo.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from security import crear_access_token, hash_password
from services import eliminacion_cuenta, stripe_portal
from session import execute, fetch_one, fetch_value

PASSWORD = "contraseña-segura-123"


# ------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------
async def _crear_portal_user(tenant_id, rol: str, *, password: str | None = PASSWORD,
                             google_id: str | None = None):
    uid = uuid4()
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active, google_id)
        VALUES ($1, $2, $3, $4, $5, true, $6)
        """,
        uid,
        tenant_id,
        f"{rol}-{uid}@ejemplo.test",
        hash_password(password) if password else None,
        rol,
        google_id,
    )
    return uid


def _headers(uid, tid, rol: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {crear_access_token(uid, tid, rol)}"}


@pytest.fixture
async def negocio(db):
    tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Mi negocio')", tid)
    owner = await _crear_portal_user(tid, "owner")
    member = await _crear_portal_user(tid, "member")
    cliente = await fetch_value(
        "INSERT INTO users (tenant_id, display_name) VALUES ($1, 'Cliente') RETURNING id", tid
    )
    await execute(
        "INSERT INTO conversations (tenant_id, user_id, channel_type) VALUES ($1, $2, 'whatsapp')",
        tid,
        cliente,
    )
    yield {
        "tenant_id": tid,
        "owner_id": owner,
        "owner": _headers(owner, tid, "owner"),
        "member": _headers(member, tid, "member"),
    }
    await execute("DELETE FROM gerencia_auditoria WHERE tenant_id = $1", tid)
    await execute("DELETE FROM tenants WHERE id = $1", tid)


@pytest.fixture
def stripe_sin_deudas(monkeypatch):
    """Stripe responde sin facturas abiertas; guarda los customers consultados."""
    consultados: list[str] = []

    async def falso(customer_id):
        consultados.append(customer_id)
        return False

    monkeypatch.setattr(stripe_portal, "tiene_facturas_abiertas", falso)
    return consultados


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


async def _suscripcion(tenant_id, estado="activa", *, renovacion=None, stripe_sub=None,
                       cancelada_en=None, cancela_al_vencer=False, customer=None,
                       mp_sub=None):
    await execute(
        """
        INSERT INTO tenant_subscriptions
            (tenant_id, plan, estado, precio_monthly, fecha_renovacion,
             stripe_subscription_id, cancelada_en, cancela_al_vencer,
             stripe_customer_id, mp_subscription_id)
        VALUES ($1, 'pro', $2, 99.99, $3, $4, $5, $6, $7, $8)
        """,
        tenant_id, estado, renovacion, stripe_sub, cancelada_en, cancela_al_vencer,
        customer, mp_sub,
    )


async def _eliminar(http_client, headers, *, confirmacion="ELIMINAR", password=PASSWORD,
                    google_credential=None):
    return await http_client.post(
        "/api/cuenta/eliminar",
        json={"confirmacion": confirmacion, "password": password,
              "google_credential": google_credential},
        headers=headers,
    )


async def _existe(tenant_id) -> bool:
    return await fetch_value("SELECT EXISTS (SELECT 1 FROM tenants WHERE id = $1)", tenant_id)


# ------------------------------------------------------------
# 1. Solo el dueño
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_un_member_no_puede_ni_revisar_ni_borrar(http_client, negocio, stripe_sin_deudas):
    r = await http_client.get("/api/cuenta/eliminacion", headers=negocio["member"])
    assert r.status_code == 403
    r = await _eliminar(http_client, negocio["member"])
    assert r.status_code == 403
    assert await _existe(negocio["tenant_id"])


@pytest.mark.asyncio
async def test_sin_token_401(http_client, negocio):
    r = await http_client.post("/api/cuenta/eliminar", json={"confirmacion": "ELIMINAR"})
    assert r.status_code == 401
    assert await _existe(negocio["tenant_id"])


# ------------------------------------------------------------
# 2. Confirmación y re-autenticación
# ------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("confirmacion", ["eliminar", "BORRAR", "ELIMINA"])
async def test_confirmacion_incorrecta_400(http_client, negocio, stripe_sin_deudas, confirmacion):
    r = await _eliminar(http_client, negocio["owner"], confirmacion=confirmacion)
    assert r.status_code == 400
    assert await _existe(negocio["tenant_id"])


@pytest.mark.asyncio
async def test_contrasena_incorrecta_403_y_cuenta_el_intento(http_client, negocio, stripe_sin_deudas):
    r = await _eliminar(http_client, negocio["owner"], password="otra-cosa")
    # 403 y no 401: el portal leería 401 como sesión vencida.
    assert r.status_code == 403
    assert await _existe(negocio["tenant_id"])
    intentos = await fetch_value(
        "SELECT eliminacion_intentos FROM portal_users WHERE id = $1", negocio["owner_id"]
    )
    assert intentos == 1


@pytest.mark.asyncio
async def test_sin_contrasena_403(http_client, negocio, stripe_sin_deudas):
    r = await _eliminar(http_client, negocio["owner"], password=None)
    assert r.status_code == 403
    assert await _existe(negocio["tenant_id"])


@pytest.mark.asyncio
async def test_tras_cinco_fallos_bloquea_aunque_luego_acierte(http_client, negocio, stripe_sin_deudas):
    for _ in range(eliminacion_cuenta.INTENTOS_MAX):
        r = await _eliminar(http_client, negocio["owner"], password="mala")
        assert r.status_code == 403

    r = await _eliminar(http_client, negocio["owner"])
    assert r.status_code == 429
    assert await _existe(negocio["tenant_id"])


@pytest.mark.asyncio
async def test_cuenta_solo_google_se_reautentica_con_su_google_id(
    http_client, db, stripe_sin_deudas, monkeypatch
):
    tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Solo Google')", tid)
    uid = await _crear_portal_user(tid, "owner", password=None, google_id="g-123")
    headers = _headers(uid, tid, "owner")

    async def credencial(token):
        return {"sub": token, "email_verified": True}

    monkeypatch.setattr(eliminacion_cuenta, "verificar_credential", credencial)
    try:
        r = await http_client.get("/api/cuenta/eliminacion", headers=headers)
        assert r.json()["verificacion"] == "google"

        # Credencial válida pero de OTRA cuenta de Google.
        r = await _eliminar(http_client, headers, password=None, google_credential="g-otro")
        assert r.status_code == 403
        assert await _existe(tid)

        r = await _eliminar(http_client, headers, password=None, google_credential="g-123")
        assert r.status_code == 204
        assert not await _existe(tid)
    finally:
        await execute("DELETE FROM gerencia_auditoria WHERE tenant_id = $1", tid)
        await execute("DELETE FROM tenants WHERE id = $1", tid)


# ------------------------------------------------------------
# 3. Lo que todavía cobra o se debe, bloquea
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_suscripcion_de_stripe_sin_cancelar_bloquea(http_client, negocio, stripe_sin_deudas):
    await _suscripcion(
        negocio["tenant_id"], renovacion=_ahora() + timedelta(days=20),
        stripe_sub="sub_viva", customer="cus_1",
    )

    rev = (await http_client.get("/api/cuenta/eliminacion", headers=negocio["owner"])).json()
    assert rev["eliminable"] is False
    assert [b["codigo"] for b in rev["bloqueos"]] == ["suscripcion_vigente"]
    assert "Cancélala" in rev["bloqueos"][0]["mensaje"]

    r = await _eliminar(http_client, negocio["owner"])
    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "suscripcion_vigente"
    assert await _existe(negocio["tenant_id"])


@pytest.mark.asyncio
async def test_stripe_pausada_por_impago_sigue_bloqueando(http_client, negocio, stripe_sin_deudas):
    # Stripe sigue reintentando el cobro aunque acá figure 'pausada'.
    await _suscripcion(negocio["tenant_id"], "pausada", stripe_sub="sub_x", customer="cus_1")
    r = await _eliminar(http_client, negocio["owner"])
    assert r.status_code == 409
    assert await _existe(negocio["tenant_id"])


@pytest.mark.asyncio
async def test_mercado_pago_activa_bloquea(http_client, negocio, stripe_sin_deudas):
    await _suscripcion(negocio["tenant_id"], mp_sub="mp_123")
    r = await _eliminar(http_client, negocio["owner"])
    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "suscripcion_vigente"


@pytest.mark.asyncio
async def test_facturas_abiertas_en_stripe_bloquean(http_client, negocio, monkeypatch):
    await _suscripcion(negocio["tenant_id"], "cancelada", customer="cus_debe",
                       stripe_sub="sub_y", cancelada_en=_ahora())

    async def debe(customer_id):
        return customer_id == "cus_debe"

    monkeypatch.setattr(stripe_portal, "tiene_facturas_abiertas", debe)

    rev = (await http_client.get("/api/cuenta/eliminacion", headers=negocio["owner"])).json()
    assert [b["codigo"] for b in rev["bloqueos"]] == ["adeudo_pendiente"]

    r = await _eliminar(http_client, negocio["owner"])
    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "adeudo_pendiente"
    assert await _existe(negocio["tenant_id"])


@pytest.mark.asyncio
async def test_si_stripe_no_contesta_no_se_borra(http_client, negocio, monkeypatch):
    await _suscripcion(negocio["tenant_id"], "cancelada", customer="cus_1",
                       stripe_sub="sub_y", cancelada_en=_ahora())

    async def caido(customer_id):
        raise stripe_portal.StripeNoDisponible("timeout")

    monkeypatch.setattr(stripe_portal, "tiene_facturas_abiertas", caido)
    r = await _eliminar(http_client, negocio["owner"])
    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "adeudo_no_verificable"
    assert await _existe(negocio["tenant_id"])


@pytest.mark.asyncio
async def test_sin_customer_de_stripe_no_consulta_stripe(http_client, negocio, stripe_sin_deudas):
    rev = (await http_client.get("/api/cuenta/eliminacion", headers=negocio["owner"])).json()
    assert rev["eliminable"] is True
    assert stripe_sin_deudas == []


# ------------------------------------------------------------
# 4. Cancelada con días pagados: advierte, no bloquea
# ------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("como", ["al_vencer", "dada_de_baja"])
async def test_cancelada_con_dias_pagados_advierte_pero_permite(
    http_client, negocio, stripe_sin_deudas, como
):
    vence = _ahora() + timedelta(days=12)
    await _suscripcion(
        negocio["tenant_id"], renovacion=vence, stripe_sub="sub_z", customer="cus_1",
        cancela_al_vencer=(como == "al_vencer"),
        cancelada_en=_ahora() if como == "dada_de_baja" else None,
    )

    rev = (await http_client.get("/api/cuenta/eliminacion", headers=negocio["owner"])).json()
    assert rev["eliminable"] is True
    assert rev["pagado_hasta"] is not None
    assert datetime.fromisoformat(rev["pagado_hasta"]).date() == vence.date()

    r = await _eliminar(http_client, negocio["owner"])
    assert r.status_code == 204
    assert not await _existe(negocio["tenant_id"])


# ------------------------------------------------------------
# 5. El borrado
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_sin_bloqueos_se_borra_todo_y_la_sesion_muere(http_client, negocio, stripe_sin_deudas):
    tid = negocio["tenant_id"]
    await _suscripcion(tid, "cancelada", customer="cus_fin", stripe_sub="sub_fin",
                       cancelada_en=_ahora() - timedelta(days=40),
                       renovacion=_ahora() - timedelta(days=10))

    rev = (await http_client.get("/api/cuenta/eliminacion", headers=negocio["owner"])).json()
    assert rev == {
        **rev,
        "eliminable": True,
        "bloqueos": [],
        "pagado_hasta": None,
        "usuarios_portal": 2,
        "conversaciones": 1,
        "verificacion": "password",
    }

    r = await _eliminar(http_client, negocio["owner"])
    assert r.status_code == 204

    assert not await _existe(tid)
    assert await fetch_value("SELECT COUNT(*) FROM portal_users WHERE tenant_id = $1", tid) == 0
    assert await fetch_value("SELECT COUNT(*) FROM conversations WHERE tenant_id = $1", tid) == 0
    # Una consulta en la revisión (GET) y otra justo antes de borrar (POST).
    assert stripe_sin_deudas == ["cus_fin", "cus_fin"]

    # Bitácora: UUID, plan y Customer; sin correos ni nombres.
    entrada = await fetch_one(
        "SELECT actor_email, accion, detalle::text AS detalle FROM gerencia_auditoria "
        "WHERE tenant_id = $1",
        tid,
    )
    assert entrada["accion"] == "eliminar_cuenta_propietario"
    assert "@" not in entrada["actor_email"]
    assert "cus_fin" in entrada["detalle"]
    assert "@" not in entrada["detalle"]
    assert "Mi negocio" not in entrada["detalle"]

    # Cualquier sesión abierta (dueño o equipo) queda fuera en la siguiente petición.
    assert (await http_client.get("/api/auth/yo", headers=negocio["owner"])).status_code == 401
    assert (await http_client.get("/api/auth/yo", headers=negocio["member"])).status_code == 401


@pytest.mark.asyncio
async def test_acertar_la_contrasena_reinicia_el_contador(http_client, negocio, monkeypatch):
    # Bloqueo por suscripción para que acertar no borre, y ver el contador.
    await _suscripcion(negocio["tenant_id"], stripe_sub="sub_v", customer="cus_1")

    async def sin_deudas(customer_id):
        return False

    monkeypatch.setattr(stripe_portal, "tiene_facturas_abiertas", sin_deudas)
    await _eliminar(http_client, negocio["owner"], password="mala")
    await _eliminar(http_client, negocio["owner"])

    intentos = await fetch_value(
        "SELECT eliminacion_intentos FROM portal_users WHERE id = $1", negocio["owner_id"]
    )
    assert intentos == 0
