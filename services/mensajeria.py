"""
Adaptador de envío por proveedor de mensajería.

Quien manda un mensaje (el agente en services/agente/envio.py, una persona
desde el portal en services/conversaciones.py) le pasa el canal, las
credenciales de `get_channel_credentials` y el destinatario, y recibe la
petición ya armada para ese proveedor: URL, cabeceras y cuerpo. Así la
diferencia entre Graph directo y NeuroAPI vive en un solo sitio.

Hoy cubre Instagram por NeuroAPI. WhatsApp (Graph o NeuroAPI) y Facebook
siguen armando su petición donde estaban; moverlos aquí es una limpieza
pendiente, no un cambio de comportamiento.

Igual que en envio.py, la API key solo se usa al armar la cabecera: no entra
en el cuerpo ni en ningún dict que se registre.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from config import settings
from services import meta

TIMEOUT = httpx.Timeout(20.0)

# Etiqueta de Meta para respuestas de una persona: amplía la ventana de
# Instagram de 24 h a 7 días. No vale para las respuestas del agente.
ETIQUETA_HUMANO = "HUMAN_AGENT"


class SinCredenciales(Exception):
    """Las credenciales del canal no alcanzan para armar el envío."""


@dataclass(frozen=True)
class Peticion:
    url: str
    cabeceras: dict[str, str]
    cuerpo: dict[str, Any]


def es_instagram_neuroapi(canal: str, cred: Mapping[str, Any]) -> bool:
    """Las cuentas de Instagram vinculadas por NeuroAPI no salen por Graph."""
    return canal == "instagram" and cred.get("bsp_provider") == "neuroapi"


def peticion_instagram(
    cred: Mapping[str, Any], recipient_id: str, texto: str, *, humano: bool = False
) -> Peticion:
    """
    `POST /neuroapi/instagram/send`. `recipient_id` es el IGSID del cliente
    (el `sender.id` del mensaje que mandó). `humano` agrega la etiqueta
    HUMAN_AGENT.
    """
    if not cred.get("access_token"):
        raise SinCredenciales("Instagram por NeuroAPI sin API key guardada")
    if not recipient_id:
        raise SinCredenciales("Instagram por NeuroAPI sin destinatario")
    cuerpo: dict[str, Any] = {
        "recipient_id": recipient_id,
        "type": "text",
        "text": {"body": texto},
    }
    if humano:
        cuerpo["tag"] = ETIQUETA_HUMANO
    return Peticion(
        url=f"{settings.NEUROAPI_API_BASE_URL.rstrip('/')}/neuroapi/instagram/send",
        cabeceras={"x-api-key": str(cred["access_token"])},
        cuerpo=cuerpo,
    )


async def enviar(peticion: Peticion) -> dict[str, Any]:
    """
    Manda la petición y devuelve el JSON de respuesta. Un rechazo se levanta
    como `meta.MetaError` (NeuroAPI reenvía el error de Meta), para que el
    portal lo traduzca con `meta.a_http` igual que un envío por Graph.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
            respuesta = await cliente.post(
                peticion.url, json=peticion.cuerpo, headers=peticion.cabeceras
            )
    except httpx.HTTPError as e:
        raise meta.MetaError(None, f"No se pudo contactar al canal: {e}") from e

    try:
        datos = respuesta.json()
    except ValueError:
        datos = {}
    if not isinstance(datos, dict):
        datos = {}

    if respuesta.status_code >= 400 or datos.get("success") is False or "error" in datos:
        raise _error(datos, respuesta.status_code)
    return datos


def _error(datos: dict[str, Any], status_code: int) -> meta.MetaError:
    """El error de NeuroAPI, que puede traer el de Meta anidado o solo un texto."""
    error = datos.get("error")
    if isinstance(error, dict):
        codigo = error.get("code")
        codigo = int(codigo) if isinstance(codigo, int) or str(codigo).isdigit() else None
        mensaje = meta.DIAGNOSTICOS.get(codigo or -1) or str(error.get("message") or "Error del canal")
        return meta.MetaError(codigo, mensaje, error.get("error_subcode"))
    texto = error if isinstance(error, str) else datos.get("message")
    return meta.MetaError(None, str(texto or f"El canal respondió {status_code}"))
