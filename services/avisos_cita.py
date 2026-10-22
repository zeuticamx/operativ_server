"""
Registro y envío de los avisos de cita por WhatsApp (spec 004, T4).

Principio D1: el registro del intento (`en_curso`) se escribe ANTES de llamar
al proveedor y se cierra DESPUÉS. La unicidad (reserva, tipo, hora_inicio) la
pone la BD (`avisos_cita_clave_unica`): quien no consigue insertar no envía
(CL-5), y no se depende de que el planificador tenga una sola instancia.

Si el proceso muere entre la llamada y el cierre, el registro queda `en_curso`
de una ejecución sin latido: `recuperar_huerfanos` lo pasa a `fallido` con
`resultado_desconocido`, sin reenviar (CL-4). Un mensaje que sí llegó puede
quedar como fallido; se asume a cambio de no duplicar.

El evento de contexto del agente solo se crea tras un cierre `enviado` (RF-14)
y solo con conversación (RF-15). Aquí no se escribe en tablas de n8n.

Las decisiones de motivo (teléfono, canal, bandera, plantilla) las toman
`avisos_cita_reglas` y quien llama (cancelación en T6, job en T7); este módulo
registra, envía y cierra.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from config import settings
from services import avisos_cita_config, avisos_cita_envio, contexto_agente, plantillas_tenant
from services.avisos_cita_reglas import (
    CITA_NO_CONFIRMADA,
    DESACTIVADO,
    ESPERANDO_APROBACION,
    HORAS_VENTANA,
    PLANTILLA_NO_APROBADA,
    SIN_NUMERO_EMISOR,
    TELEFONO_INVALIDO,
    canal_soportado,
    clasificar_telefono,
    ejecucion_viva,
    fecha_mostrada,
    motivo_cancelacion,
    motivo_recordatorio,
    parametros_de_envio,
    se_registra,
)
from services.pipeline import get_tenant_servicios
from session import execute, fetch_all, fetch_one, fetch_value

log = logging.getLogger("operativai.avisos_cita")

TIPO_CANCELACION = "cancelacion"
TIPO_RECORDATORIO = "recordatorio"

ESTADO_EN_CURSO = "en_curso"
ESTADO_ENVIADO = "enviado"
ESTADO_OMITIDO = "omitido"
ESTADO_FALLIDO = "fallido"

# Motivos que solo aparecen al llamar al proveedor o al recuperar huérfanos (los de envío
# viven en `avisos_cita_envio`, que no importa este módulo).
ERROR_PROVEEDOR = avisos_cita_envio.ERROR_PROVEEDOR
PLANTILLA_NO_DISPONIBLE = avisos_cita_envio.PLANTILLA_NO_DISPONIBLE
RESULTADO_DESCONOCIDO = "resultado_desconocido"

TIPO_ALERTA_GERENCIA = "aviso_cita_no_enviado"

# Motivos de una cancelación que se avisan al owner (RF-7, RF-8; spec 007: texto del negocio,
# una variante por motivo). Quedan fuera `canal_no_soportado` y `desactivado_por_configuracion`
# (sin aviso por spec).
_MENSAJE_GERENCIA = {
    "sin_telefono": "la cita no tiene teléfono del cliente.",
    "telefono_invalido": "el teléfono de la cita no es válido.",
    PLANTILLA_NO_APROBADA: "la plantilla de cancelación de tu negocio no está aprobada.",
    SIN_NUMERO_EMISOR: (
        "tu negocio no tiene un número principal de WhatsApp activo con el que enviar el aviso."
    ),
    ERROR_PROVEEDOR: "el envío por WhatsApp falló.",
    PLANTILLA_NO_DISPONIBLE: "WhatsApp no pudo usar la plantilla de cancelación de tu negocio.",
    RESULTADO_DESCONOCIDO: (
        "no se pudo confirmar si el mensaje salió (el proceso se reinició durante el envío)."
    ),
}


@dataclass(frozen=True)
class ResultadoAviso:
    """
    Resultado de un intento. `estado` None significa que la clave ya existía y
    este proceso no envió ni creó registro (CL-5).
    """

    aviso_id: UUID | None
    estado: str | None
    motivo: str | None = None
    evento_creado: bool = False

    @property
    def enviado(self) -> bool:
        return self.estado == ESTADO_ENVIADO


@dataclass(frozen=True)
class EstadoAviso:
    """
    Lo que decide un aviso de cita del negocio (spec 007): `habilitado` = calendario
    activo y función del tipo encendida (D7: si no, ni se envía ni se registra);
    nombre vigente de la plantilla, su estado en la caché local (None = ausente) y si
    hay número emisor coincidente (D6).
    """

    habilitado: bool
    nombre_plantilla: str
    plantilla_estado: str | None
    emisor_ok: bool


async def _estado_aviso(
    tenant_id: UUID, tipo: str, *, acceso: avisos_cita_config.Acceso | None = None
) -> EstadoAviso:
    """
    Lee la configuración del negocio, la caché de su plantilla y el emisor. Sin función
    activa o con calendario apagado no consulta plantilla ni emisor (no hacen falta).
    Fila de config ausente = oferta pendiente = función apagada.
    """
    a = acceso or avisos_cita_config.AccesoBD()
    config = avisos_cita_config.config_efectiva(await a.leer_config(tenant_id))
    nombre = config[avisos_cita_config.columna_nombre(tipo)]
    activa = bool(config[avisos_cita_config.columna_activo(tipo)])
    if not activa or not await a.calendario_activo(tenant_id):
        return EstadoAviso(False, nombre, None, False)
    fila = await a.plantilla(tenant_id, nombre)
    estado = str(fila["estado"]) if fila else None
    return EstadoAviso(True, nombre, estado, await a.emisor_ok(tenant_id))


# El job renueva el latido de su ejecución al menos cada minuto mientras trabaja (plan 2.3).
INTERVALO_LATIDO_SEGUNDOS = 60


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _monotonico() -> float:
    return time.monotonic()


# ------------------------------------------------------------
# Reglas puras
# ------------------------------------------------------------
def estado_para_motivo(motivo: str) -> str:
    """`telefono_invalido` queda `fallido` (RF-7.2, RF-11.2); el resto de motivos de omisión, `omitido`."""
    return ESTADO_FALLIDO if motivo == TELEFONO_INVALIDO else ESTADO_OMITIDO


def debe_avisar_gerencia(tipo: str, motivo: str | None) -> bool:
    """RF-7/RF-8: solo las cancelaciones, y solo con los motivos que la spec manda avisar."""
    return tipo == TIPO_CANCELACION and motivo in _MENSAJE_GERENCIA


# ------------------------------------------------------------
# Ejecuciones (base de "ejecución viva", RF-10 y RF-16)
# ------------------------------------------------------------
async def crear_ejecucion(tipo: str) -> UUID:
    """Una fila por pasada del job o por cancelación."""
    return await fetch_value(
        "INSERT INTO avisos_cita_ejecuciones (tipo) VALUES ($1) RETURNING id", tipo
    )


async def latir_ejecucion(ejecucion_id: UUID) -> None:
    """Renueva la marca de vida; el job lo hace al menos cada minuto."""
    await execute(
        "UPDATE avisos_cita_ejecuciones SET latido_en = NOW() WHERE id = $1", ejecucion_id
    )


async def terminar_ejecucion(ejecucion_id: UUID) -> None:
    await execute(
        """
        UPDATE avisos_cita_ejecuciones
        SET terminada_en = NOW(), latido_en = NOW()
        WHERE id = $1
        """,
        ejecucion_id,
    )


# ------------------------------------------------------------
# Registro
# ------------------------------------------------------------
async def registrar_intento(
    *,
    tenant_id: UUID,
    reserva_id: UUID,
    tipo: str,
    hora_inicio: datetime,
    ejecucion_id: UUID | None,
    estado: str = ESTADO_EN_CURSO,
    motivo: str | None = None,
    sin_conversacion: bool = False,
) -> UUID | None:
    """
    Puerta del envío (CL-5): `INSERT ... ON CONFLICT DO NOTHING`. Devuelve el id
    del registro nuevo o None si la clave (reserva, tipo, hora_inicio) ya existía;
    en ese caso quien llama NO envía ni crea nada. Un estado final se inserta ya
    cerrado.
    """
    return await fetch_value(
        """
        INSERT INTO avisos_cita
            (tenant_id, reserva_id, tipo, hora_inicio, estado, motivo,
             sin_conversacion, ejecucion_id, cerrado_en)
        VALUES ($1, $2, $3, $4, $5::varchar, $6, $7, $8,
                CASE WHEN $5::varchar = 'en_curso' THEN NULL ELSE NOW() END)
        ON CONFLICT (reserva_id, tipo, hora_inicio) DO NOTHING
        RETURNING id
        """,
        tenant_id,
        reserva_id,
        tipo,
        hora_inicio,
        estado,
        motivo,
        sin_conversacion,
        ejecucion_id,
    )


async def cerrar_registro(
    aviso_id: UUID,
    estado: str,
    motivo: str | None = None,
    telefono_destino: str | None = None,
) -> bool:
    """
    Cierra un registro `en_curso`. Solo actúa si sigue `en_curso`: si la
    recuperación de huérfanos se adelantó, devuelve False y no pisa su resultado.
    `telefono_destino` solo se guarda al pasar a `enviado` (D13).
    """
    cerrado = await fetch_value(
        """
        UPDATE avisos_cita
        SET estado = $2, motivo = $3, telefono_destino = $4, cerrado_en = NOW()
        WHERE id = $1 AND estado = 'en_curso'
        RETURNING id
        """,
        aviso_id,
        estado,
        motivo,
        telefono_destino if estado == ESTADO_ENVIADO else None,
    )
    return cerrado is not None


async def cerrar_sin_envio(aviso_id: UUID, motivo: str) -> bool:
    """
    Cierra sin llamar al proveedor un registro ya insertado (revalidación del
    recordatorio, RF-11). `hora_cambiada` solo puede salir por aquí: es un
    registro que el job ya había insertado para una hora que luego cambió.
    """
    return await cerrar_registro(aviso_id, estado_para_motivo(motivo), motivo)


async def avisar_gerencia_cancelacion(tenant_id: UUID, reserva_id: UUID, motivo: str) -> None:
    """
    Aviso a gerencia (alerta personal por owner/superadmin activo, plan 2.7) de que
    el cliente no fue avisado de la cancelación. Nunca lanza: un fallo aquí no
    cambia el resultado del registro.
    """
    detalle = _MENSAJE_GERENCIA.get(motivo)
    if detalle is None:
        return
    try:
        # Import perezoso: realtime arrastra Socket.IO y esta capa no lo necesita para el resto.
        from realtime import avisar_gerencia

        await avisar_gerencia(
            tenant_id,
            TIPO_ALERTA_GERENCIA,
            "No se pudo avisar al cliente de la cancelación",
            f"El cliente no recibió el aviso de cancelación de su cita: {detalle}",
            {"reserva_id": str(reserva_id), "motivo": motivo},
        )
    except Exception:
        log.exception("No se pudo emitir el aviso a gerencia (reserva %s, motivo %s)", reserva_id, motivo)


async def registrar_sin_envio(
    *,
    tenant_id: UUID,
    reserva_id: UUID,
    tipo: str,
    hora_inicio: datetime,
    ejecucion_id: UUID | None,
    motivo: str,
    sin_conversacion: bool = False,
) -> ResultadoAviso:
    """
    Registra directamente un estado final (`omitido`/`fallido`) sin llamar al
    proveedor, con la misma puerta de unicidad. Si es una cancelación y el motivo
    lo exige, avisa a gerencia.
    """
    estado = estado_para_motivo(motivo)
    aviso_id = await registrar_intento(
        tenant_id=tenant_id,
        reserva_id=reserva_id,
        tipo=tipo,
        hora_inicio=hora_inicio,
        ejecucion_id=ejecucion_id,
        estado=estado,
        motivo=motivo,
        sin_conversacion=sin_conversacion,
    )
    if aviso_id is None:
        return ResultadoAviso(None, None)
    if debe_avisar_gerencia(tipo, motivo):
        await avisar_gerencia_cancelacion(tenant_id, reserva_id, motivo)
    return ResultadoAviso(aviso_id, estado, motivo)


# ------------------------------------------------------------
# Envío
# ------------------------------------------------------------
async def _marcar_plantilla_caida(tenant_id: UUID, nombre: str, cuerpo: str | None) -> None:
    """
    `plantilla_no_disponible`: si el cuerpo de Meta trae un código que lo explica (pausada
    o deshabilitada) marca la plantilla del negocio y deja que `reaccion_estado` apague la
    función y alerte una vez. Sin código reconocible no toca nada. Nunca lanza.
    """
    estado = plantillas_tenant.estado_por_error_de_envio(cuerpo)
    if estado is None:
        return
    try:
        await plantillas_tenant.marcar_estado_por_error(tenant_id, nombre, estado)
    except Exception:
        log.exception("No se pudo marcar la plantilla %r del negocio %s como %s", nombre, tenant_id, estado)


async def enviar_registrado(
    aviso_id: UUID,
    *,
    tenant_id: UUID,
    reserva_id: UUID,
    tipo: str,
    hora_inicio: datetime,
    nombre_plantilla: str,
    destinatario_e164: str,
    parametros: list[dict[str, object]],
    conversation_id: UUID | None,
    servicio: str | None,
) -> ResultadoAviso:
    """
    Llama al proveedor para un registro `en_curso` ya insertado y lo cierra. Envía la
    plantilla `nombre_plantilla` del negocio desde su número principal activo
    (`avisos_cita_envio`, nunca la cuenta global); `parametros` es
    `template.components[]` (`avisos_cita_reglas.parametros_de_envio`).
    Un solo intento, sin reintentos. Nunca lanza por un fallo del proveedor: lo deja
    `fallido` (o `omitido: sin_numero_emisor` si el número dejó de servir entre la
    decisión y el envío). Si el proceso muere dentro, el registro queda `en_curso` y
    lo recoge `recuperar_huerfanos`.
    """
    motivo: str | None = None
    try:
        res = await avisos_cita_envio.enviar_plantilla_tenant(
            tenant_id, destinatario_e164, nombre_plantilla, parametros
        )
    except Exception:
        log.exception("Aviso %s (%s): error inesperado al enviar", aviso_id, tipo)
        motivo = ERROR_PROVEEDOR
    else:
        if not res.enviado:
            motivo = res.motivo or ERROR_PROVEEDOR
            log.warning(
                "Aviso %s (%s) no enviado: %s [status=%s]", aviso_id, tipo, motivo, res.status_code
            )
            if motivo == PLANTILLA_NO_DISPONIBLE:
                await _marcar_plantilla_caida(tenant_id, nombre_plantilla, res.cuerpo)

    if motivo is not None:
        # Sin número emisor no se llegó a llamar al proveedor: es una omisión, no un fallo.
        estado = ESTADO_OMITIDO if motivo == SIN_NUMERO_EMISOR else ESTADO_FALLIDO
        cerrado = await cerrar_registro(aviso_id, estado, motivo)
        if cerrado and debe_avisar_gerencia(tipo, motivo):
            await avisar_gerencia_cancelacion(tenant_id, reserva_id, motivo)
        return ResultadoAviso(aviso_id, estado if cerrado else None, motivo)

    cerrado = await cerrar_registro(
        aviso_id, ESTADO_ENVIADO, telefono_destino=destinatario_e164
    )
    if not cerrado:
        # La recuperación de huérfanos lo cerró antes: manda su resultado y no hay evento.
        log.warning("Aviso %s ya estaba cerrado al terminar el envío", aviso_id)
        return ResultadoAviso(aviso_id, None)

    evento_creado = False
    try:
        evento_creado = await contexto_agente.registrar_evento(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            reserva_id=reserva_id,
            aviso_id=aviso_id,
            tipo_aviso=tipo,
            hora_cita=hora_inicio,
            servicio=servicio,
        )
    except Exception:
        # El aviso ya salió: un fallo del evento no lo cambia (queda en el log).
        log.exception("Aviso %s enviado, pero no se pudo crear el evento de contexto", aviso_id)
    return ResultadoAviso(aviso_id, ESTADO_ENVIADO, None, evento_creado)


async def enviar_con_registro(
    *,
    tenant_id: UUID,
    reserva_id: UUID,
    tipo: str,
    hora_inicio: datetime,
    ejecucion_id: UUID | None,
    nombre_plantilla: str,
    destinatario_e164: str,
    parametros: list[dict[str, object]],
    conversation_id: UUID | None,
    servicio: str | None,
) -> ResultadoAviso:
    """
    `en_curso` -> proveedor -> cierre (D1). Si la clave ya existe (otra pasada u
    otro proceso), no envía ni crea registro. Quien llama ya decidió que no
    aplica ningún motivo de omisión.
    """
    aviso_id = await registrar_intento(
        tenant_id=tenant_id,
        reserva_id=reserva_id,
        tipo=tipo,
        hora_inicio=hora_inicio,
        ejecucion_id=ejecucion_id,
        sin_conversacion=conversation_id is None,
    )
    if aviso_id is None:
        return ResultadoAviso(None, None)
    return await enviar_registrado(
        aviso_id,
        tenant_id=tenant_id,
        reserva_id=reserva_id,
        tipo=tipo,
        hora_inicio=hora_inicio,
        nombre_plantilla=nombre_plantilla,
        destinatario_e164=destinatario_e164,
        parametros=parametros,
        conversation_id=conversation_id,
        servicio=servicio,
    )


# ------------------------------------------------------------
# Huérfanos
# ------------------------------------------------------------
async def recuperar_huerfanos(ahora: datetime | None = None) -> list[UUID]:
    """
    Todo `en_curso` cuya ejecución ya no está viva (sin latido en
    AVISOS_EJECUCION_VIVA_MINUTOS, terminada o borrada) pasa a `fallido` con
    `resultado_desconocido`, sin reenviar (CL-4). Si era una cancelación, avisa a
    gerencia. Un `en_curso` de ejecución viva no se toca (CL-5). Devuelve los ids
    cerrados por esta llamada.
    """
    ahora = ahora or _ahora()
    filas = await fetch_all(
        """
        SELECT a.id, a.tenant_id, a.reserva_id, a.tipo, e.latido_en, e.terminada_en
        FROM avisos_cita a
        LEFT JOIN avisos_cita_ejecuciones e ON e.id = a.ejecucion_id
        WHERE a.estado = 'en_curso'
        """
    )
    cerrados: list[UUID] = []
    for fila in filas:
        viva = fila["terminada_en"] is None and ejecucion_viva(
            fila["latido_en"], ahora, settings.AVISOS_EJECUCION_VIVA_MINUTOS
        )
        if viva:
            continue
        if not await cerrar_registro(fila["id"], ESTADO_FALLIDO, RESULTADO_DESCONOCIDO):
            continue
        cerrados.append(fila["id"])
        log.warning("Aviso huérfano %s (%s) pasa a fallido resultado_desconocido", fila["id"], fila["tipo"])
        if debe_avisar_gerencia(fila["tipo"], RESULTADO_DESCONOCIDO):
            await avisar_gerencia_cancelacion(
                fila["tenant_id"], fila["reserva_id"], RESULTADO_DESCONOCIDO
            )
    return cerrados


# ------------------------------------------------------------
# Cancelación del equipo (RF-6 a RF-8, RF-15)
# ------------------------------------------------------------
async def _datos_cancelacion(tenant_id: UUID, reserva_id: UUID) -> dict | None:
    """
    Relee la reserva ya cancelada (plan 2.4): hora, teléfono, cliente, servicio y
    el canal de la conversación más reciente del cliente (None si no tiene).
    """
    fila = await fetch_one(
        """
        SELECT r.estado, r.hora_inicio, r.cliente_telefono, r.user_id,
               s.nombre AS servicio_nombre,
               (SELECT c.channel_type
                FROM conversations c
                WHERE c.tenant_id = r.tenant_id AND c.user_id = r.user_id
                ORDER BY c.last_message_at DESC NULLS LAST
                LIMIT 1) AS channel_type
        FROM reservas r
        JOIN servicios s ON s.id = r.servicio_id
        WHERE r.id = $1 AND r.tenant_id = $2
        """,
        reserva_id,
        tenant_id,
    )
    return dict(fila) if fila is not None else None


async def procesar_cancelacion(tenant_id: UUID, reserva_id: UUID) -> ResultadoAviso | None:
    """
    Aviso de cancelación del equipo, ejecutado en segundo plano tras el commit de
    la cancelación. Evalúa RF-7 en su orden y registra y envía una sola vez
    (`UNIQUE reserva, tipo, hora_inicio`). NUNCA lanza: un fallo aquí no cambia
    el resultado de la cancelación, que ya está hecha (queda en el log).
    """
    ejecucion_id: UUID | None = None
    try:
        # D7: función apagada, calendario inactivo u oferta pendiente -> ni se envía ni se
        # registra (tampoco se crea ejecución). No hay reenvío retroactivo al activarla.
        aviso = await _estado_aviso(tenant_id, TIPO_CANCELACION)
        if not aviso.habilitado:
            log.info("Aviso de cancelación sin función activa (reserva %s): no se registra", reserva_id)
            return None

        datos = await _datos_cancelacion(tenant_id, reserva_id)
        if datos is None or datos["estado"] != "cancelada":
            log.warning("Aviso de cancelación omitido: reserva %s no está cancelada", reserva_id)
            return None

        estado_telefono, telefono_e164 = clasificar_telefono(datos["cliente_telefono"])
        conversation_id = await contexto_agente.conversacion_whatsapp_del_cliente(
            tenant_id, datos["user_id"]
        )
        motivo = motivo_cancelacion(
            telefono=estado_telefono,
            canal_ok=canal_soportado(datos["channel_type"]),
            activo=settings.AVISOS_CITA_WHATSAPP_ACTIVOS,
            plantilla_estado=aviso.plantilla_estado,
            emisor_ok=aviso.emisor_ok,
        )
        if motivo is not None and not se_registra(motivo):
            # Plantilla PENDING (esperando aprobación): sin registro ni alerta (RF-9).
            log.info("Aviso de cancelación en espera de aprobación (reserva %s)", reserva_id)
            return None

        # La ejecución se crea solo cuando va a haber registro: una cancelación en espera
        # (PENDING) no deja fila en `avisos_cita_ejecuciones`.
        ejecucion_id = await crear_ejecucion(TIPO_CANCELACION)
        if motivo is not None or telefono_e164 is None:
            return await registrar_sin_envio(
                tenant_id=tenant_id,
                reserva_id=reserva_id,
                tipo=TIPO_CANCELACION,
                hora_inicio=datos["hora_inicio"],
                ejecucion_id=ejecucion_id,
                motivo=motivo or TELEFONO_INVALIDO,
                sin_conversacion=conversation_id is None,
            )

        zona = (await get_tenant_servicios(tenant_id)).zona_horaria
        return await enviar_con_registro(
            tenant_id=tenant_id,
            reserva_id=reserva_id,
            tipo=TIPO_CANCELACION,
            hora_inicio=datos["hora_inicio"],
            ejecucion_id=ejecucion_id,
            nombre_plantilla=aviso.nombre_plantilla,
            destinatario_e164=telefono_e164,
            parametros=parametros_de_envio(
                TIPO_CANCELACION,
                "",
                datos["servicio_nombre"],
                fecha_mostrada(datos["hora_inicio"], zona),
            ),
            conversation_id=conversation_id,
            servicio=datos["servicio_nombre"],
        )
    except Exception:
        log.exception("No se pudo procesar el aviso de cancelación de la reserva %s", reserva_id)
        return None
    finally:
        if ejecucion_id is not None:
            try:
                await terminar_ejecucion(ejecucion_id)
            except Exception:
                log.exception("No se pudo cerrar la ejecución %s", ejecucion_id)


# ------------------------------------------------------------
# Recordatorio automático (RF-9 a RF-12, RF-17)
# ------------------------------------------------------------
# Mismo criterio de canal que la cancelación: la conversación más reciente del cliente.
_SQL_DATOS_RECORDATORIO = """
    SELECT r.id AS reserva_id, r.tenant_id, r.hora_inicio, r.estado,
           r.cliente_telefono, r.user_id,
           s.nombre AS servicio_nombre,
           t.name AS nombre_negocio,
           (SELECT c.channel_type
            FROM conversations c
            WHERE c.tenant_id = r.tenant_id AND c.user_id = r.user_id
            ORDER BY c.last_message_at DESC NULLS LAST
            LIMIT 1) AS channel_type
    FROM reservas r
    JOIN servicios s ON s.id = r.servicio_id
    JOIN tenants t ON t.id = r.tenant_id
"""


async def _candidatos_recordatorio(ahora: datetime) -> list[dict]:
    """
    Citas `confirmada` con inicio en `(ahora, ahora + 5 h]` sin registro de recordatorio
    para su hora actual, de negocios con el calendario activo y el recordatorio encendido
    (D7: sin función activa no hay candidato ni registro; fila de config ausente = oferta
    pendiente = apagado). Sin AVISOS_CITA_WHATSAPP_ACTIVOS tampoco hay candidatos.
    Se evalúa por ventana, sin "desde cuándo": una cita ya dentro de las 5 h cuando se
    activa la función sí lo recibe (RF-10); solo la frena un registro previo (idempotencia).
    """
    if not settings.AVISOS_CITA_WHATSAPP_ACTIVOS:
        return []
    filas = await fetch_all(
        _SQL_DATOS_RECORDATORIO
        + """
        JOIN tenant_servicios ts ON ts.tenant_id = r.tenant_id AND ts.calendario_activo
        JOIN avisos_cita_config cfg ON cfg.tenant_id = r.tenant_id AND cfg.recordatorio_activo
        WHERE r.estado = 'confirmada'
          AND r.hora_inicio > $1
          AND r.hora_inicio <= $1 + make_interval(hours => $2)
          AND NOT EXISTS (
              SELECT 1 FROM avisos_cita a
              WHERE a.reserva_id = r.id AND a.tipo = 'recordatorio'
                AND a.hora_inicio = r.hora_inicio
          )
        ORDER BY r.hora_inicio
        """,
        ahora,
        HORAS_VENTANA,
    )
    return [dict(f) for f in filas]


async def _datos_reserva_recordatorio(tenant_id: UUID, reserva_id: UUID) -> dict | None:
    """Relee la reserva justo antes de enviar (revalidación, RF-11)."""
    fila = await fetch_one(
        _SQL_DATOS_RECORDATORIO + " WHERE r.id = $1 AND r.tenant_id = $2",
        reserva_id,
        tenant_id,
    )
    return dict(fila) if fila is not None else None


async def _procesar_recordatorio(
    cand: dict,
    *,
    ejecucion_id: UUID,
    ahora: datetime,
    ahora_fijo: datetime | None,
    zonas: dict[UUID, str],
) -> None:
    """Un recordatorio: decide, registra, revalida y envía. Puede lanzar; quien llama lo contiene."""
    tenant_id: UUID = cand["tenant_id"]
    reserva_id: UUID = cand["reserva_id"]
    hora_inicio: datetime = cand["hora_inicio"]

    estado_tel, _ = clasificar_telefono(cand["cliente_telefono"])
    conversation_id = await contexto_agente.conversacion_whatsapp_del_cliente(
        tenant_id, cand["user_id"]
    )
    # El filtro SQL de candidatos ya exige función activa; esto solo cubre que la apaguen
    # entre la consulta y este punto (D7: ni envío ni registro).
    aviso = await _estado_aviso(tenant_id, TIPO_RECORDATORIO)
    if not aviso.habilitado:
        return
    motivo = motivo_recordatorio(
        telefono=estado_tel,
        canal_ok=canal_soportado(cand["channel_type"]),
        estado_cita=cand["estado"],
        hora_coincide=True,
        inicio_llegado=hora_inicio <= ahora,
        activo=settings.AVISOS_CITA_WHATSAPP_ACTIVOS,
        plantilla_estado=aviso.plantilla_estado,
        emisor_ok=aviso.emisor_ok,
    )
    if motivo is not None and not se_registra(motivo):
        return  # plantilla PENDING: sin registro (RF-9); la siguiente pasada lo reintenta
    if motivo is not None:
        # Estado final desde el principio; sin aviso a gerencia (los recordatorios no avisan).
        await registrar_sin_envio(
            tenant_id=tenant_id,
            reserva_id=reserva_id,
            tipo=TIPO_RECORDATORIO,
            hora_inicio=hora_inicio,
            ejecucion_id=ejecucion_id,
            motivo=motivo,
            sin_conversacion=conversation_id is None,
        )
        return

    aviso_id = await registrar_intento(
        tenant_id=tenant_id,
        reserva_id=reserva_id,
        tipo=TIPO_RECORDATORIO,
        hora_inicio=hora_inicio,
        ejecucion_id=ejecucion_id,
        sin_conversacion=conversation_id is None,
    )
    if aviso_id is None:
        return  # otra pasada u otro proceso ya tiene la clave (CL-5)

    try:
        # Revalidación justo antes de enviar (RF-11): estado, hora, teléfono, canal, bandera y plantilla.
        ahora_envio = ahora_fijo or _ahora()
        fresca = await _datos_reserva_recordatorio(tenant_id, reserva_id)
        if fresca is None:
            await cerrar_sin_envio(aviso_id, CITA_NO_CONFIRMADA)
            return
        estado_tel, telefono_e164 = clasificar_telefono(fresca["cliente_telefono"])
        fresco = await _estado_aviso(tenant_id, TIPO_RECORDATORIO)
        motivo = motivo_recordatorio(
            telefono=estado_tel,
            canal_ok=canal_soportado(fresca["channel_type"]),
            estado_cita=fresca["estado"],
            hora_coincide=fresca["hora_inicio"] == hora_inicio,
            inicio_llegado=fresca["hora_inicio"] <= ahora_envio,
            activo=settings.AVISOS_CITA_WHATSAPP_ACTIVOS,
            plantilla_estado=fresco.plantilla_estado,
            emisor_ok=fresco.emisor_ok,
        )
        if motivo is None and not fresco.habilitado:
            motivo = DESACTIVADO  # la apagaron entre medias; el registro ya existe, se cierra
        if motivo == ESPERANDO_APROBACION:
            motivo = PLANTILLA_NO_APROBADA  # el CHECK no admite la espera; el registro ya existe
        if motivo is not None or telefono_e164 is None:
            await cerrar_sin_envio(aviso_id, motivo or TELEFONO_INVALIDO)
            return

        if tenant_id not in zonas:
            zonas[tenant_id] = (await get_tenant_servicios(tenant_id)).zona_horaria
        parametros = parametros_de_envio(
            TIPO_RECORDATORIO,
            fresca["nombre_negocio"],
            None,
            fecha_mostrada(hora_inicio, zonas[tenant_id]),
        )
    except Exception:
        # El registro ya está `en_curso`: se cierra aquí para no dejarlo hasta la recuperación de huérfanos.
        log.exception("Recordatorio %s: falló antes de enviar", aviso_id)
        await cerrar_registro(aviso_id, ESTADO_FALLIDO, ERROR_PROVEEDOR)
        return

    await enviar_registrado(
        aviso_id,
        tenant_id=tenant_id,
        reserva_id=reserva_id,
        tipo=TIPO_RECORDATORIO,
        hora_inicio=hora_inicio,
        nombre_plantilla=fresco.nombre_plantilla,
        destinatario_e164=telefono_e164,
        parametros=parametros,
        conversation_id=conversation_id,
        servicio=fresca["servicio_nombre"],
    )


async def procesar_recordatorios(ahora: datetime | None = None) -> dict[str, int]:
    """
    Una pasada del job (cada AVISOS_CITA_REVISION_MINUTOS). Crea su ejecución,
    recupera los huérfanos de ejecuciones muertas y trata cada cita en ventana;
    un fallo en una cita no frena las demás. Devuelve `{"revisadas", "errores"}`.
    """
    ejecucion_id = await crear_ejecucion(TIPO_RECORDATORIO)
    revisadas = 0
    errores = 0
    try:
        try:
            await recuperar_huerfanos()
        except Exception:
            log.exception("No se pudieron recuperar los avisos huérfanos")

        ahora_pasada = ahora or _ahora()
        candidatos = await _candidatos_recordatorio(ahora_pasada)
        zonas: dict[UUID, str] = {}
        ultimo_latido = _monotonico()
        for cand in candidatos:
            if _monotonico() - ultimo_latido >= INTERVALO_LATIDO_SEGUNDOS:
                await latir_ejecucion(ejecucion_id)
                ultimo_latido = _monotonico()
            revisadas += 1
            try:
                await _procesar_recordatorio(
                    cand,
                    ejecucion_id=ejecucion_id,
                    ahora=ahora_pasada,
                    ahora_fijo=ahora,
                    zonas=zonas,
                )
            except Exception:
                errores += 1
                log.exception("No se pudo procesar el recordatorio de la reserva %s", cand.get("reserva_id"))
        return {"revisadas": revisadas, "errores": errores}
    finally:
        try:
            await terminar_ejecucion(ejecucion_id)
        except Exception:
            log.exception("No se pudo cerrar la ejecución %s", ejecucion_id)


# ------------------------------------------------------------
# Consulta (RF-16)
# ------------------------------------------------------------
async def listar_avisos_de_reserva(tenant_id: UUID, reserva_id: UUID) -> list[dict]:
    """Registros de aviso de una reserva del negocio, del más antiguo al más nuevo."""
    filas = await fetch_all(
        """
        SELECT id, reserva_id, tipo, hora_inicio, estado, motivo,
               sin_conversacion, creado_en, cerrado_en
        FROM avisos_cita
        WHERE tenant_id = $1 AND reserva_id = $2
        ORDER BY creado_en, id
        """,
        tenant_id,
        reserva_id,
    )
    return [dict(f) for f in filas]
