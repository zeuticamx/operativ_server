"""
Proxy de mensajes entrantes de WhatsApp (NeuroAPI) hacia n8n o hacia el
agente en código, según `AGENTE_EN_CODIGO`.

NeuroAPI manda a la misma `webhook_url` de la Connect Session tanto el alta
de la cuenta como los mensajes posteriores. El backend valida la firma una
sola vez (routers/canales_neuroapi_webhook.py), identifica de qué tenant es
el número receptor y reenvía el cuerpo *intacto* al flujo que le toca. El
destino recibe exactamente el mismo JSON que mandaría NeuroAPI, más cabeceras
con lo que el backend ya resolvió, y se autentica por X-Internal-Token en vez
de recalcular el HMAC — n8n en `/webhook/entrada-canal-universal`, el agente
en código en `/api/canales/entrada` (routers/agente_webhook.py), que valida
esa misma cabecera.

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
    Tenant dueño de la línea (spec 005a RF-16, T6): el número vinculado
    (activo o pausado) y, si se desvinculó, el último dueño (sus mensajes van
    a las ocultas de ese negocio, sin respuesta). Solo atribuye el mensaje:
    si el agente contesta o no lo decide el agente (T7).
    """
    fila = await fetch_one(
        "SELECT tenant_id FROM resuelve_numero($1) LIMIT 1",
        phone_number_id,
    )
    return fila["tenant_id"] if fila else None


def destino_para(tenant_id: UUID) -> str:
    """
    URL a la que se reenvían los mensajes de este tenant.

    Con `AGENTE_EN_CODIGO` encendida va al propio backend
    (routers/agente_webhook.py), que es quien a partir de ahí atiende el
    mensaje; apagada, sigue yendo al webhook `entrada-canal-universal` de
    n8n, que es el destino real mientras dure la migración. La decisión es
    global y no por tenant: `AGENTE_EN_CODIGO` es una bandera de proceso, no
    de negocio — ver el docstring de `services/agente/__init__.py`.

    `tenant_id` no entra en la decisión hoy; queda en la firma para el día
    que haga falta enrutar por tenant (otra cola, otro agente) sin tocar el
    router ni la validación de la firma.
    """
    if settings.AGENTE_EN_CODIGO:
        return f"{settings.BASE_URL_BACKEND}/api/canales/entrada"
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
