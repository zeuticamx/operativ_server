"""
Tests HTTP del control de acceso por plan (services/acceso_plan.py +
deps.requiere_herramienta), contra la base real.

Cubren las dos puertas por las que entra el gate:
  - dependency de router (agente, conversaciones, canales, herramientas,
    CRM de campo)
  - choke points de módulo que ya existían (_exigir_modulo de vendedores,
    verificar_calendario_activo), que conservan su 409 antes del 402

y lo que NO debe bloquearse nunca (pagos, /auth/yo), para que un negocio
bloqueado siempre pueda ver por qué y pagar.
"""

from uuid import uuid4

import asyncpg
import pytest

from config import settings
from services.pipeline import set_tenant_servicios
from session import execute


async def _suscripcion(tenant_id, plan: str, estado: str = "activa") -> None:
    await execute(
        """
        INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly)
        VALUES ($1, $2, $3, 100)
        """,
        tenant_id,
        plan,
        estado,
    )


async def _estado_plataforma(tenant_id, estado: str) -> None:
    await execute(
        """
        INSERT INTO tenant_estado_plataforma (tenant_id, estado, motivo)
        VALUES ($1, $2, 'test')
        ON CONFLICT (tenant_id) DO UPDATE SET estado = EXCLUDED.estado
        """,
        tenant_id,
        estado,
    )


async def _encender_todo(tenant_id) -> None:
    await set_tenant_servicios(
        tenant_id, agente_ia_activo=True, gestion_vendedores_activo=True, calendario_activo=True
    )


@pytest.fixture
def auth(tenant_y_usuario):
    return {"Authorization": f"Bearer {tenant_y_usuario['token']}"}


@pytest.fixture
async def plan_temporal(db):
    """Un plan dado de alta 'desde gerencia' con una matriz a medida."""
    nombre = f"basico-{uuid4().hex[:10]}"
    await execute(
        """
        INSERT INTO planes
            (nombre, precio_monthly, agente_ia_activo, gestion_vendedores_activo,
             herramientas_activo, crm_campo_activo, calendario_activo, orden)
        VALUES ($1, 49, true, false, false, false, true, 99)
        """,
        nombre,
    )
    yield nombre
    # Este teardown corre antes que el de tenant_y_usuario, y la FK no deja
    # borrar un plan que alguien tiene contratado: primero la suscripción.
    await execute("DELETE FROM tenant_subscriptions WHERE plan = $1", nombre)
    await execute("DELETE FROM planes WHERE nombre = $1", nombre)


# ============================================================
# Plan vigente: solo las herramientas de su nivel
# ============================================================
@pytest.mark.asyncio
async def test_starter_no_entra_al_calendario_y_se_le_dice_que_plan_lo_incluye(
    http_client, tenant_y_usuario, auth
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_todo(tenant_id)
    await _suscripcion(tenant_id, "starter")

    r = await http_client.get(f"/api/tenants/{tenant_id}/calendario/proveedores", headers=auth)

    assert r.status_code == 402
    detalle = r.json()["detail"]
    assert detalle["codigo"] == "plan_insuficiente"
    assert detalle["herramienta"] == "calendario"
    assert detalle["plan_actual"] == "starter"
    assert "pro" in detalle["planes_que_la_incluyen"]
    assert "starter" not in detalle["planes_que_la_incluyen"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ruta, herramienta",
    [
        ("/api/clientes", "crm_campo"),
        ("/api/tareas", "crm_campo"),
        ("/api/visitas", "crm_campo"),
        ("/api/reportes/actividad", "crm_campo"),
        ("/api/herramientas", "herramientas"),
    ],
)
async def test_starter_no_entra_a_lo_que_su_plan_no_incluye(
    http_client, tenant_y_usuario, auth, ruta, herramienta
):
    await _suscripcion(tenant_y_usuario["tenant_id"], "starter")

    r = await http_client.get(ruta, headers=auth)

    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "plan_insuficiente"
    assert r.json()["detail"]["herramienta"] == herramienta


@pytest.mark.asyncio
async def test_starter_si_usa_lo_que_su_plan_incluye(http_client, tenant_y_usuario, auth):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_todo(tenant_id)
    await _suscripcion(tenant_id, "starter")

    assert (await http_client.get("/api/conversaciones", headers=auth)).status_code == 200
    assert (await http_client.get("/api/canales", headers=auth)).status_code == 200
    assert (
        await http_client.get(f"/api/tenants/{tenant_id}/pipeline", headers=auth)
    ).status_code == 200


@pytest.mark.asyncio
async def test_pro_entra_a_calendario_crm_y_herramientas(http_client, tenant_y_usuario, auth):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_todo(tenant_id)
    await _suscripcion(tenant_id, "pro")

    for ruta in (
        f"/api/tenants/{tenant_id}/calendario/proveedores",
        "/api/clientes",
        "/api/tareas",
        "/api/herramientas",
    ):
        r = await http_client.get(ruta, headers=auth)
        assert r.status_code == 200, (ruta, r.text)


@pytest.mark.asyncio
async def test_el_409_de_modulo_apagado_va_antes_que_el_402_del_plan(
    http_client, tenant_y_usuario, auth
):
    """Apagado a propósito por el dueño no es un problema de plan."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _suscripcion(tenant_id, "starter")  # calendario apagado (default) y fuera del plan

    r = await http_client.get(f"/api/tenants/{tenant_id}/calendario/proveedores", headers=auth)

    assert r.status_code == 409


# ============================================================
# Cuenta inactiva: vencida, cancelada, sin plan, suspendida
# ============================================================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "estado_suscripcion, estado_cuenta",
    [("pausada", "vencido"), ("cancelada", "cancelado")],
)
async def test_plan_vencido_o_cancelado_bloquea_con_plan_requerido(
    http_client, tenant_y_usuario, auth, estado_suscripcion, estado_cuenta
):
    await _suscripcion(tenant_y_usuario["tenant_id"], "enterprise", estado_suscripcion)

    r = await http_client.get("/api/agente", headers=auth)

    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "plan_requerido"
    assert r.json()["detail"]["estado"] == estado_cuenta


@pytest.mark.asyncio
async def test_sin_suscripcion_bloquea_con_plan_requerido(http_client, tenant_y_usuario, auth):
    r = await http_client.get("/api/canales", headers=auth)

    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "plan_requerido"
    assert r.json()["detail"]["estado"] == "sin_plan"


@pytest.mark.asyncio
async def test_plan_vencido_sigue_viendo_su_historial_pero_no_opera(
    http_client, tenant_y_usuario, auth
):
    await _suscripcion(tenant_y_usuario["tenant_id"], "pro", "pausada")

    lectura = await http_client.get("/api/conversaciones", headers=auth)
    assert lectura.status_code == 200

    escritura = await http_client.post(f"/api/conversaciones/{uuid4()}/volver-a-ia", headers=auth)
    assert escritura.status_code == 402
    assert escritura.json()["detail"]["codigo"] == "plan_requerido"


@pytest.mark.asyncio
async def test_cuenta_suspendida_bloquea_hasta_la_lectura(http_client, tenant_y_usuario, auth):
    """La suspensión de gerencia gana sobre un plan al día, y sin excepción de lectura."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _suscripcion(tenant_id, "enterprise")
    await _estado_plataforma(tenant_id, "suspendido")

    r = await http_client.get("/api/conversaciones", headers=auth)

    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "cuenta_suspendida"


@pytest.mark.asyncio
async def test_negocio_piloto_usa_todo_sin_plan(http_client, tenant_y_usuario, auth):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _estado_plataforma(tenant_id, "prueba")

    assert (await http_client.get("/api/clientes", headers=auth)).status_code == 200
    assert (await http_client.get("/api/herramientas", headers=auth)).status_code == 200


# ============================================================
# Lo que nunca se bloquea: por dónde se sale del bloqueo
# ============================================================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ruta", ["/api/auth/yo", "/api/pagos/suscripcion", "/api/pagos/catalogo", "/api/pagos/acceso"]
)
async def test_pagos_y_sesion_responden_aunque_la_cuenta_este_bloqueada(
    http_client, tenant_y_usuario, auth, ruta
):
    await _estado_plataforma(tenant_y_usuario["tenant_id"], "suspendido")

    r = await http_client.get(ruta, headers=auth)

    assert r.status_code == 200


@pytest.mark.asyncio
async def test_acceso_describe_el_estado_y_la_matriz(http_client, tenant_y_usuario, auth):
    await _suscripcion(tenant_y_usuario["tenant_id"], "starter")

    r = await http_client.get("/api/pagos/acceso", headers=auth)

    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["estado"] == "vigente"
    assert cuerpo["plan"] == "starter"
    assert cuerpo["herramientas"] == ["agente", "vendedores"]
    planes = {p["nombre"]: p["herramientas"] for p in cuerpo["planes"]}
    assert "calendario" in planes["pro"]
    assert "calendario" not in planes["starter"]


@pytest.mark.asyncio
async def test_acceso_sin_plan_no_permite_nada(http_client, auth):
    r = await http_client.get("/api/pagos/acceso", headers=auth)

    assert r.status_code == 200
    assert r.json()["estado"] == "sin_plan"
    assert r.json()["herramientas"] == []


# ============================================================
# Encender un módulo ya no sirve para saltarse el plan
# ============================================================
@pytest.mark.asyncio
async def test_el_dueno_no_puede_encender_un_modulo_fuera_de_su_plan(
    http_client, tenant_y_usuario, auth
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _suscripcion(tenant_id, "starter")

    r = await http_client.patch(
        f"/api/tenants/{tenant_id}/servicios", json={"calendario_activo": True}, headers=auth
    )

    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "plan_insuficiente"
    assert r.json()["detail"]["herramienta"] == "calendario"


@pytest.mark.asyncio
async def test_apagar_un_modulo_siempre_se_puede(http_client, tenant_y_usuario, auth):
    tenant_id = tenant_y_usuario["tenant_id"]
    # Sin plan: aun así puede apagar lo que tenga encendido.
    r = await http_client.patch(
        f"/api/tenants/{tenant_id}/servicios", json={"calendario_activo": False}, headers=auth
    )

    assert r.status_code == 200


@pytest.mark.asyncio
async def test_el_dueno_si_enciende_lo_que_su_plan_incluye(http_client, tenant_y_usuario, auth):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _suscripcion(tenant_id, "pro")

    r = await http_client.patch(
        f"/api/tenants/{tenant_id}/servicios", json={"calendario_activo": True}, headers=auth
    )

    assert r.status_code == 200
    assert r.json()["calendario_activo"] is True


# ============================================================
# Planes nuevos: se pueden contratar y su matriz se respeta
# ============================================================
@pytest.mark.asyncio
async def test_un_plan_nuevo_se_puede_contratar_y_manda_su_propia_matriz(
    http_client, tenant_y_usuario, auth, plan_temporal
):
    """Antes el CHECK de tenant_subscriptions.plan solo admitía los tres de fábrica."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_todo(tenant_id)
    await _suscripcion(tenant_id, plan_temporal)  # antes: CheckViolationError

    calendario = await http_client.get(
        f"/api/tenants/{tenant_id}/calendario/proveedores", headers=auth
    )
    assert calendario.status_code == 200

    pipeline = await http_client.get(f"/api/tenants/{tenant_id}/pipeline", headers=auth)
    assert pipeline.status_code == 402
    assert pipeline.json()["detail"]["codigo"] == "plan_insuficiente"


@pytest.mark.asyncio
async def test_no_se_puede_contratar_un_plan_inexistente(tenant_y_usuario):
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _suscripcion(tenant_y_usuario["tenant_id"], f"inventado-{uuid4().hex[:8]}")


# ============================================================
# El lado de n8n no cambia
# ============================================================
@pytest.mark.asyncio
async def test_n8n_sigue_agendando_con_el_gate_de_pagos_de_siempre(
    http_client, tenant_y_usuario, monkeypatch
):
    """
    Un negocio sin plan que nunca pagó: el portal le pide plan, pero el
    agente de WhatsApp sigue pudiendo consultar el calendario (acceso_pagos
    no lo bloquea). Lo que ve el cliente final no depende del control por plan.
    """
    monkeypatch.setattr(settings, "N8N_INTERNAL_TOKEN", "secreto-plan")
    tenant_id = tenant_y_usuario["tenant_id"]
    await set_tenant_servicios(tenant_id, None, None, calendario_activo=True)

    r = await http_client.get(
        "/api/eventos/calendario/servicios",
        params={"tenant_id": str(tenant_id)},
        headers={"X-Internal-Token": "secreto-plan"},
    )

    assert r.status_code == 200
