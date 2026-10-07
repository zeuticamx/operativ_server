"""
Cartera de clientes del CRM de campo.

Un vendedor solo ve y toca su propia cartera, y puede dar de alta en ella
(el cliente nuevo le queda asignado a él). Editar, reasignar y desasignar
siguen siendo de gerencia, que además ve la cartera del negocio entero.
El `tenant_id` sale del usuario autenticado, nunca del cliente HTTP.
"""

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from deps import requiere_herramienta
from services.crm import (
    SELECT_CLIENTE,
    AccesoCRM,
    acceso_crm,
    cargar_cliente,
    exigir_gerencia_crm,
    vendedor_del_tenant,
)
from schemas import (
    ClienteActualizarIn,
    ClienteCrearIn,
    ClienteOut,
    ClienteVincularLeadIn,
    ContactoLeadOut,
)
from session import fetch_all, fetch_one

router = APIRouter(
    prefix="/clientes",
    tags=["crm"],
    dependencies=[Depends(requiere_herramienta("crm_campo"))],
)


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


@router.get("/leads-disponibles", response_model=list[ContactoLeadOut])
async def leads_disponibles(
    buscar: str = Query(..., min_length=2, max_length=100),
    acceso: AccesoCRM = Depends(exigir_gerencia_crm),
):
    """
    Busca contactos de chat (`users`) para vincular a un cliente de campo.

    Registrado ANTES de `GET /{cliente_id}` a propósito: si fuera después,
    "leads-disponibles" caería en esa ruta e intentaría parsearse como UUID.

    Excluye a los que ya están vinculados a otro cliente — el índice único
    parcial de la migración no deja vincular dos veces, así que ofrecerlos
    solo llevaría a un 409 al intentarlo.
    """
    filas = await fetch_all(
        """
        SELECT
            u.id AS user_id,
            NULLIF(NULLIF(TRIM(u.display_name), ''), 'null') AS nombre,
            COALESCE(
                NULLIF(NULLIF(TRIM(u.whatsapp_id), ''), 'null'),
                NULLIF(NULLIF(TRIM(u.instagram_id), ''), 'null'),
                NULLIF(NULLIF(TRIM(u.facebook_id), ''), 'null')
            ) AS handle,
            EXISTS(
                SELECT 1 FROM client_pipeline p
                WHERE p.user_id = u.id AND p.tenant_id = u.tenant_id
            ) AS tiene_pipeline
        FROM users u
        WHERE u.tenant_id = $1
          AND NOT EXISTS (SELECT 1 FROM clientes c WHERE c.user_id = u.id)
          AND (
            NULLIF(NULLIF(TRIM(u.display_name), ''), 'null') ILIKE '%' || $2 || '%'
            OR u.whatsapp_id  ILIKE '%' || $2 || '%'
            OR u.instagram_id ILIKE '%' || $2 || '%'
            OR u.facebook_id  ILIKE '%' || $2 || '%'
          )
        ORDER BY nombre NULLS LAST
        LIMIT 20
        """,
        acceso.tenant_id,
        buscar,
    )
    return [ContactoLeadOut(**dict(f)) for f in filas]


@router.patch("/{cliente_id}/vincular-lead", response_model=ClienteOut)
async def vincular_lead(
    cliente_id: UUID,
    datos: ClienteVincularLeadIn,
    acceso: AccesoCRM = Depends(exigir_gerencia_crm),
):
    """
    Enlaza este cliente con un contacto de chat que ya existe.

    No crea nada en `users` ni en `client_pipeline` — eso sigue siendo
    exclusivo de n8n (primer mensaje) o de `POST /pipeline/{user_id}/asignar`.
    Esto solo apunta el cliente de campo hacia un lead que ya está ahí.
    """
    await cargar_cliente(cliente_id, acceso)

    contacto = await fetch_one(
        "SELECT id FROM users WHERE id = $1 AND tenant_id = $2",
        datos.user_id,
        acceso.tenant_id,
    )
    if contacto is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Contacto no encontrado",
        )

    try:
        await fetch_one(
            """
            UPDATE clientes SET user_id = $3, actualizado_en = NOW()
            WHERE id = $1 AND tenant_id = $2
            RETURNING id
            """,
            cliente_id,
            acceso.tenant_id,
            datos.user_id,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ese contacto ya está vinculado a otro cliente de la cartera",
        )

    fila = await fetch_one(f"{SELECT_CLIENTE} WHERE c.id = $1", cliente_id)
    return ClienteOut(**dict(fila))


@router.post("/{cliente_id}/desvincular-lead", response_model=ClienteOut)
async def desvincular_lead(
    cliente_id: UUID,
    acceso: AccesoCRM = Depends(exigir_gerencia_crm),
):
    """Quita el enlace con el embudo. El lead en sí no se toca."""
    await cargar_cliente(cliente_id, acceso)

    await fetch_one(
        """
        UPDATE clientes SET user_id = NULL, actualizado_en = NOW()
        WHERE id = $1 AND tenant_id = $2
        RETURNING id
        """,
        cliente_id,
        acceso.tenant_id,
    )

    fila = await fetch_one(f"{SELECT_CLIENTE} WHERE c.id = $1", cliente_id)
    return ClienteOut(**dict(fila))


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
    acceso: AccesoCRM = Depends(acceso_crm),
):
    """
    Da de alta un cliente en la cartera.

    Un vendedor puede crearlo y le queda asignado a él: el `vendedor_id` del
    body se ignora, por lo mismo que en el listado — si se respetara, serviría
    para colgarle cartera a un compañero. Gerencia sí elige a quién se lo
    asigna, o lo deja sin asignar para repartirlo después.

    El vendedor que viene de `acceso` ya salió de `vendedor_actual`, que releyó
    su ficha y comprobó el tenant, así que ese id no se vuelve a validar; el que
    manda gerencia sí, porque es un UUID del cliente HTTP.
    """
    if acceso.es_vendedor:
        vendedor_id = acceso.vendedor_id
    else:
        vendedor_id = datos.vendedor_id
        if vendedor_id is not None:
            await vendedor_del_tenant(vendedor_id, acceso.tenant_id)

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
        vendedor_id,
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
