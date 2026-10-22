"""
Stripe Connect con Accounts v2: la forma de los requests que mandamos.

Accounts v1 con `type` rechaza cuentas cuyas pérdidas asume la plataforma
(400 "legacy type field ... platform as the losses collector"), así que estas
pruebas fijan que la creación va por /v2/core/accounts sin `type` y con
Stripe como losses_collector. No llaman a Stripe: capturan el payload.
"""

import pytest

from services import stripe_connect


@pytest.fixture
def capturados(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    llamadas: list[tuple[str, dict]] = []

    async def falso_post(path, payload, **kwargs):
        llamadas.append((path, payload))
        return {"id": "acct_test", "url": "https://connect.stripe.com/setup/x"}

    monkeypatch.setattr(stripe_connect, "_post", falso_post)
    return llamadas


@pytest.mark.asyncio
async def test_crear_cuenta_usa_accounts_v2_sin_type(capturados):
    cuenta_id = await stripe_connect._crear_cuenta_en_stripe()

    assert cuenta_id == "acct_test"
    path, payload = capturados[0]
    assert path == "/v2/core/accounts"
    assert "type" not in payload
    assert payload["dashboard"] == "express"
    assert payload["identity"] == {"country": stripe_connect.PAIS_CUENTAS_CONECTADAS}
    assert payload["defaults"]["responsibilities"] == {
        "fees_collector": "stripe",
        "losses_collector": "stripe",
    }
    assert payload["configuration"]["merchant"]["capabilities"]["card_payments"] == {
        "requested": True
    }


@pytest.mark.asyncio
async def test_account_link_usa_use_case_de_onboarding(capturados):
    url = await stripe_connect.crear_account_link(
        "acct_test",
        refresh_url="https://portal.example/cobros/conectar?refresh=1",
        return_url="https://portal.example/cobros/conectar?listo=1",
    )

    assert url == "https://connect.stripe.com/setup/x"
    path, payload = capturados[0]
    assert path == "/v2/core/account_links"
    assert payload["account"] == "acct_test"
    assert payload["use_case"]["type"] == "account_onboarding"
    onboarding = payload["use_case"]["account_onboarding"]
    assert "configurations" not in onboarding
    assert onboarding["refresh_url"].endswith("refresh=1")
    assert onboarding["return_url"].endswith("listo=1")
