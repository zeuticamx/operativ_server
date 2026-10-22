"""
Herramienta del agente para generar cobros de Stripe Connect, creada sola.

Mismo mecanismo que services/herramientas_calendario.py: el agente (en
código o todavía en n8n vía `ejecutar-herramienta-tenant`) no conoce Stripe
por sí mismo, lo usa a través de una fila de `tenant_tools` que apunta a
`POST /api/eventos/pagos/connect/crear-cobro` (routers/eventos.py) con
X-Internal-Token.

A diferencia del calendario, acá no hay compuerta por conversation_id (esta
tool no necesita deducir quién es el cliente, solo de qué tenant es la
conversación) — la única condición para que exista es que el tenant haya
terminado el onboarding de Connect (`tenant_stripe_connect.charges_habilitado`).

Respeta D27 (services/herramientas_plan.py): dar de alta la fila en
`tenant_tools` no la activa en ningún número ni en Instagram/Facebook/widget
— el dueño la prende desde /herramientas como cualquier otra tool, incluidas
las de calendario. `sincronizar` solo crea/pausa la fila del catálogo.
"""

import json
import logging
from typing import Any
from uuid import UUID

import asyncpg

from config import settings
from session import transaccion

log = logging.getLogger("operativai.herramientas_pagos_connect")

AUTH_HEADER = "X-Internal-Token"
HERRAMIENTA_COBRO = "generar_cobro_connect"


def definicion() -> dict[str, Any]:
    """Función y no constante: la URL sale de BASE_URL_BACKEND (los tests la cambian)."""
    return {
        "tool_key": HERRAMIENTA_COBRO,
        "tool_type": "http_post_json",
        "display_name": "Generar link de cobro",
        "description": (
            "Usa esta herramienta cuando el cliente acepte pagar algo específico (un "
            "producto, un servicio, una reparación) y ya sepas el monto exacto a "
            "cobrarle. Genera un link de pago de Stripe y te lo devuelve en "
            "`url_cobro` — mándaselo al cliente en tu respuesta. No le pidas datos de "
            "tarjeta ni de pago: el link ya lleva todo lo necesario. Si la respuesta "
            "trae `ok: false`, el negocio todavía no tiene cobros propios activados; "
            "dile al cliente que no puedes generar el link ahora mismo."
        ),
        "parametros_schema": {
            "descripcion": {
                "type": "string",
                "required": True,
                "description": "Concepto del cobro, claro para el cliente (ej. 'Reparación de bomba de agua').",
            },
            "monto": {
                "type": "string",
                "required": True,
                "description": "Monto a cobrar, en la moneda del negocio. Nunca lo inventes: confírmalo con el cliente antes.",
            },
        },
        "config": {
            "method": "POST",
            "idempotent": True,
            "incluir_contacto": False,
            "auth_header": AUTH_HEADER,
            "url_template": f"{settings.BASE_URL_BACKEND}/api/eventos/pagos/connect/crear-cobro",
        },
    }


def clave() -> str:
    return HERRAMIENTA_COBRO


async def _encender(conn: asyncpg.Connection, tenant_id: UUID) -> None:
    d = definicion()
    # Mismas reglas que herramientas_calendario._encender: una pausa del
    # dueño (is_enabled=false sin marca del sistema) sobrevive; solo se
    # reactiva lo que había pausado este módulo.
    await conn.execute(
        """
        INSERT INTO tenant_tools
            (tenant_id, tool_key, tool_type, display_name, description,
             parametros_schema, config, is_enabled)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, true)
        ON CONFLICT (tenant_id, tool_key) DO UPDATE SET
            tool_type         = EXCLUDED.tool_type,
            parametros_schema = EXCLUDED.parametros_schema,
            config            = $7::jsonb,
            is_enabled        = tenant_tools.is_enabled
                                 OR COALESCE((tenant_tools.config->>'pausada_por_sistema')::boolean, false),
            updated_at        = NOW()
        """,
        tenant_id,
        d["tool_key"],
        d["tool_type"],
        d["display_name"],
        d["description"],
        d["parametros_schema"],
        d["config"],
    )

    if settings.N8N_INTERNAL_TOKEN:
        await conn.execute(
            "SELECT set_tool_credentials($1, $2, $3)",
            tenant_id,
            d["tool_key"],
            json.dumps({"token": settings.N8N_INTERNAL_TOKEN}),
        )
    else:
        log.warning(
            "N8N_INTERNAL_TOKEN vacío: la herramienta de cobros del tenant %s queda sin credencial",
            tenant_id,
        )


async def _apagar(conn: asyncpg.Connection, tenant_id: UUID) -> None:
    await conn.execute(
        """
        UPDATE tenant_tools
           SET is_enabled = false,
               config     = config || '{"pausada_por_sistema": true}'::jsonb,
               updated_at = NOW()
         WHERE tenant_id = $1 AND tool_key = $2 AND is_enabled
        """,
        tenant_id,
        HERRAMIENTA_COBRO,
    )


async def sincronizar(tenant_id: UUID, charges_habilitado: bool) -> None:
    """
    Da de alta o pausa la fila del catálogo según el estado de la cuenta
    Connect del tenant. Idempotente; nunca propaga un error (se llama desde
    el procesador del webhook de Stripe, que no puede devolver 500 a Stripe
    por esto — ver services/herramientas_calendario.sincronizar).
    """
    try:
        async with transaccion() as conn:
            if charges_habilitado:
                await _encender(conn, tenant_id)
            else:
                await _apagar(conn, tenant_id)
    except Exception:  # noqa: BLE001 - ver docstring
        log.exception("No se pudo sincronizar la herramienta de cobros de %s", tenant_id)
