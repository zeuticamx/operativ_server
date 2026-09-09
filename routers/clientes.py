"""
Cartera de clientes del CRM de campo.

Un vendedor solo ve y toca su propia cartera; gerencia ve la del negocio
entero y es la única que da de alta o edita. El `tenant_id` sale del
usuario autenticado, nunca del cliente HTTP.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from services.crm import (
    SELECT_CLIENTE,
    AccesoCRM,
    acceso_crm,
    cargar_cliente,
    exigir_gerencia_crm,
    vendedor_del_tenant,
)
from schemas import ClienteActualizarIn, ClienteCrearIn, ClienteOut
from session import fetch_all, fetch_one

router = APIRouter(prefix="/clientes", tags=["crm"])


@router.get("", response_model=list[ClienteOut])
async def listar(
    acceso: AccesoCRM = Depends(acceso_crm),
    estado: str | None = Query(None),
    prioridad: str | None = Query(None),
    buscar: str | None = Query(None, max_length=100, description="Nombre del negocio o del contacto"),
    vendedor_id: UUID | None = Query(None, description="Solo gerencia; un vendedor siempre ve la suya"),
    sin_vendedor: bool = Query(False, description="Solo clientes sin vendedor asignado"),
    limite: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """
    La cartera visible para quien llama.

    Para un vendedor el filtro por vendedor no es opcional: se fuerza a su
    propio id y se ignora lo que haya mandado en `vendedor_id`. Así el
    parámetro no sirve para asomarse a la cartera de un compañero.
    """
    filtro_vendedor = acceso.vendedor_id if acceso.es_vendedor else vendedor_id
    solo_sin_vendedor = sin_vendedor and not acceso.es_vendedor

    filas = await fetch_all(
        f"""
        {SELECT_CLIENTE}
        WHERE c.tenant_id = $1
          AND ($2::uuid IS NULL OR c.vendedor_id = $2)
          AND (NOT $3::boolean OR c.vendedor_id IS NULL)
          AND ($4::text IS NULL OR c.estado = $4)
          AND ($5::text IS NULL OR c.prioridad = $5)
          AND (
            $6::text IS NULL
            OR c.nombre_negocio  ILIKE '%' || $6 || '%'
            OR c.contacto_nombre ILIKE '%' || $6 || '%'
          )
        ORDER BY
            -- Los de prioridad alta primero: es una lista de ruta, no un
            -- listado alfabético.
            CASE c.prioridad WHEN 'alta' THEN 0 WHEN 'media' THEN 1 ELSE 2 END,
            c.nombre_negocio
        LIMIT $7 OFFSET $8
        """,
        acceso.tenant_id,
        filtro_vendedor,
        solo_sin_vendedor,
        estado,
        prioridad,
        buscar,
        limite,
        offset,
    )
    return [ClienteOut(**dict(f)) for f in filas]


@router.get("/{cliente_id}", response_model=ClienteOut)
async def detalle(
    cliente_id: UUID,
    acceso: AccesoCRM = Depends(acceso_crm),
):
    fila = await cargar_cliente(cliente_id, acceso)
    return ClienteOut(**dict(fila))


@router.post("", response_model=ClienteOut, status_code=201)
async def crear(
    datos: ClienteCrearIn,
    acceso: AccesoCRM = Depends(exigir_gerencia_crm),
):
    if datos.vendedor_id is not None:
        await vendedor_del_tenant(datos.vendedor_id, acceso.tenant_id)

    creado = await fetch_one(
        """
        INSERT INTO clientes (
            tenant_id, vendedor_id, nombre_negocio, contacto_nombre, telefono,
            direccion, latitud, longitud, radio_tolerancia_metros,
            estado, prioridad, notas
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        RETURNING id
        """,
        acceso.tenant_id,
        datos.vendedor_id,
        datos.nombre_negocio,
        datos.contacto_nombre,
        datos.telefono,
        datos.direccion,
        datos.latitud,
        datos.longitud,
        datos.radio_tolerancia_metros,
        datos.estado,
        datos.prioridad,
        datos.notas,
    )

    fila = await fetch_one(f"{SELECT_CLIENTE} WHERE c.id = $1", creado["id"])
    return ClienteOut(**dict(fila))


@router.put("/{cliente_id}", response_model=ClienteOut)
async def actualizar(
    cliente_id: UUID,
    datos: ClienteActualizarIn,
    acceso: AccesoCRM = Depends(exigir_gerencia_crm),
):
    """
    Edita un cliente. Parcial: los campos que no vengan se conservan.

    Mover el pin o cambiar el radio afecta solo a las visitas futuras. Las
    ya registradas guardan su propia distancia y su veredicto, así que no
    cambian de resultado por editar el cliente después.
    """
    await cargar_cliente(cliente_id, acceso)

    if datos.vendedor_id is not None:
        await vendedor_del_tenant(datos.vendedor_id, acceso.tenant_id)

    await fetch_one(
        """
        UPDATE clientes SET
            nombre_negocio          = COALESCE($3, nombre_negocio),
            contacto_nombre         = COALESCE($4, contacto_nombre),
            telefono                = COALESCE($5, telefono),
            direccion               = COALESCE($6, direccion),
            latitud                 = COALESCE($7, latitud),
            longitud                = COALESCE($8, longitud),
            radio_tolerancia_metros = COALESCE($9, radio_tolerancia_metros),
            estado                  = COALESCE($10, estado),
            prioridad               = COALESCE($11, prioridad),
            notas                   = COALESCE($12, notas),
            vendedor_id             = COALESCE($13, vendedor_id),
            actualizado_en          = NOW()
        WHERE id = $1 AND tenant_id = $2
        RETURNING id
        """,
        cliente_id,
        acceso.tenant_id,
        datos.nombre_negocio,
        datos.contacto_nombre,
        datos.telefono,
        datos.direccion,
        datos.latitud,
        datos.longitud,
        datos.radio_tolerancia_metros,
        datos.estado,
        datos.prioridad,
        datos.notas,
        datos.vendedor_id,
    )

    fila = await fetch_one(f"{SELECT_CLIENTE} WHERE c.id = $1", cliente_id)
    return ClienteOut(**dict(fila))


@router.post("/{cliente_id}/desasignar", response_model=ClienteOut, status_code=200)
async def desasignar(
    cliente_id: UUID,
    acceso: AccesoCRM = Depends(exigir_gerencia_crm),
):
    """
    Deja al cliente sin vendedor.

    Existe aparte porque el PUT usa COALESCE para la actualización parcial
    y ahí `vendedor_id: null` es indistinguible de "no lo mandes": no hay
    forma de quitar la asignación por esa vía.
    """
    await cargar_cliente(cliente_id, acceso)

    await fetch_one(
        """
        UPDATE clientes
        SET vendedor_id = NULL, actualizado_en = NOW()
        WHERE id = $1 AND tenant_id = $2
        RETURNING id
        """,
        cliente_id,
        acceso.tenant_id,
    )

    fila = await fetch_one(f"{SELECT_CLIENTE} WHERE c.id = $1", cliente_id)
    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cliente no encontrado",
        )
    return ClienteOut(**dict(fila))
