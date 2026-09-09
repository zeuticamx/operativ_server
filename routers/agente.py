"""Configuración del agente por tenant: prompt, modelo, parámetros."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from deps import tenant_actual
from session import fetch_one
from schemas import AgenteConfigIn, AgenteConfigOut

router = APIRouter(prefix="/agente", tags=["agente"])

# Lista blanca: si un tenant guarda un nombre de modelo inventado, el
# workflow de n8n truena en ejecución con un error poco claro. Mejor
# rechazarlo aquí, donde el usuario puede corregirlo.
MODELOS_PERMITIDOS = {
    "claude-sonnet-5",
    "claude-opus-5",
    "claude-haiku-4-5-20251001",
}


@router.get("", response_model=AgenteConfigOut)
async def obtener(tenant_id: UUID = Depends(tenant_actual)):
    fila = await fetch_one(
        """
        SELECT agent_name, system_prompt, model, temperature,
               history_window, is_active
        FROM tenant_agent_config
        WHERE tenant_id = $1
        """,
        tenant_id,
    )
    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Este negocio no tiene configuración de agente",
        )
    datos_salida = dict(fila)
    datos_salida["temperature"] = float(datos_salida["temperature"])
    return AgenteConfigOut(**datos_salida)


@router.put("", response_model=AgenteConfigOut)
async def actualizar(
    datos: AgenteConfigIn,
    tenant_id: UUID = Depends(tenant_actual),
):
    if datos.model and datos.model not in MODELOS_PERMITIDOS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Modelo no permitido. Opciones: {', '.join(sorted(MODELOS_PERMITIDOS))}",
        )

    fila = await fetch_one(
        """
        INSERT INTO tenant_agent_config
            (tenant_id, agent_name, system_prompt, model, temperature,
             history_window, is_active, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
        ON CONFLICT (tenant_id) DO UPDATE SET
            agent_name     = EXCLUDED.agent_name,
            system_prompt  = EXCLUDED.system_prompt,
            model          = EXCLUDED.model,
            temperature    = EXCLUDED.temperature,
            history_window = EXCLUDED.history_window,
            is_active      = EXCLUDED.is_active,
            updated_at     = NOW()
        RETURNING agent_name, system_prompt, model, temperature,
                  history_window, is_active
        """,
        tenant_id,
        datos.agent_name,
        datos.system_prompt,
        datos.model,
        datos.temperature,
        datos.history_window,
        datos.is_active,
    )

    datos_salida = dict(fila)
    datos_salida["temperature"] = float(datos_salida["temperature"])
    return AgenteConfigOut(**datos_salida)


@router.get("/modelos")
async def modelos():
    return {"modelos": sorted(MODELOS_PERMITIDOS)}
