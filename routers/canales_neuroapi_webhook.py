"""
Webhook de NeuroAPI Connect Sessions.

Vive aparte de routers/canales.py por el mismo motivo que el de Stripe vive
aparte de pagos.py: es el único endpoint del módulo de canales sin JWT, se
autentica por el HMAC de la cabecera `X-Hub-Signature-256`.

Al terminar el Embedded Signup, NeuroAPI dispara el evento
`whatsapp.connected` a la `webhook_url` de la sesión. El formato del sobre no
está confirmado, así que el parser acepta tres variantes:

  {"event": "whatsapp.connected", "data": {"session_id": ..., ...}}
  {"event": "whatsapp.connected", "session_id": ..., "phone_number_id": ...}
  {"entry": [{"changes": [{"value": {...}}]}]}          (sobre estándar de Meta)
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from config import settings
from services import neuroapi_connect
from session import execute, fetch_one

router = APIRouter(prefix="/canales/whatsapp/neuroapi", tags=["canales"])

log = logging.getLogger("operativai.canales.neuroapi_connect")

EVENTOS_CONECTADO = {"whatsapp.connected"}

# La tabla solo admite pendiente/completado/fallido (CHECK en
# sql/24_neuroapi_connect_sessions.sql): cualquier otro valor hacía fallar el
# UPDATE con 500. Se traduce el vocabulario de NeuroAPI a esos tres.
ESTADOS: dict[str, str] = {
    "completado": "completado",
    "completed": "completado",
    "complete": "completado",
    "connected": "completado",
    "success": "completado",
    "succeeded": "completado",
    "fallido": "fallido",
    "failed": "fallido",
    "error": "fallido",
    "cancelled": "fallido",
    "canceled": "fallido",
    "expired": "fallido",
    "pendiente": "pendiente",
    "pending": "pendiente",
}

# Nunca van en claro al log.
_CABECERAS_SENSIBLES = {"authorization", "cookie", "x-api-key", "x-internal-token"}
_FRAGMENTOS_SENSIBLES = ("token", "secret", "password", "api_key", "apikey")


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

    # TEMPORAL: volcar el evento completo hasta confirmar el formato real de
    # NeuroAPI. Antes de la firma, para ver también los que se rechazan.
    _registrar_evento_crudo(dict(request.headers), cuerpo_crudo)

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

    if not isinstance(evento, dict):
        log.warning("Evento de NeuroAPI Connect que no es un objeto JSON")
        return {"recibido": True}

    fuentes = _fuentes(evento)

    def campo(*nombres: str) -> Any:
        """Primer valor no vacío, buscando cada nombre en todas las fuentes en orden."""
        for nombre in nombres:
            for fuente in fuentes:
                valor = fuente.get(nombre)
                if valor not in (None, ""):
                    return valor
        return None

    tipo_evento = str(campo("event", "type") or "").strip().lower()
    session_id = campo("session_id") or evento.get("id")
    phone_number_id = campo("phone_number_id", "otp_phone_number_id")
    phone_number = campo("phone_number", "display_phone_number")
    waba_id = campo("waba_id")

    if tipo_evento in EVENTOS_CONECTADO:
        estado_crudo = tipo_evento
        estado: str | None = "completado"
    else:
        estado_crudo = str(campo("status", "state") or "").strip()
        if not estado_crudo:
            # Formato "connect_session.completed": el estado va tras el punto.
            estado_crudo = tipo_evento.rsplit(".", 1)[-1]
        estado = ESTADOS.get(estado_crudo.lower())

    log.warning(
        "NeuroAPI Connect: evento=%r estado=%r session_id=%s phone_number_id=%s "
        "phone_number=%s waba_id=%s",
        tipo_evento, estado_crudo, session_id, phone_number_id, phone_number, waba_id,
    )

    if not session_id:
        log.warning("Evento de NeuroAPI Connect sin session_id; %s", _estructura(evento))
        return {"recibido": True}

    if estado is None:
        log.warning(
            "NeuroAPI Connect mandó un estado desconocido %r para la sesión %s; %s",
            estado_crudo, session_id, _estructura(evento),
        )
        estado = "pendiente"

    fila = await fetch_one(
        """
        UPDATE neuroapi_connect_sessions
           SET status = $2, detalle = $3, updated_at = NOW()
         WHERE session_id = $1
        RETURNING tenant_id
        """,
        str(session_id),
        estado,
        campo("detail", "error")
        or (estado_crudo if estado_crudo.lower() not in ESTADOS else None),
    )
    if fila is None:
        log.warning("Evento de NeuroAPI Connect para una sesión que no es nuestra: %s", session_id)
        return {"recibido": True}

    if estado == "completado":
        if phone_number_id:
            await _activar_whatsapp(fila["tenant_id"], str(phone_number_id))
            log.warning(
                "WhatsApp activado vía NeuroAPI Connect: tenant=%s sesión=%s phone_number_id=%s",
                fila["tenant_id"], session_id, phone_number_id,
            )
        else:
            log.warning(
                "NeuroAPI Connect marcó la sesión %s como completada sin phone_number_id; %s",
                session_id, _estructura(evento),
            )

    return {"recibido": True}


async def _activar_whatsapp(tenant_id: Any, phone_number_id: str) -> None:
    await fetch_one(
        "SELECT set_channel_credentials($1, 'whatsapp', NULL, $2, NULL, NULL)",
        tenant_id,
        phone_number_id,
    )
    await execute(
        """
        INSERT INTO tenant_channels (tenant_id, channel_type)
        VALUES ($1, 'whatsapp')
        ON CONFLICT (tenant_id, channel_type) DO UPDATE SET is_active = true
        """,
        tenant_id,
    )


def _fuentes(evento: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Dónde buscar cada campo, en orden de prioridad: la raíz, `data`, y cada
    `entry[].changes[].value` del sobre de Meta (con su `data` si lo trae).
    """
    fuentes: list[dict[str, Any]] = [evento]
    if isinstance(evento.get("data"), dict):
        fuentes.append(evento["data"])
    for entrada in evento.get("entry") or []:
        if not isinstance(entrada, dict):
            continue
        for cambio in entrada.get("changes") or []:
            valor = cambio.get("value") if isinstance(cambio, dict) else None
            if isinstance(valor, dict):
                fuentes.append(valor)
                if isinstance(valor.get("data"), dict):
                    fuentes.append(valor["data"])
    return fuentes


def _ocultar(valor: Any) -> Any:
    if isinstance(valor, dict):
        return {
            k: "***" if any(f in k.lower() for f in _FRAGMENTOS_SENSIBLES) else _ocultar(v)
            for k, v in valor.items()
        }
    if isinstance(valor, list):
        return [_ocultar(v) for v in valor]
    return valor


def _registrar_evento_crudo(cabeceras: dict[str, str], cuerpo_crudo: bytes) -> None:
    cabeceras_log = {
        k: "***" if k.lower() in _CABECERAS_SENSIBLES else v for k, v in cabeceras.items()
    }
    try:
        cuerpo_log = json.dumps(_ocultar(json.loads(cuerpo_crudo)), indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, UnicodeDecodeError):
        cuerpo_log = f"<no JSON, {len(cuerpo_crudo)} bytes>"
    log.warning(
        "NeuroAPI Connect webhook recibido\ncabeceras=%s\ncuerpo=%s",
        json.dumps(cabeceras_log, indent=2, ensure_ascii=False),
        cuerpo_log,
    )


def _estructura(evento: dict[str, Any]) -> str:
    """Nombres de campos (sin valores) para diagnosticar el formato."""
    partes = []
    for clave in sorted(evento):
        valor = evento[clave]
        if isinstance(valor, dict):
            partes.append(f"{clave}{{{', '.join(sorted(valor))}}}")
        else:
            partes.append(clave)
    return "campos=" + ", ".join(partes)
