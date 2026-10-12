"""
Herramientas del agente para el calendario (services/herramientas_calendario.py).

Lo que se cubre: que encender el calendario deje las nueve filas de
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
    "consultar_cancelacion_reserva",
    "historial_citas_cliente",
    "consultar_citas_proximas",
    "reagendar_reserva",
}
# Dependen del conversation_id real de n8n: comparten la compuerta del historial.
CON_COMPUERTA = {"historial_citas_cliente", "consultar_citas_proximas", "reagendar_reserva"}
# Las que quedan activas con la compuerta de n8n cerrada (el valor por defecto).
CLAVES_ACTIVAS = CLAVES - CON_COMPUERTA
HISTORIAL = "historial_citas_cliente"


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    monkeypatch.setattr(settings, "N8N_INTERNAL_TOKEN", TOKEN)
    # Cerrada por defecto; los tests de la compuerta abierta la cambian.
    monkeypatch.setattr(settings, "HISTORIAL_CITAS_N8N_PUBLICADO", False)
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
# Encender: las seis, como las espera n8n
# ============================================================
@pytest.mark.asyncio
async def test_encender_el_calendario_crea_las_nueve_herramientas(
    http_client, tenant_y_usuario, auth
):
    tenant_id = tenant_y_usuario["tenant_id"]
    assert await _filas(tenant_id) == {}

    await _calendario(http_client, tenant_id, auth, True)

    filas = await _filas(tenant_id)
    assert set(filas) == CLAVES
    # Con la compuerta cerrada, la séptima existe pero queda desactivada.
    assert {k for k, f in filas.items() if f["is_enabled"]} == CLAVES_ACTIVAS
    assert filas[HISTORIAL]["is_enabled"] is False


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
    assert {k for k, f in filas.items() if f["is_enabled"]} == CLAVES_ACTIVAS
    assert {k for k, f in filas.items() if f["config"].get("pausada_por_sistema")} == CON_COMPUERTA


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
    assert n == 9


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

# ============================================================
# consultar_cancelacion_reserva (spec 001, R31-R32, escenario 13)
# ============================================================
HERRAMIENTA = "consultar_cancelacion_reserva"


@pytest.mark.asyncio
async def test_consultar_cancelacion_se_crea_al_encender_con_url_y_descripcion(
    http_client, tenant_y_usuario, auth
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _calendario(http_client, tenant_id, auth, True)

    filas = await _filas(tenant_id)
    fila = filas[HERRAMIENTA]

    assert fila["is_enabled"] is True
    assert fila["tool_type"] == "http_post_json"
    assert fila["config"] == {
        "method": "POST",
        "idempotent": True,
        "auth_header": "X-Internal-Token",
        "url_template": f"{BASE}/api/eventos/calendario/reservas/consultar-cancelacion",
        "incluir_contacto": False,
    }
    assert fila["parametros_schema"]["reserva_id"]["required"] is True
    assert "antes de cancelar" in fila["description"].lower()
    assert "consultar_cancelacion_reserva" in filas["cancelar_reserva"]["description"]


@pytest.mark.asyncio
async def test_consultar_cancelacion_se_pausa_al_apagar_y_se_reactiva_al_encender(
    http_client, tenant_y_usuario, auth
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _calendario(http_client, tenant_id, auth, True)

    await _calendario(http_client, tenant_id, auth, False)
    pausada = (await _filas(tenant_id))[HERRAMIENTA]
    assert pausada["is_enabled"] is False
    assert pausada["config"]["pausada_por_sistema"] is True

    await _calendario(http_client, tenant_id, auth, True)
    reactivada = (await _filas(tenant_id))[HERRAMIENTA]
    assert reactivada["is_enabled"] is True
    assert "pausada_por_sistema" not in reactivada["config"]


@pytest.mark.asyncio
async def test_consultar_cancelacion_pausada_por_el_dueno_no_se_reactiva(
    http_client, tenant_y_usuario, auth
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _calendario(http_client, tenant_id, auth, True)
    r = await http_client.patch(f"/api/herramientas/{HERRAMIENTA}", json={"is_enabled": False}, headers=auth)
    assert r.status_code == 200

    await _calendario(http_client, tenant_id, auth, False)
    await _calendario(http_client, tenant_id, auth, True)
    await herramientas_calendario.sincronizar(tenant_id, True)

    assert (await _filas(tenant_id))[HERRAMIENTA]["is_enabled"] is False


@pytest.mark.asyncio
async def test_consultar_cancelacion_no_pisa_la_descripcion_editada(http_client, tenant_y_usuario, auth):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _calendario(http_client, tenant_id, auth, True)
    await http_client.patch(
        f"/api/herramientas/{HERRAMIENTA}", json={"description": "Texto del dueno."}, headers=auth
    )

    await herramientas_calendario.sincronizar(tenant_id, True)

    assert (await _filas(tenant_id))[HERRAMIENTA]["description"] == "Texto del dueno."


@pytest.mark.asyncio
async def test_al_arrancar_un_negocio_sin_la_nueva_la_recibe_sin_tocar_las_demas(
    monkeypatch, tenant_y_usuario
):
    """Negocio con las cinco anteriores: recibe la sexta; cancelar_reserva conserva su texto y su pausa."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await herramientas_calendario.sincronizar(tenant_id, True)
    await execute(
        "DELETE FROM tenant_tools WHERE tenant_id = $1 AND tool_key = $2", tenant_id, HERRAMIENTA
    )
    await execute(
        "UPDATE tenant_tools SET description = 'Texto viejo', is_enabled = false "
        "WHERE tenant_id = $1 AND tool_key = 'cancelar_reserva'",
        tenant_id,
    )
    await execute(
        """
        INSERT INTO tenant_servicios (tenant_id, calendario_activo) VALUES ($1, true)
        ON CONFLICT (tenant_id) DO UPDATE SET calendario_activo = true
        """,
        tenant_id,
    )
    consulta_real = herramientas_calendario.fetch_all

    async def solo_este(sql, *args):
        return [f for f in await consulta_real(sql, *args) if f["tenant_id"] == tenant_id]

    monkeypatch.setattr(herramientas_calendario, "fetch_all", solo_este)
    await herramientas_calendario.sincronizar_todos()

    filas = await _filas(tenant_id)
    assert filas[HERRAMIENTA]["is_enabled"] is True
    assert filas["cancelar_reserva"]["description"] == "Texto viejo"
    assert filas["cancelar_reserva"]["is_enabled"] is False


def test_son_nueve_definiciones():
    assert len(herramientas_calendario.definiciones()) == 9
    assert herramientas_calendario.es_gestionada(HERRAMIENTA)


# ============================================================
# historial_citas_cliente (spec 002, R22, R25, R26) y su compuerta
# ============================================================
def _abrir(monkeypatch, abierta: bool) -> None:
    monkeypatch.setattr(settings, "HISTORIAL_CITAS_N8N_PUBLICADO", abierta)


def _definicion_historial() -> dict:
    return next(d for d in herramientas_calendario.definiciones() if d["tool_key"] == HISTORIAL)


def test_historial_no_recibe_datos_del_modelo_ni_contacto():
    d = _definicion_historial()

    assert d["tool_type"] == "http_post_json"
    assert d["parametros_schema"] == {}
    assert d["config"] == {
        "method": "POST",
        "idempotent": False,
        "auth_header": "X-Internal-Token",
        "url_template": f"{settings.BASE_URL_BACKEND}/api/eventos/calendario/historial-cliente",
        "incluir_contacto": False,
    }


def test_la_descripcion_del_historial_tiene_las_ideas_de_r26():
    texto = _definicion_historial()["description"].lower()

    for idea in (
        "citas pasadas y canceladas",
        "como máximo las 10 más recientes",
        "no incluye citas futuras confirmadas",
        "no sirve para cancelar ni reprogramar",
        "historial",
        "no se encontró la conversación",
        "no se puede consultar ahora",
    ):
        assert idea in texto, idea


def test_compuerta_abierta_solo_afecta_a_las_herramientas_con_conversation_id():
    for clave in CON_COMPUERTA:
        assert herramientas_calendario.compuerta_abierta(clave, False) is False
        assert herramientas_calendario.compuerta_abierta(clave, True) is True
    for clave in CLAVES_ACTIVAS:
        assert herramientas_calendario.compuerta_abierta(clave, False) is True
        assert herramientas_calendario.compuerta_abierta(clave, True) is True


@pytest.mark.asyncio
async def test_compuerta_cerrada_la_fila_se_crea_desactivada_y_marcada(
    http_client, tenant_y_usuario, auth
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _calendario(http_client, tenant_id, auth, True)

    fila = (await _filas(tenant_id))[HISTORIAL]

    assert fila["is_enabled"] is False
    assert fila["config"]["pausada_por_sistema"] is True
    raw = await fetch_value("SELECT get_tool_credentials($1, $2)", tenant_id, HISTORIAL)
    assert json.loads(raw) == {"token": TOKEN}


@pytest.mark.asyncio
async def test_compuerta_cerrada_activarla_da_409_y_no_cambia_nada(
    http_client, tenant_y_usuario, auth
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _calendario(http_client, tenant_id, auth, True)
    antes = (await _filas(tenant_id))[HISTORIAL]

    r = await http_client.patch(
        f"/api/herramientas/{HISTORIAL}",
        json={"is_enabled": True, "description": "Texto nuevo"},
        headers=auth,
    )

    assert r.status_code == 409
    assert r.json()["detail"] == "Esta herramienta aún no está disponible para tu negocio."
    despues = (await _filas(tenant_id))[HISTORIAL]
    assert despues["is_enabled"] is False
    assert despues["description"] == antes["description"]


@pytest.mark.asyncio
async def test_compuerta_cerrada_un_patch_de_texto_o_de_apagar_sigue_funcionando(
    http_client, tenant_y_usuario, auth
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _calendario(http_client, tenant_id, auth, True)

    texto = await http_client.patch(
        f"/api/herramientas/{HISTORIAL}", json={"description": "Texto del dueno."}, headers=auth
    )
    apagar = await http_client.patch(
        f"/api/herramientas/{HISTORIAL}", json={"is_enabled": False}, headers=auth
    )

    assert texto.status_code == 200, texto.text
    assert texto.json()["description"] == "Texto del dueno."
    assert apagar.status_code == 200, apagar.text
    assert (await _filas(tenant_id))[HISTORIAL]["is_enabled"] is False


@pytest.mark.asyncio
async def test_compuerta_cerrada_si_la_herramienta_no_existe_es_404(
    http_client, tenant_y_usuario, auth
):
    """Calendario nunca encendido: no hay fila, y el 404 va antes que el 409."""
    r = await http_client.patch(
        f"/api/herramientas/{HISTORIAL}", json={"is_enabled": True}, headers=auth
    )

    assert r.status_code == 404


@pytest.mark.asyncio
async def test_compuerta_cerrada_las_otras_herramientas_se_siguen_activando(
    http_client, tenant_y_usuario, auth
):
    await _calendario(http_client, tenant_y_usuario["tenant_id"], auth, True)
    await http_client.patch("/api/herramientas/crear_reserva", json={"is_enabled": False}, headers=auth)

    r = await http_client.patch("/api/herramientas/crear_reserva", json={"is_enabled": True}, headers=auth)

    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_compuerta_abierta_se_activa_al_encender_y_se_pausa_al_apagar(
    monkeypatch, http_client, tenant_y_usuario, auth
):
    _abrir(monkeypatch, True)
    tenant_id = tenant_y_usuario["tenant_id"]
    await _calendario(http_client, tenant_id, auth, True)
    activa = (await _filas(tenant_id))[HISTORIAL]
    assert activa["is_enabled"] is True
    assert "pausada_por_sistema" not in activa["config"]

    await _calendario(http_client, tenant_id, auth, False)
    pausada = (await _filas(tenant_id))[HISTORIAL]
    assert pausada["is_enabled"] is False
    assert pausada["config"]["pausada_por_sistema"] is True

    await _calendario(http_client, tenant_id, auth, True)
    filas = await _filas(tenant_id)
    assert {k for k, f in filas.items() if f["is_enabled"]} == CLAVES
    assert not any(f["config"].get("pausada_por_sistema") for f in filas.values())


@pytest.mark.asyncio
async def test_compuerta_abierta_el_dueno_puede_pausarla_y_activarla(
    monkeypatch, http_client, tenant_y_usuario, auth
):
    _abrir(monkeypatch, True)
    await _calendario(http_client, tenant_y_usuario["tenant_id"], auth, True)

    pausar = await http_client.patch(f"/api/herramientas/{HISTORIAL}", json={"is_enabled": False}, headers=auth)
    activar = await http_client.patch(f"/api/herramientas/{HISTORIAL}", json={"is_enabled": True}, headers=auth)

    assert pausar.status_code == 200 and activar.status_code == 200
    assert activar.json()["is_enabled"] is True


@pytest.mark.asyncio
async def test_compuerta_abierta_conserva_pausa_y_descripcion_del_dueno(
    monkeypatch, http_client, tenant_y_usuario, auth
):
    _abrir(monkeypatch, True)
    tenant_id = tenant_y_usuario["tenant_id"]
    await _calendario(http_client, tenant_id, auth, True)
    await http_client.patch(
        f"/api/herramientas/{HISTORIAL}",
        json={"is_enabled": False, "description": "Texto del dueno."},
        headers=auth,
    )

    await _calendario(http_client, tenant_id, auth, False)
    await _calendario(http_client, tenant_id, auth, True)
    await herramientas_calendario.sincronizar(tenant_id, True)

    fila = (await _filas(tenant_id))[HISTORIAL]
    assert fila["is_enabled"] is False
    assert fila["description"] == "Texto del dueno."


@pytest.mark.asyncio
async def test_compuerta_abierta_no_se_puede_eliminar(
    monkeypatch, http_client, tenant_y_usuario, auth
):
    _abrir(monkeypatch, True)
    tenant_id = tenant_y_usuario["tenant_id"]
    await _calendario(http_client, tenant_id, auth, True)

    r = await http_client.delete(f"/api/herramientas/{HISTORIAL}", headers=auth)

    assert r.status_code == 409
    assert await fetch_one(
        "SELECT 1 FROM tenant_tools WHERE tenant_id = $1 AND tool_key = $2", tenant_id, HISTORIAL
    )


@pytest.mark.asyncio
async def test_compuerta_abierta_una_fila_borrada_se_recrea(monkeypatch, tenant_y_usuario):
    _abrir(monkeypatch, True)
    tenant_id = tenant_y_usuario["tenant_id"]
    await herramientas_calendario.sincronizar(tenant_id, True)
    await execute(
        "DELETE FROM tenant_tools WHERE tenant_id = $1 AND tool_key = $2", tenant_id, HISTORIAL
    )

    await herramientas_calendario.sincronizar(tenant_id, True)

    assert (await _filas(tenant_id))[HISTORIAL]["is_enabled"] is True


@pytest.mark.asyncio
async def test_la_pausa_del_dueno_sobrevive_a_abrir_cerrar_y_abrir(
    monkeypatch, http_client, tenant_y_usuario, auth
):
    tenant_id = tenant_y_usuario["tenant_id"]
    _abrir(monkeypatch, True)
    await _calendario(http_client, tenant_id, auth, True)
    r = await http_client.patch(f"/api/herramientas/{HISTORIAL}", json={"is_enabled": False}, headers=auth)
    assert r.status_code == 200

    _abrir(monkeypatch, False)
    await herramientas_calendario.sincronizar(tenant_id, True)
    cerrada = (await _filas(tenant_id))[HISTORIAL]
    assert cerrada["is_enabled"] is False
    assert "pausada_por_sistema" not in cerrada["config"]

    _abrir(monkeypatch, True)
    await herramientas_calendario.sincronizar(tenant_id, True)
    assert (await _filas(tenant_id))[HISTORIAL]["is_enabled"] is False


@pytest.mark.asyncio
async def test_activa_cerrar_y_abrir_vuelve_a_activa(monkeypatch, http_client, tenant_y_usuario, auth):
    tenant_id = tenant_y_usuario["tenant_id"]
    _abrir(monkeypatch, True)
    await _calendario(http_client, tenant_id, auth, True)
    assert (await _filas(tenant_id))[HISTORIAL]["is_enabled"] is True

    _abrir(monkeypatch, False)
    await herramientas_calendario.sincronizar(tenant_id, True)
    cerrada = (await _filas(tenant_id))[HISTORIAL]
    assert cerrada["is_enabled"] is False
    assert cerrada["config"]["pausada_por_sistema"] is True

    _abrir(monkeypatch, True)
    await herramientas_calendario.sincronizar(tenant_id, True)
    abierta = (await _filas(tenant_id))[HISTORIAL]
    assert abierta["is_enabled"] is True
    assert "pausada_por_sistema" not in abierta["config"]


@pytest.mark.asyncio
async def test_al_arrancar_con_la_compuerta_cerrada_avisa_en_el_log(
    monkeypatch, caplog, tenant_y_usuario
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await execute(
        """
        INSERT INTO tenant_servicios (tenant_id, calendario_activo) VALUES ($1, true)
        ON CONFLICT (tenant_id) DO UPDATE SET calendario_activo = true
        """,
        tenant_id,
    )
    consulta_real = herramientas_calendario.fetch_all

    async def solo_este(sql, *args):
        return [f for f in await consulta_real(sql, *args) if f["tenant_id"] == tenant_id]

    monkeypatch.setattr(herramientas_calendario, "fetch_all", solo_este)

    with caplog.at_level("WARNING", logger="operativai.herramientas_calendario"):
        await herramientas_calendario.sincronizar_todos()
    avisos = [r.getMessage() for r in caplog.records if "HISTORIAL_CITAS_N8N_PUBLICADO" in r.getMessage()]
    assert len(avisos) == 1
    assert "1 negocio" in avisos[0]

    caplog.clear()
    _abrir(monkeypatch, True)
    with caplog.at_level("WARNING", logger="operativai.herramientas_calendario"):
        await herramientas_calendario.sincronizar_todos()
    assert not [r for r in caplog.records if "HISTORIAL_CITAS_N8N_PUBLICADO" in r.getMessage()]
