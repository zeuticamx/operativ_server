"""
Límites del agente por plan (services/limites_agente.py, 45_limites_agente_plan.sql).

Lo que se cubre:
1. La regla pura (sin BD): piloto y sin plan no tienen topes.
2. PUT /api/agente: 402 si el modelo o el historial no caben en el plan, y el
   modelo por defecto del plan cuando no se elige ninguno.
3. Que la fila de tenant_agent_config quede SIEMPRE dentro del plan (n8n la lee
   directo): cambio de plan del negocio, baja de topes desde gerencia, y que
   recortar nunca suba un valor ni toque al piloto.
4. El panel de planes: alta y edición de los topes (null = no tocar,
   []/0 = quitar) y su bitácora.
"""

from uuid import uuid4

import pytest

from security import crear_access_token, hash_password
from services.limites_agente import (
    MODELOS_AGENTE,
    SIN_LIMITES,
    evaluar,
    ordenar_modelos,
)
from session import execute, fetch_one

HAIKU, SONNET, OPUS = MODELOS_AGENTE
PROMPT = "Eres un asistente de pruebas del negocio."


# ------------------------------------------------------------
# 1. Regla pura
# ------------------------------------------------------------
def test_sin_plan_o_en_piloto_no_hay_topes():
    assert evaluar(None, [HAIKU], 50, "activo") == SIN_LIMITES
    assert evaluar("starter", [HAIKU], 50, "prueba") == SIN_LIMITES


def test_con_plan_rigen_sus_topes():
    lim = evaluar("starter", [HAIKU], 50, "activo")
    assert lim.modelo_cabe(HAIKU) and not lim.modelo_cabe(OPUS)
    assert lim.modelo_por_defecto == HAIKU
    assert lim.historial_cabe(50) and not lim.historial_cabe(51)


def test_modelo_null_solo_cabe_si_el_plan_no_limita_modelos():
    assert not evaluar("starter", [HAIKU], None, "activo").modelo_cabe(None)
    assert evaluar("enterprise", None, None, "activo").modelo_cabe(None)


def test_plan_sin_topes_deja_todo_el_catalogo():
    lim = evaluar("enterprise", None, None, "activo")
    assert lim.modelos_disponibles() == MODELOS_AGENTE
    assert lim.tope_historial() == 200
    assert lim.modelo_por_defecto is None


def test_los_modelos_se_ordenan_barato_a_caro_y_sin_repetidos():
    assert ordenar_modelos([OPUS, HAIKU, HAIKU]) == [HAIKU, OPUS]


# ------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------
@pytest.fixture
async def plan_limitado(db):
    """Plan propio con topes (haiku+sonnet, 60 mensajes). El teardown lo borra."""
    nombre = f"plan-lim-{uuid4().hex[:12]}"
    await execute(
        """
        INSERT INTO planes (nombre, precio_monthly, modelos_permitidos, historial_maximo)
        VALUES ($1, 10, ARRAY[$2, $3], 60)
        """,
        nombre,
        HAIKU,
        SONNET,
    )
    yield nombre
    await execute("DELETE FROM tenant_subscriptions WHERE plan = $1", nombre)
    await execute("DELETE FROM planes WHERE nombre = $1", nombre)


async def _suscribir(tenant_id, plan):
    await execute(
        """
        INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly)
        VALUES ($1, $2, 'activa', 10)
        ON CONFLICT (tenant_id) DO UPDATE SET plan = EXCLUDED.plan
        """,
        tenant_id,
        plan,
    )


async def _config(tenant_id, model=None, history_window=30):
    await execute(
        """
        INSERT INTO tenant_agent_config (tenant_id, system_prompt, model, history_window)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (tenant_id) DO UPDATE SET model = EXCLUDED.model,
                                              history_window = EXCLUDED.history_window
        """,
        tenant_id,
        PROMPT,
        model,
        history_window,
    )


async def _leer(tenant_id):
    return await fetch_one(
        "SELECT model, history_window FROM tenant_agent_config WHERE tenant_id = $1", tenant_id
    )


def _cuerpo(**cambios):
    return {"agent_name": "Asistente", "system_prompt": PROMPT, **cambios}


# ------------------------------------------------------------
# 2. PUT /api/agente
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_put_rechaza_un_modelo_fuera_del_plan(
    http_client, tenant_y_usuario, headers_autenticado, plan_limitado
):
    await _suscribir(tenant_y_usuario["tenant_id"], plan_limitado)

    r = await http_client.put("/api/agente", json=_cuerpo(model=OPUS), headers=headers_autenticado)

    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "modelo_no_incluido_en_plan"
    assert r.json()["detail"]["modelos_permitidos"] == [HAIKU, SONNET]


@pytest.mark.asyncio
async def test_put_rechaza_un_historial_que_excede_el_plan(
    http_client, tenant_y_usuario, headers_autenticado, plan_limitado
):
    await _suscribir(tenant_y_usuario["tenant_id"], plan_limitado)

    r = await http_client.put(
        "/api/agente", json=_cuerpo(model=HAIKU, history_window=61), headers=headers_autenticado
    )

    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "historial_excede_plan"
    assert r.json()["detail"]["historial_maximo"] == 60


@pytest.mark.asyncio
async def test_put_dentro_del_plan_guarda(
    http_client, tenant_y_usuario, headers_autenticado, plan_limitado
):
    await _suscribir(tenant_y_usuario["tenant_id"], plan_limitado)

    r = await http_client.put(
        "/api/agente", json=_cuerpo(model=SONNET, history_window=60), headers=headers_autenticado
    )

    assert r.status_code == 200
    assert r.json()["model"] == SONNET
    assert (await _leer(tenant_y_usuario["tenant_id"]))["history_window"] == 60


@pytest.mark.asyncio
async def test_put_sin_modelo_guarda_el_por_defecto_del_plan(
    http_client, tenant_y_usuario, headers_autenticado, plan_limitado
):
    await _suscribir(tenant_y_usuario["tenant_id"], plan_limitado)

    r = await http_client.put("/api/agente", json=_cuerpo(), headers=headers_autenticado)

    assert r.status_code == 200
    assert r.json()["model"] == HAIKU


@pytest.mark.asyncio
async def test_get_devuelve_los_limites_del_plan(
    http_client, tenant_y_usuario, headers_autenticado, plan_limitado
):
    await _suscribir(tenant_y_usuario["tenant_id"], plan_limitado)
    await _config(tenant_y_usuario["tenant_id"], model=HAIKU, history_window=30)

    r = await http_client.get("/api/agente", headers=headers_autenticado)

    assert r.status_code == 200
    assert r.json()["limites_plan"] == {
        "plan": plan_limitado,
        "modelos_permitidos": [HAIKU, SONNET],
        "modelo_por_defecto": HAIKU,
        "historial_maximo": 60,
    }


@pytest.mark.asyncio
async def test_plan_sin_topes_deja_elegir_cualquier_modelo(
    http_client, tenant_y_usuario, headers_autenticado, plan_enterprise
):
    r = await http_client.put(
        "/api/agente", json=_cuerpo(model=OPUS, history_window=200), headers=headers_autenticado
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_el_piloto_con_plan_usa_los_topes_del_plan(
    http_client, tenant_y_usuario, headers_autenticado, plan_limitado
):
    """Spec 005a D14: la prueba vigente con plan asociado usa sus límites."""
    tid = tenant_y_usuario["tenant_id"]
    await _suscribir(tid, plan_limitado)
    await execute(
        """
        INSERT INTO tenant_estado_plataforma (tenant_id, estado) VALUES ($1, 'prueba')
        ON CONFLICT (tenant_id) DO UPDATE SET estado = 'prueba'
        """,
        tid,
    )

    r = await http_client.put(
        "/api/agente", json=_cuerpo(model=OPUS, history_window=150), headers=headers_autenticado
    )

    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "modelo_no_incluido_en_plan"


@pytest.mark.asyncio
async def test_el_piloto_sin_plan_no_tiene_topes(
    http_client, tenant_y_usuario, headers_autenticado
):
    """Sin plan asociado, la prueba vigente sigue sin límites (D14)."""
    tid = tenant_y_usuario["tenant_id"]
    await execute(
        """
        INSERT INTO tenant_estado_plataforma (tenant_id, estado) VALUES ($1, 'prueba')
        ON CONFLICT (tenant_id) DO UPDATE SET estado = 'prueba'
        """,
        tid,
    )

    r = await http_client.put(
        "/api/agente", json=_cuerpo(model=OPUS, history_window=150), headers=headers_autenticado
    )

    assert r.status_code == 200


# ------------------------------------------------------------
# 3. La fila queda siempre dentro del plan (la lee n8n)
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_cambiar_de_plan_recorta_la_config(tenant_y_usuario, plan_limitado):
    tid = tenant_y_usuario["tenant_id"]
    await _config(tid, model=OPUS, history_window=150)

    await _suscribir(tid, plan_limitado)

    fila = await _leer(tid)
    assert fila["model"] == HAIKU  # el por defecto del plan
    assert fila["history_window"] == 60


@pytest.mark.asyncio
async def test_modelo_null_pasa_al_por_defecto_del_plan(tenant_y_usuario, plan_limitado):
    tid = tenant_y_usuario["tenant_id"]
    await _config(tid, model=None, history_window=30)

    await _suscribir(tid, plan_limitado)

    assert (await _leer(tid))["model"] == HAIKU


@pytest.mark.asyncio
async def test_recortar_nunca_sube_un_valor(tenant_y_usuario, plan_limitado):
    tid = tenant_y_usuario["tenant_id"]
    await _config(tid, model=SONNET, history_window=10)

    await _suscribir(tid, plan_limitado)

    fila = await _leer(tid)
    assert fila["model"] == SONNET  # ya cabía
    assert fila["history_window"] == 10  # no se sube a 60


@pytest.mark.asyncio
async def test_el_piloto_con_plan_se_recorta(tenant_y_usuario, plan_limitado):
    """Spec 005a D14: el recorte aplica a la prueba con plan asociado."""
    tid = tenant_y_usuario["tenant_id"]
    await execute(
        """
        INSERT INTO tenant_estado_plataforma (tenant_id, estado) VALUES ($1, 'prueba')
        ON CONFLICT (tenant_id) DO UPDATE SET estado = 'prueba'
        """,
        tid,
    )
    await _config(tid, model=OPUS, history_window=150)

    await _suscribir(tid, plan_limitado)

    fila = await _leer(tid)
    assert (fila["model"], fila["history_window"]) == (HAIKU, 60)


@pytest.mark.asyncio
async def test_el_piloto_sin_plan_no_se_recorta(tenant_y_usuario):
    tid = tenant_y_usuario["tenant_id"]
    await execute(
        """
        INSERT INTO tenant_estado_plataforma (tenant_id, estado) VALUES ($1, 'prueba')
        ON CONFLICT (tenant_id) DO UPDATE SET estado = 'prueba'
        """,
        tid,
    )
    await _config(tid, model=OPUS, history_window=150)

    fila = await _leer(tid)
    assert (fila["model"], fila["history_window"]) == (OPUS, 150)


@pytest.mark.asyncio
async def test_salir_del_piloto_recorta(tenant_y_usuario, plan_limitado):
    tid = tenant_y_usuario["tenant_id"]
    await execute(
        """
        INSERT INTO tenant_estado_plataforma (tenant_id, estado) VALUES ($1, 'prueba')
        ON CONFLICT (tenant_id) DO UPDATE SET estado = 'prueba'
        """,
        tid,
    )
    await _config(tid, model=OPUS, history_window=150)
    await _suscribir(tid, plan_limitado)

    await execute("UPDATE tenant_estado_plataforma SET estado = 'activo' WHERE tenant_id = $1", tid)

    fila = await _leer(tid)
    assert (fila["model"], fila["history_window"]) == (HAIKU, 60)


# ------------------------------------------------------------
# 4. Panel de planes
# ------------------------------------------------------------
@pytest.fixture
async def gerente(db):
    tid = uuid4()
    uid = uuid4()
    email = f"gerente-{uid}@ejemplo.com"

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

    yield {
        "email": email,
        "headers": {"Authorization": f"Bearer {crear_access_token(uid, tid, 'owner')}"},
    }

    await execute("DELETE FROM gerencia_users WHERE LOWER(email) = LOWER($1)", email)
    await execute("DELETE FROM gerencia_auditoria WHERE actor_email = $1", email)
    await execute("DELETE FROM tenants WHERE id = $1", tid)


@pytest.fixture
async def nombre_plan(db):
    nombre = f"plan-prueba-{uuid4().hex[:16]}"
    yield nombre
    await execute("DELETE FROM tenant_subscriptions WHERE plan = $1", nombre)
    await execute("DELETE FROM planes WHERE nombre = $1", nombre)


@pytest.mark.asyncio
async def test_el_panel_lee_el_catalogo_de_modelos(http_client, gerente, headers_autenticado):
    r = await http_client.get("/api/gerencia/planes/modelos-agente", headers=gerente["headers"])
    assert r.status_code == 200
    assert r.json() == {"modelos": list(MODELOS_AGENTE), "historial_min": 5, "historial_max": 200}

    # Un owner de negocio no entra (mismo candado que el resto de /gerencia/planes).
    r = await http_client.get("/api/gerencia/planes/modelos-agente", headers=headers_autenticado)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_crear_plan_con_topes_ordena_los_modelos(http_client, gerente, nombre_plan):
    r = await http_client.post(
        "/api/gerencia/planes",
        json={
            "nombre": nombre_plan,
            "precio_monthly": "10",
            "modelos_permitidos": [SONNET, HAIKU, HAIKU],
            "historial_maximo": 80,
        },
        headers=gerente["headers"],
    )

    assert r.status_code == 201
    assert r.json()["modelos_permitidos"] == [HAIKU, SONNET]  # barato → caro, sin repetidos
    assert r.json()["historial_maximo"] == 80


@pytest.mark.asyncio
async def test_un_plan_nuevo_nace_sin_topes(http_client, gerente, nombre_plan):
    r = await http_client.post(
        "/api/gerencia/planes",
        json={"nombre": nombre_plan, "precio_monthly": "10"},
        headers=gerente["headers"],
    )

    assert r.json()["modelos_permitidos"] is None
    assert r.json()["historial_maximo"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalido",
    [
        {"modelos_permitidos": ["gpt-9"]},
        {"modelos_permitidos": []},
        {"historial_maximo": 4},
        {"historial_maximo": 201},
    ],
)
async def test_topes_invalidos_se_rechazan_al_crear(http_client, gerente, nombre_plan, invalido):
    r = await http_client.post(
        "/api/gerencia/planes",
        json={"nombre": nombre_plan, "precio_monthly": "10", **invalido},
        headers=gerente["headers"],
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_null_no_toca_y_vacio_o_cero_quita_el_tope(http_client, gerente, nombre_plan):
    await http_client.post(
        "/api/gerencia/planes",
        json={
            "nombre": nombre_plan,
            "precio_monthly": "10",
            "modelos_permitidos": [HAIKU],
            "historial_maximo": 50,
        },
        headers=gerente["headers"],
    )
    url = f"/api/gerencia/planes/{nombre_plan}"

    r = await http_client.patch(url, json={"orden": 2}, headers=gerente["headers"])
    assert r.json()["modelos_permitidos"] == [HAIKU]
    assert r.json()["historial_maximo"] == 50

    r = await http_client.patch(
        url, json={"modelos_permitidos": [], "historial_maximo": 0}, headers=gerente["headers"]
    )
    assert r.status_code == 200
    assert r.json()["modelos_permitidos"] is None
    assert r.json()["historial_maximo"] is None


@pytest.mark.asyncio
async def test_patch_con_historial_entre_1_y_4_se_rechaza(http_client, gerente, nombre_plan):
    await http_client.post(
        "/api/gerencia/planes",
        json={"nombre": nombre_plan, "precio_monthly": "10"},
        headers=gerente["headers"],
    )

    r = await http_client.patch(
        f"/api/gerencia/planes/{nombre_plan}", json={"historial_maximo": 3}, headers=gerente["headers"]
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_bajar_los_topes_recorta_a_los_negocios_y_lo_audita(
    http_client, gerente, nombre_plan, tenant_y_usuario
):
    tid = tenant_y_usuario["tenant_id"]
    await http_client.post(
        "/api/gerencia/planes",
        json={"nombre": nombre_plan, "precio_monthly": "10"},
        headers=gerente["headers"],
    )
    await _suscribir(tid, nombre_plan)
    await _config(tid, model=OPUS, history_window=120)  # el plan aún no limita

    r = await http_client.patch(
        f"/api/gerencia/planes/{nombre_plan}",
        json={"modelos_permitidos": [HAIKU, SONNET], "historial_maximo": 40},
        headers=gerente["headers"],
    )
    assert r.status_code == 200

    fila = await _leer(tid)
    assert (fila["model"], fila["history_window"]) == (HAIKU, 40)

    asiento = await fetch_one(
        "SELECT detalle FROM gerencia_auditoria "
        "WHERE accion = 'plan_actualizado' AND actor_email = $1 ORDER BY created_at DESC LIMIT 1",
        gerente["email"],
    )
    assert asiento["detalle"]["negocios_recortados"] == 1
