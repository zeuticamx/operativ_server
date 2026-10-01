"""
Tests del proxy de mensajes entrantes de NeuroAPI hacia n8n.

Lo que importa si falla:

  1. un mensaje de un número de un tenant llega a n8n con el cuerpo intacto
     y el tenant correcto en las cabeceras
  2. un mensaje de un número ajeno no se reenvía (ni se atribuye a nadie)
  3. si n8n no lo recibe, se responde error para que NeuroAPI reintente y
     el mensaje no se pierda
  4. los eventos de vinculación siguen yendo por su camino, no a n8n

n8n no se llama nunca: se simula httpx.AsyncClient.
"""

import hashlib
import hmac
import json

import httpx
import pytest

from config import settings
from services import entrada_mensajes
from services.entrada_mensajes import extraer_phone_number_id
from session import execute

SECRETO = "connect-secreto-de-prueba"
DESTINO = "https://n8n.ejemplo.com/webhook/entrada-canal-universal"
TOKEN = "token-interno-de-prueba"
URL = "/api/canales/whatsapp/neuroapi/webhook"


def firmar(cuerpo: bytes) -> str:
    return "sha256=" + hmac.new(SECRETO.encode(), cuerpo, hashlib.sha256).hexdigest()


def mensaje_meta(phone_number_id: str) -> dict:
    """Sobre estándar de Meta para un mensaje de texto entrante."""
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "999", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "5215500000000",
                         "phone_number_id": phone_number_id},
            "contacts": [{"profile": {"name": "Cliente"}, "wa_id": "5215599999999"}],
            "messages": [{"from": "5215599999999", "id": "wamid.X", "type": "text",
                          "text": {"body": "hola"}}],
        }}]}],
    }


class _ClienteFalso:
    """Reemplaza httpx.AsyncClient: guarda cada POST y responde lo pactado."""

    llamadas: list[dict] = []

    def __init__(self, status_code: int = 200, error: Exception | None = None):
        self._status_code = status_code
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, url, content=None, headers=None):
        _ClienteFalso.llamadas.append({"url": url, "content": content, "headers": headers})
        if self._error:
            raise self._error
        return httpx.Response(self._status_code, text="ok")


@pytest.fixture
def n8n(monkeypatch):
    """Configura el proxy y devuelve una función para fijar la respuesta de n8n."""
    monkeypatch.setattr(settings, "NEUROAPI_CONNECT_WEBHOOK_SECRET", SECRETO)
    monkeypatch.setattr(settings, "N8N_WEBHOOK_ENTRADA_URL", DESTINO)
    monkeypatch.setattr(settings, "N8N_INTERNAL_TOKEN", TOKEN)
    _ClienteFalso.llamadas = []

    def responder(status_code: int = 200, error: Exception | None = None):
        monkeypatch.setattr(
            entrada_mensajes.httpx,
            "AsyncClient",
            lambda *a, **k: _ClienteFalso(status_code, error),
        )

    responder()
    return responder


async def _linea_whatsapp(tenant_id, phone_number_id: str) -> None:
    await execute(
        "SELECT set_channel_credentials($1, 'whatsapp', NULL, $2, NULL, NULL)",
        tenant_id,
        phone_number_id,
    )


async def _post_firmado(http_client, evento: dict):
    cuerpo = json.dumps(evento).encode()
    respuesta = await http_client.post(
        URL,
        content=cuerpo,
        headers={"x-hub-signature-256": firmar(cuerpo), "content-type": "application/json"},
    )
    return respuesta, cuerpo


# ============================================================
# Extracción del número receptor
# ============================================================
@pytest.mark.parametrize(
    "evento",
    [
        mensaje_meta("111"),
        {"event": "message.received", "data": {"phone_number_id": "111", "from": "52155"}},
        {"event": "message.received", "data": {"metadata": {"phone_number_id": "111"}}},
        {"event": "message.received", "phoneNumberId": "111"},
    ],
    ids=["sobre-meta", "en-data", "metadata-en-data", "camel-en-raiz"],
)
def test_extrae_el_phone_number_id_receptor(evento):
    assert extraer_phone_number_id(evento) == "111"


def test_sin_numero_receptor_devuelve_none():
    assert extraer_phone_number_id({"event": "message.received", "data": {}}) is None


# ============================================================
# Reenvío
# ============================================================
@pytest.mark.asyncio
async def test_un_mensaje_se_reenvia_intacto_con_el_tenant(http_client, tenant_y_usuario, n8n):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _linea_whatsapp(tenant_id, "5215533333333")

    respuesta, cuerpo = await _post_firmado(http_client, mensaje_meta("5215533333333"))

    assert respuesta.status_code == 200
    assert len(_ClienteFalso.llamadas) == 1
    llamada = _ClienteFalso.llamadas[0]
    assert llamada["url"] == DESTINO
    # Bytes idénticos: n8n recibe lo mismo que mandaría NeuroAPI.
    assert llamada["content"] == cuerpo
    assert llamada["headers"]["X-Tenant-Id"] == str(tenant_id)
    assert llamada["headers"]["X-Internal-Token"] == TOKEN
    assert llamada["headers"]["X-Canal"] == "whatsapp"


@pytest.mark.asyncio
async def test_un_numero_ajeno_no_se_reenvia(http_client, db, n8n):
    respuesta, _ = await _post_firmado(http_client, mensaje_meta("000000000000"))

    assert respuesta.status_code == 200
    assert _ClienteFalso.llamadas == []


@pytest.mark.asyncio
async def test_una_linea_desactivada_no_se_reenvia(http_client, tenant_y_usuario, n8n):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _linea_whatsapp(tenant_id, "5215544444444")
    await execute(
        "UPDATE channel_credentials SET is_active = false "
        "WHERE tenant_id = $1 AND channel_type = 'whatsapp'",
        tenant_id,
    )

    respuesta, _ = await _post_firmado(http_client, mensaje_meta("5215544444444"))

    assert respuesta.status_code == 200
    assert _ClienteFalso.llamadas == []


@pytest.mark.asyncio
async def test_un_mensaje_con_firma_invalida_no_se_reenvia(http_client, n8n):
    respuesta = await http_client.post(
        URL,
        json=mensaje_meta("5215533333333"),
        headers={"x-hub-signature-256": "sha256=firmafalsa"},
    )

    assert respuesta.status_code == 401
    assert _ClienteFalso.llamadas == []


@pytest.mark.parametrize(
    "status_code, error",
    [
        (500, None),
        (404, None),  # workflow de n8n desactivado
        (200, httpx.ConnectError("n8n caído")),
    ],
    ids=["n8n-500", "n8n-404", "n8n-inalcanzable"],
)
@pytest.mark.asyncio
async def test_si_n8n_no_lo_recibe_se_pide_reintento(
    http_client, tenant_y_usuario, n8n, status_code, error
):
    await _linea_whatsapp(tenant_y_usuario["tenant_id"], "5215555555555")
    n8n(status_code, error)

    respuesta, _ = await _post_firmado(http_client, mensaje_meta("5215555555555"))

    assert respuesta.status_code == 502


@pytest.mark.asyncio
async def test_sin_destino_configurado_se_pide_reintento(
    http_client, tenant_y_usuario, n8n, monkeypatch
):
    await _linea_whatsapp(tenant_y_usuario["tenant_id"], "5215566666666")
    monkeypatch.setattr(settings, "N8N_WEBHOOK_ENTRADA_URL", "")

    respuesta, _ = await _post_firmado(http_client, mensaje_meta("5215566666666"))

    assert respuesta.status_code == 503
    assert _ClienteFalso.llamadas == []


@pytest.mark.asyncio
async def test_un_evento_de_vinculacion_no_va_a_n8n(http_client, tenant_y_usuario, n8n):
    tenant_id = tenant_y_usuario["tenant_id"]
    await execute(
        "INSERT INTO neuroapi_connect_sessions (tenant_id, session_id) VALUES ($1, $2)",
        tenant_id,
        "sess_no_n8n",
    )

    respuesta, _ = await _post_firmado(
        http_client,
        {"event": "whatsapp.connected", "session_id": "sess_no_n8n",
         "data": {"phones": [{"phoneNumberId": "5215577777777"}]}},
    )

    assert respuesta.status_code == 200
    assert _ClienteFalso.llamadas == []
