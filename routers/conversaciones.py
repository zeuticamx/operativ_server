"""Panel de conversaciones: listado, detalle, métricas y handoff humano."""

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status

from deps import (
    ROL_PROVEEDOR,
    UsuarioActual,
    permitir_a_proveedor,
    proveedor_actual,
    requiere_herramienta,
    tenant_actual,
    usuario_actual,
)
from realtime import emit_conversacion_estado, emit_mensaje
from session import execute, fetch_one
from schemas import (
    AdjuntoOut,
    ContactoOut,
    ConversacionDetalleOut,
    ConversacionEstadoOut,
    ConversacionOut,
    EnviarMensajeIn,
    MensajeOut,
    MetricasOut,
)
from services import adjuntos
from services import conversaciones as svc
from services import meta

# lectura_sin_plan: con el plan vencido el negocio sigue viendo su historial;
# lo que se corta es contestar a mano o devolverle el control a la IA.
router = APIRouter(
    prefix="/conversaciones",
    tags=["conversaciones"],
    # Las conversaciones son del negocio entero: el vendedor no las ve. El
    # proveedor del calendario entra solo a estos endpoints, y cada uno lo
    # acota a las conversaciones de sus clientes (conversacion_visible).
    # Ni métricas del negocio ni editar el contacto.
    dependencies=[
        Depends(
            permitir_a_proveedor(
                "listar",
                "detalle",
                "enviar_mensaje",
                "enviar_adjunto",
                "descargar_adjunto",
                "tomar",
                "volver_a_ia",
            )
        ),
        Depends(requiere_herramienta("agente", lectura_sin_plan=True)),
    ],
)


async def _proveedor_propio(usuario: UsuarioActual) -> UUID | None:
    """El id de proveedor de quien llama, o None si es del equipo del negocio (ve todo)."""
    if usuario.role != ROL_PROVEEDOR:
        return None
    return (await proveedor_actual(usuario)).id


async def conversacion_visible(
    conversacion_id: UUID,
    usuario: UsuarioActual = Depends(usuario_actual),
    tenant_id: UUID = Depends(tenant_actual),
) -> None:
    """
    Para un proveedor: 404 si la conversación no existe en el negocio, 403 si
    es de un cliente de otro proveedor (es del mismo negocio, no se esconde
    que existe — mismo criterio que con el vendedor y sus leads).
    """
    propio = await _proveedor_propio(usuario)
    if propio is None:
        return
    suya = await svc.es_de_proveedor(tenant_id, conversacion_id, propio)
    if suya is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversación no encontrada")
    if not suya:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta conversación es de un cliente de otro proveedor",
        )


@router.get("", response_model=list[ConversacionOut])
async def listar(
    tenant_id: UUID = Depends(tenant_actual),
    canal: str | None = Query(None),
    estado: str | None = Query(None),
    buscar: str | None = Query(None, max_length=100),
    limite: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    usuario: UsuarioActual = Depends(usuario_actual),
):
    propio = await _proveedor_propio(usuario)
    filas = await svc.listar(tenant_id, canal, estado, buscar, limite, offset, proveedor_id=propio)
    return [ConversacionOut(**dict(f)) for f in filas]


@router.get("/metricas", response_model=MetricasOut)
async def metricas(tenant_id: UUID = Depends(tenant_actual)):
    fila, canales = await svc.metricas(tenant_id)
    return MetricasOut(
        **dict(fila),
        por_canal={c["channel_type"]: c["n"] for c in canales},
    )


@router.get("/{conversacion_id}", response_model=ConversacionDetalleOut)
async def detalle(
    conversacion_id: UUID,
    tenant_id: UUID = Depends(tenant_actual),
    _: None = Depends(conversacion_visible),
):
    # El filtro por tenant_id va en el WHERE, no como validación aparte:
    # así es imposible leer la conversación de otro tenant aunque se
    # adivine el UUID.
    cab, mensajes = await svc.detalle(tenant_id, conversacion_id)
    if cab is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversación no encontrada",
        )

    return ConversacionDetalleOut(
        **dict(cab),
        mensajes=[MensajeOut(**dict(m)) for m in mensajes],
    )


@router.post(
    "/{conversacion_id}/mensajes",
    response_model=MensajeOut,
    status_code=status.HTTP_201_CREATED,
)
async def enviar_mensaje(
    conversacion_id: UUID,
    datos: EnviarMensajeIn,
    usuario: UsuarioActual = Depends(usuario_actual),
    tenant_id: UUID = Depends(tenant_actual),
    _: None = Depends(conversacion_visible),
):
    """
    Respuesta manual mientras la conversación está transferida a un humano
    (Escenario 2). No requiere `gerencia_actual`: contestarle a un cliente
    es trabajo operativo del día a día, no un cambio de configuración del
    negocio — mismo criterio con el que hoy 'member' ya ve el pipeline.
    """
    fila = await svc.enviar_mensaje_humano(
        tenant_id, conversacion_id, datos.texto, usuario.id
    )
    mensaje = MensajeOut(**dict(fila), enviado_por=usuario.email)
    await emit_mensaje(tenant_id, conversacion_id, mensaje.model_dump(mode="json"))
    return mensaje


@router.post(
    "/{conversacion_id}/adjuntos",
    response_model=MensajeOut,
    status_code=status.HTTP_201_CREATED,
)
async def enviar_adjunto(
    conversacion_id: UUID,
    archivo: UploadFile = File(...),
    leyenda: str | None = Form(None, max_length=1024),
    usuario: UsuarioActual = Depends(usuario_actual),
    tenant_id: UUID = Depends(tenant_actual),
    _: None = Depends(conversacion_visible),
):
    """
    Imagen (JPG/PNG) o documento (PDF/DOCX) como respuesta manual por
    WhatsApp. Errores de archivo: 413 pesa de más, 415 tipo no permitido o
    extensión que no coincide, 422 vacío.
    """
    contenido = await svc.leer_subida(archivo)
    fila, adjunto = await svc.enviar_adjunto_humano(
        tenant_id, conversacion_id, contenido, archivo.filename, leyenda, usuario.id
    )
    mensaje = MensajeOut(
        **dict(fila),
        enviado_por=usuario.email,
        adjuntos=[AdjuntoOut(**dict(adjunto))],
    )
    await emit_mensaje(tenant_id, conversacion_id, mensaje.model_dump(mode="json"))
    return mensaje


@router.get("/{conversacion_id}/adjuntos/{adjunto_id}")
async def descargar_adjunto(
    conversacion_id: UUID,
    adjunto_id: UUID,
    tenant_id: UUID = Depends(tenant_actual),
    _: None = Depends(conversacion_visible),
):
    """Contenido de un adjunto (enviado o recibido) de una conversación del tenant."""
    fila = await svc.obtener_adjunto(tenant_id, conversacion_id, adjunto_id)
    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Adjunto no encontrado",
        )
    return Response(
        content=bytes(fila["contenido"]),
        media_type=fila["mime"],
        headers=adjuntos.cabeceras_descarga(fila["nombre"], privado=True),
    )


@router.post("/{conversacion_id}/tomar", response_model=ConversacionEstadoOut)
async def tomar(
    conversacion_id: UUID,
    tenant_id: UUID = Depends(tenant_actual),
    _: None = Depends(conversacion_visible),
):
    """Un humano toma el control: la IA deja de contestar (inversa de volver-a-ia)."""
    fila = await svc.tomar_conversacion(tenant_id, conversacion_id)
    await emit_conversacion_estado(tenant_id, conversacion_id, fila["status"])
    return ConversacionEstadoOut(**dict(fila))


@router.post("/{conversacion_id}/volver-a-ia", response_model=ConversacionEstadoOut)
async def volver_a_ia(
    conversacion_id: UUID,
    tenant_id: UUID = Depends(tenant_actual),
    _: None = Depends(conversacion_visible),
):
    """Le devuelve el control a la IA (Escenario 4)."""
    fila = await svc.volver_a_ia(tenant_id, conversacion_id)
    await emit_conversacion_estado(tenant_id, conversacion_id, fila["status"])
    return ConversacionEstadoOut(**dict(fila))


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
