"""
Avisos a vendedores.

STUB A PROPÓSITO: el envío real lo hace un sub-workflow de n8n que ya
existe. Acá solo queda la interfaz y el punto donde engancharlo, para que
`eventos.py` no tenga que cambiar cuando se conecte de verdad.
"""

import logging
from typing import Any, Optional
from uuid import UUID

log = logging.getLogger("operativai.notificaciones")


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
