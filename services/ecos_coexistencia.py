"""
Ecos del modo coexistencia: lo que una persona escribe desde la app de
WhatsApp Business del móvil, en una línea que también está en la API.

Meta los entrega en el mismo webhook que los mensajes de clientes, en otro
campo del sobre (`entry[].changes[]`):

  {"field": "smb_message_echoes",
   "value": {"metadata": {"display_phone_number": "...", "phone_number_id": "..."},
             "message_echoes": [{"from": "<negocio>", "to": "<cliente>",
                                 "id": "wamid...", "type": "text",
                                 "text": {"body": "..."}}]}}

(`message_echoes` dentro de `value` también se acepta con otro `field`.)

Se guardan en `messages` como `role='human'`, `desde_app_movil=true` y sin
remitente del portal: son solo lectura, nadie del portal los mandó. Esta
sincronización SÍ pasa la conversación a modo humano: una persona está
atendiendo desde el móvil, así que la IA se apaga (`status='transferred'`, sin
acuse al cliente). Devolverla a la IA es la acción de siempre del portal. Solo
un eco nuevo la pasa; el eco de lo que mandó nuestra API no.

Deduplicación (obligatoria: el eco de un mensaje que mandó nuestra API también
llega por aquí):
  1. Por `wamid`: el índice único (conversation_id, channel_msg_id) y una
     consulta previa. Cubre reintentos del proveedor y los mensajes cuyo wamid
     ya guardamos al enviarlos.
  2. Por contenido: un mensaje saliente reciente de la misma conversación, sin
     wamid y con el mismo texto, ES este eco. Se le completa el wamid y no se
     inserta otro. Hace falta porque el agente aún no guarda el wamid de lo que
     envía.

El modo real lo decide el usuario dentro del popup de Meta, no nuestra
pantalla: un eco es la prueba de que el número está en coexistencia. Si llega
a un número marcado `standard`, se promueve a `coexistence` y se procesa (si no,
las respuestas del móvil quedarían ocultas).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from session import conexion, execute, fetch_one

log = logging.getLogger("operativai.canales.ecos_coexistencia")

CAMPOS_ECO = ("smb_message_echoes", "message_echoes")

# Un saliente nuestro y su eco llegan con segundos de diferencia; 5 minutos
# cubre reintentos y colas sin confundir dos mensajes iguales muy separados.
VENTANA_MISMO_CONTENIDO_MIN = 5

_TEXTO_POR_TIPO = {
    "image": "[Imagen]",
    "video": "[Video]",
    "document": "[Documento]",
    "audio": "[Audio]",
    "sticker": "[Sticker]",
    "location": "[Ubicación]",
}

ResultadoEco = Literal["guardado", "duplicado", "ya_registrado", "ignorado_numero", "sin_cliente"]


@dataclass(frozen=True)
class EcoMensaje:
    """Un mensaje que el negocio escribió desde el móvil."""

    wamid: str
    # wa_id del cliente al que se le escribió (`to` en el eco).
    cliente: str
    contenido: str
    tipo: str


def _sobres(evento: dict[str, Any]) -> list[dict[str, Any]]:
    """Los `entry` del evento, en la raíz o dentro de `data` (formato propio de NeuroAPI)."""
    sobres: list[dict[str, Any]] = []
    candidatos = [evento]
    if isinstance(evento.get("data"), dict):
        candidatos.append(evento["data"])
    for c in candidatos:
        sobres.extend(e for e in c.get("entry") or [] if isinstance(e, dict))
    return sobres


def _cambios(evento: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        c
        for entrada in _sobres(evento)
        for c in entrada.get("changes") or []
        if isinstance(c, dict) and isinstance(c.get("value"), dict)
    ]


def hay_ecos(evento: dict[str, Any]) -> bool:
    """El evento trae al menos un campo de ecos (aunque venga vacío o ilegible)."""
    return any(
        c.get("field") in CAMPOS_ECO or "message_echoes" in c["value"] for c in _cambios(evento)
    )


def hay_mensajes_de_clientes(evento: dict[str, Any]) -> bool:
    """El mismo evento trae además mensajes entrantes que sí hay que reenviar al agente."""
    return any(c["value"].get("messages") for c in _cambios(evento))


def contenido_del_eco(item: dict[str, Any]) -> str:
    """Texto legible del eco: el cuerpo, la leyenda del adjunto o un marcador por tipo."""
    tipo = str(item.get("type") or "text")
    if tipo == "text":
        cuerpo = (item.get("text") or {}).get("body")
        if cuerpo:
            return str(cuerpo)
    bloque = item.get(tipo)
    if isinstance(bloque, dict):
        leyenda = bloque.get("caption") or (bloque.get("filename") if tipo == "document" else None)
        if leyenda:
            return str(leyenda)
    return _TEXTO_POR_TIPO.get(tipo, "[Mensaje sin contenido reconocible]")


def extraer_ecos(evento: dict[str, Any]) -> list[EcoMensaje]:
    """Los ecos válidos del evento (con wamid y cliente); el resto se descarta."""
    ecos: list[EcoMensaje] = []
    for cambio in _cambios(evento):
        valor = cambio["value"]
        if cambio.get("field") not in CAMPOS_ECO and "message_echoes" not in valor:
            continue
        items = valor.get("message_echoes") or valor.get("smb_message_echoes") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            wamid = str(item.get("id") or "").strip()
            cliente = str(item.get("to") or "").strip()
            if not wamid or not cliente:
                log.warning("Eco de coexistencia sin id o sin destinatario; se descarta")
                continue
            ecos.append(
                EcoMensaje(
                    wamid=wamid,
                    cliente=cliente,
                    contenido=contenido_del_eco(item),
                    tipo=str(item.get("type") or "text"),
                )
            )
    return ecos


async def procesar_ecos(evento: dict[str, Any], phone_number_id: str) -> list[ResultadoEco]:
    """
    Sincroniza los ecos del evento para el número `phone_number_id`. Nunca
    lanza por un eco individual: un fallo se registra y se sigue con el
    siguiente (el 200 al proveedor no depende de esto).
    """
    ecos = extraer_ecos(evento)
    if not ecos:
        return []

    numero = await fetch_one(
        """
        SELECT a.id, a.tenant_id, a.estado, a.connection_mode
          FROM resuelve_numero($1) r
          JOIN agentes_canal a ON a.id = r.agente_canal_id
         LIMIT 1
        """,
        phone_number_id,
    )
    if numero is None or numero["estado"] == "desvinculado":
        log.warning("Ecos de coexistencia de un número sin tenant activo (%s); se ignoran", phone_number_id)
        return ["ignorado_numero" for _ in ecos]
    if numero["connection_mode"] != "coexistence":
        log.warning(
            "Ecos en el número %s marcado como %s: está en coexistencia, se actualiza",
            phone_number_id, numero["connection_mode"],
        )
        await execute(
            "UPDATE agentes_canal SET connection_mode = 'coexistence' WHERE id = $1", numero["id"]
        )

    resultados: list[ResultadoEco] = []
    for eco in ecos:
        try:
            resultados.append(await _guardar_eco(numero["tenant_id"], numero["id"], eco))
        except Exception:
            log.exception("No se pudo sincronizar el eco %s (número %s)", eco.wamid, phone_number_id)
    return resultados


async def _guardar_eco(tenant_id: UUID, agente_canal_id: UUID, eco: EcoMensaje) -> ResultadoEco:
    # Import perezoso: services.agente arrastra el agente entero y este módulo
    # lo importa el router del webhook en el arranque.
    from services.agente import ingreso

    usuario = await fetch_one(
        """
        INSERT INTO users (tenant_id, whatsapp_id) VALUES ($1, $2)
        ON CONFLICT (tenant_id, whatsapp_id) DO UPDATE SET whatsapp_id = EXCLUDED.whatsapp_id
        RETURNING id
        """,
        tenant_id,
        eco.cliente,
    )
    if usuario is None:
        return "sin_cliente"
    conversacion = await ingreso._conversacion(tenant_id, usuario["id"], "whatsapp", agente_canal_id)
    conversation_id = conversacion["id"]

    async with conexion() as conn:
        async with conn.transaction():
            # Serializa ecos concurrentes de la misma conversación (reintentos).
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", str(conversation_id))

            if await conn.fetchval(
                "SELECT 1 FROM messages WHERE conversation_id = $1 AND channel_msg_id = $2",
                conversation_id,
                eco.wamid,
            ):
                return "duplicado"

            propio = await conn.fetchval(
                """
                SELECT id FROM messages
                 WHERE conversation_id = $1
                   AND channel_msg_id IS NULL
                   AND role IN ('assistant', 'human')
                   AND content = $2
                   AND created_at > NOW() - make_interval(mins => $3)
                 ORDER BY created_at DESC
                 LIMIT 1
                 FOR UPDATE
                """,
                conversation_id,
                eco.contenido,
                VENTANA_MISMO_CONTENIDO_MIN,
            )
            if propio is not None:
                await conn.execute(
                    "UPDATE messages SET channel_msg_id = $2 WHERE id = $1", propio, eco.wamid
                )
                return "ya_registrado"

            fila = await conn.fetchrow(
                """
                INSERT INTO messages
                    (conversation_id, tenant_id, role, content, channel_msg_id, desde_app_movil)
                VALUES ($1, $2, 'human', $3, $4, true)
                RETURNING id, role, content, created_at
                """,
                conversation_id,
                tenant_id,
                eco.contenido,
                eco.wamid,
            )
            # Modo humano: la IA calla mientras una persona atiende desde el móvil.
            # `ack_pendiente` en false evita el acuse "te atenderá una persona".
            await conn.execute(
                """
                UPDATE conversations
                   SET last_message_at = NOW(),
                       status = CASE WHEN status = 'active' THEN 'transferred' ELSE status END,
                       metadata = CASE WHEN status = 'active'
                                       THEN jsonb_set(COALESCE(metadata, '{}'::jsonb),
                                                      '{ack_pendiente}', 'false'::jsonb)
                                       ELSE metadata END
                 WHERE id = $1
                """,
                conversation_id,
            )

    await _avisar_en_vivo(tenant_id, conversation_id, fila)
    return "guardado"


async def _avisar_en_vivo(tenant_id: UUID, conversation_id: UUID, fila: Any) -> None:
    """Empuja el mensaje a quien tenga abierta la conversación; un fallo aquí no deshace el guardado."""
    try:
        from realtime import emit_mensaje
        from schemas import MensajeOut

        mensaje = MensajeOut(**dict(fila), enviado_por="App móvil de WhatsApp Business")
        await emit_mensaje(tenant_id, conversation_id, mensaje.model_dump(mode="json"))
    except Exception:
        log.exception("No se pudo emitir en vivo el eco de la conversación %s", conversation_id)
