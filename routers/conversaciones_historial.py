"""
Historial de citas del cliente de una conversación, para el portal (spec 002).

Router propio, aparte de `conversaciones.py`, a propósito: aquel deja pasar al
vendedor (`permitir_a_roles_acotados`) y su `conversacion_visible` responde 403
cuando la conversación es de otra persona. Aquí el vendedor no entra (403 de
rol) y a un proveedor al que la conversación no le es visible se le responde
404, igual que si no existiera: no se revela que existe (spec 002 R6, R10).

Orden de verificación (spec, "Roles y accesos"): 401 sesión, 403 rol (router),
402 acceso a conversaciones por plan (router), 403 ficha del proveedor, 409/402
módulo de calendarios, 404 conversación de canal y visible para el rol.
"""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from deps import (
    ROL_PROVEEDOR,
    UsuarioActual,
    permitir_a_proveedor,
    proveedor_actual,
    requiere_herramienta,
    tenant_actual,
    usuario_actual,
)
from schemas import HistorialCitasPortalOut
from services import calendario as cal
from services import conversaciones as svc

# lectura_sin_plan: igual que conversaciones; con el plan vencido esta
# dependencia deja pasar y es el módulo de calendarios (dentro del endpoint)
# el que responde 402 `plan_requerido`.
# OJO: `permitir_a_proveedor` filtra por NOMBRE de función. El endpoint se
# llama `historial_citas`; con otro nombre el proveedor recibiría 403.
router = APIRouter(
    prefix="/conversaciones",
    tags=["conversaciones"],
    dependencies=[
        Depends(permitir_a_proveedor("historial_citas")),
        Depends(requiere_herramienta("agente", lectura_sin_plan=True)),
    ],
)

_NO_ENCONTRADA = "Conversación no encontrada"


def _no_encontrada() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_ENCONTRADA)


@router.get("/{conversacion_id}/historial-citas", response_model=HistorialCitasPortalOut)
async def historial_citas(
    conversacion_id: UUID,
    usuario: UsuarioActual = Depends(usuario_actual),
    tenant_id: UUID = Depends(tenant_actual),
) -> dict:
    """
    Las 10 citas pasadas o canceladas más recientes del cliente de la
    conversación (más `hay_mas`). Solo lectura. El owner, el superadmin y el
    member ven todas las del cliente en el negocio; el proveedor, solo las
    suyas y solo si la conversación le es visible.
    """
    # 1) Ficha del proveedor (403 desactivado / sin ficha).
    proveedor_id: UUID | None = None
    if usuario.role == ROL_PROVEEDOR:
        proveedor_id = (await proveedor_actual(usuario)).id

    # 2) Módulo de calendarios: 409 con código si está apagado; 402 de plan.
    await cal.verificar_calendario_activo(tenant_id, con_codigo=True)

    # 3) Visibilidad para el proveedor: 404 (no 403) si no le es visible.
    if proveedor_id is not None:
        visible = await svc.es_visible(
            tenant_id, conversacion_id, svc.Alcance(usuario.id, proveedor_id=proveedor_id)
        )
        if not visible:
            raise _no_encontrada()

    # 4) Conversación de canal del negocio.
    historial = await cal.historial_citas_de_conversacion(
        tenant_id,
        conversacion_id,
        ahora=datetime.now(timezone.utc),
        proveedor_id=proveedor_id,
    )
    if historial is None:
        raise _no_encontrada()
    return historial
