"""
Avisos de cita por negocio (spec 007): oferta de las plantillas de recordatorio y de
cancelación, interruptores de cada una y su estado ante Meta.

Leer es de `negocio_actual` (owner, superadmin y member). Decidir la oferta y activar o
apagar funciones es de `gerencia_actual`; vendedor y proveedor reciben 403. El negocio sale
siempre del token, nunca del cuerpo. `/api/avisos-cita` está en `RUTAS_SENSIBLES`: desde
"ver como" se puede mirar, pero no escribir, ni con permiso de edición (aceptar crea
plantillas en la cuenta real de WhatsApp del negocio).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from deps import UsuarioActual, gerencia_actual, negocio_actual
from schemas import ConfigAvisosOut, ConfigAvisosPatchIn, OfertaAvisosIn
from services import avisos_cita_config as servicio
from services.neuroapi_plantillas import PlantillaApiError, a_http

log = logging.getLogger("operativai.avisos_cita_config")

router = APIRouter(prefix="/avisos-cita", tags=["avisos-cita"])


def _tenant(usuario: UsuarioActual):
    if usuario.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El usuario no tiene un negocio asociado todavía",
        )
    return usuario.tenant_id


@router.get("/config", response_model=ConfigAvisosOut)
async def obtener_config(usuario: UsuarioActual = Depends(negocio_actual)):
    tenant_id = _tenant(usuario)
    try:
        await servicio.sincronizar(tenant_id)
    except Exception:
        # La conciliación es un favor: sin red o sin credenciales se responde con la caché.
        log.warning("No se pudo conciliar los avisos de cita de %s", tenant_id, exc_info=True)
    return await servicio.obtener(tenant_id)


@router.post("/oferta", response_model=ConfigAvisosOut)
async def responder_oferta(
    datos: OfertaAvisosIn, usuario: UsuarioActual = Depends(gerencia_actual)
):
    tenant_id = _tenant(usuario)
    try:
        if datos.accion == "aceptar":
            return await servicio.aceptar(tenant_id, usuario.id)
        return await servicio.rechazar(tenant_id, usuario.id)
    except servicio.AvisosError as e:
        raise servicio.a_http(e)
    except PlantillaApiError as e:
        raise a_http(e)


@router.patch("/config", response_model=ConfigAvisosOut)
async def cambiar_config(
    datos: ConfigAvisosPatchIn, usuario: UsuarioActual = Depends(gerencia_actual)
):
    tenant_id = _tenant(usuario)
    try:
        return await servicio.parchear(
            tenant_id,
            usuario.id,
            recordatorio_activo=datos.recordatorio_activo,
            cancelacion_activo=datos.cancelacion_activo,
        )
    except servicio.AvisosError as e:
        raise servicio.a_http(e)
    except PlantillaApiError as e:
        raise a_http(e)
