"""
Envío de imágenes/documentos por WhatsApp: services/conversaciones.
enviar_adjunto_humano, el cuerpo que arma NeuroApiProvider.enviar_media y
la descarga de adjuntos. Sin BD ni red: se sustituyen fetch_one, execute,
transaccion y los clientes HTTP.
"""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi import HTTPException

from config import settings
from services import conversaciones as svc
from services import meta, whatsapp
from tests.test_adjuntos import PDF, PNG

TOKEN_FIJO = "t" * 43
MSG_ID, ADJ_ID = uuid4(), uuid4()


class _Conn:
    """Conexión falsa: guarda los INSERT y devuelve filas fijas."""

    def __init__(self, registro):
        self.registro = registro

    async def fetchrow(self, sql, *args):
        self.registro.append((sql, args))
        if "INTO messages" in sql:
            return {"id": MSG_ID, "role": "human", "content": args[2], "created_at": None}
        return {"id": ADJ_ID, "mime": args[2], "nombre": args[3], "bytes": args[4]}


@pytest.fixture
def entorno(monkeypatch):
    """Parcha todo lo que toca BD/red; devuelve el registro de llamadas."""
    reg = {"inserts": [], "execute": [], "proveedor": [], "meta": []}
    cred = {
        "access_token": "api-key",
        "bsp_provider": "neuroapi",
        "phone_number_id": "123",
    }
    conv = {
        "id": uuid4(),
        "status": "transferred",
        "channel_type": "whatsapp",
        "whatsapp_id": "593999111222",
        "instagram_id": None,
        "facebook_id": None,
    }
    reg["conv"], reg["cred"] = conv, cred

    async def fetch_one(sql, *args):
        return cred if "get_channel_credentials" in sql else reg["conv"]

    async def execute(sql, *args):
        reg["execute"].append((sql, args))
        return "OK"

    @asynccontextmanager
    async def transaccion():
        yield _Conn(reg["inserts"])

    async def enviar_media(self, destinatario, tipo, url, nombre=None, leyenda=None):
        reg["proveedor"].append((destinatario, tipo, url, nombre, leyenda))
        if reg.get("falla"):
            raise whatsapp.ProveedorWhatsAppError("boom", status_code=500)
        return whatsapp.ResultadoEnvio(id_mensaje="x")

    async def enviar_texto(url, token, body):
        reg["meta"].append((url, token, body))
        return {}

    monkeypatch.setattr(svc, "fetch_one", fetch_one)
    monkeypatch.setattr(svc, "execute", execute)
    monkeypatch.setattr(svc, "transaccion", transaccion)
    monkeypatch.setattr(svc.secrets, "token_urlsafe", lambda n: TOKEN_FIJO)
    monkeypatch.setattr(whatsapp.NeuroApiProvider, "enviar_media", enviar_media)
    monkeypatch.setattr(meta, "enviar_texto", enviar_texto)
    monkeypatch.setattr(settings, "BASE_URL_BACKEND", "https://api.ejemplo.com")
    return reg


async def _enviar(contenido=PNG, nombre="foto.png", leyenda=None):
    return await svc.enviar_adjunto_humano(
        uuid4(), uuid4(), contenido, nombre, leyenda, uuid4()
    )


async def test_neuroapi_manda_el_link_publico_y_guarda_mensaje_y_adjunto(entorno):
    fila, adjunto = await _enviar(leyenda="Tu cotización")

    destinatario, tipo, url, nombre, leyenda = entorno["proveedor"][0]
    assert destinatario == "593999111222"
    assert tipo == "image"
    assert url == f"https://api.ejemplo.com/api/media/{TOKEN_FIJO}"
    assert leyenda == "Tu cotización"

    sql_msg, args_msg = entorno["inserts"][0]
    assert args_msg[2] == "Tu cotización"
    sql_adj, args_adj = entorno["inserts"][1]
    assert "'out'" in sql_adj
    assert args_adj[2:5] == ("image/png", "foto.png", len(PNG))
    assert args_adj[6] == TOKEN_FIJO
    assert adjunto["mime"] == "image/png"
    assert any("last_message_at" in s for s, _ in entorno["execute"])


async def test_documento_sin_leyenda_usa_etiqueta_y_tipo_document(entorno):
    await _enviar(PDF, "contrato.pdf")

    assert entorno["proveedor"][0][1] == "document"
    assert entorno["inserts"][0][1][2] == "[Documento: contrato.pdf]"


async def test_camino_graph_api_arma_el_body_de_whatsapp(entorno):
    entorno["cred"]["bsp_provider"] = None
    await _enviar(PDF, "contrato.pdf", "ver")

    url, token, body = entorno["meta"][0]
    assert url.endswith("/123/messages") and token == "api-key"
    assert body["messaging_product"] == "whatsapp"
    assert body["type"] == "document"
    assert body["document"] == {
        "link": f"https://api.ejemplo.com/api/media/{TOKEN_FIJO}",
        "caption": "ver",
        "filename": "contrato.pdf",
    }


async def test_archivo_invalido_no_toca_al_proveedor_ni_la_bd(entorno):
    with pytest.raises(HTTPException) as e:
        await _enviar(PDF, "falso.png")

    assert e.value.status_code == 415
    assert entorno["proveedor"] == [] and entorno["inserts"] == []


async def test_archivo_demasiado_grande_da_413(entorno, monkeypatch):
    monkeypatch.setattr(settings, "WA_IMAGEN_MAX_BYTES", 5)
    with pytest.raises(HTTPException) as e:
        await _enviar()
    assert e.value.status_code == 413
    assert entorno["proveedor"] == []


async def test_conversacion_no_transferida_da_409(entorno):
    entorno["conv"]["status"] = "active"
    with pytest.raises(HTTPException) as e:
        await _enviar()
    assert e.value.status_code == 409
    assert entorno["inserts"] == []


@pytest.mark.parametrize("canal", ["instagram", "facebook"])
async def test_otros_canales_dan_400(entorno, canal):
    entorno["conv"]["channel_type"] = canal
    entorno["conv"][f"{canal}_id"] = "abc"
    with pytest.raises(HTTPException) as e:
        await _enviar()
    assert e.value.status_code == 400
    assert entorno["proveedor"] == []


async def test_backend_sin_https_publico_da_409(entorno, monkeypatch):
    monkeypatch.setattr(settings, "BASE_URL_BACKEND", "http://localhost:8000")
    with pytest.raises(HTTPException) as e:
        await _enviar()
    assert e.value.status_code == 409
    assert entorno["inserts"] == []


async def test_si_el_proveedor_falla_se_borra_el_mensaje_y_da_502(entorno):
    entorno["falla"] = True
    with pytest.raises(HTTPException) as e:
        await _enviar()

    assert e.value.status_code == 502
    borrados = [a for s, a in entorno["execute"] if s.startswith("DELETE FROM messages")]
    assert borrados == [(MSG_ID,)]
    # No se actualiza last_message_at de un mensaje que no salió.
    assert not any("last_message_at" in s for s, _ in entorno["execute"])


# ------------------------------------------------------------
# Proveedor
# ------------------------------------------------------------
def test_cuerpo_media_imagen_y_documento():
    img = whatsapp.cuerpo_media("593", "image", "https://x/y", "a.png", "hola")
    assert img == {
        "to": "593",
        "type": "image",
        "image": {"link": "https://x/y", "caption": "hola"},
    }

    doc = whatsapp.cuerpo_media("593", "document", "https://x/y", "a.pdf")
    assert doc["document"] == {"link": "https://x/y", "filename": "a.pdf"}


def test_cuerpo_media_rechaza_otros_tipos():
    with pytest.raises(ValueError):
        whatsapp.cuerpo_media("593", "video", "https://x/y")


async def test_neuroapi_enviar_media_postea_el_cuerpo(monkeypatch):
    capturado = {}

    async def _post(self, cuerpo):
        capturado.update(cuerpo)
        return {"success": True, "data": {"message_id": "wamid.1", "status": "sent"}}

    monkeypatch.setattr(whatsapp.NeuroApiProvider, "_post", _post)
    p = whatsapp.NeuroApiProvider("k", "", "https://api")
    r = await p.enviar_media("593", "document", "https://x/y", "a.pdf", "ver")

    assert r.id_mensaje == "wamid.1"
    assert capturado["type"] == "document"
    assert capturado["document"]["link"] == "https://x/y"


async def test_proveedor_sin_soporte_de_media_falla_controlado():
    p = whatsapp.MetaProvider()
    with pytest.raises(whatsapp.ProveedorWhatsAppError):
        await p.enviar_media("593", "image", "https://x/y")


# ------------------------------------------------------------
# Descarga
# ------------------------------------------------------------
async def test_adjunto_de_otro_tenant_no_se_encuentra(monkeypatch):
    llamadas = []

    async def fetch_one(sql, *args):
        llamadas.append((sql, args))
        return None

    monkeypatch.setattr(svc, "fetch_one", fetch_one)
    tenant, conv, adj = uuid4(), uuid4(), uuid4()

    assert await svc.obtener_adjunto(tenant, conv, adj) is None
    sql, args = llamadas[0]
    assert "a.tenant_id = $3" in sql and args == (adj, conv, tenant)


async def test_media_publica_token_desconocido_da_404(monkeypatch):
    from routers import media

    async def fetch_one(sql, *args):
        return None

    monkeypatch.setattr(media, "fetch_one", fetch_one)
    with pytest.raises(HTTPException) as e:
        await media.descargar("x" * 43)
    assert e.value.status_code == 404


async def test_media_publica_token_con_largo_absurdo_ni_consulta_la_bd(monkeypatch):
    from routers import media

    async def fetch_one(sql, *args):
        raise AssertionError("no debería consultar")

    monkeypatch.setattr(media, "fetch_one", fetch_one)
    with pytest.raises(HTTPException) as e:
        await media.descargar("x" * 500)
    assert e.value.status_code == 404


async def test_media_publica_sirve_el_contenido_con_nosniff(monkeypatch):
    from routers import media

    async def fetch_one(sql, *args):
        assert "direccion = 'out'" in sql
        return {"mime": "application/pdf", "nombre": "a b.pdf", "contenido": PDF}

    monkeypatch.setattr(media, "fetch_one", fetch_one)
    r = await media.descargar("x" * 43)

    assert r.body == PDF
    assert r.media_type == "application/pdf"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "a%20b.pdf" in r.headers["content-disposition"]
