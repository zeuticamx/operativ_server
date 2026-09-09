"""
Reglas de reparto de leads.

Se prueba la capa de decisión, que es pura: recibe los candidatos ya
cargados y devuelve un id. La SQL que los carga no se prueba acá.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from services.asignacion import VendedorCandidato, elegir_por_carga, elegir_round_robin

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def vendedor(sufijo: int, carga: int = 0, dias_alta: int = 0, ultima_hace: int | None = None):
    """
    Candidato de prueba. `ultima_hace` son días desde BASE hacia atrás;
    None = nunca recibió nada.
    """
    return VendedorCandidato(
        id=UUID(f"00000000-0000-0000-0000-{sufijo:012d}"),
        creado_en=BASE + timedelta(days=dias_alta),
        carga=carga,
        ultima_asignacion=None if ultima_hace is None else BASE - timedelta(days=ultima_hace),
    )


# ============================================================
# Estrategia 'carga'
# ============================================================
def test_carga_elige_al_que_tiene_menos_leads_abiertos():
    a = vendedor(1, carga=5)
    b = vendedor(2, carga=1)
    c = vendedor(3, carga=3)
    assert elegir_por_carga([a, b, c]) == b.id


def test_carga_empate_gana_la_asignacion_mas_antigua():
    """Misma carga: le toca al que lleva más tiempo sin recibir nada."""
    reciente = vendedor(1, carga=2, ultima_hace=1)
    antiguo = vendedor(2, carga=2, ultima_hace=30)
    assert elegir_por_carga([reciente, antiguo]) == antiguo.id


def test_carga_el_que_nunca_recibio_nada_gana_el_empate():
    """
    Un vendedor recién dado de alta tiene que entrar a la rueda antes que
    uno que ya viene recibiendo, no quedarse esperando.
    """
    con_historia = vendedor(1, carga=0, ultima_hace=10)
    nunca = vendedor(2, carga=0, ultima_hace=None)
    assert elegir_por_carga([con_historia, nunca]) == nunca.id


def test_carga_manda_sobre_la_antiguedad():
    """El desempate solo aplica con carga igual; si no, gana el menos cargado."""
    poca_carga_reciente = vendedor(1, carga=1, ultima_hace=1)
    mucha_carga_antiguo = vendedor(2, carga=9, ultima_hace=90)
    assert elegir_por_carga([poca_carga_reciente, mucha_carga_antiguo]) == poca_carga_reciente.id


def test_carga_empate_total_desempata_el_vendedor_mas_antiguo():
    nuevo = vendedor(1, carga=0, dias_alta=10)
    viejo = vendedor(2, carga=0, dias_alta=0)
    assert elegir_por_carga([nuevo, viejo]) == viejo.id


def test_carga_es_determinista_pase_lo_que_pase():
    """
    Dos candidatos idénticos en todo tienen que resolverse siempre igual,
    sin depender del orden en que los devolvió Postgres.
    """
    a = vendedor(1)
    b = vendedor(2)
    assert elegir_por_carga([a, b]) == elegir_por_carga([b, a])


def test_carga_sin_candidatos_devuelve_none():
    """Tenant sin vendedores activos: None, nunca una excepción."""
    assert elegir_por_carga([]) is None


def test_carga_con_un_solo_candidato():
    solo = vendedor(1, carga=100)
    assert elegir_por_carga([solo]) == solo.id


# ============================================================
# Estrategia 'round_robin'
# ============================================================
def test_round_robin_avanza_al_siguiente():
    a = vendedor(1, dias_alta=0)
    b = vendedor(2, dias_alta=1)
    c = vendedor(3, dias_alta=2)
    assert elegir_round_robin([a, b, c], a.id) == b.id
    assert elegir_round_robin([a, b, c], b.id) == c.id


def test_round_robin_da_la_vuelta():
    a = vendedor(1, dias_alta=0)
    b = vendedor(2, dias_alta=1)
    c = vendedor(3, dias_alta=2)
    assert elegir_round_robin([a, b, c], c.id) == a.id


def test_round_robin_sin_puntero_arranca_por_el_mas_antiguo():
    a = vendedor(1, dias_alta=0)
    b = vendedor(2, dias_alta=1)
    assert elegir_round_robin([a, b], None) == a.id


def test_round_robin_ignora_un_puntero_que_ya_no_existe():
    """
    Si al último asignado lo desactivaron o lo borraron, la rueda arranca de
    nuevo en vez de quedarse trabada sin devolver a nadie.
    """
    a = vendedor(1, dias_alta=0)
    b = vendedor(2, dias_alta=1)
    fantasma = UUID("99999999-9999-9999-9999-999999999999")
    assert elegir_round_robin([a, b], fantasma) == a.id


def test_round_robin_ordena_por_alta_no_por_como_llega_la_lista():
    a = vendedor(1, dias_alta=0)
    b = vendedor(2, dias_alta=1)
    c = vendedor(3, dias_alta=2)
    assert elegir_round_robin([c, a, b], a.id) == b.id


def test_round_robin_da_una_vuelta_completa_y_pareja():
    """Seis asignaciones sobre tres vendedores: dos a cada uno, en orden."""
    a = vendedor(1, dias_alta=0)
    b = vendedor(2, dias_alta=1)
    c = vendedor(3, dias_alta=2)
    candidatos = [a, b, c]

    puntero = None
    obtenidos = []
    for _ in range(6):
        puntero = elegir_round_robin(candidatos, puntero)
        obtenidos.append(puntero)

    assert obtenidos == [a.id, b.id, c.id, a.id, b.id, c.id]


def test_round_robin_con_un_solo_vendedor_siempre_devuelve_el_mismo():
    solo = vendedor(1)
    assert elegir_round_robin([solo], solo.id) == solo.id


def test_round_robin_sin_candidatos_devuelve_none():
    assert elegir_round_robin([], None) is None
    assert elegir_round_robin([], UUID(int=1)) is None


# ============================================================
# Reasignación de pendientes: el excluido no puede recuperarlos
# ============================================================
def test_excluir_al_unico_otro_vendedor_deja_sin_destino():
    """
    Reasignar los pendientes de alguien cuando no queda nadie más: la lista
    de candidatos llega vacía y las dos estrategias devuelven None, que es
    lo que hace que el lead se quede donde está en vez de romperse.
    """
    assert elegir_por_carga([]) is None
    assert elegir_round_robin([], None) is None


@pytest.mark.parametrize("estrategia", [elegir_por_carga, lambda c: elegir_round_robin(c, None)])
def test_ninguna_estrategia_lanza_con_lista_vacia(estrategia):
    assert estrategia([]) is None
