"""
Modo coexistencia de WhatsApp (app móvil + API): elección al vincular, permiso
por plan y sincronización de los ecos de la app móvil (solo lectura, sin
duplicar lo que mandó nuestra API).

Lo que se prueba es nuestro lado; NeuroAPI y Meta no se llaman nunca.
"""

import json
from uuid import uuid4

import pytest

from config import settings
from services import ecos_coexistencia, neuroapi_connect
from services.ecos_coexistencia import EcoMensaje, contenido_del_eco, extraer_ecos
from session import execute, fetch_all, fetch_one, fetch_value
from tests.test_neuroapi_connect import (
    SECRETO,
    _ClienteFalso,
    _RespuestaFalsa,
    _sesion_pendiente,
    con_secreto,  # noqa: F401  (fixture)
    firmar,
    neuroapi_simulado,  # noqa: F401  (fixture)
)

pytestmark = pytest.mark.usefixtures("plan_enterprise")


def _sobre_eco(*items: dict, phone_number_id: str = "pn-eco", campo: str = "smb_message_echoes") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": campo,
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "5215500000000",
                                "phone_number_id": phone_number_id,
                            },
                            "message_echoes": list(items),
                        },
                    }
                ],
            }
        ],
    }


def _eco(wamid: str, texto: str = "hola desde el móvil", to: str = "5215511112222") -> dict:
    return {
        "from": "5215500000000",
        "to": to,
        "id": wamid,
        "type": "text",
        "text": {"body": texto},
    }


# ============================================================
# Parser (puro)
# ============================================================
def test_extrae_ecos_del_campo_smb_message_echoes():
    ecos = extraer_ecos(_sobre_eco(_eco("wamid.A", "uno"), _eco("wamid.B", "dos")))
    assert ecos == [
        EcoMensaje("wamid.A", "5215511112222", "uno", "text"),
        EcoMensaje("wamid.B", "5215511112222", "dos", "text"),
    ]


def test_acepta_message_echoes_en_value_con_otro_field():
    assert len(extraer_ecos(_sobre_eco(_eco("wamid.A"), campo="messages"))) == 1


def test_acepta_el_sobre_dentro_de_data():
    evento = {"event": "x", "data": _sobre_eco(_eco("wamid.A"))}
    assert [e.wamid for e in extraer_ecos(evento)] == ["wamid.A"]


def test_descarta_ecos_sin_id_o_sin_destinatario():
    sin_id = {**_eco("wamid.A"), "id": ""}
    sin_to = {**_eco("wamid.B"), "to": None}
    assert extraer_ecos(_sobre_eco(sin_id, sin_to, "no soy un dict")) == []


def test_un_mensaje_de_cliente_no_es_un_eco():
    evento = {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {"messages": [{"from": "521", "id": "wamid.C", "type": "text"}]},
                    }
                ]
            }
        ]
    }
    assert not ecos_coexistencia.hay_ecos(evento)
    assert ecos_coexistencia.hay_mensajes_de_clientes(evento)
    assert extraer_ecos(evento) == []


@pytest.mark.parametrize(
    "item, esperado",
    [
        ({"type": "text", "text": {"body": "hola"}}, "hola"),
        ({"type": "image", "image": {"caption": "foto"}}, "foto"),
        ({"type": "image", "image": {"id": "m1"}}, "[Imagen]"),
        ({"type": "document", "document": {"filename": "lista.pdf"}}, "lista.pdf"),
        ({"type": "audio", "audio": {"id": "m2"}}, "[Audio]"),
        ({"type": "raro"}, "[Mensaje sin contenido reconocible]"),
    ],
)
def test_contenido_del_eco(item, esperado):
    assert contenido_del_eco(item) == esperado


# ============================================================
# Plan: gerencia decide si la incluye
# ============================================================
@pytest.fixture
async def plan_con_coexistencia():
    """Cambia la bandera del plan enterprise y la deja como estaba."""
    previo = await fetch_value("SELECT coexistencia_activa FROM planes WHERE nombre = 'enterprise'")

    async def poner(valor: bool) -> None:
        await execute("UPDATE planes SET coexistencia_activa = $1 WHERE nombre = 'enterprise'", valor)

    yield poner
    await execute("UPDATE planes SET coexistencia_activa = $1 WHERE nombre = 'enterprise'", previo)


async def _iniciar(http_client, tenant_y_usuario, cuerpo=None):
    return await http_client.post(
        "/api/canales/whatsapp/neuroapi/iniciar",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
        json=cuerpo,
    )


@pytest.mark.asyncio
async def test_coexistencia_con_plan_que_no_la_incluye_es_402(
    http_client, tenant_y_usuario, neuroapi_simulado, plan_con_coexistencia
):
    await plan_con_coexistencia(False)
    neuroapi_simulado(_RespuestaFalsa({"data": {"session_id": "s1", "url": "https://x"}}))

    r = await _iniciar(http_client, tenant_y_usuario, {"connection_mode": "coexistence"})

    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "coexistencia_no_incluida_en_plan"
    assert _ClienteFalso.ultimo_payload is None  # ni se abrió la sesión en NeuroAPI
    assert await fetch_value(
        "SELECT COUNT(*) FROM neuroapi_connect_sessions WHERE tenant_id = $1",
        tenant_y_usuario["tenant_id"],
    ) == 0


@pytest.mark.asyncio
async def test_modo_exclusivo_funciona_aunque_el_plan_no_incluya_coexistencia(
    http_client, tenant_y_usuario, neuroapi_simulado, plan_con_coexistencia
):
    await plan_con_coexistencia(False)
    neuroapi_simulado(_RespuestaFalsa({"data": {"session_id": "s_std", "url": "https://x"}}))

    for cuerpo in (None, {"connection_mode": "standard"}):
        await execute(
            "DELETE FROM neuroapi_connect_sessions WHERE tenant_id = $1", tenant_y_usuario["tenant_id"]
        )
        r = await _iniciar(http_client, tenant_y_usuario, cuerpo)
        assert r.status_code == 200
        modo = await fetch_value(
            "SELECT connection_mode FROM neuroapi_connect_sessions WHERE session_id = 's_std'"
        )
        assert modo == "standard"


@pytest.mark.asyncio
async def test_modo_invalido_es_422(http_client, tenant_y_usuario, neuroapi_simulado):
    neuroapi_simulado(_RespuestaFalsa({"data": {"session_id": "s", "url": "https://x"}}))
    r = await _iniciar(http_client, tenant_y_usuario, {"connection_mode": "otro"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_coexistencia_se_guarda_en_la_sesion_y_no_cambia_el_payload_a_neuroapi(
    http_client, tenant_y_usuario, neuroapi_simulado, plan_con_coexistencia
):
    await plan_con_coexistencia(True)
    neuroapi_simulado(_RespuestaFalsa({"data": {"session_id": "s_coex", "url": "https://x"}}))

    r = await _iniciar(http_client, tenant_y_usuario, {"connection_mode": "coexistence"})

    assert r.status_code == 200
    fila = await fetch_one(
        "SELECT status, connection_mode FROM neuroapi_connect_sessions WHERE session_id = 's_coex'"
    )
    assert (fila["status"], fila["connection_mode"]) == ("pendiente", "coexistence")
    assert set(_ClienteFalso.ultimo_payload) == {
        "service_type", "return_url", "webhook_url", "webhook_secret",
    }


@pytest.mark.asyncio
async def test_la_lista_de_numeros_dice_si_el_plan_incluye_coexistencia(
    http_client, tenant_y_usuario, plan_con_coexistencia
):
    headers = {"Authorization": f"Bearer {tenant_y_usuario['token']}"}
    for valor in (False, True):
        await plan_con_coexistencia(valor)
        r = await http_client.get("/api/canales/whatsapp/numeros", headers=headers)
        assert r.status_code == 200
        assert r.json()["coexistencia_permitida"] is valor


# ============================================================
# Webhook: whatsapp.connected marca el modo en el número
# ============================================================
async def _conectar(http_client, session_id: str, phone_number_id: str) -> None:
    cuerpo = json.dumps(
        {
            "event": "whatsapp.connected",
            "session_id": session_id,
            "data": {"phones": [{"phoneNumberId": phone_number_id, "displayPhone": "+52 55 0000"}]},
        }
    ).encode()
    r = await http_client.post(
        "/api/canales/whatsapp/neuroapi/webhook",
        content=cuerpo,
        headers={"x-hub-signature-256": firmar(cuerpo), "content-type": "application/json"},
    )
    assert r.status_code == 200


async def _sesion_con_modo(tenant_id, session_id: str, modo: str) -> None:
    await _sesion_pendiente(tenant_id, session_id)
    await execute(
        "UPDATE neuroapi_connect_sessions SET connection_mode = $2 WHERE session_id = $1",
        session_id,
        modo,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("modo", ["standard", "coexistence"])
async def test_el_numero_vinculado_queda_con_el_modo_de_la_sesion(
    http_client, tenant_y_usuario, con_secreto, modo
):
    pn = f"pn-{uuid4().hex[:10]}"
    await _sesion_con_modo(tenant_y_usuario["tenant_id"], f"sess-{pn}", modo)

    await _conectar(http_client, f"sess-{pn}", pn)

    fila = await fetch_one(
        "SELECT estado, connection_mode FROM agentes_canal WHERE phone_number_id = $1", pn
    )
    assert (fila["estado"], fila["connection_mode"]) == ("activo", modo)


# ============================================================
# Webhook: ecos
# ============================================================
async def _numero_coexistencia(tenant_id, modo: str = "coexistence") -> str:
    pn = f"pn-{uuid4().hex[:10]}"
    await execute(
        """
        INSERT INTO agentes_canal (tenant_id, phone_number_id, estado, connection_mode)
        VALUES ($1, $2, 'activo', $3)
        """,
        tenant_id,
        pn,
        modo,
    )
    return pn


async def _enviar_evento(http_client, evento: dict):
    cuerpo = json.dumps(evento).encode()
    return await http_client.post(
        "/api/canales/whatsapp/neuroapi/webhook",
        content=cuerpo,
        headers={"x-hub-signature-256": firmar(cuerpo), "content-type": "application/json"},
    )


async def _mensajes(tenant_id):
    return await fetch_all(
        "SELECT role, content, channel_msg_id, desde_app_movil, sender_portal_user_id "
        "FROM messages WHERE tenant_id = $1 ORDER BY created_at, id",
        tenant_id,
    )


@pytest.fixture
def sin_reenvio(monkeypatch):
    """Si un eco llegara a reenviarse al agente, el test lo ve."""
    reenviados: list[bytes] = []

    async def _reenviar(cuerpo, tenant_id, destino, **_):
        reenviados.append(cuerpo)

    monkeypatch.setattr(entrada_mensajes_modulo(), "reenviar", _reenviar)
    return reenviados


def entrada_mensajes_modulo():
    from services import entrada_mensajes

    return entrada_mensajes


@pytest.mark.asyncio
async def test_un_eco_se_guarda_como_mensaje_humano_de_solo_lectura(
    http_client, tenant_y_usuario, con_secreto, sin_reenvio
):
    tid = tenant_y_usuario["tenant_id"]
    pn = await _numero_coexistencia(tid)

    r = await _enviar_evento(http_client, _sobre_eco(_eco("wamid.E1", "te mando la lista"), phone_number_id=pn))

    assert r.status_code == 200
    mensajes = await _mensajes(tid)
    assert len(mensajes) == 1
    m = mensajes[0]
    assert (m["role"], m["content"], m["channel_msg_id"]) == ("human", "te mando la lista", "wamid.E1")
    assert m["desde_app_movil"] is True
    assert m["sender_portal_user_id"] is None
    assert sin_reenvio == []  # el eco no llega al agente


@pytest.mark.asyncio
async def test_el_mismo_wamid_no_se_guarda_dos_veces(
    http_client, tenant_y_usuario, con_secreto, sin_reenvio
):
    tid = tenant_y_usuario["tenant_id"]
    pn = await _numero_coexistencia(tid)
    evento = _sobre_eco(_eco("wamid.REINTENTO"), phone_number_id=pn)

    await _enviar_evento(http_client, evento)
    await _enviar_evento(http_client, evento)

    assert len(await _mensajes(tid)) == 1


@pytest.mark.asyncio
async def test_el_eco_de_lo_que_mando_nuestra_api_con_wamid_no_se_duplica(
    http_client, tenant_y_usuario, con_secreto, sin_reenvio
):
    tid = tenant_y_usuario["tenant_id"]
    pn = await _numero_coexistencia(tid)
    # Un primer eco crea la conversación; luego "nuestra API" guarda su envío con wamid.
    await _enviar_evento(http_client, _sobre_eco(_eco("wamid.PRIMERO", "hola"), phone_number_id=pn))
    conversation_id = await fetch_value("SELECT id FROM conversations WHERE tenant_id = $1", tid)
    await execute(
        "INSERT INTO messages (conversation_id, tenant_id, role, content, channel_msg_id) "
        "VALUES ($1, $2, 'human', 'desde el portal', 'wamid.PORTAL')",
        conversation_id,
        tid,
    )

    await _enviar_evento(http_client, _sobre_eco(_eco("wamid.PORTAL", "desde el portal"), phone_number_id=pn))

    assert [m["content"] for m in await _mensajes(tid)] == ["hola", "desde el portal"]


@pytest.mark.asyncio
async def test_el_eco_de_una_respuesta_del_agente_sin_wamid_se_reconoce_por_contenido(
    http_client, tenant_y_usuario, con_secreto, sin_reenvio
):
    tid = tenant_y_usuario["tenant_id"]
    pn = await _numero_coexistencia(tid)
    await _enviar_evento(http_client, _sobre_eco(_eco("wamid.PRIMERO", "hola"), phone_number_id=pn))
    conversation_id = await fetch_value("SELECT id FROM conversations WHERE tenant_id = $1", tid)
    await execute(
        "INSERT INTO messages (conversation_id, tenant_id, role, content) "
        "VALUES ($1, $2, 'assistant', 'Claro, con gusto te ayudo')",
        conversation_id,
        tid,
    )

    await _enviar_evento(
        http_client, _sobre_eco(_eco("wamid.AGENTE", "Claro, con gusto te ayudo"), phone_number_id=pn)
    )

    mensajes = await _mensajes(tid)
    assert [(m["role"], m["content"]) for m in mensajes] == [
        ("human", "hola"),
        ("assistant", "Claro, con gusto te ayudo"),
    ]
    # Y el wamid quedó anotado: un segundo eco igual ya se descarta por id.
    assert mensajes[1]["channel_msg_id"] == "wamid.AGENTE"
    assert mensajes[1]["desde_app_movil"] is False


@pytest.mark.asyncio
async def test_un_eco_en_un_numero_marcado_exclusivo_lo_promueve_a_coexistencia(
    http_client, tenant_y_usuario, con_secreto, sin_reenvio
):
    tid = tenant_y_usuario["tenant_id"]
    pn = await _numero_coexistencia(tid, modo="standard")

    r = await _enviar_evento(http_client, _sobre_eco(_eco("wamid.X"), phone_number_id=pn))

    assert r.status_code == 200
    assert len(await _mensajes(tid)) == 1
    assert await fetch_value(
        "SELECT connection_mode FROM agentes_canal WHERE phone_number_id = $1", pn
    ) == "coexistence"
    assert sin_reenvio == []


@pytest.mark.asyncio
async def test_un_eco_nuevo_pasa_la_conversacion_a_modo_humano_sin_acuse(
    http_client, tenant_y_usuario, con_secreto, sin_reenvio
):
    tid = tenant_y_usuario["tenant_id"]
    pn = await _numero_coexistencia(tid)

    await _enviar_evento(http_client, _sobre_eco(_eco("wamid.H1"), phone_number_id=pn))

    fila = await fetch_one(
        "SELECT status, metadata FROM conversations WHERE tenant_id = $1", tid
    )
    assert fila["status"] == "transferred"
    assert fila["metadata"]["ack_pendiente"] is False


@pytest.mark.asyncio
async def test_el_eco_de_lo_que_mando_nuestra_api_no_apaga_la_ia(
    http_client, tenant_y_usuario, con_secreto, sin_reenvio
):
    tid = tenant_y_usuario["tenant_id"]
    pn = await _numero_coexistencia(tid)
    await _enviar_evento(http_client, _sobre_eco(_eco("wamid.H1", "hola"), phone_number_id=pn))
    conversation_id = await fetch_value("SELECT id FROM conversations WHERE tenant_id = $1", tid)
    # La IA vuelve a tomar la conversación y responde.
    await execute("UPDATE conversations SET status = 'active' WHERE id = $1", conversation_id)
    await execute(
        "INSERT INTO messages (conversation_id, tenant_id, role, content) "
        "VALUES ($1, $2, 'assistant', 'respuesta de la IA')",
        conversation_id,
        tid,
    )

    await _enviar_evento(
        http_client, _sobre_eco(_eco("wamid.IA", "respuesta de la IA"), phone_number_id=pn)
    )

    assert await fetch_value("SELECT status FROM conversations WHERE id = $1", conversation_id) == "active"


@pytest.mark.asyncio
async def test_ecos_de_un_numero_ajeno_no_se_guardan_en_ningun_negocio(
    http_client, tenant_y_usuario, con_secreto, sin_reenvio
):
    r = await _enviar_evento(http_client, _sobre_eco(_eco("wamid.AJENO"), phone_number_id="pn-nadie"))

    assert r.status_code == 200
    assert await _mensajes(tenant_y_usuario["tenant_id"]) == []


@pytest.mark.asyncio
async def test_un_mensaje_de_cliente_sigue_reenviandose_al_agente(
    http_client, tenant_y_usuario, con_secreto, sin_reenvio, monkeypatch
):
    tid = tenant_y_usuario["tenant_id"]
    pn = await _numero_coexistencia(tid)
    evento = {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": pn},
                            "messages": [{"from": "5215511112222", "id": "wamid.C", "type": "text",
                                          "text": {"body": "hola"}}],
                        },
                    }
                ]
            }
        ]
    }

    await _enviar_evento(http_client, evento)

    assert len(sin_reenvio) == 1
    assert await _mensajes(tid) == []  # lo guarda el agente, no la sincronización de ecos
