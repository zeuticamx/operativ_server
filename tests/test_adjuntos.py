"""
services/adjuntos.validar_adjunto: tipo por firma de bytes, extensión que
coincide y tope de tamaño por tipo. Sin BD.
"""

import io
import zipfile

import pytest

from config import settings
from services import adjuntos
from services.adjuntos import AdjuntoInvalido, validar_adjunto

JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"x" * 32


def _zip(*nombres: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in nombres:
            z.writestr(n, "x")
    return buf.getvalue()


DOCX = _zip("[Content_Types].xml", "word/document.xml")


@pytest.mark.parametrize(
    "contenido,nombre,mime,tipo_wa",
    [
        (JPG, "foto.jpg", "image/jpeg", "image"),
        (JPG, "FOTO.JPEG", "image/jpeg", "image"),
        (PNG, "captura.png", "image/png", "image"),
        (PDF, "contrato.pdf", "application/pdf", "document"),
        (DOCX, "oferta.docx", adjuntos.MIME_DOCX, "document"),
    ],
)
def test_acepta_los_cuatro_tipos(contenido, nombre, mime, tipo_wa):
    info = validar_adjunto(contenido, nombre)
    assert (info.mime, info.tipo_wa) == (mime, tipo_wa)


def test_vacio_da_422():
    with pytest.raises(AdjuntoInvalido) as e:
        validar_adjunto(b"", "a.png")
    assert e.value.status_code == 422


def test_sin_nombre_da_422():
    with pytest.raises(AdjuntoInvalido) as e:
        validar_adjunto(PNG, "   ")
    assert e.value.status_code == 422


@pytest.mark.parametrize(
    "contenido,nombre",
    [
        (b"MZ\x90\x00 ejecutable", "virus.png"),  # firma desconocida
        (b"GIF89a....", "animado.gif"),  # imagen de otro formato
        (PDF, "falso.png"),  # PDF renombrado a imagen
        (PNG, "falso.pdf"),  # imagen renombrada a PDF
        (_zip("a.txt"), "cualquiera.docx"),  # ZIP que no es Word
        (_zip("[Content_Types].xml", "xl/workbook.xml"), "hoja.docx"),  # xlsx
        (DOCX, "oferta.doc"),  # extensión incorrecta
        (b"PK\x03\x04roto", "roto.docx"),  # ZIP corrupto
    ],
)
def test_tipo_no_permitido_o_extension_que_no_coincide_da_415(contenido, nombre):
    with pytest.raises(AdjuntoInvalido) as e:
        validar_adjunto(contenido, nombre)
    assert e.value.status_code == 415


def test_ignora_la_ruta_del_nombre():
    info = validar_adjunto(PDF, "C:\\Users\\x\\..\\contrato.pdf")
    assert info.nombre == "contrato.pdf"


def test_limite_de_imagen_en_el_borde(monkeypatch):
    monkeypatch.setattr(settings, "WA_IMAGEN_MAX_BYTES", len(PNG))
    assert validar_adjunto(PNG, "a.png").mime == "image/png"
    with pytest.raises(AdjuntoInvalido) as e:
        validar_adjunto(PNG + b"\x00", "a.png")
    assert e.value.status_code == 413


def test_limite_de_documento_es_independiente_del_de_imagen(monkeypatch):
    monkeypatch.setattr(settings, "WA_IMAGEN_MAX_BYTES", 10)
    monkeypatch.setattr(settings, "WA_DOCUMENTO_MAX_BYTES", len(PDF))
    assert validar_adjunto(PDF, "a.pdf").tipo_wa == "document"
    monkeypatch.setattr(settings, "WA_DOCUMENTO_MAX_BYTES", len(PDF) - 1)
    with pytest.raises(AdjuntoInvalido) as e:
        validar_adjunto(PDF, "a.pdf")
    assert e.value.status_code == 413


def test_texto_del_mensaje():
    img = validar_adjunto(PNG, "a.png")
    doc = validar_adjunto(PDF, "contrato.pdf")
    assert adjuntos.texto_mensaje(img, None) == "[Imagen]"
    assert adjuntos.texto_mensaje(img, "  mira esto ") == "mira esto"
    assert adjuntos.texto_mensaje(doc, "") == "[Documento: contrato.pdf]"
