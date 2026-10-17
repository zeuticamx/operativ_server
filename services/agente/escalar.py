"""
Herramienta `escalar_humano`: pasar la conversación a una persona.

Port del workflow `escalar-humano`. Marca la conversación como `transferred`
y avisa al portal para que alguien la tome; a partir de ahí el agente deja de
contestar (lo corta `ingreso._conversacion_transferida`).

Dos cosas que se conservan del original:

  - El aviso al portal va DESPUÉS del UPDATE y su fallo no revierte nada. Si
    la conversación quedó transferida pero el equipo no se enteró, al agente
    se le devuelve un texto distinto para que no le prometa al cliente una
    atención inmediata que nadie va a ver.
  - `escalar_humano` no consume crédito. Sin saldo, quien pide una persona
    tiene que poder llegar a ella (ver services/creditos.py).

El aviso ya no sale por HTTP contra este mismo proceso: se llama directo a la
función del router.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from session import execute

log = logging.getLogger("operativai.agente.escalar")

OK = "Conversación transferida a un humano. El equipo ha sido notificado."

# El UPDATE se hizo pero el portal no se enteró. El agente necesita saberlo
# para no prometer una atención que nadie tiene en pantalla todavía.
SIN_AVISO = (
    "La conversación quedó marcada como transferida a un humano, pero NO se pudo "
    "avisar al equipo en el portal. Dile al cliente que una persona lo atenderá en "
    "cuanto revise las conversaciones."
)


async def escalar(
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    canal: str,
    motivo: str | None,
    area: str | None,
    cliente_nombre: str | None,
    cliente_telefono: str | None,
) -> str:
    """
    Transfiere y devuelve el texto que lee el agente.

    `ack_pendiente` queda en true para que el siguiente mensaje del cliente
    reciba un acuse una sola vez (ver ingreso._conversacion_transferida).
    """
    await execute(
        """
        UPDATE conversations
           SET status = 'transferred',
               metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                   'motivo_escalamiento', $2::text,
                   'escalado_en', NOW()::text,
                   'ack_pendiente', true
               )
         WHERE id = $1
        """,
        conversation_id,
        motivo,
    )

    # Import diferido por la misma razón que en ingreso.py: esto es lógica de
    # negocio que hoy vive en un router, y los servicios no importan routers.
    from routers.eventos import conversacion_transferida
    from schemas import ConversacionTransferidaIn

    try:
        await conversacion_transferida(
            ConversacionTransferidaIn(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                canal=canal or "desconocido",
                motivo=motivo,
                cliente_nombre=cliente_nombre,
                cliente_telefono=cliente_telefono,
                area=area,
            )
        )
    except Exception:
        # La transferencia ya está hecha y es lo que importa: el bot deja de
        # contestar igual. Lo que falló es el aviso.
        log.exception(
            "Conversación %s transferida pero sin aviso al portal", conversation_id
        )
        return SIN_AVISO

    return OK


def definicion() -> dict[str, Any]:
    """
    El esquema que ve el modelo.

    Solo pide `motivo` y `area`: el tenant, la conversación y los datos del
    cliente los pone el servidor desde el contexto. Si el modelo pudiera
    elegir `conversation_id`, podría transferir la conversación de otro
    cliente.
    """
    return {
        "name": "escalar_humano",
        "description": (
            "Transfiere la conversación a una persona del equipo. Úsala cuando el "
            "cliente lo pida explícitamente, cuando haya una queja seria, o cuando la "
            "solicitud esté fuera de lo que puedes resolver. Tras usarla dejas de "
            "atender esta conversación."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {
                    "type": "string",
                    "description": "Por qué se transfiere, en una frase.",
                },
                "area": {
                    "type": "string",
                    "enum": ["ventas", "agenda", "otro"],
                    "description": (
                        "A quién le corresponde: 'ventas' (cotizaciones, precios, "
                        "compras), 'agenda' (citas, reservas, horarios) u 'otro'. "
                        "Si dudas, usa 'otro'."
                    ),
                },
            },
            "required": ["motivo", "area"],
        },
    }
