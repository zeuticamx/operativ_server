"""
Descarga pública de adjuntos salientes.

NeuroAPI/Meta no tienen sesión de portal: bajan el archivo de la URL que se
les manda en `link`. Por eso este es uno de los pocos endpoints sin JWT; lo
único que autoriza la descarga es el token aleatorio de 256 bits de
`message_attachments.token_publico`. Un token desconocido da 404, sin
distinguir "no existe" de "no es tuyo".
"""

from fastapi import APIRouter, HTTPException, Response, status

from services import adjuntos
from session import fetch_one

router = APIRouter(prefix="/media", tags=["media"])


@router.get("/{token}")
async def descargar(token: str) -> Response:
    # Tope de largo antes de ir a la BD: los tokens reales miden 43.
    fila = (
        await fetch_one(
            """
            SELECT mime, nombre, contenido
            FROM message_attachments
            WHERE token_publico = $1 AND direccion = 'out'
            """,
            token,
        )
        if 20 <= len(token) <= 64
        else None
    )
    if fila is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No encontrado")
    return Response(
        content=bytes(fila["contenido"]),
        media_type=fila["mime"],
        headers=adjuntos.cabeceras_descarga(fila["nombre"], privado=False),
    )
