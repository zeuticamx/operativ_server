"""
services/imagen.py: formato por firma de bytes y dimensiones por
encabezado, sin librerías de imágenes.
"""

import pytest

from services.imagen import ImagenInvalida, validar_foto
from tests.imagenes_prueba import GIF, PDF, SVG, WEBP, jpeg, png

MAX = 642


@pytest.mark.parametrize(
    "nombre, contenido, mime",
    [
        ("foto.png", png(642, 642), "image/png"),
        ("foto.PNG", png(1, 1), "image/png"),
        ("foto.jpg", jpeg(642, 300), "image/jpeg"),
        ("foto.jpeg", jpeg(200, 642), "image/jpeg"),
        # JPEG progresivo (SOF2): también es un JPEG válido.
        ("foto.jpg", jpeg(400, 400, sof=0xC2), "image/jpeg"),
    ],
)
def test_acepta_jpeg_y_png_hasta_el_maximo(nombre, contenido, mime):
    info = validar_foto(contenido, nombre, MAX)
    assert info.mime == mime


def test_lee_las_dimensiones_reales():
    assert (validar_foto(png(300, 120), "a.png", MAX).ancho, validar_foto(png(300, 120), "a.png", MAX).alto) == (300, 120)
    info = validar_foto(jpeg(512, 256), "a.jpg", MAX)
    assert (info.ancho, info.alto) == (512, 256)


@pytest.mark.parametrize(
    "contenido, nombre",
    [
        (png(643, 100), "a.png"),
        (png(100, 643), "a.png"),
        (jpeg(643, 643), "a.jpg"),
        (jpeg(4000, 3000), "a.jpeg"),
    ],
)
def test_rechaza_lo_que_pasa_de_642_px_por_cualquier_lado(contenido, nombre):
    with pytest.raises(ImagenInvalida) as e:
        validar_foto(contenido, nombre, MAX)
    assert e.value.status_code == 422
    assert "642x642" in e.value.mensaje


@pytest.mark.parametrize(
    "contenido, nombre",
    [
        (GIF, "a.gif"),
        (WEBP, "a.webp"),
        (SVG, "a.svg"),
        (PDF, "a.pdf"),
        # Renombrados: la extensión dice imagen, los bytes no.
        (GIF, "a.png"),
        (PDF, "a.jpg"),
        (SVG, "a.png"),
        # Sin extensión.
        (png(10, 10), "foto"),
        (png(10, 10), None),
    ],
)
def test_rechaza_lo_que_no_es_jpeg_ni_png(contenido, nombre):
    with pytest.raises(ImagenInvalida) as e:
        validar_foto(contenido, nombre, MAX)
    assert e.value.status_code == 415


def test_la_extension_tiene_que_coincidir_con_el_contenido():
    """Un JPEG llamado .png (o al revés) no pasa."""
    with pytest.raises(ImagenInvalida) as e:
        validar_foto(jpeg(10, 10), "a.png", MAX)
    assert e.value.status_code == 415
    with pytest.raises(ImagenInvalida):
        validar_foto(png(10, 10), "a.jpg", MAX)


@pytest.mark.parametrize(
    "contenido, nombre",
    [
        (png(10, 10)[:20], "a.png"),  # corta antes del IHDR completo
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 30, "a.png"),  # sin IHDR
        (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00", "a.jpg"),  # sin SOF
        (b"\xff\xd8\xff\xd9", "a.jpg"),  # SOI + EOI, nada en medio
        (png(0, 10), "a.png"),  # dimensión en cero
    ],
)
def test_encabezado_danado_o_sin_dimensiones_es_422(contenido, nombre):
    with pytest.raises(ImagenInvalida) as e:
        validar_foto(contenido, nombre, MAX)
    assert e.value.status_code == 422
