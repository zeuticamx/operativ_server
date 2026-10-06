"""
Cuentas del equipo por invitación: routers/equipo.py (lado del dueño) y
/auth/invitacion/* (lado del invitado). Ver services/invitaciones.py.
"""

from uuid import uuid4

import pytest

from security import crear_access_token, hash_password
from services.pipeline import set_tenant_servicios
from session import execute, fetch_one, fetch_value

pytestmark = pytest.mark.usefixtures("plan_enterprise")


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _token_de(enlace: str) -> str:
    assert "#t=" in enlace, enlace
    return enlace.split("#t=", 1)[1]


async def _cuenta(tenant_id, role: str) -> tuple[str, str]:
    uid = uuid4()
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, $5, true)
        """,
        uid,
        tenant_id,
        f"{role}-{uid}@ejemplo.com",
        hash_password("test_password"),
        role,
    )
    return uid, crear_access_token(uid, tenant_id, role)


async def _ficha(tenant_id, nombre="Ana", activo=True):
    return await fetch_value(
        "INSERT INTO vendedores (tenant_id, nombre, activo) VALUES ($1, $2, $3) RETURNING id",
        tenant_id,
        nombre,
        activo,
    )


def _correo() -> str:
    return f"invitado-{uuid4()}@ejemplo.com"


@pytest.fixture
async def dueno(tenant_y_usuario):
    await set_tenant_servicios(tenant_y_usuario["tenant_id"], None, True)
    return tenant_y_usuario


async def _invitar(http_client, dueno, **cuerpo):
    return await http_client.post("/api/equipo/invitaciones", json=cuerpo, headers=_h(dueno["token"]))


async def _aceptar(http_client, token, password="clave-segura-1", acepta=True):
    return await http_client.post(
        "/api/auth/invitacion/aceptar",
        json={"token": token, "password": password, "acepta_terminos": acepta},
    )


# ============================================================
# Flujo completo
# ============================================================
@pytest.mark.asyncio
async def test_invitar_y_aceptar_member(http_client, dueno):
    email = _correo()
    r = await _invitar(http_client, dueno, email=email, role="member")
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert cuerpo["role"] == "member"
    assert cuerpo["vencida"] is False
    token = _token_de(cuerpo["enlace"])

    # Solo el hash queda en la base.
    guardado = await fetch_value("SELECT token_hash FROM invitaciones_equipo WHERE id = $1", cuerpo["id"])
    assert token not in guardado

    info = await http_client.post("/api/auth/invitacion/revisar", json={"token": token})
    assert info.status_code == 200
    assert info.json()["email"] == email
    assert info.json()["nombre_negocio"] == "Test Tenant"

    r = await _aceptar(http_client, token)
    assert r.status_code == 201, r.text
    sesion = r.json()["access_token"]

    yo = await http_client.get("/api/auth/yo", headers=_h(sesion))
    assert yo.json()["role"] == "member"
    assert yo.json()["tenant_id"] == str(dueno["tenant_id"])

    # Un solo uso, y ya no aparece como pendiente.
    assert (await _aceptar(http_client, token)).status_code == 410
    pendientes = await http_client.get("/api/equipo/invitaciones", headers=_h(dueno["token"]))
    assert pendientes.json() == []

    # Entra con la contraseña que eligió, no con una que puso el dueño.
    login = await http_client.post("/api/auth/login", json={"email": email, "password": "clave-segura-1"})
    assert login.status_code == 200


@pytest.mark.asyncio
async def test_invitar_vendedor_liga_la_ficha(http_client, dueno):
    ficha = await _ficha(dueno["tenant_id"])
    r = await _invitar(http_client, dueno, email=_correo(), role="vendedor", vendedor_id=str(ficha))
    assert r.status_code == 201, r.text
    assert r.json()["vendedor_nombre"] == "Ana"

    r = await _aceptar(http_client, _token_de(r.json()["enlace"]))
    assert r.status_code == 201
    sesion = r.json()["access_token"]

    yo = await http_client.get("/api/vendedores/yo", headers=_h(sesion))
    assert yo.status_code == 200
    assert yo.json()["id"] == str(ficha)

    usuarios = await http_client.get("/api/equipo/usuarios", headers=_h(dueno["token"]))
    vendedor = next(u for u in usuarios.json() if u["role"] == "vendedor")
    assert vendedor["vendedor_id"] == str(ficha)


# ============================================================
# Quién puede invitar y a quién
# ============================================================
@pytest.mark.asyncio
async def test_solo_gerencia_invita(http_client, dueno):
    _, token_member = await _cuenta(dueno["tenant_id"], "member")
    r = await http_client.post(
        "/api/equipo/invitaciones",
        json={"email": _correo(), "role": "member"},
        headers=_h(token_member),
    )
    assert r.status_code == 403
    assert (await http_client.get("/api/equipo/usuarios", headers=_h(token_member))).status_code == 403


@pytest.mark.asyncio
async def test_no_se_invita_owner(http_client, dueno):
    r = await _invitar(http_client, dueno, email=_correo(), role="owner")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_ficha_segun_rol(http_client, dueno):
    ficha = await _ficha(dueno["tenant_id"])
    assert (await _invitar(http_client, dueno, email=_correo(), role="vendedor")).status_code == 422
    r = await _invitar(http_client, dueno, email=_correo(), role="member", vendedor_id=str(ficha))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_correo_con_cuenta_no_se_invita(http_client, dueno):
    r = await _invitar(http_client, dueno, email=dueno["email"], role="member")
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_ficha_de_otro_negocio_da_404(http_client, dueno):
    otro = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro')", otro)
    try:
        ajena = await _ficha(otro)
        r = await _invitar(http_client, dueno, email=_correo(), role="vendedor", vendedor_id=str(ajena))
        assert r.status_code == 404
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", otro)


@pytest.mark.asyncio
async def test_ficha_inactiva_o_con_acceso_da_409(http_client, dueno):
    inactiva = await _ficha(dueno["tenant_id"], "Beto", activo=False)
    r = await _invitar(http_client, dueno, email=_correo(), role="vendedor", vendedor_id=str(inactiva))
    assert r.status_code == 409

    uid, _ = await _cuenta(dueno["tenant_id"], "vendedor")
    con_acceso = await _ficha(dueno["tenant_id"], "Carla")
    await execute("UPDATE vendedores SET portal_user_id = $1 WHERE id = $2", uid, con_acceso)
    r = await _invitar(http_client, dueno, email=_correo(), role="vendedor", vendedor_id=str(con_acceso))
    assert r.status_code == 409


# ============================================================
# Ciclo de vida del enlace
# ============================================================
@pytest.mark.asyncio
async def test_invitar_de_nuevo_mata_el_enlace_anterior(http_client, dueno):
    email = _correo()
    viejo = _token_de((await _invitar(http_client, dueno, email=email, role="member")).json()["enlace"])
    nuevo = _token_de((await _invitar(http_client, dueno, email=email, role="member")).json()["enlace"])

    assert (await _aceptar(http_client, viejo)).status_code == 410
    assert (await _aceptar(http_client, nuevo)).status_code == 201


@pytest.mark.asyncio
async def test_reenviar_cambia_el_enlace(http_client, dueno):
    creada = (await _invitar(http_client, dueno, email=_correo(), role="member")).json()
    viejo = _token_de(creada["enlace"])

    r = await http_client.post(
        f"/api/equipo/invitaciones/{creada['id']}/reenviar", headers=_h(dueno["token"])
    )
    assert r.status_code == 200
    nuevo = _token_de(r.json()["enlace"])
    assert nuevo != viejo

    assert (await _aceptar(http_client, viejo)).status_code == 410
    assert (await _aceptar(http_client, nuevo)).status_code == 201


@pytest.mark.asyncio
async def test_revocar(http_client, dueno):
    creada = (await _invitar(http_client, dueno, email=_correo(), role="member")).json()
    r = await http_client.delete(f"/api/equipo/invitaciones/{creada['id']}", headers=_h(dueno["token"]))
    assert r.status_code == 204
    assert (await _aceptar(http_client, _token_de(creada["enlace"]))).status_code == 410
    # Revocar dos veces: ya no existe como pendiente.
    r = await http_client.delete(f"/api/equipo/invitaciones/{creada['id']}", headers=_h(dueno["token"]))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_invitacion_vencida(http_client, dueno):
    creada = (await _invitar(http_client, dueno, email=_correo(), role="member")).json()
    await execute(
        "UPDATE invitaciones_equipo SET expira_en = NOW() - INTERVAL '1 minute' WHERE id = $1",
        creada["id"],
    )
    token = _token_de(creada["enlace"])
    assert (await http_client.post("/api/auth/invitacion/revisar", json={"token": token})).status_code == 410
    assert (await _aceptar(http_client, token)).status_code == 410

    pendientes = await http_client.get("/api/equipo/invitaciones", headers=_h(dueno["token"]))
    assert pendientes.json()[0]["vencida"] is True


@pytest.mark.asyncio
async def test_token_inventado(http_client, dueno):
    r = await http_client.post("/api/auth/invitacion/revisar", json={"token": "x" * 43})
    assert r.status_code == 410


@pytest.mark.asyncio
async def test_sin_aceptar_terminos_no_se_crea(http_client, dueno):
    email = _correo()
    creada = (await _invitar(http_client, dueno, email=email, role="member")).json()
    r = await _aceptar(http_client, _token_de(creada["enlace"]), acepta=False)
    assert r.status_code == 422
    assert await fetch_one("SELECT 1 FROM portal_users WHERE email = $1", email) is None


@pytest.mark.asyncio
async def test_ficha_desactivada_despues_de_invitar(http_client, dueno):
    email = _correo()
    ficha = await _ficha(dueno["tenant_id"])
    creada = (await _invitar(http_client, dueno, email=email, role="vendedor", vendedor_id=str(ficha))).json()
    await execute("UPDATE vendedores SET activo = false WHERE id = $1", ficha)

    r = await _aceptar(http_client, _token_de(creada["enlace"]))
    assert r.status_code == 409
    # Todo o nada: ni cuenta ni invitación cerrada.
    assert await fetch_one("SELECT 1 FROM portal_users WHERE email = $1", email) is None
    assert await fetch_value(
        "SELECT aceptada_en FROM invitaciones_equipo WHERE id = $1", creada["id"]
    ) is None


# ============================================================
# Quitar y devolver el acceso
# ============================================================
@pytest.mark.asyncio
async def test_quitar_acceso_corta_la_sesion(http_client, dueno):
    uid, token_member = await _cuenta(dueno["tenant_id"], "member")
    assert (await http_client.get("/api/auth/yo", headers=_h(token_member))).status_code == 200

    r = await http_client.patch(
        f"/api/equipo/usuarios/{uid}", json={"activo": False}, headers=_h(dueno["token"])
    )
    assert r.status_code == 200
    assert r.json()["activo"] is False
    assert (await http_client.get("/api/auth/yo", headers=_h(token_member))).status_code == 401

    r = await http_client.patch(
        f"/api/equipo/usuarios/{uid}", json={"activo": True}, headers=_h(dueno["token"])
    )
    assert r.status_code == 200
    assert (await http_client.get("/api/auth/yo", headers=_h(token_member))).status_code == 200


@pytest.mark.asyncio
async def test_no_se_quita_el_acceso_al_dueno(http_client, dueno):
    r = await http_client.patch(
        f"/api/equipo/usuarios/{dueno['usuario_id']}", json={"activo": False}, headers=_h(dueno["token"])
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_usuario_de_otro_negocio_da_404(http_client, dueno):
    otro = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro')", otro)
    try:
        uid, _ = await _cuenta(otro, "member")
        r = await http_client.patch(
            f"/api/equipo/usuarios/{uid}", json={"activo": False}, headers=_h(dueno["token"])
        )
        assert r.status_code == 404
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", otro)
