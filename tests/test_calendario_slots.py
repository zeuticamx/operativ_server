"""Cálculo de slots de disponibilidad. Todo puro: no hace falta base de datos."""

from datetime import date, datetime, time, timedelta, timezone

import pytest

from services.calendario_slots import (
    BloqueHorario,
    Descanso,
    Excepcion,
    RangoOcupado,
    dentro_de_horario,
    descanso_dentro_de_jornada,
    generar_slots,
    se_traslapan,
    ventanas_del_dia,
)

ZONA = "America/Mexico_City"  # UTC-6 todo el año desde que México abolió el horario de verano en 2022.


def _utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


# ------------------------------------------------------------
# se_traslapan
# ------------------------------------------------------------
@pytest.mark.parametrize(
    "inicio_a, fin_a, inicio_b, fin_b, esperado",
    [
        # Se tocan en el borde pero no comparten instante: [10,11) y [11,12).
        (_utc(2026, 1, 5, 10), _utc(2026, 1, 5, 11), _utc(2026, 1, 5, 11), _utc(2026, 1, 5, 12), False),
        # Traslape parcial.
        (_utc(2026, 1, 5, 10), _utc(2026, 1, 5, 11), _utc(2026, 1, 5, 10, 30), _utc(2026, 1, 5, 11, 30), True),
        # Uno contiene por completo al otro.
        (_utc(2026, 1, 5, 9), _utc(2026, 1, 5, 12), _utc(2026, 1, 5, 10), _utc(2026, 1, 5, 11), True),
        # Rangos idénticos.
        (_utc(2026, 1, 5, 9), _utc(2026, 1, 5, 10), _utc(2026, 1, 5, 9), _utc(2026, 1, 5, 10), True),
        # Completamente separados.
        (_utc(2026, 1, 5, 9), _utc(2026, 1, 5, 10), _utc(2026, 1, 5, 11), _utc(2026, 1, 5, 12), False),
    ],
)
def test_se_traslapan(inicio_a, fin_a, inicio_b, fin_b, esperado):
    assert se_traslapan(inicio_a, fin_a, inicio_b, fin_b) is esperado


# ------------------------------------------------------------
# ventanas_del_dia
# ------------------------------------------------------------
def test_ventana_semanal_se_convierte_a_utc():
    # Lunes 5 de enero de 2026 es lunes -> weekday() == 0.
    lunes = date(2026, 1, 5)
    horarios = [BloqueHorario(dia_semana=0, hora_inicio=time(9, 0), hora_fin=time(18, 0))]
    ventanas = ventanas_del_dia(lunes, horarios, {}, ZONA)
    assert ventanas == [(_utc(2026, 1, 5, 15, 0), _utc(2026, 1, 6, 0, 0))]


def test_dia_sin_bloque_semanal_no_tiene_ventanas():
    domingo = date(2026, 1, 4)
    horarios = [BloqueHorario(dia_semana=0, hora_inicio=time(9, 0), hora_fin=time(18, 0))]
    assert ventanas_del_dia(domingo, horarios, {}, ZONA) == []


def test_turno_partido_dos_ventanas_el_mismo_dia():
    lunes = date(2026, 1, 5)
    horarios = [
        BloqueHorario(dia_semana=0, hora_inicio=time(9, 0), hora_fin=time(14, 0)),
        BloqueHorario(dia_semana=0, hora_inicio=time(16, 0), hora_fin=time(20, 0)),
    ]
    ventanas = ventanas_del_dia(lunes, horarios, {}, ZONA)
    assert len(ventanas) == 2


def test_excepcion_no_disponible_anula_el_horario_semanal():
    lunes = date(2026, 1, 5)
    horarios = [BloqueHorario(dia_semana=0, hora_inicio=time(9, 0), hora_fin=time(18, 0))]
    excepciones = {lunes: Excepcion(fecha=lunes, disponible=False, hora_inicio=None, hora_fin=None)}
    assert ventanas_del_dia(lunes, horarios, excepciones, ZONA) == []


def test_excepcion_disponible_reemplaza_al_horario_semanal_no_lo_combina():
    lunes = date(2026, 1, 5)
    horarios = [BloqueHorario(dia_semana=0, hora_inicio=time(9, 0), hora_fin=time(18, 0))]
    excepciones = {
        lunes: Excepcion(fecha=lunes, disponible=True, hora_inicio=time(10, 0), hora_fin=time(12, 0))
    }
    ventanas = ventanas_del_dia(lunes, horarios, excepciones, ZONA)
    assert ventanas == [(_utc(2026, 1, 5, 16, 0), _utc(2026, 1, 5, 18, 0))]


# ------------------------------------------------------------
# generar_slots
# ------------------------------------------------------------
def test_slots_de_una_ventana_completa():
    lunes = date(2026, 1, 5)
    horarios = [BloqueHorario(dia_semana=0, hora_inicio=time(9, 0), hora_fin=time(11, 0))]
    slots = generar_slots(lunes, lunes, 30, horarios, [], [], ZONA)
    # 9-11 local en bloques de 30 min: 4 slots.
    assert len(slots) == 4
    assert slots[0] == _utc(2026, 1, 5, 15, 0)
    assert slots[-1] == _utc(2026, 1, 5, 16, 30)


def test_duracion_que_no_divide_exacto_descarta_el_sobrante():
    lunes = date(2026, 1, 5)
    horarios = [BloqueHorario(dia_semana=0, hora_inicio=time(9, 0), hora_fin=time(10, 40))]
    # 100 minutos de ventana, slots de 45: caben 2 (90 min), sobran 10 sin ofrecer.
    slots = generar_slots(lunes, lunes, 45, horarios, [], [], ZONA)
    assert len(slots) == 2


def test_reserva_ocupada_recorta_exactamente_ese_slot():
    lunes = date(2026, 1, 5)
    horarios = [BloqueHorario(dia_semana=0, hora_inicio=time(9, 0), hora_fin=time(11, 0))]
    # El segundo slot (9:30-10:00 local = 15:30-16:00 UTC) está ocupado.
    ocupados = [RangoOcupado(inicio=_utc(2026, 1, 5, 15, 30), fin=_utc(2026, 1, 5, 16, 0))]
    slots = generar_slots(lunes, lunes, 30, horarios, [], ocupados, ZONA)
    assert _utc(2026, 1, 5, 15, 30) not in slots
    assert len(slots) == 3


def test_traslape_parcial_tambien_excluye_el_slot():
    lunes = date(2026, 1, 5)
    horarios = [BloqueHorario(dia_semana=0, hora_inicio=time(9, 0), hora_fin=time(10, 0))]
    # Ocupa de 9:15 a 9:45 local, que traslapa parcialmente los dos slots de 30 min.
    ocupados = [RangoOcupado(inicio=_utc(2026, 1, 5, 15, 15), fin=_utc(2026, 1, 5, 15, 45))]
    slots = generar_slots(lunes, lunes, 30, horarios, [], ocupados, ZONA)
    assert slots == []


def test_ahora_utc_descarta_slots_pasados_y_deja_los_futuros():
    lunes = date(2026, 1, 5)
    horarios = [BloqueHorario(dia_semana=0, hora_inicio=time(9, 0), hora_fin=time(11, 0))]
    ahora = _utc(2026, 1, 5, 15, 45)  # 9:45 local: el primer slot (9:00) ya pasó.
    slots = generar_slots(lunes, lunes, 30, horarios, [], [], ZONA, ahora_utc=ahora)
    assert _utc(2026, 1, 5, 15, 0) not in slots
    assert _utc(2026, 1, 5, 16, 0) in slots


def test_rango_de_varios_dias_junta_los_slots_de_cada_uno():
    horarios = [BloqueHorario(dia_semana=d, hora_inicio=time(9, 0), hora_fin=time(10, 0)) for d in range(5)]
    slots = generar_slots(date(2026, 1, 5), date(2026, 1, 6), 60, horarios, [], [], ZONA)
    assert slots == [_utc(2026, 1, 5, 15, 0), _utc(2026, 1, 6, 15, 0)]


def test_sin_horario_ni_excepcion_no_hay_slots():
    lunes = date(2026, 1, 5)
    assert generar_slots(lunes, lunes, 30, [], [], [], ZONA) == []


# ------------------------------------------------------------
# dentro_de_horario — jornada laboral del barbero (historia de usuario)
# ------------------------------------------------------------
HORARIO_10_A_19 = [BloqueHorario(dia_semana=0, hora_inicio=time(10, 0), hora_fin=time(19, 0))]


def test_cita_completa_dentro_de_la_jornada_es_valida():
    # Escenario 1: jornada 10:00-19:00, cita de 10:00 a 10:30.
    assert dentro_de_horario(
        _utc(2026, 1, 5, 16, 0), _utc(2026, 1, 5, 16, 30), HORARIO_10_A_19, [], ZONA
    ) is True


def test_cita_antes_de_que_abra_es_invalida():
    # 09:30 local es antes de las 10:00: fuera de la jornada.
    assert dentro_de_horario(
        _utc(2026, 1, 5, 15, 30), _utc(2026, 1, 5, 16, 0), HORARIO_10_A_19, [], ZONA
    ) is False


def test_cita_que_excede_el_fin_de_turno_es_invalida():
    # Escenario 2: jornada termina a las 19:00, servicio de 45 min a las 18:30
    # terminaría a las 19:15 -> no cabe entera en la ventana.
    inicio = _utc(2026, 1, 6, 0, 30)  # 18:30 CDMX = 00:30 UTC del día siguiente
    fin = inicio + timedelta(minutes=45)
    assert dentro_de_horario(inicio, fin, HORARIO_10_A_19, [], ZONA) is False


def test_cita_que_termina_justo_al_cierre_es_valida():
    # Límite exacto: 18:30-19:00 sí cabe (el cierre es un límite inclusivo).
    inicio = _utc(2026, 1, 6, 0, 30)  # 18:30 CDMX
    fin = _utc(2026, 1, 6, 1, 0)  # 19:00 CDMX
    assert dentro_de_horario(inicio, fin, HORARIO_10_A_19, [], ZONA) is True


def test_cita_dentro_de_la_pausa_de_turno_partido_es_invalida():
    # Escenario 4: turno partido 10-14 y 15-19 (pausa de 14 a 15). Una cita
    # de 13:30 a 14:30 cruza el hueco: no cabe entera en NINGUNA ventana,
    # aunque cada extremo por separado caiga dentro de algún bloque.
    horarios = [
        BloqueHorario(dia_semana=0, hora_inicio=time(10, 0), hora_fin=time(14, 0)),
        BloqueHorario(dia_semana=0, hora_inicio=time(15, 0), hora_fin=time(19, 0)),
    ]
    inicio = _utc(2026, 1, 5, 19, 30)  # 13:30 CDMX
    fin = _utc(2026, 1, 5, 20, 30)  # 14:30 CDMX
    assert dentro_de_horario(inicio, fin, horarios, [], ZONA) is False


def test_cita_dentro_del_segundo_bloque_del_turno_partido_es_valida():
    horarios = [
        BloqueHorario(dia_semana=0, hora_inicio=time(10, 0), hora_fin=time(14, 0)),
        BloqueHorario(dia_semana=0, hora_inicio=time(15, 0), hora_fin=time(19, 0)),
    ]
    inicio = _utc(2026, 1, 5, 21, 0)  # 15:00 CDMX
    fin = _utc(2026, 1, 5, 22, 0)  # 16:00 CDMX
    assert dentro_de_horario(inicio, fin, horarios, [], ZONA) is True


def test_dia_sin_ninguna_ventana_rechaza_cualquier_cita():
    domingo = date(2026, 1, 4)
    inicio = datetime.combine(domingo, time(12, 0), tzinfo=timezone.utc)
    fin = datetime.combine(domingo, time(12, 30), tzinfo=timezone.utc)
    assert dentro_de_horario(inicio, fin, HORARIO_10_A_19, [], ZONA) is False


def test_excepcion_no_disponible_rechaza_incluso_dentro_del_horario_semanal():
    lunes = date(2026, 1, 5)
    excepciones = [Excepcion(fecha=lunes, disponible=False, hora_inicio=None, hora_fin=None)]
    assert dentro_de_horario(
        _utc(2026, 1, 5, 16, 0), _utc(2026, 1, 5, 16, 30), HORARIO_10_A_19, excepciones, ZONA
    ) is False


# ------------------------------------------------------------
# Descansos (pausas dentro de la jornada) — historia de usuario del barbero
# ------------------------------------------------------------
def test_descanso_recurrente_parte_la_ventana_en_dos():
    # Jornada 10-19 con un descanso de 13-14: mismo resultado que configurar
    # un turno partido a mano, pero acá se calcula solo.
    lunes = date(2026, 1, 5)
    descansos = [Descanso(dia_semana=0, fecha=None, hora_inicio=time(13, 0), hora_fin=time(14, 0))]
    ventanas = ventanas_del_dia(lunes, HORARIO_10_A_19, {}, ZONA, descansos)
    assert ventanas == [
        (_utc(2026, 1, 5, 16, 0), _utc(2026, 1, 5, 19, 0)),  # 10-13 CDMX
        (_utc(2026, 1, 5, 20, 0), _utc(2026, 1, 6, 1, 0)),  # 14-19 CDMX
    ]


def test_cita_que_cruza_el_descanso_es_invalida():
    # Escenario 4: descanso 14:00-14:45 (CDMX), un servicio de 45 min que
    # arranca a las 13:30 termina a las 14:15 -> cruza el descanso.
    descansos = [Descanso(dia_semana=0, fecha=None, hora_inicio=time(14, 0), hora_fin=time(14, 45))]
    inicio = _utc(2026, 1, 5, 19, 30)  # 13:30 CDMX
    fin = _utc(2026, 1, 5, 20, 15)  # 14:15 CDMX
    assert dentro_de_horario(inicio, fin, HORARIO_10_A_19, [], ZONA, descansos) is False


def test_cita_despues_del_descanso_es_valida():
    descansos = [Descanso(dia_semana=0, fecha=None, hora_inicio=time(13, 0), hora_fin=time(14, 0))]
    inicio = _utc(2026, 1, 5, 20, 0)  # 14:00 CDMX
    fin = _utc(2026, 1, 5, 21, 0)  # 15:00 CDMX
    assert dentro_de_horario(inicio, fin, HORARIO_10_A_19, [], ZONA, descansos) is True


def test_descanso_puntual_no_aplica_otros_dias():
    # El descanso es solo para el martes 6, no afecta al lunes 5.
    martes = date(2026, 1, 6)
    descansos = [
        Descanso(dia_semana=None, fecha=martes, hora_inicio=time(13, 0), hora_fin=time(14, 0))
    ]
    lunes = date(2026, 1, 5)
    horarios = [
        BloqueHorario(dia_semana=0, hora_inicio=time(10, 0), hora_fin=time(19, 0)),
        BloqueHorario(dia_semana=1, hora_inicio=time(10, 0), hora_fin=time(19, 0)),
    ]
    assert ventanas_del_dia(lunes, horarios, {}, ZONA, descansos) == [
        (_utc(2026, 1, 5, 16, 0), _utc(2026, 1, 6, 1, 0)),  # 10-19 CDMX, sin cortar
    ]
    ventanas_martes = ventanas_del_dia(martes, horarios, {}, ZONA, descansos)
    assert ventanas_martes == [
        (_utc(2026, 1, 6, 16, 0), _utc(2026, 1, 6, 19, 0)),
        (_utc(2026, 1, 6, 20, 0), _utc(2026, 1, 7, 1, 0)),
    ]


def test_dos_descansos_el_mismo_dia_se_restan_ambos():
    # Pausa matutina de 15 min + almuerzo de 60 min, mismo día: los técnicos
    # de negocio piden soportar más de un descanso diario.
    descansos = [
        Descanso(dia_semana=0, fecha=None, hora_inicio=time(11, 0), hora_fin=time(11, 15)),
        Descanso(dia_semana=0, fecha=None, hora_inicio=time(13, 0), hora_fin=time(14, 0)),
    ]
    lunes = date(2026, 1, 5)
    ventanas = ventanas_del_dia(lunes, HORARIO_10_A_19, {}, ZONA, descansos)
    assert len(ventanas) == 3


def test_descanso_dentro_de_jornada_true_cuando_cabe_completo():
    ventanas_base = [(time(9, 0), time(18, 0))]
    assert descanso_dentro_de_jornada(time(13, 0), time(14, 0), ventanas_base) is True


def test_descanso_dentro_de_jornada_false_si_empieza_antes_de_la_apertura():
    ventanas_base = [(time(9, 0), time(18, 0))]
    assert descanso_dentro_de_jornada(time(8, 30), time(9, 30), ventanas_base) is False


def test_descanso_dentro_de_jornada_false_si_termina_despues_del_cierre():
    ventanas_base = [(time(9, 0), time(18, 0))]
    assert descanso_dentro_de_jornada(time(17, 30), time(18, 30), ventanas_base) is False


def test_descanso_dentro_de_jornada_se_mide_contra_la_jornada_no_contra_otro_descanso():
    # Un segundo descanso (15:00-15:15) se valida contra la jornada
    # completa 9-18, no contra lo que ya recortó el primero (13-14): no
    # debe rechazarse solo porque ventanas_del_dia ya tiene un hueco antes.
    ventanas_base = [(time(9, 0), time(18, 0))]
    assert descanso_dentro_de_jornada(time(15, 0), time(15, 15), ventanas_base) is True
