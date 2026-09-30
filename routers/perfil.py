"""
Perfil personal del usuario del portal: datos y foto, desde /preferencias.

Siempre sobre el usuario de la sesión — no hay `{id}` en ninguna ruta, así
que no hay forma de leer ni editar el perfil de otro. Cualquier rol puede
editar el suyo. En "ver como" (impersonación) las escrituras ya las corta
deps.usuario_actual con 403.

Qué cuenta como "completo" está en schemas.CAMPOS_PERFIL_OBLIGATORIOS; al
completarse, `perfil_completado_en` corta los recordatorios de
jobs/perfil_background.py.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status

from config import settings
from deps import UsuarioActual, usuario_actual
from schemas import CAMPOS_PERFIL_OBLIGATORIOS, PerfilIn, PerfilOut
from services.imagen import ImagenInvalida, validar_foto
from session import execute, fetch_one, transaccion

router = APIRouter(prefix="/perfil", tags=["perfil"])

# `created_at` es TIMESTAMP sin zona (guardado con la hora local del
# servidor), así que se compara contra LOCALTIMESTAMP y no contra NOW():
# los dos lados en la misma zona.
#
# `empresa` cae por defecto en el nombre del negocio, que ya se pidió al
# crear la cuenta: no se le vuelve a preguntar a nadie. Si la persona
# escribe otra, manda la suya; si la borra, vuelve a verse la del negocio.
_SELECT_PERFIL = """
    SELECT pu.nombres, pu.apellido_paterno, pu.apellido_materno,
           pu.fecha_nacimiento, pu.genero,
           COALESCE(pu.empresa, t.name) AS empresa,
           pu.perfil_completado_en,
           f.actualizada_en AS foto_actualizada_en,
           CEIL(EXTRACT(EPOCH FROM (pu.created_at + make_interval(days => $2))
                                  - LOCALTIMESTAMP) / 86400)::int AS dias_restantes
    FROM portal_users pu
    LEFT JOIN tenants t ON t.id = pu.tenant_id
    LEFT JOIN portal_user_fotos f ON f.portal_user_id = pu.id
    WHERE pu.id = $1
"""


def faltantes(fila) -> list[str]:
    """Obligatorios que siguen vacíos, en el orden del formulario."""
    return [c for c in CAMPOS_PERFIL_OBLIGATORIOS if fila[c] is None]


def version_foto(actualizada_en: datetime | None) -> str | None:
    """
    Cambia cada vez que la foto cambia. El portal la agrega a la URL de la
    foto para no mostrar una vieja desde caché.
    """
    return str(int(actualizada_en.timestamp() * 1000)) if actualizada_en else None


async def _perfil(user_id: UUID) -> PerfilOut:
    fila = await fetch_one(_SELECT_PERFIL, user_id, settings.PERFIL_RECORDATORIO_DIAS)
    if fila is None:
        # usuario_actual ya garantizó que existe; esto solo cubre un borrado
        # entre medias.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")

    pendientes = faltantes(fila)
    completo = not pendientes
    dias = fila["dias_restantes"]
    return PerfilOut(
        nombres=fila["nombres"],
        apellido_paterno=fila["apellido_paterno"],
        apellido_materno=fila["apellido_materno"],
        fecha_nacimiento=fila["fecha_nacimiento"],
        genero=fila["genero"],
        empresa=fila["empresa"],
        completo=completo,
        faltantes=pendientes,
        tiene_foto=fila["foto_actualizada_en"] is not None,
        foto_version=version_foto(fila["foto_actualizada_en"]),
        recordatorio_dias_restantes=dias if (not completo and dias is not None and dias > 0) else None,
    )


@router.get("", response_model=PerfilOut)
async def ver_perfil(usuario: UsuarioActual = Depends(usuario_actual)) -> PerfilOut:
    return await _perfil(usuario.id)


@router.put("", response_model=PerfilOut)
async def guardar_perfil(
    datos: PerfilIn, usuario: UsuarioActual = Depends(usuario_actual)
) -> PerfilOut:
    """
    Guarda el perfil entero. `perfil_completado_en` lo decide el servidor:
    se fija la primera vez que están todos los obligatorios y vuelve a NULL
    si alguno se vacía (y con eso los recordatorios, si todavía está dentro
    de la ventana, se reanudan).
    """
    # La misma lista que usa GET para `faltantes`: una sola definición de
    # "completo" (schemas.CAMPOS_PERFIL_OBLIGATORIOS).
    completo = all(getattr(datos, c) is not None for c in CAMPOS_PERFIL_OBLIGATORIOS)

    async with transaccion() as conn:
        fila = await conn.fetchrow(
            """
            UPDATE portal_users SET
                nombres          = $2,
                apellido_paterno = $3,
                apellido_materno = $4,
                fecha_nacimiento = $5,
                genero           = $6,
                empresa          = $7,
                perfil_completado_en = CASE
                    WHEN $8 THEN COALESCE(perfil_completado_en, NOW())
                    ELSE NULL
                END
            WHERE id = $1
            RETURNING perfil_completado_en
            """,
            usuario.id,
            datos.nombres,
            datos.apellido_paterno,
            datos.apellido_materno,
            datos.fecha_nacimiento,
            datos.genero,
            datos.empresa,
            completo,
        )

        # Con el perfil completo, el recordatorio que siga sin leer en la
        # campana ya no tiene nada que pedir.
        if fila is not None and fila["perfil_completado_en"] is not None:
            await conn.execute(
                """
                UPDATE alertas SET leido = true
                 WHERE portal_user_id = $1 AND tipo = 'perfil_incompleto' AND leido = false
                """,
                usuario.id,
            )

    return await _perfil(usuario.id)


@router.put("/foto", response_model=PerfilOut)
async def subir_foto(
    archivo: UploadFile = File(...),
    usuario: UsuarioActual = Depends(usuario_actual),
) -> PerfilOut:
    """
    Reemplaza la foto. Solo JPEG/PNG de hasta PERFIL_FOTO_MAX_PX de lado y
    PERFIL_FOTO_MAX_BYTES de peso (ver services/imagen.py):
      413 -> pesa de más
      415 -> no es JPEG/PNG, o la extensión no coincide con el contenido
      422 -> encabezado dañado o dimensiones fuera de rango
    """
    # Se lee un byte más que el tope: si llega a leerlo, el archivo excede,
    # sin tener que cargar entero algo enorme en memoria.
    contenido = await archivo.read(settings.PERFIL_FOTO_MAX_BYTES + 1)
    if len(contenido) > settings.PERFIL_FOTO_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"La foto pesa más de {settings.PERFIL_FOTO_MAX_BYTES // (1024 * 1024)} MB.",
        )
    if not contenido:
        raise HTTPException(status_code=422, detail="El archivo está vacío.")

    try:
        info = validar_foto(contenido, archivo.filename, settings.PERFIL_FOTO_MAX_PX)
    except ImagenInvalida as e:
        raise HTTPException(status_code=e.status_code, detail=e.mensaje)

    await execute(
        """
        INSERT INTO portal_user_fotos
            (portal_user_id, contenido, mime, ancho, alto, bytes, actualizada_en)
        VALUES ($1, $2, $3, $4, $5, $6, NOW())
        ON CONFLICT (portal_user_id) DO UPDATE SET
            contenido      = EXCLUDED.contenido,
            mime           = EXCLUDED.mime,
            ancho          = EXCLUDED.ancho,
            alto           = EXCLUDED.alto,
            bytes          = EXCLUDED.bytes,
            actualizada_en = NOW()
        """,
        usuario.id,
        contenido,
        info.mime,
        info.ancho,
        info.alto,
        len(contenido),
    )
    return await _perfil(usuario.id)


@router.get("/foto")
async def ver_foto(usuario: UsuarioActual = Depends(usuario_actual)) -> Response:
    """
    La foto propia. Con token y no pública: una URL sin autenticación
    dejaría ver la foto de cualquiera con solo conocer la ruta. El portal la
    pide con apiFetch y la muestra como blob.
    """
    fila = await fetch_one(
        "SELECT contenido, mime FROM portal_user_fotos WHERE portal_user_id = $1", usuario.id
    )
    if fila is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sin foto de perfil")
    return Response(
        content=bytes(fila["contenido"]),
        media_type=fila["mime"],
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


@router.delete("/foto", status_code=status.HTTP_204_NO_CONTENT)
async def borrar_foto(usuario: UsuarioActual = Depends(usuario_actual)) -> Response:
    await execute("DELETE FROM portal_user_fotos WHERE portal_user_id = $1", usuario.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
