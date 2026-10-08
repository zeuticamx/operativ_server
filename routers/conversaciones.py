"""Panel de conversaciones: listado, detalle, métricas y handoff humano."""

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status

from deps import (
    ROL_PROVEEDOR,
    ROL_VENDEDOR,
    ROLES_GERENCIA,
    UsuarioActual,
    gerencia_actual,
    permitir_a_roles_acotados,
    proveedor_actual,
    requiere_herramienta,
    tenant_actual,
    usuario_actual,
    vendedor_actual,
)
from realtime import avisar_a_usuario, emit_conversacion_estado, emit_mensaje
from session import execute, fetch_one
from schemas import (
    AdjuntoOut,
    AsignableOut,
    AsignarConversacionIn,
    ContactoOut,
    ConversacionDetalleOut,
    ConversacionEstadoOut,
    ConversacionOut,
    EnviarMensajeIn,
    MensajeOut,
    MetricasOut,
)
from services import adjuntos
from services import asignacion_conversaciones as asig
from services import conversaciones as svc
from services import meta

# lectura_sin_plan: con el plan vencido el negocio sigue viendo su historial;
# lo que se corta es contestar a mano o devolverle el control a la IA.
router = APIRouter(
    prefix="/conversaciones",
    tags=["conversaciones"],
    # Las conversaciones son del negocio entero: el owner, el superadmin y el
    # member las ven todas, con quién las tiene. El vendedor y el proveedor
    # entran solo a estos endpoints, y cada uno los acota a lo suyo: lo que
    # les asignaron, más lo que su ficha les hace propio mientras nadie lo
    # tenga (conversacion_visible). Ni métricas ni editar el contacto.
    dependencies=[
        Depends(
            permitir_a_roles_acotados(
                "listar",
                "detalle",
                "enviar_mensaje",
                "enviar_adjunto",
                "descargar_adjunto",
                "tomar",
                "volver_a_ia",
                "soltar",
            )
        ),
        Depends(requiere_herramienta("agente", lectura_sin_plan=True)),
    ],
)


async def _alcance(usuario: UsuarioActual) -> svc.Alcance | None:
    """
    Qué ve quien llama: None si es del equipo del negocio (ve todo), o el
    alcance de su ficha si es vendedor / proveedor (ficha activa, o 403).
    """
    if usuario.role == ROL_PROVEEDOR:
        return svc.Alcance(usuario.id, proveedor_id=(await proveedor_actual(usuario)).id)
    if usuario.role == ROL_VENDEDOR:
        return svc.Alcance(usuario.id, vendedor_id=(await vendedor_actual(usuario)).id)
    return None


async def conversacion_visible(
    conversacion_id: UUID,
    usuario: UsuarioActual = Depends(usuario_actual),
    tenant_id: UUID = Depends(tenant_actual),
) -> None:
    """
    Para vendedor / proveedor: 404 si la conversación no existe en el negocio,
    403 si es de otro (es del mismo negocio, no se esconde que existe — mismo
    criterio que con el vendedor y sus leads).
    """
    alcance = await _alcance(usuario)
    if alcance is None:
        return
    suya = await svc.es_visible(tenant_id, conversacion_id, alcance)
    if suya is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversación no encontrada")
    if not suya:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta conversación es de otra persona del equipo",
        )


async def puede_operar(
    conversacion_id: UUID,
    usuario: UsuarioActual = Depends(usuario_actual),
    tenant_id: UUID = Depends(tenant_actual),
) -> None:
    """
    Para contestar o devolverla a la IA: si alguien la tiene asignada, solo él
    o la gerencia (owner/superadmin). Sin asignar, cualquiera que la vea (como
    hasta ahora). 403 con el nombre de quien la tiene: es del mismo negocio.
    Además de `conversacion_visible`, no en vez de: este solo mira al asignado.
    """
    fila = await fetch_one(
        f"""
        SELECT c.asignado_a, {svc._ASIGNADO_NOMBRE} AS nombre
        FROM conversations c
        LEFT JOIN portal_users pa ON pa.id = c.asignado_a
        WHERE c.id = $1 AND c.tenant_id = $2
        """,
        conversacion_id,
        tenant_id,
    )
    if fila is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversación no encontrada")
    if (
        fila["asignado_a"] is not None
        and fila["asignado_a"] != usuario.id
        and usuario.role not in ROLES_GERENCIA
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"La conversación la tiene {fila['nombre']}",
        )


def _filtro_asignada(asignada: str | None) -> str | None:
    """'mias' | 'sin_asignar' | uuid de una persona. Otra cosa: 422."""
    if asignada is None or asignada in ("mias", "sin_asignar"):
        return asignada
    try:
        return str(UUID(asignada))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'asignada' debe ser 'mias', 'sin_asignar' o el id de una persona",
        )


@router.get("", response_model=list[ConversacionOut])
async def listar(
    tenant_id: UUID = Depends(tenant_actual),
    canal: str | None = Query(None),
    estado: str | None = Query(None),
    buscar: str | None = Query(None, max_length=100),
    asignada: str | None = Query(None, max_length=36),
    limite: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    usuario: UsuarioActual = Depends(usuario_actual),
):
    filas = await svc.listar(
        tenant_id,
        canal,
        estado,
        buscar,
        limite,
        offset,
        alcance=await _alcance(usuario),
        asignada=_filtro_asignada(asignada),
        yo=usuario.id,
    )
    return [ConversacionOut(**dict(f)) for f in filas]


@router.get("/asignables", response_model=list[AsignableOut])
async def listar_asignables(
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
):
    """Cuentas del negocio a las que se les puede asignar una conversación (solo gerencia)."""
    return [AsignableOut(**dict(f)) for f in await asig.asignables(tenant_id)]


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
    __: None = Depends(puede_operar),
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
    __: None = Depends(puede_operar),
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
    usuario: UsuarioActual = Depends(usuario_actual),
    tenant_id: UUID = Depends(tenant_actual),
    _: None = Depends(conversacion_visible),
):
    """
    Un humano toma el control y la conversación pasa a ser suya: la IA deja de
    contestar (inversa de volver-a-ia). Vale si está sin asignar; si ya la
    tiene otro, 409 — solo el owner se la quita (PUT/DELETE .../asignacion).
    """
    fila = await asig.tomar(tenant_id, conversacion_id, usuario.id)
    await emit_conversacion_estado(tenant_id, conversacion_id, fila["status"], fila["asignado_a"])
    return ConversacionEstadoOut(**dict(fila))


@router.post("/{conversacion_id}/volver-a-ia", response_model=ConversacionEstadoOut)
async def volver_a_ia(
    conversacion_id: UUID,
    usuario: UsuarioActual = Depends(usuario_actual),
    tenant_id: UUID = Depends(tenant_actual),
    _: None = Depends(conversacion_visible),
    __: None = Depends(puede_operar),
):
    """Le devuelve el control a la IA (Escenario 4) y termina la asignación."""
    previo = (await fetch_one(
        "SELECT asignado_a FROM conversations WHERE id = $1 AND tenant_id = $2",
        conversacion_id,
        tenant_id,
    ))
    fila = await svc.volver_a_ia(tenant_id, conversacion_id, usuario.id)
    await emit_conversacion_estado(
        tenant_id, conversacion_id, fila["status"], None,
        extra_usuarios=[previo["asignado_a"]] if previo else (),
    )
    return ConversacionEstadoOut(**dict(fila))


@router.put("/{conversacion_id}/asignacion", response_model=ConversacionEstadoOut)
async def asignar(
    conversacion_id: UUID,
    datos: AsignarConversacionIn,
    usuario: UsuarioActual = Depends(gerencia_actual),
    tenant_id: UUID = Depends(tenant_actual),
):
    """
    El owner (o superadmin) asigna o reasigna la conversación a una cuenta de
    `ROLES_ASIGNABLES`. Apaga la IA. Al asignado le llega una alerta personal;
    al anterior, el aviso de que ya no es suya.
    """
    fila, anterior = await asig.asignar(
        tenant_id, conversacion_id, datos.asignado_a, usuario.id, datos.nota
    )
    await emit_conversacion_estado(
        tenant_id, conversacion_id, fila["status"], fila["asignado_a"], extra_usuarios=[anterior]
    )
    if datos.asignado_a != usuario.id:
        await avisar_a_usuario(
            tenant_id,
            datos.asignado_a,
            "conversacion_asignada",
            "Te asignaron una conversación",
            "Tienes un cliente esperando atención" + (f" — {datos.nota}" if datos.nota else ""),
            {"conversation_id": str(conversacion_id)},
        )
    return ConversacionEstadoOut(**dict(fila))


@router.delete("/{conversacion_id}/asignacion", response_model=ConversacionEstadoOut)
async def soltar(
    conversacion_id: UUID,
    usuario: UsuarioActual = Depends(usuario_actual),
    tenant_id: UUID = Depends(tenant_actual),
    _: None = Depends(conversacion_visible),
):
    """
    Deja la conversación sin asignar (sigue en modo humano). El owner se la
    quita a quien sea; el asignado puede soltar la suya.
    """
    fila, anterior = await asig.liberar(
        tenant_id, conversacion_id, usuario.id, es_gerencia=usuario.role in ROLES_GERENCIA
    )
    await emit_conversacion_estado(
        tenant_id, conversacion_id, fila["status"], None, extra_usuarios=[anterior]
    )
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
