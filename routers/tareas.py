"""
Tareas de seguimiento del CRM de campo.

Un vendedor ve, crea y completa las suyas; gerencia ve las del negocio
entero y puede asignarlas a cualquiera de su equipo.
"""

from datetime import datetime
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from services.crm import AccesoCRM, acceso_crm, cargar_cliente, vendedor_del_tenant
from schemas import TareaActualizarIn, TareaCrearIn, TareaOut
from session import conexion, fetch_all, transaccion

router = APIRouter(prefix="/tareas", tags=["crm"])

SELECT_TAREA = """
    SELECT
        t.id, t.tenant_id, t.vendedor_id, t.cliente_id, t.titulo,
        t.descripcion, t.fecha_programada, t.estado, t.completado_en,
        t.creado_en,
        v.nombre AS vendedor_nombre,
        c.nombre_negocio AS cliente_nombre_negocio
    FROM tareas_seguimiento t
    JOIN vendedores v ON v.id = t.vendedor_id
    JOIN clientes   c ON c.id = t.cliente_id
"""


async def _cargar_tarea(
    tarea_id: UUID,
    acceso: AccesoCRM,
    conn: asyncpg.Connection | None = None,
) -> asyncpg.Record:
    """
    Trae una tarea comprobando permisos, con la misma regla que clientes:
    404 si no existe o es de otro tenant, 403 si es de otro vendedor del
    mismo negocio.
    """
    sql = f"{SELECT_TAREA} WHERE t.id = $1 AND t.tenant_id = $2"
    if conn is not None:
        fila = await conn.fetchrow(sql, tarea_id, acceso.tenant_id)
    else:
        async with conexion() as propia:
            fila = await propia.fetchrow(sql, tarea_id, acceso.tenant_id)

    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tarea no encontrada",
        )

    if acceso.es_vendedor and fila["vendedor_id"] != acceso.vendedor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esa tarea es de otro vendedor",
        )

    return fila


@router.get("", response_model=list[TareaOut])
async def listar(
    acceso: AccesoCRM = Depends(acceso_crm),
    estado: str | None = Query(None),
    cliente_id: UUID | None = Query(None),
    vendedor_id: UUID | None = Query(None, description="Solo gerencia; un vendedor ve las suyas"),
    desde: datetime | None = Query(None, description="fecha_programada >= "),
    hasta: datetime | None = Query(None, description="fecha_programada < (exclusivo)"),
    limite: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """
    Las tareas visibles para quien llama, ordenadas por fecha programada:
    lo más próximo primero, que es como se trabaja una agenda.
    """
    filtro_vendedor = acceso.vendedor_id if acceso.es_vendedor else vendedor_id

    filas = await fetch_all(
        f"""
        {SELECT_TAREA}
        WHERE t.tenant_id = $1
          AND ($2::uuid IS NULL OR t.vendedor_id = $2)
          AND ($3::uuid IS NULL OR t.cliente_id = $3)
          AND ($4::text IS NULL OR t.estado = $4)
          AND ($5::timestamptz IS NULL OR t.fecha_programada >= $5)
          AND ($6::timestamptz IS NULL OR t.fecha_programada <  $6)
        ORDER BY t.fecha_programada
        LIMIT $7 OFFSET $8
        """,
        acceso.tenant_id,
        filtro_vendedor,
        cliente_id,
        estado,
        desde,
        hasta,
        limite,
        offset,
    )
    return [TareaOut(**dict(f)) for f in filas]


@router.get("/{tarea_id}", response_model=TareaOut)
async def detalle(tarea_id: UUID, acceso: AccesoCRM = Depends(acceso_crm)):
    return TareaOut(**dict(await _cargar_tarea(tarea_id, acceso)))


@router.post("", response_model=TareaOut, status_code=201)
async def crear(
    datos: TareaCrearIn,
    acceso: AccesoCRM = Depends(acceso_crm),
):
    """
    Crea una tarea.

    Un vendedor solo puede crearla para sí mismo sobre un cliente de su
    cartera: el `vendedor_id` del body se ignora. Gerencia sí puede
    asignarla a quien quiera de su equipo.
    """
    if acceso.es_vendedor:
        destino = acceso.vendedor_id
    elif datos.vendedor_id is not None:
        await vendedor_del_tenant(datos.vendedor_id, acceso.tenant_id)
        destino = datos.vendedor_id
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Falta vendedor_id: indica a qué vendedor se le asigna la tarea",
        )

    async with transaccion() as conn:
        # Valida existencia y permiso sobre el cliente antes de insertar.
        cliente = await cargar_cliente(datos.cliente_id, acceso, conn)

        # Gerencia puede asignar la tarea a un vendedor distinto del dueño
        # del cliente (una cobertura, una visita de apoyo), pero no a un
        # cliente que no tenga dueño y encima de otro tenant: eso ya lo
        # cubrió cargar_cliente.
        creada = await conn.fetchrow(
            """
            INSERT INTO tareas_seguimiento
                (tenant_id, vendedor_id, cliente_id, titulo, descripcion, fecha_programada)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            acceso.tenant_id,
            destino,
            cliente["id"],
            datos.titulo,
            datos.descripcion,
            datos.fecha_programada,
        )
        fila = await conn.fetchrow(f"{SELECT_TAREA} WHERE t.id = $1", creada["id"])

    return TareaOut(**dict(fila))


@router.put("/{tarea_id}", response_model=TareaOut)
async def actualizar(
    tarea_id: UUID,
    datos: TareaActualizarIn,
    acceso: AccesoCRM = Depends(acceso_crm),
):
    """
    Edita una tarea. Parcial: lo que no venga se conserva.

    Reasignarla a otro vendedor es cosa de gerencia. Y 'completada' no se
    pone por acá — para eso está POST /tareas/{id}/completar, que sella
    `completado_en` junto con el estado en un solo paso.
    """
    async with transaccion() as conn:
        await _cargar_tarea(tarea_id, acceso, conn)

        if datos.vendedor_id is not None:
            if not acceso.es_gerencia:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Solo gerencia puede reasignar una tarea",
                )
            await vendedor_del_tenant(datos.vendedor_id, acceso.tenant_id, conn)

        await conn.execute(
            """
            UPDATE tareas_seguimiento SET
                titulo           = COALESCE($3, titulo),
                descripcion      = COALESCE($4, descripcion),
                fecha_programada = COALESCE($5, fecha_programada),
                estado           = COALESCE($6, estado),
                vendedor_id      = COALESCE($7, vendedor_id)
            WHERE id = $1 AND tenant_id = $2
            """,
            tarea_id,
            acceso.tenant_id,
            datos.titulo,
            datos.descripcion,
            datos.fecha_programada,
            datos.estado,
            datos.vendedor_id,
        )
        fila = await conn.fetchrow(f"{SELECT_TAREA} WHERE t.id = $1", tarea_id)

    return TareaOut(**dict(fila))


@router.post("/{tarea_id}/completar", response_model=TareaOut)
async def completar(
    tarea_id: UUID,
    acceso: AccesoCRM = Depends(acceso_crm),
):
    """
    Marca la tarea como completada y sella `completado_en`.

    Endpoint propio y no un PUT con `estado=completada` porque el CHECK de
    la tabla exige que estado y completado_en sean coherentes; dejarlo al
    que llama abriría la puerta a una tarea completada sin fecha.

    Completar dos veces no mueve la fecha original: se responde la tarea
    tal como quedó la primera vez, sin error. Una app offline puede mandar
    el mismo "listo" dos veces.
    """
    async with transaccion() as conn:
        actual = await _cargar_tarea(tarea_id, acceso, conn)

        if actual["estado"] == "completada":
            return TareaOut(**dict(actual))

        await conn.execute(
            """
            UPDATE tareas_seguimiento
            SET estado = 'completada', completado_en = NOW()
            WHERE id = $1 AND tenant_id = $2
            """,
            tarea_id,
            acceso.tenant_id,
        )
        fila = await conn.fetchrow(f"{SELECT_TAREA} WHERE t.id = $1", tarea_id)

    return TareaOut(**dict(fila))


@router.post("/{tarea_id}/reabrir", response_model=TareaOut)
async def reabrir(
    tarea_id: UUID,
    acceso: AccesoCRM = Depends(acceso_crm),
):
    """Devuelve una tarea a 'pendiente' y limpia `completado_en`."""
    async with transaccion() as conn:
        await _cargar_tarea(tarea_id, acceso, conn)
        await conn.execute(
            """
            UPDATE tareas_seguimiento
            SET estado = 'pendiente', completado_en = NULL
            WHERE id = $1 AND tenant_id = $2
            """,
            tarea_id,
            acceso.tenant_id,
        )
        fila = await conn.fetchrow(f"{SELECT_TAREA} WHERE t.id = $1", tarea_id)

    return TareaOut(**dict(fila))


@router.delete("/{tarea_id}", status_code=204)
async def eliminar(tarea_id: UUID, acceso: AccesoCRM = Depends(acceso_crm)):
    async with transaccion() as conn:
        await _cargar_tarea(tarea_id, acceso, conn)
        await conn.execute(
            "DELETE FROM tareas_seguimiento WHERE id = $1 AND tenant_id = $2",
            tarea_id,
            acceso.tenant_id,
        )
