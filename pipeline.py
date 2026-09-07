"""
Acceso a datos del embudo de venta.

Vive aparte de los routers porque tiene dos consumidores con formas muy
distintas: los endpoints del portal (`vendedores.py`) y el endpoint que
llama n8n en cada mensaje entrante (`eventos.py`). Las reglas de quién
queda asignado y qué se anota en la bitácora tienen que ser las mismas por
los dos caminos.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional
from uuid import UUID

import asyncpg

from pipeline_estados import ESTADO_INICIAL
from session import conexion

# Columnas del embudo que devuelven todas las funciones de acá, para no
# tener tres SELECT que traen cosas ligeramente distintas.
_COLUMNAS = """
    id, tenant_id, user_id, vendedor_id, estado,
    monto_estimado, motivo_perdida, actualizado_en
"""


@dataclass(frozen=True)
class Servicios:
    tenant_id: UUID
    agente_ia_activo: bool
    gestion_vendedores_activo: bool


@dataclass(frozen=True)
class Pipeline:
    id: UUID
    tenant_id: UUID
    user_id: UUID
    vendedor_id: Optional[UUID]
    estado: str
    monto_estimado: Optional[Decimal]
    motivo_perdida: Optional[str]
    # True solo la primera vez que se ve a este cliente en el embudo.
    recien_creado: bool = False

    @classmethod
    def desde_fila(cls, fila: asyncpg.Record, recien_creado: bool = False) -> "Pipeline":
        return cls(
            id=fila["id"],
            tenant_id=fila["tenant_id"],
            user_id=fila["user_id"],
            vendedor_id=fila["vendedor_id"],
            estado=fila["estado"],
            monto_estimado=fila["monto_estimado"],
            motivo_perdida=fila["motivo_perdida"],
            recien_creado=recien_creado,
        )


# ============================================================
# Servicios del tenant
# ============================================================
async def get_tenant_servicios(
    tenant_id: UUID,
    conn: Optional[asyncpg.Connection] = None,
) -> Servicios:
    """
    Qué módulos tiene encendidos el tenant.

    Si no hay fila devuelve los valores por defecto (agente sí, vendedores
    no) en vez de fallar: es exactamente el estado de un tenant que se dio
    de alta y nunca tocó esta pantalla, y el endpoint de eventos no puede
    romperse por eso a mitad de un mensaje entrante.
    """
    sql = """
        SELECT tenant_id, agente_ia_activo, gestion_vendedores_activo
        FROM tenant_servicios
        WHERE tenant_id = $1
    """

    if conn is not None:
        fila = await conn.fetchrow(sql, tenant_id)
    else:
        async with conexion() as propia:
            fila = await propia.fetchrow(sql, tenant_id)

    if fila is None:
        return Servicios(
            tenant_id=tenant_id,
            agente_ia_activo=True,
            gestion_vendedores_activo=False,
        )

    return Servicios(
        tenant_id=fila["tenant_id"],
        agente_ia_activo=fila["agente_ia_activo"],
        gestion_vendedores_activo=fila["gestion_vendedores_activo"],
    )


async def set_tenant_servicios(
    tenant_id: UUID,
    agente_ia_activo: Optional[bool],
    gestion_vendedores_activo: Optional[bool],
) -> Servicios:
    """Actualización parcial: lo que llegue como None se deja como estaba."""
    async with conexion() as conn:
        fila = await conn.fetchrow(
            """
            INSERT INTO tenant_servicios
                (tenant_id, agente_ia_activo, gestion_vendedores_activo, actualizado_en)
            VALUES ($1, COALESCE($2, true), COALESCE($3, false), NOW())
            ON CONFLICT (tenant_id) DO UPDATE SET
                agente_ia_activo          = COALESCE($2, tenant_servicios.agente_ia_activo),
                gestion_vendedores_activo = COALESCE($3, tenant_servicios.gestion_vendedores_activo),
                actualizado_en            = NOW()
            RETURNING tenant_id, agente_ia_activo, gestion_vendedores_activo
            """,
            tenant_id,
            agente_ia_activo,
            gestion_vendedores_activo,
        )

    return Servicios(
        tenant_id=fila["tenant_id"],
        agente_ia_activo=fila["agente_ia_activo"],
        gestion_vendedores_activo=fila["gestion_vendedores_activo"],
    )


# ============================================================
# Embudo
# ============================================================
async def get_or_create_pipeline(
    tenant_id: UUID,
    user_id: UUID,
    conn: asyncpg.Connection,
) -> Pipeline:
    """
    La fila de embudo de este cliente, creándola en 'nuevo' si es la primera
    vez que escribe.

    El UPSERT es lo que respeta el UNIQUE (tenant_id, user_id): dos mensajes
    que entran a la vez no pueden crear dos embudos para el mismo cliente.
    El `DO UPDATE` que no cambia nada existe solo para que el RETURNING
    devuelva la fila también cuando ya existía (un DO NOTHING no devuelve
    nada). `xmax = 0` distingue el INSERT real del conflicto.
    """
    fila = await conn.fetchrow(
        f"""
        INSERT INTO client_pipeline (tenant_id, user_id, estado)
        VALUES ($1, $2, $3)
        ON CONFLICT (tenant_id, user_id) DO UPDATE
            SET actualizado_en = client_pipeline.actualizado_en
        RETURNING {_COLUMNAS}, (xmax = 0) AS creado
        """,
        tenant_id,
        user_id,
        ESTADO_INICIAL,
    )

    if fila["creado"]:
        # Primer punto de la línea de tiempo. Sin él, la métrica de tiempo
        # por etapa no sabría cuándo entró el lead a 'nuevo'.
        await conn.execute(
            """
            INSERT INTO pipeline_historial
                (client_pipeline_id, estado_anterior, estado_nuevo, nota)
            VALUES ($1, NULL, $2, 'Alta en el embudo')
            """,
            fila["id"],
            ESTADO_INICIAL,
        )

    return Pipeline.desde_fila(fila, recien_creado=fila["creado"])


async def get_pipeline_por_usuario(
    tenant_id: UUID,
    user_id: UUID,
    conn: asyncpg.Connection,
) -> Optional[Pipeline]:
    fila = await conn.fetchrow(
        f"SELECT {_COLUMNAS} FROM client_pipeline WHERE tenant_id = $1 AND user_id = $2",
        tenant_id,
        user_id,
    )
    return Pipeline.desde_fila(fila) if fila is not None else None


async def asignar_vendedor(
    pipeline_id: UUID,
    vendedor_id: Optional[UUID],
    conn: asyncpg.Connection,
    nota: str = "Asignación automática",
) -> Pipeline:
    """
    Deja al lead en manos de `vendedor_id` y lo anota en la bitácora.

    Sobrescribe al vendedor anterior si lo había: reasignar es un UPDATE de
    la fila que ya existe, nunca una fila nueva.

    La anotación va con estado_anterior = estado_nuevo. Es la convención que
    distingue una asignación de un cambio de etapa (ver 06_vendedores.sql):
    el lead no se movió de columna, cambió de dueño.
    """
    fila = await conn.fetchrow(
        f"""
        UPDATE client_pipeline
        SET vendedor_id = $2, actualizado_en = NOW()
        WHERE id = $1
        RETURNING {_COLUMNAS}
        """,
        pipeline_id,
        vendedor_id,
    )

    await conn.execute(
        """
        INSERT INTO pipeline_historial
            (client_pipeline_id, estado_anterior, estado_nuevo, vendedor_id, nota)
        VALUES ($1, $2, $2, $3, $4)
        """,
        pipeline_id,
        fila["estado"],
        vendedor_id,
        nota,
    )

    return Pipeline.desde_fila(fila)
