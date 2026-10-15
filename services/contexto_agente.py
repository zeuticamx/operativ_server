"""
Eventos de contexto del agente por avisos de cita (spec 004, RF-13 a RF-15).

Tabla propia `contexto_agente_eventos`: n8n la lee por `conversation_id` para
que la siguiente respuesta del agente sepa que la cita se canceló o que se
recordó. El backend no escribe en tablas de n8n (solo lee `conversations`).

Regla central (RF-14): un evento SOLO se crea tras un aviso cerrado como
`enviado`. Nada de aquí mira `tenant_agent_config` ni si el agente está
encendido (CL-12).
"""

import logging
from datetime import datetime
from uuid import UUID

from session import execute, fetch_one

log = logging.getLogger("operativai.contexto_agente")

EVENTO_CANCELACION = "cita_cancelada_por_negocio"
EVENTO_RECORDATORIO = "recordatorio_cita_enviado"

_EVENTO_POR_TIPO_AVISO = {
    "cancelacion": EVENTO_CANCELACION,
    "recordatorio": EVENTO_RECORDATORIO,
}


def evento_para_aviso(tipo_aviso: str) -> str:
    """Etiqueta del evento (P13) según el tipo de aviso."""
    try:
        return _EVENTO_POR_TIPO_AVISO[tipo_aviso]
    except KeyError as exc:
        raise ValueError(f"tipo de aviso desconocido: {tipo_aviso!r}") from exc


async def conversacion_whatsapp_del_cliente(tenant_id: UUID, user_id: UUID | None) -> UUID | None:
    """
    P14: la conversación de WhatsApp más reciente por actividad del cliente con
    el negocio. None si el cliente no tiene ninguna (RF-15: se envía igual,
    con `sin_conversacion`). Solo lectura de `conversations`.
    """
    if user_id is None:
        return None
    fila = await fetch_one(
        """
        SELECT id
        FROM conversations
        WHERE tenant_id = $1 AND user_id = $2 AND channel_type = 'whatsapp'
        ORDER BY last_message_at DESC NULLS LAST
        LIMIT 1
        """,
        tenant_id,
        user_id,
    )
    return fila["id"] if fila is not None else None


async def registrar_evento(
    *,
    tenant_id: UUID,
    conversation_id: UUID | None,
    reserva_id: UUID,
    aviso_id: UUID,
    tipo_aviso: str,
    hora_cita: datetime,
    servicio: str | None,
) -> bool:
    """
    Inserta el evento de un aviso `enviado`. Sin conversación no hay nada que
    registrar (False). Idempotente por `aviso_id` (UNIQUE): un aviso genera como
    máximo un evento. El llamador solo debe invocarlo tras cerrar `enviado`.
    """
    if conversation_id is None:
        return False
    await execute(
        """
        INSERT INTO contexto_agente_eventos
            (tenant_id, conversation_id, reserva_id, aviso_id, tipo, hora_cita, servicio)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (aviso_id) DO NOTHING
        """,
        tenant_id,
        conversation_id,
        reserva_id,
        aviso_id,
        evento_para_aviso(tipo_aviso),
        hora_cita,
        (servicio or "").strip() or None,
    )
    return True
