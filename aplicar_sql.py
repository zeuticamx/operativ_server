"""
Aplica archivos .sql contra la MISMA base que usa el backend.

Pensado para correr dentro del contenedor desplegado, donde no hay cliente
psql pero sí Python y asyncpg. Al leer el mismo DATABASE_URL que la app, lo
que se aplique cae sí o sí en la base donde el backend escribe — que es
justamente lo que hay que garantizar para que n8n lea lo mismo.

    python aplicar_sql.py --revisar
    python aplicar_sql.py sql/01_portal.sql sql/02_token_expires_tz.sql
"""

import asyncio
import sys
from pathlib import Path
from urllib.parse import urlparse

import asyncpg

from config import settings

# Las migraciones viven en sql/. Se acepta tanto "01_portal.sql" como
# "sql/01_portal.sql" para no romper comandos que alguien tenga
# copiados de antes de que se movieran a la carpeta.
SQL_DIR = Path(__file__).parent / "sql"

# Tablas y funciones que nos importan para saber si estamos en la base correcta.
TABLAS = [
    ("tenants", "n8n"),
    ("users", "n8n"),
    ("conversations", "n8n"),
    ("messages", "n8n"),
    ("channel_credentials", "n8n"),
    ("tenant_channels", "n8n"),
    ("tenant_agent_config", "n8n"),
    # Las escribe el envío del agente (services/agente/envio.py). Igual que
    # las funciones de abajo, nacieron del lado de n8n y vivieron solo en la
    # base; ya están versionadas en sql/54_agente_envio_tablas.sql. Sin
    # `delivery_errors` no se cae nada: se pierden los diagnósticos de envío
    # en silencio, así que conviene verla listada.
    ("delivery_errors", "n8n"),
    ("tenant_templates", "n8n"),
    # Del backend, no de n8n: la lista de bajas que cierra el hallazgo 1
    # (sql/55_agente_bajas.sql). Sin ella, `bajas.esta_de_baja` falla cerrado
    # y NINGUNA plantilla de reapertura sale, así que conviene verla listada.
    ("agente_bajas", "portal"),
    # Varios números de WhatsApp por negocio (spec 005a, sql/56 a 59). Sin
    # `agentes_canal` el código nuevo no sabe a qué negocio atribuir un mensaje
    # de WhatsApp, así que conviene verlas listadas.
    ("agentes_canal", "portal"),
    ("config_referencia_negocio", "portal"),
    ("agente_canal_herramientas", "portal"),
    ("social_herramientas", "portal"),
    ("numeros_bitacora", "portal"),
    ("intentos_alta_ajena", "portal"),
    ("plan_herramientas", "portal"),
    ("numeros_reevaluacion_cola", "portal"),
    ("numeros_despliegue", "portal"),
    ("numeros_recortes_pendientes", "portal"),
    ("portal_users", "portal"),
    ("meta_connections", "portal"),
    ("email_verifications", "portal"),
    ("tenant_servicios", "portal"),
    ("tenant_onboarding", "portal"),
    ("vendedores", "portal"),
    ("tenant_vendedor_config", "portal"),
    ("client_pipeline", "portal"),
    ("pipeline_historial", "portal"),
    ("dispositivos_push", "portal"),
    ("clientes", "portal"),
    ("visitas", "portal"),
    ("tareas_seguimiento", "portal"),
    ("agenda_reprogramaciones", "portal"),
    ("gerencia_users", "portal"),
    ("tenant_estado_plataforma", "portal"),
    ("tenant_token_usage", "portal"),
    ("gerencia_auditoria", "portal"),
    ("gerencia_alertas", "portal"),
]

FUNCIONES = [
    ("resuelve_tenant", "n8n"),
    ("set_channel_credentials", "n8n"),
    # Las usa el agente en código (services/agente/). Se crearon del lado de
    # n8n y durante un tiempo vivieron solo en la base; ya están versionadas:
    # resuelve_tenant y dentro_de_ventana_24h en sql/52_agente_funciones.sql,
    # obtiene_app_secret en sql/53, que además retira su clave de cifrado de
    # ejemplo — leer ese archivo entero antes de aplicarlo.
    ("obtiene_app_secret", "n8n"),
    ("dentro_de_ventana_24h", "n8n"),
    # Spec 005a (sql/56 a 59): reglas efectivas del negocio y resolución de
    # números. `resuelve_tenant` (arriba) cambia en 58: busca en agentes_canal.
    ("configuracion_incompleta", "portal"),
    ("familia_de_herramienta", "portal"),
    ("situacion_comercial", "portal"),
    ("plan_mas_bajo_activo", "portal"),
    ("limites_efectivos", "portal"),
    ("recortar_agente_a_plan", "portal"),
    ("resuelve_numero", "portal"),
    ("numeros_encolar_reevaluacion", "portal"),
    ("set_meta_connection", "portal"),
    ("get_meta_connection", "portal"),
    ("distancia_metros", "portal"),
    # Instagram Direct vía NeuroAPI (sql/69). Sin ella, `instagram.connected`
    # deja la sesión completada pero el canal sin credenciales.
    ("set_instagram_neuroapi", "portal"),
]


def dsn_sin_password(dsn: str) -> str:
    """Para poder mostrar a qué base apunta sin filtrar la contraseña."""
    try:
        p = urlparse(dsn)
        host = p.hostname or "?"
        puerto = p.port or 5432
        base = (p.path or "/").lstrip("/") or "?"
        usuario = p.username or "?"
        return f"{usuario}@{host}:{puerto}/{base}"
    except Exception:
        return "(no se pudo interpretar DATABASE_URL)"


async def revisar(conn: asyncpg.Connection) -> None:
    base = await conn.fetchval("SELECT current_database()")
    version = await conn.fetchval("SHOW server_version")
    print(f"Base de datos : {base}")
    print(f"Postgres      : {version}")
    print(f"DATABASE_URL  : {dsn_sin_password(settings.DATABASE_URL)}")
    print()

    existentes = {
        r["tablename"]
        for r in await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE tablename = ANY($1::text[])",
            [t for t, _ in TABLAS],
        )
    }
    print("TABLAS")
    for tabla, origen in TABLAS:
        marca = "si" if tabla in existentes else "NO"
        print(f"  [{marca:>2}] {tabla:<22} ({origen})")

    funcs = {
        r["proname"]
        for r in await conn.fetch(
            "SELECT proname FROM pg_proc WHERE proname = ANY($1::text[])",
            [f for f, _ in FUNCIONES],
        )
    }
    print()
    print("FUNCIONES")
    for func, origen in FUNCIONES:
        marca = "si" if func in funcs else "NO"
        print(f"  [{marca:>2}] {func:<22} ({origen})")

    cred = await conn.fetchval("SELECT current_setting('app.cred_key', true)")
    print()
    print(f"app.cred_key  : {'configurada' if cred else 'NO CONFIGURADA'}")
    print()

    falta_n8n = [t for t, o in TABLAS if o == "n8n" and t not in existentes]
    falta_portal = [t for t, o in TABLAS if o == "portal" and t not in existentes]

    if falta_n8n:
        print("AVISO: faltan tablas del lado de n8n: " + ", ".join(falta_n8n))
        print("       Esta NO parece ser la base que usa n8n. Aplicar el esquema")
        print("       del portal acá repetiría el problema original: el backend")
        print("       escribiría en una base que n8n no lee.")
    elif falta_portal:
        print("Base correcta (están las tablas de n8n), falta el esquema del portal:")
        print("       " + ", ".join(falta_portal))
        print("       Aplicar: python aplicar_sql.py sql/01_portal.sql sql/02_token_expires_tz.sql \\")
        print("                                      sql/03_users_username.sql sql/04_verificacion_email.sql \\")
        print("                                      sql/05_herramientas.sql sql/06_vendedores.sql \\")
        print("                                      sql/07_crm_campo.sql")
    else:
        print("Todo presente: esquema de n8n y del portal en la misma base.")


def resolver_ruta(nombre: str) -> Path:
    directa = Path(__file__).parent / nombre
    if directa.is_file():
        return directa
    return SQL_DIR / nombre


async def aplicar(conn: asyncpg.Connection, archivos: list[str]) -> None:
    for nombre in archivos:
        ruta = resolver_ruta(nombre)
        if not ruta.is_file():
            raise SystemExit(f"No existe el archivo: {ruta}")

        sql = ruta.read_text(encoding="utf-8")
        print(f"--> {nombre} ({len(sql)} bytes)")
        # En transacción: si algo falla, no queda a medio aplicar.
        async with conn.transaction():
            await conn.execute(sql)
        print(f"    ok")


async def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        raise SystemExit(1)

    conn = await asyncpg.connect(dsn=settings.DATABASE_URL)
    try:
        if args[0] in ("--revisar", "-r"):
            await revisar(conn)
        else:
            await aplicar(conn, args)
            print()
            print("Estado después de aplicar:")
            print()
            await revisar(conn)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
