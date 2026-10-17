"""
Respuesta fija de la cuenta global de WhatsApp (spec 004, plan 2.13, D13).

La cuenta global es la línea de un tenant que además tiene su propio agente.
Si un cliente de OTRO negocio responde a un aviso de cita, ese mensaje no debe
llegar al agente del tenant de la línea: se contesta con un texto fijo, como
máximo una vez cada 24 h, y no se reenvía a n8n ni se crea evento de contexto.

El gancho vive en `routers/canales_neuroapi_webhook._reenviar_mensaje` y solo
actúa con `AVISOS_CUENTA_GLOBAL_RESPUESTA_ACTIVA`. Se desvía únicamente si:
  1. el receptor es la línea de la cuenta global;
  2. el remitente tiene un aviso `enviado` en las últimas 24 h
     (`avisos_cita.telefono_destino`);
  3. el remitente no es ya un `users` del tenant de la línea.
Cualquier otro caso (o cualquier fallo del gancho) sigue el flujo de siempre.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from config import settings
from services import cuenta_global_wa
from services.avisos_cita_reglas import HORAS_RESPUESTA, puede_responder
from session import execute, fetch_one

log = logging.getLogger("operativai.respuesta_cuenta_global")

# Texto literal acordado con el usuario, con su ortografía (no corregir).
TEXTO_RESPUESTA_CUENTA_GLOBAL = (
    "Este mensaje es con fines informativos para mayor informacion "
    "comunicate al numero donde realizaste tu cita"
)


def extraer_remitente(evento: dict[str, Any]) -> str | None:
    """
    Remitente del mensaje entrante: `messages[].from` del sobre de Meta
    (`entry[].changes[].value`) o, en el formato propio de NeuroAPI, `from` en
    `data` o en la raíz. Solo dígitos; None si no hay (p. ej. un evento de estado).
    """
    candidatos: list[Any] = []
    fuentes: list[dict[str, Any]] = []
    for entrada in evento.get("entry") or []:
        if not isinstance(entrada, dict):
            continue
        for cambio in entrada.get("changes") or []:
            valor = cambio.get("value") if isinstance(cambio, dict) else None
            if isinstance(valor, dict):
                fuentes.append(valor)
    if isinstance(evento.get("data"), dict):
        fuentes.append(evento["data"])
    fuentes.append(evento)

    for fuente in fuentes:
        for mensaje in fuente.get("messages") or []:
            if isinstance(mensaje, dict):
                candidatos.append(mensaje.get("from"))
        candidatos.append(fuente.get("from"))

    for valor in candidatos:
        if isinstance(valor, (str, int)):
            digitos = re.sub(r"\D", "", str(valor))
            if digitos:
                return digitos
    return None


def variantes_digitos(remitente: str) -> list[str]:
    """
    Formas equivalentes del número (solo dígitos). Meta entrega los móviles de
    México como `521XXXXXXXXXX` y el aviso se guardó como `+52XXXXXXXXXX`.
    """
    variantes = [remitente]
    if remitente.startswith("521") and len(remitente) == 13:
        variantes.append("52" + remitente[3:])
    elif remitente.startswith("52") and len(remitente) == 12:
        variantes.append("521" + remitente[2:])
    return variantes


def _tenant_global() -> UUID | None:
    try:
        return UUID((settings.AVISOS_CUENTA_GLOBAL_TENANT_ID or "").strip())
    except ValueError:
        return None


async def _es_linea_global(tenant_id: UUID, phone_number_id: str) -> bool:
    # La credencial (primer número, P2) o la marca de línea de avisos en
    # agentes_canal (spec 005a RF-16): cualquiera de las dos vale.
    fila = await fetch_one(
        """
        SELECT 1 AS ok
          FROM channel_credentials
         WHERE tenant_id = $1
           AND channel_type = 'whatsapp'
           AND phone_number_id = $2
           AND is_active = true
         LIMIT 1
        """,
        tenant_id,
        phone_number_id,
    )
    if fila is not None:
        return True
    fila = await fetch_one(
        """
        SELECT 1 AS ok
          FROM agentes_canal
         WHERE tenant_id = $1
           AND phone_number_id = $2
           AND es_linea_avisos
           AND estado IN ('activo', 'pausado')
         LIMIT 1
        """,
        tenant_id,
        phone_number_id,
    )
    return fila is not None


async def _telefono_con_aviso_reciente(variantes: list[str]) -> str | None:
    """E.164 (con '+') del aviso `enviado` en las últimas 24 h, o None."""
    fila = await fetch_one(
        """
        SELECT telefono_destino
          FROM avisos_cita
         WHERE estado = 'enviado'
           AND telefono_destino = ANY($1::text[])
           AND cerrado_en >= NOW() - make_interval(hours => $2)
         ORDER BY cerrado_en DESC
         LIMIT 1
        """,
        ["+" + v for v in variantes],
        HORAS_RESPUESTA,
    )
    return fila["telefono_destino"] if fila else None


async def _es_cliente_del_tenant(tenant_id: UUID, variantes: list[str]) -> bool:
    # `users` es de n8n (solo lectura); el teléfono de WhatsApp está en `whatsapp_id`.
    fila = await fetch_one(
        """
        SELECT 1 AS ok
          FROM users
         WHERE tenant_id = $1
           AND regexp_replace(COALESCE(whatsapp_id, ''), '\\D', '', 'g') = ANY($2::text[])
         LIMIT 1
        """,
        tenant_id,
        variantes,
    )
    return fila is not None


async def es_respuesta_a_aviso(phone_number_id: str, remitente: str) -> str | None:
    """
    Aplica las tres condiciones de 2.13. Devuelve el E.164 del aviso al que
    responde el remitente, o None si el mensaje debe seguir el flujo normal.
    """
    tenant_id = _tenant_global()
    if tenant_id is None:
        return None
    if not await _es_linea_global(tenant_id, phone_number_id):
        return None
    variantes = variantes_digitos(remitente)
    telefono = await _telefono_con_aviso_reciente(variantes)
    if telefono is None:
        return None
    if await _es_cliente_del_tenant(tenant_id, variantes):
        return None
    return telefono


async def desviar_si_es_respuesta(evento: dict[str, Any], phone_number_id: str) -> bool:
    """
    Gancho del webhook. True = el mensaje se desvió (no se reenvía a n8n).
    Con la bandera apagada, sin remitente o ante cualquier fallo devuelve False.
    """
    if not settings.AVISOS_CUENTA_GLOBAL_RESPUESTA_ACTIVA:
        return False
    try:
        remitente = extraer_remitente(evento)
        if not remitente:
            return False
        telefono = await es_respuesta_a_aviso(phone_number_id, remitente)
        if telefono is None:
            return False
    except Exception:
        log.exception("Falló la comprobación de respuesta a aviso; el mensaje sigue su flujo")
        return False

    # Desde aquí el mensaje YA es una respuesta a un aviso: no debe llegar al agente.
    try:
        fila = await fetch_one(
            "SELECT ultima_respuesta_en FROM cuenta_global_respuestas WHERE telefono = $1",
            telefono,
        )
        ultima: datetime | None = fila["ultima_respuesta_en"] if fila else None
        if not puede_responder(ultima, datetime.now(timezone.utc)):
            log.info("Respuesta a aviso descartada: ya se respondió en las últimas 24 h")
            return True
        await cuenta_global_wa.enviar_texto_global(telefono, TEXTO_RESPUESTA_CUENTA_GLOBAL)
        await execute(
            """
            INSERT INTO cuenta_global_respuestas (telefono, ultima_respuesta_en)
            VALUES ($1, NOW())
            ON CONFLICT (telefono) DO UPDATE SET ultima_respuesta_en = NOW()
            """,
            telefono,
        )
    except Exception:
        # Sin registrar la respuesta: el siguiente mensaje volverá a intentarlo.
        log.exception("No se pudo enviar la respuesta fija de la cuenta global")
    return True
