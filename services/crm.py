"""
Piezas compartidas del CRM de campo (clientes, visitas, tareas, reportes).

Lo que resuelve acá es siempre lo mismo: quién llama, sobre qué tenant y
qué parte de la cartera puede ver. Vive aparte porque los cuatro routers
tienen que responder igual a esa pregunta.

Regla de oro del módulo: el `tenant_id` sale SIEMPRE del usuario
autenticado, nunca del body ni del query string. Un tenant_id que llegue
del cliente se ignora.
"""

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

import asyncpg
from fastapi import Depends, HTTPException, status

from deps import (
    ROLES_GERENCIA,
    ROL_VENDEDOR,
    UsuarioActual,
    usuario_actual,
    vendedor_actual,
    VendedorActual,
)
from session import fetch_one


@dataclass
class AccesoCRM:
    """
    Alcance de quien llama.

    `vendedor_id` distingue los dos modos:
      - con valor  -> es un vendedor: solo ve y toca su propia cartera
      - None       -> es gerencia: ve todo el tenant
    """

    tenant_id: UUID
    vendedor_id: Optional[UUID]
    es_gerencia: bool
    # Para dejar rastro de quién hizo qué en notas y comentarios.
    etiqueta: str

    @property
    def es_vendedor(self) -> bool:
        return self.vendedor_id is not None


async def acceso_crm(
    usuario: UsuarioActual = Depends(usuario_actual),
) -> AccesoCRM:
    """
    Resuelve el alcance de la petición a partir del rol.

    Un vendedor se resuelve con `vendedor_actual`, que relee la ficha de
    BD y rechaza al desactivado. Gerencia y 'member' ven el tenant
    completo; escribir sobre clientes exige además `gerencia_actual`.
    """
    if usuario.role == ROL_VENDEDOR:
        v: VendedorActual = await vendedor_actual(usuario)
        return AccesoCRM(
            tenant_id=v.tenant_id,
            vendedor_id=v.id,
            es_gerencia=False,
            etiqueta=v.nombre,
        )

    if usuario.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El usuario no tiene un negocio asociado todavía",
        )

    return AccesoCRM(
        tenant_id=usuario.tenant_id,
        vendedor_id=None,
        es_gerencia=usuario.role in ROLES_GERENCIA,
        etiqueta=usuario.email,
    )


async def exigir_gerencia_crm(
    acceso: AccesoCRM = Depends(acceso_crm),
) -> AccesoCRM:
    """Para lo que solo puede hacer gerencia: alta y edición de clientes."""
    if not acceso.es_gerencia:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Hace falta ser dueño o administrador del negocio",
        )
    return acceso


# ============================================================
# Acceso a un cliente concreto
# ============================================================
_COLUMNAS_CLIENTE = """
    c.id, c.tenant_id, c.vendedor_id, c.nombre_negocio, c.contacto_nombre,
    c.telefono, c.direccion, c.latitud, c.longitud,
    c.radio_tolerancia_metros, c.estado, c.prioridad, c.notas,
    c.creado_en, c.actualizado_en
"""

SELECT_CLIENTE = f"""
    SELECT {_COLUMNAS_CLIENTE}, v.nombre AS vendedor_nombre
    FROM clientes c
    LEFT JOIN vendedores v ON v.id = c.vendedor_id
"""


async def cargar_cliente(
    cliente_id: UUID,
    acceso: AccesoCRM,
    conn: Optional[asyncpg.Connection] = None,
) -> asyncpg.Record:
    """
    Trae un cliente comprobando que quien llama pueda verlo.

    Distingue los dos fallos a propósito:

      404  no existe, o es de otro tenant. Se responden igual para que
           probar UUIDs no sirva para averiguar qué negocios existen en
           otras cuentas.

      403  existe en este mismo tenant pero es de otro vendedor. Acá sí
           se puede decir la verdad: es gente de la misma empresa, y un
           404 mandaría al vendedor a reportar un cliente "perdido" que
           en realidad solo no es suyo.
    """
    sql = f"{SELECT_CLIENTE} WHERE c.id = $1 AND c.tenant_id = $2"
    if conn is not None:
        fila = await conn.fetchrow(sql, cliente_id, acceso.tenant_id)
    else:
        fila = await fetch_one(sql, cliente_id, acceso.tenant_id)

    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cliente no encontrado",
        )

    if acceso.es_vendedor and fila["vendedor_id"] != acceso.vendedor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Ese cliente está en la cartera de otro vendedor",
        )

    return fila


async def vendedor_del_tenant(
    vendedor_id: UUID,
    tenant_id: UUID,
    conn: Optional[asyncpg.Connection] = None,
) -> asyncpg.Record:
    """Comprueba que un vendedor exista y sea de este negocio."""
    sql = """
        SELECT id, nombre, activo
        FROM vendedores
        WHERE id = $1 AND tenant_id = $2
    """
    if conn is not None:
        fila = await conn.fetchrow(sql, vendedor_id, tenant_id)
    else:
        fila = await fetch_one(sql, vendedor_id, tenant_id)

    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendedor no encontrado",
        )
    return fila
