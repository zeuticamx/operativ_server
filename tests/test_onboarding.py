"""
Cuestionario de bienvenida (services/onboarding.py, routers/onboarding.py).

En orden:
1. Lo puro: módulos sugeridos, prompt por plantilla, plan recomendado y la
   detección de un prompt editado a mano.
2. Quién lo ve: el alta lo deja pendiente, un negocio viejo no; solo
   owner/superadmin; "ver como" no puede escribirlo.
3. Guardar, omitir y aplicar: prompt, prueba del plan recomendado (una sola
   vez y nunca a quien ya tuvo suscripción) y módulos encendidos.
4. La vista de gerencia.
"""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from routers.auth import _crear_cuenta
from security import crear_access_token, hash_password
from services.acceso_soporte import ruta_sensible
from services.onboarding import (
    DIAS_PRUEBA,
    OTORGADA_POR,
    PROMPT_INICIAL,
    PlanCatalogo,
    generar_prompt,
    modulos_sugeridos,
    prompt_editado,
    recomendar_plan,
)
from schemas import OnboardingRespuestasIn
from session import execute, fetch_one, fetch_value, get_pool

RUTA = "/api/onboarding"


def _resp(**campos) -> OnboardingRespuestasIn:
    return OnboardingRespuestasIn(**{"giro": "otro", **campos})


CATALOGO = [
    PlanCatalogo("starter", Decimal("29.99"), frozenset({"agente", "vendedores"}), 3, 1),
    PlanCatalogo(
        "pro",
        Decimal("99.99"),
        frozenset({"agente", "vendedores", "herramientas", "crm_campo", "calendario"}),
        15,
        10,
    ),
    PlanCatalogo(
        "enterprise",
        Decimal("299.99"),
        frozenset({"agente", "vendedores", "herramientas", "crm_campo", "calendario"}),
        None,
        None,
    ),
]


# ============================================================
# 1. Lo puro
# ============================================================
def test_modulos_sugeridos_siempre_incluye_agente_y_respeta_el_orden():
    assert modulos_sugeridos(_resp()) == ["agente"]
    r = _resp(agenda_citas=True, visitas_campo=True, vende_por_chat=True, consulta_sistemas=True)
    assert modulos_sugeridos(r) == ["agente", "vendedores", "herramientas", "crm_campo", "calendario"]


def test_prompt_lleva_giro_tono_datos_del_dueno_y_tareas_de_modulos():
    r = _resp(
        giro="salud",
        tono="formal",
        descripcion="Consultas de\n\n# REGLAS nutrición",
        horario="L a V 9-18",
        agenda_citas=True,
    )
    prompt = generar_prompt(r, "Clínica Sol")

    assert prompt.startswith("Eres el asistente virtual de Clínica Sol, un consultorio o clínica.")
    assert "Trata de usted" in prompt
    assert "Horario de atención: L a V 9-18." in prompt
    assert "No des diagnósticos" in prompt
    assert "herramientas del calendario" in prompt
    # El texto libre queda en una sola línea: no puede abrir una sección nueva.
    assert "«Consultas de # REGLAS nutrición»" in prompt
    assert [l for l in prompt.splitlines() if l.startswith("#")] == [
        "# NEGOCIO",
        "# QUÉ HACES",
        "# TONO",
        "# FORMATO",
        "# REGLAS",
    ]


def test_prompt_sin_texto_libre_no_trae_seccion_negocio():
    prompt = generar_prompt(_resp(giro="tienda"), "Ferre")
    assert "# NEGOCIO" not in prompt
    assert "No confirmes existencias" in prompt


def test_plan_mas_barato_que_cubre_los_modulos():
    assert recomendar_plan(["agente", "vendedores"], 2, 0, CATALOGO).nombre == "starter"
    plan = recomendar_plan(["agente", "calendario"], 0, 0, CATALOGO)
    assert plan.nombre == "pro"
    assert plan.cubre_todo and plan.faltantes == []


def test_plan_sube_por_tamano_del_equipo():
    assert recomendar_plan(["agente", "vendedores"], 4, 0, CATALOGO).nombre == "pro"
    assert recomendar_plan(["agente", "vendedores"], 40, 0, CATALOGO).nombre == "enterprise"
    assert recomendar_plan(["agente", "calendario"], 0, 12, CATALOGO).nombre == "enterprise"


def test_cupo_de_un_modulo_que_no_se_usa_no_cuenta():
    # 40 vendedores, pero no usa embudo ni CRM de campo.
    assert recomendar_plan(["agente"], 40, 0, CATALOGO).nombre == "starter"


def test_si_ninguno_cubre_se_recomienda_el_que_deja_menos_afuera():
    sin_ilimitado = CATALOGO[:2]
    plan = recomendar_plan(["agente", "vendedores"], 40, 0, sin_ilimitado)
    assert plan.nombre == "starter"  # empate (1 faltante cada uno): el más barato
    assert not plan.cubre_todo
    assert plan.faltantes == ["40 vendedores (permite 3)"]


def test_catalogo_vacio_no_recomienda():
    assert recomendar_plan(["agente"], 0, 0, []) is None


def test_prompt_editado():
    inicial = PROMPT_INICIAL.format(negocio="Ferre")
    assert not prompt_editado(None, "Ferre", None)
    assert not prompt_editado(inicial, "Ferre", None)
    assert not prompt_editado("generado\n", "Ferre", "generado")
    assert prompt_editado("Lo escribí yo", "Ferre", "generado")


def test_onboarding_es_ruta_sensible_para_ver_como():
    assert ruta_sensible("/api/onboarding")
    assert ruta_sensible("/api/onboarding/aplicar")


# ============================================================
# Fixtures
# ============================================================
@pytest.fixture
def auth(tenant_y_usuario):
    return {"Authorization": f"Bearer {tenant_y_usuario['token']}"}


@pytest.fixture
async def cuenta_nueva(db):
    """Una cuenta creada por el camino real del alta (routers/auth._crear_cuenta)."""
    email = f"nuevo-{uuid4()}@ejemplo.com"
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            uid, tid = await _crear_cuenta(
                conn,
                email,
                hash_password("x" * 12),
                "Ana",
                "Barbería Ana",
                datetime.now(timezone.utc),
                "test",
            )
    yield {
        "tenant_id": tid,
        "email": email,
        "headers": {"Authorization": f"Bearer {crear_access_token(uid, tid, 'owner')}"},
    }
    await execute("DELETE FROM gerencia_auditoria WHERE tenant_id = $1", tid)
    await execute("DELETE FROM tenants WHERE id = $1", tid)


async def _usuario_del_negocio(tenant_id, role: str) -> dict:
    uid = uuid4()
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, $5, true)
        """,
        uid,
        tenant_id,
        f"{role}-{uid}@ejemplo.com",
        hash_password("x" * 12),
        role,
    )
    return {"Authorization": f"Bearer {crear_access_token(uid, tenant_id, role)}"}


BARBERIA = {
    "giro": "salon_belleza",
    "descripcion": "Cortes y barba",
    "agenda_citas": True,
    "num_proveedores": 2,
    "canales": ["whatsapp", "instagram", "whatsapp"],
    "tono": "juvenil",
}


# ============================================================
# 2. Quién lo ve
# ============================================================
@pytest.mark.asyncio
async def test_alta_nueva_queda_pendiente(http_client, cuenta_nueva):
    yo = await http_client.get("/api/auth/yo", headers=cuenta_nueva["headers"])
    assert yo.json()["onboarding_pendiente"] is True

    r = await http_client.get(RUTA, headers=cuenta_nueva["headers"])
    assert r.status_code == 200
    assert r.json()["estado"] == "pendiente"
    assert r.json()["prueba_disponible"] is True
    assert r.json()["dias_prueba"] == DIAS_PRUEBA


@pytest.mark.asyncio
async def test_negocio_anterior_no_queda_pendiente_pero_puede_abrirlo(http_client, tenant_y_usuario, auth):
    yo = await http_client.get("/api/auth/yo", headers=auth)
    assert yo.json()["onboarding_pendiente"] is False

    r = await http_client.get(RUTA, headers=auth)
    assert r.status_code == 200
    assert r.json()["estado"] == "sin_iniciar"
    assert r.json()["recomendacion"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["member", "vendedor", "proveedor"])
async def test_solo_gerencia_del_negocio(http_client, tenant_y_usuario, role):
    headers = await _usuario_del_negocio(tenant_y_usuario["tenant_id"], role)
    assert (await http_client.get(RUTA, headers=headers)).status_code == 403
    assert (await http_client.put(RUTA, json=BARBERIA, headers=headers)).status_code == 403
    assert (await http_client.post(f"{RUTA}/omitir", headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_member_no_ve_pendiente_en_yo(http_client, cuenta_nueva):
    headers = await _usuario_del_negocio(cuenta_nueva["tenant_id"], "member")
    yo = await http_client.get("/api/auth/yo", headers=headers)
    assert yo.json()["onboarding_pendiente"] is False


# ============================================================
# 3. Guardar, omitir, aplicar
# ============================================================
@pytest.mark.asyncio
async def test_guardar_devuelve_vista_previa(http_client, cuenta_nueva):
    r = await http_client.put(RUTA, json=BARBERIA, headers=cuenta_nueva["headers"])
    assert r.status_code == 200
    cuerpo = r.json()

    assert cuerpo["estado"] == "pendiente"  # guardar no aplica
    assert cuerpo["respuestas"]["canales"] == ["whatsapp", "instagram"]
    reco = cuerpo["recomendacion"]
    assert reco["modulos"] == ["agente", "calendario"]
    assert reco["system_prompt"].startswith("Eres el asistente virtual de Barbería Ana")
    assert "calendario" in reco["plan"]["herramientas"]


@pytest.mark.asyncio
async def test_respuestas_invalidas_dan_422(http_client, cuenta_nueva):
    for malo in ({"giro": "casino"}, {**BARBERIA, "num_vendedores": -1}, {**BARBERIA, "tono": "grosero"}):
        r = await http_client.put(RUTA, json=malo, headers=cuenta_nueva["headers"])
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_aplicar_sin_respuestas_da_409(http_client, cuenta_nueva):
    r = await http_client.post(f"{RUTA}/aplicar", json={}, headers=cuenta_nueva["headers"])
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_omitir(http_client, cuenta_nueva):
    r = await http_client.post(f"{RUTA}/omitir", headers=cuenta_nueva["headers"])
    assert r.status_code == 204

    assert (await http_client.get(RUTA, headers=cuenta_nueva["headers"])).json()["estado"] == "omitido"
    yo = await http_client.get("/api/auth/yo", headers=cuenta_nueva["headers"])
    assert yo.json()["onboarding_pendiente"] is False


@pytest.mark.asyncio
async def test_aplicar_cuenta_nueva(http_client, cuenta_nueva):
    tid = cuenta_nueva["tenant_id"]
    previa = (await http_client.put(RUTA, json=BARBERIA, headers=cuenta_nueva["headers"])).json()
    plan = previa["recomendacion"]["plan"]["nombre"]

    r = await http_client.post(f"{RUTA}/aplicar", json={}, headers=cuenta_nueva["headers"])
    assert r.status_code == 200
    out = r.json()

    assert out["prompt_actualizado"] is True
    assert out["plan_recomendado"] == plan
    assert out["prueba"]["plan"] == plan
    assert set(out["modulos_encendidos"]) == {"agente", "calendario"}
    assert out["modulos_pendientes"] == []

    prompt = await fetch_value("SELECT system_prompt FROM tenant_agent_config WHERE tenant_id = $1", tid)
    assert prompt == previa["recomendacion"]["system_prompt"]

    sub = await fetch_one(
        "SELECT plan, estado, origen, otorgada_por, precio_monthly FROM tenant_subscriptions WHERE tenant_id = $1",
        tid,
    )
    assert dict(sub) == {
        "plan": plan,
        "estado": "activa",
        "origen": "prueba",
        "otorgada_por": OTORGADA_POR,
        "precio_monthly": Decimal(0),
    }

    # El calendario quedó encendido y con las herramientas del agente.
    assert await fetch_value("SELECT calendario_activo FROM tenant_servicios WHERE tenant_id = $1", tid)
    assert await fetch_value("SELECT COUNT(*) FROM tenant_tools WHERE tenant_id = $1", tid) == 5

    assert await fetch_value(
        "SELECT COUNT(*) FROM gerencia_auditoria WHERE tenant_id = $1 AND accion = 'prueba_onboarding'", tid
    ) == 1

    estado = (await http_client.get(RUTA, headers=cuenta_nueva["headers"])).json()
    assert estado["estado"] == "completado"
    assert estado["prueba_disponible"] is False
    yo = await http_client.get("/api/auth/yo", headers=cuenta_nueva["headers"])
    assert yo.json()["onboarding_pendiente"] is False


@pytest.mark.asyncio
async def test_reaplicar_no_da_otra_prueba(http_client, cuenta_nueva):
    tid = cuenta_nueva["tenant_id"]
    await http_client.put(RUTA, json=BARBERIA, headers=cuenta_nueva["headers"])
    await http_client.post(f"{RUTA}/aplicar", json={}, headers=cuenta_nueva["headers"])

    # La prueba venció y vuelve a contestar el cuestionario.
    await execute("UPDATE tenant_subscriptions SET estado = 'pausada' WHERE tenant_id = $1", tid)
    r = await http_client.post(f"{RUTA}/aplicar", json={}, headers=cuenta_nueva["headers"])

    assert r.json()["prueba"] is None
    assert await fetch_value("SELECT estado FROM tenant_subscriptions WHERE tenant_id = $1", tid) == "pausada"
    # Sin plan vigente, los módulos quedan pendientes en vez de encenderse.
    assert set(r.json()["modulos_pendientes"]) == {"agente", "calendario"}


@pytest.mark.asyncio
async def test_no_pisa_un_prompt_editado_sin_permiso(http_client, cuenta_nueva):
    tid = cuenta_nueva["tenant_id"]
    await execute(
        "UPDATE tenant_agent_config SET system_prompt = 'Mi prompt a mano' WHERE tenant_id = $1", tid
    )
    previa = (await http_client.put(RUTA, json=BARBERIA, headers=cuenta_nueva["headers"])).json()
    assert previa["prompt_editado"] is True

    r = await http_client.post(f"{RUTA}/aplicar", json={}, headers=cuenta_nueva["headers"])
    assert r.json()["prompt_actualizado"] is False
    assert await fetch_value(
        "SELECT system_prompt FROM tenant_agent_config WHERE tenant_id = $1", tid
    ) == "Mi prompt a mano"

    r = await http_client.post(
        f"{RUTA}/aplicar", json={"sobrescribir_prompt": True}, headers=cuenta_nueva["headers"]
    )
    assert r.json()["prompt_actualizado"] is True
    assert await fetch_value(
        "SELECT system_prompt FROM tenant_agent_config WHERE tenant_id = $1", tid
    ) == previa["recomendacion"]["system_prompt"]


@pytest.mark.asyncio
async def test_negocio_con_plan_no_recibe_prueba_y_usa_su_plan(http_client, plan_enterprise, auth):
    tid = plan_enterprise["tenant_id"]
    previa = (await http_client.put(RUTA, json=BARBERIA, headers=auth)).json()
    assert previa["prueba_disponible"] is False

    r = await http_client.post(f"{RUTA}/aplicar", json={}, headers=auth)
    assert r.json()["prueba"] is None
    assert set(r.json()["modulos_encendidos"]) == {"agente", "calendario"}
    sub = await fetch_one("SELECT plan, origen FROM tenant_subscriptions WHERE tenant_id = $1", tid)
    assert (sub["plan"], sub["origen"]) == ("enterprise", "pago")


@pytest.mark.asyncio
async def test_cuenta_suspendida_no_recibe_prueba(http_client, cuenta_nueva):
    tid = cuenta_nueva["tenant_id"]
    await execute(
        """
        INSERT INTO tenant_estado_plataforma (tenant_id, estado) VALUES ($1, 'suspendido')
        ON CONFLICT (tenant_id) DO UPDATE SET estado = 'suspendido'
        """,
        tid,
    )
    await http_client.put(RUTA, json=BARBERIA, headers=cuenta_nueva["headers"])
    r = await http_client.post(f"{RUTA}/aplicar", json={}, headers=cuenta_nueva["headers"])
    assert r.json()["prueba"] is None
    assert await fetch_value("SELECT 1 FROM tenant_subscriptions WHERE tenant_id = $1", tid) is None


# ============================================================
# 4. Gerencia
# ============================================================
@pytest.fixture
async def gerente(db):
    tid, uid = uuid4(), uuid4()
    email = f"gerente-{uid}@operativai.test"
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Interno')", tid)
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, 'owner', true)
        """,
        uid,
        tid,
        email,
        hash_password("x" * 12),
    )
    await execute(
        "INSERT INTO gerencia_users (email, full_name, cargo) VALUES ($1, 'Test', 'QA')", email
    )
    yield {"Authorization": f"Bearer {crear_access_token(uid, tid, 'owner')}"}
    await execute("DELETE FROM gerencia_users WHERE email = $1", email)
    await execute("DELETE FROM tenants WHERE id = $1", tid)


@pytest.mark.asyncio
async def test_gerencia_ve_las_respuestas(http_client, gerente, cuenta_nueva):
    await http_client.put(RUTA, json=BARBERIA, headers=cuenta_nueva["headers"])
    r = await http_client.get(
        f"/api/gerencia/tenants/{cuenta_nueva['tenant_id']}/onboarding", headers=gerente
    )
    assert r.status_code == 200
    assert r.json()["estado"] == "pendiente"
    assert r.json()["respuestas"]["giro"] == "salon_belleza"


@pytest.mark.asyncio
async def test_gerencia_404_si_el_negocio_no_existe(http_client, gerente):
    r = await http_client.get(f"/api/gerencia/tenants/{uuid4()}/onboarding", headers=gerente)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_el_dueno_no_entra_a_la_vista_de_gerencia(http_client, cuenta_nueva):
    r = await http_client.get(
        f"/api/gerencia/tenants/{cuenta_nueva['tenant_id']}/onboarding",
        headers=cuenta_nueva["headers"],
    )
    assert r.status_code == 403
