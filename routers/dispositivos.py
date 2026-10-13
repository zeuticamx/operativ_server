"""
Dispositivos de la app móvil que reciben push (services/push.py).

La app registra su Expo push token al iniciar sesión y lo da de baja al
cerrarla. Cualquier rol con negocio puede registrarse: qué le llega lo
decide `broadcast_alerta` con las mismas reglas que el WebSocket.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from deps import UsuarioActual, usuario_actual
from schemas import DispositivoPushIn
from session import execute

router = APIRouter(prefix="/dispositivos", tags=["dispositivos"])


@router.put("/push", status_code=status.HTTP_204_NO_CONTENT)
async def registrar_push(
    datos: DispositivoPushIn,
    usuario: UsuarioActual = Depends(usuario_actual),
):
    """
    Registra (o reasigna) el token de este teléfono al usuario que llama.

    UPSERT por token: si en el mismo teléfono inició sesión otra persona, el
    token pasa a ser suyo y el usuario anterior deja de recibir push ahí.
    Idempotente: la app lo llama en cada arranque con sesión.
    """
    if usuario.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Esta cuenta no tiene negocio: no hay avisos que mandarle",
        )
    # Un gerente mirando con "ver como" no debe dejar su teléfono recibiendo
    # los avisos del dueño cuando la sesión prestada ya venció.
    if usuario.impersonado_por is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Una sesión de 'ver como' no registra dispositivos",
        )

    await execute(
        """
        INSERT INTO dispositivos_push (token, tenant_id, portal_user_id, plataforma)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (token) DO UPDATE
           SET tenant_id = EXCLUDED.tenant_id,
               portal_user_id = EXCLUDED.portal_user_id,
               plataforma = EXCLUDED.plataforma,
               actualizado_en = NOW()
        """,
        datos.token,
        usuario.tenant_id,
        usuario.id,
        datos.plataforma,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/push", status_code=status.HTTP_204_NO_CONTENT)
async def dar_de_baja_push(
    token: str = Query(..., min_length=10, max_length=255),
    usuario: UsuarioActual = Depends(usuario_actual),
):
    """
    Al cerrar sesión. Solo borra el token si es del usuario que llama: 204
    igual si no existía, para que la app no tenga que distinguir.
    """
    await execute(
        "DELETE FROM dispositivos_push WHERE token = $1 AND portal_user_id = $2",
        token,
        usuario.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
