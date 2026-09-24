"""
Cálculo puro de slots de disponibilidad del módulo de calendarios.

Sin base de datos y sin imports fuera de la librería estándar, mismo
criterio que pipeline_estados.py: se puede probar con pytest sin levantar
Postgres. services/calendario.py es quien lee horarios/excepciones/reservas
reales y alimenta estas funciones; acá no se sabe nada de asyncpg.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class BloqueHorario:
    """Una fila de proveedor_horarios: un tramo del horario semanal."""

    dia_semana: int  # 0=lunes … 6=domingo, igual que date.weekday()
    hora_inicio: time
    hora_fin: time


@dataclass(frozen=True)
class Excepcion:
    """Una fila de proveedor_excepciones para una fecha puntual."""

    fecha: date
    disponible: bool
    hora_inicio: time | None
    hora_fin: time | None


@dataclass(frozen=True)
class RangoOcupado:
    """Una reserva activa, ya en datetime tz-aware (UTC)."""

    inicio: datetime
    fin: datetime


@dataclass(frozen=True)
class Descanso:
    """
    Una fila de proveedor_descansos: recurrente (dia_semana, todos los
    lunes p.ej.) o puntual (fecha, un día concreto) — exactamente uno de
    los dos, nunca ambos. A diferencia de Excepcion, un descanso nunca
    reemplaza la jornada: siempre se RESTA de las ventanas ya calculadas.
    """

    dia_semana: int | None
    fecha: date | None
    hora_inicio: time
    hora_fin: time


def se_traslapan(inicio_a: datetime, fin_a: datetime, inicio_b: datetime, fin_b: datetime) -> bool:
    """True si [inicio_a, fin_a) y [inicio_b, fin_b) comparten algún instante."""
    return inicio_a < fin_b and inicio_b < fin_a


def _restar_intervalo(
    ventanas: list[tuple[time, time]], resta_inicio: time, resta_fin: time
) -> list[tuple[time, time]]:
    """
    Recorta [resta_inicio, resta_fin) de cada ventana, partiéndola en dos si
    el corte cae en medio (mismo efecto que ya tiene un turno partido, pero
    calculado en vez de configurado a mano bloque por bloque).
    """
    resultado: list[tuple[time, time]] = []
    for inicio, fin in ventanas:
        if resta_fin <= inicio or resta_inicio >= fin:
            resultado.append((inicio, fin))
            continue
        if resta_inicio > inicio:
            resultado.append((inicio, resta_inicio))
        if resta_fin < fin:
            resultado.append((resta_fin, fin))
    return resultado


def descansos_del_dia(fecha: date, descansos: list[Descanso]) -> list[tuple[time, time]]:
    """Descansos que aplican a `fecha`: los recurrentes de ese día de la semana + los puntuales de esa fecha exacta."""
    return [
        (d.hora_inicio, d.hora_fin)
        for d in descansos
        if d.fecha == fecha or (d.fecha is None and d.dia_semana == fecha.weekday())
    ]


def descanso_dentro_de_jornada(
    hora_inicio: time, hora_fin: time, ventanas_base: list[tuple[time, time]]
) -> bool:
    """
    True si [hora_inicio, hora_fin) cabe COMPLETO dentro de alguna ventana
    de `ventanas_base` — la jornada configurada ANTES de restarle ningún
    otro descanso. Se valida así (y no contra el resultado ya recortado de
    ventanas_del_dia) para que un segundo descanso no se rechace por culpa
    del hueco que dejó el primero: cada descanso se mide contra la jornada
    real, no contra lo que otro descanso ya recortó.
    """
    return any(ini <= hora_inicio and hora_fin <= fin for ini, fin in ventanas_base)


def ventanas_del_dia(
    fecha: date,
    horarios: list[BloqueHorario],
    excepciones: dict[date, Excepcion],
    zona_horaria: str,
    descansos: list[Descanso] | None = None,
) -> list[tuple[datetime, datetime]]:
    """
    Ventanas de atención de un proveedor para `fecha`, como tuplas UTC
    tz-aware.

    Si hay una excepción para esa fecha, REEMPLAZA por completo al horario
    semanal: día no disponible -> sin ventanas; horario especial -> solo esa
    ventana. Sin excepción, se usan los BloqueHorario cuyo dia_semana
    coincide con fecha.weekday() (puede haber más de uno: turno partido).
    Los `descansos` aplicables a `fecha` se restan al final, sea cual sea el
    origen de la ventana.
    """
    zona = ZoneInfo(zona_horaria)
    excepcion = excepciones.get(fecha)

    if excepcion is not None:
        if not excepcion.disponible:
            return []
        bloques_del_dia = [(excepcion.hora_inicio, excepcion.hora_fin)]
    else:
        bloques_del_dia = [
            (b.hora_inicio, b.hora_fin) for b in horarios if b.dia_semana == fecha.weekday()
        ]

    for hora_inicio, hora_fin in descansos_del_dia(fecha, descansos or []):
        bloques_del_dia = _restar_intervalo(bloques_del_dia, hora_inicio, hora_fin)

    ventanas = []
    for hora_inicio, hora_fin in bloques_del_dia:
        inicio_local = datetime.combine(fecha, hora_inicio, tzinfo=zona)
        fin_local = datetime.combine(fecha, hora_fin, tzinfo=zona)
        ventanas.append((inicio_local.astimezone(timezone.utc), fin_local.astimezone(timezone.utc)))
    return ventanas


def dentro_de_horario(
    hora_inicio: datetime,
    hora_fin: datetime,
    horarios: list[BloqueHorario],
    excepciones: list[Excepcion],
    zona_horaria: str,
    descansos: list[Descanso] | None = None,
) -> bool:
    """
    True si [hora_inicio, hora_fin) cabe COMPLETO dentro de una sola ventana
    de atención del proveedor ese día (jornada semanal o excepción, con sus
    descansos ya restados).

    Un turno partido (dos BloqueHorario el mismo día, p.ej. 9-14 y 15-20) o
    un descanso configurado (p.ej. 13:00-14:00 dentro de un bloque 9-18)
    dejan un hueco que NO es ninguna ventana: una cita que lo cruza (13:30 a
    15:30, por ejemplo) se rechaza aunque sus dos extremos caigan, por
    separado, dentro de algún tramo. Por eso se compara contra cada ventana
    entera con `and`, no con "el inicio está en alguna ventana" por un lado
    y "el fin en otra" por el otro.
    """
    mapa_excepciones = {e.fecha: e for e in excepciones}
    fecha = hora_inicio.astimezone(ZoneInfo(zona_horaria)).date()
    ventanas = ventanas_del_dia(fecha, horarios, mapa_excepciones, zona_horaria, descansos)
    return any(inicio_v <= hora_inicio and hora_fin <= fin_v for inicio_v, fin_v in ventanas)


def generar_slots(
    fecha_desde: date,
    fecha_hasta: date,
    duracion_minutos: int,
    horarios: list[BloqueHorario],
    excepciones: list[Excepcion],
    ocupados: list[RangoOcupado],
    zona_horaria: str,
    ahora_utc: datetime | None = None,
    descansos: list[Descanso] | None = None,
) -> list[datetime]:
    """
    Horas de inicio (UTC) de cada slot libre de `duracion_minutos` dentro de
    las ventanas de atención del rango [fecha_desde, fecha_hasta], evitando
    traslape con `ocupados`, con los `descansos` configurados y, si se da
    `ahora_utc`, los que ya pasaron.

    El paso entre slots candidatos es `duracion_minutos` desde el inicio de
    cada ventana: no hay una grilla fija de 15/30 min independiente del
    servicio, así que un servicio de 45 min ofrece horas :00/:45/:30... en
    vez de forzar siempre a los cuartos de hora. Un slot que no cabe entero
    dentro de la ventana (el resto que sobra al final) no se ofrece.
    """
    mapa_excepciones = {e.fecha: e for e in excepciones}
    duracion = timedelta(minutes=duracion_minutos)
    slots: list[datetime] = []

    dia = fecha_desde
    while dia <= fecha_hasta:
        for inicio_ventana, fin_ventana in ventanas_del_dia(
            dia, horarios, mapa_excepciones, zona_horaria, descansos
        ):
            candidato = inicio_ventana
            while candidato + duracion <= fin_ventana:
                fin_candidato = candidato + duracion
                pasado = ahora_utc is not None and candidato < ahora_utc
                ocupado = any(
                    se_traslapan(candidato, fin_candidato, o.inicio, o.fin) for o in ocupados
                )
                if not pasado and not ocupado:
                    slots.append(candidato)
                candidato += duracion
        dia += timedelta(days=1)

    return slots
