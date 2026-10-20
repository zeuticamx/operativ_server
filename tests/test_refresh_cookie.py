"""
Helpers de sesión de routers/auth.py (spec 006): atributos de la cookie,
CSRF de /auth/refresh y /auth/logout, y de dónde sale el refresh según el
cliente. Todos los casos de aquí fallan ANTES de tocar la base de datos
(Origin inválido, token ausente o ilegible), así que corren sin fixture
`db`.
"""

from fastapi import Response

from config import settings
from routers.auth import _borrar_refresh, _fijar_refresh

ORIGEN_PERMITIDO = settings.FRONTEND_ORIGINS[0]
REFRESH_URL = "/api/auth/refresh"
LOGOUT_URL = "/api/auth/logout"


# ============================================================
# Atributos de la cookie
# ============================================================
def test_fijar_refresh_pone_los_atributos_esperados():
    resp = Response()
    _fijar_refresh(resp, "un-token")

    cookie = resp.headers["set-cookie"]
    assert "op_refresh=un-token" in cookie
    assert "HttpOnly" in cookie
    assert "Path=/api/auth" in cookie
    if settings.COOKIE_SECURE:
        assert "Secure" in cookie
    assert f"samesite={settings.COOKIE_SAMESITE}".lower() in cookie.lower()


def test_borrar_refresh_la_deja_vencida():
    resp = Response()
    _borrar_refresh(resp)

    cookie = resp.headers["set-cookie"]
    assert cookie.startswith("op_refresh=")
    assert ("Max-Age=0" in cookie) or ("expires=" in cookie.lower())


# ============================================================
# CSRF: /auth/refresh y /auth/logout exigen Origin permitido (D1)
# ============================================================
async def test_refresh_sin_origin_da_403(http_client):
    r = await http_client.post(REFRESH_URL)
    assert r.status_code == 403


async def test_refresh_con_origin_ajeno_da_403(http_client):
    r = await http_client.post(REFRESH_URL, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


async def test_logout_sin_origin_da_403(http_client):
    r = await http_client.post(LOGOUT_URL)
    assert r.status_code == 403


async def test_movil_no_necesita_origin(http_client):
    """La app móvil no manda Origin ni usa cookie: queda exenta del CSRF."""
    r = await http_client.post(REFRESH_URL, headers={"X-Cliente": "movil"})
    # Pasa el CSRF (no es 403); sin refresh_token en el body, falla más
    # adelante con 401 — sin tocar la base de datos.
    assert r.status_code == 401


# ============================================================
# De dónde sale el refresh (precedencia)
# ============================================================
async def test_web_sin_cookie_ni_body_da_401(http_client):
    r = await http_client.post(REFRESH_URL, headers={"Origin": ORIGEN_PERMITIDO})
    assert r.status_code == 401


async def test_web_con_body_legacy_intenta_decodificarlo(http_client):
    """
    REFRESH_BODY_LEGACY (default true) acepta el refresh del body en web.
    Un valor ilegible falla al decodificar, antes de llegar a la base de
    datos: sigue siendo 401, pero confirma que SÍ se leyó el body.
    """
    assert settings.REFRESH_BODY_LEGACY is True
    r = await http_client.post(
        REFRESH_URL,
        headers={"Origin": ORIGEN_PERMITIDO},
        json={"refresh_token": "no-es-un-jwt"},
    )
    assert r.status_code == 401


async def test_movil_con_body_invalido_da_401(http_client):
    r = await http_client.post(
        REFRESH_URL,
        headers={"X-Cliente": "movil"},
        json={"refresh_token": "no-es-un-jwt"},
    )
    assert r.status_code == 401
