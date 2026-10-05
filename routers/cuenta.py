"""
Borrado de la cuenta por el propio dueño (services/eliminacion_cuenta.py).

  GET  /cuenta/eliminacion  si se puede borrar, por qué no, y qué se pierde
  POST /cuenta/eliminar     borra el negocio entero. Irreversible.

Solo `owner`. Una sesión de "ver como" no llega a borrar nada: deps rechaza
cualquier escritura de una sesión impersonada (403) antes de entrar acá.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status

from deps import UsuarioActual, usuario_actual
from schemas import EliminacionCuentaOut, EliminarCuentaIn
from services import eliminacion_cuenta as svc

router = APIRouter(prefix="/cuenta", tags=["cuenta"])


def _solo_propietario() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Solo el dueño del negocio puede borrar la cuenta",
    )


@router.get("/eliminacion", response_model=EliminacionCuentaOut)
async def revisar_eliminacion(usuario: UsuarioActual = Depends(usuario_actual)):
    try:
        rc = await svc.revisar(usuario)
    except svc.NoEsPropietario:
        raise _solo_propietario()
    r = rc.revision
    return EliminacionCuentaOut(
        eliminable=not rc.bloqueos,
        bloqueos=rc.bloqueos,
        pagado_hasta=r.pagado_hasta,
        creditos_disponibles=r.creditos_disponibles,
        usuarios_portal=r.usuarios_portal,
        conversaciones=r.conversaciones,
        verificacion=rc.verificacion,
    )


@router.post("/eliminar", status_code=status.HTTP_204_NO_CONTENT)
async def eliminar(
    datos: EliminarCuentaIn,
    usuario: UsuarioActual = Depends(usuario_actual),
) -> Response:
    """
    POST y no DELETE porque lleva la confirmación y la credencial en el body.

      403 no es el dueño, o la contraseña/credencial no es válida (no 401: el
          portal leería un 401 como sesión vencida y mandaría al login)
      400 la confirmación no es "ELIMINAR"
      429 demasiados intentos fallidos
      409 {codigo, mensaje, bloqueos} suscripción que todavía cobra, adeudos
          o algo que impide el borrado. No se borra nada.
    """
    try:
        await svc.eliminar(
            usuario,
            confirmacion=datos.confirmacion,
            password=datos.password,
            google_credential=datos.google_credential,
        )
    except svc.NoEsPropietario:
        raise _solo_propietario()
    except svc.ConfirmacionIncorrecta:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Escribe "{svc.CONFIRMACION}" para confirmar',
        )
    except svc.DemasiadosIntentos as e:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Demasiados intentos fallidos. Vuelve a intentarlo en {e.minutos} min.",
        )
    except svc.CredencialIncorrecta:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="La contraseña no es correcta",
        )
    except svc.EliminacionBloqueada as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "codigo": e.bloqueos[0].codigo,
                "mensaje": e.bloqueos[0].mensaje,
                "bloqueos": [b.model_dump() for b in e.bloqueos],
            },
        )
    except svc.ReferenciasPendientes:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "codigo": "referencias_pendientes",
                "mensaje": (
                    "No se pudo borrar la cuenta por datos vinculados. No se eliminó "
                    "nada; contacta a soporte."
                ),
            },
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
