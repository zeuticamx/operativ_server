"""
Distancia y geocerca. Todo puro: no hace falta base de datos.

Los puntos de referencia son coordenadas reales de Ciudad de México, con
distancias verificables en un mapa.
"""

import pytest

from services.geo import (
    CoordenadaInvalidaError,
    RADIO_TIERRA_M,
    distancia_metros,
    evaluar_geocerca,
    validar_coordenada,
)

# Zócalo de la Ciudad de México.
ZOCALO = (19.432608, -99.133209)


def desplazar_metros_norte(lat: float, lon: float, metros: float) -> tuple[float, float]:
    """
    Mueve un punto hacia el norte. Sobre un meridiano, un grado de latitud
    son siempre los mismos metros, así que sirve para armar casos con
    distancia conocida sin depender de la longitud.
    """
    import math

    grados = metros / (RADIO_TIERRA_M * math.pi / 180)
    return lat + grados, lon


# ============================================================
# distancia_metros
# ============================================================
def test_el_mismo_punto_da_cero():
    assert distancia_metros(*ZOCALO, *ZOCALO) == pytest.approx(0.0, abs=1e-6)


def test_es_simetrica():
    a = (19.432608, -99.133209)
    b = (19.435000, -99.140000)
    assert distancia_metros(*a, *b) == pytest.approx(distancia_metros(*b, *a))


@pytest.mark.parametrize("metros", [10, 50, 120, 500, 2_000])
def test_reconstruye_una_distancia_conocida(metros):
    """Un desplazamiento de N metros al norte tiene que medir N metros."""
    destino = desplazar_metros_norte(*ZOCALO, metros)
    assert distancia_metros(*ZOCALO, *destino) == pytest.approx(metros, rel=1e-6)


def test_un_grado_de_latitud_son_unos_111_km():
    d = distancia_metros(0.0, 0.0, 1.0, 0.0)
    assert 111_100 < d < 111_200, d


def test_distancia_entre_ciudades():
    """CDMX a Guadalajara: ~460 km en línea recta."""
    guadalajara = (20.676667, -103.347222)
    d = distancia_metros(*ZOCALO, *guadalajara) / 1000
    assert 455 < d < 465, d


def test_cruzar_el_antimeridiano_no_da_la_vuelta_al_mundo():
    """
    Dos puntos a un grado de distancia sobre el meridiano 180 están cerca,
    aunque sus longitudes difieran en 359 grados.
    """
    d = distancia_metros(0.0, 179.5, 0.0, -179.5)
    assert d == pytest.approx(111_195, rel=0.01), d


# ============================================================
# Coordenadas inválidas
# ============================================================
@pytest.mark.parametrize(
    "lat, lon",
    [
        (91.0, 0.0),
        (-91.0, 0.0),
        (0.0, 181.0),
        (0.0, -181.0),
        (900.0, 900.0),
    ],
)
def test_coordenada_fuera_de_rango(lat, lon):
    with pytest.raises(CoordenadaInvalidaError):
        validar_coordenada(lat, lon)


@pytest.mark.parametrize(
    "lat, lon",
    [(90.0, 180.0), (-90.0, -180.0), (0.0, 0.0), (19.43, -99.13)],
)
def test_coordenada_en_el_limite_es_valida(lat, lon):
    validar_coordenada(lat, lon)


def test_distancia_rechaza_coordenadas_imposibles():
    with pytest.raises(CoordenadaInvalidaError):
        distancia_metros(19.43, -99.13, 91.0, 0.0)


# ============================================================
# evaluar_geocerca — DENTRO
# ============================================================
def test_checkin_en_el_punto_exacto_esta_dentro():
    r = evaluar_geocerca(*ZOCALO, *ZOCALO, radio_metros=120)
    assert r.dentro is True
    assert r.distancia_metros == pytest.approx(0.0, abs=1e-6)
    assert r.exceso_metros == 0.0


def test_checkin_cerca_esta_dentro_del_radio_por_defecto():
    """50 m del punto, con la tolerancia de 120 m que trae la tabla."""
    lat, lon = desplazar_metros_norte(*ZOCALO, 50)
    r = evaluar_geocerca(*ZOCALO, lat, lon, radio_metros=120)
    assert r.dentro is True
    assert r.distancia_metros == pytest.approx(50, rel=1e-3)
    assert r.exceso_metros == 0.0


def test_el_borde_cuenta_como_dentro():
    """
    Justo sobre el radio se acepta. El radio ya es una tolerancia; dejar
    fuera una lectura que cayó en el límite castigaría al vendedor por el
    redondeo del GPS.
    """
    lat, lon = desplazar_metros_norte(*ZOCALO, 120)
    r = evaluar_geocerca(*ZOCALO, lat, lon, radio_metros=120)
    assert r.dentro is True


def test_un_radio_grande_acepta_lo_que_uno_chico_rechaza():
    """El radio es por cliente: la misma lectura cambia de veredicto."""
    lat, lon = desplazar_metros_norte(*ZOCALO, 200)
    assert evaluar_geocerca(*ZOCALO, lat, lon, radio_metros=120).dentro is False
    assert evaluar_geocerca(*ZOCALO, lat, lon, radio_metros=300).dentro is True


# ============================================================
# evaluar_geocerca — FUERA
# ============================================================
def test_checkin_lejos_queda_fuera():
    lat, lon = desplazar_metros_norte(*ZOCALO, 500)
    r = evaluar_geocerca(*ZOCALO, lat, lon, radio_metros=120)
    assert r.dentro is False
    assert r.distancia_metros == pytest.approx(500, rel=1e-3)


def test_reporta_cuanto_se_paso_del_radio():
    lat, lon = desplazar_metros_norte(*ZOCALO, 300)
    r = evaluar_geocerca(*ZOCALO, lat, lon, radio_metros=120)
    assert r.exceso_metros == pytest.approx(180, rel=1e-2)


def test_un_metro_mas_alla_del_radio_ya_esta_fuera():
    lat, lon = desplazar_metros_norte(*ZOCALO, 121)
    r = evaluar_geocerca(*ZOCALO, lat, lon, radio_metros=120)
    assert r.dentro is False


def test_checkin_desde_otra_ciudad_queda_fuera():
    """El caso que el módulo existe para detectar."""
    guadalajara = (20.676667, -103.347222)
    r = evaluar_geocerca(*ZOCALO, *guadalajara, radio_metros=120)
    assert r.dentro is False
    assert r.exceso_metros > 400_000


def test_el_resultado_es_inmutable():
    """Nadie puede cambiar el veredicto después de calcularlo."""
    r = evaluar_geocerca(*ZOCALO, *ZOCALO, radio_metros=120)
    with pytest.raises(Exception):
        r.dentro = False  # type: ignore[misc]
