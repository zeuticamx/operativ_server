"""
Herramientas por tenant.
"""

import re
import unicodedata
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool

from deps import UsuarioActual, tenant_actual, usuario_actual
from session import execute, fetch_all, fetch_one
from schemas import (
    ActualizarHerramientaIn,
    ConectarGoogleDocIn,
    ConectarGoogleSheetIn,
    HerramientaInfoOut,
    HerramientaOut,
)
from services import google_tools

router = APIRouter(prefix="/herramientas", tags=["herramientas"])


@router.get("/info", response_model=HerramientaInfoOut)
async def info(usuario: UsuarioActual = Depends(usuario_actual)):
    try:
        correo = google_tools.correo_cuenta_servicio()
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    return HerramientaInfoOut(correo_servicio=correo)


@router.get("", response_model=list[HerramientaOut])
async def listar(tenant_id: UUID = Depends(tenant_actual)):
    filas = await fetch_all(
        """
        SELECT tool_key, tool_type, display_name, description,
               is_enabled, last_verified_at, config
        FROM tenant_tools
        WHERE tenant_id = $1
        ORDER BY created_at DESC
        """,
        tenant_id,
    )
    return [
        HerramientaOut(
            tool_key=f["tool_key"],
            tool_type=f["tool_type"],
            display_name=f["display_name"],
            description=f["description"],
            is_enabled=f["is_enabled"],
            last_verified_at=f["last_verified_at"],
            nombre_documento=(f["config"] or {}).get("nombre_documento"),
            url_original=(f["config"] or {}).get("url_original"),
        )
        for f in filas
    ]


def _slug(texto: str) -> str:
    sin_acentos = (
        unicodedata.normalize("NFKD", texto)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    limpio = re.sub(r"[^a-zA-Z0-9]+", "_", sin_acentos).strip("_").lower()
    return limpio[:80] or "herramienta"


async def _tool_key_disponible(tenant_id: UUID, base: str) -> str:
    candidato = base
    intento = 1
    while True:
        existe = await fetch_one(
            "SELECT 1 FROM tenant_tools WHERE tenant_id = $1 AND tool_key = $2",
            tenant_id,
            candidato,
        )
        if not existe:
            return candidato
        intento += 1
        candidato = f"{base}_{intento}"


@router.post("/google-sheets", response_model=HerramientaOut, status_code=201)
async def conectar_sheet(
    datos: ConectarGoogleSheetIn,
    tenant_id: UUID = Depends(tenant_actual),
):
    try:
        spreadsheet_id = google_tools.extraer_id_de_url(datos.url)
    except google_tools.UrlInvalidaError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.mensaje)

    try:
        # googleapiclient es síncrono y hace red: fuera del event loop.
        info = await run_in_threadpool(google_tools.validar_sheet, spreadsheet_id)
    except google_tools.ToolAccessError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=e.mensaje)

    tool_key = await _tool_key_disponible(tenant_id, _slug(datos.display_name))

    config = {
        "spreadsheet_id": spreadsheet_id,
        "range": datos.rango,
        "nombre_documento": info["titulo"],
        "hojas_disponibles": info["hojas"],
        "url_original": datos.url,
    }

    fila = await fetch_one(
        """
        INSERT INTO tenant_tools
            (tenant_id, tool_key, tool_type, display_name, description,
             parametros_schema, config, is_enabled, last_verified_at)
        VALUES ($1, $2, 'google_sheets', $3, $4, '{}'::jsonb, $5::jsonb, true, NOW())
        RETURNING tool_key, tool_type, display_name, description,
                  is_enabled, last_verified_at, config
        """,
        tenant_id,
        tool_key,
        datos.display_name,
        datos.description,
        config,
    )

    return HerramientaOut(
        tool_key=fila["tool_key"],
        tool_type=fila["tool_type"],
        display_name=fila["display_name"],
        description=fila["description"],
        is_enabled=fila["is_enabled"],
        last_verified_at=fila["last_verified_at"],
        nombre_documento=info["titulo"],
        url_original=datos.url,
    )


@router.post("/google-docs", response_model=HerramientaOut, status_code=201)
async def conectar_doc(
    datos: ConectarGoogleDocIn,
    tenant_id: UUID = Depends(tenant_actual),
):
    try:
        document_id = google_tools.extraer_id_de_url(datos.url)
    except google_tools.UrlInvalidaError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.mensaje)

    try:
        info = await run_in_threadpool(google_tools.validar_doc, document_id)
    except google_tools.ToolAccessError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=e.mensaje)

    tool_key = await _tool_key_disponible(tenant_id, _slug(datos.display_name))

    config = {
        "document_id": document_id,
        "nombre_documento": info["titulo"],
        "url_original": datos.url,
    }

    fila = await fetch_one(
        """
        INSERT INTO tenant_tools
            (tenant_id, tool_key, tool_type, display_name, description,
             parametros_schema, config, is_enabled, last_verified_at)
        VALUES ($1, $2, 'google_docs', $3, $4, '{}'::jsonb, $5::jsonb, true, NOW())
        RETURNING tool_key, tool_type, display_name, description,
                  is_enabled, last_verified_at, config
        """,
        tenant_id,
        tool_key,
        datos.display_name,
        datos.description,
        config,
    )

    return HerramientaOut(
        tool_key=fila["tool_key"],
        tool_type=fila["tool_type"],
        display_name=fila["display_name"],
        description=fila["description"],
        is_enabled=fila["is_enabled"],
        last_verified_at=fila["last_verified_at"],
        nombre_documento=info["titulo"],
        url_original=datos.url,
    )


@router.post("/{tool_key}/verificar", response_model=HerramientaOut)
async def verificar(tool_key: str, tenant_id: UUID = Depends(tenant_actual)):
    fila = await fetch_one(
        """
        SELECT tool_type, display_name, description, is_enabled, config
        FROM tenant_tools
        WHERE tenant_id = $1 AND tool_key = $2
        """,
        tenant_id,
        tool_key,
    )
    if fila is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Herramienta no encontrada")

    config = fila["config"] or {}

    try:
        if fila["tool_type"] == "google_sheets":
            info = await run_in_threadpool(
                google_tools.validar_sheet, config["spreadsheet_id"]
            )
            config["hojas_disponibles"] = info["hojas"]
        elif fila["tool_type"] == "google_docs":
            info = await run_in_threadpool(
                google_tools.validar_doc, config["document_id"]
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Este tipo de herramienta no se verifica por esta vía",
            )
    except google_tools.ToolAccessError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=e.mensaje)

    config["nombre_documento"] = info["titulo"]

    actualizada = await fetch_one(
        """
        UPDATE tenant_tools
        SET config = $3::jsonb, last_verified_at = NOW(), updated_at = NOW()
        WHERE tenant_id = $1 AND tool_key = $2
        RETURNING tool_key, tool_type, display_name, description,
                  is_enabled, last_verified_at, config
        """,
        tenant_id,
        tool_key,
        config,
    )

    return HerramientaOut(
        tool_key=actualizada["tool_key"],
        tool_type=actualizada["tool_type"],
        display_name=actualizada["display_name"],
        description=actualizada["description"],
        is_enabled=actualizada["is_enabled"],
        last_verified_at=actualizada["last_verified_at"],
        nombre_documento=config.get("nombre_documento"),
        url_original=config.get("url_original"),
    )


@router.patch("/{tool_key}", response_model=HerramientaOut)
async def actualizar(
    tool_key: str,
    datos: ActualizarHerramientaIn,
    tenant_id: UUID = Depends(tenant_actual),
):
    fila = await fetch_one(
        """
        UPDATE tenant_tools SET
            display_name = COALESCE($3, display_name),
            description  = COALESCE($4, description),
            is_enabled   = COALESCE($5, is_enabled),
            updated_at   = NOW()
        WHERE tenant_id = $1 AND tool_key = $2
        RETURNING tool_key, tool_type, display_name, description,
                  is_enabled, last_verified_at, config
        """,
        tenant_id,
        tool_key,
        datos.display_name,
        datos.description,
        datos.is_enabled,
    )
    if fila is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Herramienta no encontrada")

    config = fila["config"] or {}
    return HerramientaOut(
        tool_key=fila["tool_key"],
        tool_type=fila["tool_type"],
        display_name=fila["display_name"],
        description=fila["description"],
        is_enabled=fila["is_enabled"],
        last_verified_at=fila["last_verified_at"],
        nombre_documento=config.get("nombre_documento"),
        url_original=config.get("url_original"),
    )


@router.delete("/{tool_key}", status_code=204)
async def eliminar(tool_key: str, tenant_id: UUID = Depends(tenant_actual)):
    await execute(
        "DELETE FROM tenant_tools WHERE tenant_id = $1 AND tool_key = $2",
        tenant_id,
        tool_key,
    )
