"""
Tests de la vinculación de WhatsApp vía NeuroAPI Connect Sessions.

Se concentran en lo que compromete una cuenta si falla:

  1. la firma del webhook — sin ella, cualquiera que descubra la URL manda
     un evento "completado" inventado y se apropia del canal de otro tenant
  2. que el endpoint de inicio no llame a NeuroAPI de verdad (se simula)
  3. que el webhook solo activa el canal del tenant dueño de la sesión

La API de NeuroAPI no se llama nunca: lo que se prueba es nuestro lado.
"""

import hashlib
import hmac
import json

import pytest

from config import settings
from services import neuroapi_connect
from services.neuroapi_connect import verificar_webhook
from session import execute, fetch_one, fetch_value

# Prueban la lógica del módulo, no el cobro: el tenant de prueba tiene plan
# vigente con todo incluido (ver el fixture en conftest.py).
pytestmark = pytest.mark.usefixtures("plan_enterprise")

SECRETO = "connect-secreto-de-prueba"


def firmar(cuerpo: bytes, secreto: str = SECRETO) -> str:
    """Arma una cabecera X-Hub-Signature-256 válida, como la que manda NeuroAPI."""
    firma = hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()
    return f"sha256={firma}"


@pytest.fixture
def con_secreto(monkeypatch):
    monkeypatch.setattr(settings, "NEUROAPI_CONNECT_WEBHOOK_SECRET", SECRETO)
    return SECRETO


# ============================================================
# Firma del webhook
# ============================================================
def test_firma_correcta_pasa(con_secreto):
    cuerpo = b'{"session_id":"sess_1","status":"completado"}'
    assert verificar_webhook(cuerpo, {"x-hub-signature-256": firmar(cuerpo)}) is True


def test_firma_de_otro_secreto_no_pasa(con_secreto):
    cuerpo = b'{"session_id":"sess_1","status":"completado"}'
    firma = firmar(cuerpo, secreto="otro-secreto")
    assert verificar_webhook(cuerpo, {"x-hub-signature-256": firma}) is False


def test_sin_cabecera_no_pasa(con_secreto):
    assert verificar_webhook(b"{}", {}) is False


def test_cabecera_sin_prefijo_sha256_no_pasa(con_secreto):
    cuerpo = b"{}"
    firma = hmac.new(SECRETO.encode(), cuerpo, hashlib.sha256).hexdigest()
    assert verificar_webhook(cuerpo, {"x-hub-signature-256": firma}) is False


def test_tocar_el_cuerpo_invalida_la_firma(con_secreto):
    original = b'{"status":"completado","phone_number_id":"1"}'
    cabecera = firmar(original)
    alterado = b'{"status":"completado","phone_number_id":"999999999"}'
    assert verificar_webhook(alterado, {"x-hub-signature-256": cabecera}) is False


def test_sin_secreto_configurado_siempre_rechaza(monkeypatch):
    monkeypatch.setattr(settings, "NEUROAPI_CONNECT_WEBHOOK_SECRET", "")
    cuerpo = b'{"status":"completado"}'
    # Aunque alguien arme una firma con un secreto adivinado, sin
    # NEUROAPI_CONNECT_WEBHOOK_SECRET configurado no hay nada contra qué
    # compararla.
    assert verificar_webhook(cuerpo, {"x-hub-signature-256": firmar(cuerpo)}) is False


# ============================================================
# Endpoint de inicio
# ============================================================
class _RespuestaFalsa:
    def __init__(self, datos: dict, error: bool = False):
        self._datos = datos
        self._error = error

    def raise_for_status(self) -> None:
        if self._error:
            raise neuroapi_connect.httpx.HTTPError("neuroapi dijo que no")

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
def neuroapi_simulado(monkeypatch):
    """Deja el endpoint de inicio operativo sin llamar a NeuroAPI de verdad."""
    monkeypatch.setattr(settings, "NEUROAPI_API_KEY", "clave-de-prueba")
    monkeypatch.setattr(settings, "NEUROAPI_CONNECT_WEBHOOK_SECRET", SECRETO)
    _ClienteFalso.ultimo_payload = None

    def instalar(respuesta: _RespuestaFalsa):
        monkeypatch.setattr(
            neuroapi_connect.httpx, "AsyncClient", lambda *a, **k: _ClienteFalso(respuesta)
        )

    return instalar


@pytest.mark.asyncio
async def test_iniciar_guarda_la_sesion_y_devuelve_la_url(
    http_client, tenant_y_usuario, neuroapi_simulado
):
    neuroapi_simulado(
        _RespuestaFalsa(
            {
                "success": True,
                "data": {"session_id": "sess_abc", "url": "https://connect.neurochat.com.ec/x"},
            }
        )
    )

    respuesta = await http_client.post(
        "/api/canales/whatsapp/neuroapi/iniciar",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["connect_url"] == "https://connect.neurochat.com.ec/x"

    fila = await fetch_one(
        "SELECT session_id, status FROM neuroapi_connect_sessions WHERE tenant_id = $1",
        tenant_y_usuario["tenant_id"],
    )
    assert fila["session_id"] == "sess_abc"
    assert fila["status"] == "pendiente"

    assert _ClienteFalso.ultimo_payload["service_type"] == "whatsapp_cloud_api"
    assert _ClienteFalso.ultimo_payload["webhook_secret"] == SECRETO


@pytest.mark.asyncio
async def test_si_neuroapi_falla_no_queda_sesion_huerfana(
    http_client, tenant_y_usuario, neuroapi_simulado
):
    neuroapi_simulado(_RespuestaFalsa({}, error=True))

    respuesta = await http_client.post(
        "/api/canales/whatsapp/neuroapi/iniciar",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 502

    pendientes = await fetch_value(
        "SELECT COUNT(*) FROM neuroapi_connect_sessions WHERE tenant_id = $1",
        tenant_y_usuario["tenant_id"],
    )
    assert pendientes == 0


@pytest.mark.asyncio
async def test_sin_api_key_configurada_iniciar_no_atiende(
    http_client, tenant_y_usuario, monkeypatch
):
    monkeypatch.setattr(settings, "NEUROAPI_API_KEY", "")

    respuesta = await http_client.post(
        "/api/canales/whatsapp/neuroapi/iniciar",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 503


# ============================================================
# Webhook
# ============================================================
@pytest.mark.asyncio
async def test_sin_secreto_configurado_el_webhook_no_atiende(http_client, monkeypatch):
    monkeypatch.setattr(settings, "NEUROAPI_CONNECT_WEBHOOK_SECRET", "")
    respuesta = await http_client.post(
        "/api/canales/whatsapp/neuroapi/webhook",
        json={"session_id": "sess_x", "status": "completado"},
    )
    assert respuesta.status_code == 503


@pytest.mark.asyncio
async def test_el_webhook_rechaza_una_firma_invalida(http_client, con_secreto):
    respuesta = await http_client.post(
        "/api/canales/whatsapp/neuroapi/webhook",
        json={"session_id": "sess_x", "status": "completado"},
        headers={"x-hub-signature-256": "sha256=firmafalsa"},
    )
    assert respuesta.status_code == 401


async def _sesion_pendiente(tenant_id, session_id: str) -> None:
    await execute(
        "INSERT INTO neuroapi_connect_sessions (tenant_id, session_id) VALUES ($1, $2)",
        tenant_id,
        session_id,
    )


@pytest.mark.asyncio
async def test_una_sesion_completada_activa_el_canal(
    http_client, tenant_y_usuario, con_secreto
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _sesion_pendiente(tenant_id, "sess_completa")

    cuerpo = json.dumps(
        {
            "session_id": "sess_completa",
            "status": "completado",
            "phone_number_id": "5215500000000",
        }
    ).encode()
    respuesta = await http_client.post(
        "/api/canales/whatsapp/neuroapi/webhook",
        content=cuerpo,
        headers={"x-hub-signature-256": firmar(cuerpo), "content-type": "application/json"},
    )
    assert respuesta.status_code == 200

    estado_sesion = await fetch_value(
        "SELECT status FROM neuroapi_connect_sessions WHERE session_id = $1", "sess_completa"
    )
    assert estado_sesion == "completado"

    canal = await fetch_one(
        "SELECT is_active FROM tenant_channels WHERE tenant_id = $1 AND channel_type = 'whatsapp'",
        tenant_id,
    )
    assert canal["is_active"] is True

    numero = await fetch_value(
        "SELECT phone_number_id FROM channel_credentials WHERE tenant_id = $1 AND channel_type = 'whatsapp'",
        tenant_id,
    )
    assert numero == "5215500000000"


@pytest.mark.asyncio
async def test_un_status_en_ingles_tambien_activa_el_canal(
    http_client, tenant_y_usuario, con_secreto
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _sesion_pendiente(tenant_id, "sess_en")

    cuerpo = json.dumps(
        {"session_id": "sess_en", "status": "completed", "phone_number_id": "5215511111111"}
    ).encode()
    respuesta = await http_client.post(
        "/api/canales/whatsapp/neuroapi/webhook",
        content=cuerpo,
        headers={"x-hub-signature-256": firmar(cuerpo), "content-type": "application/json"},
    )
    assert respuesta.status_code == 200

    canal = await fetch_one(
        "SELECT is_active FROM tenant_channels WHERE tenant_id = $1 AND channel_type = 'whatsapp'",
        tenant_id,
    )
    assert canal["is_active"] is True


@pytest.mark.parametrize(
    "evento",
    [
        # Mismo sobre que la respuesta de crear la sesión.
        {"success": True, "data": {"session_id": "sess_sobre", "status": "completed",
                                    "phone_number_id": "5215522222222"}},
        # Estado en el nombre del evento, sin `status`.
        {"event": "connect_session.completed", "session_id": "sess_sobre",
         "data": {"phone_number_id": "5215522222222"}},
        # Evento oficial de NeuroAPI con los datos en `data`.
        {"event": "whatsapp.connected",
         "data": {"session_id": "sess_sobre", "phone_number_id": "5215522222222",
                  "phone_number": "+52 1 55 2222 2222", "waba_id": "999"}},
        # Evento oficial con todo en la raíz.
        {"event": "whatsapp.connected", "session_id": "sess_sobre",
         "phone_number_id": "5215522222222", "phone_number": "+52 1 55 2222 2222"},
        # `type` en lugar de `event`, y el id del número como otp_phone_number_id.
        {"type": "whatsapp.connected", "session_id": "sess_sobre",
         "otp_phone_number_id": "5215522222222"},
        # Sobre estándar de Meta.
        {"object": "whatsapp_business_account",
         "entry": [{"id": "999", "changes": [{"field": "account_update", "value": {
             "event": "whatsapp.connected", "session_id": "sess_sobre",
             "phone_number_id": "5215522222222", "waba_id": "999"}}]}]},
    ],
    ids=[
        "status-en-data", "status-en-event", "connected-en-data", "connected-en-raiz",
        "connected-type-otp", "connected-sobre-meta",
    ],
)
@pytest.mark.asyncio
async def test_un_evento_con_los_datos_anidados_activa_el_canal(
    http_client, tenant_y_usuario, con_secreto, evento
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _sesion_pendiente(tenant_id, "sess_sobre")

    cuerpo = json.dumps(evento).encode()
    respuesta = await http_client.post(
        "/api/canales/whatsapp/neuroapi/webhook",
        content=cuerpo,
        headers={"x-hub-signature-256": firmar(cuerpo), "content-type": "application/json"},
    )
    assert respuesta.status_code == 200

    canal = await fetch_one(
        "SELECT is_active FROM tenant_channels WHERE tenant_id = $1 AND channel_type = 'whatsapp'",
        tenant_id,
    )
    assert canal is not None and canal["is_active"] is True
    numero = await fetch_value(
        "SELECT phone_number_id FROM channel_credentials WHERE tenant_id = $1 AND channel_type = 'whatsapp'",
        tenant_id,
    )
    assert numero == "5215522222222"


@pytest.mark.asyncio
async def test_el_volcado_del_evento_no_muestra_tokens(http_client, con_secreto, caplog):
    cuerpo = json.dumps(
        {"event": "whatsapp.connected", "session_id": "sess_ajena",
         "data": {"access_token": "EAAG-secreto", "phone_number_id": "1"}}
    ).encode()
    with caplog.at_level("WARNING", logger="operativai.canales.neuroapi_connect"):
        await http_client.post(
            "/api/canales/whatsapp/neuroapi/webhook",
            content=cuerpo,
            headers={"x-hub-signature-256": firmar(cuerpo), "content-type": "application/json"},
        )
    assert "EAAG-secreto" not in caplog.text
    assert "phone_number_id" in caplog.text


@pytest.mark.asyncio
async def test_un_status_desconocido_no_rompe_el_webhook(
    http_client, tenant_y_usuario, con_secreto
):
    """Antes chocaba con el CHECK de la tabla y respondía 500."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _sesion_pendiente(tenant_id, "sess_rara")

    cuerpo = json.dumps({"session_id": "sess_rara", "status": "in_review"}).encode()
    respuesta = await http_client.post(
        "/api/canales/whatsapp/neuroapi/webhook",
        content=cuerpo,
        headers={"x-hub-signature-256": firmar(cuerpo), "content-type": "application/json"},
    )
    assert respuesta.status_code == 200

    fila = await fetch_one(
        "SELECT status, detalle FROM neuroapi_connect_sessions WHERE session_id = $1", "sess_rara"
    )
    assert fila["status"] == "pendiente"
    assert fila["detalle"] == "in_review"


@pytest.mark.asyncio
async def test_un_session_id_desconocido_no_rompe(http_client, db, con_secreto):
    """Una sesión que no es nuestra se contesta 200 y no toca nada."""
    cuerpo = json.dumps({"session_id": "sess_ajena", "status": "completado"}).encode()
    respuesta = await http_client.post(
        "/api/canales/whatsapp/neuroapi/webhook",
        content=cuerpo,
        headers={"x-hub-signature-256": firmar(cuerpo), "content-type": "application/json"},
    )
    assert respuesta.status_code == 200


@pytest.mark.asyncio
async def test_una_sesion_fallida_no_activa_ningun_canal(
    http_client, tenant_y_usuario, con_secreto
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _sesion_pendiente(tenant_id, "sess_fallida")

    cuerpo = json.dumps(
        {"session_id": "sess_fallida", "status": "fallido", "error": "usuario canceló"}
    ).encode()
    respuesta = await http_client.post(
        "/api/canales/whatsapp/neuroapi/webhook",
        content=cuerpo,
        headers={"x-hub-signature-256": firmar(cuerpo), "content-type": "application/json"},
    )
    assert respuesta.status_code == 200

    estado_sesion = await fetch_value(
        "SELECT status FROM neuroapi_connect_sessions WHERE session_id = $1", "sess_fallida"
    )
    assert estado_sesion == "fallido"

    canal = await fetch_one(
        "SELECT 1 FROM tenant_channels WHERE tenant_id = $1 AND channel_type = 'whatsapp'",
        tenant_id,
    )
    assert canal is None
