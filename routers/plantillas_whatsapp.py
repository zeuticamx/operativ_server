"""
Estado global de las plantillas de WhatsApp de los avisos de cita (spec 004,
T8). Solo API, solo gerencia de plataforma (`gerencia_users`): un owner,
superadmin, member, vendedor o proveedor de un negocio no las ve, y una sesión
"ver como" tampoco (`gerencia_plataforma_actual` la rechaza con 403).

Estas dos plantillas globales se crean y aprueban a mano en Meta Business
Manager (las plantillas propias de cada negocio sí se administran por la API de
NeuroAPI: routers/plantillas_whatsapp_tenant.py). `reintentar` solo vuelve a
`pendiente_aprobacion` en nuestra BD; no llama a Meta ni a NeuroAPI. El estado
también cambia por el evento `message_template_status_update` que llega al
webhook de NeuroAPI; gana el último cambio.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from deps import UsuarioActual, gerencia_plataforma_actual
from schemas import PlantillaWhatsappEstadoIn, PlantillaWhatsappOut
from services import plantillas_whatsapp as servicio

router = APIRouter(
    prefix="/gerencia/plantillas-whatsapp",
    tags=["gerencia"],
    dependencies=[Depends(gerencia_plataforma_actual)],
)


def _no_encontrada() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plantilla no encontrada")


async def _plantilla(nombre: str) -> PlantillaWhatsappOut:
    for fila in await servicio.listar_plantillas():
        if fila["nombre"] == nombre:
            return PlantillaWhatsappOut(**fila)
    raise _no_encontrada()


@router.get("", response_model=list[PlantillaWhatsappOut])
async def listar_plantillas():
    return [PlantillaWhatsappOut(**f) for f in await servicio.listar_plantillas()]


@router.patch("/{nombre}", response_model=PlantillaWhatsappOut)
async def fijar_estado(
    nombre: str,
    datos: PlantillaWhatsappEstadoIn,
    gerente: UsuarioActual = Depends(gerencia_plataforma_actual),
):
    """Mecanismo manual: fija el estado que Meta muestra en Business Manager."""
    try:
        await servicio.cambiar_estado(
            nombre,
            datos.estado,
            nota=datos.nota,
            actor_email=gerente.email,
            actor_id=gerente.id,
            origen="manual",
        )
    except servicio.PlantillaInexistente:
        raise _no_encontrada()
    return await _plantilla(nombre)


@router.post("/{nombre}/reintentar", response_model=PlantillaWhatsappOut)
async def reintentar_solicitud(
    nombre: str,
    gerente: UsuarioActual = Depends(gerencia_plataforma_actual),
):
    """Pasa a `pendiente_aprobacion`; 409 si ya está `aprobada` o `pendiente_aprobacion`."""
    try:
        await servicio.cambiar_estado(
            nombre,
            servicio.PENDIENTE_APROBACION,
            nota="reintento de solicitud",
            actor_email=gerente.email,
            actor_id=gerente.id,
            origen="reintento",
            solo_si_reintentable=True,
        )
    except servicio.PlantillaInexistente:
        raise _no_encontrada()
    except servicio.CambioNoPermitido as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"La plantilla está {e.estado_actual}; no admite un nuevo intento",
        )
    return await _plantilla(nombre)
