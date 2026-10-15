"""
Reglas puras de los avisos de cita por WhatsApp (spec 004).

Sin I/O a propósito: ni base de datos, ni FastAPI, ni reloj propio. El instante
actual (`ahora`) siempre lo pasa quien llama (en producción,
`datetime.now(timezone.utc)`), así los tests lo fijan y la cancelación y el job
de recordatorios aplican EXACTAMENTE la misma regla.

Los instantes deben traer zona horaria; uno ingenuo lanza ValueError.
"""

import re
from datetime import datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from services.historial_citas import es_canal_de_cliente

# Motivos de omisión o fallo (lista cerrada, igual que el CHECK de `avisos_cita`).
SIN_TELEFONO = "sin_telefono"
TELEFONO_INVALIDO = "telefono_invalido"
CANAL_NO_SOPORTADO = "canal_no_soportado"
DESACTIVADO = "desactivado_por_configuracion"
PLANTILLA_NO_APROBADA = "plantilla_no_aprobada"
CITA_NO_CONFIRMADA = "cita_no_confirmada"
HORA_CAMBIADA = "hora_cambiada"
CITA_YA_INICIADA = "cita_ya_iniciada"

ESTADO_CONFIRMADA = "confirmada"
SERVICIO_RESPALDO = "servicio agendado"
HORAS_VENTANA = 5
MINUTOS_EJECUCION_VIVA = 5
HORAS_RESPUESTA = 24
PREFIJO_MEXICO = "+52"

_PATRON_E164 = re.compile(r"\+[1-9]\d{7,14}")
_SEPARADORES = re.compile(r"[\s\-().]")


class EstadoTelefono(str, Enum):
    """Clasificación del teléfono de la reserva (plan 2.14)."""

    VACIO = "VACIO"
    VALIDO = "VALIDO"
    NO_NORMALIZABLE = "NO_NORMALIZABLE"


def _exigir_zona(instante: datetime, nombre: str) -> None:
    if instante.tzinfo is None or instante.utcoffset() is None:
        raise ValueError(f"{nombre} debe incluir zona horaria")


def _zona(zona: str) -> ZoneInfo:
    try:
        return ZoneInfo(zona)
    except (KeyError, ValueError, OSError) as exc:
        raise ValueError(f"zona horaria inválida: {zona!r}") from exc


def clasificar_telefono(crudo: str | None) -> tuple[EstadoTelefono, str | None]:
    """
    Regla del plan 2.14, en este orden: vacío -> VACIO; quitar espacios, guiones,
    paréntesis y puntos; `00` inicial pasa a `+`; exactamente 10 dígitos son de
    México (+52); E.164 `^\\+[1-9]\\d{7,14}$` se acepta tal cual; el resto es
    NO_NORMALIZABLE. Devuelve (estado, E.164 o None).
    """
    if crudo is None or not crudo.strip():
        return EstadoTelefono.VACIO, None
    limpio = _SEPARADORES.sub("", crudo)
    if limpio.startswith("00"):
        limpio = "+" + limpio[2:]
    if len(limpio) == 10 and limpio.isascii() and limpio.isdigit():
        return EstadoTelefono.VALIDO, PREFIJO_MEXICO + limpio
    if _PATRON_E164.fullmatch(limpio):
        return EstadoTelefono.VALIDO, limpio
    return EstadoTelefono.NO_NORMALIZABLE, None


def canal_soportado(channel_type: str | None) -> bool:
    """
    Falso solo para el widget/chat-web (cualquier canal que no sea WhatsApp,
    Instagram o Facebook). Sin conversación (None) cuenta como soportado: el
    teléfono de la reserva basta (RF-15).
    """
    if channel_type is None:
        return True
    return es_canal_de_cliente(channel_type)


def motivo_cancelacion(
    *,
    telefono: EstadoTelefono,
    canal_ok: bool,
    activo: bool,
    plantilla_aprobada: bool,
) -> str | None:
    """Orden exacto de RF-7: el primer motivo que aplique decide; None = se intenta el envío."""
    if telefono == EstadoTelefono.VACIO:
        return SIN_TELEFONO
    if telefono == EstadoTelefono.NO_NORMALIZABLE:
        return TELEFONO_INVALIDO
    if not canal_ok:
        return CANAL_NO_SOPORTADO
    if not activo:
        return DESACTIVADO
    if not plantilla_aprobada:
        return PLANTILLA_NO_APROBADA
    return None


def motivo_recordatorio(
    *,
    telefono: EstadoTelefono,
    canal_ok: bool,
    estado_cita: str,
    hora_coincide: bool,
    inicio_llegado: bool,
    activo: bool,
    plantilla_aprobada: bool,
) -> str | None:
    """
    Orden de RF-11 con `canal_no_soportado` tras `telefono_invalido` (plan 2.2,
    igual que RF-7). None = se inserta `en_curso` y se llama al proveedor.
    """
    if telefono == EstadoTelefono.VACIO:
        return SIN_TELEFONO
    if telefono == EstadoTelefono.NO_NORMALIZABLE:
        return TELEFONO_INVALIDO
    if not canal_ok:
        return CANAL_NO_SOPORTADO
    if estado_cita != ESTADO_CONFIRMADA:
        return CITA_NO_CONFIRMADA
    if not hora_coincide:
        return HORA_CAMBIADA
    if inicio_llegado:
        return CITA_YA_INICIADA
    if not activo:
        return DESACTIVADO
    if not plantilla_aprobada:
        return PLANTILLA_NO_APROBADA
    return None


def en_ventana(inicio: datetime, ahora: datetime, horas: int = HORAS_VENTANA) -> bool:
    """RF-9: `ahora < inicio <= ahora + horas`, con el instante real (nunca la fecha mostrada)."""
    _exigir_zona(inicio, "inicio")
    _exigir_zona(ahora, "ahora")
    return ahora < inicio <= ahora + timedelta(hours=horas)


def fecha_mostrada(inicio: datetime, zona: str) -> str:
    """Fecha dd-mm-aaaa de la cita en la zona del negocio (solo para las plantillas)."""
    _exigir_zona(inicio, "inicio")
    return inicio.astimezone(_zona(zona)).strftime("%d-%m-%Y")


def variables_cancelacion(servicio: str | None, inicio: datetime, zona: str) -> list[str]:
    """`notificacion_cancelacion`: [{{1}} servicio, {{2}} fecha]. Nunca vacío (CL-18)."""
    nombre = (servicio or "").strip() or SERVICIO_RESPALDO
    return [nombre, fecha_mostrada(inicio, zona)]


def variables_recordatorio(nombre_negocio: str, inicio: datetime, zona: str) -> list[str]:
    """`recordatorio_cita`: [{{1}} negocio, {{2}} fecha]. El nombre del negocio no puede faltar."""
    nombre = (nombre_negocio or "").strip()
    if not nombre:
        raise ValueError("el nombre del negocio no puede ir vacío en la plantilla")
    return [nombre, fecha_mostrada(inicio, zona)]


def ejecucion_viva(
    latido_en: datetime | None, ahora: datetime, minutos: int = MINUTOS_EJECUCION_VIVA
) -> bool:
    """Viva si renovó su marca hace menos de `minutos`; sin marca, muerta."""
    if latido_en is None:
        return False
    _exigir_zona(latido_en, "latido_en")
    _exigir_zona(ahora, "ahora")
    return ahora - latido_en < timedelta(minutes=minutos)


def puede_responder(
    ultima_en: datetime | None, ahora: datetime, horas: int = HORAS_RESPUESTA
) -> bool:
    """D13: como máximo una respuesta fija cada `horas` h, contadas desde la última enviada."""
    if ultima_en is None:
        return True
    _exigir_zona(ultima_en, "ultima_en")
    _exigir_zona(ahora, "ahora")
    return ahora - ultima_en >= timedelta(hours=horas)
