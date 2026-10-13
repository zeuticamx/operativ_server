"""
Push a la app móvil (crm-mobile-app) vía Expo Push Service.

No es un canal aparte: es la copia "con la app cerrada" de lo que ya sale
por WebSocket. `realtime.broadcast_alerta` llama a `programar_push_alerta`
con los mismos destinatarios que la room a la que emite:

  alerta personal (portal_user_id)  -> los dispositivos de ese usuario
  alerta del negocio                -> los de quien está en la room del
                                       negocio (deps.ROLES_NEGOCIO). Un
                                       vendedor o proveedor no está ahí por
                                       WebSocket y tampoco recibe el push.

Los tokens los registra la app en `dispositivos_push`
(routers/dispositivos.py). Expo responde un "ticket" por mensaje; un
`DeviceNotRegistered` significa que la app se desinstaló o el token caducó,
y ese token se borra para no volver a intentarlo.

Best-effort, igual que el WebSocket: corre en una tarea aparte para no
demorar la petición que dispara la alerta, y nunca levanta excepción.
"""

import asyncio
import json
import logging
from typing import Any, Optional
from uuid import UUID

import httpx

from config import settings
from deps import ROLES_NEGOCIO
from session import execute, fetch_all

log = logging.getLogger("operativai.push")

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

# Tope de Expo por petición.
_MENSAJES_POR_LOTE = 100

# El payload completo de un push no puede pasar de 4 KB. `datos` de una
# alerta suele ser chico, pero si no cabe se manda sin él: la app igual
# abre la pantalla de alertas y ahí lo lee completo por HTTP.
_MAX_BYTES_DATOS = 2048

# Canal de Android que crea la app (src/utils/push.ts). Si no existe en el
# teléfono, Android usa el canal por defecto.
CANAL_ANDROID = "alertas"

# Referencias a las tareas en curso: sin esto el recolector de basura puede
# llevarse una tarea de asyncio a medio camino (documentado en create_task).
_tareas: set[asyncio.Task] = set()


async def tokens_destino(tenant_id: UUID, portal_user_id: Optional[UUID]) -> list[str]:
    """Tokens de quien recibiría la alerta en vivo. Usuarios inactivos, fuera."""
    filas = await fetch_all(
        """
        SELECT d.token
        FROM dispositivos_push d
        JOIN portal_users pu ON pu.id = d.portal_user_id
        WHERE d.tenant_id = $1
          AND pu.is_active
          AND (
                ($2::uuid IS NOT NULL AND d.portal_user_id = $2)
             OR ($2::uuid IS NULL AND pu.role = ANY($3::text[]))
          )
        """,
        tenant_id,
        portal_user_id,
        list(ROLES_NEGOCIO),
    )
    return [f["token"] for f in filas]


def armar_mensajes(
    tokens: list[str],
    alerta_id: UUID,
    tipo: str,
    titulo: str,
    mensaje: str,
    datos: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    data: dict[str, Any] = {"alerta_id": str(alerta_id), "tipo": tipo}
    if datos:
        crudo = json.dumps(datos, default=str, ensure_ascii=False)
        if len(crudo.encode("utf-8")) <= _MAX_BYTES_DATOS:
            data["datos"] = json.loads(crudo)
    return [
        {
            "to": token,
            "title": titulo,
            "body": mensaje,
            "data": data,
            "sound": "default",
            "priority": "high",
            "channelId": CANAL_ANDROID,
        }
        for token in tokens
    ]


def tokens_invalidos(lote: list[dict[str, Any]], tickets: list[dict[str, Any]]) -> list[str]:
    """Los tickets vuelven en el mismo orden que los mensajes."""
    return [
        mensaje["to"]
        for mensaje, ticket in zip(lote, tickets)
        if ticket.get("status") == "error"
        and (ticket.get("details") or {}).get("error") == "DeviceNotRegistered"
    ]


async def enviar(mensajes: list[dict[str, Any]]) -> int:
    """Manda los mensajes en lotes. Devuelve cuántos aceptó Expo."""
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if settings.EXPO_ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {settings.EXPO_ACCESS_TOKEN}"

    aceptados = 0
    async with httpx.AsyncClient(timeout=10) as cliente:
        for inicio in range(0, len(mensajes), _MENSAJES_POR_LOTE):
            lote = mensajes[inicio : inicio + _MENSAJES_POR_LOTE]
            try:
                respuesta = await cliente.post(EXPO_PUSH_URL, json=lote, headers=headers)
                respuesta.raise_for_status()
                tickets = respuesta.json().get("data") or []
            except Exception:
                # Un lote que falla no frena los demás.
                log.exception("Expo rechazó un lote de %d push", len(lote))
                continue

            aceptados += sum(1 for t in tickets if t.get("status") == "ok")
            invalidos = tokens_invalidos(lote, tickets)
            if invalidos:
                await execute(
                    "DELETE FROM dispositivos_push WHERE token = ANY($1::text[])",
                    invalidos,
                )
                log.info("Se borraron %d tokens de push que ya no existen", len(invalidos))
    return aceptados


async def enviar_push_alerta(
    tenant_id: UUID,
    portal_user_id: Optional[UUID],
    alerta_id: UUID,
    tipo: str,
    titulo: str,
    mensaje: str,
    datos: Optional[dict[str, Any]] = None,
) -> int:
    """Busca los dispositivos y manda el push. Nunca levanta excepción."""
    if not settings.PUSH_ACTIVO:
        return 0
    try:
        tokens = await tokens_destino(tenant_id, portal_user_id)
        if not tokens:
            return 0
        return await enviar(armar_mensajes(tokens, alerta_id, tipo, titulo, mensaje, datos))
    except Exception:
        log.exception("No se pudo enviar el push de la alerta %s", alerta_id)
        return 0


def programar_push_alerta(
    tenant_id: UUID,
    portal_user_id: Optional[UUID],
    alerta_id: UUID,
    tipo: str,
    titulo: str,
    mensaje: str,
    datos: Optional[dict[str, Any]] = None,
) -> None:
    """
    Igual que `enviar_push_alerta` pero sin esperar: la petición que creó la
    alerta no tiene por qué pagar la ida y vuelta a Expo (ni un reparto de
    pendientes esperar cientos de envíos).
    """
    if not settings.PUSH_ACTIVO:
        return
    tarea = asyncio.create_task(
        enviar_push_alerta(tenant_id, portal_user_id, alerta_id, tipo, titulo, mensaje, datos)
    )
    _tareas.add(tarea)
    tarea.add_done_callback(_tareas.discard)
