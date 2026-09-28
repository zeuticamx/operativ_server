"""
Webhook de NeuroAPI Connect Sessions.

Vive aparte de routers/canales.py por el mismo motivo que el de Stripe vive
aparte de pagos.py: es el único endpoint del módulo de canales sin JWT, se
autentica por el HMAC de la cabecera `X-Hub-Signature-256`.
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Request, status

from config import settings
from services import neuroapi_connect
from session import execute, fetch_one

router = APIRouter(prefix="/canales/whatsapp/neuroapi", tags=["canales"])

log = logging.getLogger("operativai.canales.neuroapi_connect")


@router.post("/webhook", status_code=200)
async def webhook(request: Request) -> dict[str, bool]:
    """
    Recibe el resultado de una Connect Session y activa el canal si terminó
    en éxito.

    Devuelve 200 salvo firma inválida o secreto sin configurar: un evento de
    un session_id que no es nuestro, o de un estado que no nos interesa, no
    se arregla con un reintento.
    """
    if not settings.NEUROAPI_CONNECT_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El webhook de NeuroAPI Connect no está configurado",
        )

    # El cuerpo crudo, no el JSON ya parseado: el HMAC se calcula sobre los
    # bytes tal cual llegaron.
    cuerpo_crudo = await request.body()

    if not neuroapi_connect.verificar_webhook(cuerpo_crudo, dict(request.headers)):
        log.warning("Evento de NeuroAPI Connect con firma inválida")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firma inválida",
        )

    try:
        evento = json.loads(cuerpo_crudo)
    except json.JSONDecodeError:
        log.warning("Evento de NeuroAPI Connect con cuerpo ilegible")
        return {"recibido": True}

    session_id = evento.get("session_id") or evento.get("id")
    estado = str(evento.get("status", ""))
    if not session_id:
        return {"recibido": True}

    fila = await fetch_one(
        """
        UPDATE neuroapi_connect_sessions
           SET status = $2, detalle = $3, updated_at = NOW()
         WHERE session_id = $1
        RETURNING tenant_id
        """,
        session_id,
        estado or "pendiente",
        evento.get("detail") or evento.get("error"),
    )
    if fila is None:
        log.info("Evento de NeuroAPI Connect para una sesión que no es nuestra: %s", session_id)
        return {"recibido": True}

    if estado == "completado":
        phone_number_id = evento.get("phone_number_id")
        if phone_number_id:
            await fetch_one(
                "SELECT set_channel_credentials($1, 'whatsapp', NULL, $2, NULL, NULL)",
                fila["tenant_id"],
                phone_number_id,
            )
            await execute(
                """
                INSERT INTO tenant_channels (tenant_id, channel_type)
                VALUES ($1, 'whatsapp')
                ON CONFLICT (tenant_id, channel_type) DO UPDATE SET is_active = true
                """,
                fila["tenant_id"],
            )
        else:
            log.warning(
                "NeuroAPI Connect marcó la sesión %s como completada sin phone_number_id",
                session_id,
            )

    return {"recibido": True}
