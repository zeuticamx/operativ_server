"""
Conversaciones: listado/detalle (lectura) y handoff humano (escritura).

La parte de lectura vivía inline en routers/conversaciones.py; se movió acá
porque routers/gerencia_operacion.py necesita exactamente las mismas
consultas (mismo negocio, filtrando por un tenant_id que llega por la URL
en vez de por tenant_actual) y duplicar ~130 líneas de SQL entre dos
routers no tenía sentido.

La parte de escritura (enviar_mensaje_humano, volver_a_ia) es el primer
lugar del backend que escribe en `conversations`/`messages` — tablas de
lectura hasta ahora, propiedad de n8n (ver CLAUDE.md). Es una excepción
deliberada y acotada a esta feature de handoff; ver backend/README.md.

Nada de lo que hay acá reemplaza al workflow entrada-canal-universal: ese
workflow sigue siendo quien realmente conversa con el cliente vía IA. Este
módulo solo le da a un humano del portal la puerta para escribir en el
mismo hilo, y para devolverle el control a la IA con un UPDATE que ese
workflow ya sabe leer.
"""

import secrets
from uuid import UUID

from fastapi import HTTPException, status

from config import settings
from services import adjuntos, meta, whatsapp
from session import execute, fetch_all, fetch_one, transaccion

# n8n arma el INSERT de `users` con expresiones de plantilla que, cuando el
# dato de origen viene undefined, terminan guardando el texto literal
# "null" en vez de dejar la columna en NULL de verdad. NULLIF(..., 'null')
# trata ambos casos igual: ni una cadena vacía ni el texto "null" cuentan
# como dato real.
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


# ============================================================
# Lectura
# ============================================================
async def listar(
    tenant_id: UUID,
    canal: str | None,
    estado: str | None,
    buscar: str | None,
    limite: int,
    offset: int,
):
    return await fetch_all(
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


async def metricas(tenant_id: UUID):
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

    return fila, canales


async def detalle(tenant_id: UUID, conversacion_id: UUID):
    """None si no existe o no es de este tenant (404 lo decide el router)."""
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
        return None, None

    mensajes = await fetch_all(
        """
        SELECT m.id, m.role, m.content, m.created_at,
               COALESCE(pu.full_name, pu.email) AS enviado_por
        FROM messages m
        LEFT JOIN portal_users pu ON pu.id = m.sender_portal_user_id
        WHERE m.conversation_id = $1
        ORDER BY m.created_at ASC
        """,
        conversacion_id,
    )
    por_mensaje = await adjuntos_de_mensajes([m["id"] for m in mensajes])
    mensajes = [{**dict(m), "adjuntos": por_mensaje.get(m["id"], [])} for m in mensajes]
    return cab, mensajes


# ============================================================
# Handoff humano
# ============================================================
async def _conversacion_para_escritura(tenant_id: UUID, conversacion_id: UUID):
    """
    Trae lo que hace falta para mandar un mensaje o devolver el control:
    estado actual, canal y los identificadores del cliente en cada canal.
    Mismo patrón 404-si-no-coincide que `detalle`: el filtro por tenant va
    en el WHERE, no en una validación aparte.
    """
    fila = await fetch_one(
        """
        SELECT c.id, c.status, c.channel_type,
               u.whatsapp_id, u.instagram_id, u.facebook_id
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
    return fila


def _url_y_body(canal: str, cred, destinatario: str, texto: str) -> tuple[str, dict]:
    """
    Mismo endpoint y mismo shape de body que arma el nodo "Prepara envio"
    de entrada-canal-universal para cada canal — para que la respuesta
    manual le llegue al cliente en el mismo hilo que ya tiene abierto.
    """
    if canal == "whatsapp":
        pnid = cred["phone_number_id"]
        if not pnid:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Falta phone_number_id para enviar por WhatsApp",
            )
        return (
            f"{settings.graph_url}/{pnid}/messages",
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": destinatario,
                "type": "text",
                "text": {"preview_url": False, "body": texto},
            },
        )

    if canal == "facebook":
        pid = cred["page_id"]
        if not pid:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Falta page_id para enviar por Facebook",
            )
        return (
            f"{settings.graph_url}/{pid}/messages",
            {
                "messaging_type": "RESPONSE",
                "recipient": {"id": destinatario},
                "message": {"text": texto},
            },
        )

    if canal == "instagram":
        # Igual que en n8n: el envío va contra el page_id, no el
        # ig_account_id (ese solo identifica de dónde vino el mensaje).
        pid = cred["page_id"]
        if not pid:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Falta page_id para enviar por Instagram",
            )
        return (
            f"{settings.graph_url}/{pid}/messages",
            {"recipient": {"id": destinatario}, "message": {"text": texto}},
        )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Canal no soportado para respuesta manual: {canal}",
    )


_DESTINATARIO_COL = {
    "whatsapp": "whatsapp_id",
    "facebook": "facebook_id",
    "instagram": "instagram_id",
}


async def _preparar_envio(tenant_id: UUID, conversacion_id: UUID):
    """
    Validaciones comunes a todo envío manual: conversación del tenant en modo
    humano, canal soportado, destinatario y credenciales del canal.
    Devuelve (canal, destinatario, credenciales).
    """
    conv = await _conversacion_para_escritura(tenant_id, conversacion_id)

    if conv["status"] != "transferred":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="La conversación no está transferida a un humano",
        )

    canal = conv["channel_type"]
    col_destinatario = _DESTINATARIO_COL.get(canal)
    if col_destinatario is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Canal no soportado para respuesta manual: {canal}",
        )

    destinatario = conv[col_destinatario]
    if not destinatario:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"El cliente no tiene un identificador de {canal} guardado",
        )

    cred = await fetch_one(
        "SELECT * FROM get_channel_credentials($1, $2)", tenant_id, canal
    )
    if cred is None or not cred["access_token"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"El canal {canal} no está conectado",
        )
    return canal, destinatario, cred


async def enviar_mensaje_humano(
    tenant_id: UUID,
    conversacion_id: UUID,
    texto: str,
    sender_portal_user_id: UUID,
):
    """
    Manda `texto` al cliente por su canal y lo deja en `messages` como
    role='human'. Solo tiene sentido mientras la IA no está contestando
    (status='transferred') — mandar algo con la IA activa se pisaría con
    su propia respuesta.
    """
    canal, destinatario, cred = await _preparar_envio(tenant_id, conversacion_id)

    if canal == "whatsapp" and cred["bsp_provider"] == "neuroapi":
        # El access_token de estas líneas es la API key de NeuroAPI, no un
        # token de Meta: mandarlo a Graph API devuelve error 190 ("token
        # expiró o fue revocado") aunque la cuenta esté sana. n8n hace la
        # misma bifurcación según bsp_provider (ver sql/23).
        proveedor = whatsapp.NeuroApiProvider(
            api_key=cred["access_token"],
            webhook_secret="",
            base_url=settings.NEUROAPI_API_BASE_URL,
            phone_number_id=cred["phone_number_id"] or "",
        )
        try:
            await proveedor.enviar_mensaje(destinatario, texto)
        except whatsapp.ProveedorWhatsAppError as e:
            raise whatsapp.a_http(e)
    else:
        url, body = _url_y_body(canal, cred, destinatario, texto)
        try:
            await meta.enviar_texto(url, cred["access_token"], body)
        except meta.MetaError as e:
            raise meta.a_http(e)

    fila = await fetch_one(
        """
        INSERT INTO messages (conversation_id, tenant_id, role, content, sender_portal_user_id)
        VALUES ($1, $2, 'human', $3, $4)
        RETURNING id, role, content, created_at
        """,
        conversacion_id,
        tenant_id,
        texto,
        sender_portal_user_id,
    )
    await execute(
        "UPDATE conversations SET last_message_at = NOW() WHERE id = $1",
        conversacion_id,
    )
    return fila


# ============================================================
# Adjuntos (imágenes y documentos por WhatsApp)
# ============================================================
def url_publica_adjunto(token: str) -> str:
    """URL HTTPS que NeuroAPI/Meta descargan (routers/media.py)."""
    return f"{settings.BASE_URL_BACKEND}/api/media/{token}"


async def _mandar_media(cred, destinatario: str, info, url: str, leyenda: str | None) -> None:
    if cred["bsp_provider"] == "neuroapi":
        proveedor = whatsapp.NeuroApiProvider(
            api_key=cred["access_token"],
            webhook_secret="",
            base_url=settings.NEUROAPI_API_BASE_URL,
            phone_number_id=cred["phone_number_id"] or "",
        )
        await proveedor.enviar_media(destinatario, info.tipo_wa, url, info.nombre, leyenda)
        return

    pnid = cred["phone_number_id"]
    if not pnid:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Falta phone_number_id para enviar por WhatsApp",
        )
    cuerpo = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        **whatsapp.cuerpo_media(destinatario, info.tipo_wa, url, info.nombre, leyenda),
    }
    await meta.enviar_texto(f"{settings.graph_url}/{pnid}/messages", cred["access_token"], cuerpo)


async def leer_subida(archivo) -> bytes:
    """
    Lee un UploadFile con tope: un byte más que el máximo permitido, para
    saber que se excede sin cargar entero algo enorme en memoria.
    """
    tope = adjuntos.tope_absoluto()
    contenido = await archivo.read(tope + 1)
    if len(contenido) > tope:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"El archivo pesa más de {tope // (1024 * 1024)} MB.",
        )
    return contenido


async def enviar_adjunto_humano(
    tenant_id: UUID,
    conversacion_id: UUID,
    contenido: bytes,
    nombre_archivo: str | None,
    leyenda: str | None,
    sender_portal_user_id: UUID,
):
    """
    Manda una imagen o un documento por WhatsApp y lo deja en `messages`
    (role='human') con su fila en `message_attachments`.

    Valida el archivo ANTES de tocar al proveedor (tipo por firma de bytes y
    tope de tamaño). Como el proveedor descarga el archivo de nuestra URL
    pública, la fila se inserta antes de enviar y se borra si el envío falla:
    no queda en el hilo un mensaje que el cliente nunca recibió.
    """
    try:
        info = adjuntos.validar_adjunto(contenido, nombre_archivo)
    except adjuntos.AdjuntoInvalido as e:
        raise HTTPException(status_code=e.status_code, detail=e.mensaje)

    canal, destinatario, cred = await _preparar_envio(tenant_id, conversacion_id)

    if canal != "whatsapp":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Solo se pueden enviar archivos por WhatsApp",
        )
    if not settings.BASE_URL_BACKEND.startswith("https://"):
        # El proveedor tiene que poder descargar el archivo: con http o
        # localhost el envío fallaría después de haber guardado todo.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="BASE_URL_BACKEND debe ser una URL HTTPS pública para enviar archivos",
        )

    leyenda = (leyenda or "").strip() or None
    token = secrets.token_urlsafe(32)

    async with transaccion() as conn:
        fila = await conn.fetchrow(
            """
            INSERT INTO messages (conversation_id, tenant_id, role, content, sender_portal_user_id)
            VALUES ($1, $2, 'human', $3, $4)
            RETURNING id, role, content, created_at
            """,
            conversacion_id,
            tenant_id,
            adjuntos.texto_mensaje(info, leyenda),
            sender_portal_user_id,
        )
        adjunto = await conn.fetchrow(
            """
            INSERT INTO message_attachments
                (tenant_id, message_id, direccion, mime, nombre, bytes, contenido, token_publico)
            VALUES ($1, $2, 'out', $3, $4, $5, $6, $7)
            RETURNING id, mime, nombre, bytes
            """,
            tenant_id,
            fila["id"],
            info.mime,
            info.nombre,
            len(contenido),
            contenido,
            token,
        )

    try:
        await _mandar_media(cred, destinatario, info, url_publica_adjunto(token), leyenda)
    except Exception as e:
        # ON DELETE CASCADE se lleva también la fila del adjunto.
        await execute("DELETE FROM messages WHERE id = $1", fila["id"])
        if isinstance(e, whatsapp.ProveedorWhatsAppError):
            raise whatsapp.a_http(e)
        if isinstance(e, meta.MetaError):
            raise meta.a_http(e)
        raise

    await execute(
        "UPDATE conversations SET last_message_at = NOW() WHERE id = $1",
        conversacion_id,
    )
    return fila, adjunto


async def adjuntos_de_mensajes(mensaje_ids: list[UUID]) -> dict[UUID, list[dict]]:
    """Adjuntos agrupados por mensaje (sin el contenido binario)."""
    if not mensaje_ids:
        return {}
    filas = await fetch_all(
        """
        SELECT id, message_id, mime, nombre, bytes
        FROM message_attachments
        WHERE message_id = ANY($1::uuid[])
        ORDER BY created_at
        """,
        mensaje_ids,
    )
    agrupados: dict[UUID, list[dict]] = {}
    for f in filas:
        agrupados.setdefault(f["message_id"], []).append(
            {"id": f["id"], "mime": f["mime"], "nombre": f["nombre"], "bytes": f["bytes"]}
        )
    return agrupados


async def obtener_adjunto(tenant_id: UUID, conversacion_id: UUID, adjunto_id: UUID):
    """Adjunto con contenido; None si no es de esa conversación y tenant."""
    return await fetch_one(
        """
        SELECT a.mime, a.nombre, a.contenido
        FROM message_attachments a
        JOIN messages m ON m.id = a.message_id
        WHERE a.id = $1 AND m.conversation_id = $2 AND a.tenant_id = $3
        """,
        adjunto_id,
        conversacion_id,
        tenant_id,
    )


async def tomar_conversacion(tenant_id: UUID, conversacion_id: UUID):
    """
    Un humano toma el control desde el portal: es la inversa de `volver_a_ia`
    y deja el mismo estado que `escalar_humano` (status='transferred'), así
    que entrada-canal-universal deja de contestar con IA sin cambios en n8n.

    `ack_pendiente` queda en false a propósito: ese acuse ("te estamos
    transfiriendo con una persona") es para cuando la IA escala por su cuenta;
    quien toma la conversación a mano va a escribir él mismo, y un aviso
    automático antes de su mensaje sería ruido.

    El UPDATE condicional (status='active') evita pisar una transferencia que
    el agente hizo entre la lectura y la escritura.
    """
    conv = await _conversacion_para_escritura(tenant_id, conversacion_id)

    if conv["status"] != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="La conversación ya está en modo humano",
        )

    fila = await fetch_one(
        """
        UPDATE conversations
        SET status = 'transferred',
            metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                'motivo_escalamiento', 'tomada_desde_portal',
                'escalado_en', NOW()::text,
                'ack_pendiente', false
            )
        WHERE id = $1 AND tenant_id = $2 AND status = 'active'
        RETURNING id, status
        """,
        conversacion_id,
        tenant_id,
    )
    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="La conversación ya está en modo humano",
        )
    return fila


async def volver_a_ia(tenant_id: UUID, conversacion_id: UUID):
    """
    Le devuelve el control a la IA: solo hace falta poner status='active'
    otra vez. entrada-canal-universal ya relee este campo en cada mensaje
    entrante (nodo "¿Conversación transferida?"), así que no hace falta
    avisarle a n8n por ningún otro lado.
    """
    conv = await _conversacion_para_escritura(tenant_id, conversacion_id)

    if conv["status"] != "transferred":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="La conversación no está en modo humano",
        )

    fila = await fetch_one(
        """
        UPDATE conversations
        SET status = 'active',
            metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), '{ack_pendiente}', 'false')
        WHERE id = $1
        RETURNING id, status
        """,
        conversacion_id,
    )
    return fila
