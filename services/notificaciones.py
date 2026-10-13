"""
Avisos a vendedores.

`notificar_vendedor_nuevo_lead` es un STUB A PROPÓSITO: el envío real por
WhatsApp lo hace un sub-workflow de n8n que ya existe. Acá solo queda la
interfaz y el punto donde engancharlo, para que `eventos.py` no tenga que
cambiar cuando se conecte de verdad.

`avisar_leads_asignados` sí está implementado: alerta personal en el
portal / app móvil (WebSocket + push) cuando un lead pasa a ser del
vendedor.
"""

import logging
from typing import Any, Iterable, Optional
from uuid import UUID

from realtime import broadcast_alerta
from session import fetch_one

log = logging.getLogger("operativai.notificaciones")


async def avisar_leads_asignados(
    tenant_id: UUID,
    vendedor_id: Optional[UUID],
    user_ids: Iterable[UUID],
    *,
    actor_portal_user_id: Optional[UUID] = None,
    cliente_nombre: Optional[str] = None,
) -> None:
    """
    Alerta PERSONAL ('lead_asignado') al vendedor que acaba de recibir uno o
    varios leads. Una sola alerta por llamada: un reparto de pendientes que
    le pasa 30 leads a alguien es "te asignaron 30 leads", no 30 toasts.

    No avisa si el vendedor se lo asignó a sí mismo, si no tiene cuenta del
    portal (la alerta personal va a la room de un usuario) o si está
    inactivo. `cliente_nombre` ahorra la consulta cuando quien llama ya lo
    tiene; con varios leads no se usa.

    Best-effort: se llama después del commit. Un fallo acá no deshace la
    asignación, que es lo que importa.
    """
    ids = list(dict.fromkeys(user_ids))
    if vendedor_id is None or not ids:
        return
    try:
        vendedor = await fetch_one(
            """
            SELECT v.portal_user_id
            FROM vendedores v
            JOIN portal_users pu ON pu.id = v.portal_user_id
            WHERE v.id = $1 AND v.tenant_id = $2 AND v.activo AND pu.is_active
            """,
            vendedor_id,
            tenant_id,
        )
        if vendedor is None or vendedor["portal_user_id"] == actor_portal_user_id:
            return

        if len(ids) == 1:
            nombre = cliente_nombre
            if nombre is None:
                fila = await fetch_one(
                    """
                    SELECT NULLIF(NULLIF(TRIM(display_name), ''), 'null') AS nombre
                    FROM users WHERE id = $1 AND tenant_id = $2
                    """,
                    ids[0],
                    tenant_id,
                )
                nombre = fila["nombre"] if fila else None
            titulo = "🎯 Tienes un lead nuevo"
            mensaje = f"{nombre or 'Un cliente'} ahora es tuyo."
            # `cliente_id` es el user_id del lead: la app abre /(leads)/[id] con él.
            datos: dict[str, Any] = {"cliente_id": str(ids[0]), "cliente_nombre": nombre}
        else:
            titulo = "🎯 Te asignaron leads"
            mensaje = f"Ahora tienes {len(ids)} leads nuevos en tu cartera."
            datos = {"cliente_ids": [str(i) for i in ids[:50]], "total": len(ids)}

        await broadcast_alerta(
            tenant_id,
            tipo="lead_asignado",
            titulo=titulo,
            mensaje=mensaje[:1000],
            datos={**datos, "vendedor_id": str(vendedor_id)},
            portal_user_id=vendedor["portal_user_id"],
        )
    except Exception:
        log.exception("No se pudo avisar la asignación al vendedor %s", vendedor_id)


async def notificar_vendedor_nuevo_lead(
    vendedor_id: UUID,
    user_id: UUID,
    mensaje: dict[str, Any],
    tenant_id: Optional[UUID] = None,
) -> bool:
    """
    Avisa a un vendedor que le acaba de caer un lead nuevo.

    Se llama únicamente cuando el agente de IA está apagado para ese tenant:
    con el agente encendido, el cliente ya está recibiendo respuesta y el
    vendedor entra después, no en caliente.

    Parámetros
    ----------
    vendedor_id : a quién avisar. Su teléfono está en `vendedores.telefono`;
                  hay que leerlo acá, no pedírselo a quien llama.
    user_id     : el cliente final (tabla `users`) que escribió. De ahí sale
                  el nombre y el handle para armar el aviso.
    mensaje     : el mensaje entrante crudo tal como lo mandó n8n. Sirve
                  para meter un preview del texto en el aviso.
    tenant_id   : de qué negocio, para elegir el canal y las credenciales.

    Qué debería hacer cuando se implemente
    --------------------------------------
    1. Leer `vendedores.telefono` y salir sin error si está vacío: un
       vendedor sin teléfono cargado no es motivo para tumbar el mensaje
       entrante del cliente.
    2. Llamar al sub-workflow de n8n de envío (HTTP POST) con el teléfono,
       el nombre del cliente y el preview.
    3. Devolver False si el aviso no salió, en vez de propagar la
       excepción: el lead ya quedó asignado y eso es lo que importa: el
       aviso es best-effort.

    Devuelve
    --------
    True si el aviso salió. Hoy siempre False, porque no sale ninguno.
    """
    # TODO: enganchar con el sub-workflow de n8n de envío.
    log.info(
        "Aviso de lead nuevo pendiente de enviar: vendedor=%s cliente=%s tenant=%s",
        vendedor_id,
        user_id,
        tenant_id,
    )
    return False
