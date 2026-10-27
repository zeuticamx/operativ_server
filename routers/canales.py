"""
Conexión de canales.

Facebook / Instagram (OAuth con Meta):
  1. El frontend abre el popup de Facebook Login for Business
  2. Meta devuelve un `code`
  3. POST /canales/meta/conectar  → guarda el user token de larga duración
  4. GET  /canales/meta/paginas   → lista lo que el usuario autorizó
  5. POST /canales/meta/activar   → guarda page tokens y suscribe webhooks

Instagram Direct (vía NeuroAPI Connect Sessions, el alta visible en el portal):
  POST /canales/instagram/neuroapi/iniciar abre la sesión; el webhook de
  routers/canales_neuroapi_webhook.py la cierra; GET .../estado lo consulta
  el portal al volver. El OAuth de Meta de arriba queda para el día que Meta
  sea el proveedor directo.

WhatsApp (vía NeuroAPI Connect Sessions, ver services/whatsapp.py):
  POST /canales/whatsapp/neuroapi/iniciar abre el Embedded Signup; el webhook
  de routers/canales_neuroapi_webhook.py vincula el número al cerrar la sesión.
"""

import json
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from config import settings
from deps import (
    UsuarioActual,
    gerencia_actual,
    negocio_actual,
    requiere_herramienta,
    tenant_actual,
)
from session import conexion, execute, fetch_all, fetch_one, fetch_value
from schemas import (
    ActivarCanalesIn,
    CanalOut,
    ConectarMetaIn,
    ConectarWhatsAppIn,
    ElegirPrincipalIn,
    EstadoInstagramNeuroApiOut,
    EstadoNeuroApiConnectOut,
    IniciarNeuroApiConnectIn,
    IniciarNeuroApiConnectOut,
    ListaNumerosOut,
    NumeroWhatsAppOut,
    PaginaDisponible,
    PausarNumeroOut,
)
from services import instagram_neuroapi, meta, neuroapi_connect
from services import herramientas_plan
from services import numeros_whatsapp as numeros
from services.numeros_bitacora import Autor
from services.numeros_reglas import configuracion_incompleta

log = logging.getLogger("operativai.canales")

router = APIRouter(
    prefix="/canales",
    tags=["canales"],
    # Ver el estado de los canales: todo el equipo del negocio. Conectar,
    # activar o desconectar: solo gerencia (cada endpoint lo pide).
    dependencies=[Depends(negocio_actual), Depends(requiere_herramienta("agente"))],
)


# ============================================================
# Estado actual
# ============================================================
@router.get("", response_model=list[CanalOut])
async def listar_canales(tenant_id: UUID = Depends(tenant_actual)):
    filas = await fetch_all(
        """
        SELECT channel_type, page_id, ig_user_id, phone_number_id,
               is_active, updated_at
        FROM v_tenant_channels
        WHERE tenant_id = $1
        ORDER BY channel_type
        """,
        tenant_id,
    )
    return [CanalOut(**dict(f)) for f in filas]


# ============================================================
# Paso 1: guardar el user token
# ============================================================
@router.post("/meta/conectar")
async def conectar_meta(
    datos: ConectarMetaIn,
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
):
    redirect_uri = datos.redirect_uri or settings.META_REDIRECT_URI
    if not redirect_uri:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Falta redirect_uri (ni en la petición ni en META_REDIRECT_URI)",
        )

    try:
        corto = await meta.intercambiar_code(datos.code, redirect_uri)
        largo = await meta.token_larga_duracion(corto["access_token"])
        info = await meta.info_token(largo["access_token"])
    except meta.MetaError as e:
        raise meta.a_http(e)

    await fetch_one(
        "SELECT set_meta_connection($1, $2, $3, $4, $5)",
        tenant_id,
        info.get("user_id"),
        largo["access_token"],
        meta.expira_en(largo.get("expires_in")),
        info.get("scopes"),
    )

    return {
        "conectado": True,
        "scopes": info.get("scopes", []),
        "expira": meta.expira_en(largo.get("expires_in")),
    }


# ============================================================
# Paso 2: qué páginas autorizó
# ============================================================
@router.get("/meta/paginas", response_model=list[PaginaDisponible])
async def listar_paginas(
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
):
    conexion = await fetch_one("SELECT * FROM get_meta_connection($1)", tenant_id)
    if conexion is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Todavía no has conectado tu cuenta de Meta",
        )

    try:
        paginas = await meta.listar_paginas(conexion["user_token"])
    except meta.MetaError as e:
        raise meta.a_http(e)

    ya = {
        f["page_id"]
        for f in await fetch_all(
            "SELECT page_id FROM v_tenant_channels WHERE tenant_id = $1 AND page_id IS NOT NULL",
            tenant_id,
        )
    }

    salida = []
    for p in paginas:
        ig = p.get("instagram_business_account") or {}
        salida.append(
            PaginaDisponible(
                page_id=p["id"],
                nombre=p.get("name", "(sin nombre)"),
                ig_user_id=ig.get("id"),
                ig_username=ig.get("username"),
                ya_conectada=p["id"] in ya,
            )
        )
    return salida


# ============================================================
# Paso 3: activar las páginas elegidas
# ============================================================
@router.post("/meta/activar")
async def activar_canales(
    datos: ActivarCanalesIn,
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
):
    conexion = await fetch_one("SELECT * FROM get_meta_connection($1)", tenant_id)
    if conexion is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Todavía no has conectado tu cuenta de Meta",
        )

    try:
        paginas = await meta.listar_paginas(conexion["user_token"])
    except meta.MetaError as e:
        raise meta.a_http(e)

    # Para que obtiene_app_secret (sql/53) pueda validar la firma HMAC de los
    # webhooks de Facebook/Instagram: sin esto channel_credentials.meta_app_id
    # queda NULL y Meta reenviando mensajes reales se rechaza con 403.
    meta_app_id = await fetch_value(
        "SELECT resolver_meta_app($1, $2)", settings.META_APP_ID, settings.META_APP_SECRET
    )

    por_id = {p["id"]: p for p in paginas}
    resultados = []

    for page_id in datos.page_ids:
        pagina = por_id.get(page_id)
        if pagina is None:
            resultados.append(
                {"page_id": page_id, "ok": False, "detalle": "No autorizada"}
            )
            continue

        page_token = pagina["access_token"]
        ig = pagina.get("instagram_business_account") or {}

        try:
            await meta.suscribir_app_a_pagina(page_id, page_token)
        except meta.MetaError as e:
            resultados.append(
                {"page_id": page_id, "ok": False, "detalle": e.mensaje}
            )
            continue

        # Facebook Messenger
        await fetch_one(
            "SELECT set_channel_credentials($1, 'facebook', $2, NULL, $3, NULL, $4)",
            tenant_id,
            page_token,
            page_id,
            meta_app_id,
        )
        await execute(
            """
            INSERT INTO tenant_channels (tenant_id, channel_type)
            VALUES ($1, 'facebook')
            ON CONFLICT (tenant_id, channel_type) DO UPDATE SET is_active = true
            """,
            tenant_id,
        )

        canales = ["facebook"]

        # Instagram, solo si la página tiene cuenta vinculada
        if ig.get("id"):
            await fetch_one(
                "SELECT set_channel_credentials($1, 'instagram', $2, NULL, $3, $4, $5)",
                tenant_id,
                page_token,
                page_id,
                ig["id"],
                meta_app_id,
            )
            await execute(
                """
                INSERT INTO tenant_channels (tenant_id, channel_type)
                VALUES ($1, 'instagram')
                ON CONFLICT (tenant_id, channel_type) DO UPDATE SET is_active = true
                """,
                tenant_id,
            )
            canales.append("instagram")

        resultados.append(
            {
                "page_id": page_id,
                "nombre": pagina.get("name"),
                "ok": True,
                "canales": canales,
            }
        )

    return {"resultados": resultados}


# ============================================================
# Varios números de WhatsApp por negocio (spec 005a, T5)
# ============================================================
def _numero_out(fila, herramientas: list[str] | None = None) -> NumeroWhatsAppOut:
    incompleta = configuracion_incompleta(fila["agent_name"], fila["system_prompt"])
    if herramientas is None and "herramientas" in fila:
        herramientas = list(fila["herramientas"] or [])
    return NumeroWhatsAppOut(
        id=fila["id"],
        phone_number_id=fila["phone_number_id"],
        telefono_visible=fila["telefono_visible"],
        bsp_provider=fila["bsp_provider"],
        estado=fila["estado"],
        es_principal=bool(fila["es_principal"]),
        es_linea_avisos=bool(fila["es_linea_avisos"]),
        vinculado_en=fila["vinculado_en"],
        pausado_en=fila["pausado_en"],
        motivo_pausa=fila["motivo_pausa"],
        agent_name=fila["agent_name"] or "",
        system_prompt=fila["system_prompt"] or "",
        model=fila["model"],
        temperature=float(fila["temperature"]),
        history_window=fila["history_window"],
        agente_activo=bool(fila["agente_activo"]),
        configuracion_incompleta=incompleta,
        herramientas=herramientas or [],
        connection_mode=(fila["connection_mode"] if "connection_mode" in fila else None) or "standard",
    )


@router.get("/whatsapp/numeros", response_model=ListaNumerosOut)
async def listar_numeros(tenant_id: UUID = Depends(tenant_actual)):
    """Números vinculados con plazas del cupo (RF-18: vendedor y proveedor reciben 403)."""
    async with conexion() as conn:
        lista = await numeros.listar(conn, tenant_id)
        coexistencia = await numeros.coexistencia_permitida(conn, tenant_id)
    return ListaNumerosOut(
        numeros=[_numero_out(f) for f in lista.numeros],
        tope=lista.tope,
        plazas_usadas=lista.plazas_usadas,
        plazas_libres=lista.plazas_libres,
        coexistencia_permitida=coexistencia,
        puede_elegir_avisos=numeros.tenant_de_avisos() == tenant_id,
    )


@router.post("/whatsapp/numeros/{numero_id}/pausar", response_model=PausarNumeroOut)
async def pausar_numero(
    numero_id: UUID,
    tenant_id: UUID = Depends(tenant_actual),
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    async with conexion() as conn:
        resultado = await numeros.pausar_manual(
            conn, tenant_id, numero_id, Autor.gerencia(usuario.id, usuario.email)
        )
        herramientas = await herramientas_plan.herramientas_de_numero(conn, tenant_id, numero_id)
    return PausarNumeroOut(
        numero=_numero_out(resultado.numero, herramientas),
        sin_numero_activo=resultado.sin_numero_activo,
    )


@router.post("/whatsapp/numeros/{numero_id}/reactivar", response_model=NumeroWhatsAppOut)
async def reactivar_numero(
    numero_id: UUID,
    tenant_id: UUID = Depends(tenant_actual),
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    async with conexion() as conn:
        fila = await numeros.reactivar(
            conn, tenant_id, numero_id, Autor.gerencia(usuario.id, usuario.email)
        )
        herramientas = await herramientas_plan.herramientas_de_numero(conn, tenant_id, numero_id)
    return _numero_out(fila, herramientas)


@router.delete("/whatsapp/numeros/{numero_id}", status_code=status.HTTP_204_NO_CONTENT)
async def desvincular_numero(
    numero_id: UUID,
    tenant_id: UUID = Depends(tenant_actual),
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    async with conexion() as conn:
        await numeros.desvincular(
            conn, tenant_id, numero_id, Autor.gerencia(usuario.id, usuario.email)
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/whatsapp/principal", response_model=NumeroWhatsAppOut)
async def elegir_principal(
    datos: ElegirPrincipalIn,
    tenant_id: UUID = Depends(tenant_actual),
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    async with conexion() as conn:
        fila = await numeros.elegir_principal(
            conn, tenant_id, datos.numero_id, Autor.gerencia(usuario.id, usuario.email)
        )
        herramientas = await herramientas_plan.herramientas_de_numero(conn, tenant_id, datos.numero_id)
    return _numero_out(fila, herramientas)


@router.put("/whatsapp/avisos", response_model=NumeroWhatsAppOut)
async def elegir_linea_avisos(
    datos: ElegirPrincipalIn,
    tenant_id: UUID = Depends(tenant_actual),
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    """
    Cambia cuál de los números vinculados y activos del negocio que aloja los
    avisos de la plataforma es la línea de avisos. 403 `linea_avisos_no_disponible`
    en cualquier otro negocio.
    """
    async with conexion() as conn:
        fila = await numeros.elegir_linea_avisos(
            conn, tenant_id, datos.numero_id, Autor.gerencia(usuario.id, usuario.email)
        )
        herramientas = await herramientas_plan.herramientas_de_numero(conn, tenant_id, datos.numero_id)
    return _numero_out(fila, herramientas)


# ============================================================
# WhatsApp (alta manual por id de línea, sin proveedor Kontesta)
# ============================================================
@router.post("/whatsapp/conectar", status_code=status.HTTP_201_CREATED)
async def conectar_whatsapp(
    datos: ConectarWhatsAppIn,
    tenant_id: UUID = Depends(tenant_actual),
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    """
    Da de alta un número (spec 005a RF-2): 429 si el negocio agotó sus
    intentos, 402 si el cupo está lleno, 409 si ya es propio, 404 si es
    ajeno, 201 si estaba libre.
    """
    async with conexion() as conn:
        fila = await numeros.vincular(
            conn,
            tenant_id,
            datos.phone_number_id,
            Autor.gerencia(usuario.id, usuario.email),
            bsp_provider="kontesta",
        )
        herramientas = await herramientas_plan.herramientas_de_numero(conn, tenant_id, fila["id"])
    return _numero_out(fila, herramientas)


# ============================================================
# WhatsApp (vía NeuroAPI Connect Sessions — Embedded Signup)
# ============================================================
@router.post("/whatsapp/neuroapi/iniciar", response_model=IniciarNeuroApiConnectOut)
async def iniciar_neuroapi_connect(
    datos_entrada: IniciarNeuroApiConnectIn | None = None,
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
):
    """
    Crea una NeuroAPI Connect Session y devuelve la URL a la que el frontend
    debe redirigir para que el negocio autorice su propia cuenta de WhatsApp
    Business (Embedded Signup de Meta, mediado por NeuroAPI). El resultado
    real de la vinculación llega después, por el webhook de
    /whatsapp/neuroapi/webhook — acá solo se abre el trámite.

    `connection_mode` (cuerpo opcional, por defecto `standard`) dice si el
    número queda solo para la API o también en la app de WhatsApp Business
    (`coexistence`). La coexistencia depende del plan: 402
    `coexistencia_no_incluida_en_plan` si el vigente no la incluye.
    """
    modo = datos_entrada.connection_mode if datos_entrada else "standard"

    if not settings.neuroapi_connect_configurado:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="La vinculación automática de WhatsApp no está configurada",
        )

    # RF-2 pasos 1 y 2 antes de abrir la sesión: con bloqueo o cupo lleno no
    # se abre el trámite (el resultado final se consulta en .../neuroapi/estado).
    async with conexion() as conn:
        await numeros.comprobar_alta_previa(conn, tenant_id)
        if modo == "coexistence":
            await numeros.exigir_coexistencia(conn, tenant_id)

    datos = await neuroapi_connect.crear_connect_session(tenant_id, modo)
    connect_url = datos.get("url") or datos.get("connect_url")
    session_id = datos.get("session_id") or datos.get("id")
    if not connect_url or not session_id:
        log.error(
            "Respuesta de NeuroAPI sin los campos esperados (tenant=%s): %s",
            tenant_id, datos,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="NeuroAPI no devolvió una sesión válida",
        )

    # `status` arranca en su valor por defecto, 'pendiente': el resultado real
    # lo escribe el webhook. La fila no se puede guardar antes de la llamada a
    # NeuroAPI porque `session_id` solo se conoce con su respuesta.
    await execute(
        "INSERT INTO neuroapi_connect_sessions (tenant_id, session_id, connection_mode) "
        "VALUES ($1, $2, $3)",
        tenant_id,
        session_id,
        modo,
    )

    return IniciarNeuroApiConnectOut(connect_url=connect_url)


@router.get("/whatsapp/neuroapi/estado", response_model=EstadoNeuroApiConnectOut)
async def estado_neuroapi_connect(
    session_id: str = Query(..., min_length=1, max_length=200),
    tenant_id: UUID = Depends(tenant_actual),
):
    """Resultado del alta por NeuroAPI: estado de la sesión y código RF-2 si falló."""
    fila = await fetch_one(
        "SELECT session_id, status, detalle, connection_mode FROM neuroapi_connect_sessions "
        "WHERE session_id = $1 AND tenant_id = $2",
        session_id,
        tenant_id,
    )
    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sesión no encontrada",
        )
    resultado = codigo = phone_number_id = None
    try:
        datos = json.loads(fila["detalle"]) if fila["detalle"] else None
    except (ValueError, TypeError):
        datos = None
    if isinstance(datos, dict):
        resultado = datos.get("resultado")
        codigo = datos.get("codigo")
        phone_number_id = datos.get("phone_number_id")
    return EstadoNeuroApiConnectOut(
        session_id=fila["session_id"],
        estado=fila["status"],
        connection_mode=fila["connection_mode"],
        resultado=resultado,
        codigo=codigo,
        phone_number_id=phone_number_id,
    )


# ============================================================
# Instagram Direct (vía NeuroAPI Connect Sessions)
# ============================================================
@router.post("/instagram/neuroapi/iniciar", response_model=IniciarNeuroApiConnectOut)
async def iniciar_instagram_neuroapi(
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
):
    """
    Abre una Connect Session de Instagram Direct y devuelve la URL a la que
    el portal redirige para que el negocio autorice su cuenta profesional en
    Meta. Igual que WhatsApp: el resultado llega después por el webhook.

    Sin cupo por plan. 429 `demasiados_intentos` si el negocio acumula cinco
    altas de cuentas o números ajenos en 24 h (el bloqueo es compartido).
    """
    if not settings.instagram_neuroapi_configurado:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="La vinculación de Instagram no está configurada",
        )

    async with conexion() as conn:
        await instagram_neuroapi.comprobar_alta_previa(conn, tenant_id)

    datos = await neuroapi_connect.crear_connect_session(tenant_id, canal="instagram")
    connect_url = datos.get("url") or datos.get("connect_url")
    session_id = datos.get("session_id") or datos.get("id")
    if not connect_url or not session_id:
        log.error(
            "Respuesta de NeuroAPI sin los campos esperados (tenant=%s, instagram): %s",
            tenant_id, datos,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="NeuroAPI no devolvió una sesión válida",
        )

    await execute(
        "INSERT INTO neuroapi_connect_sessions (tenant_id, session_id, canal) "
        "VALUES ($1, $2, 'instagram')",
        tenant_id,
        session_id,
    )
    return IniciarNeuroApiConnectOut(connect_url=connect_url)


@router.get("/instagram/neuroapi/estado", response_model=EstadoInstagramNeuroApiOut)
async def estado_instagram_neuroapi(
    session_id: str | None = Query(None, min_length=1, max_length=200),
    tenant_id: UUID = Depends(tenant_actual),
):
    """
    Una sesión de Instagram del negocio. Con `session_id` (NeuroAPI lo agrega
    al retorno: `?status=success&session_id=…`) esa sesión; sin él, la última.
    404 si no existe, no es de este negocio o el negocio nunca abrió una.
    """
    fila = await fetch_one(
        """
        SELECT session_id, status, detalle FROM neuroapi_connect_sessions
         WHERE tenant_id = $1 AND canal = 'instagram'
           AND ($2::text IS NULL OR session_id = $2)
         ORDER BY created_at DESC
         LIMIT 1
        """,
        tenant_id,
        session_id,
    )
    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sesión no encontrada",
        )
    try:
        datos = json.loads(fila["detalle"]) if fila["detalle"] else None
    except (ValueError, TypeError):
        datos = None
    if not isinstance(datos, dict):
        datos = {}
    return EstadoInstagramNeuroApiOut(
        session_id=fila["session_id"],
        estado=fila["status"],
        resultado=datos.get("resultado"),
        codigo=datos.get("codigo"),
        username=datos.get("username"),
    )


# ============================================================
# Desconectar
# ============================================================
@router.delete("/{channel_type}")
async def desconectar_canal(
    channel_type: str,
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
):
    # Spec 005a: WhatsApp se gestiona por número (pausar/desvincular en
    # /canales/whatsapp/numeros); la baja en bloque ya no existe.
    if channel_type == "whatsapp":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "codigo": "usar_numeros",
                "mensaje": "WhatsApp se gestiona número por número: pausa o desvincula cada número desde la lista de números.",
            },
        )
    if channel_type not in ("facebook", "instagram"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Canal no válido: {channel_type}",
        )

    # bsp_provider vuelve en la respuesta porque NeuroAPI no tiene endpoint de
    # baja: desactivar aquí basta para que el proxy deje de reenviar y n8n de
    # contestar, pero la línea sigue vinculada a la app hasta que el negocio
    # retire el acceso en su Meta Business Manager, y el portal se lo avisa.
    fila = await fetch_one(
        """
        UPDATE channel_credentials SET is_active = false
        WHERE tenant_id = $1 AND channel_type = $2
        RETURNING bsp_provider
        """,
        tenant_id,
        channel_type,
    )
    await execute(
        """
        UPDATE tenant_channels SET is_active = false
        WHERE tenant_id = $1 AND channel_type = $2
        """,
        tenant_id,
        channel_type,
    )
    return {"desconectado": channel_type, "proveedor": fila["bsp_provider"] if fila else None}
