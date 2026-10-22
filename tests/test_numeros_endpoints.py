"""
Endpoints de varios números (spec 005a T5, RF-2, RF-4, RF-5, RF-6, RF-12,
RF-14, RF-15, RF-17, RF-18, RF-19).

Cada ruta con sus códigos: 201/200/204/400/402/403/404/409/429. El 404 es
idéntico para número ajeno (activo o pausado) e inexistente, y el 403 de rol
o de "ver como" va antes que el 404 de pertenencia.
"""

from uuid import uuid4

import pytest

from security import crear_access_token, hash_password
from session import execute, fetch_one

pytestmark = pytest.mark.usefixtures("plan_enterprise")


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _pn() -> str:
    return f"pn-{uuid4().hex[:10]}"


async def _cuenta(tid, role: str):
    uid = uuid4()
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, $5, true)
        """,
        uid,
        tid,
        f"{role}-{uid}@ejemplo.com",
        hash_password("x" * 12),
        role,
    )
    return _h(crear_access_token(uid, tid, role))


async def _conectar(http_client, headers, pnid=None):
    pnid = pnid or _pn()
    r = await http_client.post(
        "/api/canales/whatsapp/conectar", json={"phone_number_id": pnid}, headers=headers
    )
    return r, pnid


async def _otro_negocio(http_client):
    from session import execute as _ex

    tid = uuid4()
    await _ex("INSERT INTO tenants (id, name) VALUES ($1, 'Otro')", tid)
    uid = uuid4()
    await _ex(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, 'owner', true)
        """,
        uid,
        tid,
        f"owner-{uid}@ejemplo.com",
        hash_password("x" * 12),
    )
    await _ex(
        """
        INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly)
        VALUES ($1, 'enterprise', 'activa', 299.99)
        """,
        tid,
    )
    return tid, _h(crear_access_token(uid, tid, "owner"))


async def _borrar_tenant(tid):
    await execute("DELETE FROM tenants WHERE id = $1", tid)


@pytest.fixture
async def plan_cupo_1(db):
    """Plan con tope de 1 número, sin límites de modelo/historial y todas las familias."""
    nombre = f"cupo1-{uuid4().hex[:10]}"
    await execute(
        """
        INSERT INTO planes (nombre, precio_monthly, max_numeros_whatsapp, activo,
                            agente_ia_activo, herramientas_activo, calendario_activo)
        VALUES ($1, 9999, 1, true, true, true, true)
        """,
        nombre,
    )
    for fam in ("calendario", "sql_lectura", "http", "google_sheets", "google_docs"):
        await execute(
            "INSERT INTO plan_herramientas (plan_nombre, familia) VALUES ($1, $2)"
            " ON CONFLICT DO NOTHING",
            nombre,
            fam,
        )
    yield nombre
    await execute("DELETE FROM tenant_subscriptions WHERE plan = $1", nombre)
    await execute("DELETE FROM plan_herramientas WHERE plan_nombre = $1", nombre)
    await execute("DELETE FROM planes WHERE nombre = $1", nombre)


async def _suscribir(tid, plan):
    await execute(
        """
        INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly)
        VALUES ($1, $2, 'activa', 10)
        ON CONFLICT (tenant_id) DO UPDATE SET plan = EXCLUDED.plan, estado = 'activa'
        """,
        tid,
        plan,
    )


async def _completar(http_client, headers, numero_id):
    return await http_client.put(
        f"/api/agente/numeros/{numero_id}",
        json={"agent_name": "Agente", "system_prompt": "Ayuda general del negocio."},
        headers=headers,
    )


async def _conversacion(tid, numero_id, estado="transferred"):
    from session import fetch_value as _fv

    user_id = await _fv(
        "INSERT INTO users (tenant_id, display_name) VALUES ($1, 'Cliente') RETURNING id", tid
    )
    return await _fv(
        """
        INSERT INTO conversations (tenant_id, user_id, channel_type, status, agente_canal_id)
        VALUES ($1, $2, 'whatsapp', $3, $4) RETURNING id
        """,
        tid,
        user_id,
        estado,
        numero_id,
    )


# ------------------------------------------------------------
# Alta (RF-2)
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_alta_libre_da_201_y_aparece_en_la_lista(
    http_client, tenant_y_usuario, headers_autenticado
):
    r, pnid = await _conectar(http_client, headers_autenticado)
    assert r.status_code == 201, r.text
    assert r.json()["phone_number_id"] == pnid
    assert r.json()["estado"] == "activo"

    r = await http_client.get("/api/canales/whatsapp/numeros", headers=headers_autenticado)
    assert r.status_code == 200
    cuerpo = r.json()
    assert [n["phone_number_id"] for n in cuerpo["numeros"]] == [pnid]
    assert cuerpo["plazas_usadas"] == 1


@pytest.mark.asyncio
async def test_con_cupo_lleno_cualquier_alta_da_402(
    http_client, tenant_y_usuario, headers_autenticado, plan_cupo_1
):
    tid = tenant_y_usuario["tenant_id"]
    await _suscribir(tid, plan_cupo_1)
    r, pnid_propio = await _conectar(http_client, headers_autenticado)
    assert r.status_code == 201

    for pnid in (_pn(), pnid_propio, "pn-ajeno-inexistente"):
        r, _ = await _conectar(http_client, headers_autenticado, pnid)
        assert r.status_code == 402, (pnid, r.text)
        assert r.json()["detail"]["codigo"] == "cupo_numeros_whatsapp"


@pytest.mark.asyncio
async def test_alta_propia_da_409_activo_o_pausado(
    http_client, tenant_y_usuario, headers_autenticado
):
    r, pnid = await _conectar(http_client, headers_autenticado)
    numero_id = r.json()["id"]
    assert r.status_code == 201

    r, _ = await _conectar(http_client, headers_autenticado, pnid)
    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "numero_ya_vinculado"

    await http_client.post(
        f"/api/canales/whatsapp/numeros/{numero_id}/pausar", headers=headers_autenticado
    )
    r, _ = await _conectar(http_client, headers_autenticado, pnid)
    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "numero_ya_vinculado"


@pytest.mark.asyncio
async def test_alta_ajena_da_404_y_a_la_quinta_bloquea_hasta_la_libre(
    http_client, tenant_y_usuario, headers_autenticado
):
    tid_otro, h_otro = await _otro_negocio(http_client)
    r, pnid_ajeno = await _conectar(http_client, h_otro)
    assert r.status_code == 201

    try:
        for _ in range(5):
            r, _ = await _conectar(http_client, headers_autenticado, pnid_ajeno)
            assert r.status_code == 404
            assert r.json()["detail"]["codigo"] == "numero_no_encontrado"

        r, _ = await _conectar(http_client, headers_autenticado)
        assert r.status_code == 429
        assert r.json()["detail"]["codigo"] == "demasiados_intentos"
        assert r.json()["detail"]["libera_en"]
        assert r.headers.get("retry-after")
    finally:
        await _borrar_tenant(tid_otro)


# ------------------------------------------------------------
# Pausa, reactivación, baja y principal (RF-15, RF-14)
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_pausar_y_reactivar(http_client, tenant_y_usuario, headers_autenticado):
    r, _ = await _conectar(http_client, headers_autenticado)
    numero_id = r.json()["id"]

    r = await http_client.post(
        f"/api/canales/whatsapp/numeros/{numero_id}/pausar", headers=headers_autenticado
    )
    assert r.status_code == 200
    assert r.json()["numero"]["estado"] == "pausado"
    assert r.json()["sin_numero_activo"] is True

    r = await http_client.post(
        f"/api/canales/whatsapp/numeros/{numero_id}/reactivar", headers=headers_autenticado
    )
    assert r.status_code == 200
    assert r.json()["estado"] == "activo"


@pytest.mark.asyncio
async def test_reactivar_sin_cupo_da_402(
    http_client, tenant_y_usuario, headers_autenticado, plan_cupo_1
):
    tid = tenant_y_usuario["tenant_id"]
    r, _ = await _conectar(http_client, headers_autenticado)
    primero = r.json()["id"]
    r, _ = await _conectar(http_client, headers_autenticado)
    segundo = r.json()["id"]
    await http_client.post(
        f"/api/canales/whatsapp/numeros/{segundo}/pausar", headers=headers_autenticado
    )
    await _suscribir(tid, plan_cupo_1)

    r = await http_client.post(
        f"/api/canales/whatsapp/numeros/{segundo}/reactivar", headers=headers_autenticado
    )
    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "cupo_numeros_whatsapp"
    assert primero != segundo


@pytest.mark.asyncio
async def test_desvincular_oculta_y_deja_solo_lectura(
    http_client, tenant_y_usuario, headers_autenticado
):
    tid = tenant_y_usuario["tenant_id"]
    r, _ = await _conectar(http_client, headers_autenticado)
    numero_id = r.json()["id"]
    conv_id = await _conversacion(tid, numero_id)

    r = await http_client.delete(
        f"/api/canales/whatsapp/numeros/{numero_id}", headers=headers_autenticado
    )
    assert r.status_code == 204

    r = await http_client.get("/api/conversaciones", headers=headers_autenticado)
    assert all(c["id"] != str(conv_id) for c in r.json())

    r = await http_client.get(f"/api/conversaciones/{conv_id}", headers=headers_autenticado)
    assert r.status_code == 404

    r = await http_client.get("/api/conversaciones/ocultas", headers=headers_autenticado)
    assert r.status_code == 200
    assert [c["id"] for c in r.json()] == [str(conv_id)]

    r = await http_client.get(
        f"/api/conversaciones/ocultas/{conv_id}", headers=headers_autenticado
    )
    assert r.status_code == 200

    r = await http_client.post(
        f"/api/conversaciones/{conv_id}/mensajes",
        json={"texto": "hola"},
        headers=headers_autenticado,
    )
    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "numero_desvinculado"


@pytest.mark.asyncio
async def test_elegir_principal_y_sus_409(
    http_client, tenant_y_usuario, headers_autenticado
):
    r, _ = await _conectar(http_client, headers_autenticado)
    incompleto = r.json()["id"]
    r, _ = await _conectar(http_client, headers_autenticado)
    completo = r.json()["id"]
    assert (await _completar(http_client, headers_autenticado, completo)).status_code == 200

    r = await http_client.put(
        "/api/canales/whatsapp/principal",
        json={"numero_id": incompleto},
        headers=headers_autenticado,
    )
    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "configuracion_incompleta"

    await http_client.post(
        f"/api/canales/whatsapp/numeros/{completo}/pausar", headers=headers_autenticado
    )
    r = await http_client.put(
        "/api/canales/whatsapp/principal",
        json={"numero_id": completo},
        headers=headers_autenticado,
    )
    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "numero_no_activo"

    await http_client.post(
        f"/api/canales/whatsapp/numeros/{completo}/reactivar", headers=headers_autenticado
    )
    r = await http_client.put(
        "/api/canales/whatsapp/principal",
        json={"numero_id": completo},
        headers=headers_autenticado,
    )
    assert r.status_code == 200
    assert r.json()["es_principal"] is True


@pytest.mark.asyncio
async def test_404_identico_para_ajeno_activo_pausado_e_inexistente(
    http_client, tenant_y_usuario, headers_autenticado
):
    tid_otro, h_otro = await _otro_negocio(http_client)
    r, _ = await _conectar(http_client, h_otro)
    ajeno = r.json()["id"]
    cuerpos = []
    try:
        for nid in (ajeno, str(uuid4())):
            r = await http_client.post(
                f"/api/canales/whatsapp/numeros/{nid}/pausar", headers=headers_autenticado
            )
            assert r.status_code == 404
            cuerpos.append(r.json())
    finally:
        await _borrar_tenant(tid_otro)
    assert cuerpos[0] == cuerpos[1]
    assert cuerpos[0]["detail"]["codigo"] == "numero_no_encontrado"


@pytest.mark.asyncio
async def test_linea_de_avisos_se_pausa_y_desvincula_pero_no_se_elige_principal(
    http_client, tenant_y_usuario, headers_autenticado
):
    r, _ = await _conectar(http_client, headers_autenticado)
    numero_id = r.json()["id"]
    await execute(
        "UPDATE agentes_canal SET es_linea_avisos = true WHERE id = $1", numero_id
    )

    # Principal sigue vetado: el CHECK de sql/56 no deja que lo sea.
    r = await http_client.put(
        "/api/canales/whatsapp/principal",
        json={"numero_id": numero_id},
        headers=headers_autenticado,
    )
    assert r.status_code == 403

    r = await http_client.post(
        f"/api/canales/whatsapp/numeros/{numero_id}/pausar", headers=headers_autenticado
    )
    assert r.status_code == 200
    assert r.json()["numero"]["estado"] == "pausado"
    assert r.json()["sin_numero_activo"] is False

    r = await http_client.delete(
        f"/api/canales/whatsapp/numeros/{numero_id}", headers=headers_autenticado
    )
    assert r.status_code == 204


# ------------------------------------------------------------
# Configuración por número (RF-5) y herramientas (RF-6, RF-17)
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_temperatura_fuera_de_rango_da_400_y_bordes_pasan(
    http_client, tenant_y_usuario, headers_autenticado
):
    r, _ = await _conectar(http_client, headers_autenticado)
    numero_id = r.json()["id"]

    r = await http_client.put(
        f"/api/agente/numeros/{numero_id}",
        json={"agent_name": "A", "system_prompt": "Ayuda.", "temperature": 1.5},
        headers=headers_autenticado,
    )
    assert r.status_code == 400
    assert r.json()["detail"]["codigo"] == "temperatura_fuera_de_rango"

    for temp in (0, 1):
        r = await http_client.put(
            f"/api/agente/numeros/{numero_id}",
            json={"agent_name": "A", "system_prompt": "Ayuda.", "temperature": temp},
            headers=headers_autenticado,
        )
        assert r.status_code == 200, (temp, r.text)


@pytest.mark.asyncio
async def test_modelo_fuera_del_plan_da_402(
    http_client, tenant_y_usuario, headers_autenticado, db
):
    from services.limites_agente import MODELOS_AGENTE

    nombre = f"mod-{uuid4().hex[:10]}"
    await execute(
        "INSERT INTO planes (nombre, precio_monthly, modelos_permitidos, historial_maximo, activo)"
        " VALUES ($1, 9999, ARRAY[$2], 60, true)",
        nombre,
        MODELOS_AGENTE[0],
    )
    try:
        await _suscribir(tenant_y_usuario["tenant_id"], nombre)
        r, _ = await _conectar(http_client, headers_autenticado)
        numero_id = r.json()["id"]

        r = await http_client.put(
            f"/api/agente/numeros/{numero_id}",
            json={"agent_name": "A", "system_prompt": "Ayuda.", "model": MODELOS_AGENTE[2]},
            headers=headers_autenticado,
        )
        assert r.status_code == 402
        assert r.json()["detail"]["codigo"] == "modelo_no_incluido_en_plan"
    finally:
        await execute("DELETE FROM tenant_subscriptions WHERE plan = $1", nombre)
        await execute("DELETE FROM planes WHERE nombre = $1", nombre)


@pytest.mark.asyncio
async def test_herramienta_fuera_del_plan_da_402(
    http_client, tenant_y_usuario, headers_autenticado, db
):
    tid = tenant_y_usuario["tenant_id"]
    nombre = f"fam-{uuid4().hex[:10]}"
    await execute(
        "INSERT INTO planes (nombre, precio_monthly, activo) VALUES ($1, 9999, true)", nombre
    )
    await execute(
        "INSERT INTO plan_herramientas (plan_nombre, familia) VALUES ($1, 'http')", nombre
    )
    try:
        await _suscribir(tid, nombre)
        r, _ = await _conectar(http_client, headers_autenticado)
        numero_id = r.json()["id"]
        await execute(
            """
            INSERT INTO tenant_tools (tenant_id, tool_key, tool_type, display_name, description, config, is_enabled)
            VALUES ($1, 'hoja-x', 'google_sheets', 'Hoja', 'Hoja de prueba', '{}'::jsonb, true),
                   ($1, 'web-x', 'http_generico', 'Web', 'Web de prueba', '{}'::jsonb, true)
            """,
            tid,
        )
        r = await http_client.put(
            f"/api/herramientas/numeros/{numero_id}",
            json={"activar": ["hoja-x"]},
            headers=headers_autenticado,
        )
        assert r.status_code == 402
        assert r.json()["detail"]["codigo"] == "herramienta_no_incluida_en_plan"

        r = await http_client.put(
            f"/api/herramientas/numeros/{numero_id}",
            json={"activar": ["web-x"]},
            headers=headers_autenticado,
        )
        assert r.status_code == 200
        activas = {h["tool_key"]: h for h in r.json()}
        assert activas["web-x"]["activa"] is True
        assert activas["web-x"]["permitida_por_plan"] is True
        assert activas["hoja-x"]["permitida_por_plan"] is False
    finally:
        await execute("DELETE FROM tenant_subscriptions WHERE plan = $1", nombre)
        await execute("DELETE FROM plan_herramientas WHERE plan_nombre = $1", nombre)
        await execute("DELETE FROM planes WHERE nombre = $1", nombre)


@pytest.mark.asyncio
async def test_filtro_por_numero_en_la_lista(
    http_client, tenant_y_usuario, headers_autenticado
):
    tid = tenant_y_usuario["tenant_id"]
    r, _ = await _conectar(http_client, headers_autenticado)
    num_a = r.json()["id"]
    r, _ = await _conectar(http_client, headers_autenticado)
    num_b = r.json()["id"]
    conv_a = await _conversacion(tid, num_a, estado="active")

    r = await http_client.get(
        f"/api/conversaciones?numero_id={num_a}", headers=headers_autenticado
    )
    assert [c["id"] for c in r.json()] == [str(conv_a)]

    r = await http_client.get(
        f"/api/conversaciones?numero_id={num_b}", headers=headers_autenticado
    )
    assert r.json() == []


@pytest.mark.asyncio
async def test_responder_en_pausado_e_incompleto_da_409(
    http_client, tenant_y_usuario, headers_autenticado
):
    tid = tenant_y_usuario["tenant_id"]
    r, _ = await _conectar(http_client, headers_autenticado)
    incompleto = r.json()["id"]
    conv = await _conversacion(tid, incompleto)

    r = await http_client.post(
        f"/api/conversaciones/{conv}/mensajes",
        json={"texto": "hola"},
        headers=headers_autenticado,
    )
    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "configuracion_incompleta"

    r, _ = await _conectar(http_client, headers_autenticado)
    pausado = r.json()["id"]
    await _completar(http_client, headers_autenticado, pausado)
    await http_client.post(
        f"/api/canales/whatsapp/numeros/{pausado}/pausar", headers=headers_autenticado
    )
    conv = await _conversacion(tid, pausado)
    r = await http_client.post(
        f"/api/conversaciones/{conv}/mensajes",
        json={"texto": "hola"},
        headers=headers_autenticado,
    )
    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "numero_no_activo"


# ------------------------------------------------------------
# Roles (RF-18)
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_member_lee_config_y_ocultas_no(
    http_client, tenant_y_usuario, headers_autenticado
):
    tid = tenant_y_usuario["tenant_id"]
    h_member = await _cuenta(tid, "member")
    r, _ = await _conectar(http_client, headers_autenticado)
    numero_id = r.json()["id"]

    assert (
        await http_client.get("/api/canales/whatsapp/numeros", headers=h_member)
    ).status_code == 200
    assert (
        await http_client.get(f"/api/agente/numeros/{numero_id}", headers=h_member)
    ).status_code == 200
    assert (
        await http_client.put(
            f"/api/agente/numeros/{numero_id}",
            json={"agent_name": "X"},
            headers=h_member,
        )
    ).status_code == 403
    assert (
        await http_client.put(
            f"/api/herramientas/numeros/{numero_id}",
            json={"activar": []},
            headers=h_member,
        )
    ).status_code == 403
    assert (
        await http_client.get("/api/conversaciones/ocultas", headers=h_member)
    ).status_code == 403


@pytest.mark.asyncio
async def test_vendedor_y_proveedor_403_en_numeros_pero_ven_lo_suyo(
    http_client, tenant_y_usuario, headers_autenticado
):
    from security import crear_access_token as _token
    from session import fetch_value as _fv

    tid = tenant_y_usuario["tenant_id"]
    uid_vend = uuid4()
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, 'vendedor', true)
        """,
        uid_vend,
        tid,
        f"vendedor-{uid_vend}@ejemplo.com",
        hash_password("x" * 12),
    )
    await execute(
        "INSERT INTO vendedores (tenant_id, portal_user_id, nombre, activo)"
        " VALUES ($1, $2, 'Ana', true)",
        tid,
        uid_vend,
    )
    h_vendedor = _h(_token(uid_vend, tid, "vendedor"))
    h_proveedor = await _cuenta(tid, "proveedor")
    r, _ = await _conectar(http_client, headers_autenticado)
    numero_id = r.json()["id"]

    for h in (h_vendedor, h_proveedor):
        assert (
            await http_client.get("/api/canales/whatsapp/numeros", headers=h)
        ).status_code == 403
        assert (
            await http_client.get(f"/api/agente/numeros/{numero_id}", headers=h)
        ).status_code == 403
        assert (
            await http_client.get(
                f"/api/conversaciones?numero_id={numero_id}", headers=h
            )
        ).status_code == 403

    # En la lista normal, el vendedor con ficha sigue entrando a lo suyo.
    assert (
        await http_client.get("/api/conversaciones", headers=h_vendedor)
    ).status_code == 200
    assert await _fv("SELECT 1") == 1
