"""
NeuroAPI Connect Sessions: alta de WhatsApp Business vía Embedded Signup.

Sin SDK, con httpx directo, igual que el resto de integraciones de este
proyecto (Meta, Stripe, Kontesta). Esto es distinto de `services/whatsapp.py`:
`NeuroApiProvider` de ahí es el BSP para *enviar/recibir mensajes* una vez la
cuenta ya está conectada; esto es *el alta de la cuenta*, mismo vendor y mismo
esquema de firma pero otro endpoint y otro secreto de webhook.

La `webhook_url` de la sesión recibe también los mensajes entrantes una vez
vinculada la cuenta; el router los reenvía a n8n (services/entrada_mensajes.py)
con la firma ya validada por `verificar_webhook` de este módulo.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException, status

from config import settings

log = logging.getLogger("operativai.canales.neuroapi_connect")

TIMEOUT = httpx.Timeout(15.0)


async def crear_connect_session(tenant_id: UUID) -> dict[str, Any]:
    """
    Crea la sesión en NeuroAPI y devuelve su JSON crudo.

    Lanza 502 si NeuroAPI rechaza o no contesta: quien llama no tiene nada
    que limpiar todavía (no se guardó ninguna fila hasta tener la sesión).
    """
    payload = {
        "service_type": "whatsapp_cloud_api",
        "return_url": f"{settings.BASE_URL_FRONTEND}/canales?whatsapp_neuroapi=retorno",
        "webhook_url": f"{settings.BASE_URL_BACKEND}/api/canales/whatsapp/neuroapi/webhook",
        "webhook_secret": settings.NEUROAPI_CONNECT_WEBHOOK_SECRET,
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
            respuesta = await cliente.post(
                f"{settings.NEUROAPI_API_BASE_URL}/neuroapi/connect/sessions",
                headers={"x-api-key": settings.NEUROAPI_API_KEY},
                json=payload,
            )
        respuesta.raise_for_status()
        # NeuroAPI responde {"success": true, "data": {session_id, url, ...}}.
        cuerpo = respuesta.json()
        return cuerpo.get("data", cuerpo)
    except httpx.HTTPError as e:
        cuerpo = e.response.text if isinstance(e, httpx.HTTPStatusError) else ""
        log.error(
            "NeuroAPI rechazó la creación de la Connect Session (tenant=%s): %s | body=%s",
            tenant_id, e, cuerpo,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo iniciar la vinculación con WhatsApp. Inténtalo de nuevo.",
        )


def verificar_webhook(cuerpo_crudo: bytes, headers: dict[str, str]) -> bool:
    """
    Firma esperada: cabecera `X-Hub-Signature-256: sha256=<hex>`, HMAC sobre
    el cuerpo crudo del POST (antes de json.loads) con
    NEUROAPI_CONNECT_WEBHOOK_SECRET. Mismo esquema que
    NeuroApiProvider.verificar_webhook en services/whatsapp.py, pero con el
    secreto dedicado a Connect Sessions.
    """
    if not settings.NEUROAPI_CONNECT_WEBHOOK_SECRET:
        log.warning("NEUROAPI_CONNECT_WEBHOOK_SECRET sin configurar: webhook rechazado")
        return False

    firma = headers.get("x-hub-signature-256") or headers.get("X-Hub-Signature-256")
    if not firma or not firma.startswith("sha256="):
        return False

    recibido = firma[len("sha256=") :]
    esperado = hmac.new(
        settings.NEUROAPI_CONNECT_WEBHOOK_SECRET.encode(), cuerpo_crudo, hashlib.sha256
    ).hexdigest()

    # compare_digest y no ==: comparación en tiempo constante.
    return hmac.compare_digest(esperado, recibido)
