"""
Proxy de mensajes entrantes de WhatsApp (NeuroAPI) hacia n8n.

NeuroAPI manda a la misma `webhook_url` de la Connect Session tanto el alta
de la cuenta como los mensajes posteriores. El backend valida la firma una
sola vez (routers/canales_neuroapi_webhook.py), identifica de qué tenant es
el número receptor y reenvía el cuerpo *intacto* al flujo que le toca. n8n
recibe exactamente el mismo JSON que mandaría NeuroAPI, más cabeceras con lo
que el backend ya resolvió, y se autentica por X-Internal-Token en vez de
recalcular el HMAC.

Sin SDK, con httpx directo, igual que el resto de integraciones.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import httpx

from config import settings
from session import fetch_one

log = logging.getLogger("operativai.canales.entrada_mensajes")

# Corto a propósito: el webhook de n8n debe responder de inmediato
# ("Respond: Immediately"), no al terminar el flujo del agente. Si tarda más,
# NeuroAPI también corta y reintenta.
TIMEOUT = httpx.Timeout(10.0)

NOMBRES_PHONE_NUMBER_ID = ("phone_number_id", "phoneNumberId", "to_phone_number_id")


class ReenvioError(Exception):
    """El destino no recibió el mensaje; conviene que NeuroAPI reintente."""


def extraer_phone_number_id(evento: dict[str, Any]) -> str | None:
    """
    phone_number_id del número *receptor* (el del negocio), que es lo que
    identifica al tenant. Busca primero en `metadata`, que en el sobre de
    Meta (`entry[].changes[].value.metadata`) es el dato exacto, y luego en
    la raíz y en `data` por si NeuroAPI lo manda en su formato propio.
    """
    candidatos: list[dict[str, Any]] = []

    for entrada in evento.get("entry") or []:
        if not isinstance(entrada, dict):
            continue
        for cambio in entrada.get("changes") or []:
            valor = cambio.get("value") if isinstance(cambio, dict) else None
            if isinstance(valor, dict):
                if isinstance(valor.get("metadata"), dict):
                    candidatos.append(valor["metadata"])
                candidatos.append(valor)

    datos = evento.get("data")
    if isinstance(datos, dict):
        if isinstance(datos.get("metadata"), dict):
            candidatos.append(datos["metadata"])
        candidatos.append(datos)
    if isinstance(evento.get("metadata"), dict):
        candidatos.append(evento["metadata"])
    candidatos.append(evento)

    for fuente in candidatos:
        for nombre in NOMBRES_PHONE_NUMBER_ID:
            valor = fuente.get(nombre)
            if valor not in (None, ""):
                return str(valor)
    return None


async def tenant_por_numero(phone_number_id: str) -> UUID | None:
    """
    Tenant dueño de la línea. Solo atribuye el mensaje: si el agente contesta
    o no lo sigue decidiendo n8n, así que aquí no se mira tenant_channels.
    """
    fila = await fetch_one(
        """
        SELECT tenant_id
          FROM channel_credentials
         WHERE channel_type = 'whatsapp'
           AND phone_number_id = $1
           AND is_active = true
         LIMIT 1
        """,
        phone_number_id,
    )
    return fila["tenant_id"] if fila else None


def destino_para(tenant_id: UUID) -> str:
    """
    URL a la que se reenvían los mensajes de este tenant.

    Hoy todos van al webhook `entrada-canal-universal` de n8n. Este es el
    punto donde se enrutaría por tenant (otra cola, otro agente) sin tocar el
    router ni la validación de la firma.
    """
    return settings.N8N_WEBHOOK_ENTRADA_URL


async def reenviar(cuerpo_crudo: bytes, tenant_id: UUID, destino: str) -> None:
    """
    POST del cuerpo tal cual llegó de NeuroAPI. Lanza ReenvioError si el
    destino no contesta o responde algo que no es 2xx (incluido el 404 de un
    workflow de n8n desactivado): en todos esos casos un reintento puede
    funcionar, así que el router responde 502 y NeuroAPI lo reintenta.
    """
    cabeceras = {
        "Content-Type": "application/json",
        "X-Tenant-Id": str(tenant_id),
        "X-Canal": "whatsapp",
        "X-Proveedor": "neuroapi",
    }
    if settings.N8N_INTERNAL_TOKEN:
        cabeceras["X-Internal-Token"] = settings.N8N_INTERNAL_TOKEN

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
            respuesta = await cliente.post(destino, content=cuerpo_crudo, headers=cabeceras)
    except httpx.HTTPError as e:
        raise ReenvioError(f"No se pudo contactar al destino: {e}") from e

    if respuesta.status_code >= 300:
        raise ReenvioError(
            f"El destino respondió {respuesta.status_code}: {respuesta.text[:300]}"
        )
