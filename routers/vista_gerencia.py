"""
Revocar o restablecer la vista de gerencia a las conversaciones del negocio.

Solo owner/superadmin con sesión propia: una sesión de "ver como" no puede decidirlo
(`/api/vista-gerencia` está en `RUTAS_SENSIBLES`, y además se rechaza aquí). Si el negocio no
es el dueño del número configurado, `aplica` es false y escribir es 404.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from deps import UsuarioActual, gerencia_actual
from schemas import VistaGerenciaOut
from services import vista_gerencia as svc
from services.gerencia import registrar_auditoria

router = APIRouter(prefix="/vista-gerencia", tags=["vista-gerencia"])


async def _dueno_propio(usuario: UsuarioActual = Depends(gerencia_actual)) -> UsuarioActual:
    if usuario.impersonado_por is not None or usuario.impersonado_por_id is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esto no se puede decidir desde una sesión de «ver como»",
        )
    if usuario.tenant_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Sin negocio asociado")
    return usuario


async def _estado(usuario: UsuarioActual) -> VistaGerenciaOut:
    if not await svc.aplica_a(usuario.tenant_id):
        return VistaGerenciaOut(aplica=False, revocada=False)
    return VistaGerenciaOut(aplica=True, revocada=await svc.vista_revocada(usuario.tenant_id))


@router.get("", response_model=VistaGerenciaOut)
async def estado(usuario: UsuarioActual = Depends(_dueno_propio)):
    return await _estado(usuario)


async def _cambiar(usuario: UsuarioActual, revocada: bool) -> VistaGerenciaOut:
    if not await svc.aplica_a(usuario.tenant_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No aplica a este negocio")
    await svc.fijar_revocada(usuario.tenant_id, revocada, usuario.id)
    await registrar_auditoria(
        actor_email=usuario.email,
        actor_portal_user_id=usuario.id,
        accion="vista_gerencia_revocada" if revocada else "vista_gerencia_restablecida",
        tenant_id=usuario.tenant_id,
        detalle={},
    )
    return VistaGerenciaOut(aplica=True, revocada=revocada)


@router.post("/revocar", response_model=VistaGerenciaOut)
async def revocar(usuario: UsuarioActual = Depends(_dueno_propio)):
    return await _cambiar(usuario, True)


@router.post("/restablecer", response_model=VistaGerenciaOut)
async def restablecer(usuario: UsuarioActual = Depends(_dueno_propio)):
    return await _cambiar(usuario, False)
