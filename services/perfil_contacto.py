"""
Nombre del contacto de Instagram/Facebook, consultado a Meta.

El webhook solo trae un id numérico (IGSID / PSID), así que el cliente nace sin
nombre. Este módulo se lo pide a Meta y lo guarda en `users`. Lo usan:

  - el botón manual del portal (`POST /api/conversaciones/{id}/contacto`), que
    quiere el error exacto, y
  - el ingreso de mensajes (`services/agente/ingreso.py`), que lo dispara solo
    cuando el cliente aparece sin nombre y no puede permitirse fallar.

Sin HTTP: los fallos de configuración salen como `PerfilNoDisponible` con el
status que el router debe devolver.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from services import meta
from session import execute, fetch_one

log = logging.getLogger("perfil_contacto")

# Un intento automático por cliente y proceso: sin esto, un cliente al que Meta
# no le devuelve nombre costaría una llamada a la Graph API por cada mensaje.
_INTENTADOS: set[UUID] = set()
_TAREAS: set[asyncio.Task] = set()


class PerfilNoDisponible(Exception):
    """No se pudo consultar el perfil por algo de la cuenta (no de Meta)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def consultar_y_guardar(
    tenant_id: UUID, user_id: UUID, canal: str, contacto_id: str
) -> dict[str, str | None]:
    """
    Pide el perfil a Meta y lo guarda. Devuelve `{"nombre", "username"}`.

    Levanta `PerfilNoDisponible` si el canal o la cuenta de Meta no están en
    condiciones, y `meta.MetaError` si Meta rechaza la consulta.
    """
    # El page_id dice a qué página pertenece la conversación; el token de
    # esa página se pide a Meta con el user token, igual que al conectar.
    canal_fila = await fetch_one(
        """
        SELECT page_id FROM v_tenant_channels
        WHERE tenant_id = $1 AND channel_type = $2 AND is_active
        """,
        tenant_id,
        canal,
    )
    if canal_fila is None or not canal_fila["page_id"]:
        raise PerfilNoDisponible(409, f"El canal {canal} no está conectado")

    conexion = await fetch_one("SELECT * FROM get_meta_connection($1)", tenant_id)
    if conexion is None:
        raise PerfilNoDisponible(409, "Todavía no has conectado tu cuenta de Meta")

    paginas = await meta.listar_paginas(conexion["user_token"])
    page_token = next(
        (p["access_token"] for p in paginas if p["id"] == canal_fila["page_id"]),
        None,
    )
    if page_token is None:
        raise PerfilNoDisponible(409, "La página conectada ya no está autorizada en Meta")
    perfil = await meta.perfil_contacto(canal, contacto_id, page_token)

    # COALESCE: si Meta no devuelve algo, se conserva lo que ya hubiera
    # en vez de borrarlo.
    await execute(
        """
        UPDATE users
        SET display_name = COALESCE($1, display_name),
            instagram_username = COALESCE($2, instagram_username)
        WHERE id = $3
        """,
        perfil["nombre"],
        perfil["username"],
        user_id,
    )
    return perfil


def pedir_en_segundo_plano(
    tenant_id: UUID, user_id: UUID, canal: str, contacto_id: str
) -> None:
    """
    Dispara la consulta sin esperarla y sin que pueda romper el ingreso.

    Un solo intento por cliente mientras viva el proceso. Cualquier fallo
    (Instagram por NeuroAPI no tiene conexión de Meta, la página perdió el
    permiso, Meta no responde) queda en el log; el botón manual sigue ahí.
    """
    if canal not in ("instagram", "facebook") or user_id in _INTENTADOS:
        return
    _INTENTADOS.add(user_id)

    async def _correr() -> None:
        try:
            await consultar_y_guardar(tenant_id, user_id, canal, contacto_id)
        except PerfilNoDisponible as e:
            log.info("Perfil de %s no consultado: %s", canal, e.detail)
        except Exception:
            log.warning("No se pudo consultar el perfil de %s", canal, exc_info=True)

    tarea = asyncio.create_task(_correr())
    _TAREAS.add(tarea)
    tarea.add_done_callback(_TAREAS.discard)
