"""
Núcleo puro del historial de citas de un cliente (spec 002).

Sin I/O a propósito: ni base de datos, ni FastAPI, ni reloj propio. El instante
actual (`ahora`) siempre lo pasa quien llama (en producción,
`datetime.now(timezone.utc)`), así los tests lo fijan y el portal y el agente
aplican EXACTAMENTE la misma regla.

Los instantes deben traer zona horaria; uno ingenuo lanza ValueError porque
compararlo contra otro con zona daría un TypeError opaco.
"""

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, TypeVar
from uuid import UUID
from zoneinfo import ZoneInfo

# Lista blanca de canales (plan, sección 6): lo que no esté aquí, incluido el
# valor que use el widget web, no es una conversación de cliente de canal.
CANALES_DE_CLIENTE: frozenset[str] = frozenset({"whatsapp", "instagram", "facebook"})
LIMITE_HISTORIAL: int = 10

ESTADOS_CERRADOS: frozenset[str] = frozenset({"cancelada", "completada", "no_asistio"})
ESTADO_CONFIRMADA = "confirmada"

# UUID canónico: 8-4-4-4-12 hexadecimales con guiones (mayúsculas o minúsculas).
_PATRON_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


class CitaFila(Protocol):
    """Lo mínimo que `seleccionar_historial` necesita leer de una cita."""

    @property
    def estado(self) -> str: ...

    @property
    def hora_inicio(self) -> datetime: ...

    @property
    def creado_en(self) -> datetime: ...


T = TypeVar("T", bound=CitaFila)


def _exigir_zona(instante: datetime, nombre: str) -> None:
    if instante.tzinfo is None or instante.utcoffset() is None:
        raise ValueError(f"{nombre} debe incluir zona horaria")


def es_canal_de_cliente(channel_type: str | None) -> bool:
    """True solo para WhatsApp, Instagram o Facebook (comparación exacta)."""
    return isinstance(channel_type, str) and channel_type in CANALES_DE_CLIENTE


def interpretar_conversation_id(valor: object) -> UUID | None:
    """
    Lee el `conversation_id` del cuerpo del agente (R6). Solo acepta un texto
    con forma de UUID canónico de 36 caracteres con guiones (mayúsculas o
    minúsculas). Todo lo demás (nulo, vacío, número, booleano, objeto, lista,
    texto sin guiones, con llaves, con `urn:` o con espacios) da None, que el
    router traduce al mismo 404 que una conversación inexistente.
    """
    if not isinstance(valor, str) or _PATRON_UUID.fullmatch(valor) is None:
        return None
    return UUID(valor)


def cita_es_elegible(*, estado: str, hora_inicio: datetime, ahora: datetime) -> bool:
    """
    R2: `cancelada`, `completada` y `no_asistio` siempre (aunque el inicio sea
    futuro); `confirmada` solo si ya empezó (`hora_inicio <= ahora`; igual al
    instante cuenta, un segundo en el futuro no). Cualquier otro estado, no.
    """
    _exigir_zona(hora_inicio, "hora_inicio")
    _exigir_zona(ahora, "ahora")
    if estado in ESTADOS_CERRADOS:
        return True
    return estado == ESTADO_CONFIRMADA and hora_inicio <= ahora


def seleccionar_historial(
    citas: Sequence[T], *, ahora: datetime, limite: int = LIMITE_HISTORIAL
) -> tuple[list[T], bool]:
    """
    R1 y R3: filtra las elegibles, ordena por `hora_inicio` descendente y, en
    empate, por `creado_en` descendente, toma `limite` y devuelve
    `(citas, hay_mas)` con `hay_mas = elegibles > limite`. Quien llama ya
    filtró por proveedor si aplica, así `hay_mas` se calcula sobre ese
    subconjunto (R10). No modifica la secuencia recibida.
    """
    elegibles = [
        c for c in citas
        if cita_es_elegible(estado=c.estado, hora_inicio=c.hora_inicio, ahora=ahora)
    ]
    elegibles.sort(key=lambda c: (c.hora_inicio, c.creado_en), reverse=True)
    return elegibles[:limite], len(elegibles) > limite


def fecha_hora_local(instante: datetime, zona: str) -> str:
    """
    ISO 8601 con el desfase de la zona vigente del negocio (R12), por ejemplo
    '2026-01-09T14:00:00-06:00'. Zona desconocida o instante ingenuo lanzan
    ValueError.
    """
    _exigir_zona(instante, "instante")
    try:
        tz = ZoneInfo(zona)
    except (KeyError, ValueError, OSError) as exc:
        raise ValueError(f"zona horaria inválida: {zona!r}") from exc
    return instante.astimezone(tz).isoformat()
