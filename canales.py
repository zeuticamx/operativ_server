"""
Conexión de canales con Meta.

Flujo:
  1. El frontend abre el popup de Facebook Login for Business
  2. Meta devuelve un `code`
  3. POST /canales/meta/conectar  → guarda el user token de larga duración
  4. GET  /canales/meta/paginas   → lista lo que el usuario autorizó
  5. POST /canales/meta/activar   → guarda page tokens y suscribe webhooks
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
    PaginaDisponible,
)
import meta

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
