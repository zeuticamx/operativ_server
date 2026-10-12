"""Regla de cancelación de citas (spec 001). Todo puro: sin base de datos ni servidor."""

from datetime import date, datetime, timedelta, timezone

import pytest

from services.cancelacion_citas import (
    evaluar_cancelacion,
    periodo_local,
    segundos_hasta,
    texto_alerta_tardia,
    validar_rango_reporte,
)

AHORA = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)


def _evaluar(
    *,
    inicio: datetime,
    antelacion: int | None = 24,
    origen: str = "cliente",
    estado: str = "confirmada",
):
    return evaluar_cancelacion(
        estado=estado,
        hora_inicio=inicio,
        antelacion_horas=antelacion,
        origen=origen,  # type: ignore[arg-type]
        ahora=AHORA,
    )


# ------------------------------------------------------------
# segundos_hasta
# ------------------------------------------------------------
def test_segundos_hasta_futuro_pasado_e_igual():
    assert segundos_hasta(AHORA + timedelta(hours=3), AHORA) == 10800
    assert segundos_hasta(AHORA - timedelta(minutes=1), AHORA) == -60
    assert segundos_hasta(AHORA, AHORA) == 0


def test_segundos_hasta_trunca_hacia_cero():
    assert segundos_hasta(AHORA + timedelta(seconds=1.9), AHORA) == 1
    assert segundos_hasta(AHORA - timedelta(seconds=1.9), AHORA) == -1


def test_segundos_hasta_entre_zonas_distintas():
    inicio = datetime.fromisoformat("2026-01-10T09:00:00-06:00")  # 15:00Z
    assert segundos_hasta(inicio, AHORA) == 10800


def test_instante_ingenuo_se_rechaza():
    with pytest.raises(ValueError):
        segundos_hasta(datetime(2026, 1, 10, 15, 0), AHORA)
    with pytest.raises(ValueError):
        _evaluar(inicio=datetime(2026, 1, 10, 15, 0))


# ------------------------------------------------------------
# Regla de tardía (R14-R16)
# ------------------------------------------------------------
def test_cliente_dentro_del_plazo_es_tardia():
    ev = _evaluar(inicio=AHORA + timedelta(hours=3))
    assert ev.puede_cancelar is True
    assert ev.motivo_no_cancelable is None
    assert ev.tardia is True
    assert ev.segundos_restantes == 10800
    assert ev.antelacion_minima_horas == 24


def test_cliente_con_holgura_no_es_tardia():
    ev = _evaluar(inicio=AHORA + timedelta(hours=30))
    assert ev.puede_cancelar is True
    assert ev.tardia is False


def test_limite_exacto_no_es_tardia():
    ev = _evaluar(inicio=AHORA + timedelta(hours=24))
    assert ev.puede_cancelar is True
    assert ev.tardia is False


def test_un_segundo_menos_del_limite_es_tardia():
    ev = _evaluar(inicio=AHORA + timedelta(hours=24) - timedelta(seconds=1))
    assert ev.tardia is True
    assert ev.segundos_restantes == 24 * 3600 - 1


def test_fraccion_de_segundo_bajo_el_limite_es_tardia():
    # La comparación es exacta: no se redondea ni se trunca antes de comparar.
    ev = _evaluar(inicio=AHORA + timedelta(hours=24) - timedelta(milliseconds=500))
    assert ev.tardia is True
    assert ev.segundos_restantes == 24 * 3600 - 1  # truncado hacia cero


def test_sin_regla_nunca_es_tardia():
    ev = _evaluar(inicio=AHORA + timedelta(seconds=1), antelacion=None)
    assert ev.puede_cancelar is True
    assert ev.tardia is False
    assert ev.antelacion_minima_horas is None


def test_antelacion_cero_ninguna_futura_es_tardia():
    ev = _evaluar(inicio=AHORA + timedelta(seconds=1), antelacion=0)
    assert ev.puede_cancelar is True
    assert ev.tardia is False
    assert ev.antelacion_minima_horas == 0


def test_antelacion_maxima():
    assert _evaluar(inicio=AHORA + timedelta(hours=719), antelacion=720).tardia is True
    assert _evaluar(inicio=AHORA + timedelta(hours=720), antelacion=720).tardia is False


# ------------------------------------------------------------
# Cita iniciada (R13)
# ------------------------------------------------------------
def test_inicio_igual_a_ahora_es_cita_iniciada():
    ev = _evaluar(inicio=AHORA)
    assert ev.puede_cancelar is False
    assert ev.motivo_no_cancelable == "cita_ya_iniciada"
    assert ev.segundos_restantes == 0
    assert ev.tardia is False


def test_inicio_pasado_es_cita_iniciada_con_segundos_negativos():
    ev = _evaluar(inicio=AHORA - timedelta(minutes=1))
    assert ev.puede_cancelar is False
    assert ev.motivo_no_cancelable == "cita_ya_iniciada"
    assert ev.segundos_restantes == -60


def test_medio_segundo_antes_del_inicio_todavia_no_empezo():
    ev = _evaluar(inicio=AHORA + timedelta(milliseconds=500), antelacion=None)
    assert ev.puede_cancelar is True
    assert ev.segundos_restantes == 0


def test_inicio_pasado_sin_regla_sigue_siendo_iniciada():
    ev = _evaluar(inicio=AHORA - timedelta(hours=1), antelacion=None)
    assert ev.motivo_no_cancelable == "cita_ya_iniciada"


# ------------------------------------------------------------
# Estado (R12) y precedencia
# ------------------------------------------------------------
@pytest.mark.parametrize("estado", ["cancelada", "completada", "no_asistio", "pendiente"])
@pytest.mark.parametrize("origen", ["cliente", "equipo"])
def test_estado_distinto_de_confirmada_no_se_cancela(estado: str, origen: str):
    ev = _evaluar(inicio=AHORA + timedelta(hours=3), estado=estado, origen=origen)
    assert ev.puede_cancelar is False
    assert ev.motivo_no_cancelable == "reserva_no_confirmada"
    assert ev.tardia is False


def test_precedencia_estado_sobre_inicio():
    ev = _evaluar(inicio=AHORA - timedelta(hours=2), estado="cancelada")
    assert ev.motivo_no_cancelable == "reserva_no_confirmada"
    assert ev.segundos_restantes == -7200


# ------------------------------------------------------------
# Equipo (R17)
# ------------------------------------------------------------
def test_equipo_nunca_es_tardia():
    ev = _evaluar(inicio=AHORA + timedelta(hours=3), origen="equipo")
    assert ev.puede_cancelar is True
    assert ev.tardia is False


def test_equipo_puede_cancelar_cita_ya_iniciada_si_sigue_confirmada():
    ev = _evaluar(inicio=AHORA - timedelta(hours=1), origen="equipo")
    assert ev.puede_cancelar is True
    assert ev.motivo_no_cancelable is None
    assert ev.tardia is False
    assert ev.segundos_restantes == -3600


def test_origen_invalido_se_rechaza():
    with pytest.raises(ValueError):
        _evaluar(inicio=AHORA + timedelta(hours=3), origen="tercero")


def test_la_evaluacion_es_inmutable():
    ev = _evaluar(inicio=AHORA + timedelta(hours=3))
    with pytest.raises(Exception):
        ev.tardia = False  # type: ignore[misc]


# ------------------------------------------------------------
# Verano: la antelación se mide en instantes absolutos (spec, casos límite)
# ------------------------------------------------------------
def test_cambio_de_horario_de_verano_no_desplaza_una_hora():
    # America/New_York pasa a horario de verano el 2026-03-08 a las 02:00 locales.
    ahora = datetime.fromisoformat("2026-03-07T12:00:00-05:00")
    inicio = datetime.fromisoformat("2026-03-08T12:00:00-04:00")  # 23 h absolutas después
    ev = evaluar_cancelacion(
        estado="confirmada", hora_inicio=inicio, antelacion_horas=24, origen="cliente", ahora=ahora
    )
    assert ev.segundos_restantes == 23 * 3600
    assert ev.tardia is True


# ------------------------------------------------------------
# texto_alerta_tardia (R22)
# ------------------------------------------------------------
def test_texto_tres_horas_exactas():
    assert texto_alerta_tardia(10800, 24) == "(cancelación tardía: faltaban 3 h 0 min, límite 24 h)"


def test_texto_trunca_23_59_59():
    assert (
        texto_alerta_tardia(24 * 3600 - 1, 24)
        == "(cancelación tardía: faltaban 23 h 59 min, límite 24 h)"
    )


def test_texto_menos_de_un_minuto_y_cero():
    assert texto_alerta_tardia(59, 2) == "(cancelación tardía: faltaban 0 h 0 min, límite 2 h)"
    assert texto_alerta_tardia(0, 2) == "(cancelación tardía: faltaban 0 h 0 min, límite 2 h)"


def test_texto_horas_grandes():
    assert (
        texto_alerta_tardia(719 * 3600 + 30 * 60, 720)
        == "(cancelación tardía: faltaban 719 h 30 min, límite 720 h)"
    )


def test_texto_no_muestra_negativos():
    assert texto_alerta_tardia(-30, 24) == "(cancelación tardía: faltaban 0 h 0 min, límite 24 h)"


# ------------------------------------------------------------
# periodo_local (R40)
# ------------------------------------------------------------
def test_periodo_borde_de_medianoche_local_mexico():
    # Spec, criterio 17: 23:30 y 00:30 locales (-06:00) caen en días distintos.
    antes = datetime.fromisoformat("2026-01-10T23:30:00-06:00")
    despues = datetime.fromisoformat("2026-01-11T00:30:00-06:00")
    assert periodo_local(antes, "America/Mexico_City", "dia") == "2026-01-10"
    assert periodo_local(despues, "America/Mexico_City", "dia") == "2026-01-11"
    assert periodo_local(antes, "America/Mexico_City", "mes") == "2026-01"
    assert periodo_local(despues, "America/Mexico_City", "mes") == "2026-01"


def test_periodo_mismo_instante_en_dos_zonas():
    instante = datetime(2026, 1, 11, 5, 30, tzinfo=timezone.utc)
    assert periodo_local(instante, "America/Mexico_City", "dia") == "2026-01-10"  # 23:30 local
    assert periodo_local(instante, "Europe/Madrid", "dia") == "2026-01-11"  # 06:30 local


def test_periodo_cambio_de_mes_depende_de_la_zona():
    instante = datetime(2026, 2, 1, 2, 0, tzinfo=timezone.utc)
    assert periodo_local(instante, "America/Mexico_City", "mes") == "2026-01"
    assert periodo_local(instante, "UTC", "mes") == "2026-02"


def test_periodo_una_cancelacion_no_cuenta_en_dos_periodos():
    instante = datetime(2026, 1, 10, 23, 59, 59, tzinfo=timezone.utc)
    assert periodo_local(instante, "UTC", "dia") == "2026-01-10"
    assert periodo_local(instante + timedelta(seconds=1), "UTC", "dia") == "2026-01-11"


def test_periodo_rechaza_entradas_invalidas():
    instante = datetime(2026, 1, 10, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        periodo_local(instante, "UTC", "semana")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        periodo_local(instante, "No/Existe", "dia")
    with pytest.raises(ValueError):
        periodo_local(datetime(2026, 1, 10), "UTC", "dia")


# ------------------------------------------------------------
# validar_rango_reporte (R41)
# ------------------------------------------------------------
HOY = date(2026, 1, 10)


def test_rango_por_defecto_son_los_ultimos_30_dias():
    desde, hasta = validar_rango_reporte(None, None, HOY)
    assert hasta == HOY
    assert desde == date(2025, 12, 12)
    assert (hasta - desde).days + 1 == 30


def test_rango_explicito_se_respeta():
    assert validar_rango_reporte(date(2026, 1, 1), date(2026, 1, 5), HOY) == (
        date(2026, 1, 1),
        date(2026, 1, 5),
    )


def test_rango_un_solo_dia_es_valido():
    assert validar_rango_reporte(HOY, HOY, HOY) == (HOY, HOY)


def test_rango_solo_desde_o_solo_hasta():
    assert validar_rango_reporte(date(2026, 1, 1), None, HOY) == (date(2026, 1, 1), HOY)
    desde, hasta = validar_rango_reporte(None, date(2026, 1, 31), HOY)
    assert hasta == date(2026, 1, 31)
    assert (hasta - desde).days + 1 == 30


def test_rango_desde_posterior_a_hasta_falla():
    with pytest.raises(ValueError):
        validar_rango_reporte(date(2026, 1, 5), date(2026, 1, 4), HOY)


def test_rango_de_366_dias_pasa_y_367_falla():
    hasta = date(2026, 12, 31)
    desde_366 = hasta - timedelta(days=365)
    assert validar_rango_reporte(desde_366, hasta, HOY) == (desde_366, hasta)
    with pytest.raises(ValueError):
        validar_rango_reporte(desde_366 - timedelta(days=1), hasta, HOY)
