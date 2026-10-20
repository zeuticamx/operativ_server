"""
Herramienta `ejecutar_herramienta`: las herramientas dinámicas del tenant.

Port de `ejecutar-herramienta-tenant`. El dueño del negocio da de alta sus
herramientas desde el portal (`tenant_tools`) y el agente las invoca por
`tool_key`; acá se resuelve qué significa cada tipo.

Orden, igual que en el workflow: validar que existe y está activa → cobrar un
crédito → ejecutar. Una herramienta inexistente no cobra, y sin crédito no se
ejecuta. El cobro falla cerrado: si no se pudo confirmar el descuento, no se
ejecuta nada.

Dos arreglos de la auditoría del 15/10/2026 van incorporados aquí:

  - Hallazgo 4: la consulta de una herramienta SQL tenía que filtrar por
    tenant_id por convención, y nada lo comprobaba — una herramienta mal
    escrita leía datos de otros negocios. Ahora el filtro es obligatorio y el
    tenant lo inyecta el servidor, no la configuración.
  - Hallazgo 5: `url_template` es configurable por tenant y se llamaba desde
    la red del servidor, así que servía para alcanzar servicios internos
    (SSRF). Ahora hay lista de dominios permitidos y se rechazan las IPs
    privadas tras resolver el nombre.

Lo que el modelo controla es el `tool_key` y los parámetros declarados. El
tenant, la conversación y el mensaje los pone el servidor: si el modelo
pudiera elegirlos, podría pedir datos de otra conversación.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import socket
from typing import Any, Optional
from urllib.parse import quote, urlparse
from uuid import UUID

import httpx

from config import settings
from services import creditos, herramientas_plan
from session import conexion, fetch_one, fetch_value

log = logging.getLogger("operativai.agente.herramientas")

TIMEOUT = httpx.Timeout(20.0)

# Tope del texto que vuelve al modelo. Un resultado enorme no mejora la
# respuesta y se paga en cada turno posterior de la conversación.
MAX_RESULTADO = 3000
MAX_DOCUMENTO = 8000

SIN_CREDITOS = (
    "SIN_CREDITOS: El negocio no tiene créditos disponibles para usar herramientas en "
    "este momento. No vuelvas a intentar esta ni otra herramienta; informa al cliente "
    "con amabilidad que por ahora no puedes realizar esa gestión y ofrécele hablar con "
    "una persona."
)

ERROR_TECNICO = (
    "Hubo un problema técnico al usar esta herramienta. Discúlpate con el cliente y "
    "ofrece intentarlo de nuevo en un momento, o hablar con una persona si insiste."
)


def definicion(catalogo: str) -> dict[str, Any]:
    """
    El esquema que ve el modelo.

    La descripción lleva el catálogo del tenant incrustado: el esquema es fijo
    (siempre tool_key + parámetros), lo que cambia por negocio es qué claves
    son válidas. Por eso la descripción se arma por petición.
    """
    return {
        "name": "ejecutar_herramienta",
        "description": (
            "Consulta o gestiona información de este negocio. Indica el tool_key "
            "exacto de una de las herramientas configuradas y sus parámetros.\n\n"
            f"{catalogo}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tool_key": {
                    "type": "string",
                    "description": "Clave exacta de la herramienta a usar.",
                },
                "parametros": {
                    "type": "object",
                    "description": (
                        "Parámetros que pide esa herramienta. Objeto vacío si no "
                        "necesita ninguno."
                    ),
                },
            },
            "required": ["tool_key"],
        },
    }


async def ejecutar(
    *,
    tenant_id: UUID,
    tool_key: str,
    parametros: dict[str, Any],
    conversation_id: UUID,
    mensaje_id: str | None,
    contacto: dict[str, Any],
    referencia_cobro: str,
    agente_canal_id: Optional[UUID] = None,
) -> str:
    """
    Resuelve una llamada del agente y devuelve el texto que lee el modelo.

    Nunca levanta: un fallo se traduce a un texto que el agente pueda
    explicarle al cliente. Una excepción aquí abortaría la respuesta entera.

    `agente_canal_id` es None para Instagram, Facebook y widget (configuración
    social); para WhatsApp es el número por el que entró. Si la herramienta no
    está activa para ese número/social, o su familia ya no está en el plan, se
    rechaza sin cobrar (spec 005a, RF-6, RF-9).
    """
    try:
        return await _ejecutar(
            tenant_id=tenant_id,
            tool_key=tool_key,
            parametros=parametros or {},
            conversation_id=conversation_id,
            mensaje_id=mensaje_id,
            contacto=contacto or {},
            referencia_cobro=referencia_cobro,
            agente_canal_id=agente_canal_id,
        )
    except Exception:
        log.exception(
            "Falló la herramienta %s del tenant %s", tool_key, tenant_id
        )
        return ERROR_TECNICO


async def _ejecutar(
    *,
    tenant_id: UUID,
    tool_key: str,
    parametros: dict[str, Any],
    conversation_id: UUID,
    mensaje_id: str | None,
    contacto: dict[str, Any],
    referencia_cobro: str,
    agente_canal_id: Optional[UUID] = None,
) -> str:
    # RF-6 / RF-9: la herramienta debe estar activa para el número (o social)
    # y su familia debe estar en el plan. Sin esto no se cobra ni se ejecuta.
    async with conexion() as conn:
        if not await herramientas_plan.herramienta_activa_para(
            conn, tenant_id, agente_canal_id, tool_key
        ):
            return f'La herramienta "{tool_key}" no está disponible.'

    definicion_tool = await fetch_one(
        """
        SELECT t.tool_type, t.config, t.is_enabled, ts.zona_horaria
          FROM tenant_tools t
          LEFT JOIN tenant_servicios ts ON ts.tenant_id = t.tenant_id
         WHERE t.tenant_id = $1 AND t.tool_key = $2
        """,
        tenant_id,
        tool_key,
    )
    if definicion_tool is None or not definicion_tool["is_enabled"]:
        # No cobra: el agente se inventó una clave o la herramienta está en pausa.
        return f'La herramienta "{tool_key}" no está disponible.'

    resultado_cobro = await creditos.consumir_credito(
        tenant_id, tool_key, referencia_cobro, conversation_id
    )
    if not resultado_cobro.permitido:
        log.warning(
            "Herramienta %s sin saldo (tenant %s)", tool_key, tenant_id
        )
        return SIN_CREDITOS

    config = definicion_tool["config"] or {}
    tipo = definicion_tool["tool_type"]

    if tipo == "sql_lectura":
        return await _sql_lectura(tenant_id, config, parametros)
    if tipo == "http_generico":
        return await _http_generico(tenant_id, tool_key, config, parametros)
    if tipo == "http_post_json":
        return await _http_post_json(
            tenant_id=tenant_id,
            tool_key=tool_key,
            config=config,
            parametros=parametros,
            conversation_id=conversation_id,
            mensaje_id=mensaje_id,
            contacto=contacto,
            zona_horaria=definicion_tool["zona_horaria"],
        )
    if tipo == "google_sheets":
        return await _google_sheets(config)
    if tipo == "google_docs":
        return await _google_docs(config)

    log.error("Herramienta %s con tipo no soportado: %s", tool_key, tipo)
    return "Esta herramienta tiene un tipo no reconocido por el sistema."


# ============================================================
# SQL de solo lectura
# ============================================================
async def _sql_lectura(
    tenant_id: UUID, config: dict[str, Any], parametros: dict[str, Any]
) -> str:
    """
    Ejecuta la consulta guardada en la herramienta.

    `ejecutar_query_dinamica` ya exige que empiece por SELECT y liga los
    valores como parámetros ($1, $2…), así que los datos del modelo nunca se
    concatenan al SQL. Lo que faltaba era el aislamiento entre negocios: la
    consulta DEBE filtrar por tenant_id, y ese valor lo pone el servidor.

    Contrato: la consulta recibe `$1` = tenant_id, y a partir de `$2` los
    parámetros en el orden de `config.orden_parametros`.
    """
    query = (config.get("query") or "").strip()
    if not query:
        return "Esta herramienta no tiene una consulta configurada."

    if "tenant_id" not in query.lower():
        # Falla cerrado: sin el filtro, la consulta leería todos los negocios.
        log.error(
            "Herramienta SQL del tenant %s sin filtro por tenant_id; se rechaza",
            tenant_id,
        )
        return (
            "Esta herramienta está mal configurada y no se puede usar. "
            "Dile al cliente que no puedes hacer esa consulta y ofrece hablar con "
            "una persona."
        )

    orden = config.get("orden_parametros") or list(parametros.keys())
    valores = [
        None if parametros.get(k) is None else str(parametros.get(k)) for k in orden
    ]

    # $1 es siempre el tenant; los del modelo empiezan en $2.
    filas = await fetch_value(
        "SELECT ejecutar_query_dinamica($1, $2)",
        query,
        [str(tenant_id), *valores],
    )

    if not filas:
        return "No se encontraron resultados."
    return json.dumps(filas, ensure_ascii=False, default=str)[:MAX_RESULTADO]


# ============================================================
# HTTP hacia servicios del tenant
# ============================================================
async def _destino_permitido(url: str) -> str | None:
    """
    Comprueba que la URL se puede llamar. Devuelve el motivo del rechazo, o
    None si pasa.

    Dos capas: el dominio tiene que estar en la lista del despliegue, y el
    nombre no puede resolver a una IP privada. La segunda hace falta porque
    un dominio permitido puede apuntar a 127.0.0.1 o a la red interna.
    """
    try:
        partes = urlparse(url)
    except ValueError:
        return "URL inválida"

    if partes.scheme != "https":
        return "Solo se permite https"
    if not partes.hostname:
        return "URL sin host"

    permitidos = settings.AGENTE_DOMINIOS_PERMITIDOS
    if not permitidos:
        return "No hay dominios permitidos configurados (AGENTE_DOMINIOS_PERMITIDOS)"

    host = partes.hostname.lower()
    if not any(host == d or host.endswith(f".{d}") for d in permitidos):
        return f"Dominio no permitido: {host}"

    # getaddrinfo bloquea: fuera del event loop.
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, partes.port or 443, type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        return f"No se pudo resolver {host}"

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return f"{host} resuelve a una dirección interna"

    return None


def _url_con_parametros(
    plantilla: str, tenant_id: UUID, parametros: dict[str, Any]
) -> str:
    url = plantilla.replace("{tenant_id}", quote(str(tenant_id), safe=""))
    for clave, valor in parametros.items():
        url = url.replace("{" + clave + "}", quote(str(valor), safe=""))
    return url


async def _credencial(tenant_id: UUID, tool_key: str) -> str | None:
    crudo = await fetch_value(
        "SELECT get_tool_credentials($1, $2)", tenant_id, tool_key
    )
    if not crudo:
        return None
    try:
        return (json.loads(crudo) or {}).get("token")
    except (json.JSONDecodeError, TypeError):
        return crudo


async def _http_generico(
    tenant_id: UUID, tool_key: str, config: dict[str, Any], parametros: dict[str, Any]
) -> str:
    url = _url_con_parametros(config.get("url_template") or "", tenant_id, parametros)

    motivo = await _destino_permitido(url)
    if motivo:
        log.error("Herramienta %s (tenant %s) rechazada: %s", tool_key, tenant_id, motivo)
        return ERROR_TECNICO

    cabeceras: dict[str, str] = {}
    token = await _credencial(tenant_id, tool_key)
    if token and config.get("auth_header"):
        # El token solo existe acá, al armar la cabecera: nunca entra en el
        # texto que vuelve al modelo ni en el log (hallazgo 2).
        cabeceras[config["auth_header"]] = token

    # Sin seguir redirects: un 302 desde un dominio permitido saltaría la lista.
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as cliente:
        respuesta = await cliente.request(
            config.get("method") or "GET", url, headers=cabeceras
        )

    if respuesta.status_code >= 400:
        log.warning(
            "Herramienta %s (tenant %s) respondió %s",
            tool_key, tenant_id, respuesta.status_code,
        )
        return ERROR_TECNICO

    return (respuesta.text or "Sin contenido.")[:MAX_RESULTADO]


# Traducción de los errores del backend de citas a algo que el agente pueda
# decirle al cliente. Viene del nodo "Formatea resultado POST".
_MOTIVOS_POR_CODIGO = {
    "reserva_no_confirmada": (
        "Esa cita ya no está confirmada; puede que ya estuviera cancelada. No la "
        "canceles de nuevo: confírmale al cliente cómo quedó."
    ),
    "cita_ya_iniciada": (
        "La hora de esa cita ya comenzó, así que no se puede cancelar por este medio. "
        "Explícaselo con amabilidad y ofrécele hablar con una persona."
    ),
    "calendario_inactivo": (
        "El sistema de citas no está disponible en este momento. Discúlpate y ofrécele "
        "hablar con una persona."
    ),
}

_MOTIVOS_POR_STATUS = {
    409: (
        "Ese horario ya no está disponible, probablemente alguien más lo tomó. Ofrece "
        "consultar disponibilidad de nuevo."
    ),
    422: (
        "Los datos enviados no son válidos (revisa el formato de fecha/hora o los IDs). "
        "No repitas la misma llamada sin corregir."
    ),
    404: "No se encontró el recurso solicitado (servicio, proveedor o reserva inexistente).",
}


async def _http_post_json(
    *,
    tenant_id: UUID,
    tool_key: str,
    config: dict[str, Any],
    parametros: dict[str, Any],
    conversation_id: UUID,
    mensaje_id: str | None,
    contacto: dict[str, Any],
    zona_horaria: str | None,
) -> str:
    """
    Gestiones que escriben (crear, cancelar o re-agendar una cita).

    El cuerpo lo arma el servidor: `conversation_id` va después de los
    parámetros del modelo precisamente para que no lo pueda sobrescribir — si
    pudiera, pediría gestiones sobre la conversación de otro cliente.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    params = dict(parametros)

    # El modelo da fecha y hora por separado (es lo natural en un chat, y
    # calcular el offset UTC lo falla seguido). El ISO con zona se arma acá,
    # con la zona horaria del negocio.
    if params.get("fecha") and params.get("hora"):
        zona = zona_horaria or "America/Mexico_City"
        try:
            dt = datetime.fromisoformat(f"{params['fecha']}T{params['hora']}")
            params["hora_inicio"] = dt.replace(tzinfo=ZoneInfo(zona)).isoformat()
        except ValueError:
            return (
                "La fecha o la hora no tienen un formato válido. Pídele al cliente "
                "que las confirme (día y hora) y vuelve a intentarlo."
            )
        params.pop("fecha", None)
        params.pop("hora", None)

    url = _url_con_parametros(config.get("url_template") or "", tenant_id, params)
    motivo_rechazo = await _destino_permitido(url)
    if motivo_rechazo:
        log.error(
            "Herramienta %s (tenant %s) rechazada: %s", tool_key, tenant_id, motivo_rechazo
        )
        return ERROR_TECNICO

    cuerpo: dict[str, Any] = {"tenant_id": str(tenant_id), **params}

    if config.get("incluir_contacto"):
        cuerpo["cliente_nombre"] = contacto.get("nombre")
        cuerpo["cliente_telefono"] = contacto.get("telefono")

    # Después de params, nunca antes: el modelo no puede sobrescribirlo.
    cuerpo["conversation_id"] = str(conversation_id)

    if config.get("idempotent"):
        # Determinista a partir de datos que el modelo no controla: el mismo
        # mensaje entrante da la misma clave, así que un reintento no duplica
        # la reserva. Se hashea porque el id de WhatsApp es largo y el backend
        # acota la clave a 120 caracteres.
        semilla = f"{tenant_id}:{conversation_id}:{mensaje_id or ''}"
        corto = hashlib.sha256(semilla.encode()).hexdigest()[:24]
        cuerpo["idempotency_key"] = f"{tool_key}:{corto}"

    cabeceras = {"Content-Type": "application/json"}
    token = await _credencial(tenant_id, tool_key)
    if token and config.get("auth_header"):
        cabeceras[config["auth_header"]] = token

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as cliente:
        respuesta = await cliente.post(url, json=cuerpo, headers=cabeceras)

    if respuesta.status_code >= 400:
        try:
            detalle = (respuesta.json() or {}).get("detail")
        except ValueError:
            detalle = None
        codigo = detalle.get("codigo") if isinstance(detalle, dict) else None

        motivo = _MOTIVOS_POR_CODIGO.get(codigo) or _MOTIVOS_POR_STATUS.get(
            respuesta.status_code, ERROR_TECNICO
        )
        log.warning(
            "Herramienta %s (tenant %s) respondió %s (codigo=%s)",
            tool_key, tenant_id, respuesta.status_code, codigo,
        )
        return motivo

    return (respuesta.text or "Listo.")[:MAX_RESULTADO]


# ============================================================
# Google Sheets y Docs
# ============================================================
async def _google_sheets(config: dict[str, Any]) -> str:
    from fastapi.concurrency import run_in_threadpool

    from services import google_tools

    try:
        filas = await run_in_threadpool(
            google_tools.leer_rango,
            config.get("spreadsheet_id"),
            config.get("range"),
        )
    except google_tools.ToolAccessError as e:
        # El mensaje dice cómo arreglarlo ("compártelo con la cuenta de
        # servicio"), pero va dirigido al dueño del negocio: al cliente que
        # está en WhatsApp no le sirve de nada. Queda en el log.
        log.error("Google Sheets inaccesible: %s", e.mensaje)
        return ERROR_TECNICO

    if not filas:
        return "La hoja no tiene datos en ese rango."
    return "\n".join(" | ".join(str(c) for c in fila) for fila in filas)[:MAX_RESULTADO]


async def _google_docs(config: dict[str, Any]) -> str:
    from fastapi.concurrency import run_in_threadpool

    from services import google_tools

    try:
        texto = await run_in_threadpool(
            google_tools.leer_documento, config.get("document_id")
        )
    except google_tools.ToolAccessError as e:
        log.error("Google Docs inaccesible: %s", e.mensaje)
        return ERROR_TECNICO

    return texto[:MAX_DOCUMENTO] if texto else "El documento está vacío."
