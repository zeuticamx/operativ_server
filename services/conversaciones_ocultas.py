"""
Conversaciones ocultas de números desvinculados (spec 005a, RF-14, RF-16).

Cuando la gerencia desvincula un número, la pertenencia termina: sus
conversaciones dejan de verse en las listas del negocio, se asignan a la cuenta
owner que creó el negocio (su responsable operativo, D23) y quedan en SOLO
LECTURA en la lista de ocultas. Nadie del negocio puede responderlas (409
`numero_desvinculado`, lo aplica el envío en T5/T7). Se conservan 12 meses desde
la desvinculación y luego se eliminan (D16).

No hay una marca "oculta" en `conversations`: una conversación está oculta
cuando su `agente_canal_id` apunta a un número `desvinculado`. Por eso volver a
vincular el número (a este negocio o a otro) crea una fila NUEVA de
`agentes_canal`, y las conversaciones viejas siguen colgando de la antigua: no
reaparecen ni se unen a las nuevas (D42).

Las listas normales de conversaciones deben añadir `FILTRO_NO_OCULTA` a su WHERE
(alias `c` para `conversations`).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Sequence
from uuid import UUID

import asyncpg

# Fragmento SQL para las listas y búsquedas del negocio (alias `c`).
FILTRO_NO_OCULTA = (
    "NOT EXISTS (SELECT 1 FROM agentes_canal ac_oc "
    "WHERE ac_oc.id = c.agente_canal_id AND ac_oc.estado = 'desvinculado')"
)

NOTA_ASIGNACION = (
    "El número por el que entró esta conversación se desvinculó. "
    "Está en solo lectura: nadie del negocio puede responderla."
)
MESES_RETENCION = 12


@dataclass(frozen=True)
class CuentaGerencia:
    """Una cuenta owner o superadmin del negocio, tal como la ve la regla de responsable."""

    id: UUID
    role: str
    is_active: bool
    created_at: datetime


def elegir_responsable(cuentas: Sequence[CuentaGerencia]) -> Optional[UUID]:
    """
    D23, A41: la cuenta owner que creó el negocio (la owner más antigua). Si está
    desactivada, otra cuenta owner activa (la más antigua). Si no hay ninguna, la
    superadmin activa más antigua. Sin nadie, None.
    """
    clave = lambda c: (c.created_at, str(c.id))  # noqa: E731 - desempate estable
    owners = sorted((c for c in cuentas if c.role == "owner"), key=clave)
    if owners and owners[0].is_active:
        return owners[0].id
    for cuenta in owners:
        if cuenta.is_active:
            return cuenta.id
    for cuenta in sorted((c for c in cuentas if c.role == "superadmin"), key=clave):
        if cuenta.is_active:
            return cuenta.id
    return None


async def responsable_de_ocultas(conn: asyncpg.Connection, tenant_id: UUID) -> Optional[UUID]:
    filas = await conn.fetch(
        """
        SELECT id, role, is_active, created_at
        FROM portal_users
        WHERE tenant_id = $1 AND role IN ('owner', 'superadmin')
        """,
        tenant_id,
    )
    return elegir_responsable(
        [CuentaGerencia(f["id"], f["role"], bool(f["is_active"]), f["created_at"]) for f in filas]
    )


async def asignar_al_responsable(
    conn: asyncpg.Connection, tenant_id: UUID, agente_canal_id: UUID, ahora: datetime
) -> int:
    """
    Asigna al responsable todas las conversaciones del número (de cualquier
    estado) y deja cada cambio en el historial de asignaciones, en una sola
    sentencia. Devuelve cuántas. Sin owner ni superadmin activo quedan sin asignar.
    """
    responsable = await responsable_de_ocultas(conn, tenant_id)
    movidas = await conn.fetchval(
        """
        WITH previas AS (
            SELECT id, asignado_a
            FROM conversations
            WHERE tenant_id = $1 AND agente_canal_id = $2
            FOR UPDATE
        ),
        actualizadas AS (
            UPDATE conversations c
            SET asignado_a = $3,
                asignado_en = CASE WHEN $3::uuid IS NULL THEN NULL ELSE $4::timestamptz END,
                asignado_origen = CASE WHEN $3::uuid IS NULL THEN NULL ELSE 'sistema' END,
                asignacion_nota = $5
            FROM previas p
            WHERE c.id = p.id
            RETURNING c.id
        ),
        historial AS (
            INSERT INTO conversacion_asignaciones
                (tenant_id, conversation_id, de_portal_user_id, a_portal_user_id,
                 origen, actor_portal_user_id, nota)
            SELECT $1, p.id, p.asignado_a, $3, 'sistema', NULL, $5
            FROM previas p
            JOIN actualizadas a ON a.id = p.id
            RETURNING 1
        )
        SELECT count(*) FROM actualizadas
        """,
        tenant_id,
        agente_canal_id,
        responsable,
        ahora,
        NOTA_ASIGNACION,
    )
    return int(movidas or 0)


async def conversacion_para_mensaje_oculto(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    agente_canal_id: UUID,
    user_id: UUID,
    ahora: datetime,
) -> UUID:
    """
    RF-16, A42: un mensaje que llega por un número desvinculado se guarda en la
    conversación de ese cliente con ESE número (la crea si no existe), asignada
    al responsable y sin respuesta. Nunca se atribuye a otro negocio ni al
    número nuevo si el identificador se vinculó otra vez. Lo usa la entrada de
    mensajes (T7). `ValueError` si el número no es un desvinculado del negocio.
    """
    numero = await conn.fetchrow(
        "SELECT id FROM agentes_canal WHERE id = $1 AND tenant_id = $2 AND estado = 'desvinculado'",
        agente_canal_id,
        tenant_id,
    )
    if numero is None:
        raise ValueError("El número no es un desvinculado de este negocio")

    existente = await conn.fetchval(
        """
        SELECT id FROM conversations
        WHERE tenant_id = $1 AND user_id = $2 AND agente_canal_id = $3
        ORDER BY started_at DESC
        LIMIT 1
        """,
        tenant_id,
        user_id,
        agente_canal_id,
    )
    if existente is not None:
        return existente

    responsable = await responsable_de_ocultas(conn, tenant_id)
    nueva = await conn.fetchval(
        """
        INSERT INTO conversations
            (tenant_id, user_id, channel_type, status, agente_canal_id,
             asignado_a, asignado_en, asignado_origen, asignacion_nota)
        VALUES ($1, $2, 'whatsapp', 'active', $3, $4,
                CASE WHEN $4::uuid IS NULL THEN NULL ELSE $5::timestamptz END,
                CASE WHEN $4::uuid IS NULL THEN NULL ELSE 'sistema' END,
                $6)
        RETURNING id
        """,
        tenant_id,
        user_id,
        agente_canal_id,
        responsable,
        ahora,
        NOTA_ASIGNACION,
    )
    await conn.execute(
        """
        INSERT INTO conversacion_asignaciones
            (tenant_id, conversation_id, de_portal_user_id, a_portal_user_id,
             origen, actor_portal_user_id, nota)
        VALUES ($1, $2, NULL, $3, 'sistema', NULL, $4)
        """,
        tenant_id,
        nueva,
        responsable,
        NOTA_ASIGNACION,
    )
    return nueva


async def listar_ocultas(
    conn: asyncpg.Connection, tenant_id: UUID, *, limite: int = 50, desplazamiento: int = 0
) -> list[asyncpg.Record]:
    """La lista de ocultas del negocio (solo lectura), lo más reciente primero."""
    return await conn.fetch(
        """
        SELECT c.id, c.channel_type, c.status, c.started_at, c.last_message_at,
               c.asignado_a, c.agente_canal_id,
               a.desvinculado_en, a.telefono_visible, a.phone_number_id,
               u.display_name AS usuario_nombre
        FROM conversations c
        JOIN agentes_canal a ON a.id = c.agente_canal_id
                            AND a.tenant_id = c.tenant_id
                            AND a.estado = 'desvinculado'
        LEFT JOIN users u ON u.id = c.user_id
        WHERE c.tenant_id = $1
        ORDER BY c.last_message_at DESC, c.id
        LIMIT $2 OFFSET $3
        """,
        tenant_id,
        limite,
        desplazamiento,
    )


async def detalle_oculta(
    conn: asyncpg.Connection, tenant_id: UUID, conversation_id: UUID
) -> Optional[dict[str, Any]]:
    """
    Una oculta con sus mensajes. None si no existe, es de otro negocio o no está
    oculta (el router responde el mismo 404 en los tres casos).
    """
    cabecera = await conn.fetchrow(
        """
        SELECT c.id, c.channel_type, c.status, c.started_at, c.last_message_at,
               c.asignado_a, c.agente_canal_id,
               a.desvinculado_en, a.telefono_visible, a.phone_number_id,
               u.display_name AS usuario_nombre
        FROM conversations c
        JOIN agentes_canal a ON a.id = c.agente_canal_id
                            AND a.tenant_id = c.tenant_id
                            AND a.estado = 'desvinculado'
        LEFT JOIN users u ON u.id = c.user_id
        WHERE c.id = $1 AND c.tenant_id = $2
        """,
        conversation_id,
        tenant_id,
    )
    if cabecera is None:
        return None
    mensajes = await conn.fetch(
        """
        SELECT id, role, content, created_at
        FROM messages
        WHERE conversation_id = $1 AND tenant_id = $2
        ORDER BY created_at, id
        """,
        conversation_id,
        tenant_id,
    )
    return {"conversacion": cabecera, "mensajes": mensajes}


async def eliminar_vencidas(conn: asyncpg.Connection, ahora: datetime) -> tuple[int, int]:
    """
    D16, A43: elimina las conversaciones (sus mensajes caen en cascada) de los
    números desvinculados hace más de 12 meses y después las filas de esos
    números que se quedaron sin conversaciones. Devuelve (conversaciones,
    números). Idempotente. Lo llama el trabajo diario (T8).
    """
    conversaciones = await conn.fetchval(
        """
        WITH borradas AS (
            DELETE FROM conversations c
            USING agentes_canal a
            WHERE c.agente_canal_id = a.id
              AND a.estado = 'desvinculado'
              AND a.desvinculado_en < $1::timestamptz - make_interval(months => $2)
            RETURNING c.id
        )
        SELECT count(*) FROM borradas
        """,
        ahora,
        MESES_RETENCION,
    )
    numeros = await conn.fetchval(
        """
        WITH borrados AS (
            DELETE FROM agentes_canal a
            WHERE a.estado = 'desvinculado'
              AND a.desvinculado_en < $1::timestamptz - make_interval(months => $2)
              AND NOT EXISTS (SELECT 1 FROM conversations c WHERE c.agente_canal_id = a.id)
            RETURNING a.id
        )
        SELECT count(*) FROM borrados
        """,
        ahora,
        MESES_RETENCION,
    )
    return int(conversaciones or 0), int(numeros or 0)
