"""
Cliente de la "cuenta global" de WhatsApp (spec 004).

La cuenta global es la línea de NeuroAPI de un solo tenant, el configurado en
`AVISOS_CUENTA_GLOBAL_TENANT_ID`. Sus credenciales se leen de su fila de
`channel_credentials` (función SQL `get_channel_credentials`, que descifra con
`app.cred_key` y solo devuelve canales activos), igual que hace
`services/conversaciones.py` para responder a un cliente. Nunca se consulta el
tenant de la reserva: el aviso sale siempre por la misma línea.

Todo fallo de configuración lanza `ProveedorWhatsAppError` ANTES de salir a la
red. Ni las credenciales ni el teléfono se escriben en el log.
"""

import logging
from uuid import UUID

from config import settings
from services import whatsapp
from services.whatsapp import ProveedorWhatsAppError, ResultadoEnvio
from session import fetch_one

log = logging.getLogger("operativai.cuenta_global_wa")

# Idioma de las dos plantillas de avisos. Siempre explícito: el default `es`
# de enviar_plantilla se queda como está para los demás llamadores.
IDIOMA_PLANTILLAS = "es_MX"


def _tenant_configurado() -> UUID:
    valor = (settings.AVISOS_CUENTA_GLOBAL_TENANT_ID or "").strip()
    if not valor:
        raise ProveedorWhatsAppError(
            "AVISOS_CUENTA_GLOBAL_TENANT_ID sin configurar", status_code=None
        )
    try:
        return UUID(valor)
    except ValueError as e:
        raise ProveedorWhatsAppError(
            "AVISOS_CUENTA_GLOBAL_TENANT_ID no es un UUID válido", status_code=None
        ) from e


async def obtener_proveedor_global() -> whatsapp.NeuroApiProvider:
    """
    Arma el `NeuroApiProvider` de la cuenta global.

    Exige canal de WhatsApp activo con `bsp_provider='neuroapi'` y un
    `phone_number_id`. Si el canal no trae `access_token`, usa
    `NEUROAPI_API_KEY` (misma clave de plataforma para todas las líneas).
    """
    tenant_id = _tenant_configurado()
    cred = await fetch_one(
        "SELECT * FROM get_channel_credentials($1, $2)", tenant_id, "whatsapp"
    )
    if cred is None:
        raise ProveedorWhatsAppError(
            "La cuenta global no tiene un canal de WhatsApp activo", status_code=None
        )
    if cred["bsp_provider"] != "neuroapi":
        raise ProveedorWhatsAppError(
            "El canal de la cuenta global no es de NeuroAPI", status_code=None
        )

    phone_number_id = cred["phone_number_id"] or ""
    if not phone_number_id:
        raise ProveedorWhatsAppError(
            "El canal de la cuenta global no tiene phone_number_id", status_code=None
        )

    api_key = cred["access_token"] or settings.NEUROAPI_API_KEY
    if not api_key:
        raise ProveedorWhatsAppError(
            "La cuenta global no tiene clave de NeuroAPI", status_code=None
        )

    return whatsapp.NeuroApiProvider(
        api_key=api_key,
        webhook_secret="",
        base_url=settings.NEUROAPI_API_BASE_URL,
        phone_number_id=phone_number_id,
    )


async def enviar_plantilla(
    destinatario_e164: str, plantilla: str, parametros: list[str]
) -> ResultadoEnvio:
    """
    Envía una plantilla aprobada (`es_MX`) desde la cuenta global.

    `parametros` van en orden como variables {{1}}, {{2}}... del cuerpo.
    """
    proveedor = await obtener_proveedor_global()
    return await proveedor.enviar_plantilla(
        destinatario_e164,
        plantilla,
        idioma=IDIOMA_PLANTILLAS,
        parametros=parametros,
    )


async def enviar_texto_global(destinatario_e164: str, texto: str) -> ResultadoEnvio:
    """
    Texto libre desde la cuenta global. Solo para la respuesta fija (D13) a quien
    acaba de escribir (ventana de 24 h abierta). NUNCA para los avisos de cita:
    esos son siempre plantilla aprobada.
    """
    proveedor = await obtener_proveedor_global()
    return await proveedor.enviar_mensaje(destinatario_e164, texto)
