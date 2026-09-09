"""
Reportes de actividad de campo, para que gerencia los consuma desde el
portal web.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from services.crm import AccesoCRM, exigir_gerencia_crm
from schemas import ActividadVendedorOut, ReporteActividadOut
from session import fetch_all

router = APIRouter(prefix="/reportes", tags=["crm"])

# Ventana por defecto si no se pide un rango.
DIAS_POR_DEFECTO = 30
# Tope: más allá de esto el reporte deja de ser una pantalla y empieza a
# ser una exportación, que es otro problema.
DIAS_MAXIMO = 366


@router.get("/actividad", response_model=ReporteActividadOut)
async def actividad(
    acceso: AccesoCRM = Depends(exigir_gerencia_crm),
    desde: datetime | None = Query(None, description="Inclusivo. Por defecto, hace 30 días"),
    hasta: datetime | None = Query(None, description="Exclusivo. Por defecto, ahora"),
    vendedor_id: UUID | None = Query(None, description="Un solo vendedor en vez de todo el equipo"),
):
    """
    Visitas y tareas completadas por vendedor en un rango de fechas.

    Las visitas se cuentan por `timestamp_servidor` y no por la hora del
    teléfono: la del dispositivo puede venir desfasada o manipulada, y un
    reporte de productividad no puede depender de eso.

    Las tareas se cuentan por `completado_en`, no por `fecha_programada`:
    lo que mide el reporte es trabajo hecho dentro del rango, no trabajo
    que estaba agendado en él.

    Salen todos los vendedores del negocio, incluidos los que no hicieron
    nada (en cero) y los desactivados: un equipo con huecos es justo lo que
    gerencia necesita ver.
    """
    ahora = datetime.now(timezone.utc)
    hasta_final = hasta or ahora
    desde_final = desde or (hasta_final - timedelta(days=DIAS_POR_DEFECTO))

    if desde_final >= hasta_final:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El inicio del rango tiene que ser anterior al final",
        )

    if (hasta_final - desde_final).days > DIAS_MAXIMO:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"El rango no puede pasar de {DIAS_MAXIMO} días",
        )

    # Dos consultas y no un JOIN de las tres tablas: unir visitas y tareas
    # en la misma fila multiplicaría los conteos (cada visita aparecería
    # una vez por tarea del mismo vendedor). Se cruzan en Python.
    filas_visitas = await fetch_all(
        """
        SELECT
            v.id AS vendedor_id, v.nombre, v.activo,
            COUNT(vi.id) AS visitas,
            COUNT(vi.id) FILTER (WHERE vi.dentro_de_geocerca) AS visitas_validadas,
            COUNT(DISTINCT vi.cliente_id) AS clientes_visitados
        FROM vendedores v
        LEFT JOIN visitas vi
               ON vi.vendedor_id = v.id
              AND vi.timestamp_servidor >= $2
              AND vi.timestamp_servidor <  $3
        WHERE v.tenant_id = $1
          AND ($4::uuid IS NULL OR v.id = $4)
        GROUP BY v.id, v.nombre, v.activo
        """,
        acceso.tenant_id,
        desde_final,
        hasta_final,
        vendedor_id,
    )

    filas_tareas = await fetch_all(
        """
        SELECT
            t.vendedor_id,
            COUNT(*) FILTER (
                WHERE t.estado = 'completada'
                  AND t.completado_en >= $2
                  AND t.completado_en <  $3
            ) AS completadas,
            -- Las pendientes son foto del momento, no del rango: es lo que
            -- le queda por hacer al vendedor hoy.
            COUNT(*) FILTER (WHERE t.estado = 'pendiente') AS pendientes
        FROM tareas_seguimiento t
        WHERE t.tenant_id = $1
          AND ($4::uuid IS NULL OR t.vendedor_id = $4)
        GROUP BY t.vendedor_id
        """,
        acceso.tenant_id,
        desde_final,
        hasta_final,
        vendedor_id,
    )

    tareas_por_vendedor = {f["vendedor_id"]: f for f in filas_tareas}

    vendedores: list[ActividadVendedorOut] = []
    for f in filas_visitas:
        t = tareas_por_vendedor.get(f["vendedor_id"])
        visitas = f["visitas"]
        validadas = f["visitas_validadas"]

        vendedores.append(
            ActividadVendedorOut(
                vendedor_id=f["vendedor_id"],
                nombre=f["nombre"],
                activo=f["activo"],
                visitas=visitas,
                visitas_validadas=validadas,
                visitas_fuera_geocerca=visitas - validadas,
                clientes_visitados=f["clientes_visitados"],
                tareas_completadas=t["completadas"] if t else 0,
                tareas_pendientes=t["pendientes"] if t else 0,
                porcentaje_validadas=(
                    round(100.0 * validadas / visitas, 2) if visitas else None
                ),
            )
        )

    # Más visitas primero; a igualdad, más tareas cerradas.
    vendedores.sort(key=lambda v: (-v.visitas, -v.tareas_completadas, v.nombre))

    return ReporteActividadOut(
        desde=desde_final,
        hasta=hasta_final,
        total_visitas=sum(v.visitas for v in vendedores),
        total_tareas_completadas=sum(v.tareas_completadas for v in vendedores),
        vendedores=vendedores,
    )
