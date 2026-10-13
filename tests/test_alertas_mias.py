"""
GET /api/alertas/mias/no-leidas y PATCH /api/alertas/mias/marcar-todas-leidas:
el contador y el "marcar todas" de la campana de la app móvil.

Mismo criterio de visibilidad que GET /api/alertas/mias: personales y, si el
rol está en la room del negocio (owner/superadmin/member), las del negocio.
"""

from uuid import uuid4

import pytest

from routers.alertas import crear_alerta
from security import crear_access_token, hash_password
from services import push
from session import execute, fetch_value


@pytest.fixture(autouse=True)
def sin_push(monkeypatch):
    """crear_alerta no programa push, pero por si algún test usa broadcast_alerta."""
    monkeypatch.setattr(push.settings, "PUSH_ACTIVO", False)


async def _usuario(tenant_id, role: str) -> dict:
    uid = uuid4()
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, $5, true)
        """,
        uid,
        tenant_id,
        f"test-{uid}@ejemplo.com",
        hash_password("x"),
        role,
    )
    return {"id": uid, "headers": {"Authorization": f"Bearer {crear_access_token(uid, tenant_id, role)}"}}


async def test_conteo_owner_incluye_negocio_y_suyas(http_client, tenant_y_usuario, clean_alertas):
    tid = tenant_y_usuario["tenant_id"]
    owner = {"Authorization": f"Bearer {tenant_y_usuario['token']}"}
    otro = await _usuario(tid, "vendedor")
    await crear_alerta(tid, "nuevo_lead", "negocio", "m")
    await crear_alerta(tid, "perfil_incompleto", "mía", "m", portal_user_id=tenant_y_usuario["usuario_id"])
    await crear_alerta(tid, "agenda_reprogramada", "del vendedor", "m", portal_user_id=otro["id"])

    r = await http_client.get("/api/alertas/mias/no-leidas", headers=owner)

    assert r.status_code == 200
    assert r.json() == {"no_leidas": 2}


async def test_conteo_vendedor_solo_personales_y_sin_tope(http_client, tenant_y_usuario, clean_alertas):
    tid = tenant_y_usuario["tenant_id"]
    vendedor = await _usuario(tid, "vendedor")
    await crear_alerta(tid, "nuevo_lead", "negocio", "m")
    # Más que el tope de /alertas/mias (100): el contador no puede cortarse ahí.
    for i in range(105):
        await crear_alerta(tid, "agenda_reprogramada", f"lead {i}", "m", portal_user_id=vendedor["id"])

    r = await http_client.get("/api/alertas/mias/no-leidas", headers=vendedor["headers"])

    assert r.json() == {"no_leidas": 105}


async def test_marcar_todas_solo_toca_las_visibles(http_client, tenant_y_usuario, clean_alertas):
    tid = tenant_y_usuario["tenant_id"]
    vendedor = await _usuario(tid, "vendedor")
    companero = await _usuario(tid, "vendedor")
    await crear_alerta(tid, "nuevo_lead", "negocio", "m")
    await crear_alerta(tid, "agenda_reprogramada", "suya 1", "m", portal_user_id=vendedor["id"])
    await crear_alerta(tid, "agenda_reprogramada", "suya 2", "m", portal_user_id=vendedor["id"])
    await crear_alerta(tid, "agenda_reprogramada", "del compañero", "m", portal_user_id=companero["id"])

    r = await http_client.patch("/api/alertas/mias/marcar-todas-leidas", headers=vendedor["headers"])

    assert r.status_code == 200
    assert r.json() == {"marcadas": 2}
    # Ni la del negocio (el vendedor no la ve) ni la del compañero cambian.
    sin_leer = await fetch_value(
        "SELECT array_agg(titulo ORDER BY titulo) FROM alertas WHERE tenant_id = $1 AND NOT leido", tid
    )
    assert sin_leer == ["del compañero", "negocio"]

    conteo = await http_client.get("/api/alertas/mias/no-leidas", headers=vendedor["headers"])
    assert conteo.json() == {"no_leidas": 0}


async def test_marcar_todas_es_idempotente(http_client, tenant_y_usuario, clean_alertas):
    headers = {"Authorization": f"Bearer {tenant_y_usuario['token']}"}
    await crear_alerta(tenant_y_usuario["tenant_id"], "nuevo_lead", "negocio", "m")

    primera = await http_client.patch("/api/alertas/mias/marcar-todas-leidas", headers=headers)
    segunda = await http_client.patch("/api/alertas/mias/marcar-todas-leidas", headers=headers)

    assert primera.json() == {"marcadas": 1}
    assert segunda.json() == {"marcadas": 0}


async def test_endpoints_exigen_sesion(http_client):
    assert (await http_client.get("/api/alertas/mias/no-leidas")).status_code == 401
    assert (await http_client.patch("/api/alertas/mias/marcar-todas-leidas")).status_code == 401
