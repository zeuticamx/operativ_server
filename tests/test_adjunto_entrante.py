"""
POST /api/eventos/adjunto-entrante: n8n asocia a un mensaje del cliente el
archivo recibido por WhatsApp. Sin BD: se sustituye fetch_one.
"""

import base64
from uuid import uuid4

import pytest
from fastapi import HTTPException

from routers import eventos
from schemas import AdjuntoEntranteIn
from tests.test_adjuntos import PDF, PNG


def _datos(contenido: bytes, nombre: str) -> AdjuntoEntranteIn:
    return AdjuntoEntranteIn(
        tenant_id=uuid4(),
        mensaje_id=uuid4(),
        nombre=nombre,
        contenido_base64=base64.b64encode(contenido).decode(),
    )


def _parchar(monkeypatch, mensaje_existe=True):
    llamadas = []

    async def fetch_one(sql, *args):
        llamadas.append((sql, args))
        if "FROM messages" in sql:
            return {"id": args[0]} if mensaje_existe else None
        return {"id": uuid4(), "mime": args[2], "nombre": args[3], "bytes": args[4]}

    monkeypatch.setattr(eventos, "fetch_one", fetch_one)
    return llamadas


async def test_guarda_el_archivo_recibido_como_entrante(monkeypatch):
    llamadas = _parchar(monkeypatch)
    datos = _datos(PNG, "foto.png")

    r = await eventos.adjunto_entrante(datos)

    assert (r.mime, r.nombre, r.bytes) == ("image/png", "foto.png", len(PNG))
    sql, args = llamadas[1]
    assert "'in'" in sql
    assert args[0] == datos.tenant_id and args[1] == datos.mensaje_id
    assert args[5] == PNG


async def test_mensaje_de_otro_tenant_da_404(monkeypatch):
    llamadas = _parchar(monkeypatch, mensaje_existe=False)

    with pytest.raises(HTTPException) as e:
        await eventos.adjunto_entrante(_datos(PDF, "a.pdf"))

    assert e.value.status_code == 404
    assert len(llamadas) == 1  # no llegó a insertar


async def test_tipo_no_permitido_da_415_sin_tocar_la_bd(monkeypatch):
    llamadas = _parchar(monkeypatch)

    with pytest.raises(HTTPException) as e:
        await eventos.adjunto_entrante(_datos(b"OggS audio", "nota.ogg"))

    assert e.value.status_code == 415
    assert llamadas == []


async def test_base64_invalido_da_422(monkeypatch):
    _parchar(monkeypatch)
    datos = _datos(PNG, "foto.png")
    datos.contenido_base64 = "no es base64!!"

    with pytest.raises(HTTPException) as e:
        await eventos.adjunto_entrante(datos)

    assert e.value.status_code == 422


# ------------------------------------------------------------
# Por URL (el link que NeuroAPI deja en el webhook)
# ------------------------------------------------------------
from pydantic import ValidationError  # noqa: E402

from services import adjuntos  # noqa: E402

LINK = "https://api.neurochat.com.ec/storage/media/temp_abc123.jpg"


def _por_url(url=LINK, nombre=None) -> AdjuntoEntranteIn:
    return AdjuntoEntranteIn(tenant_id=uuid4(), mensaje_id=uuid4(), url=url, nombre=nombre)


def test_exige_exactamente_una_fuente():
    base = dict(tenant_id=uuid4(), mensaje_id=uuid4())
    with pytest.raises(ValidationError):
        AdjuntoEntranteIn(**base)
    with pytest.raises(ValidationError):
        AdjuntoEntranteIn(**base, url=LINK, contenido_base64="AAAA")


async def test_descarga_por_url_y_toma_el_nombre_del_link(monkeypatch):
    llamadas = _parchar(monkeypatch)
    pedidas = []

    async def descargar(url):
        pedidas.append(url)
        return PNG

    monkeypatch.setattr(adjuntos, "descargar_media", descargar)
    # Hoy no sería .jpg: el nombre del link manda y debe coincidir con los bytes.
    r = await eventos.adjunto_entrante(_por_url("https://api.neurochat.com.ec/m/temp.png"))

    assert pedidas == ["https://api.neurochat.com.ec/m/temp.png"]
    assert (r.mime, r.nombre) == ("image/png", "temp.png")
    assert "'in'" in llamadas[1][0]


async def test_el_nombre_explicito_gana_al_del_link(monkeypatch):
    _parchar(monkeypatch)

    async def descargar(url):
        return PDF

    monkeypatch.setattr(adjuntos, "descargar_media", descargar)
    r = await eventos.adjunto_entrante(_por_url("https://api.neurochat.com.ec/m/x", "oferta.pdf"))
    assert r.nombre == "oferta.pdf"


@pytest.mark.parametrize(
    "url",
    [
        "http://api.neurochat.com.ec/m/a.jpg",  # sin TLS
        "https://evil.example.com/a.jpg",  # otro host (SSRF)
        "https://api.neurochat.com.ec.evil.com/a.jpg",  # host que solo empieza igual
        "https://169.254.169.254/latest/meta-data",  # metadatos de la nube
    ],
)
async def test_url_de_origen_no_permitido_da_422_sin_salir_a_la_red(url):
    with pytest.raises(adjuntos.AdjuntoInvalido) as e:
        await adjuntos.descargar_media(url)
    assert e.value.status_code == 422


async def test_endpoint_traduce_el_rechazo_de_origen_a_422(monkeypatch):
    llamadas = _parchar(monkeypatch)

    with pytest.raises(HTTPException) as e:
        await eventos.adjunto_entrante(_por_url("https://evil.example.com/a.jpg"))

    assert e.value.status_code == 422
    assert llamadas == []
