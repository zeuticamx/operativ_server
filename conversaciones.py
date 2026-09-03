"""Panel de conversaciones: listado, detalle y métricas."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from deps import tenant_actual
from session import fetch_all, fetch_one
from schemas import (
    ConversacionDetalleOut,
    ConversacionOut,
    MensajeOut,
    MetricasOut,
)

router = APIRouter(prefix="/conversaciones", tags=["conversaciones"])


@router.get("", response_model=list[ConversacionOut])
async def listar(
    tenant_id: UUID = Depends(tenant_actual),
    canal: str | None = Query(None),
    estado: str | None = Query(None),
    buscar: str | None = Query(None, max_length=100),
    limite: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    filas = await fetch_all(
        """
        SELECT
            c.id,
            c.channel_type,
            c.status,
            c.started_at,
            c.last_message_at,
            u.display_name AS usuario_nombre,
            COALESCE(u.whatsapp_id, u.instagram_id, u.facebook_id) AS usuario_handle,
            (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id)
                AS total_mensajes,
            (SELECT m.content FROM messages m
             WHERE m.conversation_id = c.id
             ORDER BY m.created_at DESC LIMIT 1) AS ultimo_mensaje
        FROM conversations c
        JOIN users u ON u.id = c.user_id
        WHERE c.tenant_id = $1
          AND ($2::varchar IS NULL OR c.channel_type = $2)
          AND ($3::varchar IS NULL OR c.status = $3)
          AND (
            $4::varchar IS NULL
            OR u.display_name ILIKE '%' || $4 || '%'
            OR COALESCE(u.whatsapp_id, u.instagram_id, u.facebook_id) ILIKE '%' || $4 || '%'
          )
        ORDER BY c.last_message_at DESC
        LIMIT $5 OFFSET $6
        """,
        tenant_id,
        canal,
        estado,
        buscar,
        limite,
        offset,
    )
    return [ConversacionOut(**dict(f)) for f in filas]


@router.get("/metricas", response_model=MetricasOut)
async def metricas(tenant_id: UUID = Depends(tenant_actual)):
    fila = await fetch_one(
        """
        SELECT
            (SELECT COUNT(*) FROM conversations
             WHERE tenant_id = $1 AND status = 'active') AS conversaciones_activas,
            (SELECT COUNT(*) FROM messages
             WHERE tenant_id = $1 AND created_at >= CURRENT_DATE) AS mensajes_hoy,
            (SELECT COUNT(*) FROM messages
             WHERE tenant_id = $1 AND created_at >= NOW() - INTERVAL '7 days')
                AS mensajes_7d,
            (SELECT COUNT(*) FROM conversations
             WHERE tenant_id = $1 AND started_at >= NOW() - INTERVAL '7 days')
                AS conversaciones_7d
        """,
        tenant_id,
    )

    canales = await fetch_all(
        """
        SELECT channel_type, COUNT(*) AS n
        FROM conversations
        WHERE tenant_id = $1
        GROUP BY channel_type
        """,
        tenant_id,
    )

    return MetricasOut(
        **dict(fila),
        por_canal={c["channel_type"]: c["n"] for c in canales},
    )


@router.get("/{conversacion_id}", response_model=ConversacionDetalleOut)
async def detalle(
    conversacion_id: UUID,
    tenant_id: UUID = Depends(tenant_actual),
):
    # El filtro por tenant_id va en el WHERE, no como validación aparte:
    # así es imposible leer la conversación de otro tenant aunque se
    # adivine el UUID.
    cab = await fetch_one(
        """
        SELECT
            c.id, c.channel_type, c.status, c.started_at,
            u.display_name AS usuario_nombre,
            COALESCE(u.whatsapp_id, u.instagram_id, u.facebook_id) AS usuario_handle
        FROM conversations c
        JOIN users u ON u.id = c.user_id
        WHERE c.id = $1 AND c.tenant_id = $2
        """,
        conversacion_id,
        tenant_id,
    )

    if cab is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversación no encontrada",
        )

    mensajes = await fetch_all(
        """
        SELECT id, role, content, created_at
        FROM messages
        WHERE conversation_id = $1
        ORDER BY created_at ASC
        """,
        conversacion_id,
    )

    return ConversacionDetalleOut(
        **dict(cab),
        mensajes=[MensajeOut(**dict(m)) for m in mensajes],
    )
