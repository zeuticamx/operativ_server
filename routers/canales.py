"""
Conexión de canales.

Facebook / Instagram (OAuth con Meta):
  1. El frontend abre el popup de Facebook Login for Business
  2. Meta devuelve un `code`
  3. POST /canales/meta/conectar  → guarda el user token de larga duración
  4. GET  /canales/meta/paginas   → lista lo que el usuario autorizó
  5. POST /canales/meta/activar   → guarda page tokens y suscribe webhooks

WhatsApp (hoy vía Kontesta, ver services/whatsapp.py):
  No hay OAuth. La cuenta de Kontesta es la de OperativAI y el número del
  negocio se da de alta como una línea dentro de ella, fuera del portal;
  POST /canales/whatsapp/conectar solo registra qué línea es de qué tenant.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from config import settings
from deps import tenant_actual
from session import execute, fetch_all, fetch_one
from schemas import (
    ActivarCanalesIn,
    CanalOut,
    ConectarMetaIn,
    ConectarWhatsAppIn,
    PaginaDisponible,
)
from services import meta

router = APIRouter(prefix="/canales", tags=["canales"])


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
async def listar_paginas(tenant_id: UUID = Depends(tenant_actual)):
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
            "SELECT set_channel_credentials($1, 'facebook', $2, NULL, $3, NULL)",
            tenant_id,
            page_token,
            page_id,
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
                "SELECT set_channel_credentials($1, 'instagram', $2, NULL, $3, $4)",
                tenant_id,
                page_token,
                page_id,
                ig["id"],
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
# WhatsApp (vía Kontesta)
# ============================================================
@router.post("/whatsapp/conectar")
async def conectar_whatsapp(
    datos: ConectarWhatsAppIn,
    tenant_id: UUID = Depends(tenant_actual),
):
    """
    Registra qué línea de Kontesta corresponde a este tenant.

    A diferencia de Meta no hay OAuth ni token que guardar: la API key de
    Kontesta es una sola para toda la plataforma (KONTESTA_API_KEY) y el
    número del negocio ya vive como línea dentro de esa cuenta. Lo que hace
    falta guardar es la correspondencia línea → tenant, que es de donde n8n
    saca el tenant de cada mensaje entrante.

    ⚠️ No se verifica contra Kontesta que la línea exista ni que sea de este
    negocio: la API de Kontesta no documenta un endpoint para consultarlas.
    Mientras tanto el alta es declarativa y el único control es que nadie
    pueda reclamar una línea que otro tenant ya tiene registrada.
    """
    # channel_credentials es tabla del lado de n8n, así que la unicidad de
    # la línea se comprueba acá y no con un índice: sin esto, un tenant
    # podría reclamar el número de otro y quedarse con su conversación.
    # Queda una ventana de carrera mínima entre el SELECT y el INSERT; el
    # arreglo de fondo es un índice único parcial, que le toca a quien sea
    # dueño del esquema de n8n.
    ocupada = await fetch_one(
        """
        SELECT 1 FROM channel_credentials
        WHERE channel_type = 'whatsapp'
          AND phone_number_id = $1
          AND is_active
          AND tenant_id <> $2
        """,
        datos.phone_number_id,
        tenant_id,
    )
    if ocupada is not None:
        # Sin decir de quién es: mismo criterio que el 404 de tenant ajeno,
        # no confirmar qué otras cuentas existen.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ese número ya está conectado en otra cuenta",
        )

    # access_token va en NULL a propósito: con Kontesta no hay credencial
    # por tenant que cifrar, solo el id de la línea.
    await fetch_one(
        "SELECT set_channel_credentials($1, 'whatsapp', NULL, $2, NULL, NULL)",
        tenant_id,
        datos.phone_number_id,
    )
    await execute(
        """
        INSERT INTO tenant_channels (tenant_id, channel_type)
        VALUES ($1, 'whatsapp')
        ON CONFLICT (tenant_id, channel_type) DO UPDATE SET is_active = true
        """,
        tenant_id,
    )

    return {"conectado": True, "phone_number_id": datos.phone_number_id}


# ============================================================
# Desconectar
# ============================================================
@router.delete("/{channel_type}")
async def desconectar_canal(
    channel_type: str,
    tenant_id: UUID = Depends(tenant_actual),
):
    if channel_type not in ("facebook", "instagram", "whatsapp"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Canal no válido: {channel_type}",
        )

    await execute(
        """
        UPDATE channel_credentials SET is_active = false
        WHERE tenant_id = $1 AND channel_type = $2
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
    return {"desconectado": channel_type}
