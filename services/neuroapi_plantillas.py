"""
Cliente de la API de plantillas de NeuroAPI (`/messaging/templates`, scope
`whatsapp_cloud_api`): listar, consultar, crear y eliminar plantillas de la
WABA de un negocio en Meta.

La `x-api-key` sale de la línea de WhatsApp activa del negocio
(`get_channel_credentials`, `bsp_provider='neuroapi'`) y, si la línea no trae
una propia, de `NEUROAPI_API_KEY` (misma clave de plataforma para todas). Todo
fallo de configuración lanza `PlantillaApiError` ANTES de salir a la red. Ni la
clave ni los teléfonos se escriben en el log.
"""

import json
import logging
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException, status

from config import settings
from services.whatsapp import NeuroApiProvider, ProveedorWhatsAppError
from session import fetch_one

log = logging.getLogger("operativai.neuroapi_plantillas")

RUTA = "/neuroapi/messaging/templates"
TIMEOUT_S = 20


class PlantillaApiError(ProveedorWhatsAppError):
    """Error al hablar con la API de plantillas (HTTP, credenciales, red)."""

    def __init__(
        self,
        mensaje: str,
        *,
        status_code: int | None = None,
        cuerpo: str | None = None,
        detalle_meta: str | None = None,
    ) -> None:
        super().__init__(mensaje, status_code=status_code, cuerpo=cuerpo)
        # Mensaje legible que Meta devolvió (p. ej. variables mal formadas).
        self.detalle_meta = detalle_meta


def _detalle_meta(cuerpo: str | None) -> str | None:
    """Mensaje de error de Meta/NeuroAPI dentro del cuerpo, si es JSON reconocible."""
    if not cuerpo:
        return None
    try:
        dato = json.loads(cuerpo)
    except ValueError:
        return None
    if not isinstance(dato, dict):
        return None
    error = dato.get("error")
    candidatos = [
        error.get("error_user_msg") if isinstance(error, dict) else None,
        error.get("message") if isinstance(error, dict) else None,
        error if isinstance(error, str) else None,
        dato.get("message"),
    ]
    for c in candidatos:
        if isinstance(c, str) and c.strip():
            return c.strip()[:500]
    return None


SUBCODIGO_DUPLICADO = "2388023"  # Meta: la plantilla ya existe en ese idioma
_MARCAS_DUPLICADO = ("already exists", "ya existe", "duplicate", str(SUBCODIGO_DUPLICADO))


def es_duplicado(e: PlantillaApiError) -> bool:
    """Meta/NeuroAPI contestó que el nombre (e idioma) ya existe: 409, o 400 con su marca."""
    if e.status_code == 409:
        return True
    if e.status_code != 400 or not e.cuerpo:
        return False
    cuerpo = e.cuerpo.lower()
    return any(m in cuerpo for m in _MARCAS_DUPLICADO)


def a_http(e: PlantillaApiError) -> HTTPException:
    """400 de Meta -> 422 con su motivo; permisos/clave -> 502; el resto, según el caso."""
    if e.status_code == 400:
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=e.detalle_meta or "Meta rechazó la plantilla (revisa las variables y el formato)",
        )
    if e.status_code == 404:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plantilla no encontrada")
    if e.status_code == 403:
        # La clave del negocio no tiene el scope whatsapp_cloud_api: no es algo que el cliente arregle.
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "codigo": "scope_whatsapp_cloud_api_faltante",
                "mensaje": "La clave de NeuroAPI no tiene permiso para administrar plantillas",
            },
        )
    if e.status_code == 429:
        return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=e.mensaje)
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=e.mensaje)


def _lista(data: Any) -> list[dict[str, Any]]:
    """Saca la lista de plantillas de {"data": [...]}, {"data": {"templates": [...]}} o similar."""
    nodo = data
    for _ in range(3):
        if isinstance(nodo, list):
            return [x for x in nodo if isinstance(x, dict)]
        if not isinstance(nodo, dict):
            return []
        for clave in ("data", "templates"):
            if clave in nodo:
                nodo = nodo[clave]
                break
        else:
            return []
    return []


def _una(data: Any) -> dict[str, Any] | None:
    nodo = data.get("data") if isinstance(data, dict) and "data" in data else data
    if isinstance(nodo, list):
        nodo = nodo[0] if nodo else None
    if isinstance(nodo, dict) and isinstance(nodo.get("template"), dict):
        nodo = nodo["template"]
    return nodo if isinstance(nodo, dict) else None


class NeuroApiTemplateService:
    def __init__(self, api_key: str, base_url: str, waba_id: str | None = None) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self.waba_id = waba_id

    # --- construcción ---
    @classmethod
    async def desde_tenant(cls, tenant_id: UUID) -> "NeuroApiTemplateService":
        cred = await fetch_one(
            "SELECT * FROM get_channel_credentials($1, $2)", tenant_id, "whatsapp"
        )
        if cred is None:
            raise PlantillaApiError("El negocio no tiene un canal de WhatsApp activo")
        if cred["bsp_provider"] != "neuroapi":
            raise PlantillaApiError("El canal de WhatsApp del negocio no es de NeuroAPI")
        api_key = cred["access_token"] or settings.NEUROAPI_API_KEY
        if not api_key:
            raise PlantillaApiError("El negocio no tiene clave de NeuroAPI")
        return cls(api_key=api_key, base_url=settings.NEUROAPI_API_BASE_URL)

    @staticmethod
    async def proveedor_envio(tenant_id: UUID) -> NeuroApiProvider:
        """`NeuroApiProvider` de la línea activa del negocio, para los envíos de prueba."""
        cred = await fetch_one(
            "SELECT * FROM get_channel_credentials($1, $2)", tenant_id, "whatsapp"
        )
        if cred is None or cred["bsp_provider"] != "neuroapi":
            raise PlantillaApiError("El negocio no tiene una línea de WhatsApp de NeuroAPI activa")
        phone_number_id = cred["phone_number_id"] or ""
        api_key = cred["access_token"] or settings.NEUROAPI_API_KEY
        if not phone_number_id or not api_key:
            raise PlantillaApiError("La línea de WhatsApp del negocio está incompleta")
        return NeuroApiProvider(
            api_key=api_key,
            webhook_secret="",
            base_url=settings.NEUROAPI_API_BASE_URL,
            phone_number_id=phone_number_id,
        )

    # --- transporte ---
    async def _pedir(
        self,
        metodo: str,
        ruta: str = "",
        *,
        params: dict[str, Any] | None = None,
        cuerpo: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base_url}{RUTA}{ruta}"
        headers = {"x-api-key": self._api_key, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as cliente:
            try:
                r = await cliente.request(metodo, url, params=params, json=cuerpo, headers=headers)
            except httpx.RequestError as e:
                raise PlantillaApiError(f"No se pudo contactar a NeuroAPI: {type(e).__name__}") from e

        if r.status_code >= 400:
            log.warning("NeuroAPI %s %s -> %s: %s", metodo, RUTA, r.status_code, r.text[:300])
            raise PlantillaApiError(
                f"NeuroAPI respondió {r.status_code}",
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
        return _lista(await self._pedir("GET", params=params))

    async def obtener(self, nombre: str) -> dict[str, Any] | None:
        return _una(await self._pedir("GET", f"/{nombre}"))

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
        return _una(data) or {}

    async def crear_o_adoptar(
        self, *, nombre: str, idioma: str, categoria: str, componentes: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], bool]:
        """
        Crea la plantilla; si Meta dice que ya existe (spec 007: duplicado -> adoptar)
        devuelve la que ya hay. Resultado: (respuesta de Meta, `creada`). Con duplicado
        y sin poder consultarla, la respuesta va vacía y `creada` es False. Cualquier
        otro error se propaga tal cual.
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
        await self._pedir("DELETE", f"/{nombre}")
