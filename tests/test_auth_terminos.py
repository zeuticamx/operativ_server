"""
Aceptación obligatoria de Términos y Condiciones (routers/auth.py).

Lo que se cubre:

  1. /registro: sin la casilla (ausente, false, o algo que no sea el
     booleano true) es 422 y no se guarda ni el alta pendiente; con ella se
     guarda la evidencia.
  2. /verificar: la cuenta nace con la evidencia del paso 1; un alta
     pendiente sin evidencia (anterior a la migración) no crea cuenta.
  3. /google: una cuenta nueva sin aceptación NO se crea (428, ni tenant ni
     usuario); con aceptación sí, con evidencia. Una cuenta existente sin
     evidencia debe aceptar en su próximo ingreso.
  4. /login: una cuenta existente sin evidencia recibe 428 (después de
     validar la contraseña) hasta que acepta; una que ya aceptó entra igual
     que antes y su evidencia no se toca.

El correo y la validación del token de Google se sustituyen: lo que se
prueba es nuestra lógica, no SMTP ni Google.
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

import routers.auth as auth
from config import settings
from security import hash_password
from session import execute, fetch_one, fetch_value

PASSWORD = "una-clave-segura"


# ============================================================
# Fixtures
# ============================================================
@pytest.fixture
def correo_simulado(monkeypatch):
    """No manda correos; guarda el último código para poder verificar."""
    enviados: list[dict] = []

    async def enviar(email: str, codigo: str, negocio: str) -> None:
        enviados.append({"email": email, "codigo": codigo, "negocio": negocio})

    monkeypatch.setattr(auth, "enviar_codigo_verificacion", enviar)
    return enviados


@pytest.fixture
def google_simulado(monkeypatch):
    """Devuelve `fijar(email, sub)`: lo que 'dice' Google del próximo credential."""
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cliente-de-prueba")
    payload: dict = {}

    async def verificar(_credential: str) -> dict:
        return dict(payload)

    monkeypatch.setattr(auth, "verificar_credential", verificar)

    def fijar(email: str, sub: str, nombre: str = "Ana Prueba") -> None:
        payload.clear()
        payload.update({"email": email, "sub": sub, "name": nombre, "email_verified": True})

    return fijar


@pytest.fixture
async def limpieza(db):
    """Borra lo que creen los tests, sea cual sea el resultado."""
    correos: list[str] = []
    yield correos
    for email in correos:
        await execute("DELETE FROM email_verifications WHERE email = $1", email)
        tenant_id = await fetch_value(
            "SELECT tenant_id FROM portal_users WHERE LOWER(email) = $1", email
        )
        if tenant_id:
            await execute("DELETE FROM tenants WHERE id = $1", tenant_id)


def _correo() -> str:
    return f"terminos-{uuid4().hex[:12]}@ejemplo.com"


def _registro(email: str, **extra) -> dict:
    return {
        "email": email,
        "password": PASSWORD,
        "full_name": "Ana Prueba",
        "nombre_negocio": "Negocio de prueba",
        **extra,
    }


async def _usuario(email: str):
    return await fetch_one(
        """
        SELECT id, tenant_id, terminos_aceptados_en, terminos_version, google_id
        FROM portal_users WHERE LOWER(email) = $1
        """,
        email,
    )


async def _cuenta_existente(limpieza, *, aceptada: bool, google_id: str | None = None) -> str:
    """Un dueño ya dado de alta, con o sin evidencia de aceptación."""
    email = _correo()
    limpieza.append(email)
    tenant_id = await fetch_value("INSERT INTO tenants (name) VALUES ('Existente') RETURNING id")
    await execute(
        """
        INSERT INTO portal_users
            (tenant_id, email, password_hash, role, is_active, google_id,
             terminos_aceptados_en, terminos_version)
        VALUES ($1, $2, $3, 'owner', true, $4, $5, $6)
        """,
        tenant_id,
        email,
        hash_password(PASSWORD),
        google_id,
        datetime(2026, 1, 15, tzinfo=timezone.utc) if aceptada else None,
        "2026-01-15" if aceptada else None,
    )
    return email


# ============================================================
# 1. /registro
# ============================================================
@pytest.mark.asyncio
async def test_registro_sin_el_campo_es_422_y_no_guarda_nada(
    http_client, correo_simulado, limpieza
):
    email = _correo()
    limpieza.append(email)

    r = await http_client.post("/api/auth/registro", json=_registro(email))

    assert r.status_code == 422
    assert correo_simulado == []
    assert await fetch_value("SELECT COUNT(*) FROM email_verifications WHERE email = $1", email) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("valor", [False, None, "true", 1, "si"])
async def test_registro_con_algo_que_no_es_true_es_422(
    http_client, correo_simulado, limpieza, valor
):
    """false, null y los "casi true" (string, número) no cuentan como aceptación."""
    email = _correo()
    limpieza.append(email)

    r = await http_client.post("/api/auth/registro", json=_registro(email, acepta_terminos=valor))

    assert r.status_code == 422
    assert correo_simulado == []
    assert await fetch_value("SELECT COUNT(*) FROM email_verifications WHERE email = $1", email) == 0


@pytest.mark.asyncio
async def test_registro_aceptando_guarda_la_evidencia_del_alta_pendiente(
    http_client, correo_simulado, limpieza
):
    email = _correo()
    limpieza.append(email)

    r = await http_client.post("/api/auth/registro", json=_registro(email, acepta_terminos=True))

    assert r.status_code == 202
    assert len(correo_simulado) == 1
    fila = await fetch_one(
        "SELECT terminos_aceptados_en, terminos_version FROM email_verifications WHERE email = $1",
        email,
    )
    assert fila["terminos_aceptados_en"] is not None
    assert fila["terminos_version"] == settings.TERMINOS_VERSION


# ============================================================
# 2. /verificar
# ============================================================
@pytest.mark.asyncio
async def test_la_cuenta_creada_al_verificar_conserva_la_evidencia(
    http_client, correo_simulado, limpieza
):
    email = _correo()
    limpieza.append(email)
    await http_client.post("/api/auth/registro", json=_registro(email, acepta_terminos=True))
    pendiente = await fetch_value(
        "SELECT terminos_aceptados_en FROM email_verifications WHERE email = $1", email
    )

    r = await http_client.post(
        "/api/auth/verificar", json={"email": email, "codigo": correo_simulado[-1]["codigo"]}
    )

    assert r.status_code == 201
    usuario = await _usuario(email)
    assert usuario["terminos_aceptados_en"] == pendiente
    assert usuario["terminos_version"] == settings.TERMINOS_VERSION


@pytest.mark.asyncio
async def test_un_alta_pendiente_sin_evidencia_no_crea_cuenta(
    http_client, correo_simulado, limpieza
):
    """Las pendientes anteriores a la migración no traen aceptación."""
    email = _correo()
    limpieza.append(email)
    await http_client.post("/api/auth/registro", json=_registro(email, acepta_terminos=True))
    await execute(
        "UPDATE email_verifications SET terminos_aceptados_en = NULL, terminos_version = NULL WHERE email = $1",
        email,
    )

    r = await http_client.post(
        "/api/auth/verificar", json={"email": email, "codigo": correo_simulado[-1]["codigo"]}
    )

    assert r.status_code == 400
    assert await _usuario(email) is None
    assert await fetch_value("SELECT COUNT(*) FROM email_verifications WHERE email = $1", email) == 0


# ============================================================
# 3. /google
# ============================================================
@pytest.mark.asyncio
@pytest.mark.parametrize("cuerpo_extra", [{}, {"acepta_terminos": False}])
async def test_google_cuenta_nueva_sin_aceptar_no_se_crea(
    http_client, google_simulado, limpieza, cuerpo_extra
):
    email = _correo()
    limpieza.append(email)
    google_simulado(email, f"g-{uuid4().hex}")
    tenants_antes = await fetch_value("SELECT COUNT(*) FROM tenants")

    r = await http_client.post("/api/auth/google", json={"credential": "x", **cuerpo_extra})

    assert r.status_code == 428
    assert r.json()["detail"]["codigo"] == "terminos_requeridos"
    assert r.json()["detail"]["cuenta_nueva"] is True
    assert "access_token" not in r.text
    # Ni usuario ni tenant: la transacción entera se descartó.
    assert await _usuario(email) is None
    assert await fetch_value("SELECT COUNT(*) FROM tenants") == tenants_antes


@pytest.mark.asyncio
async def test_google_cuenta_nueva_aceptando_se_crea_con_evidencia(
    http_client, google_simulado, limpieza
):
    email = _correo()
    limpieza.append(email)
    sub = f"g-{uuid4().hex}"
    google_simulado(email, sub)

    r = await http_client.post(
        "/api/auth/google", json={"credential": "x", "acepta_terminos": True}
    )

    assert r.status_code == 200
    assert r.json()["access_token"]
    usuario = await _usuario(email)
    assert usuario["google_id"] == sub
    assert usuario["terminos_aceptados_en"] is not None
    assert usuario["terminos_version"] == settings.TERMINOS_VERSION


@pytest.mark.asyncio
async def test_google_cuenta_existente_sin_evidencia_debe_aceptar_y_entonces_se_guarda(
    http_client, google_simulado, limpieza
):
    email = await _cuenta_existente(limpieza, aceptada=False, google_id="g-previo")
    google_simulado(email, "g-previo")

    sin = await http_client.post("/api/auth/google", json={"credential": "x"})
    assert sin.status_code == 428
    assert sin.json()["detail"]["cuenta_nueva"] is False
    assert (await _usuario(email))["terminos_aceptados_en"] is None

    con = await http_client.post(
        "/api/auth/google", json={"credential": "x", "acepta_terminos": True}
    )
    assert con.status_code == 200
    usuario = await _usuario(email)
    assert usuario["terminos_aceptados_en"] is not None
    assert usuario["terminos_version"] == settings.TERMINOS_VERSION


@pytest.mark.asyncio
async def test_google_vincular_una_cuenta_sin_evidencia_tampoco_entra_sin_aceptar(
    http_client, google_simulado, limpieza
):
    """La vinculación por correo se revierte junto con el 428."""
    email = await _cuenta_existente(limpieza, aceptada=False, google_id=None)
    google_simulado(email, "g-nuevo")

    r = await http_client.post("/api/auth/google", json={"credential": "x"})

    assert r.status_code == 428
    assert (await _usuario(email))["google_id"] is None


@pytest.mark.asyncio
async def test_google_cuenta_ya_aceptada_entra_sin_mandar_nada_y_no_pisa_la_evidencia(
    http_client, google_simulado, limpieza
):
    email = await _cuenta_existente(limpieza, aceptada=True, google_id="g-ok")
    google_simulado(email, "g-ok")
    antes = (await _usuario(email))["terminos_aceptados_en"]

    r = await http_client.post(
        "/api/auth/google", json={"credential": "x", "acepta_terminos": True}
    )

    assert r.status_code == 200
    usuario = await _usuario(email)
    assert usuario["terminos_aceptados_en"] == antes
    assert usuario["terminos_version"] == "2026-01-15"


# ============================================================
# 4. /login
# ============================================================
@pytest.mark.asyncio
async def test_login_de_una_cuenta_anterior_pide_aceptar_y_despues_entra(
    http_client, limpieza
):
    email = await _cuenta_existente(limpieza, aceptada=False)

    sin = await http_client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert sin.status_code == 428
    assert sin.json()["detail"]["codigo"] == "terminos_requeridos"
    assert "access_token" not in sin.text
    assert (await _usuario(email))["terminos_aceptados_en"] is None

    con = await http_client.post(
        "/api/auth/login",
        json={"email": email, "password": PASSWORD, "acepta_terminos": True},
    )
    assert con.status_code == 200
    usuario = await _usuario(email)
    assert usuario["terminos_aceptados_en"] is not None
    assert usuario["terminos_version"] == settings.TERMINOS_VERSION

    # A partir de ahí entra sin mandar nada.
    despues = await http_client.post(
        "/api/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert despues.status_code == 200


@pytest.mark.asyncio
async def test_login_con_contrasena_incorrecta_no_delata_si_falta_aceptar(
    http_client, limpieza
):
    """El 428 va después de la contraseña: no sirve para confirmar un correo."""
    email = await _cuenta_existente(limpieza, aceptada=False)

    r = await http_client.post(
        "/api/auth/login", json={"email": email, "password": "otra-clave-distinta"}
    )

    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_de_una_cuenta_ya_aceptada_no_cambia(http_client, limpieza):
    email = await _cuenta_existente(limpieza, aceptada=True)
    antes = (await _usuario(email))["terminos_aceptados_en"]

    r = await http_client.post(
        "/api/auth/login",
        json={"email": email, "password": PASSWORD, "acepta_terminos": True},
    )

    assert r.status_code == 200
    usuario = await _usuario(email)
    assert usuario["terminos_aceptados_en"] == antes
    assert usuario["terminos_version"] == "2026-01-15"
