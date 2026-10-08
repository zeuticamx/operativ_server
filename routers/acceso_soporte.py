"""
Acceso de soporte con escritura, autorizado por el dueño
(services/acceso_soporte.py, sql/40_acceso_soporte_escritura.sql).

Tres audiencias, en un solo router porque comparten la misma tabla:

  - Soporte, desde su sesión de "ver como": `/estado`, `/solicitar`,
    `/cancelar`. Las dos escrituras son las únicas que deps deja pasar en
    solo lectura (RUTAS_ESCRITURA_SIEMPRE_PERMITIDAS).
  - El dueño, ya logueado: listar, aprobar, rechazar, revocar. Solo
    owner/superadmin, y NUNCA una sesión de "ver como": si no, soporte se
    aprobaría a sí mismo.
  - El dueño desde el enlace del correo: `/enlace/*`, públicos. El token va
    en el cuerpo (no en la URL) y es de un solo uso.

Una solicitud de otro negocio es 404, no 403, para no confirmar que existe.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from deps import UsuarioActual, gerencia_actual, usuario_actual
from schemas import (
    AprobarEscrituraIn,
    InfoEnlaceEscrituraOut,
    SolicitarEscrituraIn,
    SolicitudEscrituraOut,
)
from services import acceso_soporte as svc

router = APIRouter(prefix="/acceso-soporte", tags=["acceso-soporte"])


def _salida(fila) -> SolicitudEscrituraOut:
    estado = svc.estado_efectivo(fila)
    return SolicitudEscrituraOut(
        id=fila["id"],
        gerente_email=fila["gerente_email"],
        motivo=fila["motivo"],
        estado=estado,
        creada_en=fila["creada_en"],
        expira_en=fila["expira_en"],
        resuelta_en=fila["resuelta_en"],
        canal=fila["canal"],
        duracion_min=fila["duracion_min"],
        concede_hasta=fila["concede_hasta"] if estado == "aprobada" else None,
    )


def _no_encontrada() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Solicitud no encontrada")


def _invalida(e: svc.SolicitudInvalida) -> HTTPException:
    return HTTPException(status_code=e.codigo, detail=e.detalle)


# ------------------------------------------------------------
# Soporte (sesión de "ver como")
# ------------------------------------------------------------
async def _soporte(usuario: UsuarioActual = Depends(usuario_actual)) -> UsuarioActual:
    if usuario.impersonado_por_id is None or usuario.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo desde una sesión de 'ver como'",
        )
    return usuario


@router.get("/estado", response_model=SolicitudEscrituraOut | None)
async def estado(usuario: UsuarioActual = Depends(_soporte)):
    """La última solicitud de este gerente para este negocio, o null."""
    fila = await svc.ultima_de_gerente(usuario.impersonado_por_id, usuario.tenant_id)
    return _salida(fila) if fila else None


@router.post("/solicitar", response_model=SolicitudEscrituraOut, status_code=201)
async def solicitar(datos: SolicitarEscrituraIn, usuario: UsuarioActual = Depends(_soporte)):
    try:
        fila = await svc.solicitar(
            usuario.tenant_id, usuario.impersonado_por_id, usuario.impersonado_por, datos.motivo
        )
    except svc.SolicitudInvalida as e:
        raise _invalida(e) from e
    return _salida(fila)


@router.post("/cancelar", response_model=SolicitudEscrituraOut | None)
async def cancelar(usuario: UsuarioActual = Depends(_soporte)):
    """Retira la solicitud pendiente o suelta la edición ya aprobada."""
    fila = await svc.cancelar(usuario.tenant_id, usuario.impersonado_por_id)
    return _salida(fila) if fila else None


# ------------------------------------------------------------
# Dueño, desde el portal
# ------------------------------------------------------------
async def _dueno(usuario: UsuarioActual = Depends(gerencia_actual)) -> UsuarioActual:
    if usuario.impersonado_por is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo el dueño del negocio puede responder esta solicitud",
        )
    if usuario.tenant_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Sin negocio asociado")
    return usuario


@router.get("/solicitudes", response_model=list[SolicitudEscrituraOut])
async def listar(usuario: UsuarioActual = Depends(_dueno)):
    return [_salida(f) for f in await svc.listar_del_negocio(usuario.tenant_id)]


@router.post("/{solicitud_id:uuid}/aprobar", response_model=SolicitudEscrituraOut)
async def aprobar(
    solicitud_id: UUID, datos: AprobarEscrituraIn, usuario: UsuarioActual = Depends(_dueno)
):
    fila = await svc.aprobar(
        solicitud_id, usuario.tenant_id, datos.duracion_min, usuario.email, usuario.id
    )
    if fila is None:
        raise _no_encontrada()
    return _salida(fila)


@router.post("/{solicitud_id:uuid}/rechazar", response_model=SolicitudEscrituraOut)
async def rechazar(solicitud_id: UUID, usuario: UsuarioActual = Depends(_dueno)):
    fila = await svc.rechazar(solicitud_id, usuario.tenant_id, usuario.email, usuario.id)
    if fila is None:
        raise _no_encontrada()
    return _salida(fila)


@router.post("/{solicitud_id:uuid}/revocar", response_model=SolicitudEscrituraOut)
async def revocar(solicitud_id: UUID, usuario: UsuarioActual = Depends(_dueno)):
    fila = await svc.revocar(solicitud_id, usuario.tenant_id, usuario.email, usuario.id)
    if fila is None:
        raise _no_encontrada()
    return _salida(fila)


# ------------------------------------------------------------
# Dueño, desde el enlace del correo (sin sesión)
# ------------------------------------------------------------
class TokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class AprobarPorEnlaceIn(TokenIn):
    duracion_min: int


def _info(fila) -> InfoEnlaceEscrituraOut:
    return InfoEnlaceEscrituraOut(**svc.info_enlace(fila).__dict__)


@router.post("/enlace/leer", response_model=InfoEnlaceEscrituraOut)
async def leer_enlace(datos: TokenIn):
    fila = await svc.leer_por_token(datos.token)
    if fila is None:
        raise _no_encontrada()
    return _info(fila)


@router.post("/enlace/aprobar", response_model=InfoEnlaceEscrituraOut)
async def aprobar_por_enlace(datos: AprobarPorEnlaceIn):
    try:
        fila = await svc.responder_por_token(
            datos.token, aprobar_=True, duracion_min=datos.duracion_min
        )
    except svc.SolicitudInvalida as e:
        raise _invalida(e) from e
    if fila is None:
        raise _no_encontrada()
    return _info(fila)


@router.post("/enlace/rechazar", response_model=InfoEnlaceEscrituraOut)
async def rechazar_por_enlace(datos: TokenIn):
    fila = await svc.responder_por_token(datos.token, aprobar_=False, duracion_min=None)
    if fila is None:
        raise _no_encontrada()
    return _info(fila)
