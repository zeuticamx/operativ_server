"""Panel de conversaciones: listado, detalle y métricas."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from deps import tenant_actual
from session import execute, fetch_all, fetch_one
from schemas import (
    ContactoOut,
    ConversacionDetalleOut,
    ConversacionOut,
    MensajeOut,
    MetricasOut,
)
import meta

router = APIRouter(prefix="/conversaciones", tags=["conversaciones"])

# n8n arma el INSERT de `users` con expresiones de plantilla; cuando el dato
# de origen (nombre de perfil, número, etc.) viene undefined, esas
# expresiones lo interpolan como el texto literal "null" en vez de dejar la
# columna en NULL de verdad. NULLIF(..., 'null') trata ambos casos igual:
# ni una cadena vacía ni el texto "null" cuentan como dato real.
_NOMBRE = "NULLIF(NULLIF(TRIM(u.display_name), ''), 'null')"
_WHATSAPP = "NULLIF(NULLIF(TRIM(u.whatsapp_id), ''), 'null')"
_INSTAGRAM = "NULLIF(NULLIF(TRIM(u.instagram_id), ''), 'null')"
_FACEBOOK = "NULLIF(NULLIF(TRIM(u.facebook_id), ''), 'null')"
_USERNAME = "NULLIF(NULLIF(TRIM(u.instagram_username), ''), 'null')"
_HANDLE = f"COALESCE({_WHATSAPP}, {_INSTAGRAM}, {_FACEBOOK})"

# Ventana de 24h de Meta: se cuenta desde el último mensaje con role='user'
# (el cliente), nunca desde uno del asistente. Todo se calcula en SQL con
# NOW() del propio Postgres — nunca se manda el timestamp crudo a Python
# para restarlo ahí, porque esta columna no tiene huso horario guardado y
# el resultado dependería de en qué zona esté corriendo cada proceso.
# Restando dos timestamps "naive" que vienen del mismo Postgres el
# resultado siempre es correcto, sin importar qué huso representen.
_ULTIMO_CLIENTE = (
    "(SELECT MAX(m.created_at) FROM messages m "
    "WHERE m.conversation_id = c.id AND m.role = 'user')"
)
_MINUTOS_VENTANA = (
    f"CASE WHEN {_ULTIMO_CLIENTE} IS NULL THEN NULL "
    f"ELSE (1440 - EXTRACT(EPOCH FROM (NOW() - {_ULTIMO_CLIENTE})) / 60)::int END"
)


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
        f"""
        SELECT
            c.id,
            c.channel_type,
            c.status,
            c.started_at,
            c.last_message_at,
            {_NOMBRE} AS usuario_nombre,
            {_HANDLE} AS usuario_handle,
            {_USERNAME} AS usuario_username,
            (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id)
                AS total_mensajes,
            (SELECT m.content FROM messages m
             WHERE m.conversation_id = c.id
             ORDER BY m.created_at DESC LIMIT 1) AS ultimo_mensaje,
            {_ULTIMO_CLIENTE} AS ultimo_mensaje_cliente_at,
            {_MINUTOS_VENTANA} AS minutos_restantes_ventana
        FROM conversations c
        JOIN users u ON u.id = c.user_id
        WHERE c.tenant_id = $1
          AND ($2::varchar IS NULL OR c.channel_type = $2)
          AND ($3::varchar IS NULL OR c.status = $3)
          AND (
            $4::varchar IS NULL
            OR u.display_name ILIKE '%' || $4 || '%'
            OR {_HANDLE} ILIKE '%' || $4 || '%'
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
        f"""
        SELECT
            c.id, c.channel_type, c.status, c.started_at,
            {_NOMBRE} AS usuario_nombre,
            {_HANDLE} AS usuario_handle,
            {_USERNAME} AS usuario_username,
            {_ULTIMO_CLIENTE} AS ultimo_mensaje_cliente_at,
            {_MINUTOS_VENTANA} AS minutos_restantes_ventana
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


@router.post("/{conversacion_id}/contacto", response_model=ContactoOut)
async def actualizar_contacto(
    conversacion_id: UUID,
    tenant_id: UUID = Depends(tenant_actual),
):
    """
    Le pregunta a Meta el nombre del contacto y lo guarda en `users`.

    El webhook solo trae un id numérico interno de la app (PSID en
    Messenger, IGSID en Instagram), por eso las conversaciones aparecen
    sin nombre hasta que se consulta el perfil. Se hace bajo demanda y no
    al listar: sería una llamada a la Graph API por conversación en cada
    carga de la pantalla.
    """
    fila = await fetch_one(
        """
        SELECT c.channel_type, u.id AS user_id,
               u.instagram_id, u.facebook_id
        FROM conversations c
        JOIN users u ON u.id = c.user_id
        WHERE c.id = $1 AND c.tenant_id = $2
        """,
        conversacion_id,
        tenant_id,
    )
    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversación no encontrada",
        )

    canal = fila["channel_type"]
    if canal == "whatsapp":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "WhatsApp no tiene API de perfil: el nombre del contacto llega "
                "en el propio mensaje y lo guarda el flujo que lo recibe."
            ),
        )

    contacto_id = fila["instagram_id"] if canal == "instagram" else fila["facebook_id"]
    if not contacto_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"El contacto no tiene un identificador de {canal} guardado",
        )

    # El page_id dice a qué página pertenece la conversación; el token de
    # esa página se pide a Meta con el user token, igual que al conectar.
    canal_fila = await fetch_one(
        """
        SELECT page_id FROM v_tenant_channels
        WHERE tenant_id = $1 AND channel_type = $2 AND is_active
        """,
        tenant_id,
        canal,
    )
    if canal_fila is None or not canal_fila["page_id"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"El canal {canal} no está conectado",
        )

    conexion = await fetch_one("SELECT * FROM get_meta_connection($1)", tenant_id)
    if conexion is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Todavía no has conectado tu cuenta de Meta",
        )

    try:
        paginas = await meta.listar_paginas(conexion["user_token"])
        page_token = next(
            (p["access_token"] for p in paginas if p["id"] == canal_fila["page_id"]),
            None,
        )
        if page_token is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="La página conectada ya no está autorizada en Meta",
            )
        perfil = await meta.perfil_contacto(canal, contacto_id, page_token)
    except meta.MetaError as e:
        raise meta.a_http(e)

    nombre = perfil["nombre"]
    username = perfil["username"]

    # COALESCE: si Meta no devuelve algo, se conserva lo que ya hubiera
    # en vez de borrarlo.
    await execute(
        """
        UPDATE users
        SET display_name = COALESCE($1, display_name),
            instagram_username = COALESCE($2, instagram_username)
        WHERE id = $3
        """,
        nombre,
        username,
        fila["user_id"],
    )

    return ContactoOut(
        usuario_nombre=nombre,
        usuario_username=username,
        actualizado=bool(nombre or username),
    )
