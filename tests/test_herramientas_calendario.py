"""
Herramientas del agente para el calendario (services/herramientas_calendario.py).

Lo que se cubre: que encender el calendario deje las cinco filas de
tenant_tools exactamente como las lee n8n (tipo, config, parámetros y
credencial), que apagarlo las pause, y que el sistema no pise lo que el
dueño decidió (textos editados, una herramienta pausada a mano).
"""

import json

import pytest

from config import settings
from services import herramientas_calendario
from session import execute, fetch_all, fetch_one, fetch_value

pytestmark = pytest.mark.usefixtures("plan_enterprise")

TOKEN = "token-interno-de-prueba"
BASE = "https://api.ejemplo.test"
CLAVES = {
    "consultar_servicios",
    "consultar_proveedores",
    "consultar_disponibilidad",
    "crear_reserva",
    "cancelar_reserva",
}


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    monkeypatch.setattr(settings, "N8N_INTERNAL_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "BASE_URL_BACKEND", BASE)


@pytest.fixture
def auth(tenant_y_usuario):
    return {"Authorization": f"Bearer {tenant_y_usuario['token']}"}


async def _calendario(http_client, tenant_id, auth, activo: bool):
    r = await http_client.patch(
        f"/api/tenants/{tenant_id}/servicios", json={"calendario_activo": activo}, headers=auth
    )
    assert r.status_code == 200, r.text


async def _filas(tenant_id) -> dict[str, dict]:
    filas = await fetch_all(
        """
        SELECT tool_key, tool_type, display_name, description,
               parametros_schema, config, is_enabled
        FROM tenant_tools WHERE tenant_id = $1
        """,
        tenant_id,
    )
    return {f["tool_key"]: dict(f) for f in filas}


# ============================================================
# Encender: las cinco, como las espera n8n
# ============================================================
@pytest.mark.asyncio
async def test_encender_el_calendario_crea_las_cinco_herramientas(
    http_client, tenant_y_usuario, auth
):
    tenant_id = tenant_y_usuario["tenant_id"]
    assert await _filas(tenant_id) == {}

    await _calendario(http_client, tenant_id, auth, True)

    filas = await _filas(tenant_id)
    assert set(filas) == CLAVES
    assert all(f["is_enabled"] for f in filas.values())


@pytest.mark.asyncio
async def test_el_contrato_con_n8n_es_el_de_produccion(http_client, tenant_y_usuario, auth):
    """Lo que leen los nodos 'Prepara HTTP' / 'Prepara HTTP POST' de ejecutar-herramienta-tenant."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _calendario(http_client, tenant_id, auth, True)
    filas = await _filas(tenant_id)

    servicios = filas["consultar_servicios"]
    assert servicios["tool_type"] == "http_generico"
    assert servicios["config"] == {
        "method": "GET",
        "auth_header": "X-Internal-Token",
        "url_template": f"{BASE}/api/eventos/calendario/servicios?tenant_id={{tenant_id}}",
    }

    crear = filas["crear_reserva"]
    assert crear["tool_type"] == "http_post_json"
    assert crear["config"] == {
        "method": "POST",
        "idempotent": True,
        "auth_header": "X-Internal-Token",
        "url_template": f"{BASE}/api/eventos/calendario/reservas",
        "incluir_contacto": True,
    }
    assert set(crear["parametros_schema"]) == {"fecha", "hora", "notas", "servicio_id", "proveedor_id"}

    assert filas["consultar_disponibilidad"]["config"]["idempotent"] is False
    assert filas["cancelar_reserva"]["config"]["url_template"] == (
        f"{BASE}/api/eventos/calendario/reservas/cancelar"
    )


@pytest.mark.asyncio
async def test_cada_herramienta_tiene_el_token_interno_cifrado(http_client, tenant_y_usuario, auth):
    """n8n lo lee con get_tool_credentials y lo manda en X-Internal-Token."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _calendario(http_client, tenant_id, auth, True)

    for clave in CLAVES:
        raw = await fetch_value("SELECT get_tool_credentials($1, $2)", tenant_id, clave)
        assert json.loads(raw) == {"token": TOKEN}, clave


@pytest.mark.asyncio
async def test_el_token_guardado_abre_los_endpoints_del_agente(
    http_client, tenant_y_usuario, auth
):
    """De punta a punta: con la URL y el token guardados, el endpoint responde."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _calendario(http_client, tenant_id, auth, True)
    config = (await _filas(tenant_id))["consultar_servicios"]["config"]
    token = json.loads(
        await fetch_value("SELECT get_tool_credentials($1, $2)", tenant_id, "consultar_servicios")
    )["token"]

    url = config["url_template"].replace(BASE, "").replace("{tenant_id}", str(tenant_id))
    r = await http_client.get(url, headers={config["auth_header"]: token})

    assert r.status_code == 200, r.text


# ============================================================
# Apagar y volver a encender
# ============================================================
@pytest.mark.asyncio
async def test_apagar_el_calendario_las_pausa_y_encenderlo_las_reactiva(
    http_client, tenant_y_usuario, auth
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _calendario(http_client, tenant_id, auth, True)

    await _calendario(http_client, tenant_id, auth, False)
    assert not any(f["is_enabled"] for f in (await _filas(tenant_id)).values())

    await _calendario(http_client, tenant_id, auth, True)
    filas = await _filas(tenant_id)
    assert all(f["is_enabled"] for f in filas.values())
    assert not any(f["config"].get("pausada_por_sistema") for f in filas.values())


@pytest.mark.asyncio
async def test_una_pausada_por_el_dueno_sigue_pausada(http_client, tenant_y_usuario, auth):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _calendario(http_client, tenant_id, auth, True)
    r = await http_client.patch(
        "/api/herramientas/cancelar_reserva", json={"is_enabled": False}, headers=auth
    )
    assert r.status_code == 200

    await _calendario(http_client, tenant_id, auth, False)
    await _calendario(http_client, tenant_id, auth, True)

    filas = await _filas(tenant_id)
    assert filas["cancelar_reserva"]["is_enabled"] is False
    assert filas["crear_reserva"]["is_enabled"] is True


@pytest.mark.asyncio
async def test_no_pisa_los_textos_que_edito_el_dueno(http_client, tenant_y_usuario, auth):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _calendario(http_client, tenant_id, auth, True)
    await http_client.patch(
        "/api/herramientas/crear_reserva",
        json={"display_name": "Reservar turno", "description": "Solo con confirmación explícita."},
        headers=auth,
    )

    await herramientas_calendario.sincronizar(tenant_id, True)

    crear = (await _filas(tenant_id))["crear_reserva"]
    assert crear["display_name"] == "Reservar turno"
    assert crear["description"] == "Solo con confirmación explícita."


@pytest.mark.asyncio
async def test_sincronizar_dos_veces_no_duplica(tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    await herramientas_calendario.sincronizar(tenant_id, True)
    await herramientas_calendario.sincronizar(tenant_id, True)

    n = await fetch_value("SELECT COUNT(*) FROM tenant_tools WHERE tenant_id = $1", tenant_id)
    assert n == 5


@pytest.mark.asyncio
async def test_al_arrancar_se_crean_para_quien_ya_tenia_el_calendario(
    monkeypatch, tenant_y_usuario
):
    """Negocios que lo encendieron antes de este módulo."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await execute(
        """
        INSERT INTO tenant_servicios (tenant_id, calendario_activo) VALUES ($1, true)
        ON CONFLICT (tenant_id) DO UPDATE SET calendario_activo = true
        """,
        tenant_id,
    )

    # La consulta real corre, pero solo se sincroniza el negocio del test:
    # los demás de la base de desarrollo quedarían apuntando a BASE.
    consulta_real = herramientas_calendario.fetch_all

    async def solo_este(sql, *args):
        return [f for f in await consulta_real(sql, *args) if f["tenant_id"] == tenant_id]

    monkeypatch.setattr(herramientas_calendario, "fetch_all", solo_este)
    await herramientas_calendario.sincronizar_todos()

    assert set(await _filas(tenant_id)) == CLAVES


@pytest.mark.asyncio
async def test_la_url_se_actualiza_si_cambia_el_backend(monkeypatch, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    await herramientas_calendario.sincronizar(tenant_id, True)

    monkeypatch.setattr(settings, "BASE_URL_BACKEND", "https://nuevo.ejemplo.test")
    await herramientas_calendario.sincronizar(tenant_id, True)

    url = (await _filas(tenant_id))["crear_reserva"]["config"]["url_template"]
    assert url.startswith("https://nuevo.ejemplo.test/")


# ============================================================
# Pantalla de herramientas
# ============================================================
@pytest.mark.asyncio
async def test_se_listan_como_gestionadas(http_client, tenant_y_usuario, auth):
    await _calendario(http_client, tenant_y_usuario["tenant_id"], auth, True)

    r = await http_client.get("/api/herramientas", headers=auth)

    assert r.status_code == 200
    assert {h["tool_key"] for h in r.json() if h["gestionada"]} == CLAVES


@pytest.mark.asyncio
async def test_no_se_pueden_eliminar(http_client, tenant_y_usuario, auth):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _calendario(http_client, tenant_id, auth, True)

    r = await http_client.delete("/api/herramientas/crear_reserva", headers=auth)

    assert r.status_code == 409
    assert await fetch_one(
        "SELECT 1 FROM tenant_tools WHERE tenant_id = $1 AND tool_key = 'crear_reserva'", tenant_id
    )
