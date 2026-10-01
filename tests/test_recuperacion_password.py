"""
Recuperación de contraseña (services/recuperacion_password.py y los dos
endpoints de routers/auth.py).

El correo no sale: se sustituye `enviar_codigo_recuperacion` para capturar
el código. El paso del tiempo se simula corriendo hacia atrás sent_at y
expires_at en la BD (lo mismo que haría el reloj), porque la vigencia se
calcula con NOW() de Postgres y no con el reloj de Python.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest

from config import settings
from security import crear_refresh_token, verify_password
from services import recuperacion_password as svc
from session import execute, fetch_one

SOLICITAR = "/api/auth/recuperar/solicitar"
RESTABLECER = "/api/auth/recuperar/restablecer"
NUEVA = "nueva-clave-segura"
ERROR_GENERICO = "El código es incorrecto o ya venció. Solicita uno nuevo."


@pytest.fixture
def correos(monkeypatch):
    """Lista de (destino, código, minutos) que se habrían mandado."""
    enviados: list[tuple[str, str, int]] = []

    async def falso(destino: str, codigo: str, minutos: int) -> None:
        enviados.append((destino, codigo, minutos))

    monkeypatch.setattr(svc, "enviar_codigo_recuperacion", falso)
    return enviados


@pytest.fixture
async def cuenta(tenant_y_usuario):
    """Usuario con términos aceptados, para poder probar /auth/login."""
    await execute(
        "UPDATE portal_users SET terminos_aceptados_en = NOW(), terminos_version = 'test' WHERE id = $1",
        tenant_y_usuario["usuario_id"],
    )
    return tenant_y_usuario


async def _solicitar(http_client, email: str):
    return await http_client.post(SOLICITAR, json={"email": email})


async def _restablecer(http_client, email: str, codigo: str, password: str = NUEVA):
    return await http_client.post(
        RESTABLECER, json={"email": email, "codigo": codigo, "password": password}
    )


async def _fila(user_id):
    return await fetch_one(
        """
        SELECT code_hash, attempts, expires_at, sent_at,
               expires_at - sent_at AS vigencia
        FROM password_resets WHERE portal_user_id = $1
        """,
        user_id,
    )


async def _correr_reloj(user_id, delta: timedelta) -> None:
    """Simula que pasó `delta` desde la emisión."""
    await execute(
        """
        UPDATE password_resets
           SET sent_at = sent_at - $2::interval, expires_at = expires_at - $2::interval
         WHERE portal_user_id = $1
        """,
        user_id,
        delta,
    )


async def _login(http_client, email: str, password: str):
    return await http_client.post("/api/auth/login", json={"email": email, "password": password})


def _token_viejo(user_id, tenant_id, tipo: str) -> str:
    """Token emitido hace 1 minuto (iat en el pasado)."""
    ahora = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "type": tipo, "iat": ahora - timedelta(minutes=1),
               "exp": ahora + timedelta(minutes=30)}
    if tipo == "access":
        payload |= {"tenant_id": str(tenant_id), "role": "owner"}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


# ============================================================
# Generación del código
# ============================================================
def test_codigo_de_6_digitos():
    codigos = {svc.generar_codigo() for _ in range(200)}
    assert all(len(c) == 6 and c.isdigit() for c in codigos)
    assert len(codigos) > 190  # aleatorio, no un valor fijo


async def test_solicitud_genera_codigo_hasheado_y_lo_manda(http_client, cuenta, correos):
    r = await _solicitar(http_client, cuenta["email"].upper())  # sin distinguir mayúsculas

    assert r.status_code == 202
    assert correos and correos[0][0] == cuenta["email"]
    _, codigo, minutos = correos[0]
    assert minutos == 5

    fila = await _fila(cuenta["usuario_id"])
    assert fila["attempts"] == 0
    # Guardado con argon2 (el mismo hashing de las contraseñas), nunca en claro.
    assert fila["code_hash"].startswith("$argon2id$")
    assert codigo not in fila["code_hash"]
    assert verify_password(codigo, fila["code_hash"])


# ============================================================
# Anti-enumeración
# ============================================================
async def test_misma_respuesta_exista_o_no_el_correo(http_client, cuenta, correos):
    existe = await _solicitar(http_client, cuenta["email"])
    no_existe = await _solicitar(http_client, f"nadie-{uuid4()}@ejemplo.com")

    assert existe.status_code == no_existe.status_code == 202
    assert existe.json() == no_existe.json() == {
        "expira_en_minutos": 5,
        "reenviar_en_segundos": settings.CODIGO_REENVIO_SEGUNDOS,
    }
    assert len(correos) == 1  # solo a la cuenta que existe


async def test_cuenta_desactivada_no_recibe_codigo(http_client, cuenta, correos):
    await execute("UPDATE portal_users SET is_active = false WHERE id = $1", cuenta["usuario_id"])

    r = await _solicitar(http_client, cuenta["email"])

    assert r.status_code == 202
    assert correos == []
    assert await _fila(cuenta["usuario_id"]) is None


async def test_correo_inexistente_da_el_mismo_error_que_un_codigo_errado(http_client, cuenta, correos):
    await _solicitar(http_client, cuenta["email"])
    codigo = correos[0][1]
    errado = "000000" if codigo != "000000" else "111111"

    sin_cuenta = await _restablecer(http_client, f"nadie-{uuid4()}@ejemplo.com", "123456")
    con_cuenta = await _restablecer(http_client, cuenta["email"], errado)

    assert sin_cuenta.status_code == con_cuenta.status_code == 400
    assert sin_cuenta.json() == con_cuenta.json() == {"detail": ERROR_GENERICO}


# ============================================================
# Vigencia estricta de 5 minutos
# ============================================================
async def test_vigencia_exacta_de_5_minutos(http_client, cuenta, correos):
    await _solicitar(http_client, cuenta["email"])
    fila = await _fila(cuenta["usuario_id"])
    assert fila["vigencia"] == timedelta(minutes=5)


async def test_a_los_4_59_todavia_vale(http_client, cuenta, correos):
    await _solicitar(http_client, cuenta["email"])
    await _correr_reloj(cuenta["usuario_id"], timedelta(minutes=4, seconds=59))

    r = await _restablecer(http_client, cuenta["email"], correos[0][1])

    assert r.status_code == 204


async def test_a_los_5_00_ya_no_vale(http_client, cuenta, correos):
    await _solicitar(http_client, cuenta["email"])
    await _correr_reloj(cuenta["usuario_id"], timedelta(minutes=5))

    r = await _restablecer(http_client, cuenta["email"], correos[0][1])

    assert r.status_code == 400
    assert r.json()["detail"] == ERROR_GENERICO
    # El vencido se descarta y la contraseña no cambió.
    assert await _fila(cuenta["usuario_id"]) is None
    assert (await _login(http_client, cuenta["email"], "test_password")).status_code == 200


# ============================================================
# Un solo uso y cambio de contraseña
# ============================================================
async def test_cambia_la_contraseña_y_el_codigo_se_invalida(http_client, cuenta, correos):
    await _solicitar(http_client, cuenta["email"])
    codigo = correos[0][1]

    primero = await _restablecer(http_client, cuenta["email"], codigo)
    assert primero.status_code == 204
    assert await _fila(cuenta["usuario_id"]) is None

    # Un solo uso: el mismo código ya no sirve, ni para otra contraseña.
    segundo = await _restablecer(http_client, cuenta["email"], codigo, "otra-clave-distinta")
    assert segundo.status_code == 400

    assert (await _login(http_client, cuenta["email"], NUEVA)).status_code == 200
    assert (await _login(http_client, cuenta["email"], "test_password")).status_code == 401

    # Mismo algoritmo de siempre (argon2id), no uno nuevo.
    usuario = await fetch_one(
        "SELECT password_hash FROM portal_users WHERE id = $1", cuenta["usuario_id"]
    )
    assert usuario["password_hash"].startswith("$argon2id$")


async def test_cuenta_solo_google_puede_definir_contraseña(http_client, cuenta, correos):
    await execute(
        "UPDATE portal_users SET password_hash = NULL WHERE id = $1", cuenta["usuario_id"]
    )
    await _solicitar(http_client, cuenta["email"])

    r = await _restablecer(http_client, cuenta["email"], correos[0][1])

    assert r.status_code == 204
    assert (await _login(http_client, cuenta["email"], NUEVA)).status_code == 200


# ============================================================
# Intentos fallidos
# ============================================================
async def test_tope_de_intentos_anula_el_codigo(http_client, cuenta, correos):
    await _solicitar(http_client, cuenta["email"])
    codigo = correos[0][1]
    errado = "000000" if codigo != "000000" else "111111"

    for _ in range(settings.CODIGO_MAX_INTENTOS):
        r = await _restablecer(http_client, cuenta["email"], errado)
        assert r.status_code == 400

    # Agotados los intentos, ni el código correcto sirve.
    r = await _restablecer(http_client, cuenta["email"], codigo)
    assert r.status_code == 400
    assert await _fila(cuenta["usuario_id"]) is None
    assert (await _login(http_client, cuenta["email"], "test_password")).status_code == 200


# ============================================================
# Reenvío
# ============================================================
async def test_freno_de_reenvio_y_codigo_nuevo_anula_el_anterior(http_client, cuenta, correos):
    await _solicitar(http_client, cuenta["email"])
    await _solicitar(http_client, cuenta["email"])
    assert len(correos) == 1  # el segundo, dentro del freno, no sale

    await _correr_reloj(cuenta["usuario_id"], timedelta(seconds=settings.CODIGO_REENVIO_SEGUNDOS + 1))
    await _solicitar(http_client, cuenta["email"])
    assert len(correos) == 2
    viejo, nuevo = correos[0][1], correos[1][1]

    if viejo != nuevo:
        r = await _restablecer(http_client, cuenta["email"], viejo)
        assert r.status_code == 400

    # El nuevo arranca con reloj completo de 5 minutos.
    fila = await _fila(cuenta["usuario_id"])
    assert fila["vigencia"] == timedelta(minutes=5)
    assert (await _restablecer(http_client, cuenta["email"], nuevo)).status_code == 204


# ============================================================
# Cierre de sesiones
# ============================================================
async def test_restablecer_cierra_las_sesiones_abiertas(http_client, cuenta, correos):
    access_viejo = _token_viejo(cuenta["usuario_id"], cuenta["tenant_id"], "access")
    refresh_viejo = _token_viejo(cuenta["usuario_id"], cuenta["tenant_id"], "refresh")
    auth_viejo = {"Authorization": f"Bearer {access_viejo}"}
    assert (await http_client.get("/api/auth/yo", headers=auth_viejo)).status_code == 200

    await _solicitar(http_client, cuenta["email"])
    assert (await _restablecer(http_client, cuenta["email"], correos[0][1])).status_code == 204

    assert (await http_client.get("/api/auth/yo", headers=auth_viejo)).status_code == 401
    r = await http_client.post("/api/auth/refresh", json={"refresh_token": refresh_viejo})
    assert r.status_code == 401

    # La sesión nueva, con la contraseña nueva, sí funciona.
    login = await _login(http_client, cuenta["email"], NUEVA)
    assert login.status_code == 200
    auth_nuevo = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await http_client.get("/api/auth/yo", headers=auth_nuevo)).status_code == 200
    r = await http_client.post(
        "/api/auth/refresh", json={"refresh_token": crear_refresh_token(cuenta["usuario_id"])}
    )
    assert r.status_code == 200
