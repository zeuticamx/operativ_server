"""
Tests del proxy de mensajes entrantes de NeuroAPI hacia el agente en
código (services/entrada_mensajes.py).

Lo que importa si falla:

  1. un mensaje de un número de un tenant llega al agente con el cuerpo intacto
     y el tenant correcto en las cabeceras
  2. un mensaje de un número ajeno no se reenvía (ni se atribuye a nadie)
  3. si el destino no lo recibe, se responde error para que NeuroAPI reintente y
     el mensaje no se pierda
  4. los eventos de vinculación siguen yendo por su camino, no al agente
  5. con AGENTE_EN_CODIGO encendida, el mismo mensaje va al propio backend
     (routers/agente_webhook.py) y NO a n8n (que ya no existe). Esto estuvo roto: la bandera
     solo apagaba /canales/entrada, pero WhatsApp por NeuroAPI —el canal con
     más tráfico real— seguía yendo a n8n sin que nada la mirara.

El agente no se llaman de verdad: se simula httpx.AsyncClient.
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
DESTINO = "https://api.ejemplo.com/api/canales/entrada"
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
def destino(monkeypatch):
    """Configura el proxy y devuelve una función para fijar la respuesta del destino."""
    monkeypatch.setattr(settings, "NEUROAPI_CONNECT_WEBHOOK_SECRET", SECRETO)
    monkeypatch.setattr(settings, "BASE_URL_BACKEND", "https://api.ejemplo.com")
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


async def _linea_whatsapp(tenant_id, phone_number_id: str, estado: str = "activo") -> None:
    """Número vinculado en agentes_canal (spec 005a T6: la atribución ya no lee channel_credentials)."""
    await execute(
        "INSERT INTO agentes_canal (tenant_id, phone_number_id, estado) VALUES ($1, $2, $3)",
        tenant_id,
        phone_number_id,
        estado,
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
async def test_un_mensaje_se_reenvia_intacto_con_el_tenant(http_client, tenant_y_usuario, destino):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _linea_whatsapp(tenant_id, "5215533333333")

    respuesta, cuerpo = await _post_firmado(http_client, mensaje_meta("5215533333333"))

    assert respuesta.status_code == 200
    assert len(_ClienteFalso.llamadas) == 1
    llamada = _ClienteFalso.llamadas[0]
    assert llamada["url"] == DESTINO
    # Bytes idénticos: el destino recibe lo mismo que mandaría NeuroAPI.
    assert llamada["content"] == cuerpo
    assert llamada["headers"]["X-Tenant-Id"] == str(tenant_id)
    assert llamada["headers"]["X-Internal-Token"] == TOKEN
    assert llamada["headers"]["X-Canal"] == "whatsapp"


@pytest.mark.asyncio
async def test_un_numero_ajeno_no_se_reenvia(http_client, db, destino):
    respuesta, _ = await _post_firmado(http_client, mensaje_meta("000000000000"))

    assert respuesta.status_code == 200
    assert _ClienteFalso.llamadas == []


@pytest.mark.asyncio
async def test_una_linea_pausada_se_sigue_atribuyendo(
    http_client, tenant_y_usuario, destino
):
    """Pausado guarda sus mensajes (no responde por otro camino): se reenvía igual."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _linea_whatsapp(tenant_id, "5215544444444", estado="pausado")

    respuesta, _ = await _post_firmado(http_client, mensaje_meta("5215544444444"))

    assert respuesta.status_code == 200
    assert len(_ClienteFalso.llamadas) == 1
    assert _ClienteFalso.llamadas[0]["headers"]["X-Tenant-Id"] == str(tenant_id)


@pytest.mark.asyncio
async def test_un_numero_desvinculado_resuelve_a_su_ultimo_dueno(
    http_client, tenant_y_usuario, destino
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _linea_whatsapp(tenant_id, "5215599999990")
    await execute(
        "UPDATE agentes_canal SET estado = 'desvinculado', desvinculado_en = NOW() "
        "WHERE tenant_id = $1 AND phone_number_id = $2",
        tenant_id,
        "5215599999990",
    )

    assert await entrada_mensajes.tenant_por_numero("5215599999990") == tenant_id
    assert await entrada_mensajes.tenant_por_numero("000000000000") is None


@pytest.mark.asyncio
async def test_un_mensaje_con_firma_invalida_no_se_reenvia(http_client, destino):
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
        (404, None),  # destino que responde 404
        (200, httpx.ConnectError("destino caído")),
    ],
    ids=["destino-500", "destino-404", "destino-inalcanzable"],
)
@pytest.mark.asyncio
async def test_si_el_destino_no_lo_recibe_se_pide_reintento(
    http_client, tenant_y_usuario, destino, status_code, error
):
    await _linea_whatsapp(tenant_y_usuario["tenant_id"], "5215555555555")
    destino(status_code, error)

    respuesta, _ = await _post_firmado(http_client, mensaje_meta("5215555555555"))

    assert respuesta.status_code == 502


# ============================================================
# A dónde se reenvía
# ============================================================
def test_destino_es_siempre_el_propio_backend(monkeypatch):
    """
    El destino es siempre el propio backend; n8n ya no existe.
    """
    monkeypatch.setattr(settings, "BASE_URL_BACKEND", "https://api.ejemplo.com")

    assert entrada_mensajes.destino_para(None) == "https://api.ejemplo.com/api/canales/entrada"


@pytest.mark.asyncio
async def test_el_mensaje_va_a_la_entrada_del_propio_backend(
    http_client, tenant_y_usuario, destino, monkeypatch
):
    """
    Integración completa: un mensaje real de NeuroAPI tiene que llegar a
    `/api/canales/entrada` del propio backend.
    """
    tenant_id = tenant_y_usuario["tenant_id"]
    await _linea_whatsapp(tenant_id, "5215588888888")

    respuesta, cuerpo = await _post_firmado(http_client, mensaje_meta("5215588888888"))

    assert respuesta.status_code == 200
    assert len(_ClienteFalso.llamadas) == 1
    llamada = _ClienteFalso.llamadas[0]
    assert llamada["url"] == f"{settings.BASE_URL_BACKEND}/api/canales/entrada"
    # Las cabeceras no cambian: el agente valida el mismo X-Internal-Token
    # + X-Tenant-Id que se validaba antes.
    assert llamada["content"] == cuerpo
    assert llamada["headers"]["X-Tenant-Id"] == str(tenant_id)
    assert llamada["headers"]["X-Internal-Token"] == TOKEN


@pytest.mark.asyncio
async def test_un_evento_de_vinculacion_no_se_reenvia(http_client, tenant_y_usuario, destino):
    tenant_id = tenant_y_usuario["tenant_id"]
    await execute(
        "INSERT INTO neuroapi_connect_sessions (tenant_id, session_id) VALUES ($1, $2)",
        tenant_id,
        "sess_no_reenvio",
    )

    respuesta, _ = await _post_firmado(
        http_client,
        {"event": "whatsapp.connected", "session_id": "sess_no_reenvio",
         "data": {"phones": [{"phoneNumberId": "5215577777777"}]}},
    )

    assert respuesta.status_code == 200
    assert _ClienteFalso.llamadas == []
