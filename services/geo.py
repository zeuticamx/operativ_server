"""
Distancias y validación de geocerca.

Sin I/O a propósito: es la pieza que decide si un check-in cuenta como
visita real, y se quiere poder probarla sin base de datos.

La misma fórmula existe como función SQL (`distancia_metros`, en
07_crm_campo.sql) para reportes y consultas ad-hoc. Las dos usan el mismo
radio terrestre; `tests/test_geo.py` compara ambas contra los mismos puntos
para que no se separen con el tiempo.
"""

from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

# Radio medio de la Tierra en metros. El mismo número que usa la función
# SQL: si se cambia acá hay que cambiarlo allá.
RADIO_TIERRA_M = 6_371_000.0

LAT_MIN, LAT_MAX = -90.0, 90.0
LON_MIN, LON_MAX = -180.0, 180.0

# Holgura para la comparación con el radio, en metros.
#
# Haversine encadena senos, cosenos y una raíz, así que un punto que está
# exactamente sobre el radio puede salir en 120.00000000014 y quedar
# fuera por comparar floats. Un micrómetro es catorce órdenes de magnitud
# menos que la precisión de cualquier GPS: no cambia ningún veredicto
# real, solo evita que el borde dependa del redondeo binario.
_HOLGURA_M = 1e-6


class CoordenadaInvalidaError(ValueError):
    """Latitud o longitud fuera del rango geográfico posible."""

    def __init__(self, mensaje: str):
        super().__init__(mensaje)
        self.mensaje = mensaje


def validar_coordenada(latitud: float, longitud: float) -> None:
    """
    Rechaza coordenadas imposibles.

    Pydantic ya acota los valores que llegan por la API; esto cubre a quien
    llame a las funciones de acá desde otro lado (un recálculo, un script)
    y evita que una latitud de 900 devuelva una distancia sin sentido en
    vez de un error.
    """
    if not LAT_MIN <= latitud <= LAT_MAX:
        raise CoordenadaInvalidaError(
            f"Latitud fuera de rango: {latitud} (debe estar entre {LAT_MIN} y {LAT_MAX})"
        )
    if not LON_MIN <= longitud <= LON_MAX:
        raise CoordenadaInvalidaError(
            f"Longitud fuera de rango: {longitud} (debe estar entre {LON_MIN} y {LON_MAX})"
        )


def distancia_metros(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """
    Distancia en metros entre dos puntos sobre la superficie terrestre.

    Haversine sobre una esfera. Para las decenas o centenas de metros que
    mide una geocerca el error frente a un elipsoide es de centímetros,
    muy por debajo de la precisión del GPS de un teléfono.
    """
    validar_coordenada(lat1, lon1)
    validar_coordenada(lat2, lon2)

    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)

    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    )
    # asin(sqrt(a)) en vez de atan2: idéntico numéricamente en este rango y
    # es la forma exacta que usa la función SQL, para que no se separen.
    return 2 * RADIO_TIERRA_M * asin(sqrt(a))


@dataclass(frozen=True)
class ResultadoGeocerca:
    distancia_metros: float
    radio_metros: int
    dentro: bool

    @property
    def exceso_metros(self) -> float:
        """Cuánto se pasó del radio. 0 si quedó dentro."""
        if self.dentro:
            return 0.0
        return max(0.0, self.distancia_metros - self.radio_metros)


def evaluar_geocerca(
    lat_cliente: float,
    lon_cliente: float,
    lat_visita: float,
    lon_visita: float,
    radio_metros: int,
) -> ResultadoGeocerca:
    """
    Decide si un check-in cae dentro del radio permitido del cliente.

    El borde cuenta como dentro: el radio ya es una tolerancia, y dejar
    fuera una lectura que cayó justo en el límite castigaría al vendedor
    por el redondeo del GPS. La holgura de `_HOLGURA_M` está para que ese
    borde no dependa además del redondeo binario de la fórmula.
    """
    distancia = distancia_metros(lat_cliente, lon_cliente, lat_visita, lon_visita)
    return ResultadoGeocerca(
        distancia_metros=distancia,
        radio_metros=radio_metros,
        dentro=distancia <= radio_metros + _HOLGURA_M,
    )
