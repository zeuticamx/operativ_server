"""
De un mensaje autenticado a un contexto listo para contestar.

Orquesta todo lo que pasa antes de que el modelo vea nada:

    resolver tenant → ¿baja o alta? → revisar acceso → persistir
    → deduplicar → ¿está en manos de una persona? → agrupar la ráfaga

Port de la primera mitad de `entrada-canal-universal`, con dos cambios
deliberados respecto del workflow:

  - El permiso para contestar lo decide `eventos.on_mensaje_entrante`, que
    ya existía y que n8n no llamaba: tenía su propia consulta a
    tenant_subscriptions con la misma regla escrita a mano. Al unificarlo,
    un mensaje que llega con el agente bloqueado ahora SÍ entra al embudo y
    le avisa al vendedor, que es justo lo que esa función hace cuando el
    agente no va a responder.
  - La ventana antiflood se resuelve contra la base, no en memoria: si el
    proceso se reinicia a mitad de la espera, el mensaje ya está guardado y
    la siguiente ráfaga lo arrastra. No depende de que haya un solo worker.

Y una etapa que n8n no tenía: BAJA / ALTA (hallazgo 1, ver `bajas.py`). Va
ANTES de revisar el acceso, y eso es deliberado: una baja hay que apuntarla
aunque el negocio tenga el agente apagado o el plan agotado. Es una
obligación con el cliente, no un servicio del plan.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from config import settings
from services import conversaciones_ocultas, instagram_neuroapi, numeros_whatsapp, perfil_contacto
from services.agente import bajas
from services.agente import normalizar as normalizar_mod
from services.agente import prompt
from services.agente.modelos import (
    COLUMNA_POR_CANAL,
    ContextoMensaje,
    Destino,
    MensajeNormalizado,
    PeticionVerificada,
    ResultadoIngreso,
)
from session import conexion, execute, fetch_all, fetch_one, fetch_value

log = logging.getLogger("operativai.agente.ingreso")

# Lo que se le contesta a un cliente cuyo negocio no puede usar el agente
# (plan sin IA, cuota agotada, suscripción vencida). Neutro a propósito: el
# cliente no tiene por qué enterarse del estado comercial del negocio.
AVISO_SIN_AGENTE = "Gracias por tu mensaje, en breve te atenderemos."

# Se manda una sola vez por escalamiento, y no lo genera el modelo: una
# conversación ya transferida no debe recibir texto de la IA.
ACUSE_TRANSFERENCIA = (
    "Ya recibimos tu mensaje. En breve te atiende alguien de nuestro equipo."
)


async def procesar(peticion: PeticionVerificada) -> ResultadoIngreso:
    """
    Punto de entrada del paquete. Devuelve qué hacer con esta petición.

    Nunca levanta por un mensaje que no toca contestar: eso se informa con
    `ResultadoIngreso(motivo=...)` y el router responde 200. Un 500 acá hace
    que el proveedor reintente un mensaje que ya guardamos.
    """
    mensaje = normalizar_mod.normalizar(peticion.cuerpo, tenant_id=peticion.tenant_id)
    if not mensaje.procesar:
        return ResultadoIngreso(motivo=mensaje.motivo)

    tenant_id = await _resolver_tenant(mensaje, peticion)
    if tenant_id is None:
        log.warning(
            "Canal %s con identificador %s sin tenant registrado",
            mensaje.canal,
            mensaje.phone_number_id or mensaje.page_id or mensaje.ig_account_id,
        )
        return ResultadoIngreso(motivo="Tenant no registrado")

    user_id = await _upsert_usuario(tenant_id, mensaje)

    # Spec 005a: para WhatsApp, resolver el número por el que entró y decidir
    # si el agente responde (RF-7, RF-12, RF-16). Instagram, Facebook y web
    # usan la configuración social del negocio (sin agente_canal_id).
    agente_canal_id: Optional[UUID] = None
    config: dict[str, Any] | None = None
    ahora = datetime.now(timezone.utc)
    if mensaje.canal == "whatsapp" and mensaje.phone_number_id:
        entrada = await numeros_whatsapp.resolver_entrada(
            mensaje.phone_number_id, ahora=ahora
        )
        if entrada.numero is None:
            return ResultadoIngreso(motivo="Número no vinculado")
        agente_canal_id = entrada.numero["id"]
        if entrada.motivo == "numero_desvinculado":
            async with conexion() as conn:
                conversacion_id = await conversaciones_ocultas.conversacion_para_mensaje_oculto(
                    conn, entrada.numero["tenant_id"], agente_canal_id, user_id, ahora
                )
            guardado = await _guardar_mensaje(tenant_id, conversacion_id, mensaje)
            if guardado is None:
                return ResultadoIngreso(motivo="Mensaje duplicado")
            await execute(
                "UPDATE conversations SET esperando_reapertura = false, last_message_at = NOW() WHERE id = $1",
                conversacion_id,
            )
            return ResultadoIngreso(
                motivo="Número desvinculado: mensaje guardado sin respuesta"
            )
        if entrada.motivo in ("numero_no_activo", "configuracion_incompleta"):
            conversacion = await _conversacion(
                tenant_id, user_id, mensaje.canal, agente_canal_id
            )
            guardado = await _guardar_mensaje(tenant_id, conversacion["id"], mensaje)
            if guardado is None:
                return ResultadoIngreso(motivo="Mensaje duplicado")
            await execute(
                "UPDATE conversations SET esperando_reapertura = false, last_message_at = NOW() WHERE id = $1",
                conversacion["id"],
            )
            return ResultadoIngreso(
                motivo=f"El número no responde ({entrada.motivo})"
            )

    # Configuración de agente del número o del social (spec 005a, RF-7).
    if agente_canal_id is not None:
        config = await prompt.config_numero(agente_canal_id)
    else:
        config = await prompt.config_social(tenant_id)

    # Antes de mirar el plan: una baja se apunta siempre. Un negocio con el
    # agente apagado sigue teniendo que dejar de escribirle a quien lo pidió.
    tramitado = await _tramitar_baja_o_alta(tenant_id, user_id, mensaje)
    if tramitado is not None:
        return tramitado

    # Import acá dentro y no arriba: `on_mensaje_entrante` es lógica de
    # negocio que hoy vive en routers/eventos.py, y en este repo los
    # servicios no importan routers (sería el único caso). Diferirlo evita
    # el ciclo; el arreglo de fondo es mudar esa función a un servicio, que
    # toca código ya probado y no corresponde a esta migración.
    from routers.eventos import on_mensaje_entrante

    # Acceso y embudo en la misma llamada. Devuelve el `agente_ia_activo`
    # efectivo: incluye el bloqueo por falta de pago, que viaja disfrazado de
    # "agente apagado" (ver el docstring de on_mensaje_entrante).
    estado = await on_mensaje_entrante(
        tenant_id,
        user_id,
        {"canal": mensaje.canal, "contenido": mensaje.contenido, "tipo": mensaje.tipo},
    )
    if not estado.agente_ia_activo:
        await _registrar_bloqueo(tenant_id, mensaje)
        return ResultadoIngreso(
            respuesta_inmediata=AVISO_SIN_AGENTE,
            # Sin conversación: si el tenant no puede usar el agente no se
            # abre hilo, pero al cliente hay que contestarle igual.
            destino=Destino(
                tenant_id=tenant_id,
                canal=mensaje.canal,
                usuario_id=mensaje.usuario_id,
                nombre=mensaje.nombre,
                phone_number_id=mensaje.phone_number_id,
                page_id=mensaje.page_id,
            ),
            motivo="Agente no disponible para este tenant",
        )

    conversacion = await _conversacion(tenant_id, user_id, mensaje.canal)
    await _completar_nombre(tenant_id, user_id, mensaje)
    guardado = await _guardar_mensaje(tenant_id, conversacion["id"], mensaje)
    if guardado is None:
        # El proveedor reintentó un mensaje que ya teníamos. El ON CONFLICT
        # no devolvió fila, así que no hay nada nuevo que contestar.
        return ResultadoIngreso(motivo="Mensaje duplicado")

    # El cliente volvió a escribir: si estábamos esperando que reabriera la
    # ventana de 24 h con una plantilla, ya no hace falta.
    await execute(
        "UPDATE conversations SET esperando_reapertura = false, last_message_at = NOW() WHERE id = $1",
        conversacion["id"],
    )

    # Adjunto de un DM de Instagram por NeuroAPI: se baja por su proxy y queda
    # asociado al mensaje para el portal. Si falla, el mensaje sigue con su
    # texto ("[image]"); no frena la respuesta.
    if mensaje.canal == "instagram" and mensaje.media_id and peticion.proveedor == "neuroapi":
        await instagram_neuroapi.guardar_adjunto_entrante(
            tenant_id, guardado["id"], mensaje.media_id
        )

    contexto = ContextoMensaje(
        tenant_id=tenant_id,
        conversation_id=conversacion["id"],
        user_id=user_id,
        canal=mensaje.canal,
        usuario_id=mensaje.usuario_id,
        nombre=mensaje.nombre,
        contenido=mensaje.contenido,
        tipo=mensaje.tipo,
        mensaje_id=mensaje.mensaje_id,
        db_message_id=guardado["id"],
        db_message_created_at=guardado["created_at"],
        phone_number_id=mensaje.phone_number_id,
        page_id=mensaje.page_id,
        agente_canal_id=agente_canal_id,
        config=config,
        conversation_status=conversacion["status"],
        conversation_metadata=conversacion["metadata"] or {},
        ids_agrupados=[guardado["id"]],
    )

    if contexto.transferida:
        return await _conversacion_transferida(contexto)

    # El widget contesta en la misma petición: hacer esperar al navegador
    # tres segundos para agrupar no tiene sentido ahí.
    if not contexto.es_web:
        if not await _esperar_rafaga(contexto):
            return ResultadoIngreso(motivo="Llegó un mensaje más nuevo; lo atiende esa pasada")

    return ResultadoIngreso(contexto=contexto)


# ============================================================
# Tenant
# ============================================================
async def _resolver_tenant(
    mensaje: MensajeNormalizado, peticion: PeticionVerificada
) -> UUID | None:
    """
    El tenant del proxy interno manda sobre el que deduzca `resuelve_tenant`.

    El proxy lo sacó de channel_credentials con la firma de NeuroAPI ya
    validada, así que es la fuente más confiable. Cuando los dos opinan
    distinto se deja constancia: significa que el mismo número está
    registrado dos veces.
    """
    if peticion.tenant_id is not None:
        return peticion.tenant_id
    if mensaje.tenant_id is not None:
        return mensaje.tenant_id

    identificador = mensaje.phone_number_id or mensaje.page_id or mensaje.ig_account_id
    if not identificador:
        return None

    return await fetch_value(
        "SELECT resuelve_tenant($1, $2)", mensaje.canal, str(identificador)
    )


# ============================================================
# Baja y alta (hallazgo 1)
# ============================================================
async def _tramitar_baja_o_alta(
    tenant_id: UUID, user_id: UUID, mensaje: MensajeNormalizado
) -> ResultadoIngreso | None:
    """
    Atiende "BAJA" y "ALTA". None si el mensaje no era ninguna de las dos y
    sigue su camino normal.

    El mensaje se guarda igual, con su conversación: el negocio tiene que ver
    en el historial que su cliente pidió la baja, o se queda sin saber por
    qué el agente dejó de poder escribirle. Reusa `_guardar_mensaje`, así que
    un reintento del proveedor no manda el acuse dos veces.

    Un ALTA de alguien que no estaba de baja NO se trata como alta: devuelve
    None y lo contesta el agente. Si no, la palabra "alta" suelta en una
    conversación cualquiera se comería el turno con un acuse que no viene a
    cuento.
    """
    palabra_baja = bajas.palabra_de_baja(mensaje.contenido)
    palabra_alta = None if palabra_baja else bajas.palabra_de_alta(mensaje.contenido)
    if not palabra_baja and not palabra_alta:
        return None

    if palabra_alta and not await bajas.quitar(
        tenant_id, mensaje.canal, mensaje.usuario_id
    ):
        return None

    if palabra_baja:
        await bajas.registrar(
            tenant_id, mensaje.canal, mensaje.usuario_id, palabra_baja
        )

    acuse = bajas.ACUSE_BAJA if palabra_baja else bajas.ACUSE_ALTA
    conversacion = await _conversacion(tenant_id, user_id, mensaje.canal)
    guardado = await _guardar_mensaje(tenant_id, conversacion["id"], mensaje)
    if guardado is None:
        # Reintento del proveedor: la baja ya estaba apuntada (es idempotente)
        # y el acuse ya salió en la primera pasada.
        return ResultadoIngreso(motivo="Petición de baja/alta duplicada")

    await execute(
        """
        INSERT INTO messages (conversation_id, tenant_id, role, content)
        VALUES ($1, $2, 'assistant', $3)
        """,
        conversacion["id"],
        tenant_id,
        acuse,
    )
    # La baja apaga `esperando_reapertura` por el mismo motivo que un mensaje
    # normal: el cliente escribió, ya no hay ventana que reabrir. Y si estaba
    # encendida, dejarla puesta haría que el envío intentara una plantilla que
    # ahora tiene prohibida.
    await execute(
        """
        UPDATE conversations
           SET esperando_reapertura = false, last_message_at = NOW()
         WHERE id = $1
        """,
        conversacion["id"],
    )

    return ResultadoIngreso(
        respuesta_inmediata=acuse,
        destino=Destino(
            tenant_id=tenant_id,
            canal=mensaje.canal,
            usuario_id=mensaje.usuario_id,
            nombre=mensaje.nombre,
            phone_number_id=mensaje.phone_number_id,
            page_id=mensaje.page_id,
            conversation_id=conversacion["id"],
        ),
        motivo="Baja registrada" if palabra_baja else "Baja levantada",
    )


# ============================================================
# Persistencia
# ============================================================
async def _upsert_usuario(tenant_id: UUID, mensaje: MensajeNormalizado) -> UUID:
    """
    Busca o crea al cliente por su identificador en el canal.

    La columna se interpola en el SQL, pero sale de `COLUMNA_POR_CANAL`, que
    es un diccionario cerrado en el código: el canal ya fue validado por el
    normalizador y nunca llega texto del cliente hasta acá.
    """
    columna = COLUMNA_POR_CANAL.get(mensaje.canal or "")
    if columna is None:
        raise ValueError(f"Canal sin columna definida: {mensaje.canal}")

    fila = await fetch_one(
        f"""
        INSERT INTO users (tenant_id, {columna}, display_name)
        VALUES ($1, $2, NULLIF(NULLIF($3, ''), 'null'))
        ON CONFLICT (tenant_id, {columna})
        DO UPDATE SET display_name = COALESCE(
            NULLIF(NULLIF(EXCLUDED.display_name, ''), 'null'), users.display_name
        )
        RETURNING id
        """,
        tenant_id,
        mensaje.usuario_id,
        mensaje.nombre,
    )
    return fila["id"]


async def _completar_nombre(
    tenant_id: UUID, user_id: UUID, mensaje: MensajeNormalizado
) -> None:
    """
    Instagram y Facebook solo mandan un id numérico: si el cliente sigue sin
    nombre, se le pregunta a Meta en segundo plano (sin frenar la respuesta).
    """
    if mensaje.canal not in ("instagram", "facebook") or not mensaje.usuario_id:
        return
    nombre = await fetch_value("SELECT display_name FROM users WHERE id = $1", user_id)
    if (nombre or "").strip():
        return
    perfil_contacto.pedir_en_segundo_plano(
        tenant_id, user_id, mensaje.canal, str(mensaje.usuario_id)
    )


async def _conversacion(
    tenant_id: UUID, user_id: UUID, canal: str, agente_canal_id: Optional[UUID] = None
) -> dict[str, Any]:
    """
    La conversación viva del cliente en ese canal, o una nueva.

    Se reutiliza también la que está en estado 'transferred': un cliente que
    sigue escribiendo mientras lo atiende una persona no abre un hilo nuevo.

    `agente_canal_id` es parte de la clave: dos números del mismo negocio y
    mismo cliente son conversaciones distintas (spec 005a, RF-13).
    """
    return await fetch_one(
        """
        WITH existente AS (
            SELECT id, status, metadata
              FROM conversations
             WHERE tenant_id = $1 AND user_id = $2 AND channel_type = $3
               AND agente_canal_id IS NOT DISTINCT FROM $4
               AND status IN ('active', 'transferred')
             ORDER BY last_message_at DESC
             LIMIT 1
        ),
        nueva AS (
            INSERT INTO conversations (tenant_id, user_id, channel_type, status, agente_canal_id)
            SELECT $1, $2, $3, 'active', $4
             WHERE NOT EXISTS (SELECT 1 FROM existente)
            RETURNING id, status, metadata
        )
        SELECT id, status, metadata FROM existente
        UNION ALL
        SELECT id, status, metadata FROM nueva
        LIMIT 1
        """,
        tenant_id,
        user_id,
        canal,
        agente_canal_id,
    )


async def _guardar_mensaje(
    tenant_id: UUID, conversation_id: UUID, mensaje: MensajeNormalizado
) -> dict[str, Any] | None:
    """
    Guarda el mensaje del cliente. None si era un reintento del proveedor.

    La deduplicación la da el índice único sobre (conversation_id,
    channel_msg_id): sin fila devuelta, ese wamid ya estaba guardado. El
    widget no tiene id de canal, así que ahí nunca hay conflicto.
    """
    return await fetch_one(
        """
        INSERT INTO messages (conversation_id, tenant_id, role, content, channel_msg_id)
        VALUES ($1, $2, 'user', $3, NULLIF(NULLIF($4, ''), 'null'))
        ON CONFLICT (conversation_id, channel_msg_id)
            WHERE channel_msg_id IS NOT NULL
        DO NOTHING
        RETURNING id, created_at
        """,
        conversation_id,
        tenant_id,
        str(mensaje.contenido or ""),
        str(mensaje.mensaje_id or ""),
    )


async def _registrar_bloqueo(tenant_id: UUID, mensaje: MensajeNormalizado) -> None:
    """Deja el intento en plan_limit_events, para que gerencia lo vea."""
    await execute(
        """
        INSERT INTO plan_limit_events (tenant_id, canal, usuario_id, mensaje_id)
        VALUES ($1, $2, $3, NULLIF(NULLIF($4, ''), 'null'))
        """,
        tenant_id,
        mensaje.canal,
        mensaje.usuario_id,
        str(mensaje.mensaje_id or ""),
    )


# ============================================================
# Conversación en manos de una persona
# ============================================================
async def _conversacion_transferida(contexto: ContextoMensaje) -> ResultadoIngreso:
    """
    El bot calla: el mensaje queda guardado y se avisa a quien la atiende.

    El acuse sale una sola vez por escalamiento, controlado por la bandera
    `ack_pendiente` del metadata. A partir del segundo mensaje el cliente ya
    sabe que lo van a atender y repetírselo molesta.
    """
    await _avisar_asignado(contexto)

    if not contexto.conversation_metadata.get("ack_pendiente"):
        return ResultadoIngreso(motivo="Conversación transferida: registrado en silencio")

    await execute(
        """
        INSERT INTO messages (conversation_id, tenant_id, role, content)
        VALUES ($1, $2, 'assistant', $3)
        """,
        contexto.conversation_id,
        contexto.tenant_id,
        ACUSE_TRANSFERENCIA,
    )
    await execute(
        """
        UPDATE conversations
           SET last_message_at = NOW(),
               metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), '{ack_pendiente}', 'false'::jsonb)
         WHERE id = $1
        """,
        contexto.conversation_id,
    )
    return ResultadoIngreso(
        respuesta_inmediata=ACUSE_TRANSFERENCIA,
        destino=contexto.destino,
        motivo="Conversación transferida: acuse enviado",
    )


async def _avisar_asignado(contexto: ContextoMensaje) -> None:
    """
    Avisa a quien tiene la conversación. Llamada directa, sin pasar por HTTP:
    en n8n esto era un POST a /api/eventos/mensaje-conversacion-asignada
    contra este mismo proceso.
    """
    from routers.eventos import mensaje_conversacion_asignada
    from schemas import MensajeConversacionAsignadaIn

    try:
        await mensaje_conversacion_asignada(
            MensajeConversacionAsignadaIn(
                tenant_id=contexto.tenant_id,
                conversation_id=contexto.conversation_id,
                cliente_nombre=contexto.nombre,
                texto=contexto.contenido,
            )
        )
    except Exception:
        # Un aviso que no sale no puede tumbar el registro del mensaje.
        log.exception(
            "No se pudo avisar al asignado de la conversación %s", contexto.conversation_id
        )


# ============================================================
# Antiflood: agrupar la ráfaga
# ============================================================
async def _esperar_rafaga(contexto: ContextoMensaje) -> bool:
    """
    Espera un momento y decide si a esta pasada le toca contestar.

    Un cliente que escribe "hola", "quería preguntar", "sobre un corte"
    dispara tres entregas del proveedor. Sin esto el agente contestaría tres
    veces. La regla: si mientras esperábamos entró un mensaje más nuevo,
    esta pasada se calla y contesta la de aquel, que ya verá los tres.

    Devuelve False si hay que callarse. Si contesta, deja agrupados en
    `contexto.contenido` todos los mensajes pendientes y los marca en la
    base para que no se vuelvan a arrastrar.
    """
    await asyncio.sleep(settings.AGENTE_ANTIFLOOD_SEGUNDOS)

    hay_mas_nuevo = await fetch_one(
        """
        SELECT EXISTS (
            SELECT 1 FROM messages
             WHERE conversation_id = $1 AND role = 'user' AND created_at > $2
        ) AS hay_mas_nuevo
        """,
        contexto.conversation_id,
        contexto.db_message_created_at,
    )
    if hay_mas_nuevo["hay_mas_nuevo"]:
        return False

    pendientes = await fetch_all(
        """
        SELECT id, content
          FROM messages
         WHERE conversation_id = $1 AND role = 'user' AND agrupado = false
         ORDER BY created_at ASC
        """,
        contexto.conversation_id,
    )
    if pendientes:
        contexto.contenido = "\n".join(f["content"] for f in pendientes)
        contexto.ids_agrupados = [f["id"] for f in pendientes]

    await execute(
        "UPDATE messages SET agrupado = true WHERE id = ANY($1::uuid[])",
        contexto.ids_agrupados,
    )
    return True
