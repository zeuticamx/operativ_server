"""
PNG y JPEG de prueba armados a mano, sin Pillow.

El PNG es válido de verdad (IHDR + IDAT comprimido + IEND, con CRC): lo
abre cualquier visor. El JPEG trae solo los segmentos que mira
services/imagen.py (SOI, APP0, SOF, EOI) — no es decodificable, pero el
backend no decodifica, solo lee el encabezado.
"""

import struct
import zlib


def _chunk(tipo: bytes, datos: bytes) -> bytes:
    return (
        struct.pack(">I", len(datos))
        + tipo
        + datos
        + struct.pack(">I", zlib.crc32(tipo + datos) & 0xFFFFFFFF)
    )


def png(ancho: int, alto: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", ancho, alto, 8, 0, 0, 0, 0)  # 8 bits, escala de grises
    fila = b"\x00" + b"\x80" * ancho  # filtro 0 + píxeles grises
    idat = zlib.compress(fila * alto)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def jpeg(ancho: int, alto: int, sof: int = 0xC0) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    componentes = b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    sof_seg = (
        bytes([0xFF, sof])
        + struct.pack(">HBHHB", 8 + len(componentes), 8, alto, ancho, 3)
        + componentes
    )
    return b"\xff\xd8" + app0 + sof_seg + b"\xff\xd9"


GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,"
WEBP = b"RIFF\x1a\x00\x00\x00WEBPVP8 \x0e\x00\x00\x00"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'
PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
