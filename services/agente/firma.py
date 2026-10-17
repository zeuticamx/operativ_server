"""
Quién puede entrar por el webhook del agente.

Tres caminos, excluyentes, en el mismo orden que los decide el router:

  1. Proxy interno. El backend ya recibió el mensaje de NeuroAPI, validó su
     firma y resolvió el tenant (services/entrada_mensajes.py); lo marca con
     `X-Internal-Token` + `X-Tenant-Id`. Es el camino que usa WhatsApp hoy.
  2. Meta directo. Trae `X-Hub-Signature-256`: se recalcula el HMAC con el
     `app_secret` del remitente (cada tenant tiene el suyo) sobre los bytes
     tal cual llegaron.
  3. Widget web. Ni firma ni token: se identifica por `widget_key` y se
     exige que el `Origin` del navegador esté entre los dominios que el
     tenant registró.

Todo falla cerrado. Una variable sin configurar, un secreto que no aparece o
un cuerpo ilegible terminan en rechazo, nunca en "pasa porque no pude
comprobarlo".
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from config import settings
from session import fetch_one, fetch_value

log = logging.getLogger("operativai.agente.firma")


class FirmaInvalida(Exception):
    """
    La petición no se pudo autenticar. El router la traduce a 403.

    `motivo` es para el log, no para la respuesta: decirle a quien llama por
    qué exactamente falló su firma le sirve para afinar el siguiente intento.
    """

    def __init__(self, motivo: str):
        super().__init__(motivo)
        self.motivo = motivo


# ============================================================
# 1. Handshake de verificación de Meta (GET)
# ============================================================
def resolver_challenge(params: dict[str, str]) -> str:
    """
    Responde el `hub.challenge` de Meta al suscribir el webhook.

    Sin `META_VERIFY_TOKEN` configurado NO se verifica nada: se rechaza. En
    n8n este nodo tenía un valor de respaldo escrito en el código, así que el
    handshake seguía funcionando aunque faltara la variable — y ese valor
    quedaba publicado en el propio workflow (hallazgo 6 de la auditoría).
    """
    esperado = settings.META_VERIFY_TOKEN
    if not esperado:
        log.error("META_VERIFY_TOKEN sin configurar: se rechaza el handshake de Meta")
        raise FirmaInvalida("META_VERIFY_TOKEN no está configurado")

    recibido = params.get("hub.verify_token") or ""
    challenge = params.get("hub.challenge") or ""

    if params.get("hub.mode") != "subscribe" or not challenge:
        raise FirmaInvalida("Handshake incompleto")

    # compare_digest y no ==: el tiempo de una comparación byte a byte filtra
    # cuántos caracteres del prefijo se acertaron.
    if not secrets.compare_digest(recibido, esperado):
        raise FirmaInvalida("hub.verify_token no coincide")

    return challenge


# ============================================================
# 2. Proxy interno (NeuroAPI vía services/entrada_mensajes.py)
# ============================================================
def verificar_token_interno(cabeceras: dict[str, str]) -> UUID:
    """
    Autentica al propio backend reenviando un mensaje ya validado.

    Devuelve el tenant que el proxy resolvió. Ese tenant manda sobre el que
    se deduzca del número receptor: el proxy lo sacó de `channel_credentials`
    con la firma de NeuroAPI ya comprobada.
    """
    esperado = settings.N8N_INTERNAL_TOKEN
    if not esperado:
        raise FirmaInvalida("N8N_INTERNAL_TOKEN no está configurado")

    recibido = cabeceras.get("x-internal-token") or ""
    if not secrets.compare_digest(recibido, esperado):
        raise FirmaInvalida("Token interno no coincide")

    crudo = cabeceras.get("x-tenant-id") or ""
    if not crudo:
        raise FirmaInvalida("Falta X-Tenant-Id")
    try:
        return UUID(crudo)
    except ValueError:
        raise FirmaInvalida("X-Tenant-Id no es un UUID") from None


# ============================================================
# 3. Meta directo (X-Hub-Signature-256)
# ============================================================
def identificar_remitente(cuerpo: dict[str, Any]) -> tuple[str | None, str | None]:
    """
    Canal y cuenta receptora, para saber con qué `app_secret` firmar.

    En Instagram y Facebook el identificador es `entry[].id`; en WhatsApp es
    el `phone_number_id` de la metadata, no el número que escribe.
    """
    entry = (cuerpo.get("entry") or [{}])[0]
    objeto = cuerpo.get("object")

    if objeto == "whatsapp_business_account":
        valor = ((entry.get("changes") or [{}])[0] or {}).get("value") or {}
        return "whatsapp", (valor.get("metadata") or {}).get("phone_number_id")
    if objeto == "instagram":
        return "instagram", entry.get("id")
    if objeto == "page":
        return "facebook", entry.get("id")
    return None, None


async def verificar_firma_meta(cuerpo_crudo: bytes, cabeceras: dict[str, str]) -> dict[str, Any]:
    """
    Comprueba el HMAC de Meta y devuelve el cuerpo parseado.

    El secreto es por remitente (`obtiene_app_secret`), no uno global: cada
    tenant conecta su propia app de Meta. Sin secreto para ese remitente se
    rechaza — es el caso de un número que ya no es nuestro.
    """
    cabecera = cabeceras.get("x-hub-signature-256") or ""
    if not cabecera.startswith("sha256="):
        raise FirmaInvalida("Sin cabecera de firma")
    recibida = cabecera[len("sha256="):]

    # El cuerpo se parsea para saber de quién es, pero el HMAC se calcula
    # siempre sobre los bytes originales: cualquier reserialización cambia
    # espacios o el orden de las claves y la firma deja de coincidir.
    try:
        cuerpo = json.loads(cuerpo_crudo)
    except json.JSONDecodeError:
        raise FirmaInvalida("Cuerpo no es JSON válido") from None
    if not isinstance(cuerpo, dict):
        raise FirmaInvalida("Cuerpo no es un objeto JSON")

    canal, identificador = identificar_remitente(cuerpo)
    if not canal or not identificador:
        raise FirmaInvalida("No se pudo identificar al remitente")

    secreto = await fetch_value("SELECT obtiene_app_secret($1, $2)", canal, str(identificador))
    if not secreto:
        raise FirmaInvalida(f"Sin app_secret para el remitente {canal}")

    calculada = hmac.new(secreto.encode(), cuerpo_crudo, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculada.lower(), recibida.lower()):
        log.warning("Firma de Meta inválida para %s %s", canal, identificador)
        raise FirmaInvalida("La firma no coincide")

    return cuerpo


# ============================================================
# 4. Widget web
# ============================================================
@dataclass(frozen=True)
class WidgetAutorizado:
    tenant_id: UUID
    origen_permitido: str


def _origen(valor: str | None) -> str | None:
    """
    Deja un origen comparable: esquema + host + puerto, en minúsculas.

    Tolera que venga sin esquema ("cliente.com") asumiendo https, porque es
    como suele quedar guardado en `dominio_permitido`. Sin esta
    normalización "https://cliente.com" y "cliente.com" no coincidían y el
    tenant recibía un 403 teniendo la clave correcta.
    """
    if not valor:
        return None
    texto = str(valor).strip().rstrip("/")
    if not texto or texto == "*":
        return None
    if "//" not in texto:
        texto = f"https://{texto}"
    try:
        partes = urlparse(texto)
    except ValueError:
        return None
    if not partes.scheme or not partes.netloc:
        return None
    return f"{partes.scheme.lower()}://{partes.netloc.lower()}"


async def verificar_widget(cuerpo: dict[str, Any], cabeceras: dict[str, str]) -> WidgetAutorizado:
    """
    Resuelve el tenant por `widget_key` y exige que el Origin esté registrado.

    El comodín `*` en `dominio_permitido` solo se acepta si
    `WIDGET_COMODIN_PERMITIDO` está encendida. Venía de una prueba de
    desarrollo y dejaba el widget de ese tenant abierto a cualquier sitio
    (hallazgo 9 de la auditoría); apagada por defecto, un tenant con `*`
    guardado deja de responder y hay que corregirle el dominio.
    """
    widget_key = cuerpo.get("widget_key")
    if not widget_key:
        raise FirmaInvalida("Falta widget_key")

    fila = await fetch_one(
        """
        SELECT tenant_id, dominio_permitido
          FROM tenant_channels
         WHERE widget_key = $1 AND channel_type = 'web'
        """,
        str(widget_key),
    )
    if fila is None:
        raise FirmaInvalida("widget_key no reconocida")

    permitidos = [
        o for o in (_origen(d) for d in str(fila["dominio_permitido"] or "").split(",")) if o
    ]
    crudo = str(fila["dominio_permitido"] or "").strip()
    comodin = crudo == "*" or "*" in [d.strip() for d in crudo.split(",")]

    if comodin:
        if not settings.WIDGET_COMODIN_PERMITIDO:
            log.error(
                "El tenant %s tiene dominio_permitido='*': se rechaza. "
                "Registra su dominio real o enciende WIDGET_COMODIN_PERMITIDO en desarrollo.",
                fila["tenant_id"],
            )
            raise FirmaInvalida("El comodín de dominio no está permitido")
        recibido = _origen(cabeceras.get("origin"))
        return WidgetAutorizado(fila["tenant_id"], recibido or "*")

    if not permitidos:
        raise FirmaInvalida("El tenant no tiene dominios registrados para el widget")

    recibido = _origen(cabeceras.get("origin"))
    if not recibido or recibido not in permitidos:
        log.warning(
            "Widget del tenant %s: origen %s no autorizado",
            fila["tenant_id"], recibido or "(sin Origin)",
        )
        raise FirmaInvalida("Origen no autorizado")

    return WidgetAutorizado(fila["tenant_id"], recibido)
