"""
Reportes de incidencias, lado equipo de plataforma (gerencia_users): ver la
lista, abrir uno, bajar su captura y marcar en qué estado va.

Separado de routers/incidencias.py (el envío, abierto a todo usuario del
portal): acá todo exige `gerencia_plataforma_actual`.
"""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from deps import UsuarioActual, gerencia_plataforma_actual
from schemas import EstadoReporte, ReporteEstadoIn, ReporteGerenciaOut
from services.gerencia import registrar_auditoria
from session import fetch_all, fetch_one, transaccion

router = APIRouter(
    prefix="/gerencia/incidencias",
    tags=["gerencia"],
    dependencies=[Depends(gerencia_plataforma_actual)],
)

_SELECT = """
    SELECT r.id, r.tenant_id, t.name AS nombre_negocio, r.email, r.resumen,
           r.descripcion, r.contexto, r.estado, r.creado_en,
           r.atendido_por, r.atendido_en,
           EXISTS (SELECT 1 FROM reportes_incidencia_adjuntos a
                    WHERE a.reporte_id = r.id) AS tiene_adjunto
      FROM reportes_incidencia r
      LEFT JOIN tenants t ON t.id = r.tenant_id
"""


def _out(f) -> ReporteGerenciaOut:
    datos = dict(f)
    if isinstance(datos["contexto"], str):  # asyncpg devuelve jsonb como texto
        datos["contexto"] = json.loads(datos["contexto"])
    return ReporteGerenciaOut(**datos)


@router.get("", response_model=list[ReporteGerenciaOut])
async def listar_incidencias(
    estado: EstadoReporte | None = Query(None),
    limite: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    filas = await fetch_all(
        _SELECT
        + """
        WHERE ($1::varchar IS NULL OR r.estado = $1)
        ORDER BY r.creado_en DESC
        LIMIT $2 OFFSET $3
        """,
        estado,
        limite,
        offset,
    )
    return [_out(f) for f in filas]


@router.get("/{reporte_id}", response_model=ReporteGerenciaOut)
async def detalle_incidencia(reporte_id: UUID):
    fila = await fetch_one(_SELECT + " WHERE r.id = $1", reporte_id)
    if fila is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reporte no encontrado")
    return _out(fila)


@router.get("/{reporte_id}/adjunto")
async def adjunto_incidencia(reporte_id: UUID) -> Response:
    fila = await fetch_one(
        "SELECT contenido, mime FROM reportes_incidencia_adjuntos WHERE reporte_id = $1",
        reporte_id,
    )
    if fila is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "El reporte no tiene adjunto")
    return Response(
        content=bytes(fila["contenido"]),
        media_type=fila["mime"],
        # El mime ya se validó por firma al guardar; nosniff impide que un
        # navegador reinterprete los bytes como otra cosa.
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"},
    )


@router.patch("/{reporte_id}", response_model=ReporteGerenciaOut)
async def actualizar_estado(
    reporte_id: UUID,
    datos: ReporteEstadoIn,
    gerente: UsuarioActual = Depends(gerencia_plataforma_actual),
):
    async with transaccion() as conn:
        actual = await conn.fetchrow(
            "SELECT estado, tenant_id FROM reportes_incidencia WHERE id = $1 FOR UPDATE",
            reporte_id,
        )
        if actual is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Reporte no encontrado")

        if actual["estado"] != datos.estado:
            await conn.execute(
                """
                UPDATE reportes_incidencia
                   SET estado = $2, atendido_por = $3, atendido_en = NOW()
                 WHERE id = $1
                """,
                reporte_id,
                datos.estado,
                gerente.email,
            )
            await registrar_auditoria(
                actor_email=gerente.email,
                actor_portal_user_id=gerente.id,
                accion="incidencia_actualizada",
                tenant_id=actual["tenant_id"],
                detalle={
                    "reporte_id": str(reporte_id),
                    "estado_anterior": actual["estado"],
                    "estado_nuevo": datos.estado,
                },
                conn=conn,
            )

    return _out(await fetch_one(_SELECT + " WHERE r.id = $1", reporte_id))
