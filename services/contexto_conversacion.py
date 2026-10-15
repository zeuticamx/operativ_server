"""
Cortes de contexto del agente por conversación (spec 003).

"Limpiar el contexto" no borra mensajes: se registra un corte y n8n lee solo
los mensajes posteriores al último. Aquí viven la regla pura de quién puede
hacerlo y las consultas a `conversacion_contexto_cortes`. Todas filtran por
`tenant_id`; si la conversación no es del negocio, `conversacion_de_cliente`
devuelve None y el router responde 404.
"""

from typing import Optional
from uuid import UUID

import asyncpg

from services.historial_citas import es_canal_de_cliente
from session import fetch_one


def puede_limpiar_contexto(
    es_owner_propio: bool,
    flag: bool,
    canal: str | None,
    plan_incluye_agente: bool,
) -> bool:
    """True solo si se cumplen las cuatro condiciones (RF10). Pura: sin reloj ni BD."""
    return bool(es_owner_propio) and bool(flag) and bool(plan_incluye_agente) and es_canal_de_cliente(canal)


async def conversacion_de_cliente(
    tenant_id: UUID, conversacion_id: UUID
) -> Optional[asyncpg.Record]:
    """Conversación del negocio de un canal de cliente; None si no existe, es de otro negocio o es web."""
    fila = await fetch_one(
        """
        SELECT id, tenant_id, channel_type, status, asignado_a, asignado_en
        FROM conversations
        WHERE id = $1 AND tenant_id = $2
        """,
        conversacion_id,
        tenant_id,
    )
    if fila is None or not es_canal_de_cliente(fila["channel_type"]):
        return None
    return fila


async def registrar_corte(
    tenant_id: UUID, conversacion_id: UUID, realizado_por: UUID
) -> asyncpg.Record:
    """Inserta un corte nuevo (nunca actualiza) y devuelve la fila con `creado_en` de la BD."""
    fila = await fetch_one(
        """
        INSERT INTO conversacion_contexto_cortes (tenant_id, conversation_id, realizado_por)
        VALUES ($1, $2, $3)
        RETURNING id, tenant_id, conversation_id, creado_en, realizado_por
        """,
        tenant_id,
        conversacion_id,
        realizado_por,
    )
    assert fila is not None
    return fila


async def ultimo_corte(tenant_id: UUID, conversacion_id: UUID) -> Optional[asyncpg.Record]:
    """Corte más reciente de la conversación, o None si nunca se limpió."""
    return await fetch_one(
        """
        SELECT id, tenant_id, conversation_id, creado_en, realizado_por
        FROM conversacion_contexto_cortes
        WHERE conversation_id = $1 AND tenant_id = $2
        ORDER BY creado_en DESC, id
        LIMIT 1
        """,
        conversacion_id,
        tenant_id,
    )
