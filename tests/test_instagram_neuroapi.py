"""
Tests de Instagram Direct vía NeuroAPI Connect Sessions.

Lo que compromete una cuenta o deja a un cliente sin respuesta si falla:

  1. la sesión se abre con el producto y la key de Instagram, no las de WhatsApp
  2. `instagram.connected` activa el canal SOLO del negocio dueño de la sesión,
     y nunca sobre una sesión de WhatsApp
  3. una cuenta activa de otro negocio no se roba ni se revela, y cuenta para
     el bloqueo de cinco altas ajenas en 24 h (compartido con WhatsApp)
  4. un DM (`object: "instagram"`) llega al negocio de la cuenta receptora,
     también en la forma anidada `entry[].entry[].messaging[]`
  5. la respuesta del agente sale por /neuroapi/instagram/send sin HUMAN_AGENT,
     y la de una persona con HUMAN_AGENT
  6. la media se baja solo por el proxy de NeuroAPI, con un id de ruta seguro

NeuroAPI no se llama nunca: se simula httpx.
"""

import hashlib
import hmac
import json
from uuid import uuid4

import pytest

from config import settings
from services import adjuntos, conversaciones, entrada_mensajes, instagram_neuroapi, mensajeria, meta, neuroapi_connect
from services.agente import envio, normalizar
from services.agente.modelos import Destino
from session import execute, fetch_one, fetch_value

pytestmark = pytest.mark.usefixtures("plan_enterprise")

SECRETO = "connect-secreto-de-prueba"
CLAVE_INSTA = "clave-insta-de-prueba"
WEBHOOK = "/api/canales/whatsapp/neuroapi/webhook"


def firmar(cuerpo: bytes) -> str:
    return "sha256=" + hmac.new(SECRETO.encode(), cuerpo, hashlib.sha256).hexdigest()


@pytest.fixture
def configurado(monkeypatch):
    monkeypatch.setattr(settings, "NEUROAPI_CONNECT_WEBHOOK_SECRET", SECRETO)
    monkeypatch.setattr(settings, "NEUROAPI_INSTA_KEY", CLAVE_INSTA)
    monkeypatch.setattr(settings, "NEUROAPI_API_KEY", "clave-whatsapp")


async def _post_webhook(http_client, evento: dict):
    cuerpo = json.dumps(evento).encode()
    return await http_client.post(
        WEBHOOK,
        content=cuerpo,
        headers={"x-hub-signature-256": firmar(cuerpo), "content-type": "application/json"},
    )


def _conectado(session_id: str, ig_id: str = "17841405822304914", username: str = "mi_tienda_oficial") -> dict:
    """Formato real que mandó NeuroAPI (2026-10-22)."""
    return {
        "event": "instagram.connected",
        "session_id": session_id,
        "service_type": "instagram_messaging",
        "timestamp": "2026-10-22T20:15:30.000Z",
        "data": {
            "accounts": [
                {
                    "id": "c98f1234-abcd-5678-ef90-1234567890ab",
                    "instagramAccountId": ig_id,
                    "username": username,
                    "name": "Mi Tienda Oficial",
                    "serviceType": "instagram_messaging",
                }
            ]
        },
    }


async def _sesion(tenant_id, session_id: str, canal: str = "instagram") -> None:
    await execute(
        "INSERT INTO neuroapi_connect_sessions (tenant_id, session_id, canal) VALUES ($1, $2, $3)",
        tenant_id, session_id, canal,
    )


@pytest.fixture
async def otro_tenant(db):
    tenant_id = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro negocio')", tenant_id)
    yield tenant_id
    await execute("DELETE FROM tenants WHERE id = $1", tenant_id)


def _ig_id() -> str:
    return f"1784{uuid4().int % 10**12:012d}"


# ============================================================
# Abrir la sesión
# ============================================================
class _Respuesta:
    def __init__(self, datos: dict):
        self._datos = datos

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._datos


class _Cliente:
    llamadas: list[dict] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, url, headers=None, json=None):
        _Cliente.llamadas.append({"url": url, "headers": headers, "json": json})
        return _Respuesta({"success": True, "data": {"session_id": "sess_ig", "url": "https://connect.neurochat.com.ec/ig"}})


@pytest.mark.asyncio
async def test_iniciar_abre_una_sesion_de_instagram_con_su_key(
    http_client, tenant_y_usuario, configurado, monkeypatch
):
    _Cliente.llamadas = []
    monkeypatch.setattr(neuroapi_connect.httpx, "AsyncClient", _Cliente)

    r = await http_client.post(
        "/api/canales/instagram/neuroapi/iniciar",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )

    assert r.status_code == 200
    assert r.json()["connect_url"] == "https://connect.neurochat.com.ec/ig"
    llamada = _Cliente.llamadas[0]
    assert llamada["headers"] == {"x-api-key": CLAVE_INSTA}
    assert llamada["json"]["service_type"] == "instagram_messaging"
    assert llamada["json"]["webhook_secret"] == SECRETO
    assert llamada["json"]["return_url"].endswith("/canales?instagram_neuroapi=retorno")
    canal = await fetch_value(
        "SELECT canal FROM neuroapi_connect_sessions WHERE session_id = 'sess_ig' AND tenant_id = $1",
        tenant_y_usuario["tenant_id"],
    )
    assert canal == "instagram"


@pytest.mark.asyncio
async def test_sin_key_de_instagram_iniciar_no_atiende(http_client, tenant_y_usuario, configurado, monkeypatch):
    monkeypatch.setattr(settings, "NEUROAPI_INSTA_KEY", "")
    r = await http_client.post(
        "/api/canales/instagram/neuroapi/iniciar",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_con_cinco_altas_ajenas_en_24h_no_se_abre_la_sesion(
    http_client, tenant_y_usuario, configurado, monkeypatch
):
    monkeypatch.setattr(neuroapi_connect.httpx, "AsyncClient", _Cliente)
    for _ in range(5):
        await execute("INSERT INTO intentos_alta_ajena (tenant_id) VALUES ($1)", tenant_y_usuario["tenant_id"])

    r = await http_client.post(
        "/api/canales/instagram/neuroapi/iniciar",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert r.status_code == 429
    assert r.json()["detail"]["codigo"] == "demasiados_intentos"


# ============================================================
# instagram.connected
# ============================================================
@pytest.mark.asyncio
async def test_instagram_connected_activa_el_canal_del_dueno_de_la_sesion(
    http_client, tenant_y_usuario, configurado
):
    tenant_id = tenant_y_usuario["tenant_id"]
    ig_id = _ig_id()
    await _sesion(tenant_id, "sess_ok")

    r = await _post_webhook(http_client, _conectado("sess_ok", ig_id))
    assert r.status_code == 200

    cred = await fetch_one("SELECT * FROM get_channel_credentials($1, 'instagram')", tenant_id)
    assert cred["bsp_provider"] == "neuroapi"
    assert cred["ig_user_id"] == ig_id
    assert cred["access_token"] == CLAVE_INSTA
    assert cred["page_id"] is None
    activo = await fetch_value(
        "SELECT is_active FROM tenant_channels WHERE tenant_id = $1 AND channel_type = 'instagram'",
        tenant_id,
    )
    assert activo is True
    assert await fetch_value("SELECT resuelve_tenant('instagram', $1)", ig_id) == tenant_id

    estado = await http_client.get(
        "/api/canales/instagram/neuroapi/estado",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert estado.status_code == 200
    assert estado.json() == {
        "session_id": "sess_ok",
        "estado": "completado",
        "resultado": "vinculado",
        "codigo": None,
        "username": "mi_tienda_oficial",
    }


@pytest.mark.asyncio
async def test_estado_sin_sesiones_de_instagram_es_404(http_client, tenant_y_usuario, configurado):
    await _sesion(tenant_y_usuario["tenant_id"], "sess_wa", canal="whatsapp")
    r = await http_client.get(
        "/api/canales/instagram/neuroapi/estado",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_instagram_connected_sobre_una_sesion_de_whatsapp_no_activa_instagram(
    http_client, tenant_y_usuario, configurado
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _sesion(tenant_id, "sess_de_wa", canal="whatsapp")

    r = await _post_webhook(http_client, _conectado("sess_de_wa", _ig_id()))
    assert r.status_code == 200
    assert await fetch_one(
        "SELECT 1 FROM channel_credentials WHERE tenant_id = $1 AND channel_type = 'instagram'", tenant_id
    ) is None


@pytest.mark.asyncio
async def test_una_cuenta_de_otro_negocio_no_se_vincula_ni_se_revela(
    http_client, tenant_y_usuario, otro_tenant, configurado
):
    ig_id = _ig_id()
    await fetch_one("SELECT set_instagram_neuroapi($1, $2, 'k')", otro_tenant, ig_id)
    tenant_id = tenant_y_usuario["tenant_id"]
    await _sesion(tenant_id, "sess_ajena")

    await _post_webhook(http_client, _conectado("sess_ajena", ig_id))

    assert await fetch_value("SELECT resuelve_tenant('instagram', $1)", ig_id) == otro_tenant
    assert await fetch_one(
        "SELECT 1 FROM channel_credentials WHERE tenant_id = $1 AND channel_type = 'instagram'", tenant_id
    ) is None
    assert await fetch_value("SELECT COUNT(*) FROM intentos_alta_ajena WHERE tenant_id = $1", tenant_id) == 1
    estado = await http_client.get(
        "/api/canales/instagram/neuroapi/estado",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert estado.json()["estado"] == "fallido"
    assert estado.json()["resultado"] == "ajeno"
    assert estado.json()["codigo"] == "cuenta_instagram_no_encontrada"


@pytest.mark.asyncio
async def test_volver_a_vincular_la_misma_cuenta_es_propio(http_client, tenant_y_usuario, configurado):
    tenant_id = tenant_y_usuario["tenant_id"]
    ig_id = _ig_id()
    await _sesion(tenant_id, "sess_1")
    await _post_webhook(http_client, _conectado("sess_1", ig_id))
    await _sesion(tenant_id, "sess_2")
    await _post_webhook(http_client, _conectado("sess_2", ig_id))

    detalle = json.loads(
        await fetch_value("SELECT detalle FROM neuroapi_connect_sessions WHERE session_id = 'sess_2'")
    )
    assert detalle["resultado"] == "propio"
    assert detalle["codigo"] == "cuenta_instagram_ya_vinculada"
    assert await fetch_value("SELECT COUNT(*) FROM intentos_alta_ajena WHERE tenant_id = $1", tenant_id) == 0


@pytest.mark.asyncio
async def test_una_cuenta_conectada_por_oauth_de_meta_se_migra_a_neuroapi(
    http_client, tenant_y_usuario, configurado
):
    """Sin page_id deja de resolverse por el webhook de Meta: no hay respuestas dobles."""
    tenant_id = tenant_y_usuario["tenant_id"]
    ig_id = _ig_id()
    await fetch_one(
        "SELECT set_channel_credentials($1, 'instagram', 'page-token', NULL, 'PAGE-1', $2)",
        tenant_id, ig_id,
    )
    await _sesion(tenant_id, "sess_migra")

    await _post_webhook(http_client, _conectado("sess_migra", ig_id))

    cred = await fetch_one("SELECT * FROM get_channel_credentials($1, 'instagram')", tenant_id)
    assert cred["bsp_provider"] == "neuroapi"
    assert cred["page_id"] is None
    assert cred["access_token"] == CLAVE_INSTA


# ============================================================
# DMs entrantes
# ============================================================
@pytest.fixture
def reenvios(monkeypatch):
    capturados: list[dict] = []

    async def _reenviar(cuerpo_crudo, tenant_id, destino, *, canal="whatsapp"):
        capturados.append({"tenant_id": tenant_id, "canal": canal, "destino": destino})

    monkeypatch.setattr(entrada_mensajes, "reenviar", _reenviar)
    monkeypatch.setattr(entrada_mensajes, "destino_para", lambda t: "https://destino.test/entrada")
    return capturados


def _dm(ig_id: str, anidado: bool = False) -> dict:
    entry = {
        "id": ig_id,
        "time": 1,
        "messaging": [
            {
                "sender": {"id": "IGSID-CLIENTE"},
                "recipient": {"id": ig_id},
                "message": {"mid": "mid.1", "text": "hola"},
            }
        ],
    }
    if anidado:
        return {"object": "instagram", "entry": [{"entry": [entry]}]}
    return {"object": "instagram", "entry": [entry]}


@pytest.mark.asyncio
@pytest.mark.parametrize("anidado", [False, True])
async def test_un_dm_llega_al_negocio_de_la_cuenta_receptora(
    http_client, tenant_y_usuario, configurado, reenvios, anidado
):
    ig_id = _ig_id()
    await fetch_one("SELECT set_instagram_neuroapi($1, $2, 'k')", tenant_y_usuario["tenant_id"], ig_id)

    r = await _post_webhook(http_client, _dm(ig_id, anidado))

    assert r.status_code == 200
    assert reenvios == [
        {"tenant_id": tenant_y_usuario["tenant_id"], "canal": "instagram", "destino": "https://destino.test/entrada"}
    ]


@pytest.mark.asyncio
async def test_un_dm_de_una_cuenta_sin_negocio_no_se_reenvia(http_client, db, configurado, reenvios):
    r = await _post_webhook(http_client, _dm(_ig_id()))
    assert r.status_code == 200
    assert reenvios == []


def test_sin_entry_id_se_usa_el_recipient():
    evento = _dm("17841400000000001")
    del evento["entry"][0]["id"]
    assert entrada_mensajes.extraer_ig_account_id(evento) == "17841400000000001"


def test_normalizar_entiende_la_forma_anidada_y_saca_el_media_id():
    evento = _dm("17841400000000002", anidado=True)
    evento["entry"][0]["entry"][0]["messaging"][0]["message"] = {
        "mid": "mid.2",
        "attachments": [{"type": "image", "payload": {"id": "MEDIA-9", "url": "https://lookaside.fbsbx.com/x"}}],
    }
    sobres = normalizar.dividir_sobre(evento)
    assert len(sobres) == 1

    m = normalizar.normalizar(sobres[0])
    assert m.procesar
    assert m.canal == "instagram"
    assert m.usuario_id == "IGSID-CLIENTE"
    assert m.ig_account_id == "17841400000000002"
    assert m.media_id == "MEDIA-9"


def test_el_eco_del_propio_negocio_no_se_atiende():
    evento = _dm("17841400000000003")
    evento["entry"][0]["messaging"][0]["message"]["is_echo"] = True
    assert normalizar.normalizar(evento).procesar is False


# ============================================================
# Envío
# ============================================================
class _RespuestaEnvio:
    def __init__(self, status_code=200, datos=None):
        self.status_code = status_code
        self._datos = datos if datos is not None else {"success": True}
        self.text = json.dumps(self._datos)

    def json(self):
        return self._datos


class _ClienteEnvio:
    enviados: list[dict] = []
    respuesta = _RespuestaEnvio()

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, url, json=None, headers=None):
        _ClienteEnvio.enviados.append({"url": url, "json": json, "headers": headers})
        return _ClienteEnvio.respuesta


CRED_IG = {
    "channel_type": "instagram",
    "phone_number_id": None,
    "page_id": None,
    "ig_user_id": "17841400000000004",
    "access_token": CLAVE_INSTA,
    "bsp_provider": "neuroapi",
}


@pytest.mark.asyncio
async def test_el_agente_responde_por_neuroapi_sin_human_agent(monkeypatch):
    _ClienteEnvio.enviados = []
    _ClienteEnvio.respuesta = _RespuestaEnvio()

    async def _fetch_one(query, *args):
        return CRED_IG if "get_channel_credentials" in query else None

    async def _execute(query, *args):
        return "OK"

    monkeypatch.setattr(envio, "fetch_one", _fetch_one)
    monkeypatch.setattr(envio, "execute", _execute)
    monkeypatch.setattr(envio.httpx, "AsyncClient", _ClienteEnvio)

    resultado = await envio.entregar(
        Destino(tenant_id=uuid4(), canal="instagram", usuario_id="IGSID-CLIENTE", conversation_id=uuid4()),
        "Hola, ¿en qué te ayudo?",
    )

    assert resultado.entregado
    enviado = _ClienteEnvio.enviados[0]
    assert enviado["url"] == f"{settings.NEUROAPI_API_BASE_URL}/neuroapi/instagram/send"
    assert enviado["headers"] == {"x-api-key": CLAVE_INSTA}
    assert enviado["json"] == {
        "recipient_id": "IGSID-CLIENTE",
        "type": "text",
        "text": {"body": "Hola, ¿en qué te ayudo?"},
    }
    # La API key solo en la cabecera.
    assert CLAVE_INSTA not in json.dumps(enviado["json"])


@pytest.mark.asyncio
async def test_la_respuesta_de_una_persona_lleva_human_agent(monkeypatch):
    _ClienteEnvio.enviados = []
    _ClienteEnvio.respuesta = _RespuestaEnvio()
    conversacion_id, tenant_id, autor = uuid4(), uuid4(), uuid4()

    async def _preparar(t, c):
        return "instagram", "IGSID-CLIENTE", CRED_IG

    async def _fetch_one(query, *args):
        return {"id": uuid4(), "role": "human", "content": args[2], "created_at": None}

    async def _execute(query, *args):
        return "OK"

    monkeypatch.setattr(conversaciones, "_preparar_envio", _preparar)
    monkeypatch.setattr(conversaciones, "fetch_one", _fetch_one)
    monkeypatch.setattr(conversaciones, "execute", _execute)
    monkeypatch.setattr(mensajeria.httpx, "AsyncClient", _ClienteEnvio)

    await conversaciones.enviar_mensaje_humano(tenant_id, conversacion_id, "Te atiende Laura", autor)

    enviado = _ClienteEnvio.enviados[0]
    assert enviado["url"].endswith("/neuroapi/instagram/send")
    assert enviado["json"]["tag"] == "HUMAN_AGENT"
    assert enviado["json"]["recipient_id"] == "IGSID-CLIENTE"


@pytest.mark.asyncio
async def test_un_rechazo_de_neuroapi_se_traduce_como_uno_de_meta(monkeypatch):
    _ClienteEnvio.respuesta = _RespuestaEnvio(
        400, {"success": False, "error": {"code": 190, "message": "Invalid OAuth access token"}}
    )
    monkeypatch.setattr(mensajeria.httpx, "AsyncClient", _ClienteEnvio)

    with pytest.raises(meta.MetaError) as e:
        await mensajeria.enviar(mensajeria.peticion_instagram(CRED_IG, "IGSID", "hola", humano=True))
    # 190 -> 409 en el portal, nunca 401 (ver meta.a_http).
    assert meta.a_http(e.value).status_code == 409


# ============================================================
# Media
# ============================================================
@pytest.mark.asyncio
@pytest.mark.parametrize("media_id", ["../secreto", "a/b", "", "..", "x" * 300])
async def test_un_media_id_que_no_es_un_segmento_seguro_se_rechaza(media_id, monkeypatch):
    monkeypatch.setattr(settings, "NEUROAPI_INSTA_KEY", CLAVE_INSTA)
    with pytest.raises(adjuntos.AdjuntoInvalido):
        await adjuntos.descargar_media_instagram(media_id)


@pytest.mark.asyncio
async def test_la_media_se_baja_por_el_proxy_de_neuroapi_con_su_key(monkeypatch):
    monkeypatch.setattr(settings, "NEUROAPI_INSTA_KEY", CLAVE_INSTA)
    pedidos: list[tuple] = []

    async def _descargar(url, cabeceras):
        pedidos.append((url, cabeceras))
        return b"\x89PNG\r\n\x1a\nresto"

    monkeypatch.setattr(adjuntos, "_descargar", _descargar)

    contenido = await adjuntos.descargar_media_instagram("MEDIA-1")

    assert contenido.startswith(b"\x89PNG")
    assert pedidos == [
        (f"{settings.NEUROAPI_API_BASE_URL}/neuroapi/instagram/media/MEDIA-1", {"x-api-key": CLAVE_INSTA})
    ]
    assert adjuntos.nombre_por_contenido(contenido, "instagram-MEDIA-1") == "instagram-MEDIA-1.png"


@pytest.mark.asyncio
async def test_un_adjunto_que_no_se_puede_bajar_no_rompe_el_mensaje(monkeypatch):
    async def _falla(media_id):
        raise adjuntos.AdjuntoInvalido(502, "caído")

    monkeypatch.setattr(adjuntos, "descargar_media_instagram", _falla)
    assert await instagram_neuroapi.guardar_adjunto_entrante(uuid4(), uuid4(), "MEDIA-2") is False


# ============================================================
# Ventana de 7 días para respuestas humanas
# ============================================================
async def _conversacion_instagram(tenant_id):
    user_id = await fetch_value(
        "INSERT INTO users (tenant_id, instagram_id) VALUES ($1, $2) RETURNING id",
        tenant_id, f"IGSID-{uuid4().hex[:8]}",
    )
    conv_id = await fetch_value(
        "INSERT INTO conversations (tenant_id, user_id, channel_type, status) "
        "VALUES ($1, $2, 'instagram', 'transferred') RETURNING id",
        tenant_id, user_id,
    )
    await execute(
        "INSERT INTO messages (conversation_id, tenant_id, role, content, created_at) "
        "VALUES ($1, $2, 'user', 'hola', NOW() - INTERVAL '2 days')",
        conv_id, tenant_id,
    )
    return conv_id


@pytest.mark.asyncio
async def test_instagram_por_neuroapi_muestra_la_ventana_de_7_dias(tenant_y_usuario, configurado):
    tenant_id = tenant_y_usuario["tenant_id"]
    await fetch_one("SELECT set_instagram_neuroapi($1, $2, 'k')", tenant_id, _ig_id())
    conv_id = await _conversacion_instagram(tenant_id)

    cab, _ = await conversaciones.detalle(tenant_id, conv_id)

    # 24 h ya cerradas, pero a una persona le quedan unos 5 días.
    assert cab["minutos_restantes_ventana"] < 0
    assert 5 * 1440 - 5 <= cab["minutos_restantes_ventana_humana"] <= 5 * 1440


@pytest.mark.asyncio
async def test_instagram_por_meta_no_tiene_ventana_de_7_dias(tenant_y_usuario, configurado):
    tenant_id = tenant_y_usuario["tenant_id"]
    await fetch_one(
        "SELECT set_channel_credentials($1, 'instagram', 'page-token', NULL, 'PAGE-2', $2)",
        tenant_id, _ig_id(),
    )
    conv_id = await _conversacion_instagram(tenant_id)

    cab, _ = await conversaciones.detalle(tenant_id, conv_id)

    assert cab["minutos_restantes_ventana_humana"] is None
