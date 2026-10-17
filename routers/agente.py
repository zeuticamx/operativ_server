"""Configuración del agente por tenant: prompt, modelo, parámetros."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from deps import (
    UsuarioActual,
    gerencia_actual,
    negocio_actual,
    requiere_herramienta,
    tenant_actual,
)
from services.limites_agente import (
    MODELOS_AGENTE,
    LimitesAgente,
    exigir_dentro_del_plan,
    limites_de_tenant,
)
from session import conexion, fetch_one
from schemas import (
    AgenteConfigIn,
    AgenteConfigOut,
    LimitesAgenteOut,
    NumeroAgenteIn,
    NumeroAgenteOut,
)
from services import herramientas_plan
from services import numeros_whatsapp as numeros
from services.numeros_bitacora import Autor
from services.numeros_reglas import configuracion_incompleta

router = APIRouter(
    prefix="/agente",
    tags=["agente"],
    # Leer la config: todo el equipo del negocio. Cambiarla: solo gerencia.
    dependencies=[Depends(negocio_actual), Depends(requiere_herramienta("agente"))],
)

# Lista blanca: si un tenant guarda un nombre de modelo inventado, el
# workflow de n8n truena en ejecución con un error poco claro. Mejor
# rechazarlo aquí, donde el usuario puede corregirlo. El catálogo vive en
# services/limites_agente.py; además el PLAN puede dejar menos modelos
# (402, ver exigir_dentro_del_plan).
MODELOS_PERMITIDOS = set(MODELOS_AGENTE)


def _limites_out(limites: LimitesAgente) -> LimitesAgenteOut:
    return LimitesAgenteOut(
        plan=limites.plan,
        modelos_permitidos=list(limites.modelos_disponibles()),
        modelo_por_defecto=limites.modelo_por_defecto,
        historial_maximo=limites.tope_historial(),
    )


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
    datos_salida["limites_plan"] = _limites_out(await limites_de_tenant(tenant_id))
    return AgenteConfigOut(**datos_salida)


@router.put("", response_model=AgenteConfigOut)
async def actualizar(
    datos: AgenteConfigIn,
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
):
    if datos.model and datos.model not in MODELOS_PERMITIDOS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Modelo no permitido. Opciones: {', '.join(sorted(MODELOS_PERMITIDOS))}",
        )

    limites = await limites_de_tenant(tenant_id)
    # Sin modelo elegido, n8n usa el suyo por defecto, que puede no estar en el
    # plan: si el plan limita modelos, se guarda el por defecto del plan.
    modelo = datos.model or limites.modelo_por_defecto
    exigir_dentro_del_plan(limites, modelo, datos.history_window)

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
        modelo,
        datos.temperature,
        datos.history_window,
        datos.is_active,
    )

    datos_salida = dict(fila)
    datos_salida["temperature"] = float(datos_salida["temperature"])
    datos_salida["limites_plan"] = _limites_out(limites)
    return AgenteConfigOut(**datos_salida)


@router.get("/modelos")
async def modelos():
    # Catálogo completo; lo que cada plan deja elegir viene en `limites_plan`
    # de GET /api/agente.
    return {"modelos": sorted(MODELOS_PERMITIDOS)}


# ============================================================
# Configuración por número (spec 005a, RF-5; T5)
# ============================================================
async def _numero_out(conn, tenant_id: UUID, fila) -> NumeroAgenteOut:
    limites = await limites_de_tenant(tenant_id)
    herramientas = await herramientas_plan.herramientas_de_numero(conn, tenant_id, fila["id"])
    return NumeroAgenteOut(
        id=fila["id"],
        phone_number_id=fila["phone_number_id"],
        estado=fila["estado"],
        es_principal=bool(fila["es_principal"]),
        es_linea_avisos=bool(fila["es_linea_avisos"]),
        agent_name=fila["agent_name"] or "",
        system_prompt=fila["system_prompt"] or "",
        model=fila["model"],
        temperature=float(fila["temperature"]),
        history_window=fila["history_window"],
        agente_activo=bool(fila["agente_activo"]),
        configuracion_incompleta=configuracion_incompleta(fila["agent_name"], fila["system_prompt"]),
        limites_plan=_limites_out(limites),
        herramientas=herramientas,
    )


@router.get("/numeros/{numero_id}", response_model=NumeroAgenteOut)
async def obtener_numero(
    numero_id: UUID,
    tenant_id: UUID = Depends(tenant_actual),
):
    """Lee la configuración de un número (RF-18: member lee, sin credenciales)."""
    async with conexion() as conn:
        fila = await numeros.numero_del_negocio(conn, tenant_id, numero_id)
        return await _numero_out(conn, tenant_id, fila)


@router.put("/numeros/{numero_id}", response_model=NumeroAgenteOut)
async def actualizar_numero(
    numero_id: UUID,
    datos: NumeroAgenteIn,
    tenant_id: UUID = Depends(tenant_actual),
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    """Edita la configuración de un número (400 temperatura, 402 plan, 404 ajeno)."""
    cambios = {k: v for k, v in datos.model_dump().items() if v is not None}
    # `model: null` explícito = default del plan (D32): se guarda NULL.
    if datos.model is None and "model" in datos.model_fields_set:
        cambios["model"] = None
    async with conexion() as conn:
        fila = await numeros.editar_configuracion(
            conn, tenant_id, numero_id, cambios, Autor.gerencia(usuario.id, usuario.email)
        )
        return await _numero_out(conn, tenant_id, fila)
