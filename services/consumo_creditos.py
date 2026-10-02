"""
Auditoría de consumo de créditos por herramienta (solo lectura).

Lee el libro `credit_transactions` (tipo 'gasto'); no toca el cobro, que
vive en services/creditos.py. Todo se filtra por el tenant de la sesión:
este módulo nunca recibe un tenant_id que venga de la petición.

El número de WhatsApp no se guarda en el cobro: se deriva por
conversation_id -> conversations -> users. Los dos JOIN exigen el mismo
tenant_id que el cobro, así que un conversation_id de otro negocio (sea
por error de n8n o manipulado) nunca filtra el número de un tercero.
"""

import re
from datetime import date, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from session import fetch_all, fetch_one, fetch_value

ZONA_POR_DEFECTO = "America/Mexico_City"

# Igual que services/conversaciones.py: n8n a veces guarda el texto "null".
_WHATSAPP = "NULLIF(NULLIF(TRIM(u.whatsapp_id), ''), 'null')"

# Cobro -> conversación -> cliente final, siempre dentro del mismo tenant.
_FROM = """
    FROM credit_transactions ct
    LEFT JOIN conversations c
           ON c.id = ct.conversation_id AND c.tenant_id = ct.tenant_id
    LEFT JOIN users u
           ON u.id = c.user_id AND u.tenant_id = ct.tenant_id
"""


def enmascarar_whatsapp(numero: str | None) -> str | None:
    """
    '5215512344567' -> '+521 •••• 4567'. Deja el prefijo internacional (lo
    que sobra sobre 10 dígitos locales) y los últimos 4. Menos de 8 dígitos
    no es un teléfono: se descarta.
    """
    digitos = re.sub(r"\D", "", numero or "")
    if len(digitos) < 8:
        return None
    prefijo = digitos[:-10] if len(digitos) > 10 else ""
    return f"{'+' + prefijo + ' ' if prefijo else ''}•••• {digitos[-4:]}"


async def zona_horaria_tenant(tenant_id: UUID) -> str:
    zona = await fetch_value(
        "SELECT zona_horaria FROM tenant_servicios WHERE tenant_id = $1", tenant_id
    )
    try:
        ZoneInfo(zona or ZONA_POR_DEFECTO)
    except (ZoneInfoNotFoundError, ValueError):
        return ZONA_POR_DEFECTO
    return zona or ZONA_POR_DEFECTO


def _filtros(
    tenant_id: UUID,
    zona: str,
    desde: date | None,
    hasta: date | None,
    herramienta: str | None,
) -> tuple[str, list]:
    """WHERE y parámetros. El tenant es siempre $1 y la zona $2."""
    # `$2::text IS NOT NULL` referencia la zona aunque no haya filtro de
    # fechas: asyncpg falla si un parámetro enviado no aparece en la query.
    where = ["ct.tenant_id = $1", "ct.tipo = 'gasto'", "$2::text IS NOT NULL"]
    params: list = [tenant_id, zona]
    if desde is not None:
        params.append(desde)
        where.append(f"ct.created_at >= (${len(params)}::date)::timestamp AT TIME ZONE $2")
    if hasta is not None:
        # `hasta` es inclusivo: el tope es el inicio del día siguiente.
        params.append(hasta + timedelta(days=1))
        where.append(f"ct.created_at < (${len(params)}::date)::timestamp AT TIME ZONE $2")
    if herramienta:
        params.append(herramienta)
        where.append(f"ct.herramienta = ${len(params)}")
    return " AND ".join(where), params


async def listar_consumo(
    tenant_id: UUID,
    desde: date | None,
    hasta: date | None,
    herramienta: str | None,
    limite: int,
    offset: int,
) -> dict:
    zona = await zona_horaria_tenant(tenant_id)
    where, params = _filtros(tenant_id, zona, desde, hasta, herramienta)

    total = await fetch_value(f"SELECT COUNT(*) {_FROM} WHERE {where}", *params)

    params_pagina = [*params, limite, offset]
    filas = await fetch_all(
        f"""
        SELECT ct.id, ct.herramienta, -ct.cantidad AS creditos, ct.bolsa,
               ct.created_at,
               c.channel_type AS canal,
               CASE WHEN c.channel_type = 'whatsapp' THEN {_WHATSAPP} END AS whatsapp
        {_FROM}
        WHERE {where}
        ORDER BY ct.created_at DESC, ct.id DESC
        LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """,
        *params_pagina,
    )

    tz = ZoneInfo(zona)
    return {
        "items": [
            {
                "id": f["id"],
                "herramienta": f["herramienta"],
                "creditos": f["creditos"],
                "bolsa": f["bolsa"],
                "creado_en": f["created_at"].astimezone(tz),
                "whatsapp": enmascarar_whatsapp(f["whatsapp"]),
                "canal": f["canal"],
            }
            for f in filas
        ],
        "total": total or 0,
        "limite": limite,
        "offset": offset,
        "zona_horaria": zona,
    }


async def resumen_consumo(
    tenant_id: UUID,
    desde: date | None,
    hasta: date | None,
    herramienta: str | None,
) -> dict:
    zona = await zona_horaria_tenant(tenant_id)
    where, params = _filtros(tenant_id, zona, desde, hasta, herramienta)

    total = await fetch_one(
        f"SELECT COALESCE(SUM(-ct.cantidad), 0) AS total {_FROM} WHERE {where}", *params
    )
    por_herramienta = await fetch_all(
        f"""
        SELECT ct.herramienta, SUM(-ct.cantidad) AS creditos
        {_FROM} WHERE {where}
        GROUP BY ct.herramienta
        ORDER BY creditos DESC, ct.herramienta
        """,
        *params,
    )
    por_dia = await fetch_all(
        f"""
        SELECT (ct.created_at AT TIME ZONE $2)::date AS dia, SUM(-ct.cantidad) AS creditos
        {_FROM} WHERE {where}
        GROUP BY dia
        ORDER BY dia
        """,
        *params,
    )
    return {
        "total_creditos": total["total"],
        "por_herramienta": [dict(f) for f in por_herramienta],
        "por_dia": [dict(f) for f in por_dia],
        "zona_horaria": zona,
    }
