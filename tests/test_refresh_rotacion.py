"""
Rotación del refresh token, cookie HttpOnly y logout (spec 006). Login real
por /api/auth/login, igual que lo haría el portal: httpx guarda la cookie
Set-Cookie del login y la reenvía sola en la siguiente petición al mismo
cliente, así que estos tests ejercitan el camino real sin tener que leer la
cookie a mano salvo cuando hace falta inspeccionarla.
"""

from datetime import datetime, timedelta, timezone

import pytest

from config import settings
from session import execute, fetch_all, fetch_one

LOGIN = "/api/auth/login"
REFRESH = "/api/auth/refresh"
LOGOUT = "/api/auth/logout"
PASSWORD = "test_password"
ORIGEN = settings.FRONTEND_ORIGINS[0]


@pytest.fixture(autouse=True)
def cookie_insegura_para_el_cliente_de_test(monkeypatch):
    """
    httpx (como cualquier navegador) no reenvía una cookie `Secure` sobre
    una conexión sin TLS, y el servidor de pruebas (ASGITransport) habla
    `http://test`. En producción la cookie sí lleva `Secure` (default de
    `COOKIE_SECURE`); acá se apaga, igual que en el flujo local sin
    `dev:https` descrito en el README, para poder comprobar el viaje de
    ida y vuelta de la cookie.
    """
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)


@pytest.fixture
async def cuenta(tenant_y_usuario):
    """La fixture compartida no acepta términos; login los exige."""
    await execute(
        "UPDATE portal_users SET terminos_aceptados_en = NOW(), terminos_version = 'test' WHERE id = $1",
        tenant_y_usuario["usuario_id"],
    )
    return tenant_y_usuario


async def _login(http_client, email: str, headers: dict | None = None):
    return await http_client.post(
        LOGIN,
        json={"email": email, "password": PASSWORD},
        headers={"Origin": ORIGEN, **(headers or {})},
    )


async def _sesiones(user_id):
    return await fetch_all("SELECT * FROM refresh_sesiones WHERE user_id = $1", user_id)


# ============================================================
# Login: cookie puesta, nada de refresh en el body
# ============================================================
async def test_login_web_pone_cookie_y_body_sin_refresh(http_client, cuenta):
    r = await _login(http_client, cuenta["email"])
    assert r.status_code == 200
    assert r.json()["refresh_token"] is None
    assert settings.COOKIE_REFRESH_NOMBRE in r.cookies

    filas = await _sesiones(cuenta["usuario_id"])
    assert len(filas) == 1
    assert filas[0]["usado_en"] is None


async def test_login_movil_recibe_refresh_en_body_sin_cookie(http_client, cuenta):
    r = await _login(http_client, cuenta["email"], headers={"X-Cliente": "movil"})
    assert r.status_code == 200
    assert r.json()["refresh_token"]
    assert settings.COOKIE_REFRESH_NOMBRE not in r.cookies


# ============================================================
# Rotación
# ============================================================
async def test_refresh_rota_el_jti(http_client, cuenta):
    await _login(http_client, cuenta["email"])
    filas_antes = await _sesiones(cuenta["usuario_id"])
    jti_viejo = filas_antes[0]["jti"]

    r = await http_client.post(REFRESH, headers={"Origin": ORIGEN})
    assert r.status_code == 200
    assert r.json()["refresh_token"] is None

    filas = await _sesiones(cuenta["usuario_id"])
    assert len(filas) == 2
    vieja = next(f for f in filas if f["jti"] == jti_viejo)
    nueva = next(f for f in filas if f["jti"] != jti_viejo)
    assert vieja["usado_en"] is not None
    assert nueva["usado_en"] is None


async def test_refresh_movil_devuelve_el_nuevo_en_el_body(http_client, cuenta):
    login = await _login(http_client, cuenta["email"], headers={"X-Cliente": "movil"})
    refresh_viejo = login.json()["refresh_token"]

    r = await http_client.post(
        REFRESH,
        headers={"X-Cliente": "movil"},
        json={"refresh_token": refresh_viejo},
    )
    assert r.status_code == 200
    assert r.json()["refresh_token"]
    assert r.json()["refresh_token"] != refresh_viejo


# ============================================================
# Reuso: dentro de la gracia no revoca, fuera de ella revoca todo
# ============================================================
async def test_reuso_dentro_de_gracia_no_revoca(http_client, cuenta):
    await _login(http_client, cuenta["email"])
    filas_antes = await _sesiones(cuenta["usuario_id"])
    jti_viejo = filas_antes[0]["jti"]
    cookie_vieja = http_client.cookies.get(settings.COOKIE_REFRESH_NOMBRE)

    # Primer canje: rota.
    assert (await http_client.post(REFRESH, headers={"Origin": ORIGEN})).status_code == 200

    # Se reusa el refresh YA canjeado (recién, dentro del margen de
    # gracia): 401, pero la sesión nueva sigue viva.
    http_client.cookies.set(settings.COOKIE_REFRESH_NOMBRE, cookie_vieja)
    r = await http_client.post(REFRESH, headers={"Origin": ORIGEN})
    assert r.status_code == 401

    filas = await _sesiones(cuenta["usuario_id"])
    assert len(filas) == 2  # no se borró nada
    assert any(f["jti"] != jti_viejo and f["usado_en"] is None for f in filas)


async def test_reuso_fuera_de_gracia_revoca_todo(http_client, cuenta, monkeypatch):
    import routers.auth as auth_module

    monkeypatch.setattr(auth_module, "_REUSO_GRACIA_SEGUNDOS", 0)

    await _login(http_client, cuenta["email"])
    cookie_vieja = http_client.cookies.get(settings.COOKIE_REFRESH_NOMBRE)
    assert (await http_client.post(REFRESH, headers={"Origin": ORIGEN})).status_code == 200

    # Han "pasado" 0 segundos de gracia: cualquier reuso ya cuenta como
    # fuera de ventana.
    http_client.cookies.set(settings.COOKIE_REFRESH_NOMBRE, cookie_vieja)
    r = await http_client.post(REFRESH, headers={"Origin": ORIGEN})
    assert r.status_code == 401

    filas = await _sesiones(cuenta["usuario_id"])
    assert filas == []  # todas las sesiones del usuario, borradas


# ============================================================
# Logout
# ============================================================
async def test_logout_borra_la_fila_y_la_cookie(http_client, cuenta):
    await _login(http_client, cuenta["email"])
    filas_antes = await _sesiones(cuenta["usuario_id"])
    assert len(filas_antes) == 1

    r = await http_client.post(LOGOUT, headers={"Origin": ORIGEN})
    assert r.status_code == 204

    filas = await _sesiones(cuenta["usuario_id"])
    assert filas == []

    r2 = await http_client.post(REFRESH, headers={"Origin": ORIGEN})
    assert r2.status_code == 401


async def test_logout_siempre_204_sin_sesion(http_client):
    r = await http_client.post(LOGOUT, headers={"Origin": ORIGEN})
    assert r.status_code == 204


# ============================================================
# REFRESH_BODY_LEGACY=false: web ignora el body
# ============================================================
async def test_sin_legacy_web_ignora_el_body(http_client, cuenta, monkeypatch):
    monkeypatch.setattr(settings, "REFRESH_BODY_LEGACY", False)

    login = await _login(http_client, cuenta["email"], headers={"X-Cliente": "movil"})
    refresh_movil = login.json()["refresh_token"]

    # Pedido "web" (sin X-Cliente) con ese refresh en el body: como la
    # ventana de migración está apagada, el body se ignora y, sin cookie
    # puesta por este cliente, da 401.
    r = await http_client.post(
        REFRESH, headers={"Origin": ORIGEN}, json={"refresh_token": refresh_movil}
    )
    assert r.status_code == 401


# ============================================================
# Restablecer contraseña cierra las sesiones de refresh (T8)
# ============================================================
async def test_restablecer_password_borra_las_sesiones(http_client, cuenta, monkeypatch):
    from services import recuperacion_password as svc

    enviados = []

    async def falso(destino, codigo, minutos):
        enviados.append((destino, codigo, minutos))

    monkeypatch.setattr(svc, "enviar_codigo_recuperacion", falso)

    await _login(http_client, cuenta["email"])
    assert len(await _sesiones(cuenta["usuario_id"])) == 1

    await http_client.post("/api/auth/recuperar/solicitar", json={"email": cuenta["email"]})
    codigo = enviados[0][1]
    r = await http_client.post(
        "/api/auth/recuperar/restablecer",
        json={"email": cuenta["email"], "codigo": codigo, "password": "otra-clave-nueva"},
    )
    assert r.status_code == 204

    assert await _sesiones(cuenta["usuario_id"]) == []
