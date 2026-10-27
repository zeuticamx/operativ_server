"""
Cliente de la API de plantillas de Meta Graph (WhatsApp Cloud API directa),
análogo a neuroapi_plantillas.py pero para negocios con
`channel_credentials.bsp_provider = 'meta'` (conectados por el Embedded
Signup de routers/canales.py `/whatsapp/meta/activar`).

El token es el mismo user token de `/canales/meta/conectar` (igual que
Facebook/Instagram); `channel_credentials` solo guarda el `phone_number_id`,
nunca el `waba_id`, así que hay que resolverlo con Graph
(`meta.obtener_waba_id`) antes de poder hablar con `/message_templates`.

Reusa `PlantillaApiError`/`a_http`/`es_duplicado` de neuroapi_plantillas: el
contrato de error ("fallo al hablar con la API de plantillas de Meta") es el
mismo para los dos proveedores, no algo propio de NeuroAPI -- por eso
`routers/plantillas_whatsapp_tenant.py` puede manejar ambos con el mismo
`except PlantillaApiError`.
"""

import json
import logging
from typing import Any
from uuid import UUID

import httpx

from config import settings
from services import meta
from services.neuroapi_plantillas import PlantillaApiError, es_duplicado
from services.whatsapp import ProveedorWhatsAppError, ResultadoEnvio
from session import fetch_one

log = logging.getLogger("operativai.meta_plantillas")

RUTA = "/message_templates"
TIMEOUT_S = 20


def _detalle_meta(cuerpo: str | None) -> str | None:
    """Mensaje de error que Meta devolvió, si el cuerpo es el JSON de error de Graph."""
    if not cuerpo:
        return None
    try:
        dato = json.loads(cuerpo)
    except ValueError:
        return None
    if not isinstance(dato, dict):
        return None
    error = dato.get("error")
    if not isinstance(error, dict):
        return None
    for candidato in (error.get("error_user_msg"), error.get("message")):
        if isinstance(candidato, str) and candidato.strip():
            return candidato.strip()[:500]
    return None


class MetaTemplateService:
    def __init__(self, token: str, waba_id: str) -> None:
        self._token = token
        self.waba_id = waba_id

    # --- construcción ---
    @classmethod
    async def desde_tenant(cls, tenant_id: UUID) -> "MetaTemplateService":
        cred = await fetch_one(
            "SELECT * FROM get_channel_credentials($1, $2)", tenant_id, "whatsapp"
        )
        if cred is None:
            raise PlantillaApiError("El negocio no tiene un canal de WhatsApp activo")
        if cred["bsp_provider"] != "meta":
            raise PlantillaApiError("El canal de WhatsApp del negocio no es de Meta")
        token = cred["access_token"]
        phone_number_id = cred["phone_number_id"]
        if not token or not phone_number_id:
            raise PlantillaApiError("La línea de WhatsApp del negocio está incompleta")
        try:
            waba_id = await meta.obtener_waba_id(phone_number_id, token)
        except meta.MetaError as e:
            raise PlantillaApiError(e.mensaje) from e
        return cls(token=token, waba_id=waba_id)

    @staticmethod
    async def proveedor_envio(tenant_id: UUID) -> "_EnvioPlantillaMeta":
        """`_EnvioPlantillaMeta` de la línea activa del negocio, para los envíos de prueba."""
        cred = await fetch_one(
            "SELECT * FROM get_channel_credentials($1, $2)", tenant_id, "whatsapp"
        )
        if cred is None or cred["bsp_provider"] != "meta":
            raise PlantillaApiError("El negocio no tiene una línea de WhatsApp de Meta activa")
        phone_number_id = cred["phone_number_id"] or ""
        token = cred["access_token"] or ""
        if not phone_number_id or not token:
            raise PlantillaApiError("La línea de WhatsApp del negocio está incompleta")
        return _EnvioPlantillaMeta(phone_number_id=phone_number_id, token=token)

    # --- transporte ---
    async def _pedir(
        self,
        metodo: str,
        *,
        params: dict[str, Any] | None = None,
        cuerpo: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{settings.graph_url}/{self.waba_id}{RUTA}"
        headers = {"Authorization": f"Bearer {self._token}"}
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as cliente:
            try:
                r = await cliente.request(metodo, url, params=params, json=cuerpo, headers=headers)
            except httpx.RequestError as e:
                raise PlantillaApiError(f"No se pudo contactar a Meta: {type(e).__name__}") from e

        if r.status_code >= 400:
            log.warning("Meta %s %s -> %s: %s", metodo, RUTA, r.status_code, r.text[:300])
            raise PlantillaApiError(
                f"Meta respondió {r.status_code}",
                status_code=r.status_code,
                cuerpo=r.text[:2000],
                detalle_meta=_detalle_meta(r.text),
            )
        try:
            return r.json()
        except ValueError:
            return {}

    # --- los 4 endpoints ---
    async def listar(
        self,
        *,
        categoria: str | None = None,
        estado: str | None = None,
        limite: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limite}
        if categoria:
            params["category"] = categoria
        if estado:
            params["status"] = estado
        data = await self._pedir("GET", params=params)
        nodo = data.get("data") if isinstance(data, dict) else None
        return [x for x in nodo if isinstance(x, dict)] if isinstance(nodo, list) else []

    async def obtener(self, nombre: str) -> dict[str, Any] | None:
        data = await self._pedir("GET", params={"name": nombre})
        nodo = data.get("data") if isinstance(data, dict) else None
        if isinstance(nodo, list) and nodo:
            return nodo[0]
        return None

    async def crear(
        self, *, nombre: str, idioma: str, categoria: str, componentes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Devuelve lo que Meta contestó (id y estado inicial); {} si vino vacío."""
        data = await self._pedir(
            "POST",
            cuerpo={
                "name": nombre,
                "language": idioma,
                "category": categoria,
                "components": componentes,
            },
        )
        return data if isinstance(data, dict) and data else {}

    async def crear_o_adoptar(
        self, *, nombre: str, idioma: str, categoria: str, componentes: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], bool]:
        """
        Crea la plantilla; si Meta dice que ya existe (spec 007: duplicado ->
        adoptar) devuelve la que ya hay. Resultado: (respuesta de Meta, `creada`).
        """
        try:
            return await self.crear(
                nombre=nombre, idioma=idioma, categoria=categoria, componentes=componentes
            ), True
        except PlantillaApiError as e:
            if not es_duplicado(e):
                raise
        try:
            return (await self.obtener(nombre)) or {}, False
        except PlantillaApiError as e:
            if e.status_code == 404:
                return {}, False
            raise

    async def eliminar(self, nombre: str) -> None:
        await self._pedir("DELETE", params={"name": nombre})


class _EnvioPlantillaMeta:
    """
    Adaptador mínimo para el botón "Enviar prueba" de una línea de Meta: solo
    implementa `enviar_plantilla_con_componentes`, lo único que
    routers/plantillas_whatsapp_tenant.py necesita de un proveedor de envío.
    Reusa services/meta.py (mismo Graph directo que services/agente/envio.py
    ya usa para el loop del agente) en vez de duplicar manejo de errores HTTP.
    """

    def __init__(self, phone_number_id: str, token: str) -> None:
        self._phone_number_id = phone_number_id
        self._token = token

    async def enviar_plantilla_con_componentes(
        self,
        destinatario: str,
        plantilla: str,
        idioma: str,
        componentes: list[dict[str, Any]],
    ) -> ResultadoEnvio:
        template: dict[str, Any] = {"name": plantilla, "language": {"code": idioma}}
        if componentes:
            template["components"] = componentes
        cuerpo = {
            "messaging_product": "whatsapp",
            "to": destinatario,
            "type": "template",
            "template": template,
        }
        try:
            data = await meta.enviar_texto(
                f"{settings.graph_url}/{self._phone_number_id}/messages", self._token, cuerpo
            )
        except meta.MetaError as e:
            raise ProveedorWhatsAppError(e.mensaje) from e

        id_mensaje = ""
        mensajes = data.get("messages") if isinstance(data, dict) else None
        if isinstance(mensajes, list) and mensajes and isinstance(mensajes[0], dict):
            id_mensaje = str(mensajes[0].get("id") or "")
        return ResultadoEnvio(id_mensaje=id_mensaje, crudo=data if isinstance(data, dict) else {})
