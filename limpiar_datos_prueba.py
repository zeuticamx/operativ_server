"""
Borra los datos ficticios del test de punta a punta del CRM de campo.

    python limpiar_datos_prueba.py --revisar   # solo mira, no borra
    python limpiar_datos_prueba.py --borrar

Solo toca filas marcadas con el prefijo [PRUEBA] o del dominio de correo
de pruebas, y solo dentro del tenant de demo. No borra tablas ni esquema:
las migraciones 06 y 07 se quedan aplicadas.

El orden es el inverso al de las claves foráneas: visitas y tareas
primero, después clientes, vendedores y por último portal_users.
"""

import asyncio
import sys

import asyncpg

from config import settings

# 'Negocio Demo'. Nunca un tenant de un cliente real.
TENANT_DEMO = "21d4217f-aab7-439b-9db0-72667399989f"
PREFIJO = "[PRUEBA]%"
DOMINIO_CORREO = "%@pruebas-operativai.com"


async def contar(conn: asyncpg.Connection) -> dict[str, int]:
    return {
        "visitas": await conn.fetchval(
            """
            SELECT COUNT(*) FROM visitas
            WHERE tenant_id = $1
              AND vendedor_id IN (SELECT id FROM vendedores
                                  WHERE tenant_id = $1 AND nombre LIKE $2)
            """,
            TENANT_DEMO, PREFIJO),
        "tareas_seguimiento": await conn.fetchval(
            """
            SELECT COUNT(*) FROM tareas_seguimiento
            WHERE tenant_id = $1
              AND vendedor_id IN (SELECT id FROM vendedores
                                  WHERE tenant_id = $1 AND nombre LIKE $2)
            """,
            TENANT_DEMO, PREFIJO),
        "clientes": await conn.fetchval(
            "SELECT COUNT(*) FROM clientes WHERE tenant_id=$1 AND nombre_negocio LIKE $2",
            TENANT_DEMO, PREFIJO),
        "vendedores": await conn.fetchval(
            "SELECT COUNT(*) FROM vendedores WHERE tenant_id=$1 AND nombre LIKE $2",
            TENANT_DEMO, PREFIJO),
        "portal_users": await conn.fetchval(
            "SELECT COUNT(*) FROM portal_users WHERE email LIKE $1", DOMINIO_CORREO),
    }


async def borrar(conn: asyncpg.Connection) -> None:
    async with conn.transaction():
        await conn.execute(
            """
            DELETE FROM visitas
            WHERE tenant_id = $1
              AND (cliente_id IN (SELECT id FROM clientes
                                  WHERE tenant_id = $1 AND nombre_negocio LIKE $2)
               OR vendedor_id IN (SELECT id FROM vendedores
                                  WHERE tenant_id = $1 AND nombre LIKE $2))
            """, TENANT_DEMO, PREFIJO)

        await conn.execute(
            """
            DELETE FROM tareas_seguimiento
            WHERE tenant_id = $1
              AND (cliente_id IN (SELECT id FROM clientes
                                  WHERE tenant_id = $1 AND nombre_negocio LIKE $2)
               OR vendedor_id IN (SELECT id FROM vendedores
                                  WHERE tenant_id = $1 AND nombre LIKE $2))
            """, TENANT_DEMO, PREFIJO)

        await conn.execute(
            "DELETE FROM clientes WHERE tenant_id = $1 AND nombre_negocio LIKE $2",
            TENANT_DEMO, PREFIJO)

        # El puntero del round-robin puede apuntar a un vendedor de prueba.
        await conn.execute(
            """
            UPDATE tenant_vendedor_config
            SET ultimo_vendedor_asignado_id = NULL
            WHERE tenant_id = $1
              AND ultimo_vendedor_asignado_id IN (
                  SELECT id FROM vendedores WHERE tenant_id = $1 AND nombre LIKE $2)
            """, TENANT_DEMO, PREFIJO)

        await conn.execute(
            "DELETE FROM vendedores WHERE tenant_id = $1 AND nombre LIKE $2",
            TENANT_DEMO, PREFIJO)

        await conn.execute(
            "DELETE FROM portal_users WHERE email LIKE $1", DOMINIO_CORREO)


async def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] not in ("--revisar", "--borrar"):
        print(__doc__)
        raise SystemExit(1)

    conn = await asyncpg.connect(dsn=settings.DATABASE_URL)
    try:
        antes = await contar(conn)
        print("Datos de prueba encontrados:")
        for tabla, n in antes.items():
            print(f"  {tabla:<20} {n}")

        if args[0] == "--revisar":
            print("\n(solo revisión; nada se borró)")
            return

        if not any(antes.values()):
            print("\nNo hay nada que borrar.")
            return

        await borrar(conn)
        print("\nDespués de borrar:")
        for tabla, n in (await contar(conn)).items():
            print(f"  {tabla:<20} {n}")
        print("\nLas tablas y las migraciones 06/07 siguen aplicadas.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
