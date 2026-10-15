"""
Ningún test puede mandar correo real (spec 004, T5, corrección).

El fixture autouse `sin_correo_real` (tests/conftest.py) blanquea el SMTP de
`config.settings` y sustituye `services.correo._enviar_sync`. Aquí se prueba
que, aun forzando un SMTP "configurado", ni `smtplib` ni el correo espejo
llegan a abrir una conexión. Sin BD, sin SMTP y sin red.
"""

import asyncio
import smtplib
from uuid import uuid4

import pytest

from config import settings
from services import correo, correo_espejo


@pytest.fixture
def smtplib_prohibido(monkeypatch: pytest.MonkeyPatch) -> None:
    def _prohibido(*args, **kwargs):
        pytest.fail("Se intentó abrir una conexión SMTP real desde un test")

    monkeypatch.setattr(smtplib, "SMTP", _prohibido)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _prohibido)


@pytest.fixture
def smtp_forzado(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simula el peor caso: un .env con SMTP completo."""
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.real.example")
    monkeypatch.setattr(settings, "SMTP_FROM", "avisos@real.example")
    monkeypatch.setattr(settings, "SMTP_USER", "usuario")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "secreto")


def test_ajustes_smtp_blanqueados() -> None:
    assert settings.SMTP_HOST == ""
    assert settings.SMTP_USER == ""
    assert settings.SMTP_PASSWORD == ""
    assert not settings.smtp_configurado


async def test_enviar_notificacion_con_smtp_forzado_no_toca_smtplib(
    smtplib_prohibido, smtp_forzado, sin_correo_real
) -> None:
    await correo.enviar_notificacion_negocio("a@x.com", "Título", "Mensaje")
    # Llegó al punto de envío, pero era el doble.
    assert len(sin_correo_real) == 1
    assert sin_correo_real[0]["To"] == "a@x.com"


async def test_espejo_de_una_alerta_no_toca_smtplib(
    monkeypatch: pytest.MonkeyPatch, smtplib_prohibido, smtp_forzado, sin_correo_real
) -> None:
    async def _cuentas(tenant_id, portal_user_id):
        return [{"id": uuid4(), "email": "dueno@x.com", "is_active": True}]

    monkeypatch.setattr(correo_espejo, "cuentas_de_la_alerta", _cuentas)

    previas = set(correo_espejo._tareas)
    correo_espejo.programar_espejo(uuid4(), "nuevo_lead", "Nuevo lead", "Mensaje")
    # Solo la tarea de este test: otras pueden haber quedado de loops ya cerrados.
    await asyncio.gather(*(correo_espejo._tareas - previas))

    assert [m["To"] for m in sin_correo_real] == ["dueno@x.com"]


async def test_espejo_sin_smtp_no_llega_ni_al_doble(
    monkeypatch: pytest.MonkeyPatch, smtplib_prohibido, sin_correo_real
) -> None:
    async def _cuentas(tenant_id, portal_user_id):
        return [{"id": uuid4(), "email": "dueno@x.com", "is_active": True}]

    monkeypatch.setattr(correo_espejo, "cuentas_de_la_alerta", _cuentas)

    previas = set(correo_espejo._tareas)
    correo_espejo.programar_espejo(uuid4(), "nuevo_lead", "Nuevo lead", "Mensaje")
    # Solo la tarea de este test: otras pueden haber quedado de loops ya cerrados.
    await asyncio.gather(*(correo_espejo._tareas - previas))

    assert sin_correo_real == []
