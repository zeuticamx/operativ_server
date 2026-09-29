"""
Herramientas del agente para el calendario, creadas solas.

El agente de n8n no conoce el calendario por sí mismo: lo usa a través de
filas de `tenant_tools`. `entrada-canal-universal` las lista (nodo "Carga
catalogo herramientas": tool_key, description, parametros_schema de las
activas) y se las describe al modelo; cuando el modelo elige una,
`ejecutar-herramienta-tenant` lee su `tool_type` + `config`, pide el token
con `get_tool_credentials(tenant, tool_key)` y llama al backend:

    http_generico   GET  {url_template} con la cabecera `auth_header`
    http_post_json  POST {url_template} con `tenant_id` + parámetros en el
                    body; `idempotent` arma la idempotency_key a partir del
                    mensaje entrante, `incluir_contacto` agrega
                    cliente_nombre / cliente_telefono.

Las cinco de acá apuntan a /api/eventos/calendario/* (routers/eventos.py)
con X-Internal-Token. Antes se daban de alta a mano por SQL en producción;
ahora las crea este módulo cuando un negocio enciende el calendario (el
dueño, gerencia o una prueba) y las pausa cuando lo apaga.

Las definiciones son las que ya corrían en producción (copiadas de las
ejecuciones de n8n), salvo `cancelar_reserva`, que nunca llegó a
ejecutarse: su config sale de la ruta y de CancelarReservaEventoIn.

Qué se respeta del dueño:
  - nombre y descripción editados desde /herramientas no se pisan;
  - una herramienta que el dueño pausó sigue pausada. Solo se reactivan
    las que pausó este módulo al apagar el calendario
    (`config.pausada_por_sistema`).
El contrato con n8n (tipo, parámetros, URL, token) sí se reescribe siempre:
es lo que tiene que coincidir con el backend.
"""

import json
import logging
from typing import Any
from uuid import UUID

import asyncpg

from config import settings
from session import fetch_all, transaccion

log = logging.getLogger("operativai.herramientas_calendario")

AUTH_HEADER = "X-Internal-Token"


def _get(ruta: str) -> dict[str, Any]:
    return {
        "method": "GET",
        "auth_header": AUTH_HEADER,
        "url_template": f"{settings.BASE_URL_BACKEND}/api/eventos/calendario/{ruta}"
        "?tenant_id={tenant_id}",
    }


def _post(ruta: str, *, idempotent: bool, incluir_contacto: bool) -> dict[str, Any]:
    return {
        "method": "POST",
        "idempotent": idempotent,
        "auth_header": AUTH_HEADER,
        "url_template": f"{settings.BASE_URL_BACKEND}/api/eventos/calendario/{ruta}",
        "incluir_contacto": incluir_contacto,
    }


def definiciones() -> list[dict[str, Any]]:
    """
    Una función y no una constante: la URL sale de BASE_URL_BACKEND, que
    los tests cambian con monkeypatch.
    """
    return [
        {
            "tool_key": "consultar_servicios",
            "tool_type": "http_generico",
            "display_name": "Consultar servicios",
            "description": (
                "Usa esta herramienta cuando el cliente pregunte qué servicios ofrece el "
                "negocio, precios o duración. No requiere parámetros — siempre devuelve el "
                "catálogo completo."
            ),
            "parametros_schema": {},
            "config": _get("servicios"),
        },
        {
            "tool_key": "consultar_proveedores",
            "tool_type": "http_generico",
            "display_name": "Consultar proveedores",
            "description": (
                "Usa esta herramienta cuando el cliente pregunte quién puede atenderlo o "
                "quiera elegir con quién agendar. Por ahora siempre devuelve todos los "
                "proveedores — filtra tú la respuesta si el cliente ya mencionó un servicio "
                "específico."
            ),
            "parametros_schema": {
                "servicio_id": {
                    "type": "string",
                    "required": False,
                    "description": (
                        "No filtra la consulta todavía (limitación actual). Si el cliente "
                        "mencionó un servicio, igual pásalo para dar contexto, pero espera "
                        "el catálogo completo."
                    ),
                },
            },
            "config": _get("proveedores"),
        },
        {
            "tool_key": "consultar_disponibilidad",
            "tool_type": "http_post_json",
            "display_name": "Consultar horarios disponibles",
            "description": (
                "Usa esta herramienta cuando el cliente quiera saber qué horarios hay libres "
                "para agendar. Necesitas el servicio_id antes de llamarla — si no lo tienes, "
                "pregunta primero o llama a consultar_servicios."
            ),
            "parametros_schema": {
                "fecha_desde": {"type": "string", "format": "YYYY-MM-DD", "required": True},
                "fecha_hasta": {
                    "type": "string",
                    "format": "YYYY-MM-DD",
                    "required": True,
                    "description": (
                        "Si el cliente solo preguntó por un día, usa la misma fecha en "
                        "fecha_desde y fecha_hasta"
                    ),
                },
                "servicio_id": {"type": "string", "required": True},
                "proveedor_id": {
                    "type": "string",
                    "required": False,
                    "description": (
                        "Si el cliente no pidió un proveedor específico, omite este campo — "
                        "el sistema mostrará disponibilidad de todos"
                    ),
                },
            },
            "config": _post("disponibilidad", idempotent=False, incluir_contacto=False),
        },
        {
            "tool_key": "crear_reserva",
            "tool_type": "http_post_json",
            "display_name": "Agendar una cita",
            "description": (
                "Usa esta herramienta SOLO cuando el cliente confirmó explícitamente un "
                "horario específico entre las opciones que le mostraste con "
                "consultar_disponibilidad. NUNCA inventes ni ofrezcas un horario que no "
                "hayas verificado primero con esa herramienta. No necesitas pedir el nombre "
                "o teléfono del cliente — el sistema ya los tiene."
            ),
            "parametros_schema": {
                "hora": {"type": "string", "format": "HH:MM", "required": True},
                "fecha": {"type": "string", "format": "YYYY-MM-DD", "required": True},
                "notas": {
                    "type": "string",
                    "required": False,
                    "description": (
                        "Cualquier detalle adicional que el cliente haya mencionado "
                        '(ej. "corte con máquina 2", "primera vez")'
                    ),
                },
                "servicio_id": {"type": "string", "required": True},
                "proveedor_id": {"type": "string", "required": True},
            },
            "config": _post("reservas", idempotent=True, incluir_contacto=True),
        },
        {
            "tool_key": "cancelar_reserva",
            "tool_type": "http_post_json",
            "display_name": "Cancelar una cita",
            "description": (
                "Usa esta herramienta cuando el cliente pida cancelar una cita existente. "
                "Necesitas su reserva_id: pídeselo si tiene un código de confirmación, o "
                "revisa si tú mismo creaste esa reserva antes en esta misma conversación "
                "(el resultado de crear_reserva incluye el id). Si no cuentas con el "
                "reserva_id y el cliente tampoco lo tiene, dile que no puedes cancelarla sin "
                "ese dato y ofrece escalar con el negocio."
            ),
            "parametros_schema": {
                "reserva_id": {
                    "type": "string",
                    "required": True,
                    "description": (
                        "UUID de la reserva a cancelar. Debe venir del código de "
                        "confirmación que dio el cliente, o del resultado de una llamada "
                        "anterior a crear_reserva en esta conversación."
                    ),
                },
            },
            "config": _post("reservas/cancelar", idempotent=False, incluir_contacto=False),
        },
    ]


def claves() -> frozenset[str]:
    return frozenset(d["tool_key"] for d in definiciones())


def es_gestionada(tool_key: str) -> bool:
    """La crea y la mantiene el sistema: el dueño puede pausarla, no borrarla."""
    return tool_key in claves()


async def _encender(conn: asyncpg.Connection, tenant_id: UUID) -> None:
    for d in definiciones():
        await conn.execute(
            """
            INSERT INTO tenant_tools
                (tenant_id, tool_key, tool_type, display_name, description,
                 parametros_schema, config, is_enabled)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, true)
            ON CONFLICT (tenant_id, tool_key) DO UPDATE SET
                tool_type         = EXCLUDED.tool_type,
                parametros_schema = EXCLUDED.parametros_schema,
                config            = EXCLUDED.config,
                is_enabled        = tenant_tools.is_enabled
                                    OR COALESCE((tenant_tools.config->>'pausada_por_sistema')::boolean,
                                                false),
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

        # Sin token n8n recibiría 503 del backend en cada llamada: mejor que
        # quede en el log que guardar un token vacío que parezca válido.
        if settings.N8N_INTERNAL_TOKEN:
            await conn.execute(
                "SELECT set_tool_credentials($1, $2, $3)",
                tenant_id,
                d["tool_key"],
                json.dumps({"token": settings.N8N_INTERNAL_TOKEN}),
            )

    if not settings.N8N_INTERNAL_TOKEN:
        log.warning(
            "N8N_INTERNAL_TOKEN vacío: herramientas de calendario del tenant %s sin credencial",
            tenant_id,
        )


async def _apagar(conn: asyncpg.Connection, tenant_id: UUID) -> None:
    await conn.execute(
        """
        UPDATE tenant_tools
           SET is_enabled = false,
               config     = config || '{"pausada_por_sistema": true}'::jsonb,
               updated_at = NOW()
         WHERE tenant_id = $1
           AND tool_key = ANY($2::text[])
           AND is_enabled
        """,
        tenant_id,
        sorted(claves()),
    )


async def sincronizar(tenant_id: UUID, calendario_activo: bool) -> None:
    """
    Deja las herramientas del calendario acordes al interruptor del módulo.
    Idempotente.

    Nunca propaga un error: se llama después de encender o apagar el
    módulo, y que falle esto (p. ej. falta `app.cred_key` en la base) no
    puede deshacer ese cambio ni devolverle un 500 al dueño. Queda en el
    log y se repara solo en el siguiente arranque (sincronizar_todos).
    """
    try:
        async with transaccion() as conn:
            if calendario_activo:
                await _encender(conn, tenant_id)
            else:
                await _apagar(conn, tenant_id)
    except Exception:  # noqa: BLE001 - ver docstring
        log.exception("No se pudieron sincronizar las herramientas de calendario de %s", tenant_id)


async def sincronizar_todos() -> None:
    """
    Al arrancar: crea las que falten a los negocios que ya tienen el
    calendario encendido, y refresca URL y token (si cambiaron
    BASE_URL_BACKEND o N8N_INTERNAL_TOKEN). Así un negocio que encendió el
    calendario antes de este módulo también las tiene.
    """
    try:
        filas = await fetch_all("SELECT tenant_id FROM tenant_servicios WHERE calendario_activo")
    except Exception:  # noqa: BLE001 - el arranque no puede caerse por esto
        log.exception("No se pudo leer qué negocios tienen el calendario encendido")
        return
    for f in filas:
        await sincronizar(f["tenant_id"], True)
    if filas:
        log.info("Herramientas de calendario sincronizadas para %s negocios", len(filas))
