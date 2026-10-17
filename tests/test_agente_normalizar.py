"""
Tests del normalizador de mensajes entrantes (services/agente/normalizar.py).

Es la pieza con más riesgo de la migración desde n8n: cada canal manda su
propio sobre, y por la misma URL llegan cosas que NO son mensajes. Un
descarte que se deja pasar hace que el agente conteste un acuse de entrega;
uno de más hace que un cliente se quede sin respuesta.

Lo que importa si falla:

  1. un mensaje de texto de cada canal sale con el cliente, el contenido y
     la cuenta receptora correctos
  2. lo que no es un mensaje (status, reacción, eco, read/delivery) se
     descarta y no llega al agente
  3. en Instagram, `entry.id` NO se confunde con el page_id de envío
  4. el widget exige los campos que el resto de canales traen de Meta

Sin base de datos: `normalizar()` es una función pura.
"""

from uuid import uuid4

import pytest

from services.agente.normalizar import dividir_sobre, normalizar

TENANT = uuid4()


# ============================================================
# WhatsApp
# ============================================================
def sobre_whatsapp(mensaje: dict, *, metadata: dict | None = None) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "entry-1", "changes": [{"value": {
            "metadata": metadata or {
                "display_phone_number": "5215500000000",
                "phone_number_id": "PNID-1",
            },
            "contacts": [{"profile": {"name": "Ana"}}],
            "messages": [mensaje],
        }}]}],
    }


def test_whatsapp_texto():
    r = normalizar(sobre_whatsapp(
        {"from": "5215599999999", "id": "wamid.A", "type": "text", "text": {"body": "hola"}}
    ))
    assert r.procesar
    assert r.canal == "whatsapp"
    assert r.usuario_id == "5215599999999"
    assert r.nombre == "Ana"
    assert r.mensaje_id == "wamid.A"
    assert r.contenido == "hola"
    assert r.phone_number_id == "PNID-1"


def test_whatsapp_imagen_usa_el_caption_y_guarda_media_id():
    r = normalizar(sobre_whatsapp(
        {"from": "521", "id": "wamid.B", "type": "image",
         "image": {"id": "media-9", "caption": "¿tienen este?"}}
    ))
    assert r.procesar
    assert r.contenido == "¿tienen este?"
    assert r.media_id == "media-9"
    assert r.tipo == "image"


def test_whatsapp_imagen_sin_caption_cae_a_una_etiqueta():
    r = normalizar(sobre_whatsapp(
        {"from": "521", "id": "wamid.C", "type": "image", "image": {"id": "media-9"}}
    ))
    assert r.procesar
    assert r.contenido == "[Imagen]"


def test_whatsapp_status_de_entrega_se_descarta():
    sobre = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}],
    }
    assert normalizar(sobre).procesar is False


def test_whatsapp_reaccion_se_descarta():
    r = normalizar(sobre_whatsapp(
        {"from": "521", "id": "wamid.D", "type": "reaction", "reaction": {"emoji": "👍"}}
    ))
    assert r.procesar is False


def test_whatsapp_eco_del_propio_numero_se_descarta():
    """
    Si la cuenta tiene suscrito `message_echoes`, Meta reenvía lo que manda
    el propio bot. Sin este filtro el agente se contesta a sí mismo en bucle.
    """
    r = normalizar(sobre_whatsapp(
        {"from": "5215500000000", "id": "wamid.E", "type": "text", "text": {"body": "eco"}},
        metadata={"display_phone_number": "+52 1 55 0000 0000", "phone_number_id": "PNID-1"},
    ))
    assert r.procesar is False
    assert "eco" in (r.motivo or "").lower()


@pytest.mark.parametrize("tipo", ["system", "unknown"])
def test_whatsapp_avisos_de_plataforma_se_descartan(tipo):
    """El cambio de número del cliente o un fallo de Meta no se contestan."""
    r = normalizar(sobre_whatsapp({"from": "521", "id": "wamid.S", "type": tipo}))
    assert r.procesar is False


def test_whatsapp_respuesta_de_boton_usa_el_texto_elegido():
    r = normalizar(sobre_whatsapp(
        {"from": "521", "id": "wamid.G", "type": "button", "button": {"text": "Sí, confirmo", "payload": "p"}}
    ))
    assert r.procesar
    assert r.contenido == "Sí, confirmo"


@pytest.mark.parametrize("subtipo,titulo", [("button_reply", "Opción A"), ("list_reply", "Fila 2")])
def test_whatsapp_interactive_usa_el_titulo_elegido(subtipo, titulo):
    r = normalizar(sobre_whatsapp({
        "from": "521", "id": "wamid.H", "type": "interactive",
        "interactive": {"type": subtipo, subtipo: {"id": "x", "title": titulo}},
    }))
    assert r.procesar
    assert r.contenido == titulo


def test_whatsapp_statuses_junto_a_mensajes_no_tapan_el_mensaje():
    sobre = sobre_whatsapp({"from": "521", "id": "wamid.I", "type": "text", "text": {"body": "hola"}})
    sobre["entry"][0]["changes"][0]["value"]["statuses"] = [{"status": "read"}]
    assert normalizar(sobre).procesar


def test_whatsapp_nombre_del_contacto_que_escribio():
    sobre = sobre_whatsapp({"from": "522", "id": "wamid.J", "type": "text", "text": {"body": "hola"}})
    sobre["entry"][0]["changes"][0]["value"]["contacts"] = [
        {"wa_id": "521", "profile": {"name": "Ana"}},
        {"wa_id": "522", "profile": {"name": "Beto"}},
    ]
    assert normalizar(sobre).nombre == "Beto"


def test_whatsapp_tipo_desconocido_no_rompe():
    r = normalizar(sobre_whatsapp({"from": "521", "id": "wamid.F", "type": "contacts"}))
    assert r.procesar
    assert r.tipo == "unsupported"


# ============================================================
# Instagram y Facebook
# ============================================================
def sobre_messenger(objeto: str, mensaje: dict, *, entry_id: str = "ENTRY-9") -> dict:
    return {
        "object": objeto,
        "entry": [{"id": entry_id, "messaging": [{"sender": {"id": "cliente-1"}, "message": mensaje}]}],
    }


def test_instagram_texto_no_confunde_entry_id_con_page_id():
    """
    En Instagram `entry.id` es el id de la CUENTA, y el envío va contra el
    page_id vinculado, que se lee de channel_credentials. Mezclarlos hacía
    fallar la respuesta.
    """
    r = normalizar(sobre_messenger("instagram", {"mid": "mid.1", "text": "hola"}))
    assert r.procesar
    assert r.canal == "instagram"
    assert r.ig_account_id == "ENTRY-9"
    assert r.page_id is None


def test_facebook_texto_si_usa_entry_id_como_page_id():
    r = normalizar(sobre_messenger("page", {"mid": "mid.2", "text": "hola"}))
    assert r.procesar
    assert r.canal == "facebook"
    assert r.page_id == "ENTRY-9"


def test_messenger_eco_del_bot_se_descarta():
    r = normalizar(sobre_messenger("page", {"mid": "mid.3", "text": "eco", "is_echo": True}))
    assert r.procesar is False


def test_messenger_mensaje_borrado_se_descarta():
    r = normalizar(sobre_messenger("instagram", {"mid": "mid.5", "is_deleted": True}))
    assert r.procesar is False


def test_messenger_evento_sin_mensaje_se_descarta():
    """Los avisos de 'read' y 'delivery' llegan por la misma URL."""
    sobre = {"object": "page", "entry": [{"id": "E", "messaging": [{"read": {"watermark": 1}}]}]}
    assert normalizar(sobre).procesar is False


def test_messenger_adjunto_sin_texto():
    r = normalizar(sobre_messenger("instagram", {
        "mid": "mid.4",
        "attachments": [{"type": "image", "payload": {"url": "https://x/y.jpg"}}],
    }))
    assert r.procesar
    assert r.tipo == "image"
    assert r.media_url == "https://x/y.jpg"


# ============================================================
# Sobres que agrupan varios eventos
# ============================================================
def _msg_wa(i: int) -> dict:
    return {"from": "521", "id": f"wamid.{i}", "type": "text", "text": {"body": f"m{i}"}}


def test_dividir_whatsapp_varios_mensajes_y_varias_entradas():
    sobre = {
        "object": "whatsapp_business_account",
        "entry": [
            {"id": "e1", "changes": [{"value": {"metadata": {"phone_number_id": "P1"}, "messages": [_msg_wa(1), _msg_wa(2)]}}]},
            {"id": "e2", "changes": [{"value": {"metadata": {"phone_number_id": "P2"}, "messages": [_msg_wa(3)]}}]},
        ],
    }
    partes = dividir_sobre(sobre)
    resultados = [normalizar(p) for p in partes]
    assert [r.contenido for r in resultados] == ["m1", "m2", "m3"]
    assert [r.phone_number_id for r in resultados] == ["P1", "P1", "P2"]


def test_dividir_whatsapp_quita_statuses_que_vienen_con_mensajes():
    sobre = sobre_whatsapp(_msg_wa(1))
    sobre["entry"][0]["changes"][0]["value"]["statuses"] = [{"status": "read"}]
    (parte,) = dividir_sobre(sobre)
    assert "statuses" not in parte["entry"][0]["changes"][0]["value"]


def test_dividir_whatsapp_solo_statuses_sigue_descartandose():
    sobre = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {"statuses": [{"status": "read"}]}}]}],
    }
    partes = dividir_sobre(sobre)
    assert len(partes) == 1
    assert normalizar(partes[0]).procesar is False


def test_dividir_messenger_varios_eventos():
    sobre = {
        "object": "page",
        "entry": [{"id": "PG", "messaging": [
            {"sender": {"id": "c1"}, "message": {"mid": "m1", "text": "a"}},
            {"sender": {"id": "c2"}, "message": {"mid": "m2", "text": "b"}},
            {"read": {"watermark": 1}},
        ]}],
    }
    resultados = [normalizar(p) for p in dividir_sobre(sobre)]
    assert [r.procesar for r in resultados] == [True, True, False]
    assert [r.usuario_id for r in resultados[:2]] == ["c1", "c2"]
    assert all(r.page_id == "PG" for r in resultados[:2])


def test_dividir_sobre_sin_entradas_devuelve_el_original():
    sobre = {"object": "page"}
    assert dividir_sobre(sobre) == [sobre]


# ============================================================
# Widget web
# ============================================================
def test_widget_mensaje_completo():
    r = normalizar(
        {"object": "web_widget", "session_id": "sess-1", "contenido": "hola", "nombre": "Leo"},
        tenant_id=TENANT,
    )
    assert r.procesar
    assert r.canal == "web"
    assert r.tenant_id == TENANT
    assert r.usuario_id == "sess-1"
    assert r.nombre == "Leo"
    # El widget no tiene id de canal: no hay reintentos contra los que
    # deduplicar, y el INSERT lo guarda como NULL.
    assert r.mensaje_id is None


def test_widget_sin_tenant_resuelto_se_descarta():
    r = normalizar({"object": "web_widget", "session_id": "s", "contenido": "hola"})
    assert r.procesar is False


def test_widget_sin_contenido_se_descarta():
    r = normalizar({"object": "web_widget", "session_id": "s"}, tenant_id=TENANT)
    assert r.procesar is False


def test_widget_sin_session_id_se_descarta():
    r = normalizar({"object": "web_widget", "contenido": "hola"}, tenant_id=TENANT)
    assert r.procesar is False


# ============================================================
# Sobres que no son de ningún canal
# ============================================================
def test_payload_sin_object_se_descarta():
    assert normalizar({"entry": []}).procesar is False


def test_payload_sin_entry_se_descarta():
    assert normalizar({"object": "page"}).procesar is False


def test_canal_desconocido_se_descarta():
    r = normalizar({"object": "threads", "entry": [{"id": "1"}]})
    assert r.procesar is False
    assert "threads" in (r.motivo or "")
