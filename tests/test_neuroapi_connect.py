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
from uuid import uuid4

import pytest

from config import settings
from security import crear_access_token, hash_password
from services import entrada_mensajes, neuroapi_connect
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


async def _completar(http_client, session_id: str, phone_number_id: str) -> None:
    cuerpo = json.dumps(
        {"session_id": session_id, "status": "completado", "phone_number_id": phone_number_id}
    ).encode()
    respuesta = await http_client.post(
        "/api/canales/whatsapp/neuroapi/webhook",
        content=cuerpo,
        headers={"x-hub-signature-256": firmar(cuerpo), "content-type": "application/json"},
    )
    assert respuesta.status_code == 200


@pytest.mark.asyncio
async def test_la_linea_activada_queda_lista_para_que_n8n_conteste(
    http_client, tenant_y_usuario, con_secreto, monkeypatch
):
    """
    n8n contesta por NeuroAPI solo si get_channel_credentials le da
    bsp_provider='neuroapi' y un token para x-api-key. Antes quedaban NULL y
    'meta', y el agente no contestaba nunca.
    """
    monkeypatch.setattr(settings, "NEUROAPI_API_KEY", "clave-de-prueba")
    tenant_id = tenant_y_usuario["tenant_id"]
    await _sesion_pendiente(tenant_id, "sess_lista")

    await _completar(http_client, "sess_lista", "5215511111111")

    cred = await fetch_one(
        "SELECT * FROM get_channel_credentials($1, 'whatsapp')", tenant_id
    )
    assert cred["bsp_provider"] == "neuroapi"
    assert cred["access_token"] == "clave-de-prueba"
    assert cred["phone_number_id"] == "5215511111111"


@pytest.mark.asyncio
async def test_reconectar_con_otro_numero_no_sobrescribe_la_credencial(
    http_client, tenant_y_usuario, con_secreto, monkeypatch
):
    """P2: channel_credentials guarda el primer número (la lee cuenta_global_wa)."""
    monkeypatch.setattr(settings, "NEUROAPI_API_KEY", "clave-de-prueba")
    tenant_id = tenant_y_usuario["tenant_id"]
    await _sesion_pendiente(tenant_id, "sess_uno")
    await _completar(http_client, "sess_uno", "5215511111111")
    await _sesion_pendiente(tenant_id, "sess_dos")
    await _completar(http_client, "sess_dos", "5215522222222")

    cred = await fetch_one(
        "SELECT * FROM get_channel_credentials($1, 'whatsapp')", tenant_id
    )
    assert cred["phone_number_id"] == "5215511111111"
    assert cred["bsp_provider"] == "neuroapi"
    assert cred["access_token"] == "clave-de-prueba"

    vinculados = await fetch_value(
        "SELECT COUNT(*) FROM agentes_canal "
        "WHERE tenant_id = $1 AND estado IN ('activo', 'pausado')",
        tenant_id,
    )
    assert vinculados == 2


@pytest.mark.asyncio
async def test_desconectar_en_bloque_ya_no_existe_usar_numeros(
    http_client, tenant_y_usuario, con_secreto, monkeypatch
):
    """
    Spec 005a: WhatsApp se gestiona número por número. Tras desvincular el
    número, lo que siga mandando NeuroAPI no llega al agente (no resuelve a
    ningún tenant).
    """
    monkeypatch.setattr(settings, "NEUROAPI_API_KEY", "clave-de-prueba")
    tenant_id = tenant_y_usuario["tenant_id"]
    await _sesion_pendiente(tenant_id, "sess_baja")
    await _completar(http_client, "sess_baja", "5215533330000")

    respuesta = await http_client.delete(
        "/api/canales/whatsapp",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 409
    assert respuesta.json()["detail"]["codigo"] == "usar_numeros"

    numero_id = await fetch_value(
        "SELECT id FROM agentes_canal WHERE tenant_id = $1 AND phone_number_id = $2",
        tenant_id,
        "5215533330000",
    )
    respuesta = await http_client.delete(
        f"/api/canales/whatsapp/numeros/{numero_id}",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 204

    # El número desvinculado se sigue atribuyendo a su último dueño (RF-16):
    # el mensaje se reenvía y ya no recibe respuesta (lo guarda en ocultas
    # la entrada del agente, T7).
    reenvios: list[tuple] = []

    async def no_reenviar(cuerpo_crudo, tenant, destino, **_):
        reenvios.append((tenant, destino))

    monkeypatch.setattr(entrada_mensajes, "reenviar", no_reenviar)
    mensaje = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "5215533330000"},
            "messages": [{"from": "5215599999999", "id": "wamid.B", "type": "text",
                          "text": {"body": "hola"}}],
        }}]}],
    }).encode()
    respuesta = await http_client.post(
        "/api/canales/whatsapp/neuroapi/webhook",
        content=mensaje,
        headers={"x-hub-signature-256": firmar(mensaje), "content-type": "application/json"},
    )
    assert respuesta.status_code == 200
    assert reenvios == [(tenant_id, entrada_mensajes.destino_para(tenant_id))]


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
        # Formato real observado en producción (2026-09-29).
        {"event": "whatsapp.connected", "session_id": "sess_sobre",
         "service_type": "whatsapp_cloud_api", "timestamp": "2026-09-29T22:48:25.778Z",
         "data": {"phones": [{"id": "ceb244b8-3608-44ab-90b7-9228d57638e5",
                              "wabaId": "1594460962159498", "wabaName": "Operativai",
                              "phoneNumberId": "5215522222222", "displayPhone": "+52 1 56 5431 9727",
                              "serviceType": "whatsapp_cloud_api", "connectionMethod": "api"}]}},
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
        "formato-real-produccion",
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


# ============================================================
# T6: iniciar comprueba cupo y bloqueo; el webhook vincula por RF-2
# ============================================================
async def _alta_ajena(http_client, headers, pnid):
    r = await http_client.post(
        "/api/canales/whatsapp/conectar", json={"phone_number_id": pnid}, headers=headers
    )
    return r


@pytest.mark.asyncio
async def test_iniciar_con_cupo_lleno_da_402_sin_abrir_sesion(
    http_client, tenant_y_usuario, neuroapi_simulado
):
    nombre = f"cupo6-{uuid4().hex[:10]}"
    await execute(
        "INSERT INTO planes (nombre, precio_monthly, max_numeros_whatsapp, activo)"
        " VALUES ($1, 9999, 1, true)",
        nombre,
    )
    try:
        await execute(
            "INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly)"
            " VALUES ($1, $2, 'activa', 10)"
            " ON CONFLICT (tenant_id) DO UPDATE SET plan = EXCLUDED.plan, estado = 'activa'",
            tenant_y_usuario["tenant_id"],
            nombre,
        )
        headers = {"Authorization": f"Bearer {tenant_y_usuario['token']}"}
        r = await _alta_ajena(http_client, headers, "5215500000001")
        assert r.status_code == 201

        neuroapi_simulado(_RespuestaFalsa({"success": True, "data": {"session_id": "s", "url": "https://x"}}))
        r = await http_client.post("/api/canales/whatsapp/neuroapi/iniciar", headers=headers)
        assert r.status_code == 402
        assert r.json()["detail"]["codigo"] == "cupo_numeros_whatsapp"
        assert _ClienteFalso.ultimo_payload is None
    finally:
        await execute("DELETE FROM tenant_subscriptions WHERE plan = $1", nombre)
        await execute("DELETE FROM planes WHERE nombre = $1", nombre)


@pytest.mark.asyncio
async def test_iniciar_bloqueado_da_429_sin_abrir_sesion(
    http_client, tenant_y_usuario, neuroapi_simulado, db
):
    from uuid import uuid4 as _uuid4

    headers = {"Authorization": f"Bearer {tenant_y_usuario['token']}"}
    tid_otro = _uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro')", tid_otro)
    try:
        uid = _uuid4()
        await execute(
            """
            INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
            VALUES ($1, $2, $3, $4, 'owner', true)
            """,
            uid,
            tid_otro,
            f"owner-{uid}@ejemplo.com",
            hash_password("x" * 12),
        )
        await execute(
            "INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly)"
            " VALUES ($1, 'enterprise', 'activa', 299.99)",
            tid_otro,
        )
        from security import crear_access_token as _token

        h_otro = {"Authorization": f"Bearer {_token(uid, tid_otro, 'owner')}"}
        r = await _alta_ajena(http_client, h_otro, "5215500000002")
        assert r.status_code == 201
        for _ in range(5):
            assert (await _alta_ajena(http_client, headers, "5215500000002")).status_code == 404

        neuroapi_simulado(_RespuestaFalsa({"success": True, "data": {"session_id": "s", "url": "https://x"}}))
        r = await http_client.post("/api/canales/whatsapp/neuroapi/iniciar", headers=headers)
        assert r.status_code == 429
        assert r.json()["detail"]["codigo"] == "demasiados_intentos"
        assert _ClienteFalso.ultimo_payload is None
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", tid_otro)


@pytest.mark.asyncio
async def test_webhook_vincula_libre_ignora_propio_y_rechaza_ajeno(
    http_client, tenant_y_usuario, con_secreto
):
    from uuid import uuid4 as _uuid4

    tid = tenant_y_usuario["tenant_id"]
    headers = {"Authorization": f"Bearer {tenant_y_usuario['token']}"}

    await _sesion_pendiente(tid, "sess_libre")
    await _completar(http_client, "sess_libre", "5215560000001")
    fila = await fetch_one(
        "SELECT estado, telefono_visible FROM agentes_canal WHERE tenant_id = $1 AND phone_number_id = $2",
        tid,
        "5215560000001",
    )
    assert fila["estado"] == "activo"

    await _sesion_pendiente(tid, "sess_propia")
    await _completar(http_client, "sess_propia", "5215560000001")
    assert await fetch_value(
        "SELECT COUNT(*) FROM agentes_canal WHERE phone_number_id = $1 AND estado IN ('activo', 'pausado')",
        "5215560000001",
    ) == 1

    tid_otro = _uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro')", tid_otro)
    try:
        await _sesion_pendiente(tid_otro, "sess_ajena")
        cuerpo = json.dumps(
            {"session_id": "sess_ajena", "status": "completado", "phone_number_id": "5215560000001"}
        ).encode()
        r = await http_client.post(
            "/api/canales/whatsapp/neuroapi/webhook",
            content=cuerpo,
            headers={"x-hub-signature-256": firmar(cuerpo), "content-type": "application/json"},
        )
        assert r.status_code == 200
        assert await fetch_value(
            "SELECT COUNT(*) FROM agentes_canal WHERE tenant_id = $1", tid_otro
        ) == 0
        intentos = await fetch_value(
            "SELECT COUNT(*) FROM intentos_alta_ajena WHERE tenant_id = $1", tid_otro
        )
        assert intentos == 1
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", tid_otro)


@pytest.mark.asyncio
async def test_estado_muestra_el_resultado_del_webhook(
    http_client, tenant_y_usuario, con_secreto
):
    tid = tenant_y_usuario["tenant_id"]
    headers = {"Authorization": f"Bearer {tenant_y_usuario['token']}"}
    await _sesion_pendiente(tid, "sess_estado")
    await _completar(http_client, "sess_estado", "5215570000001")

    r = await http_client.get(
        "/api/canales/whatsapp/neuroapi/estado",
        params={"session_id": "sess_estado"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json() == {
        "session_id": "sess_estado",
        "estado": "completado",
        "connection_mode": "standard",
        "resultado": "vinculado",
        "codigo": None,
        "phone_number_id": "5215570000001",
    }

    r = await http_client.get(
        "/api/canales/whatsapp/neuroapi/estado",
        params={"session_id": "sess_ajena_xxx"},
        headers=headers,
    )
    assert r.status_code == 404
