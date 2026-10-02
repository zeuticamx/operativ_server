"""
Auditoría de consumo de créditos (GET /api/herramientas/consumo[/resumen]).

Lo que importa si falla:

  1. Aislamiento: un tenant nunca ve cobros (ni totales) de otro, ni el
     número de WhatsApp de una conversación ajena aunque el cobro apunte a ella.
  2. Metadatos: herramienta, créditos, fecha/hora en la zona del tenant y
     número enmascarado; "N/A" (None) si no vino de WhatsApp.
  3. Orden cronológico inverso, paginación estable y filtro por fechas.
  4. Solo el owner; sin plan vigente, 402.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from security import crear_access_token, hash_password
from services.consumo_creditos import enmascarar_whatsapp
from session import execute

URL = "/api/herramientas/consumo"
URL_RESUMEN = "/api/herramientas/consumo/resumen"


async def _conversacion(tenant_id, canal="whatsapp", whatsapp="5215512344567"):
    user_id, conv_id = uuid4(), uuid4()
    await execute(
        "INSERT INTO users (id, tenant_id, whatsapp_id) VALUES ($1, $2, $3)",
        user_id, tenant_id, whatsapp,
    )
    await execute(
        "INSERT INTO conversations (id, tenant_id, user_id, channel_type) VALUES ($1, $2, $3, $4)",
        conv_id, tenant_id, user_id, canal,
    )
    return conv_id


async def _gasto(tenant_id, herramienta, cuando, conversation_id=None):
    await execute(
        """
        INSERT INTO credit_transactions
            (tenant_id, tipo, cantidad, concepto, bolsa, herramienta,
             conversation_id, referencia, created_at)
        VALUES ($1, 'gasto', -1, $2, 'plan', $3, $4, $5, $6)
        """,
        tenant_id, f"Herramienta {herramienta}", herramienta, conversation_id,
        f"test-{uuid4()}", cuando,
    )


async def _zona(tenant_id, zona):
    await execute(
        """
        INSERT INTO tenant_servicios (tenant_id, zona_horaria) VALUES ($1, $2)
        ON CONFLICT (tenant_id) DO UPDATE SET zona_horaria = EXCLUDED.zona_horaria
        """,
        tenant_id, zona,
    )


@pytest.fixture
async def otro_tenant(db):
    tenant_id = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro Tenant')", tenant_id)
    yield tenant_id
    await execute("DELETE FROM tenants WHERE id = $1", tenant_id)


def _headers(ctx):
    return {"Authorization": f"Bearer {ctx['token']}"}


# ------------------------------------------------------------
# Enmascarado (puro)
# ------------------------------------------------------------
def test_enmascarar_whatsapp():
    assert enmascarar_whatsapp("5215512344567") == "+521 •••• 4567"
    assert enmascarar_whatsapp("+52 1 55 1234 4567") == "+521 •••• 4567"
    assert enmascarar_whatsapp("5512344567") == "•••• 4567"
    assert enmascarar_whatsapp("null") is None
    assert enmascarar_whatsapp("") is None
    assert enmascarar_whatsapp(None) is None


# ------------------------------------------------------------
# Aislamiento
# ------------------------------------------------------------
async def test_no_filtra_cobros_de_otros_tenants(
    http_client, plan_enterprise, otro_tenant
):
    mio = plan_enterprise["tenant_id"]
    ahora = datetime.now(timezone.utc)
    await _gasto(mio, "mi_herramienta", ahora)
    await _gasto(otro_tenant, "herramienta_ajena", ahora)
    await _gasto(otro_tenant, "herramienta_ajena", ahora)

    r = await http_client.get(URL, headers=_headers(plan_enterprise))
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["total"] == 1
    assert [i["herramienta"] for i in cuerpo["items"]] == ["mi_herramienta"]

    r = await http_client.get(URL_RESUMEN, headers=_headers(plan_enterprise))
    resumen = r.json()
    assert float(resumen["total_creditos"]) == 1
    assert [h["herramienta"] for h in resumen["por_herramienta"]] == ["mi_herramienta"]


async def test_conversacion_ajena_no_expone_su_numero(
    http_client, plan_enterprise, otro_tenant
):
    conv_ajena = await _conversacion(otro_tenant, whatsapp="5219998887777")
    await _gasto(plan_enterprise["tenant_id"], "t", datetime.now(timezone.utc), conv_ajena)

    r = await http_client.get(URL, headers=_headers(plan_enterprise))
    item = r.json()["items"][0]
    assert item["whatsapp"] is None
    assert item["canal"] is None
    assert "7777" not in r.text


async def test_el_tenant_no_se_puede_pedir_por_parametro(
    http_client, plan_enterprise, otro_tenant
):
    await _gasto(otro_tenant, "ajena", datetime.now(timezone.utc))
    r = await http_client.get(
        URL, params={"tenant_id": str(otro_tenant)}, headers=_headers(plan_enterprise)
    )
    assert r.status_code == 200
    assert r.json()["total"] == 0


# ------------------------------------------------------------
# Metadatos
# ------------------------------------------------------------
async def test_devuelve_fecha_hora_zona_y_numero(http_client, plan_enterprise):
    tenant_id = plan_enterprise["tenant_id"]
    await _zona(tenant_id, "America/Bogota")  # UTC-5, sin horario de verano
    conv = await _conversacion(tenant_id, whatsapp="5215512344567")
    await _gasto(tenant_id, "consultar_servicios", datetime(2026, 3, 10, 15, 30, tzinfo=timezone.utc), conv)

    r = await http_client.get(URL, headers=_headers(plan_enterprise))
    cuerpo = r.json()
    assert cuerpo["zona_horaria"] == "America/Bogota"
    item = cuerpo["items"][0]
    assert item["herramienta"] == "consultar_servicios"
    assert float(item["creditos"]) == 1
    assert item["bolsa"] == "plan"
    assert item["canal"] == "whatsapp"
    assert item["whatsapp"] == "+521 •••• 4567"
    assert "5512344567" not in r.text
    # 15:30 UTC -> 10:30 en Bogotá, con el offset en el ISO.
    assert item["creado_en"].startswith("2026-03-10T10:30:00")
    assert item["creado_en"].endswith("-05:00")


async def test_na_cuando_no_viene_de_whatsapp(http_client, plan_enterprise):
    tenant_id = plan_enterprise["tenant_id"]
    ig = await _conversacion(tenant_id, canal="instagram", whatsapp="5215512344567")
    ahora = datetime.now(timezone.utc)
    await _gasto(tenant_id, "con_instagram", ahora, ig)
    await _gasto(tenant_id, "sin_conversacion", ahora - timedelta(minutes=1))

    r = await http_client.get(URL, headers=_headers(plan_enterprise))
    por_nombre = {i["herramienta"]: i for i in r.json()["items"]}
    assert por_nombre["con_instagram"]["whatsapp"] is None
    assert por_nombre["con_instagram"]["canal"] == "instagram"
    assert por_nombre["sin_conversacion"]["whatsapp"] is None


# ------------------------------------------------------------
# Orden, paginación y fechas
# ------------------------------------------------------------
async def test_orden_descendente_y_paginacion(http_client, plan_enterprise):
    tenant_id = plan_enterprise["tenant_id"]
    base = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    for i in range(5):
        await _gasto(tenant_id, f"h{i}", base + timedelta(minutes=i))

    p1 = (await http_client.get(URL, params={"limite": 2, "offset": 0}, headers=_headers(plan_enterprise))).json()
    p2 = (await http_client.get(URL, params={"limite": 2, "offset": 2}, headers=_headers(plan_enterprise))).json()
    p3 = (await http_client.get(URL, params={"limite": 2, "offset": 4}, headers=_headers(plan_enterprise))).json()

    assert p1["total"] == 5
    nombres = [i["herramienta"] for p in (p1, p2, p3) for i in p["items"]]
    assert nombres == ["h4", "h3", "h2", "h1", "h0"]


async def test_filtro_de_fechas_usa_la_zona_del_tenant(http_client, plan_enterprise):
    tenant_id = plan_enterprise["tenant_id"]
    await _zona(tenant_id, "America/Bogota")
    # 03:00 UTC del día 11 = 22:00 del día 10 en Bogotá.
    await _gasto(tenant_id, "noche_del_10", datetime(2026, 3, 11, 3, 0, tzinfo=timezone.utc))
    await _gasto(tenant_id, "dia_11", datetime(2026, 3, 11, 15, 0, tzinfo=timezone.utc))

    r = await http_client.get(
        URL, params={"desde": "2026-03-10", "hasta": "2026-03-10"}, headers=_headers(plan_enterprise)
    )
    assert [i["herramienta"] for i in r.json()["items"]] == ["noche_del_10"]

    r = await http_client.get(
        URL_RESUMEN, params={"desde": "2026-03-11", "hasta": "2026-03-11"}, headers=_headers(plan_enterprise)
    )
    assert [d["dia"] for d in r.json()["por_dia"]] == ["2026-03-11"]


async def test_rango_invertido_da_422(http_client, plan_enterprise):
    r = await http_client.get(
        URL, params={"desde": "2026-03-11", "hasta": "2026-03-10"}, headers=_headers(plan_enterprise)
    )
    assert r.status_code == 422


# ------------------------------------------------------------
# Acceso
# ------------------------------------------------------------
async def test_solo_el_owner(http_client, plan_enterprise):
    tenant_id = plan_enterprise["tenant_id"]
    miembro_id = uuid4()
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, 'member', true)
        """,
        miembro_id, tenant_id, f"test-{miembro_id}@ejemplo.com", hash_password("x"),
    )
    token = crear_access_token(miembro_id, tenant_id, "member")
    r = await http_client.get(URL, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


async def test_sin_token_da_401(http_client):
    assert (await http_client.get(URL)).status_code == 401


async def test_sin_plan_vigente_da_402(http_client, tenant_y_usuario):
    r = await http_client.get(URL, headers=_headers(tenant_y_usuario))
    assert r.status_code == 402
