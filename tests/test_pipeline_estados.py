"""Máquina de estados del embudo. Todo puro: no hace falta base de datos."""

import pytest

from services.pipeline_estados import (
    ESTADOS,
    ESTADOS_CERRADOS,
    ESTADOS_TERMINALES,
    TRANSICIONES_VALIDAS,
    es_estado_valido,
    es_terminal,
    transiciones_desde,
    validar_transicion,
)


# ------------------------------------------------------------
# Transiciones válidas
# ------------------------------------------------------------
@pytest.mark.parametrize(
    "actual, nuevo",
    [
        ("nuevo", "contactado"),
        ("nuevo", "perdido"),
        ("contactado", "en_seguimiento"),
        ("contactado", "cotizado"),
        ("en_seguimiento", "cotizado"),
        ("cotizado", "negociacion"),
        ("cotizado", "ganado"),
        ("negociacion", "ganado"),
    ],
)
def test_transicion_valida(actual, nuevo):
    assert validar_transicion(actual, nuevo) is True


def test_camino_completo_hasta_ganado():
    """El recorrido feliz entero, paso por paso."""
    camino = ["nuevo", "contactado", "en_seguimiento", "cotizado", "negociacion", "ganado"]
    for actual, siguiente in zip(camino, camino[1:]):
        assert validar_transicion(actual, siguiente), f"{actual} -> {siguiente}"


def test_desde_cualquier_estado_abierto_se_puede_perder():
    """Un lead se cae en cualquier momento salvo si ya está cerrado."""
    for estado in ESTADOS:
        if estado in ESTADOS_CERRADOS:
            continue
        assert validar_transicion(estado, "perdido"), estado


# ------------------------------------------------------------
# Transiciones inválidas
# ------------------------------------------------------------
@pytest.mark.parametrize(
    "actual, nuevo",
    [
        # Saltarse etapas hacia adelante.
        ("nuevo", "cotizado"),
        ("nuevo", "ganado"),
        ("contactado", "negociacion"),
        ("contactado", "ganado"),
        ("en_seguimiento", "ganado"),
        # Hacia atrás.
        ("cotizado", "contactado"),
        ("negociacion", "cotizado"),
        ("en_seguimiento", "nuevo"),
        # Nadie vuelve a 'nuevo': es solo el punto de entrada.
        ("perdido", "nuevo"),
    ],
)
def test_transicion_invalida(actual, nuevo):
    assert validar_transicion(actual, nuevo) is False


def test_quedarse_en_el_mismo_estado_es_invalido():
    """
    Un PATCH que no mueve nada es un error del que llama, no un no-op
    silencioso: si el frontend manda el estado actual, quiere enterarse.
    """
    for estado in ESTADOS:
        assert validar_transicion(estado, estado) is False, estado


@pytest.mark.parametrize(
    "actual, nuevo",
    [
        ("inventado", "contactado"),
        ("nuevo", "inventado"),
        ("", "nuevo"),
        ("NUEVO", "contactado"),  # los estados son sensibles a mayúsculas
    ],
)
def test_estados_desconocidos_no_pasan(actual, nuevo):
    assert validar_transicion(actual, nuevo) is False


# ------------------------------------------------------------
# Estado terminal
# ------------------------------------------------------------
def test_ganado_es_el_unico_terminal():
    assert ESTADOS_TERMINALES == {"ganado"}


def test_desde_ganado_no_sale_nada():
    """Una venta cerrada no se reabre ni se pierde después."""
    assert transiciones_desde("ganado") == []
    for destino in ESTADOS:
        assert validar_transicion("ganado", destino) is False, destino


def test_es_terminal():
    assert es_terminal("ganado") is True
    assert es_terminal("perdido") is False
    assert es_terminal("nuevo") is False


# ------------------------------------------------------------
# Reapertura desde 'perdido'
# ------------------------------------------------------------
def test_perdido_se_reabre_solo_hacia_contactado():
    assert validar_transicion("perdido", "contactado") is True
    assert transiciones_desde("perdido") == ["contactado"]


def test_perdido_no_es_terminal_aunque_este_cerrado():
    """
    'perdido' cuenta como cerrado (no ocupa al vendedor, no suma carga) pero
    no es terminal: son dos cosas distintas y el código depende de ambas.
    """
    assert "perdido" in ESTADOS_CERRADOS
    assert "perdido" not in ESTADOS_TERMINALES


def test_lead_reabierto_puede_volver_a_recorrer_el_embudo():
    assert validar_transicion("perdido", "contactado")
    assert validar_transicion("contactado", "cotizado")
    assert validar_transicion("cotizado", "ganado")


# ------------------------------------------------------------
# Consistencia del mapa
# ------------------------------------------------------------
def test_todos_los_destinos_son_estados_conocidos():
    """Un typo en TRANSICIONES_VALIDAS daría un destino al que no se llega."""
    for origen, destinos in TRANSICIONES_VALIDAS.items():
        for destino in destinos:
            assert es_estado_valido(destino), f"{origen} apunta a '{destino}'"


def test_transiciones_desde_devuelve_copia():
    """Que el que pregunta no pueda mutar el mapa global sin querer."""
    devuelto = transiciones_desde("nuevo")
    devuelto.append("ganado")
    assert "ganado" not in TRANSICIONES_VALIDAS["nuevo"]


def test_transiciones_desde_estado_desconocido_es_lista_vacia():
    assert transiciones_desde("inventado") == []
