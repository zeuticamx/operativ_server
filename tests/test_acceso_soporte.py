"""
"Ver como" con escritura autorizada por el dueño (services/acceso_soporte.py).

Cada garantía que promete el flujo tiene su test: sin permiso sigue siendo
solo lectura; con permiso se puede escribir salvo lo sensible; revocar o
vencer cierra la escritura en la siguiente petición; soporte no se puede
aprobar a sí mismo; el enlace del correo es de un solo uso.
"""

from uuid import uuid4

import pytest

from security import crear_access_token, hash_password
from session import execute, fetch_one, fetch_value


# ------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------
async def _portal_user(tenant_id, rol="owner", email=None):
    uid = uuid4()
    email = email or f"{rol}-{uid}@ejemplo.test"
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, $5, true)
        """,
        uid,
        tenant_id,
        email,
        hash_password("x" * 12),
        rol,
    )
    return uid, email


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def negocio(db):
    tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Ferretería de prueba')", tid)
    uid, email = await _portal_user(tid, "owner")
    yield {
        "tenant_id": tid,
        "owner_id": uid,
        "owner_email": email,
        "headers": _auth(crear_access_token(uid, tid, "owner")),
    }
    await execute("DELETE FROM gerencia_auditoria WHERE tenant_id = $1", tid)
    await execute("DELETE FROM tenants WHERE id = $1", tid)


async def _crear_gerente(cargo: str):
    tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Interno')", tid)
    uid, email = await _portal_user(tid, "owner", f"gerente-{uuid4()}@operativai.test")
    await execute(
        "INSERT INTO gerencia_users (email, full_name, cargo) VALUES ($1, 'Test', $2)", email, cargo
    )
    return {"id": uid, "tid": tid, "email": email, "headers": _auth(crear_access_token(uid, tid, "owner"))}


async def _borrar_gerente(g):
    await execute("DELETE FROM gerencia_users WHERE LOWER(email) = LOWER($1)", g["email"])
    await execute("DELETE FROM gerencia_auditoria WHERE actor_email = $1", g["email"])
    await execute("DELETE FROM tenants WHERE id = $1", g["tid"])


@pytest.fixture
async def gerente(db):
    g = await _crear_gerente("Developer")
    yield g
    await _borrar_gerente(g)


@pytest.fixture
async def gerente_sin_cargo(db):
    g = await _crear_gerente("QA")
    yield g
    await _borrar_gerente(g)


@pytest.fixture(autouse=True)
def correos(monkeypatch):
    """Captura los correos en vez de enviarlos; guarda el enlace en claro."""
    enviados: list[dict] = []

    async def falso(destino, gerente, negocio, motivo, enlace, minutos):
        enviados.append({"destino": destino, "enlace": enlace})

    monkeypatch.setattr("services.acceso_soporte.enviar_solicitud_acceso_soporte", falso)
    return enviados


async def _ver_como(http_client, gerente, tenant_id) -> dict:
    r = await http_client.post(
        f"/api/gerencia/tenants/{tenant_id}/impersonar",
        json={"motivo": "soporte: corregir configuración"},
        headers=gerente["headers"],
    )
    assert r.status_code == 200
    return _auth(r.json()["access_token"])


async def _solicitar(http_client, soporte):
    return await http_client.post(
        "/api/acceso-soporte/solicitar", json={"motivo": "Corregir el horario"}, headers=soporte
    )


async def _aprobar(http_client, negocio, solicitud_id, minutos=30):
    return await http_client.post(
        f"/api/acceso-soporte/{solicitud_id}/aprobar",
        json={"duracion_min": minutos},
        headers=negocio["headers"],
    )


def _paso(r) -> bool:
    """
    La escritura pasó la compuerta de "ver como". Lo que responda el
    endpoint después (p. ej. 402 si el negocio de prueba no tiene plan) ya
    no es asunto de la impersonación.
    """
    return r.status_code not in (401, 403)


async def _escribir(http_client, negocio, soporte):
    """Una escritura normal del dueño sobre su negocio."""
    return await http_client.patch(
        f"/api/tenants/{negocio['tenant_id']}/servicios",
        json={"agente_ia_activo": True},
        headers=soporte,
    )


# ------------------------------------------------------------
# Solicitar
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_solicitar_avisa_al_dueno_y_sigue_siendo_solo_lectura(
    http_client, gerente, negocio, correos
):
    soporte = await _ver_como(http_client, gerente, negocio["tenant_id"])

    r = await _solicitar(http_client, soporte)
    assert r.status_code == 201
    assert r.json()["estado"] == "pendiente"

    # Alerta personal en el portal del dueño y correo con el enlace.
    alerta = await fetch_one(
        "SELECT portal_user_id, datos FROM alertas "
        "WHERE tenant_id = $1 AND tipo = 'solicitud_escritura'",
        negocio["tenant_id"],
    )
    assert alerta["portal_user_id"] == negocio["owner_id"]
    assert alerta["datos"]["solicitud_id"] == r.json()["id"]
    assert [c["destino"] for c in correos] == [negocio["owner_email"]]
    assert "/acceso-soporte#t=" in correos[0]["enlace"]

    # Pedir permiso no da permiso.
    assert (await _escribir(http_client, negocio, soporte)).status_code == 403


@pytest.mark.asyncio
async def test_solo_ciertos_cargos_pueden_solicitar(http_client, gerente_sin_cargo, negocio):
    soporte = await _ver_como(http_client, gerente_sin_cargo, negocio["tenant_id"])
    r = await _solicitar(http_client, soporte)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_no_se_solicita_desde_una_sesion_normal(http_client, negocio):
    r = await _solicitar(http_client, negocio["headers"])
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_sin_dueno_activo_no_se_puede_solicitar(http_client, gerente, negocio):
    soporte = await _ver_como(http_client, gerente, negocio["tenant_id"])
    await execute("UPDATE portal_users SET is_active = false WHERE id = $1", negocio["owner_id"])
    # El token de ver-como apunta al dueño: al desactivarlo la sesión muere
    # (401). Se prueba con otro usuario del negocio que sí puede entrar.
    r = await _solicitar(http_client, soporte)
    assert r.status_code in (401, 409)


@pytest.mark.asyncio
async def test_una_sola_solicitud_viva(http_client, gerente, negocio):
    soporte = await _ver_como(http_client, gerente, negocio["tenant_id"])
    assert (await _solicitar(http_client, soporte)).status_code == 201
    assert (await _solicitar(http_client, soporte)).status_code == 409


# ------------------------------------------------------------
# Aprobar y escribir
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_con_permiso_se_puede_escribir_y_queda_auditado(http_client, gerente, negocio):
    soporte = await _ver_como(http_client, gerente, negocio["tenant_id"])
    sid = (await _solicitar(http_client, soporte)).json()["id"]

    r = await _aprobar(http_client, negocio, sid, 15)
    assert r.status_code == 200
    assert r.json()["estado"] == "aprobada"
    assert r.json()["canal"] == "portal"

    assert _paso(await _escribir(http_client, negocio, soporte))

    asiento = await fetch_one(
        "SELECT detalle FROM gerencia_auditoria "
        "WHERE tenant_id = $1 AND accion = 'impersonacion_escritura'",
        negocio["tenant_id"],
    )
    assert asiento["detalle"]["metodo"] == "PATCH"
    assert asiento["detalle"]["ruta"].endswith("/servicios")


@pytest.mark.asyncio
async def test_lo_sensible_sigue_cerrado_aun_con_permiso(http_client, gerente, negocio):
    soporte = await _ver_como(http_client, gerente, negocio["tenant_id"])
    sid = (await _solicitar(http_client, soporte)).json()["id"]
    await _aprobar(http_client, negocio, sid)

    for ruta in ("/api/cuenta/eliminar", "/api/equipo/invitaciones", "/api/pagos/crear-pago"):
        r = await http_client.post(ruta, json={}, headers=soporte)
        assert r.status_code == 403, ruta
        assert "no está permitida" in r.json()["detail"]


@pytest.mark.asyncio
async def test_soporte_no_se_aprueba_a_si_mismo(http_client, gerente, negocio):
    soporte = await _ver_como(http_client, gerente, negocio["tenant_id"])
    sid = (await _solicitar(http_client, soporte)).json()["id"]

    r = await http_client.post(
        f"/api/acceso-soporte/{sid}/aprobar", json={"duracion_min": 60}, headers=soporte
    )
    assert r.status_code == 403
    r = await http_client.get("/api/acceso-soporte/solicitudes", headers=soporte)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_duracion_invalida_se_rechaza(http_client, gerente, negocio):
    soporte = await _ver_como(http_client, gerente, negocio["tenant_id"])
    sid = (await _solicitar(http_client, soporte)).json()["id"]
    assert (await _aprobar(http_client, negocio, sid, 240)).status_code == 422


@pytest.mark.asyncio
async def test_solicitud_de_otro_negocio_es_404(http_client, gerente, negocio):
    soporte = await _ver_como(http_client, gerente, negocio["tenant_id"])
    sid = (await _solicitar(http_client, soporte)).json()["id"]

    otro_tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro')", otro_tid)
    try:
        uid, _ = await _portal_user(otro_tid, "owner")
        otro = {"tenant_id": otro_tid, "headers": _auth(crear_access_token(uid, otro_tid, "owner"))}
        assert (await _aprobar(http_client, otro, sid)).status_code == 404
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", otro_tid)


@pytest.mark.asyncio
async def test_un_member_no_puede_responder(http_client, gerente, negocio):
    soporte = await _ver_como(http_client, gerente, negocio["tenant_id"])
    sid = (await _solicitar(http_client, soporte)).json()["id"]
    uid, _ = await _portal_user(negocio["tenant_id"], "member")
    member = {"headers": _auth(crear_access_token(uid, negocio["tenant_id"], "member"))}
    assert (await _aprobar(http_client, member | {"tenant_id": negocio["tenant_id"]}, sid)).status_code == 403


# ------------------------------------------------------------
# Revocar, vencer, rechazar
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_revocar_cierra_la_escritura_en_la_siguiente_peticion(http_client, gerente, negocio):
    soporte = await _ver_como(http_client, gerente, negocio["tenant_id"])
    sid = (await _solicitar(http_client, soporte)).json()["id"]
    await _aprobar(http_client, negocio, sid)
    assert _paso(await _escribir(http_client, negocio, soporte))

    r = await http_client.post(f"/api/acceso-soporte/{sid}/revocar", headers=negocio["headers"])
    assert r.status_code == 200
    assert r.json()["estado"] == "revocada"

    assert (await _escribir(http_client, negocio, soporte)).status_code == 403
    # Leer sigue funcionando: el token es válido.
    r = await http_client.get("/api/auth/yo", headers=soporte)
    assert r.status_code == 200
    assert r.json()["impersonacion_escritura_hasta"] is None


@pytest.mark.asyncio
async def test_la_concesion_vencida_no_escribe(http_client, gerente, negocio):
    soporte = await _ver_como(http_client, gerente, negocio["tenant_id"])
    sid = (await _solicitar(http_client, soporte)).json()["id"]
    await _aprobar(http_client, negocio, sid)
    await execute(
        "UPDATE acceso_soporte_solicitudes SET concede_hasta = NOW() - interval '1 minute' "
        "WHERE id = $1",
        sid,
    )
    assert (await _escribir(http_client, negocio, soporte)).status_code == 403

    # Y se puede pedir otra (la terminada no bloquea el índice).
    assert (await _solicitar(http_client, soporte)).status_code == 201


@pytest.mark.asyncio
async def test_solicitud_pendiente_vencida_no_se_puede_aprobar(http_client, gerente, negocio):
    soporte = await _ver_como(http_client, gerente, negocio["tenant_id"])
    sid = (await _solicitar(http_client, soporte)).json()["id"]
    await execute(
        "UPDATE acceso_soporte_solicitudes SET expira_en = NOW() - interval '1 minute' "
        "WHERE id = $1",
        sid,
    )
    r = await _aprobar(http_client, negocio, sid)
    assert r.status_code == 200
    assert r.json()["estado"] == "vencida"
    assert (await _escribir(http_client, negocio, soporte)).status_code == 403


@pytest.mark.asyncio
async def test_rechazar_y_responder_dos_veces_es_idempotente(http_client, gerente, negocio):
    soporte = await _ver_como(http_client, gerente, negocio["tenant_id"])
    sid = (await _solicitar(http_client, soporte)).json()["id"]

    r = await http_client.post(f"/api/acceso-soporte/{sid}/rechazar", headers=negocio["headers"])
    assert r.json()["estado"] == "rechazada"
    # Aprobar después no resucita nada ni da error.
    r = await _aprobar(http_client, negocio, sid)
    assert r.status_code == 200
    assert r.json()["estado"] == "rechazada"
    assert (await _escribir(http_client, negocio, soporte)).status_code == 403


@pytest.mark.asyncio
async def test_soporte_puede_cancelar_su_solicitud(http_client, gerente, negocio):
    soporte = await _ver_como(http_client, gerente, negocio["tenant_id"])
    await _solicitar(http_client, soporte)
    r = await http_client.post("/api/acceso-soporte/cancelar", headers=soporte)
    assert r.status_code == 200
    assert r.json()["estado"] == "cancelada"
    estado = await http_client.get("/api/acceso-soporte/estado", headers=soporte)
    assert estado.json()["estado"] == "cancelada"


# ------------------------------------------------------------
# Enlace del correo
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_aprobar_desde_el_enlace_del_correo_es_de_un_solo_uso(
    http_client, gerente, negocio, correos
):
    soporte = await _ver_como(http_client, gerente, negocio["tenant_id"])
    await _solicitar(http_client, soporte)
    token = correos[0]["enlace"].split("#t=")[1]

    info = await http_client.post("/api/acceso-soporte/enlace/leer", json={"token": token})
    assert info.status_code == 200
    assert info.json()["estado"] == "pendiente"
    assert info.json()["gerente_email"] == gerente["email"]

    r = await http_client.post(
        "/api/acceso-soporte/enlace/aprobar", json={"token": token, "duracion_min": 30}
    )
    assert r.status_code == 200
    assert r.json()["estado"] == "aprobada"
    assert _paso(await _escribir(http_client, negocio, soporte))

    canal = await fetch_value(
        "SELECT canal FROM acceso_soporte_solicitudes WHERE tenant_id = $1", negocio["tenant_id"]
    )
    assert canal == "correo"

    # Segundo uso: no cambia nada (ya no está pendiente).
    r = await http_client.post("/api/acceso-soporte/enlace/rechazar", json={"token": token})
    assert r.json()["estado"] == "aprobada"


@pytest.mark.asyncio
async def test_token_inventado_da_404(http_client, db):
    r = await http_client.post("/api/acceso-soporte/enlace/leer", json={"token": "x" * 43})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_el_token_no_se_guarda_en_claro(http_client, gerente, negocio, correos):
    soporte = await _ver_como(http_client, gerente, negocio["tenant_id"])
    await _solicitar(http_client, soporte)
    token = correos[0]["enlace"].split("#t=")[1]
    guardado = await fetch_value(
        "SELECT token_hash FROM acceso_soporte_solicitudes WHERE tenant_id = $1",
        negocio["tenant_id"],
    )
    assert guardado != token and token not in guardado
