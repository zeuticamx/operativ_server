"""Valores por defecto de la cookie de refresh (spec 006)."""

import importlib.util
import os
from pathlib import Path

import pytest

import config as config_real


@pytest.fixture
def settings_sin_entorno(monkeypatch):
    """
    `Settings` tal como queda sin variables de entorno ni .env: carga una
    copia aislada de config.py con os.getenv devolviendo siempre el default,
    así el .env local (p. ej. COOKIE_SAMESITE=lax en desarrollo) no cambia
    lo que se verifica.
    """
    monkeypatch.setattr(os, "getenv", lambda _nombre, default=None: default)
    spec = importlib.util.spec_from_file_location(
        "config_sin_entorno", Path(config_real.__file__)
    )
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo.settings


def test_valores_por_defecto_de_la_cookie(settings_sin_entorno):
    settings = settings_sin_entorno
    assert settings.COOKIE_REFRESH_NOMBRE == "op_refresh"
    # Portal y API en dominios distintos (spec 006, D1): SameSite=None
    # exige Secure, y es el default de producción.
    assert settings.COOKIE_SAMESITE == "none"
    assert settings.COOKIE_SECURE is True
    # Ventana de migración abierta por defecto: un cliente viejo con el
    # refresh en localStorage sigue pudiendo mandarlo en el body.
    assert settings.REFRESH_BODY_LEGACY is True
