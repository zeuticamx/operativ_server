"""
Alertas persistentes cuando una conversación cambia de manos.

`realtime.emit_conversacion_estado` solo refresca la lista de quien tiene el
panel abierto: no deja nada en `alertas`, así que quien no estaba conectado no
se entera. Estas funciones agregan la notificación guardada (y empujada por
WebSocket) para los cambios que hace una persona: tomar, soltar, quitar,
reasignar o devolver a la IA. El aviso cuando el agente delega ya vive en
routers/eventos.py::conversacion_transferida.

Son best-effort: la operación ya se aplicó, así que un fallo al avisar se
registra y no tumba la petición.
"""

import logging
from typing import Optional
from uuid import UUID

from realtime import avisar_a_usuario, broadcast_alerta

logger = logging.getLogger("operativai.avisos_conversacion")


def _datos(conversacion_id: UUID, **extra: str) -> dict:
    return {"conversation_id": str(conversacion_id), **extra}


async def _al_negocio(
    tenant_id: UUID, tipo: str, titulo: str, mensaje: str, datos: dict
) -> None:
    try:
        await broadcast_alerta(tenant_id, tipo, titulo, mensaje, datos)
    except Exception:
        logger.exception("No se pudo guardar la alerta %s", tipo)


async def _a_usuario(
    tenant_id: UUID,
    portal_user_id: Optional[UUID],
    tipo: str,
    titulo: str,
    mensaje: str,
    datos: dict,
) -> None:
    if portal_user_id is None:
        return
    try:
        await avisar_a_usuario(tenant_id, portal_user_id, tipo, titulo, mensaje, datos)
    except Exception:
        logger.exception("No se pudo guardar la alerta personal %s", tipo)


async def tomada(
    tenant_id: UUID, conversacion_id: UUID, quien: str, *, desde_soporte: bool = False
) -> None:
    """Alguien (o soporte de plataforma) tomó una conversación: lo ve todo el negocio."""
    sujeto = "El equipo de soporte de OperativAI" if desde_soporte else quien
    await _al_negocio(
        tenant_id,
        "conversacion_tomada",
        "Conversación tomada por una persona",
        f"{sujeto} tomó la conversación; la IA dejó de responder.",
        _datos(conversacion_id),
    )


async def liberada(
    tenant_id: UUID,
    conversacion_id: UUID,
    anterior: Optional[UUID],
    actor_id: UUID,
    quien: str,
    *,
    reasignada: bool = False,
) -> None:
    """
    Le avisa al asignado anterior que ya no es suya (se la quitaron o la
    reasignaron). Si la soltó él mismo no hay nada que avisar.
    """
    if anterior is None or anterior == actor_id:
        return
    accion = "reasignó a otra persona" if reasignada else "te quitó"
    await _a_usuario(
        tenant_id,
        anterior,
        "conversacion_liberada",
        "Ya no tienes una conversación",
        f"{quien} {accion} una conversación que tenías asignada.",
        _datos(conversacion_id),
    )


async def devuelta_a_ia(
    tenant_id: UUID,
    conversacion_id: UUID,
    anterior: Optional[UUID],
    actor_id: Optional[UUID],
    quien: str,
    *,
    desde_soporte: bool = False,
) -> None:
    """La conversación volvió a la IA: lo ve el negocio y, si la tenía alguien, esa persona."""
    sujeto = "El equipo de soporte de OperativAI" if desde_soporte else quien
    mensaje = f"{sujeto} le devolvió la conversación a la IA."
    datos = _datos(conversacion_id)
    await _al_negocio(
        tenant_id, "conversacion_devuelta_ia", "Conversación devuelta a la IA", mensaje, datos
    )
    if anterior is not None and anterior != actor_id:
        await _a_usuario(
            tenant_id, anterior, "conversacion_devuelta_ia",
            "Conversación devuelta a la IA", mensaje, datos,
        )
