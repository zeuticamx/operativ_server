"""
Regla de cancelación de citas (spec 001).

Sin I/O a propósito: ni base de datos, ni FastAPI, ni reloj propio. El
instante actual (`ahora`) siempre lo pasa quien llama (en producción,
`datetime.now(timezone.utc)`), así los tests lo fijan y la consulta previa del
agente y la cancelación real evalúan EXACTAMENTE con el mismo criterio (R28).

Todos los instantes deben traer zona horaria; uno ingenuo lanza ValueError
porque compararlo contra otro con zona daría un TypeError opaco.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

Origen = Literal["cliente", "equipo"]
MotivoNoCancelable = Literal["reserva_no_confirmada", "cita_ya_iniciada"]
Granularidad = Literal["dia", "mes"]

ESTADO_CONFIRMADA = "confirmada"

DIAS_RANGO_DEFECTO = 30
DIAS_RANGO_MAXIMO = 366


@dataclass(frozen=True)
class EvaluacionCancelacion:
    puede_cancelar: bool
    motivo_no_cancelable: MotivoNoCancelable | None
    segundos_restantes: int  # exacto; negativo si ya empezó
    antelacion_minima_horas: int | None
    tardia: bool  # solo True si origen == "cliente" y fuera de plazo


def _exigir_zona(instante: datetime, nombre: str) -> None:
    if instante.tzinfo is None or instante.utcoffset() is None:
        raise ValueError(f"{nombre} debe incluir zona horaria")


def segundos_hasta(hora_inicio: datetime, ahora: datetime) -> int:
    """Segundos de `ahora` a `hora_inicio`, truncados hacia cero (negativo si ya empezó)."""
    _exigir_zona(hora_inicio, "hora_inicio")
    _exigir_zona(ahora, "ahora")
    return int((hora_inicio - ahora).total_seconds())


def evaluar_cancelacion(
    *,
    estado: str,
    hora_inicio: datetime,
    antelacion_horas: int | None,
    origen: Origen,
    ahora: datetime,
) -> EvaluacionCancelacion:
    """
    Decide si se puede cancelar y si la cancelación es tardía.

    Orden (R12-R16): estado primero, luego hora de inicio (solo para el
    cliente; el equipo puede cancelar una cita ya iniciada), luego la regla.
    La comparación de tardía usa el instante exacto, sin redondear horas:
    faltar exactamente N x 3600 s NO es tardía.
    """
    if origen not in ("cliente", "equipo"):
        raise ValueError(f"origen inválido: {origen!r}")
    _exigir_zona(hora_inicio, "hora_inicio")
    _exigir_zona(ahora, "ahora")

    restante = hora_inicio - ahora
    segundos = int(restante.total_seconds())

    def _rechazo(motivo: MotivoNoCancelable) -> EvaluacionCancelacion:
        return EvaluacionCancelacion(
            puede_cancelar=False,
            motivo_no_cancelable=motivo,
            segundos_restantes=segundos,
            antelacion_minima_horas=antelacion_horas,
            tardia=False,
        )

    if estado != ESTADO_CONFIRMADA:
        return _rechazo("reserva_no_confirmada")

    # Comparación exacta (no sobre los segundos truncados): a 0,5 s del inicio
    # la cita todavía no empezó.
    if origen == "cliente" and restante <= timedelta(0):
        return _rechazo("cita_ya_iniciada")

    tardia = (
        origen == "cliente"
        and antelacion_horas is not None
        and restante < timedelta(hours=antelacion_horas)
    )
    return EvaluacionCancelacion(
        puede_cancelar=True,
        motivo_no_cancelable=None,
        segundos_restantes=segundos,
        antelacion_minima_horas=antelacion_horas,
        tardia=tardia,
    )


def texto_alerta_tardia(segundos_restantes: int, antelacion_horas: int) -> str:
    """
    Sufijo del mensaje de la alerta "Reserva cancelada" (R22):
    "(cancelación tardía: faltaban H h M min, límite N h)". H y M son enteros
    truncados (23 h 59 min 59 s -> "23 h 59 min"). Devuelve solo el sufijo;
    quien llama lo concatena al mensaje con un espacio.
    """
    restantes = max(segundos_restantes, 0)
    horas, resto = divmod(restantes, 3600)
    minutos = resto // 60
    return f"(cancelación tardía: faltaban {horas} h {minutos} min, límite {antelacion_horas} h)"


def periodo_local(instante: datetime, zona: str, granularidad: Granularidad) -> str:
    """
    Periodo del reporte (R40) en la zona del tenant: 'AAAA-MM-DD' o 'AAAA-MM'.
    Una zona desconocida o una granularidad inválida lanzan ValueError.
    """
    _exigir_zona(instante, "instante")
    if granularidad not in ("dia", "mes"):
        raise ValueError("granularidad debe ser 'dia' o 'mes'")
    try:
        tz = ZoneInfo(zona)
    except (KeyError, ValueError, OSError) as exc:
        raise ValueError(f"zona horaria inválida: {zona!r}") from exc
    local = instante.astimezone(tz)
    return local.strftime("%Y-%m-%d" if granularidad == "dia" else "%Y-%m")


def validar_rango_reporte(
    desde: date | None, hasta: date | None, hoy: date
) -> tuple[date, date]:
    """
    Resuelve el rango inclusivo del reporte (R41). Sin rango: últimos 30 días
    contando `hoy` (hoy-29 .. hoy). Con un solo extremo, el otro se deduce
    (hasta = hoy, o desde = hasta - 29). Lanza ValueError si desde > hasta o el
    rango supera 366 días inclusivos; el router lo vuelve 422.
    """
    if hasta is None:
        hasta = hoy
    if desde is None:
        desde = hasta - timedelta(days=DIAS_RANGO_DEFECTO - 1)
    if desde > hasta:
        raise ValueError("'desde' no puede ser posterior a 'hasta'")
    if (hasta - desde).days + 1 > DIAS_RANGO_MAXIMO:
        raise ValueError(f"El rango no puede superar {DIAS_RANGO_MAXIMO} días")
    return desde, hasta
