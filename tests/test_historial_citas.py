"""Núcleo puro del historial de citas (spec 002). Sin base de datos ni servidor."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from services.historial_citas import (
    CANALES_DE_CLIENTE,
    LIMITE_HISTORIAL,
    cita_es_elegible,
    es_canal_de_cliente,
    fecha_hora_local,
    interpretar_conversation_id,
    seleccionar_historial,
)

AHORA = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class CitaPrueba:
    etiqueta: str
    estado: str
    hora_inicio: datetime
    creado_en: datetime


def _cita(etiqueta: str, *, dias: float = -1, estado: str = "completada", creada_hace: float = 30) -> CitaPrueba:
    """`dias` se mide respecto a AHORA (negativo = pasado)."""
    return CitaPrueba(
        etiqueta=etiqueta,
        estado=estado,
        hora_inicio=AHORA + timedelta(days=dias),
        creado_en=AHORA - timedelta(days=creada_hace),
    )


# ------------------------------------------------------------
# cita_es_elegible (R2)
# ------------------------------------------------------------
def test_confirmada_a_un_segundo_en_el_futuro_no_es_elegible():
    inicio = AHORA + timedelta(seconds=1)
    assert cita_es_elegible(estado="confirmada", hora_inicio=inicio, ahora=AHORA) is False


def test_confirmada_igual_al_instante_es_elegible():
    assert cita_es_elegible(estado="confirmada", hora_inicio=AHORA, ahora=AHORA) is True


def test_confirmada_pasada_es_elegible():
    inicio = AHORA - timedelta(hours=2)
    assert cita_es_elegible(estado="confirmada", hora_inicio=inicio, ahora=AHORA) is True


def test_confirmada_futura_entra_en_una_consulta_posterior():
    inicio = AHORA + timedelta(seconds=1)
    assert cita_es_elegible(estado="confirmada", hora_inicio=inicio, ahora=AHORA) is False
    assert (
        cita_es_elegible(estado="confirmada", hora_inicio=inicio, ahora=AHORA + timedelta(seconds=1))
        is True
    )


@pytest.mark.parametrize("estado", ["cancelada", "completada", "no_asistio"])
def test_estados_cerrados_son_elegibles_aunque_el_inicio_sea_futuro(estado: str):
    inicio = AHORA + timedelta(days=30)
    assert cita_es_elegible(estado=estado, hora_inicio=inicio, ahora=AHORA) is True


def test_estado_desconocido_no_es_elegible():
    assert cita_es_elegible(estado="pendiente", hora_inicio=AHORA - timedelta(days=1), ahora=AHORA) is False


def test_comparacion_entre_zonas_distintas():
    # 06:00-06:00 = 12:00Z = AHORA exacto
    inicio = datetime.fromisoformat("2026-01-10T06:00:00-06:00")
    assert cita_es_elegible(estado="confirmada", hora_inicio=inicio, ahora=AHORA) is True


def test_instante_ingenuo_se_rechaza():
    with pytest.raises(ValueError):
        cita_es_elegible(estado="confirmada", hora_inicio=datetime(2026, 1, 1), ahora=AHORA)
    with pytest.raises(ValueError):
        cita_es_elegible(estado="confirmada", hora_inicio=AHORA, ahora=datetime(2026, 1, 1))


# ------------------------------------------------------------
# seleccionar_historial (R1, R2, R3)
# ------------------------------------------------------------
def test_filtra_las_no_elegibles():
    citas = [
        _cita("futura", dias=2, estado="confirmada"),
        _cita("pasada", dias=-2, estado="confirmada"),
    ]
    elegidas, hay_mas = seleccionar_historial(citas, ahora=AHORA)
    assert [c.etiqueta for c in elegidas] == ["pasada"]
    assert hay_mas is False


def test_criterio_2_a_las_12_00_00_y_12_00_01():
    citas = [
        CitaPrueba("c_mas_1s", "confirmada", AHORA + timedelta(seconds=1), AHORA - timedelta(days=5)),
        CitaPrueba("c_igual", "confirmada", AHORA, AHORA - timedelta(days=5)),
        CitaPrueba("cancelada_feb", "cancelada", datetime(2026, 2, 1, 10, tzinfo=timezone.utc), AHORA - timedelta(days=5)),
        CitaPrueba("completada_11", "completada", datetime(2026, 1, 11, 10, tzinfo=timezone.utc), AHORA - timedelta(days=5)),
    ]
    a_las_12_00_00, _ = seleccionar_historial(citas, ahora=AHORA)
    assert {c.etiqueta for c in a_las_12_00_00} == {"c_igual", "cancelada_feb", "completada_11"}

    a_las_12_00_01, _ = seleccionar_historial(citas, ahora=AHORA + timedelta(seconds=1))
    assert {c.etiqueta for c in a_las_12_00_01} == {"c_mas_1s", "c_igual", "cancelada_feb", "completada_11"}


def test_orden_por_inicio_descendente():
    citas = [_cita("a", dias=-5), _cita("b", dias=-1), _cita("c", dias=-3)]
    elegidas, _ = seleccionar_historial(citas, ahora=AHORA)
    assert [c.etiqueta for c in elegidas] == ["b", "c", "a"]


def test_desempate_por_creado_en_descendente():
    citas = [
        _cita("vieja", dias=-2, creada_hace=10),
        _cita("nueva", dias=-2, creada_hace=1),
        _cita("media", dias=-2, creada_hace=5),
    ]
    elegidas, _ = seleccionar_historial(citas, ahora=AHORA)
    assert [c.etiqueta for c in elegidas] == ["nueva", "media", "vieja"]


def test_exactamente_diez_no_hay_mas():
    citas = [_cita(f"c{i}", dias=-(i + 1)) for i in range(10)]
    elegidas, hay_mas = seleccionar_historial(citas, ahora=AHORA)
    assert len(elegidas) == 10
    assert hay_mas is False


def test_once_devuelve_diez_y_hay_mas():
    citas = [_cita(f"c{i}", dias=-(i + 1)) for i in range(11)]
    elegidas, hay_mas = seleccionar_historial(citas, ahora=AHORA)
    assert len(elegidas) == 10
    assert hay_mas is True
    # la descartada es la más antigua
    assert "c10" not in [c.etiqueta for c in elegidas]
    assert elegidas[0].etiqueta == "c0"


def test_doce_elegibles_con_estados_mezclados():
    estados = ["cancelada", "completada", "no_asistio", "confirmada"]
    citas = [_cita(f"c{i}", dias=-(i + 1), estado=estados[i % 4]) for i in range(12)]
    elegidas, hay_mas = seleccionar_historial(citas, ahora=AHORA)
    assert [c.etiqueta for c in elegidas] == [f"c{i}" for i in range(10)]
    assert hay_mas is True


def test_las_no_elegibles_no_cuentan_para_hay_mas():
    citas = [_cita(f"c{i}", dias=-(i + 1)) for i in range(10)]
    citas += [_cita(f"f{i}", dias=i + 1, estado="confirmada") for i in range(5)]
    elegidas, hay_mas = seleccionar_historial(citas, ahora=AHORA)
    assert len(elegidas) == 10
    assert hay_mas is False


def test_vacio():
    assert seleccionar_historial([], ahora=AHORA) == ([], False)


def test_limite_personalizado_y_defecto():
    assert LIMITE_HISTORIAL == 10
    citas = [_cita(f"c{i}", dias=-(i + 1)) for i in range(4)]
    elegidas, hay_mas = seleccionar_historial(citas, ahora=AHORA, limite=3)
    assert len(elegidas) == 3
    assert hay_mas is True


def test_no_modifica_la_lista_de_entrada():
    citas = [_cita("a", dias=-5), _cita("b", dias=-1)]
    copia = list(citas)
    seleccionar_historial(citas, ahora=AHORA)
    assert citas == copia


# ------------------------------------------------------------
# fecha_hora_local (R12, criterio 19)
# ------------------------------------------------------------
def test_hora_local_en_mexico_utc_menos_6():
    instante = datetime(2026, 1, 9, 20, 0, 0, tzinfo=timezone.utc)
    assert fecha_hora_local(instante, "America/Mexico_City") == "2026-01-09T14:00:00-06:00"


def test_hora_local_tras_cambiar_a_bogota():
    instante = datetime(2026, 1, 9, 20, 0, 0, tzinfo=timezone.utc)
    assert fecha_hora_local(instante, "America/Bogota") == "2026-01-09T15:00:00-05:00"


def test_hora_local_cruza_de_dia():
    instante = datetime(2026, 1, 10, 3, 30, 0, tzinfo=timezone.utc)
    assert fecha_hora_local(instante, "America/Mexico_City") == "2026-01-09T21:30:00-06:00"


def test_hora_local_desde_otra_zona_de_entrada():
    instante = datetime.fromisoformat("2026-01-09T15:00:00-05:00")
    assert fecha_hora_local(instante, "America/Mexico_City") == "2026-01-09T14:00:00-06:00"


def test_hora_local_zona_invalida_e_instante_ingenuo():
    with pytest.raises(ValueError):
        fecha_hora_local(AHORA, "No/Existe")
    with pytest.raises(ValueError):
        fecha_hora_local(datetime(2026, 1, 1), "America/Bogota")


# ------------------------------------------------------------
# interpretar_conversation_id (R6)
# ------------------------------------------------------------
UUID_VALIDO = "3f2b8c1e-5a4d-4e6f-9b7a-1c2d3e4f5a6b"


def test_uuid_valido_en_minusculas():
    assert interpretar_conversation_id(UUID_VALIDO) == UUID(UUID_VALIDO)


def test_uuid_valido_en_mayusculas_se_acepta_y_es_el_mismo():
    assert interpretar_conversation_id(UUID_VALIDO.upper()) == UUID(UUID_VALIDO)


@pytest.mark.parametrize(
    "valor",
    [
        None,
        "",
        "abc",
        "   ",
        123,
        1.5,
        True,
        False,
        {},
        {"id": UUID_VALIDO},
        [],
        [UUID_VALIDO],
        UUID_VALIDO.replace("-", ""),  # sin guiones
        "{" + UUID_VALIDO + "}",  # con llaves
        "urn:uuid:" + UUID_VALIDO,  # con prefijo urn:
        " " + UUID_VALIDO,  # espacios alrededor
        UUID_VALIDO + " ",
        UUID_VALIDO + "\n",
        UUID_VALIDO[:-1],  # corto
        UUID_VALIDO + "0",  # largo
        UUID_VALIDO.replace("a", "g", 1),  # carácter no hexadecimal
        UUID(UUID_VALIDO),  # un objeto UUID no es un texto del cuerpo JSON
        UUID_VALIDO.encode(),
    ],
)
def test_conversation_id_invalido_da_none(valor: object):
    assert interpretar_conversation_id(valor) is None


# ------------------------------------------------------------
# canales (R6)
# ------------------------------------------------------------
def test_canales_de_cliente():
    assert CANALES_DE_CLIENTE == frozenset({"whatsapp", "instagram", "facebook"})


@pytest.mark.parametrize("canal", ["whatsapp", "instagram", "facebook"])
def test_canales_admitidos(canal: str):
    assert es_canal_de_cliente(canal) is True


@pytest.mark.parametrize("canal", [None, "", "web", "widget", "WhatsApp", "telegram", " whatsapp"])
def test_canales_no_admitidos(canal: str | None):
    assert es_canal_de_cliente(canal) is False
