"""
De cada sobre de canal a un `MensajeNormalizado`.

Cada plataforma manda su propio formato, y por la misma URL llegan cosas que
no son mensajes: acuses de entrega, reacciones, ecos del propio bot, avisos
de lectura. Todo eso se descarta acá con `procesar=False` y un motivo, que
es información para el log y no un error.

Port del nodo "Normalizar mensaje" de `entrada-canal-universal`, con los
arreglos que ese nodo fue acumulando: el filtro de ecos de WhatsApp (sin él
el bot se contestaba a sí mismo en bucle) y la distinción entre el id de la
cuenta de Instagram y el page_id contra el que hay que enviar.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from services.agente.modelos import MensajeNormalizado

log = logging.getLogger("operativai.agente.normalizar")


def _descartar(motivo: str) -> MensajeNormalizado:
    return MensajeNormalizado(procesar=False, motivo=motivo)


def aplanar_entradas(cuerpo: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Las `entry` del sobre. En Instagram y Messenger, NeuroAPI a veces las
    manda anidadas (`entry[].entry[].messaging[]`): se aplanan a la forma de
    Meta (`entry[].messaging[]`) para que el resto del parser no distinga.
    """
    planas: list[dict[str, Any]] = []
    for entrada in cuerpo.get("entry") or []:
        if not isinstance(entrada, dict):
            continue
        anidadas = [e for e in entrada.get("entry") or [] if isinstance(e, dict)]
        if anidadas and cuerpo.get("object") in ("instagram", "page"):
            planas.extend(anidadas)
        else:
            planas.append(entrada)
    return planas


def dividir_sobre(cuerpo: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Parte un sobre de Meta que agrupa varios eventos en un sobre por evento.

    Meta puede juntar varias `entry`, `changes`, `messages` o `messaging` en
    un mismo POST, y `normalizar` solo lee el primero de cada uno: sin esto
    los demás se perdían sin dejar rastro. Cada sobre devuelto trae un único
    mensaje y es válido para `normalizar`. Lo que no es de Meta (widget) o no
    trae nada que partir vuelve tal cual, en una lista de uno.
    """
    objeto = cuerpo.get("object")
    sobres: list[dict[str, Any]] = []

    for entry in aplanar_entradas(cuerpo):
        if objeto == "whatsapp_business_account":
            for cambio in entry.get("changes") or []:
                if not isinstance(cambio, dict):
                    continue
                valor = cambio.get("value") or {}
                mensajes = valor.get("messages") or []
                if not mensajes:
                    # Statuses u otra cosa: un sobre, para que normalizar lo
                    # descarte con su motivo.
                    sobres.append({**cuerpo, "entry": [{**entry, "changes": [cambio]}]})
                    continue
                # Los statuses que vinieran junto a mensajes se quitan: ya no
                # pueden tapar a los mensajes (ver `_whatsapp`).
                base = {k: v for k, v in valor.items() if k != "statuses"}
                for mensaje in mensajes:
                    nuevo = {**cambio, "value": {**base, "messages": [mensaje]}}
                    sobres.append({**cuerpo, "entry": [{**entry, "changes": [nuevo]}]})

        elif objeto in ("instagram", "page"):
            eventos = entry.get("messaging") or []
            if not eventos:
                sobres.append({**cuerpo, "entry": [entry]})
            for evento in eventos:
                sobres.append({**cuerpo, "entry": [{**entry, "messaging": [evento]}]})

        else:
            sobres.append({**cuerpo, "entry": [entry]})

    return sobres or [cuerpo]


def normalizar(
    cuerpo: dict[str, Any],
    *,
    tenant_id: UUID | None = None,
) -> MensajeNormalizado:
    """
    Punto de entrada: reparte por `object` y devuelve el mensaje desenvuelto.

    `tenant_id` llega resuelto cuando autenticó el proxy interno o el widget;
    en Meta directo viene None y lo resuelve `ingreso.py` por el
    identificador de la cuenta receptora.
    """
    objeto = cuerpo.get("object")

    if objeto == "web_widget":
        return _widget(cuerpo, tenant_id)

    if not objeto:
        return _descartar("Payload sin campo object")

    entradas = aplanar_entradas(cuerpo)
    if not entradas:
        return _descartar("Payload sin entry")
    entry = entradas[0]

    if objeto == "whatsapp_business_account":
        return _whatsapp(entry, tenant_id)
    if objeto == "instagram":
        return _instagram(entry, tenant_id)
    if objeto == "page":
        return _facebook(entry, tenant_id)

    return _descartar(f"Canal no reconocido: {objeto}")


# ============================================================
# Widget web
# ============================================================
def _widget(cuerpo: dict[str, Any], tenant_id: UUID | None) -> MensajeNormalizado:
    """
    El widget ya viene autenticado por `firma.verificar_widget`, que resolvió
    el tenant desde la `widget_key`. El cuerpo es nuestro, no de Meta, así
    que es plano.
    """
    if tenant_id is None:
        return _descartar("Widget: tenant no resuelto")
    if not cuerpo.get("contenido"):
        return _descartar("Widget: mensaje sin contenido")
    if not cuerpo.get("session_id"):
        return _descartar("Widget: falta session_id")

    return MensajeNormalizado(
        procesar=True,
        tenant_id=tenant_id,
        canal="web",
        usuario_id=str(cuerpo["session_id"]),
        nombre=cuerpo.get("nombre") or "Visitante web",
        # El widget no tiene id de mensaje del canal: no hay reintentos del
        # proveedor contra los que deduplicar.
        mensaje_id=None,
        contenido=str(cuerpo["contenido"]),
        tipo="text",
    )


# ============================================================
# WhatsApp
# ============================================================
# Cómo se resume cada tipo de mensaje para el historial y el modelo. El
# adjunto en sí lo guarda aparte services/adjuntos.py.
_TEXTO_POR_TIPO_WA = {
    "audio": "[Audio]",
    "sticker": "[Sticker]",
}

# Tipos que Meta manda por la misma vía pero que no son algo que el cliente
# haya escrito: `system` (cambio de número, etc.) y `unknown` (Meta no pudo
# entregar el contenido y adjunta `errors`).
_TIPOS_WA_SIN_MENSAJE = frozenset({"system", "unknown"})


def _whatsapp(entry: dict[str, Any], tenant_id: UUID | None) -> MensajeNormalizado:
    valor = ((entry.get("changes") or [{}])[0] or {}).get("value") or {}

    if valor.get("statuses") and not valor.get("messages"):
        return _descartar("Es un status de entrega, no un mensaje")

    mensajes = valor.get("messages") or []
    if not mensajes:
        return _descartar("WhatsApp: entry sin mensajes")
    mensaje = mensajes[0] or {}

    metadata = valor.get("metadata") or {}
    phone_number_id = metadata.get("phone_number_id")

    # Eco del propio número: pasa si la cuenta tiene suscrito
    # `message_echoes`. Sin este filtro el bot entra en bucle contestándose.
    propio = str(metadata.get("display_phone_number") or "")
    if propio:
        solo_digitos = "".join(c for c in propio if c.isdigit())
        if str(mensaje.get("from") or "") == solo_digitos:
            return _descartar("WhatsApp: eco del propio número")

    tipo = mensaje.get("type")
    contenido: str | None = None
    media_id: str | None = None

    if tipo == "text":
        contenido = (mensaje.get("text") or {}).get("body")
    elif tipo in ("image", "video"):
        bloque = mensaje.get(tipo) or {}
        contenido = bloque.get("caption") or f"[{'Imagen' if tipo == 'image' else 'Video'}]"
        media_id = bloque.get("id")
    elif tipo == "document":
        bloque = mensaje.get("document") or {}
        contenido = bloque.get("filename") or "[Documento]"
        media_id = bloque.get("id")
    elif tipo in _TEXTO_POR_TIPO_WA:
        contenido = _TEXTO_POR_TIPO_WA[tipo]
        media_id = (mensaje.get(tipo) or {}).get("id")
    elif tipo == "location":
        ubicacion = mensaje.get("location") or {}
        contenido = f"[Ubicacion: {ubicacion.get('latitude')}, {ubicacion.get('longitude')}]"
    elif tipo == "reaction":
        return _descartar("Es una reaccion, no un mensaje")
    elif tipo in _TIPOS_WA_SIN_MENSAJE:
        # Avisos de la plataforma (el cliente cambió de número) o fallos de
        # entrega de Meta: no los escribió el cliente, no toca contestarlos.
        return _descartar(f"WhatsApp: evento '{tipo}', no es un mensaje del cliente")
    elif tipo == "button":
        # Respuesta rápida de una plantilla: lo que el cliente eligió.
        bloque = mensaje.get("button") or {}
        contenido = bloque.get("text") or bloque.get("payload")
    elif tipo == "interactive":
        interactivo = mensaje.get("interactive") or {}
        elegido = interactivo.get(interactivo.get("type") or "") or {}
        contenido = elegido.get("title") or elegido.get("id")
    else:
        contenido = f"[Tipo no soportado: {tipo}]"
        tipo = "unsupported"

    if not contenido:
        return _descartar("Mensaje sin contenido")

    # Con varios contactos en el sobre, el del remitente de este mensaje.
    contactos = [c for c in (valor.get("contacts") or []) if isinstance(c, dict)]
    contacto = next(
        (c for c in contactos if c.get("wa_id") and c.get("wa_id") == mensaje.get("from")),
        contactos[0] if contactos else {},
    )

    return MensajeNormalizado(
        procesar=True,
        tenant_id=tenant_id,
        canal="whatsapp",
        usuario_id=mensaje.get("from"),
        nombre=(contacto.get("profile") or {}).get("name"),
        mensaje_id=mensaje.get("id"),
        contenido=contenido,
        tipo=tipo or "text",
        media_id=media_id,
        phone_number_id=phone_number_id,
    )


# ============================================================
# Instagram y Facebook Messenger
# ============================================================
def _mensajeria_meta(entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """
    Parte común de IG y FB: ambos usan el formato de Messenger.

    Devuelve (mensaje, motivo_de_descarte).
    """
    conversaciones = entry.get("messaging") or []
    if not conversaciones:
        return None, "entry sin messaging"
    msg = conversaciones[0] or {}

    mensaje = msg.get("message")
    if isinstance(mensaje, dict) and mensaje.get("is_echo"):
        return None, "Es un echo del propio bot"
    if isinstance(mensaje, dict) and mensaje.get("is_deleted"):
        return None, "El cliente borró el mensaje"
    if not mensaje:
        return None, "evento sin mensaje (read/delivery)"
    return msg, None


def _contenido_messenger(mensaje: dict[str, Any]) -> tuple[str, str, str | None]:
    """(contenido, tipo, media_url) de un mensaje de Messenger."""
    if mensaje.get("text"):
        return mensaje["text"], "text", None
    adjuntos = mensaje.get("attachments") or []
    if adjuntos:
        adjunto = adjuntos[0] or {}
        tipo = adjunto.get("type") or "unsupported"
        return f"[{tipo}]", tipo, (adjunto.get("payload") or {}).get("url")
    return "[Mensaje sin contenido reconocible]", "unsupported", None


def _media_id_instagram(mensaje: dict[str, Any]) -> str | None:
    """
    Id del primer adjunto, para bajarlo por el proxy de NeuroAPI
    (`/neuroapi/instagram/media/:mediaId`). El formato de NeuroAPI no está
    documentado: se acepta el id en el adjunto o en su `payload`. La URL de
    CDN de Meta que trae `payload.url` no se descarga nunca (SSRF).
    """
    adjuntos = mensaje.get("attachments") or []
    adjunto = adjuntos[0] if adjuntos and isinstance(adjuntos[0], dict) else {}
    payload = adjunto.get("payload") if isinstance(adjunto.get("payload"), dict) else {}
    for valor in (
        adjunto.get("media_id"),
        adjunto.get("id"),
        payload.get("media_id"),
        payload.get("attachment_id"),
        payload.get("id"),
    ):
        if valor not in (None, ""):
            return str(valor)
    return None


def _instagram(entry: dict[str, Any], tenant_id: UUID | None) -> MensajeNormalizado:
    msg, motivo = _mensajeria_meta(entry)
    if msg is None:
        return _descartar(f"Instagram: {motivo}")

    mensaje = msg.get("message") or {}
    contenido, tipo, media_url = _contenido_messenger(mensaje)

    return MensajeNormalizado(
        procesar=True,
        tenant_id=tenant_id,
        canal="instagram",
        usuario_id=(msg.get("sender") or {}).get("id"),
        mensaje_id=mensaje.get("mid"),
        contenido=contenido,
        tipo=tipo,
        media_id=_media_id_instagram(mensaje),
        media_url=media_url,
        # `entry.id` en Instagram es el id de la CUENTA, no el page_id: el
        # envío va contra la página vinculada, que se lee de
        # channel_credentials. Mezclarlos hacía fallar la respuesta.
        ig_account_id=entry.get("id") or (msg.get("recipient") or {}).get("id"),
    )


def _facebook(entry: dict[str, Any], tenant_id: UUID | None) -> MensajeNormalizado:
    msg, motivo = _mensajeria_meta(entry)
    if msg is None:
        return _descartar(f"Facebook: {motivo}")

    contenido, tipo, media_url = _contenido_messenger(msg.get("message") or {})

    return MensajeNormalizado(
        procesar=True,
        tenant_id=tenant_id,
        canal="facebook",
        usuario_id=(msg.get("sender") or {}).get("id"),
        mensaje_id=(msg.get("message") or {}).get("mid"),
        contenido=contenido,
        tipo=tipo,
        media_url=media_url,
        page_id=entry.get("id"),
    )
