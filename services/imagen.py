"""
Validación de la foto de perfil sin librerías de imágenes.

Solo se aceptan JPEG y PNG, y ninguno de los dos lados puede pasar de
PERFIL_FOTO_MAX_PX (642). No se redimensiona acá: eso exigiría Pillow, una
dependencia más. El portal redimensiona en el navegador (canvas) antes de
subir; esto es la barrera para quien llame a la API directo.

El formato se decide por la FIRMA de los bytes, no por el nombre ni por el
Content-Type, que los pone el cliente: un PDF renombrado a .png no pasa. La
extensión del nombre se exige además y tiene que coincidir con la firma.

Las dimensiones se leen del encabezado (IHDR en PNG, marcador SOF en JPEG)
con `struct`. No se decodifica la imagen entera: un archivo con encabezado
válido y cuerpo corrupto pasaría y se vería roto — con el re-codificado del
navegador ese caso solo aparece llamando a la API a mano.
"""

import struct
from dataclasses import dataclass

FIRMA_PNG = b"\x89PNG\r\n\x1a\n"
FIRMA_JPEG = b"\xff\xd8\xff"

EXTENSIONES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

# Marcadores Start Of Frame de JPEG: son los que traen alto y ancho.
# C4 (tabla Huffman), C8 (reservado) y CC (tabla aritmética) caen en el
# mismo rango pero no son SOF.
_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# Marcadores sin segmento de longitud detrás.
_SIN_LONGITUD = frozenset({0x01, 0xD8, *range(0xD0, 0xD8)})


class ImagenInvalida(Exception):
    """La imagen no se acepta. `status_code` es el HTTP que corresponde."""

    def __init__(self, status_code: int, mensaje: str):
        super().__init__(mensaje)
        self.status_code = status_code
        self.mensaje = mensaje


@dataclass(frozen=True)
class InfoImagen:
    mime: str
    ancho: int
    alto: int


def _mime_por_firma(contenido: bytes) -> str | None:
    if contenido.startswith(FIRMA_PNG):
        return "image/png"
    if contenido.startswith(FIRMA_JPEG):
        return "image/jpeg"
    return None


def _dimensiones_png(contenido: bytes) -> tuple[int, int]:
    # Firma (8) + largo del chunk (4) + "IHDR" (4) + ancho (4) + alto (4).
    if len(contenido) < 24 or contenido[12:16] != b"IHDR":
        raise ImagenInvalida(422, "El archivo PNG está dañado o incompleto.")
    ancho, alto = struct.unpack(">II", contenido[16:24])
    return ancho, alto


def _dimensiones_jpeg(contenido: bytes) -> tuple[int, int]:
    i = 2  # después de FF D8
    largo = len(contenido)
    while i < largo:
        if contenido[i] != 0xFF:
            break
        # Puede haber bytes de relleno 0xFF antes del marcador.
        while i < largo and contenido[i] == 0xFF:
            i += 1
        if i >= largo:
            break
        marcador = contenido[i]
        i += 1
        if marcador in _SIN_LONGITUD:
            continue
        # Fin de imagen o inicio de los datos sin haber visto un SOF: no hay
        # dimensiones que leer.
        if marcador in (0xD9, 0xDA):
            break
        if i + 2 > largo:
            break
        (longitud,) = struct.unpack(">H", contenido[i : i + 2])
        if longitud < 2:
            break
        if marcador in _SOF:
            # longitud (2) + precisión (1) + alto (2) + ancho (2)
            if i + 7 > largo:
                break
            alto, ancho = struct.unpack(">HH", contenido[i + 3 : i + 7])
            return ancho, alto
        i += longitud
    raise ImagenInvalida(422, "El archivo JPEG está dañado o incompleto.")


def validar_foto(contenido: bytes, nombre_archivo: str | None, max_px: int) -> InfoImagen:
    """
    Devuelve el tipo real y las dimensiones, o lanza ImagenInvalida:
      415 -> no es JPEG/PNG, o la extensión no coincide con el contenido
      422 -> encabezado dañado, dimensiones en cero o mayores a `max_px`
    """
    nombre = (nombre_archivo or "").lower().strip()
    extension = "." + nombre.rsplit(".", 1)[-1] if "." in nombre else ""
    mime_extension = EXTENSIONES.get(extension)
    if mime_extension is None:
        raise ImagenInvalida(415, "La foto tiene que ser JPG, JPEG o PNG.")

    mime = _mime_por_firma(contenido)
    if mime is None:
        raise ImagenInvalida(415, "La foto tiene que ser JPG, JPEG o PNG.")
    if mime != mime_extension:
        raise ImagenInvalida(
            415, "La extensión del archivo no coincide con su contenido."
        )

    ancho, alto = _dimensiones_png(contenido) if mime == "image/png" else _dimensiones_jpeg(contenido)

    if ancho < 1 or alto < 1:
        raise ImagenInvalida(422, "La imagen no tiene dimensiones válidas.")
    if ancho > max_px or alto > max_px:
        raise ImagenInvalida(
            422,
            f"La foto mide {ancho}x{alto} px; el máximo es {max_px}x{max_px} px.",
        )

    return InfoImagen(mime=mime, ancho=ancho, alto=alto)
