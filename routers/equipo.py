"""
Cuentas del equipo de un negocio: quién entra al portal además del dueño.

    GET    /equipo/usuarios                    cuentas del negocio
    PATCH  /equipo/usuarios/{id}               quitar o devolver el acceso
    GET    /equipo/invitaciones                invitaciones sin aceptar
    POST   /equipo/invitaciones                invitar a un member o a un vendedor
    POST   /equipo/invitaciones/{id}/reenviar  enlace nuevo (el anterior muere)
    DELETE /equipo/invitaciones/{id}           revocar

Todo es de gerencia (owner/superadmin) y siempre sobre el tenant del token.
Un id de otro negocio da 404, no 403, para no confirmar que existe (mismo
criterio que deps.verificar_acceso_tenant).

La otra mitad del flujo, la del invitado, es pública y vive en
routers/auth.py (/auth/invitacion/*).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

from deps import ROLES_INVITABLES, ROL_PROVEEDOR, ROL_VENDEDOR, UsuarioActual, gerencia_actual
from schemas import (
    InvitacionCrearIn,
    InvitacionCreadaOut,
    InvitacionOut,
    UsuarioEquipoActualizarIn,
    UsuarioEquipoOut,
)
from services import invitaciones as svc
from session import execute, fetch_all, fetch_one, transaccion

router = APIRouter(prefix="/equipo", tags=["equipo"])


def _tenant(usuario: UsuarioActual) -> UUID:
    if usuario.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El usuario no tiene un negocio asociado todavía",
        )
    return usuario.tenant_id


_SELECT_USUARIOS = """
    SELECT pu.id, pu.email, pu.role, pu.is_active AS activo,
           NULLIF(TRIM(CONCAT_WS(' ', pu.nombres, pu.apellido_paterno)), '') AS nombre,
           v.id AS vendedor_id, v.nombre AS vendedor_nombre,
           pr.id AS proveedor_id, pr.nombre AS proveedor_nombre,
           pu.last_login_at AS ultimo_acceso
    FROM portal_users pu
    LEFT JOIN vendedores  v  ON v.portal_user_id  = pu.id AND v.tenant_id  = pu.tenant_id
    LEFT JOIN proveedores pr ON pr.portal_user_id = pu.id AND pr.tenant_id = pu.tenant_id
"""

# Dueño primero, después quien opera el negocio, al final quien solo ve lo suyo.
_ORDEN_ROLES = """
    CASE pu.role WHEN 'owner' THEN 0 WHEN 'superadmin' THEN 1
                 WHEN 'member' THEN 2 WHEN 'vendedor' THEN 3 ELSE 4 END
"""


def _usuario_out(fila, usuario: UsuarioActual) -> UsuarioEquipoOut:
    return UsuarioEquipoOut(**dict(fila), es_tu_cuenta=fila["id"] == usuario.id)


@router.get("/usuarios", response_model=list[UsuarioEquipoOut])
async def listar_usuarios(usuario: UsuarioActual = Depends(gerencia_actual)):
    filas = await fetch_all(
        f"""
        {_SELECT_USUARIOS}
        WHERE pu.tenant_id = $1
        ORDER BY {_ORDEN_ROLES}, pu.is_active DESC, pu.email
        """,
        _tenant(usuario),
    )
    return [_usuario_out(f, usuario) for f in filas]


@router.patch("/usuarios/{usuario_id}", response_model=UsuarioEquipoOut)
async def actualizar_usuario(
    usuario_id: UUID,
    datos: UsuarioEquipoActualizarIn,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    """
    Quita o devuelve el acceso al portal (y a la app de vendedores).

    Corta en la siguiente petición: deps.usuario_actual relee is_active en
    cada llamada y /auth/refresh también. No toca la ficha de vendedor ni la
    de proveedor: siguen recibiendo leads o citas mientras la ficha esté
    activa — quitar el acceso no es sacarlo del reparto ni de la agenda.

    Solo cuentas invitables: al dueño no se lo apaga desde acá.
    """
    tenant_id = _tenant(usuario)
    fila = await fetch_one(
        "SELECT id, role FROM portal_users WHERE id = $1 AND tenant_id = $2",
        usuario_id,
        tenant_id,
    )
    if fila is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")
    if fila["role"] not in ROLES_INVITABLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El acceso del dueño del negocio no se puede quitar desde acá",
        )

    await execute(
        "UPDATE portal_users SET is_active = $3 WHERE id = $1 AND tenant_id = $2",
        usuario_id,
        tenant_id,
        datos.activo,
    )
    actualizado = await fetch_one(f"{_SELECT_USUARIOS} WHERE pu.id = $1", usuario_id)
    return _usuario_out(actualizado, usuario)


@router.get("/invitaciones", response_model=list[InvitacionOut])
async def listar_invitaciones(usuario: UsuarioActual = Depends(gerencia_actual)):
    filas = await fetch_all(
        f"""
        {svc.SELECT_INVITACION}
        WHERE i.tenant_id = $1
          AND i.aceptada_en IS NULL AND i.revocada_en IS NULL
        ORDER BY i.creada_en DESC
        """,
        _tenant(usuario),
    )
    return [InvitacionOut(**dict(f)) for f in filas]


async def _remitente(usuario: UsuarioActual) -> tuple[str, str]:
    """(quién invita, nombre del negocio) para el texto del correo."""
    fila = await fetch_one(
        """
        SELECT t.name AS negocio,
               NULLIF(TRIM(CONCAT_WS(' ', pu.nombres, pu.apellido_paterno)), '') AS nombre
        FROM portal_users pu
        JOIN tenants t ON t.id = pu.tenant_id
        WHERE pu.id = $1
        """,
        usuario.id,
    )
    if fila is None:
        return usuario.email, "tu negocio"
    return fila["nombre"] or usuario.email, fila["negocio"]


@router.post("/invitaciones", response_model=InvitacionCreadaOut, status_code=201)
async def invitar(
    datos: InvitacionCrearIn,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    """
    Invita a alguien al negocio. El correo lleva un enlace para que elija su
    contraseña; la respuesta trae el mismo enlace por si el correo no llega.

      404 la ficha (de vendedor o de proveedor) no es de este negocio
      409 el correo ya tiene cuenta, o la ficha está inactiva o ya tiene acceso

    Invitar otra vez al mismo correo (o a la misma ficha) reemplaza la
    invitación anterior.

    No consume cupo del plan: los topes cuentan fichas activas, y la ficha
    ya existe (se contó al darla de alta).
    """
    tenant_id = _tenant(usuario)
    email = datos.email.lower()

    async with transaccion() as conn:
        # Nombres fijos (no entrada del usuario): van interpolados en el SQL.
        ficha_de = {
            ROL_VENDEDOR: ("vendedores", datos.vendedor_id, "vendedor"),
            ROL_PROVEEDOR: ("proveedores", datos.proveedor_id, "proveedor"),
        }
        if datos.role in ficha_de:
            tabla, ficha_id, nombre = ficha_de[datos.role]
            ficha = await conn.fetchrow(
                f"SELECT activo, portal_user_id FROM {tabla} WHERE id = $1 AND tenant_id = $2",
                ficha_id,
                tenant_id,
            )
            if ficha is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"{nombre.capitalize()} no encontrado",
                )
            if not ficha["activo"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Ese {nombre} está desactivado. Actívalo antes de darle acceso.",
                )
            if ficha["portal_user_id"] is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Ese {nombre} ya tiene acceso",
                )

        # El correo es único en toda la plataforma. Decirlo acá filtra que
        # existe una cuenta con ese correo, pero solo a un dueño autenticado,
        # y sin esto el invitado se enteraría recién al intentar aceptar.
        if await conn.fetchval("SELECT 1 FROM portal_users WHERE LOWER(email) = $1", email):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Ese correo ya tiene una cuenta en OperativAI. Usa otro correo.",
            )

        fila, token = await svc.crear(
            conn,
            tenant_id,
            email,
            datos.role,
            datos.vendedor_id,
            usuario.id,
            proveedor_id=datos.proveedor_id,
        )

    # Fuera de la transacción: la invitación existe aunque el SMTP falle.
    enviado = await svc.mandar_correo(fila, token, *await _remitente(usuario))
    return InvitacionCreadaOut(**dict(fila), enlace=svc.enlace(token), correo_enviado=enviado)


@router.post("/invitaciones/{invitacion_id}/reenviar", response_model=InvitacionCreadaOut)
async def reenviar(invitacion_id: UUID, usuario: UsuarioActual = Depends(gerencia_actual)):
    tenant_id = _tenant(usuario)
    async with transaccion() as conn:
        resultado = await svc.renovar(conn, invitacion_id, tenant_id)
    if resultado is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitación no encontrada")
    fila, token = resultado
    enviado = await svc.mandar_correo(fila, token, *await _remitente(usuario))
    return InvitacionCreadaOut(**dict(fila), enlace=svc.enlace(token), correo_enviado=enviado)


@router.delete("/invitaciones/{invitacion_id}", status_code=204)
async def revocar(invitacion_id: UUID, usuario: UsuarioActual = Depends(gerencia_actual)) -> Response:
    revocada = await fetch_one(
        """
        UPDATE invitaciones_equipo SET revocada_en = NOW()
         WHERE id = $1 AND tenant_id = $2
           AND aceptada_en IS NULL AND revocada_en IS NULL
        RETURNING id
        """,
        invitacion_id,
        _tenant(usuario),
    )
    if revocada is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitación no encontrada")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
