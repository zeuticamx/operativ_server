"""
Tests de services/banxico.py: de dónde sale el tipo de cambio y qué pasa
cuando el SIE API de Banxico no responde.

No hay DB de por medio (nada de `db`/`http_client`): todo es puro contra
la caché de módulo y un cliente HTTP falso, mismo patrón que
test_pagos_stripe.py usa para no llamar a Stripe de verdad.
"""

from decimal import Decimal

import pytest

from config import settings
from services import banxico


@pytest.fixture(autouse=True)
def _caja_limpia(monkeypatch):
    """
    Cada test arranca sin caché y con la config de moneda que le toque
    fijar explícitamente. La caché es de módulo (a propósito: sobrevive
    entre requests), así que sin este fixture un test contaminaría al
    siguiente con un FIX que ya no debería existir.
    """
    banxico._reiniciar_cache_para_tests()
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "stripe")
    monkeypatch.setattr(settings, "STRIPE_CURRENCY", "mxn")
    monkeypatch.setattr(settings, "BANXICO_TOKEN", "")
    monkeypatch.setattr(settings, "TIPO_CAMBIO_USD", "")
    yield
    banxico._reiniciar_cache_para_tests()


# ------------------------------------------------------------
# Cliente HTTP falso (mismo patrón que test_pagos_stripe.py)
# ------------------------------------------------------------
class _RespuestaFalsa:
    def __init__(self, datos: dict | None = None, status: int = 200):
        self._datos = datos or {}
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise banxico.httpx.HTTPStatusError(
                "error", request=None, response=self  # type: ignore[arg-type]
            )

    def json(self) -> dict:
        return self._datos


class _ClienteFalso:
    llamadas = 0

    def __init__(self, respuesta):
        self._respuesta = respuesta

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, url, headers=None):
        _ClienteFalso.llamadas += 1
        if isinstance(self._respuesta, Exception):
            raise self._respuesta
        return self._respuesta


def _cuerpo_fix(valor: str) -> dict:
    return {"bmx": {"series": [{"datos": [{"fecha": "01/01/2026", "dato": valor}]}]}}


def _instalar(monkeypatch, respuesta):
    _ClienteFalso.llamadas = 0
    monkeypatch.setattr(banxico.httpx, "AsyncClient", lambda *a, **k: _ClienteFalso(respuesta))


# ------------------------------------------------------------
# Selección de fuente
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_moneda_usd_no_llama_a_nadie(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_CURRENCY", "usd")
    monkeypatch.setattr(settings, "BANXICO_TOKEN", "token-de-prueba")
    _instalar(monkeypatch, RuntimeError("no debería llamarse"))

    tc = await banxico.obtener_tipo_cambio()
    assert tc.fuente == "moneda_usd"
    assert tc.valor == Decimal(1)
    assert _ClienteFalso.llamadas == 0


@pytest.mark.asyncio
async def test_sin_token_ni_manual_no_hay_tipo_de_cambio():
    tc = await banxico.obtener_tipo_cambio()
    assert tc == banxico.TipoCambio(valor=None, fuente="ninguno")


@pytest.mark.asyncio
async def test_sin_token_usa_el_manual(monkeypatch):
    monkeypatch.setattr(settings, "TIPO_CAMBIO_USD", "18.50")

    tc = await banxico.obtener_tipo_cambio()
    assert tc == banxico.TipoCambio(valor=Decimal("18.50"), fuente="manual")


@pytest.mark.asyncio
async def test_con_token_y_mxn_prefiere_banxico_sobre_el_manual(monkeypatch):
    monkeypatch.setattr(settings, "BANXICO_TOKEN", "token-de-prueba")
    monkeypatch.setattr(settings, "TIPO_CAMBIO_USD", "18.50")
    _instalar(monkeypatch, _RespuestaFalsa(_cuerpo_fix("20.1234")))

    tc = await banxico.obtener_tipo_cambio()
    assert tc.fuente == "banxico"
    assert tc.valor == Decimal("20.1234")


@pytest.mark.asyncio
async def test_token_configurado_pero_moneda_no_es_mxn_usa_el_manual(monkeypatch):
    """El FIX es pesos por dólar; con otra moneda de cobro no significa nada."""
    monkeypatch.setattr(settings, "STRIPE_CURRENCY", "cop")
    monkeypatch.setattr(settings, "BANXICO_TOKEN", "token-de-prueba")
    monkeypatch.setattr(settings, "TIPO_CAMBIO_USD", "4200")
    _instalar(monkeypatch, RuntimeError("no debería llamarse"))

    tc = await banxico.obtener_tipo_cambio()
    assert tc.fuente == "manual"
    assert _ClienteFalso.llamadas == 0


# ------------------------------------------------------------
# Caché
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_una_segunda_llamada_usa_la_cache(monkeypatch):
    monkeypatch.setattr(settings, "BANXICO_TOKEN", "token-de-prueba")
    _instalar(monkeypatch, _RespuestaFalsa(_cuerpo_fix("19.00")))

    primera = await banxico.obtener_tipo_cambio()
    segunda = await banxico.obtener_tipo_cambio()

    assert primera.valor == segunda.valor == Decimal("19.00")
    assert _ClienteFalso.llamadas == 1


@pytest.mark.asyncio
async def test_cache_vencida_se_refresca(monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(settings, "BANXICO_TOKEN", "token-de-prueba")
    _instalar(monkeypatch, _RespuestaFalsa(_cuerpo_fix("19.00")))
    await banxico.obtener_tipo_cambio()

    # Se envejece la caché a mano en vez de dormir seis horas.
    banxico._cache.obtenido_en = datetime.now(timezone.utc) - banxico.CACHE_TTL - timedelta(seconds=1)
    _instalar(monkeypatch, _RespuestaFalsa(_cuerpo_fix("21.50")))

    tc = await banxico.obtener_tipo_cambio()
    assert tc.valor == Decimal("21.50")


# ------------------------------------------------------------
# Falla de Banxico: cae a caché vencida y, si nunca hubo nada, al manual
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_si_falla_pero_hay_algo_en_cache_sirve_lo_vencido(monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(settings, "BANXICO_TOKEN", "token-de-prueba")
    monkeypatch.setattr(settings, "TIPO_CAMBIO_USD", "1.00")  # no debería usarse
    _instalar(monkeypatch, _RespuestaFalsa(_cuerpo_fix("19.00")))
    await banxico.obtener_tipo_cambio()

    banxico._cache.obtenido_en = datetime.now(timezone.utc) - banxico.CACHE_TTL - timedelta(seconds=1)
    _instalar(monkeypatch, RuntimeError("Banxico caído"))

    tc = await banxico.obtener_tipo_cambio()
    assert tc.fuente == "banxico"
    assert tc.valor == Decimal("19.00")


@pytest.mark.asyncio
async def test_si_falla_y_nunca_hubo_cache_cae_al_manual(monkeypatch):
    monkeypatch.setattr(settings, "BANXICO_TOKEN", "token-de-prueba")
    monkeypatch.setattr(settings, "TIPO_CAMBIO_USD", "18.50")
    _instalar(monkeypatch, RuntimeError("token vencido"))

    tc = await banxico.obtener_tipo_cambio()
    assert tc == banxico.TipoCambio(valor=Decimal("18.50"), fuente="manual")


@pytest.mark.asyncio
async def test_si_falla_y_no_hay_manual_tampoco_queda_en_ninguno(monkeypatch):
    monkeypatch.setattr(settings, "BANXICO_TOKEN", "token-de-prueba")
    _instalar(monkeypatch, RuntimeError("token vencido"))

    tc = await banxico.obtener_tipo_cambio()
    assert tc == banxico.TipoCambio(valor=None, fuente="ninguno")


@pytest.mark.asyncio
async def test_respuesta_con_forma_inesperada_no_revienta(monkeypatch):
    monkeypatch.setattr(settings, "BANXICO_TOKEN", "token-de-prueba")
    monkeypatch.setattr(settings, "TIPO_CAMBIO_USD", "18.50")
    _instalar(monkeypatch, _RespuestaFalsa({"bmx": {"series": []}}))

    tc = await banxico.obtener_tipo_cambio()
    assert tc.fuente == "manual"


@pytest.mark.asyncio
async def test_un_fix_no_positivo_se_descarta(monkeypatch):
    """Un 0 o negativo de la serie es un dato roto, no un tipo de cambio real."""
    monkeypatch.setattr(settings, "BANXICO_TOKEN", "token-de-prueba")
    monkeypatch.setattr(settings, "TIPO_CAMBIO_USD", "18.50")
    _instalar(monkeypatch, _RespuestaFalsa(_cuerpo_fix("0")))

    tc = await banxico.obtener_tipo_cambio()
    assert tc.fuente == "manual"


# ------------------------------------------------------------
# Concurrencia: N peticiones con la caché vencida no disparan N llamadas
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_llamadas_concurrentes_solo_piden_una_vez(monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "BANXICO_TOKEN", "token-de-prueba")
    _instalar(monkeypatch, _RespuestaFalsa(_cuerpo_fix("19.00")))

    resultados = await asyncio.gather(*[banxico.obtener_tipo_cambio() for _ in range(8)])

    assert all(tc.valor == Decimal("19.00") for tc in resultados)
    assert _ClienteFalso.llamadas == 1
