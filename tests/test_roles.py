"""
Matriz de permisos por rol dentro de un negocio.

    owner / superadmin  todo
    member              ve y opera el negocio; no configura (agente,
                        canales, herramientas, equipo)
    vendedor            solo lo suyo: su ficha, sus leads, su bitácora

Y el cupo de vendedores activos del plan (planes.max_vendedores).

Se prueba por HTTP: lo que importa es que cada router tenga puesta la
guarda, no la lógica de la guarda (deps.negocio_actual es una línea).
"""

from uuid import uuid4

import pytest

from security import crear_access_token, hash_password
from services.pipeline import set_tenant_servicios
from session import execute, fetch_value

pytestmark = pytest.mark.usefixtures("plan_enterprise")


# ============================================================
# Helpers
# ============================================================
async def _cuenta(tenant_id, role: str) -> tuple[str, str]:
    """(portal_user_id, token) de una cuenta nueva del tenant con ese rol."""
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


async def _vendedor(tenant_id, nombre: str, portal_user_id=None, activo=True):
    return await fetch_value(
        """
        INSERT INTO vendedores (tenant_id, portal_user_id, nombre, activo)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        tenant_id,
        portal_user_id,
        nombre,
        activo,
    )


async def _lead(tenant_id, vendedor_id):
    """Un contacto de chat en 'nuevo', asignado a `vendedor_id`."""
    user_id = await fetch_value(
        "INSERT INTO users (tenant_id, display_name) VALUES ($1, 'Cliente') RETURNING id",
        tenant_id,
    )
    pipeline_id = await fetch_value(
        """
        INSERT INTO client_pipeline (tenant_id, user_id, vendedor_id, estado)
        VALUES ($1, $2, $3, 'nuevo')
        RETURNING id
        """,
        tenant_id,
        user_id,
        vendedor_id,
    )
    await execute(
        """
        INSERT INTO pipeline_historial (client_pipeline_id, estado_anterior, estado_nuevo, vendedor_id)
        VALUES ($1, NULL, 'nuevo', $2)
        """,
        pipeline_id,
        vendedor_id,
    )
    return user_id


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def negocio(tenant_y_usuario):
    """Owner + member + vendedor con ficha (Ana) y otro vendedor (Beto), módulo encendido."""
    tid = tenant_y_usuario["tenant_id"]
    await set_tenant_servicios(tid, None, True)

    _, token_member = await _cuenta(tid, "member")
    uid_vendedor, token_vendedor = await _cuenta(tid, "vendedor")
    ana = await _vendedor(tid, "Ana", portal_user_id=uid_vendedor)
    beto = await _vendedor(tid, "Beto")

    return {
        "tid": tid,
        "owner": tenant_y_usuario["token"],
        "member": token_member,
        "vendedor": token_vendedor,
        "ana": ana,
        "beto": beto,
    }


# ============================================================
# El vendedor no ve el negocio
# ============================================================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ruta",
    [
        "/api/agente",
        "/api/canales",
        "/api/herramientas",
        "/api/conversaciones/metricas",
        "/api/conversaciones/asignables",
        "/api/pagos/suscripcion",
        "/api/pagos/historial",
        "/api/tenants/{tid}/pipeline",
        "/api/tenants/{tid}/metricas",
        "/api/tenants/{tid}/vendedores",
        "/api/tenants/{tid}/config-vendedores",
        "/api/tenants/{tid}/calendario/proveedores",
    ],
)
async def test_vendedor_no_ve_secciones_del_negocio(http_client, negocio, ruta):
    r = await http_client.get(ruta.format(tid=negocio["tid"]), headers=_h(negocio["vendedor"]))
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_vendedor_si_ve_lo_que_necesita_su_portal(http_client, negocio):
    h = _h(negocio["vendedor"])
    # Para el candado de pantallas y los flags del negocio.
    assert (await http_client.get("/api/pagos/acceso", headers=h)).status_code == 200
    r = await http_client.get(f"/api/tenants/{negocio['tid']}/servicios", headers=h)
    assert r.status_code == 200

    yo = await http_client.get("/api/vendedores/yo", headers=h)
    assert yo.status_code == 200
    assert yo.json()["id"] == str(negocio["ana"])


@pytest.mark.asyncio
async def test_yo_es_solo_para_vendedores(http_client, negocio):
    r = await http_client.get("/api/vendedores/yo", headers=_h(negocio["owner"]))
    assert r.status_code == 403


# ============================================================
# El vendedor solo toca lo suyo
# ============================================================
@pytest.mark.asyncio
async def test_vendedor_ve_su_cartera_y_no_la_ajena(http_client, negocio):
    h = _h(negocio["vendedor"])
    await _lead(negocio["tid"], negocio["ana"])

    propia = await http_client.get(f"/api/vendedores/{negocio['ana']}/pipeline", headers=h)
    assert propia.status_code == 200
    assert len(propia.json()) == 1

    ajena = await http_client.get(f"/api/vendedores/{negocio['beto']}/pipeline", headers=h)
    assert ajena.status_code == 403
    historial = await http_client.get(f"/api/vendedores/{negocio['beto']}/historial", headers=h)
    assert historial.status_code == 403


@pytest.mark.asyncio
async def test_vendedor_mueve_su_lead_y_no_el_ajeno(http_client, negocio):
    h = _h(negocio["vendedor"])
    suyo = await _lead(negocio["tid"], negocio["ana"])
    ajeno = await _lead(negocio["tid"], negocio["beto"])

    r = await http_client.patch(f"/api/pipeline/{suyo}/estado", json={"estado": "contactado"}, headers=h)
    assert r.status_code == 200, r.text

    r = await http_client.patch(f"/api/pipeline/{ajeno}/estado", json={"estado": "contactado"}, headers=h)
    assert r.status_code == 403
    estado = await fetch_value(
        "SELECT estado FROM client_pipeline WHERE user_id = $1", ajeno
    )
    assert estado == "nuevo"

    assert (await http_client.get(f"/api/pipeline/{suyo}/historial", headers=h)).status_code == 200
    assert (await http_client.get(f"/api/pipeline/{ajeno}/historial", headers=h)).status_code == 403


@pytest.mark.asyncio
async def test_vendedor_no_reparte_leads(http_client, negocio):
    lead = await _lead(negocio["tid"], negocio["beto"])
    r = await http_client.post(
        f"/api/pipeline/{lead}/asignar",
        json={"vendedor_id": str(negocio["ana"])},
        headers=_h(negocio["vendedor"]),
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_vendedor_desactivado_pierde_acceso_al_instante(http_client, negocio):
    await execute("UPDATE vendedores SET activo = false WHERE id = $1", negocio["ana"])
    r = await http_client.get("/api/vendedores/yo", headers=_h(negocio["vendedor"]))
    assert r.status_code == 403


# ============================================================
# 'member' mira y opera, no configura
# ============================================================
@pytest.mark.asyncio
async def test_member_ve_el_negocio(http_client, negocio):
    h = _h(negocio["member"])
    for ruta in (
        "/api/agente",
        "/api/conversaciones",
        f"/api/tenants/{negocio['tid']}/pipeline",
        f"/api/tenants/{negocio['tid']}/vendedores",
    ):
        r = await http_client.get(ruta, headers=h)
        # 404 en /agente es que el tenant de prueba no tiene config: la
        # guarda ya lo dejó pasar, que es lo que se prueba.
        assert r.status_code != 403, (ruta, r.text)


@pytest.mark.asyncio
async def test_member_no_configura(http_client, negocio):
    h = _h(negocio["member"])
    assert (await http_client.put("/api/agente", json={"agent_name": "X"}, headers=h)).status_code == 403
    assert (await http_client.delete("/api/canales/whatsapp", headers=h)).status_code == 403
    assert (
        await http_client.post("/api/vendedores", json={"nombre": "Nuevo"}, headers=h)
    ).status_code == 403
    assert (
        await http_client.patch(f"/api/vendedores/{negocio['beto']}", json={"activo": False}, headers=h)
    ).status_code == 403


# ============================================================
# Cupo de vendedores del plan
# ============================================================
@pytest.fixture
async def plan_starter(negocio):
    """Starter con su tope real (3 activos): Ana y Beto ya ocupan dos."""
    await execute(
        "UPDATE tenant_subscriptions SET plan = 'starter' WHERE tenant_id = $1",
        negocio["tid"],
    )
    # `planes` es global: se fija el tope para el test y se deja como estaba.
    previo = await fetch_value("SELECT max_vendedores FROM planes WHERE nombre = 'starter'")
    await execute("UPDATE planes SET max_vendedores = 3 WHERE nombre = 'starter'")
    yield negocio
    await execute("UPDATE planes SET max_vendedores = $1 WHERE nombre = 'starter'", previo)


@pytest.mark.asyncio
async def test_alta_respeta_el_cupo_del_plan(http_client, plan_starter):
    h = _h(plan_starter["owner"])
    r = await http_client.post("/api/vendedores", json={"nombre": "Carla"}, headers=h)
    assert r.status_code == 201, r.text

    r = await http_client.post("/api/vendedores", json={"nombre": "Dani"}, headers=h)
    assert r.status_code == 402
    detalle = r.json()["detail"]
    assert detalle["codigo"] == "cupo_vendedores"
    assert detalle["maximo"] == 3
    assert detalle["activos"] == 3

    cupo = await http_client.get(f"/api/tenants/{plan_starter['tid']}/vendedores/cupo", headers=h)
    assert cupo.json() == {"plan": "starter", "maximo": 3, "activos": 3}


@pytest.mark.asyncio
async def test_desactivar_libera_lugar_y_reactivar_lo_ocupa(http_client, plan_starter):
    h = _h(plan_starter["owner"])
    tid = plan_starter["tid"]
    inactivo = await _vendedor(tid, "Eli", activo=False)
    await _vendedor(tid, "Fer")  # tres activos: Ana, Beto, Fer

    r = await http_client.patch(f"/api/vendedores/{inactivo}", json={"activo": True}, headers=h)
    assert r.status_code == 402

    # Editar a un activo no cuenta como ocupar otro lugar.
    r = await http_client.patch(f"/api/vendedores/{plan_starter['beto']}", json={"nombre": "Alberto"}, headers=h)
    assert r.status_code == 200

    r = await http_client.patch(f"/api/vendedores/{plan_starter['beto']}", json={"activo": False}, headers=h)
    assert r.status_code == 200
    r = await http_client.patch(f"/api/vendedores/{inactivo}", json={"activo": True}, headers=h)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_enterprise_no_tiene_tope(http_client, negocio):
    h = _h(negocio["owner"])
    for i in range(4):
        r = await http_client.post("/api/vendedores", json={"nombre": f"V{i}"}, headers=h)
        assert r.status_code == 201, r.text


# ============================================================
# Cada alta llega a su handler: /api/clientes (campo) y /api/pipeline (chat)
# ============================================================
@pytest.mark.asyncio
async def test_las_dos_altas_de_cliente_no_se_pisan(http_client, negocio):
    h = _h(negocio["owner"])

    # Cliente de campo: lo atiende routers/clientes.py (body ClienteCrearIn).
    r = await http_client.post(
        "/api/clientes",
        json={"nombre_negocio": "Abarrotes La Esquina", "latitud": 19.43, "longitud": -99.13},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["nombre_negocio"] == "Abarrotes La Esquina"

    # Lead del embudo de chat: routers/vendedores.py, bajo /api/pipeline.
    r = await http_client.post(
        "/api/pipeline",
        json={"tenant_id": str(negocio["tid"]), "nombre": "Lead manual"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["cliente_nombre"] == "Lead manual"


@pytest.mark.asyncio
async def test_vendedor_no_crea_leads_del_embudo(http_client, negocio):
    r = await http_client.post(
        "/api/pipeline",
        json={"tenant_id": str(negocio["tid"]), "nombre": "X"},
        headers=_h(negocio["vendedor"]),
    )
    assert r.status_code == 403
