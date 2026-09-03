"""
Pool de Postgres con asyncpg.

Se usa SQL directo (no ORM) a propósito: todas las operaciones sensibles
(credenciales, resolución de tenant) ya viven en funciones SQL con
SECURITY DEFINER, y meter un ORM encima solo agregaría una capa
que hay que traducir de ida y vuelta.
"""

from typing import Any, Optional

import asyncpg

from config import settings

_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> None:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL,
            min_size=2,
            max_size=10,
            command_timeout=30,
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
