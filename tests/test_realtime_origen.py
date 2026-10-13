"""
Origen aceptado por el handshake del WebSocket (realtime.origen_permitido).

La app móvil en Android manda `Origin` con el dominio del propio API (React
Native, WebSocketModule.getDefaultOrigin): antes se rechazaba con 403.
"""

import pytest

import realtime
from realtime import origen_permitido


@pytest.fixture(autouse=True)
def portal(monkeypatch):
    monkeypatch.setattr(realtime.settings, "FRONTEND_ORIGINS", ["https://portal.ejemplo.com"])


def test_portal_web_sigue_permitido():
    assert origen_permitido("https://portal.ejemplo.com", {"HTTP_HOST": "api.ejemplo.com"})


def test_app_movil_con_origen_del_propio_api():
    assert origen_permitido("https://api.ejemplo.com", {"HTTP_HOST": "api.ejemplo.com"})


def test_detras_de_proxy_esquema_distinto_y_host_reenviado():
    # uvicorn ve http y un host interno; el proxy reenvía el host público.
    environ = {"HTTP_HOST": "10.11.0.5:8000", "HTTP_X_FORWARDED_HOST": "api.ejemplo.com"}
    assert origen_permitido("https://api.ejemplo.com", environ)


def test_con_puerto():
    assert origen_permitido("http://192.168.1.20:8000", {"HTTP_HOST": "192.168.1.20:8000"})


def test_otro_sitio_rechazado():
    assert not origen_permitido("https://malicioso.com", {"HTTP_HOST": "api.ejemplo.com"})


def test_subdominio_parecido_rechazado():
    assert not origen_permitido("https://api.ejemplo.com.malicioso.com", {"HTTP_HOST": "api.ejemplo.com"})


def test_sin_origin_no_se_valida():
    # python-engineio solo llama al validador si viene Origin; igual responde True.
    assert origen_permitido(None, {"HTTP_HOST": "api.ejemplo.com"})


def test_origin_basura_rechazado():
    assert not origen_permitido("null", {"HTTP_HOST": "api.ejemplo.com"})
