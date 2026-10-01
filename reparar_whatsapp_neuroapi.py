"""
Repara las líneas de WhatsApp vinculadas por NeuroAPI Connect antes de
sql/30_whatsapp_neuroapi_credenciales.sql.

Esas filas de channel_credentials quedaron con access_token NULL y
bsp_provider 'meta', así que n8n no puede contestar por ellas. Este script
les guarda la API key de la plataforma (NEUROAPI_API_KEY) con
bsp_provider='neuroapi', con la misma función que usa ahora la vinculación.

Lee el mismo DATABASE_URL que la app, igual que aplicar_sql.py. Sin
--aplicar solo lista lo que cambiaría.

    python reparar_whatsapp_neuroapi.py              # revisar
    python reparar_whatsapp_neuroapi.py --aplicar    # guardar
"""

import asyncio
import sys

import asyncpg

from config import settings

# Solo tenants con una Connect Session completada: una línea de Kontesta
# también tiene access_token NULL y no hay que convertirla.
PENDIENTES = """
    SELECT c.tenant_id, c.phone_number_id, c.bsp_provider,
           c.access_token IS NULL AS sin_token
      FROM channel_credentials c
     WHERE c.channel_type = 'whatsapp'
       AND c.is_active
       AND c.phone_number_id IS NOT NULL
       AND (c.access_token IS NULL OR c.bsp_provider <> 'neuroapi')
       AND EXISTS (
            SELECT 1 FROM neuroapi_connect_sessions s
             WHERE s.tenant_id = c.tenant_id
               AND s.status = 'completado'
       )
     ORDER BY c.tenant_id
"""


async def main() -> None:
    aplicar = "--aplicar" in sys.argv[1:]

    if aplicar and not settings.NEUROAPI_API_KEY:
        raise SystemExit("NEUROAPI_API_KEY sin configurar: no hay nada que guardar.")

    conn = await asyncpg.connect(dsn=settings.DATABASE_URL)
    try:
        filas = await conn.fetch(PENDIENTES)
        if not filas:
            print("No hay líneas de NeuroAPI pendientes de reparar.")
            return

        for f in filas:
            print(
                f"  tenant={f['tenant_id']} phone_number_id={f['phone_number_id']} "
                f"bsp_provider={f['bsp_provider']} sin_token={f['sin_token']}"
            )

        if not aplicar:
            print(f"\n{len(filas)} línea(s). Correr con --aplicar para repararlas.")
            return

        async with conn.transaction():
            for f in filas:
                await conn.execute(
                    "SELECT set_whatsapp_neuroapi($1, $2, $3)",
                    f["tenant_id"],
                    f["phone_number_id"],
                    settings.NEUROAPI_API_KEY,
                )
        print(f"\n{len(filas)} línea(s) reparada(s).")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
