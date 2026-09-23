"""
Lógica del panel de plataforma (nivel gerencia).

Acá vive lo que no es HTTP: el rango de fechas que mira el panel, el
agregado de consumo de tokens y el registro en la bitácora. routers/
gerencia.py se queda con los códigos de estado y la forma de la respuesta.

Nada de este módulo filtra por tenant. Es a propósito: el nivel gerencia
mira la plataforma completa, y quién puede llegar hasta acá ya lo decidió
deps.gerencia_plataforma_actual.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

import asyncpg

from session import execute, fetch_all, fetch_one, fetch_value

# Ventana por defecto del panel. Un mes es lo que dura un ciclo de cobro,
# así que es la comparación que gerencia hace mentalmente igual.
DIAS_POR_DEFECTO = 30

# Tope duro. Más allá de esto el panel deja de ser una pantalla y pasa a
# ser una exportación — mismo criterio que routers/reportes.py.
DIAS_MAXIMO = 366


# El gate de pagos escrito en SQL, para poder resolverlo de una sola vez
# para toda la página en vez de una consulta por fila.
#
# OJO: es un espejo de services/acceso_pagos.py, que sigue siendo la
# fuente de verdad (es la que responde n8n en /api/eventos). Si cambia la
# regla allá, hay que cambiarla acá — tests/test_gerencia.py compara las
# dos implementaciones justamente para que no se separen en silencio.
#
# IS NOT DISTINCT FROM y no `= 'activa'`: un tenant sin suscripción tiene
# estado_suscripcion NULL, y `NULL = 'activa'` da NULL, no FALSE. Ese NULL
# se propaga por el OR y el resultado entero sale NULL — que en JSON viaja
# como `null` y rompe el booleano del schema.
SQL_AGENTE_OPERANDO = """
    g.agente_ia_activo
    AND g.estado NOT IN ('suspendido', 'baja')
    AND (
        (g.estado_suscripcion IS NULL AND cr.creditos_disponibles IS NULL)
        OR g.estado_suscripcion IS NOT DISTINCT FROM 'activa'
        OR COALESCE(cr.creditos_disponibles, 0) > 0
    )
"""


@dataclass
class Rango:
    desde: datetime
    hasta: datetime
    dias: int


async def rango_dias(dias: int | None) -> Rango:
    """
    Ventana [ahora - dias, ahora), acotada a DIAS_MAXIMO.

    El "ahora" sale de Postgres (`SELECT NOW()`) y no de `datetime.now()` a
    propósito: las filas que se cuentan las estampa la base con su propio
    reloj, y el backend puede correr en otra máquina. Con unos segundos de
    desfase alcanza para que el consumo recién escrito quede fuera de una
    ventana que termina "ahora" — se pierde justo lo último, que es lo que
    alguien está mirando cuando abre el panel.
    """
    total = min(max(dias or DIAS_POR_DEFECTO, 1), DIAS_MAXIMO)
    hasta: datetime = await fetch_value("SELECT NOW()")
    return Rango(desde=hasta - timedelta(days=total), hasta=hasta, dias=total)


# ============================================================
# Consumo de tokens
# ============================================================
@dataclass
class Consumo:
    tokens_entrada: int
    tokens_salida: int
    tokens_total: int
    costo_usd: Decimal
    llamadas: int


VACIO = Consumo(0, 0, 0, Decimal(0), 0)


def _consumo_de_fila(fila: Optional[asyncpg.Record]) -> Consumo:
    """
    SUM() sobre cero filas devuelve NULL, no 0. Un tenant que todavía no
    consumió nada es el caso normal el primer día, no un error, así que se
    normaliza a cero acá en vez de repetir COALESCE en cada consulta.
    """
    if fila is None:
        return VACIO
    return Consumo(
        tokens_entrada=fila["tokens_entrada"] or 0,
        tokens_salida=fila["tokens_salida"] or 0,
        tokens_total=fila["tokens_total"] or 0,
        costo_usd=fila["costo_usd"] or Decimal(0),
        llamadas=fila["llamadas"] or 0,
    )


async def consumo_global(rango: Rango, tenant_id: UUID | None = None) -> Consumo:
    fila = await fetch_one(
        """
        SELECT
            SUM(tokens_entrada) AS tokens_entrada,
            SUM(tokens_salida)  AS tokens_salida,
            SUM(tokens_total)   AS tokens_total,
            SUM(costo_usd)      AS costo_usd,
            COUNT(*)            AS llamadas
        FROM tenant_token_usage
        WHERE created_at >= $1
          AND created_at <  $2
          AND ($3::uuid IS NULL OR tenant_id = $3)
        """,
        rango.desde,
        rango.hasta,
        tenant_id,
    )
    return _consumo_de_fila(fila)


async def consumo_por_dia(
    rango: Rango, tenant_id: UUID | None = None
) -> list[asyncpg.Record]:
    """
    Serie diaria para el gráfico. Los días sin consumo NO vienen en el
    resultado: rellenarlos es cosa de quien dibuja, que es el único que
    sabe si quiere una línea con huecos o con ceros.
    """
    return await fetch_all(
        """
        SELECT
            (created_at AT TIME ZONE 'UTC')::date AS dia,
            SUM(tokens_total) AS tokens_total,
            SUM(costo_usd)    AS costo_usd,
            COUNT(*)          AS llamadas
        FROM tenant_token_usage
        WHERE created_at >= $1
          AND created_at <  $2
          AND ($3::uuid IS NULL OR tenant_id = $3)
        GROUP BY 1
        ORDER BY 1
        """,
        rango.desde,
        rango.hasta,
        tenant_id,
    )


async def consumo_por_modelo(
    rango: Rango, tenant_id: UUID | None = None
) -> list[asyncpg.Record]:
    """
    Desglose por modelo, de mayor a menor. Sirve para lo que motiva mirar
    esta pantalla: ver si el gasto se fue a un modelo caro que quedó
    configurado en un tenant y nadie revisó.
    """
    return await fetch_all(
        """
        SELECT
            COALESCE(modelo, 'sin especificar') AS modelo,
            SUM(tokens_total) AS tokens_total,
            SUM(costo_usd)    AS costo_usd,
            COUNT(*)          AS llamadas
        FROM tenant_token_usage
        WHERE created_at >= $1
          AND created_at <  $2
          AND ($3::uuid IS NULL OR tenant_id = $3)
        GROUP BY 1
        ORDER BY 2 DESC
        """,
        rango.desde,
        rango.hasta,
        tenant_id,
    )


async def registrar_uso_tokens(
    tenant_id: UUID,
    *,
    conversation_id: UUID | None,
    origen: str,
    modelo: str | None,
    tokens_entrada: int,
    tokens_salida: int,
    costo_usd: Decimal | None,
    idempotency_key: str | None,
) -> bool:
    """
    Anota una llamada al modelo. Devuelve False si la clave de idempotencia
    ya estaba: n8n reintentó y el consumo ya se había contado.

    ON CONFLICT DO NOTHING y no un SELECT previo: entre el SELECT y el
    INSERT cabe perfectamente el reintento del mismo nodo, y el índice
    único es el que de verdad garantiza que no se cuente dos veces.
    """
    resultado = await execute(
        """
        INSERT INTO tenant_token_usage
            (tenant_id, conversation_id, origen, modelo,
             tokens_entrada, tokens_salida, costo_usd, idempotency_key)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
        DO NOTHING
        """,
        tenant_id,
        conversation_id,
        origen,
        modelo,
        tokens_entrada,
        tokens_salida,
        costo_usd,
        idempotency_key,
    )
    # asyncpg devuelve el command tag; "INSERT 0 0" = no insertó nada.
    return resultado.endswith(" 1")


# ============================================================
# Bitácora
# ============================================================
async def registrar_auditoria(
    *,
    actor_email: str,
    actor_portal_user_id: UUID | None,
    accion: str,
    tenant_id: UUID | None,
    detalle: dict[str, Any],
    conn: asyncpg.Connection | None = None,
) -> None:
    """
    Deja constancia de una acción de gerencia.

    Acepta `conn` para poder escribir dentro de la misma transacción que el
    cambio que documenta: una suspensión que se aplica pero no se registra
    es peor que una que no se aplicó.
    """
    sql = """
        INSERT INTO gerencia_auditoria
            (actor_email, actor_portal_user_id, accion, tenant_id, detalle)
        VALUES ($1, $2, $3, $4, $5::jsonb)
    """
    args = (actor_email, actor_portal_user_id, accion, tenant_id, detalle)

    if conn is not None:
        await conn.execute(sql, *args)
    else:
        await execute(sql, *args)
