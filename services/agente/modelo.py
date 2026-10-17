"""
Qué parámetros acepta cada modelo del catálogo.

`services/limites_agente.MODELOS_AGENTE` abarca tres generaciones de la API de
Anthropic, y no aceptan la misma petición. Mandar un parámetro que el modelo
ya no admite no se degrada: devuelve 400 y el cliente se queda sin respuesta.

    claude-opus-5     piensa por defecto · acepta `effort` · RECHAZA temperature
    claude-sonnet-5   piensa por defecto · acepta `effort` · RECHAZA temperature
    claude-haiku-4-5  sin pensamiento adaptativo · sin `effort` · acepta temperature

De ahí este módulo: el tenant elige el modelo (`tenant_agent_config.model`) y
acá se decide qué viaja en la petición.

Dos cosas que esto arregla de la migración:

  - `tenant_agent_config.temperature` se le pasaba al modelo desde n8n, y el
    modelo por defecto del propio esquema es `claude-sonnet-5`, que lo
    rechaza. Acá la columna se sigue respetando donde es válida y se omite
    donde daría 400.
  - Un modelo que no esté en la tabla se trata como desconocido y se le manda
    la petición mínima. Omitir un parámetro siempre funciona; inventarlo no.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Esfuerzo por defecto. Esto es atención al cliente por chat: alguien está
# esperando del otro lado, así que se prioriza latencia. Los niveles altos se
# reservan para trabajo agéntico largo, que no es este caso.
EFFORT_POR_DEFECTO = "low"


@dataclass(frozen=True)
class Capacidades:
    """Qué admite un modelo. Lo que no admite, no se manda."""

    thinking_adaptativo: bool = False
    effort: bool = False
    temperature: bool = False


# Lo que no esté acá se trata como desconocido (todo en False): la petición
# mínima es la única que se puede dar por válida sin haberlo comprobado.
_CAPACIDADES: dict[str, Capacidades] = {
    "claude-opus-5": Capacidades(thinking_adaptativo=True, effort=True),
    "claude-sonnet-5": Capacidades(thinking_adaptativo=True, effort=True),
    "claude-haiku-4-5": Capacidades(temperature=True),
    "claude-haiku-4-5-20251001": Capacidades(temperature=True),
}

DESCONOCIDO = Capacidades()


def capacidades_de(modelo: str) -> Capacidades:
    return _CAPACIDADES.get(modelo, DESCONOCIDO)


def parametros_modelo(
    modelo: str,
    *,
    temperature: float | None = None,
    effort: str = EFFORT_POR_DEFECTO,
) -> dict[str, Any]:
    """
    Los parámetros opcionales que este modelo sí acepta.

    Se omite deliberadamente `thinking` incluso en los modelos que lo admiten:
    en Opus 5 y Sonnet 5 el pensamiento adaptativo ya está activo por omisión,
    y apagarlo tiene efectos secundarios documentados (llamadas a herramienta
    escritas como texto plano, que no se ejecutan y nadie detecta). El control
    de coste y latencia va por `effort`, que es el lever correcto.
    """
    cap = capacidades_de(modelo)
    params: dict[str, Any] = {}

    if cap.effort:
        params["output_config"] = {"effort": effort}

    # Solo donde es válido. En Opus 5 / Sonnet 5 esto sería un 400.
    if cap.temperature and temperature is not None:
        params["temperature"] = temperature

    return params
