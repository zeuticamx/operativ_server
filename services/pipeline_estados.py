"""
Máquina de estados del embudo de venta.

Sin I/O a propósito: es la única pieza que decide si un movimiento del
embudo es legal, y se quiere poder probarla sin base de datos ni FastAPI.
El CHECK de `client_pipeline.estado` en 06_vendedores.sql lista los mismos
estados; si se agrega uno hay que tocar los dos lados.
"""

ESTADO_INICIAL = "nuevo"

TRANSICIONES_VALIDAS: dict[str, list[str]] = {
    "nuevo": ["contactado", "perdido"],
    "contactado": ["en_seguimiento", "cotizado", "perdido"],
    "en_seguimiento": ["cotizado", "perdido"],
    "cotizado": ["negociacion", "ganado", "perdido"],
    "negociacion": ["ganado", "perdido"],
    "ganado": [],
    "perdido": ["contactado"],
}

ESTADOS: tuple[str, ...] = tuple(TRANSICIONES_VALIDAS)

# Terminal = sin salida. 'perdido' NO es terminal: un lead perdido se puede
# retomar volviendo a 'contactado'.
ESTADOS_TERMINALES = frozenset(
    estado for estado, destinos in TRANSICIONES_VALIDAS.items() if not destinos
)

# Cerrados = ya no ocupan al vendedor. Es lo que cuenta como "carga" para
# repartir leads nuevos, y es distinto de terminal: 'perdido' está cerrado
# pero se puede reabrir.
ESTADOS_CERRADOS = frozenset({"ganado", "perdido"})


def es_estado_valido(estado: str) -> bool:
    return estado in TRANSICIONES_VALIDAS


def transiciones_desde(estado: str) -> list[str]:
    """
    A dónde se puede mover un lead que está en `estado`.

    Lista vacía tanto para un estado terminal como para uno desconocido: el
    que pregunta solo quiere saber qué ofrecer, y en ambos casos no hay nada.
    """
    return list(TRANSICIONES_VALIDAS.get(estado, ()))


def es_terminal(estado: str) -> bool:
    return estado in ESTADOS_TERMINALES


def validar_transicion(estado_actual: str, estado_nuevo: str) -> bool:
    """
    True solo si el movimiento está permitido.

    Quedarse en el mismo estado devuelve False: ningún estado se lista a sí
    mismo como destino. Un PATCH que no mueve nada es un error del que
    llama, no un no-op silencioso.
    """
    return estado_nuevo in TRANSICIONES_VALIDAS.get(estado_actual, ())
