"""
Pool de Postgres con asyncpg.

Se usa SQL directo (no ORM) a propósito: todas las operaciones sensibles
(credenciales, resolución de tenant) ya viven en funciones SQL con
SECURITY DEFINER, y meter un ORM encima solo agregaría una capa
que hay que traducir de ida y vuelta.
"""

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import asyncpg

from config import settings

_pool: Optional[asyncpg.Pool] = None


async def _configurar_conexion(conn: asyncpg.Connection) -> None:
    """
    Por defecto asyncpg trata json/jsonb como texto plano. Este códec
    convierte automáticamente dict de Python <-> jsonb de Postgres,
    para todo el pool.
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def init_pool() -> None:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL,
            min_size=2,
            max_size=10,
            command_timeout=30,
            init=_configurar_conexion,
        )


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("El pool no está inicializado. ¿Falta init_pool()?")
    return _pool


# ============================================================
# Helpers
# ============================================================
async def fetch_all(query: str, *args: Any) -> list[asyncpg.Record]:
    async with get_pool().acquire() as conn:
        return await conn.fetch(query, *args)


async def fetch_one(query: str, *args: Any) -> Optional[asyncpg.Record]:
    async with get_pool().acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetch_value(query: str, *args: Any) -> Any:
    async with get_pool().acquire() as conn:
        return await conn.fetchval(query, *args)


async def execute(query: str, *args: Any) -> str:
    async with get_pool().acquire() as conn:
        return await conn.execute(query, *args)


@asynccontextmanager
async def conexion() -> AsyncIterator[asyncpg.Connection]:
    """
    Una conexión para varias consultas seguidas.

    Los helpers de arriba toman y sueltan una conexión por consulta, así que
    dos llamadas seguidas pueden caer en conexiones distintas del pool. Para
    leer-y-decidir (por ejemplo elegir vendedor) hace falta que todo hable
    con la misma.
    """
    async with get_pool().acquire() as conn:
        yield conn


@asynccontextmanager
async def transaccion() -> AsyncIterator[asyncpg.Connection]:
    """
    Igual que `conexion`, pero todo lo de adentro se confirma o se descarta
    junto. Para escrituras que tienen que verse como una sola: cambiar el
    estado de un lead y anotarlo en la bitácora, por ejemplo.
    """
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            yield conn
