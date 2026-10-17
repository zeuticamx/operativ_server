"""
Herramientas del agente por plan y por número (spec 005a, RF-6, RF-9, RF-17).

La herramienta es del negocio (`tenant_tools`, con sus credenciales). Cada
número de WhatsApp activa un subconjunto (`agente_canal_herramientas`), e
Instagram, Facebook y el widget otro (`social_herramientas`). Reglas:

  - Solo se activa una herramienta del negocio que esté habilitada y cuya
    FAMILIA incluya la lista del plan que rige al negocio (402
    `herramienta_no_incluida_en_plan`). Familias: calendario, sql_lectura, http,
    google_sheets, google_docs (P5).
  - Una herramienta nueva del negocio nace desactivada en todos los números
    (D27): aquí nada la activa por su cuenta.
  - Si el plan retira una familia, las herramientas quedan desactivadas, sin
    borrar la configuración, y no se reactivan solas si el plan la vuelve a
    incluir (`retirar_herramientas_por_plan`).
  - Quitar la herramienta del negocio la retira de todos: lo hace la cascada de
    la FK a `tenant_tools` (sql/56).

`familia_de` tiene la misma regla que la función SQL `familia_de_herramienta`
(sql/57); un test lo comprueba. Todas las funciones reciben la conexión de quien
las llama, para escribir en su transacción.
"""

from typing import Any, Iterable, Optional
from uuid import UUID

import asyncpg
from fastapi import HTTPException

from services.numeros_bitacora import Autor, SISTEMA, registrar
from services.numeros_reglas import FAMILIAS_HERRAMIENTAS, permite_activar

# Claves fijas del calendario (services/herramientas_calendario.py). Mismas que
# en `familia_de_herramienta` de sql/57.
CLAVES_CALENDARIO: frozenset[str] = frozenset(
    {
        "consultar_servicios",
        "consultar_proveedores",
        "consultar_disponibilidad",
        "crear_reserva",
        "cancelar_reserva",
        "consultar_cancelacion_reserva",
        "historial_citas_cliente",
        "consultar_citas_proximas",
        "reagendar_reserva",
    }
)

_FAMILIA_POR_TIPO: dict[str, str] = {
    "sql_lectura": "sql_lectura",
    "http_generico": "http",
    "http_post_json": "http",
    "google_sheets": "google_sheets",
    "google_docs": "google_docs",
}


def familia_de(tool_type: str, tool_key: str) -> Optional[str]:
    """
    Familia de una herramienta (P5): las claves fijas del calendario son
    'calendario'; el resto, por `tool_type`. Un tipo desconocido da None (no
    pertenece a ninguna familia: no se puede activar).
    """
    if tool_key in CLAVES_CALENDARIO:
        return "calendario"
    return _FAMILIA_POR_TIPO.get(tool_type)


async def familias_aplicables(conn: asyncpg.Connection, tenant_id: UUID) -> frozenset[str]:
    """Las familias que la lista del plan que rige hoy al negocio incluye (RF-9)."""
    fila = await conn.fetchval("SELECT familias FROM limites_efectivos($1)", tenant_id)
    return frozenset(fila if fila is not None else FAMILIAS_HERRAMIENTAS)


# ------------------------------------------------------------
# Lectura
# ------------------------------------------------------------
async def herramientas_de_numero(conn: asyncpg.Connection, tenant_id: UUID, agente_canal_id: UUID) -> list[str]:
    filas = await conn.fetch(
        "SELECT tool_key FROM agente_canal_herramientas WHERE tenant_id = $1 AND agente_canal_id = $2 ORDER BY tool_key",
        tenant_id,
        agente_canal_id,
    )
    return [f["tool_key"] for f in filas]


async def herramientas_sociales(conn: asyncpg.Connection, tenant_id: UUID) -> list[str]:
    filas = await conn.fetch(
        "SELECT tool_key FROM social_herramientas WHERE tenant_id = $1 ORDER BY tool_key", tenant_id
    )
    return [f["tool_key"] for f in filas]


async def herramienta_activa_para(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    agente_canal_id: Optional[UUID],
    tool_key: str,
) -> bool:
    """
    True si la herramienta existe, está habilitada en el negocio, su familia
    está en el plan (RF-9) y está activa para el número (o para social si
    `agente_canal_id` es None) (RF-6, RF-17).
    """
    fila = await conn.fetchrow(
        """
        SELECT tool_type FROM tenant_tools
        WHERE tenant_id = $1 AND tool_key = $2 AND is_enabled = true
        """,
        tenant_id,
        tool_key,
    )
    if fila is None:
        return False
    familia = familia_de(fila["tool_type"], tool_key)
    if familia is None:
        return False
    permitidas = await familias_aplicables(conn, tenant_id)
    if familia not in permitidas:
        return False
    if agente_canal_id is None:
        return (
            await conn.fetchval(
                "SELECT 1 FROM social_herramientas WHERE tenant_id = $1 AND tool_key = $2",
                tenant_id,
                tool_key,
            )
            is not None
        )
    return (
        await conn.fetchval(
            """
            SELECT 1 FROM agente_canal_herramientas
            WHERE tenant_id = $1 AND agente_canal_id = $2 AND tool_key = $3
            """,
            tenant_id,
            agente_canal_id,
            tool_key,
        )
        is not None
    )


async def catalogo(
    conn: asyncpg.Connection, tenant_id: UUID, agente_canal_id: Optional[UUID]
) -> list[dict[str, Any]]:
    """
    Las herramientas habilitadas del negocio con `activa` (en ese número, o en
    Instagram/Facebook/widget si `agente_canal_id` es None) y `permitida_por_plan`.
    Sin credenciales. Para la pantalla de herramientas por número (T5).
    """
    permitidas = await familias_aplicables(conn, tenant_id)
    activas = set(
        await (
            herramientas_sociales(conn, tenant_id)
            if agente_canal_id is None
            else herramientas_de_numero(conn, tenant_id, agente_canal_id)
        )
    )
    filas = await conn.fetch(
        """
        SELECT tool_key, tool_type, display_name, description
        FROM tenant_tools
        WHERE tenant_id = $1 AND is_enabled = true
        ORDER BY display_name, tool_key
        """,
        tenant_id,
    )
    salida: list[dict[str, Any]] = []
    for f in filas:
        familia = familia_de(f["tool_type"], f["tool_key"])
        salida.append(
            {
                "tool_key": f["tool_key"],
                "tool_type": f["tool_type"],
                "display_name": f["display_name"],
                "description": f["description"],
                "familia": familia,
                "activa": f["tool_key"] in activas,
                "permitida_por_plan": familia is not None and permite_activar(familia, permitidas),
            }
        )
    return salida


# ------------------------------------------------------------
# Escritura
# ------------------------------------------------------------
def _error(estado: int, codigo: str, mensaje: str) -> HTTPException:
    return HTTPException(status_code=estado, detail={"codigo": codigo, "mensaje": mensaje})


async def _validar_activables(
    conn: asyncpg.Connection, tenant_id: UUID, tool_keys: list[str]
) -> None:
    """
    Todas existen y están habilitadas en el negocio (404 `herramienta_no_encontrada`)
    y su familia está en el plan (402 `herramienta_no_incluida_en_plan`). Todo o nada.
    """
    if not tool_keys:
        return
    permitidas = await familias_aplicables(conn, tenant_id)
    filas = await conn.fetch(
        """
        SELECT tool_key, tool_type FROM tenant_tools
        WHERE tenant_id = $1 AND is_enabled = true AND tool_key = ANY($2::text[])
        """,
        tenant_id,
        tool_keys,
    )
    por_clave = {f["tool_key"]: f["tool_type"] for f in filas}
    for clave in tool_keys:
        if clave not in por_clave:
            raise _error(404, "herramienta_no_encontrada", "La herramienta no existe o no está habilitada en tu negocio.")
    for clave in tool_keys:
        familia = familia_de(por_clave[clave], clave)
        if familia is None or not permite_activar(familia, permitidas):
            raise _error(
                402,
                "herramienta_no_incluida_en_plan",
                f"La herramienta {clave} no está incluida en tu plan. Mejora tu plan para activarla.",
            )


async def _refrescar_si_principal(conn: asyncpg.Connection, agente_canal_id: Optional[UUID]) -> None:
    """La referencia del negocio copia las herramientas del principal (se reescribe al cambiar)."""
    if agente_canal_id is None:
        return
    from services import numeros_whatsapp

    await numeros_whatsapp.refrescar_referencia(conn, agente_canal_id)


async def _activar(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    agente_canal_id: Optional[UUID],
    tool_keys: Iterable[str],
    autor: Autor,
) -> list[str]:
    claves = list(dict.fromkeys(tool_keys))
    async with conn.transaction():
        await _validar_activables(conn, tenant_id, claves)
        nuevas: list[str] = []
        for clave in claves:
            if agente_canal_id is None:
                insertada = await conn.fetchval(
                    "INSERT INTO social_herramientas (tenant_id, tool_key) VALUES ($1, $2) "
                    "ON CONFLICT DO NOTHING RETURNING tool_key",
                    tenant_id,
                    clave,
                )
            else:
                insertada = await conn.fetchval(
                    "INSERT INTO agente_canal_herramientas (agente_canal_id, tenant_id, tool_key) "
                    "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING RETURNING tool_key",
                    agente_canal_id,
                    tenant_id,
                    clave,
                )
            if insertada is not None:
                nuevas.append(clave)
                await registrar(
                    conn, tenant_id, agente_canal_id, "herramienta_activada", autor, {"tool_key": clave}
                )
        if nuevas:
            await _refrescar_si_principal(conn, agente_canal_id)
    return nuevas


async def _desactivar(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    agente_canal_id: Optional[UUID],
    tool_keys: Iterable[str],
    autor: Autor,
) -> list[str]:
    claves = list(dict.fromkeys(tool_keys))
    async with conn.transaction():
        if agente_canal_id is None:
            filas = await conn.fetch(
                "DELETE FROM social_herramientas WHERE tenant_id = $1 AND tool_key = ANY($2::text[]) RETURNING tool_key",
                tenant_id,
                claves,
            )
        else:
            filas = await conn.fetch(
                "DELETE FROM agente_canal_herramientas "
                "WHERE tenant_id = $1 AND agente_canal_id = $2 AND tool_key = ANY($3::text[]) RETURNING tool_key",
                tenant_id,
                agente_canal_id,
                claves,
            )
        quitadas = sorted(f["tool_key"] for f in filas)
        for clave in quitadas:
            await registrar(conn, tenant_id, agente_canal_id, "herramienta_desactivada", autor, {"tool_key": clave})
        if quitadas:
            await _refrescar_si_principal(conn, agente_canal_id)
    return quitadas


async def activar_herramientas_de_numero(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    agente_canal_id: UUID,
    tool_keys: Iterable[str],
    autor: Autor,
) -> list[str]:
    """
    Activa herramientas en un número (activo o pausado). 404 `numero_no_encontrado`
    si el número no es del negocio; 404 `herramienta_no_encontrada` si alguna no
    está habilitada; 402 `herramienta_no_incluida_en_plan` si su familia no está
    en el plan. Todo o nada. Devuelve las que se activaron ahora (las que ya
    estaban activas no cuentan ni se anotan).
    """
    from services import numeros_whatsapp

    async with conn.transaction():
        await numeros_whatsapp.numero_del_negocio(conn, tenant_id, agente_canal_id)
        return await _activar(conn, tenant_id, agente_canal_id, tool_keys, autor)


async def desactivar_herramientas_de_numero(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    agente_canal_id: UUID,
    tool_keys: Iterable[str],
    autor: Autor,
) -> list[str]:
    """Desactiva herramientas de un número. 404 si no es del negocio. Devuelve las que quitó."""
    from services import numeros_whatsapp

    async with conn.transaction():
        await numeros_whatsapp.numero_del_negocio(conn, tenant_id, agente_canal_id)
        return await _desactivar(conn, tenant_id, agente_canal_id, tool_keys, autor)


async def activar_herramientas_social(
    conn: asyncpg.Connection, tenant_id: UUID, tool_keys: Iterable[str], autor: Autor
) -> list[str]:
    """Lo mismo para Instagram, Facebook y el widget (RF-17): mismas reglas (RF-6)."""
    return await _activar(conn, tenant_id, None, tool_keys, autor)


async def desactivar_herramientas_social(
    conn: asyncpg.Connection, tenant_id: UUID, tool_keys: Iterable[str], autor: Autor
) -> list[str]:
    return await _desactivar(conn, tenant_id, None, tool_keys, autor)


async def retirar_herramientas_por_plan(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    familias: Optional[Iterable[str]] = None,
    autor: Autor = SISTEMA,
) -> list[tuple[Optional[UUID], str]]:
    """
    RF-9, A25: desactiva, en cada número (pausados incluidos) y en Instagram y
    Facebook, las herramientas cuya familia ya no está en la lista del plan, sin
    borrar nada más, y lo anota en la bitácora (`herramienta_retirada_por_plan`).
    También las quita de la referencia del negocio. `familias` por defecto son
    las del plan vigente. No reactiva nada cuando el plan vuelve a incluirlas.
    Devuelve (agente_canal_id o None para social, tool_key). Idempotente.
    """
    permitidas = sorted(set(familias) if familias is not None else await familias_aplicables(conn, tenant_id))
    retiradas: list[tuple[Optional[UUID], str]] = []
    async with conn.transaction():
        filas = await conn.fetch(
            """
            DELETE FROM agente_canal_herramientas h
            USING tenant_tools t
            WHERE h.tenant_id = $1
              AND t.tenant_id = h.tenant_id AND t.tool_key = h.tool_key
              AND (familia_de_herramienta(t.tool_type, t.tool_key) IS NULL
                   OR NOT (familia_de_herramienta(t.tool_type, t.tool_key) = ANY($2::text[])))
            RETURNING h.agente_canal_id, h.tool_key
            """,
            tenant_id,
            permitidas,
        )
        retiradas.extend((f["agente_canal_id"], f["tool_key"]) for f in filas)
        filas = await conn.fetch(
            """
            DELETE FROM social_herramientas h
            USING tenant_tools t
            WHERE h.tenant_id = $1
              AND t.tenant_id = h.tenant_id AND t.tool_key = h.tool_key
              AND (familia_de_herramienta(t.tool_type, t.tool_key) IS NULL
                   OR NOT (familia_de_herramienta(t.tool_type, t.tool_key) = ANY($2::text[])))
            RETURNING h.tool_key
            """,
            tenant_id,
            permitidas,
        )
        retiradas.extend((None, f["tool_key"]) for f in filas)

        retiradas.sort(key=lambda r: (str(r[0] or ""), r[1]))
        for agente_canal_id, clave in retiradas:
            await registrar(
                conn, tenant_id, agente_canal_id, "herramienta_retirada_por_plan", autor, {"tool_key": clave}
            )

        # La referencia del negocio tampoco puede traerlas de vuelta a un número nuevo.
        await conn.execute(
            """
            UPDATE config_referencia_negocio r
            SET herramientas = ARRAY(
                    SELECT k FROM unnest(r.herramientas) AS k
                    WHERE NOT EXISTS (
                        SELECT 1 FROM tenant_tools t
                        WHERE t.tenant_id = r.tenant_id AND t.tool_key = k
                          AND (familia_de_herramienta(t.tool_type, t.tool_key) IS NULL
                               OR NOT (familia_de_herramienta(t.tool_type, t.tool_key) = ANY($2::text[])))
                    )),
                updated_at = NOW()
            WHERE r.tenant_id = $1
            """,
            tenant_id,
            permitidas,
        )
    return retiradas
