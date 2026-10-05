"""
Validación de adjuntos de WhatsApp (JPG, PNG, PDF, DOCX) sin dependencias.

Igual que services/imagen.py, el tipo se decide por la FIRMA de los bytes y
no por el nombre ni por el Content-Type, que los pone el cliente. La
extensión del nombre se exige además y tiene que coincidir con lo detectado:
un PDF renombrado a .png, o un ZIP cualquiera renombrado a .docx, no pasan.
"""

import io
import os
import re
import zipfile
from urllib.parse import quote, urlparse

import httpx
from dataclasses import dataclass

from config import settings
from services.imagen import FIRMA_JPEG, FIRMA_PNG

# mime -> (tipo de mensaje de WhatsApp, extensiones válidas)
TIPOS: dict[str, tuple[str, tuple[str, ...]]] = {
    "image/jpeg": ("image", (".jpg", ".jpeg")),
    "image/png": ("image", (".png",)),
    "application/pdf": ("document", (".pdf",)),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
        "document",
        (".docx",),
    ),
}

MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Texto que se guarda en messages.content cuando no hay leyenda.
ETIQUETA_IMAGEN = "[Imagen]"


class AdjuntoInvalido(Exception):
    """El adjunto no se acepta. `status_code` es el HTTP que corresponde."""

    def __init__(self, status_code: int, mensaje: str):
        super().__init__(mensaje)
        self.status_code = status_code
        self.mensaje = mensaje


@dataclass(frozen=True)
class InfoAdjunto:
    mime: str
    tipo_wa: str  # "image" | "document"
    nombre: str


def tope_absoluto() -> int:
    """Lo máximo que vale la pena leer de una subida antes de validar."""
    return max(settings.WA_IMAGEN_MAX_BYTES, settings.WA_DOCUMENTO_MAX_BYTES)


def limite_para(mime: str) -> int:
    tipo_wa = TIPOS[mime][0]
    return settings.WA_IMAGEN_MAX_BYTES if tipo_wa == "image" else settings.WA_DOCUMENTO_MAX_BYTES


def nombre_seguro(nombre: str | None) -> str:
    """Solo el nombre de archivo (sin ruta ni caracteres de control), acotado."""
    base = os.path.basename((nombre or "").replace("\\", "/"))
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip()
    if len(base) > 100:
        raiz, ext = os.path.splitext(base)
        base = raiz[: 100 - len(ext)] + ext
    return base


def _es_docx(contenido: bytes) -> bool:
    if not contenido.startswith(b"PK\x03\x04"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(contenido)) as z:
            nombres = z.namelist()
    except zipfile.BadZipFile:
        return False
    return "[Content_Types].xml" in nombres and any(n.startswith("word/") for n in nombres)


def _mime_por_firma(contenido: bytes) -> str | None:
    if contenido.startswith(FIRMA_PNG):
        return "image/png"
    if contenido.startswith(FIRMA_JPEG):
        return "image/jpeg"
    # La especificación de PDF admite basura antes de la cabecera, dentro
    # del primer KB.
    if b"%PDF-" in contenido[:1024]:
        return "application/pdf"
    if _es_docx(contenido):
        return MIME_DOCX
    return None


def validar_adjunto(contenido: bytes, nombre: str | None) -> InfoAdjunto:
    """
      422 -> vacío o sin nombre
      415 -> no es JPG/PNG/PDF/DOCX, o la extensión no coincide con el contenido
      413 -> pesa más que el tope de su tipo
    """
    if not contenido:
        raise AdjuntoInvalido(422, "El archivo está vacío.")

    limpio = nombre_seguro(nombre)
    if not limpio:
        raise AdjuntoInvalido(422, "El archivo no tiene nombre.")

    mime = _mime_por_firma(contenido)
    if mime is None:
        raise AdjuntoInvalido(
            415, "Tipo de archivo no permitido. Solo JPG, PNG, PDF y DOCX."
        )

    tipo_wa, extensiones = TIPOS[mime]
    if os.path.splitext(limpio)[1].lower() not in extensiones:
        raise AdjuntoInvalido(
            415, "La extensión del archivo no coincide con su contenido."
        )

    tope = limite_para(mime)
    if len(contenido) > tope:
        raise AdjuntoInvalido(
            413, f"El archivo pesa más de {tope // (1024 * 1024)} MB."
        )

    return InfoAdjunto(mime=mime, tipo_wa=tipo_wa, nombre=limpio)


def texto_mensaje(info: InfoAdjunto, leyenda: str | None) -> str:
    """messages.content: la leyenda si hay; si no, una etiqueta legible."""
    if leyenda and leyenda.strip():
        return leyenda.strip()
    if info.tipo_wa == "image":
        return ETIQUETA_IMAGEN
    return f"[Documento: {info.nombre}]"


def cabeceras_descarga(nombre: str, *, privado: bool) -> dict[str, str]:
    """Cabeceras de respuesta al servir un adjunto (nombre codificado RFC 5987)."""
    return {
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(nombre)}",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=3600" if privado else "no-store",
    }


def host_permitido() -> str:
    """Único host del que se descargan archivos entrantes: el de NeuroAPI."""
    return (urlparse(settings.NEUROAPI_API_BASE_URL).hostname or "").lower()


def nombre_desde_url(url: str) -> str:
    return nombre_seguro(os.path.basename(urlparse(url).path))


async def descargar_media(url: str) -> bytes:
    """
    Baja el archivo que NeuroAPI ya resolvió y dejó en una URL HTTPS directa.

    La URL llega en un webhook, así que se trata como no confiable (SSRF):
    solo https, solo el host de NeuroAPI, sin seguir redirecciones, y se corta
    la lectura al pasar el tope absoluto sin cargar todo en memoria.
    """
    partes = urlparse(url)
    if partes.scheme != "https" or (partes.hostname or "").lower() != host_permitido():
        raise AdjuntoInvalido(422, "La URL del archivo no es de un origen permitido.")

    tope = tope_absoluto()
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as cliente:
            async with cliente.stream("GET", url) as r:
                if r.status_code != 200:
                    raise AdjuntoInvalido(502, f"No se pudo descargar el archivo ({r.status_code}).")
                trozos: list[bytes] = []
                total = 0
                async for trozo in r.aiter_bytes():
                    total += len(trozo)
                    if total > tope:
                        raise AdjuntoInvalido(413, f"El archivo pesa más de {tope // (1024 * 1024)} MB.")
                    trozos.append(trozo)
    except httpx.HTTPError as e:
        raise AdjuntoInvalido(502, f"No se pudo descargar el archivo: {e}") from e
    return b"".join(trozos)
