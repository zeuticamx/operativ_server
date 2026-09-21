"""
Envío de WhatsApp, agnóstico de proveedor (patrón Adapter/Strategy).

Hoy el proveedor activo es Kontesta (`KontestaProvider`), porque la app de
Meta todavía no tiene Acceso Avanzado aprobado en App Review. `MetaProvider`
es el destino final: un stub vacío, listo para completarse el día que se
apruebe, sin que routers ni jobs tengan que cambiar una sola línea — solo
`WHATSAPP_PROVIDER=meta` en el entorno.

Todo el código de negocio debe depender únicamente de `ProveedorWhatsApp`
(vía `obtener_proveedor()`), nunca de `KontestaProvider` ni de `httpx`
directamente. Eso es lo que hace que cambiar de proveedor sea un cambio de
variable de entorno y no una migración de código.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import httpx
from fastapi import HTTPException, status

from config import settings

log = logging.getLogger("operativai.whatsapp")


# ============================================================
# Tipos comunes, sin forma propia de ningún proveedor
# ============================================================
@dataclass
class ResultadoEnvio:
    """Lo que le importa a quien llama, sin importar quién mandó el mensaje."""

    id_mensaje: str
    estado: str | None = None
    crudo: dict[str, Any] = field(default_factory=dict, repr=False)


class ProveedorWhatsAppError(Exception):
    """Error al hablar con la API del proveedor (HTTP, credenciales, etc)."""

    def __init__(self, mensaje: str, *, status_code: int | None = None):
        self.mensaje = mensaje
        self.status_code = status_code
        super().__init__(mensaje)


def a_http(e: ProveedorWhatsAppError) -> HTTPException:
    """Traduce un error de proveedor a una respuesta HTTP con sentido."""
    if e.status_code in (401, 403):
        codigo = status.HTTP_502_BAD_GATEWAY  # credencial nuestra, no del cliente
    elif e.status_code == 429:
        codigo = status.HTTP_429_TOO_MANY_REQUESTS
    else:
        codigo = status.HTTP_502_BAD_GATEWAY
    return HTTPException(status_code=codigo, detail=e.mensaje)


# ============================================================
# Interfaz común
# ============================================================
class ProveedorWhatsApp(ABC):
    """
    Contrato que cualquier proveedor de WhatsApp tiene que cumplir.

    Equivale al `IWhatsAppProvider` del patrón Adapter/Strategy: los
    nombres van en snake_case y sin el prefijo `I` para seguir la
    convención Python/PEP 8 del resto del backend, no una diferencia de
    diseño.
    """

    @abstractmethod
    async def enviar_mensaje(self, conversation_id: str, texto: str) -> ResultadoEnvio:
        """
        Manda texto libre dentro de una conversación ya abierta (ventana
        de 24h). `conversation_id` es el id que el proveedor asignó a esa
        conversación, no el teléfono del contacto.
        """
        ...

    @abstractmethod
    async def enviar_plantilla(
        self,
        destinatario: str,
        plantilla: str,
        idioma: str = "es",
        parametros: list[str] | None = None,
    ) -> ResultadoEnvio:
        """
        Inicia conversación fuera de la ventana de 24h con una plantilla
        aprobada. `destinatario` es el teléfono en formato E.164
        (ej. "+525512345678"), `plantilla` el nombre exacto registrado
        ante Meta, `parametros` los valores posicionales de sus variables.
        """
        ...

    @abstractmethod
    def verificar_webhook(self, cuerpo_crudo: bytes, headers: dict[str, str]) -> bool:
        """
        Valida la firma de un webhook entrante contra el secreto del
        proveedor. `cuerpo_crudo` tiene que ser exactamente los bytes que
        llegaron en el POST (antes de json.loads/request.json()): si se
        re-serializa el JSON para firmar, el HMAC no va a coincidir aunque
        el contenido sea "el mismo", porque el orden de llaves o los
        espacios pueden variar.
        """
        ...


# ============================================================
# Kontesta
# ============================================================
class KontestaProvider(ProveedorWhatsApp):
    """
    Implementación contra la API REST de Kontesta.

    api_key y webhook_secret se inyectan por parámetro (normalmente desde
    `settings`, ver `obtener_proveedor()` más abajo) y nunca se hardcodean.
    """

    def __init__(self, api_key: str, webhook_secret: str, base_url: str):
        self._api_key = api_key
        self._webhook_secret = webhook_secret
        self._base_url = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        # X-Api-Key es lo que documenta Kontesta; se manda también
        # Authorization: Bearer por si el endpoint lo prefiere — no hace
        # daño mandar ambas cabeceras de auth server-to-server.
        return {
            "X-Api-Key": self._api_key,
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, cuerpo: dict[str, Any]) -> dict[str, Any]:
        if not self._api_key:
            raise ProveedorWhatsAppError(
                "KONTESTA_API_KEY sin configurar", status_code=None
            )
        url = f"{self._base_url}{path}"
        async with httpx.AsyncClient(timeout=20) as cliente:
            try:
                r = await cliente.post(url, json=cuerpo, headers=self._headers())
            except httpx.RequestError as e:
                raise ProveedorWhatsAppError(f"No se pudo contactar a Kontesta: {e}") from e

        if r.status_code >= 400:
            log.warning("Kontesta %s -> %s: %s", path, r.status_code, r.text[:500])
            raise ProveedorWhatsAppError(
                f"Kontesta respondió {r.status_code}", status_code=r.status_code
            )
        try:
            return r.json()
        except ValueError:
            return {}

    async def enviar_mensaje(self, conversation_id: str, texto: str) -> ResultadoEnvio:
        data = await self._post(
            f"/conversations/{conversation_id}/messages",
            {"kind": "text", "text": texto},
        )
        return ResultadoEnvio(
            id_mensaje=str(data.get("id") or data.get("message_id") or ""),
            estado=data.get("status"),
            crudo=data,
        )

    async def enviar_plantilla(
        self,
        destinatario: str,
        plantilla: str,
        idioma: str = "es",
        parametros: list[str] | None = None,
    ) -> ResultadoEnvio:
        # ⚠️ El cuerpo exacto de /v1/messaging/send no vino especificado
        # (solo que "requiere una plantilla aprobada"). Se sigue la forma
        # estándar de WhatsApp Business (name/language/components) porque
        # es la que usan la mayoría de los BSP compatibles; hay que
        # confirmarla contra la documentación real de Kontesta antes de
        # mandar tráfico de producción.
        cuerpo: dict[str, Any] = {
            "to": destinatario,
            "type": "template",
            "template": {
                "name": plantilla,
                "language": {"code": idioma},
            },
        }
        if parametros:
            cuerpo["template"]["components"] = [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in parametros],
                }
            ]

        data = await self._post("/messaging/send", cuerpo)
        return ResultadoEnvio(
            id_mensaje=str(data.get("id") or data.get("message_id") or ""),
            estado=data.get("status"),
            crudo=data,
        )

    def verificar_webhook(self, cuerpo_crudo: bytes, headers: dict[str, str]) -> bool:
        """
        Firma esperada: cabecera `X-Kontesta-Signature` con la forma
        "t=<timestamp>,v1=<hmac>" (mismo formato que Stripe/Mercado Pago).

        ⚠️ El nombre exacto de la cabecera no vino especificado más que
        como "las cabeceras" de las que se extraen t y v1; confirmar contra
        la documentación real de Kontesta. Si el proveedor manda t y v1 en
        dos cabeceras separadas en vez de una combinada, ajustar solo este
        método — el resto del código no se entera.
        """
        if not self._webhook_secret:
            log.warning("KONTESTA_WEBHOOK_SECRET sin configurar: webhook rechazado")
            return False

        firma = headers.get("x-kontesta-signature") or headers.get("X-Kontesta-Signature")
        if not firma:
            return False

        partes: dict[str, str] = {}
        for trozo in firma.split(","):
            if "=" not in trozo:
                continue
            clave, _, valor = trozo.partition("=")
            partes[clave.strip()] = valor.strip()

        ts = partes.get("t")
        v1 = partes.get("v1")
        if not ts or not v1:
            return False

        # Ventana de tolerancia contra replay: un webhook con timestamp de
        # hace horas no debería aceptarse aunque la firma sea válida.
        try:
            if abs(time.time() - int(ts)) > 300:
                log.warning("Webhook de Kontesta con timestamp fuera de ventana (t=%s)", ts)
                return False
        except ValueError:
            return False

        # Es CRÍTICO firmar el cuerpo crudo tal cual llegó (bytes), nunca
        # un json.dumps() propio: cualquier diferencia de espacios u orden
        # de llaves cambia el HMAC aunque el contenido sea "el mismo".
        mensaje_firmado = ts.encode() + b"." + cuerpo_crudo
        esperado = hmac.new(
            self._webhook_secret.encode(), mensaje_firmado, hashlib.sha256
        ).hexdigest()

        # compare_digest y no ==: comparación en tiempo constante.
        return hmac.compare_digest(esperado, v1)


# ============================================================
# Meta (stub — destino final, hoy sin usar)
# ============================================================
class MetaProvider(ProveedorWhatsApp):
    """
    Placeholder para cuando la app de Meta tenga Acceso Avanzado aprobado
    y se migre de Kontesta a la Cloud API de Meta directamente.

    Implementación intencionalmente vacía: existe para que el resto del
    código (routers, jobs) ya pueda depender de `ProveedorWhatsApp` sin
    esperar a la migración. `services/meta.py` ya tiene el cliente HTTP de
    Graph API (OAuth, páginas, perfil de contacto); esta clase debería
    reusar ese mismo `httpx.AsyncClient` y `settings.graph_url` en vez de
    duplicar lógica de auth.
    """

    async def enviar_mensaje(self, conversation_id: str, texto: str) -> ResultadoEnvio:
        raise NotImplementedError("MetaProvider.enviar_mensaje: pendiente de migración")

    async def enviar_plantilla(
        self,
        destinatario: str,
        plantilla: str,
        idioma: str = "es",
        parametros: list[str] | None = None,
    ) -> ResultadoEnvio:
        raise NotImplementedError("MetaProvider.enviar_plantilla: pendiente de migración")

    def verificar_webhook(self, cuerpo_crudo: bytes, headers: dict[str, str]) -> bool:
        raise NotImplementedError("MetaProvider.verificar_webhook: pendiente de migración")


# ============================================================
# Inyección de dependencias: qué proveedor usar
# ============================================================
_PROVEEDORES = {
    "kontesta": lambda: KontestaProvider(
        api_key=settings.KONTESTA_API_KEY,
        webhook_secret=settings.KONTESTA_WEBHOOK_SECRET,
        base_url=settings.KONTESTA_API_BASE_URL,
    ),
    "meta": MetaProvider,
}


@lru_cache
def obtener_proveedor() -> ProveedorWhatsApp:
    """
    Instancia única del proveedor activo, elegido por `WHATSAPP_PROVIDER`.

    `@lru_cache` sin argumentos = se arma una sola vez por proceso, igual
    que `config.get_settings()`. Si `WHATSAPP_PROVIDER` trae un valor que
    no existe, falla al arrancar y no en medio del primer envío.
    """
    nombre = settings.WHATSAPP_PROVIDER
    fabrica = _PROVEEDORES.get(nombre)
    if fabrica is None:
        raise RuntimeError(
            f"WHATSAPP_PROVIDER={nombre!r} no es válido "
            f"(opciones: {', '.join(_PROVEEDORES)})"
        )
    return fabrica()
