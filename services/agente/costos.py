"""
Lo que cuesta de verdad cada mensaje, en dólares.

Hasta ahora el sistema medía tokens pero no precio: `tenant_token_usage` tiene
la columna `costo_usd` desde sql/14 y el agente la dejaba vacía, así que el
panel de gerencia podía decir "este negocio gastó 400.000 tokens" y no "este
negocio costó 1,20 USD". Son cosas distintas: un millón de tokens en Haiku y
un millón en Opus se diferencian en 5x, y la misma cantidad de tokens de
entrada cuesta 10 veces menos si vino del caché.

El cálculo tiene tres piezas y la tercera es la que se olvida:

  1. La tarifa del modelo, por millón de tokens, distinta para entrada y
     salida.
  2. Que la salida cuesta ~5x la entrada en toda la familia.
  3. Que la entrada NO es un solo precio. Lo que llega cacheado se cobra a
     ~0,1x y lo que se escribe en caché a 1,25x. Sumar los tres conceptos en
     un único "tokens de entrada" —que es lo que hace `tenant_token_usage`
     para el volumen— y multiplicarlo por la tarifa base sobreestima el coste
     de cualquier conversación con caché, que son todas.

Un modelo que no esté en la tabla devuelve None, NO cero. Un cero diría "esto
fue gratis" y se sumaría en los informes como si lo fuera; un NULL dice "no sé
qué costó", que es la verdad y se ve en cuanto alguien mire la columna.

Precios de la tabla pública de Anthropic (consultados el 16/10/2026). Si
cambian, se cambian acá: `tenant_token_usage` guarda el importe ya calculado,
así que los movimientos pasados quedan con el precio que estaba vigente
cuando se registraron, que es lo que se quiere para auditar.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

__all__ = ["Tarifa", "calcular_costo", "tarifa_de"]

# El precio se publica por millón de tokens.
POR_MILLON = Decimal(1_000_000)

# Lo que llega leído de caché se cobra a una décima parte de la tarifa de
# entrada, y escribir en caché cuesta un 25% más que procesar el token suelto.
#
# El 1,25 corresponde al TTL de 5 minutos, que es el que usa
# `prompt.bloques_system` al marcar con `{"type": "ephemeral"}` sin `ttl`. Si
# algún día se pasa a `ttl: "1h"`, el multiplicador de escritura es 2 y hay
# que cambiarlo acá — no hay forma de deducirlo del `usage` que devuelve la
# API, porque informa los tokens pero no a qué TTL se escribieron.
MULT_CACHE_LECTURA = Decimal("0.1")
MULT_CACHE_ESCRITURA = Decimal("1.25")


@dataclass(frozen=True)
class Tarifa:
    """USD por millón de tokens."""

    entrada: Decimal
    salida: Decimal


# Solo los modelos que pueden aparecer en `tenant_agent_config.model`: los tres
# del catálogo actual (`limites_agente.MODELOS_AGENTE`) y los que puedan haber
# quedado escritos por n8n en filas viejas, porque la columna no tiene CHECK y
# acepta cualquier texto.
_TARIFAS: dict[str, Tarifa] = {
    # Catálogo actual
    "claude-opus-5": Tarifa(Decimal(5), Decimal(25)),
    "claude-sonnet-5": Tarifa(Decimal(3), Decimal(15)),
    "claude-haiku-4-5": Tarifa(Decimal(1), Decimal(5)),
    # Generaciones anteriores, por si una fila vieja las conserva
    "claude-opus-4-8": Tarifa(Decimal(5), Decimal(25)),
    "claude-opus-4-7": Tarifa(Decimal(5), Decimal(25)),
    "claude-opus-4-6": Tarifa(Decimal(5), Decimal(25)),
    "claude-opus-4-5": Tarifa(Decimal(5), Decimal(25)),
    "claude-sonnet-4-6": Tarifa(Decimal(3), Decimal(15)),
    "claude-sonnet-4-5": Tarifa(Decimal(3), Decimal(15)),
}


def tarifa_de(modelo: str | None) -> Tarifa | None:
    """
    La tarifa del modelo, o None si no se conoce su precio.

    Se intenta primero la coincidencia exacta y después por prefijo, porque lo
    que se registra es `respuesta.model` —lo que devolvió la API, no lo que se
    pidió— y ahí el id puede venir con fecha: se pide `claude-haiku-4-5` y
    puede volver `claude-haiku-4-5-20251001`.

    El prefijo se prueba de la clave más larga a la más corta para que un id
    con fecha no case con una familia más corta por accidente.
    """
    if not modelo:
        return None
    if modelo in _TARIFAS:
        return _TARIFAS[modelo]
    for clave in sorted(_TARIFAS, key=len, reverse=True):
        if modelo.startswith(clave):
            return _TARIFAS[clave]
    return None


def calcular_costo(
    modelo: str | None,
    *,
    entrada: int,
    salida: int,
    cache_lectura: int = 0,
    cache_escritura: int = 0,
) -> Decimal | None:
    """
    El coste en USD de una respuesta, o None si el modelo no tiene tarifa.

    `entrada` es lo que la API informa como `input_tokens`, que es SOLO lo no
    cacheado: el total del prompt es `entrada + cache_lectura +
    cache_escritura`, y cada parte lleva su propio precio. Pasar aquí el total
    ya sumado cobraría todo a tarifa plena.

    Se redondea a 6 decimales porque es la precisión de la columna
    (`NUMERIC(12,6)`): insertar más decimales los redondearía Postgres de
    todas formas, y mejor que el valor que viaja sea el que se guarda.
    """
    tarifa = tarifa_de(modelo)
    if tarifa is None:
        return None

    unidades_entrada = (
        Decimal(entrada)
        + Decimal(cache_lectura) * MULT_CACHE_LECTURA
        + Decimal(cache_escritura) * MULT_CACHE_ESCRITURA
    )
    total = (
        unidades_entrada * tarifa.entrada + Decimal(salida) * tarifa.salida
    ) / POR_MILLON
    return total.quantize(Decimal("0.000001"))
