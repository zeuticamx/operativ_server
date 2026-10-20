"""
Tope de intentos fallidos de login, por correo y por IP.

En memoria y de un solo proceso, igual que realtime.py: el despliegue actual
es una sola instancia. Si el backend llega a correr en varios procesos hay que
llevar esto a la base (cada proceso contaría por su cuenta y el tope real
sería N veces mayor). Un reinicio lo pone a cero: es un freno contra fuerza
bruta, no un registro de auditoría.
"""

import time
from collections import defaultdict, deque

MAX_FALLOS_POR_CORREO = 5
MAX_FALLOS_POR_IP = 20
VENTANA_SEGUNDOS = 15 * 60
# Tope de claves guardadas: sin él, un atacante que rota correos inventados
# haría crecer el diccionario sin límite.
MAX_CLAVES = 50_000

_fallos: dict[str, deque[float]] = defaultdict(deque)


def _purgar(clave: str, ahora: float) -> deque[float]:
    cola = _fallos[clave]
    while cola and ahora - cola[0] > VENTANA_SEGUNDOS:
        cola.popleft()
    if not cola:
        _fallos.pop(clave, None)
    return cola


def _claves(correo: str, ip: str | None) -> tuple[str, str | None]:
    return f"c:{correo.strip().lower()}", (f"i:{ip}" if ip else None)


def segundos_de_espera(correo: str, ip: str | None, ahora: float | None = None) -> int:
    """0 si puede intentar; si no, cuántos segundos faltan para el próximo intento."""
    ahora = time.monotonic() if ahora is None else ahora
    k_correo, k_ip = _claves(correo, ip)
    espera = 0
    for clave, tope in ((k_correo, MAX_FALLOS_POR_CORREO), (k_ip, MAX_FALLOS_POR_IP)):
        if clave is None:
            continue
        cola = _purgar(clave, ahora)
        if len(cola) >= tope:
            espera = max(espera, int(VENTANA_SEGUNDOS - (ahora - cola[0])) + 1)
    return espera


def registrar_fallo(correo: str, ip: str | None, ahora: float | None = None) -> None:
    ahora = time.monotonic() if ahora is None else ahora
    if len(_fallos) >= MAX_CLAVES:
        for clave in list(_fallos):
            _purgar(clave, ahora)
        if len(_fallos) >= MAX_CLAVES:
            return  # saturado: no se guarda más; los topes existentes siguen valiendo
    for clave in _claves(correo, ip):
        if clave is not None:
            _fallos[clave].append(ahora)


def limpiar_correo(correo: str) -> None:
    """Un login correcto borra los fallos del correo (no los de la IP)."""
    _fallos.pop(_claves(correo, None)[0], None)


def reiniciar() -> None:
    """Para las pruebas."""
    _fallos.clear()
