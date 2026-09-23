"""
Panel de plataforma (nivel gerencia).

Lo que se cubre acá, en orden de importancia:

1. La puerta. Un owner de tenant con un JWT perfectamente válido NO entra:
   son dos niveles distintos y confundirlos dejaría a cualquier cliente
   mirando a los demás.
2. Que la suspensión manual tenga dientes — que apague el agente de verdad.
3. Que el espejo en SQL del gate de pagos (routers/gerencia.SQL_AGENTE_OPERANDO)
   diga lo mismo que services/acceso_pagos.py en todas las combinaciones. Esa
   duplicación es el punto frágil del módulo, y este test es lo que la sostiene.
4. Que el consumo de tokens no se cuente dos veces cuando n8n reintenta.
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from services.acceso_pagos import acceso_pagos
from services.gerencia import consumo_global, rango_dias, registrar_uso_tokens
from security import crear_access_token, hash_password
from session import execute, fetch_one, fetch_value


# ------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------
@pytest.fixture
async def tenant_id(db):
    tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Negocio de prueba')", tid)
    # El backfill de 14_gerencia_auditoria.sql solo corrió una vez, sobre los
    # tenants que existían entonces; los nuevos los crea la app cuando hace
    # falta. Acá se deja sin fila a propósito en los tests que miran el
    # COALESCE, y se inserta explícitamente en los que necesitan un estado.
    yield tid
    await execute("DELETE FROM gerencia_auditoria WHERE tenant_id = $1", tid)
    await execute("DELETE FROM tenants WHERE id = $1", tid)


@pytest.fixture
async def gerente(db):
    """Un portal_user cuyo correo también está en gerencia_users."""
    tid = uuid4()
    uid = uuid4()
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
        "INSERT INTO gerencia_users (email, full_name, cargo) VALUES ($1, 'Test', 'QA')",
        email,
    )

    yield {
        "email": email,
        "headers": {"Authorization": f"Bearer {crear_access_token(uid, tid, 'owner')}"},
    }

    await execute("DELETE FROM gerencia_users WHERE email = $1", email)
    await execute("DELETE FROM gerencia_auditoria WHERE actor_email = $1", email)
    await execute("DELETE FROM tenants WHERE id = $1", tid)


async def _estado(tenant_id, estado: str, motivo: str | None = "prueba") -> None:
    await execute(
        """
        INSERT INTO tenant_estado_plataforma (tenant_id, estado, motivo)
        VALUES ($1, $2, $3)
        ON CONFLICT (tenant_id) DO UPDATE SET estado = EXCLUDED.estado,
                                              motivo = EXCLUDED.motivo
        """,
        tenant_id,
        estado,
        motivo,
    )


# ------------------------------------------------------------
# 1. La puerta
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_un_owner_de_tenant_no_entra_al_panel_de_plataforma(
    http_client, headers_autenticado
):
    """
    El fixture da un owner con token válido. Es dueño de SU negocio, no de
    la plataforma: 403 en todo el router.
    """
    for path in ("/api/gerencia/resumen", "/api/gerencia/tenants", "/api/gerencia/auditoria"):
        r = await http_client.get(path, headers=headers_autenticado)
        assert r.status_code == 403, path


@pytest.mark.asyncio
async def test_sin_token_no_entra(http_client):
    r = await http_client.get("/api/gerencia/tenants")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_gerencia_ve_el_listado_completo(http_client, gerente, tenant_id):
    r = await http_client.get("/api/gerencia/tenants?limite=100", headers=gerente["headers"])
    assert r.status_code == 200

    cuerpo = r.json()
    assert cuerpo["total"] >= 1
    # Un tenant recién creado, sin fila en tenant_estado_plataforma ni en
    # tenant_servicios, tiene que aparecer igual: que le falte configuración
    # es justamente lo que hay que poder ver.
    assert any(t["tenant_id"] == str(tenant_id) for t in cuerpo["items"])


@pytest.mark.asyncio
async def test_un_tenant_sin_portal_users_tiene_email_null(http_client, gerente, tenant_id):
    """El fixture `tenant_id` no crea ningún portal_user: no hay a quién mostrar."""
    r = await http_client.get(f"/api/gerencia/tenants/{tenant_id}", headers=gerente["headers"])
    assert r.status_code == 200
    assert r.json()["email"] is None


@pytest.mark.asyncio
async def test_el_correo_mostrado_prioriza_al_owner(http_client, gerente, tenant_id):
    """
    Con varios portal_users, se muestra el owner aunque no sea el más
    viejo: mismo criterio de prioridad que /impersonar (ver
    routers/gerencia._sql_fichas).
    """
    async def _crear(email: str, role: str) -> None:
        await execute(
            """
            INSERT INTO portal_users (tenant_id, email, password_hash, role, is_active)
            VALUES ($1, $2, $3, $4, true)
            """,
            tenant_id,
            email,
            hash_password("x" * 12),
            role,
        )

    # El member se crea primero (más antiguo) pero no tiene que ganar.
    await _crear("member@ejemplo.com", "member")
    await _crear("owner@ejemplo.com", "owner")

    r = await http_client.get(f"/api/gerencia/tenants/{tenant_id}", headers=gerente["headers"])
    assert r.json()["email"] == "owner@ejemplo.com"


@pytest.mark.asyncio
async def test_un_portal_user_desactivado_no_cuenta_para_el_correo(
    http_client, gerente, tenant_id
):
    await execute(
        """
        INSERT INTO portal_users (tenant_id, email, password_hash, role, is_active)
        VALUES ($1, 'baja@ejemplo.com', $2, 'owner', false)
        """,
        tenant_id,
        hash_password("x" * 12),
    )

    r = await http_client.get(f"/api/gerencia/tenants/{tenant_id}", headers=gerente["headers"])
    assert r.json()["email"] is None


# ------------------------------------------------------------
# 2. La suspensión tiene dientes
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_suspender_apaga_el_agente_aunque_este_al_dia(db, tenant_id):
    """
    Suscripción activa y créditos: sin suspensión, el agente opera. Con
    ella, no. Es la razón de ser del estado — si no llegara hasta
    acceso_pagos sería solo una etiqueta de colores.
    """
    await execute(
        """
        INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly)
        VALUES ($1, 'pro', 'activa', 99.99)
        """,
        tenant_id,
    )

    assert (await acceso_pagos(tenant_id)).permitido is True

    await _estado(tenant_id, "suspendido", "falta de pago")
    acceso = await acceso_pagos(tenant_id)
    assert acceso.permitido is False
    assert acceso.suspendido is True
    # La suscripción sigue activa: la suspensión no la toca, solo bloquea.
    assert acceso.suscripcion_activa is True


@pytest.mark.asyncio
async def test_prueba_no_bloquea(db, tenant_id):
    """Un piloto tiene que poder usar el servicio; para eso es el piloto."""
    await _estado(tenant_id, "prueba", None)
    acceso = await acceso_pagos(tenant_id)
    assert acceso.permitido is True
    assert acceso.suspendido is False


@pytest.mark.asyncio
async def test_baja_bloquea(db, tenant_id):
    await _estado(tenant_id, "baja", "se fue")
    assert (await acceso_pagos(tenant_id)).permitido is False


@pytest.mark.asyncio
async def test_cambiar_estado_exige_motivo_y_queda_en_la_bitacora(
    http_client, gerente, tenant_id
):
    sin_motivo = await http_client.patch(
        f"/api/gerencia/tenants/{tenant_id}/estado",
        json={"estado": "suspendido"},
        headers=gerente["headers"],
    )
    assert sin_motivo.status_code == 400

    r = await http_client.patch(
        f"/api/gerencia/tenants/{tenant_id}/estado",
        json={"estado": "suspendido", "motivo": "impago de 3 meses"},
        headers=gerente["headers"],
    )
    assert r.status_code == 200
    assert r.json()["estado"] == "suspendido"
    # El agente deja de operar en la misma respuesta, sin esperar a nada.
    assert r.json()["agente_operando"] is False

    bitacora = await fetch_one(
        """
        SELECT actor_email, accion, detalle
        FROM gerencia_auditoria
        WHERE tenant_id = $1 AND accion = 'estado_tenant'
        ORDER BY created_at DESC
        """,
        tenant_id,
    )
    assert bitacora["actor_email"] == gerente["email"]
    assert bitacora["detalle"]["antes"] == "activo"
    assert bitacora["detalle"]["despues"] == "suspendido"
    assert bitacora["detalle"]["motivo"] == "impago de 3 meses"


@pytest.mark.asyncio
async def test_reactivar_no_exige_motivo(http_client, gerente, tenant_id):
    await _estado(tenant_id, "suspendido", "lo que sea")
    r = await http_client.patch(
        f"/api/gerencia/tenants/{tenant_id}/estado",
        json={"estado": "activo"},
        headers=gerente["headers"],
    )
    assert r.status_code == 200
    assert r.json()["estado"] == "activo"


@pytest.mark.asyncio
async def test_estado_de_un_tenant_inexistente_es_404(http_client, gerente):
    r = await http_client.patch(
        f"/api/gerencia/tenants/{uuid4()}/estado",
        json={"estado": "activo"},
        headers=gerente["headers"],
    )
    assert r.status_code == 404


# ------------------------------------------------------------
# 3. El espejo en SQL del gate de pagos
# ------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suscripcion, creditos, estado_plataforma, servicio_encendido",
    [
        (None,        None,          None,          True),   # nunca pagó
        (None,        None,          None,          False),  # apagado por el dueño
        ("activa",    None,          None,          True),
        ("pausada",   Decimal(100),  None,          True),
        ("pausada",   Decimal(0),    None,          True),   # bloqueado por pagos
        ("cancelada", None,          None,          True),
        (None,        Decimal(0),    None,          True),
        ("activa",    Decimal(500),  "suspendido",  True),   # bloqueado a mano
        ("activa",    Decimal(500),  "baja",        True),
        ("activa",    Decimal(500),  "prueba",      True),
        ("pausada",   Decimal(0),    "activo",      True),
    ],
)
async def test_el_sql_del_listado_coincide_con_acceso_pagos(
    db, tenant_id, suscripcion, creditos, estado_plataforma, servicio_encendido
):
    """
    routers/gerencia.py calcula `agente_operando` en SQL para no hacer una
    consulta por fila, duplicando la regla de services/acceso_pagos.py. Este
    test es el contrato entre las dos: si alguien cambia una sola, falla.
    """
    from routers.gerencia import SQL_AGENTE_OPERANDO

    if suscripcion is not None:
        await execute(
            """
            INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly)
            VALUES ($1, 'pro', $2, 99.99)
            """,
            tenant_id,
            suscripcion,
        )
    if creditos is not None:
        await execute(
            "INSERT INTO tenant_credits (tenant_id, creditos_disponibles) VALUES ($1, $2)",
            tenant_id,
            creditos,
        )
    if estado_plataforma is not None:
        await _estado(tenant_id, estado_plataforma)

    await execute(
        """
        INSERT INTO tenant_servicios (tenant_id, agente_ia_activo)
        VALUES ($1, $2)
        ON CONFLICT (tenant_id) DO UPDATE SET agente_ia_activo = EXCLUDED.agente_ia_activo
        """,
        tenant_id,
        servicio_encendido,
    )

    segun_sql = await fetch_value(
        f"""
        SELECT ({SQL_AGENTE_OPERANDO})
        FROM v_gerencia_tenants g
        LEFT JOIN tenant_credits cr ON cr.tenant_id = g.tenant_id
        WHERE g.tenant_id = $1
        """,
        tenant_id,
    )

    acceso = await acceso_pagos(tenant_id)
    segun_python = servicio_encendido and acceso.permitido

    assert segun_sql == segun_python, (
        f"SQL dice {segun_sql} y acceso_pagos dice {segun_python} "
        f"(suscripción={suscripcion}, créditos={creditos}, estado={estado_plataforma})"
    )


# ------------------------------------------------------------
# 4. Consumo de tokens
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_consumo_se_suma_por_tenant(db, tenant_id):
    for entrada, salida in ((1000, 200), (500, 100)):
        await registrar_uso_tokens(
            tenant_id,
            conversation_id=None,
            origen="agente",
            modelo="gpt-4o-mini",
            tokens_entrada=entrada,
            tokens_salida=salida,
            costo_usd=Decimal("0.001500"),
            idempotency_key=None,
        )

    consumo = await consumo_global(await rango_dias(7), tenant_id)
    assert consumo.tokens_entrada == 1500
    assert consumo.tokens_salida == 300
    assert consumo.tokens_total == 1800
    assert consumo.llamadas == 2
    assert consumo.costo_usd == Decimal("0.003000")


@pytest.mark.asyncio
async def test_un_reintento_de_n8n_no_cuenta_dos_veces(db, tenant_id):
    clave = f"exec-{uuid4()}:nodo-agente"

    async def reportar():
        return await registrar_uso_tokens(
            tenant_id,
            conversation_id=None,
            origen="agente",
            modelo="gpt-4o-mini",
            tokens_entrada=800,
            tokens_salida=120,
            costo_usd=None,
            idempotency_key=clave,
        )

    assert await reportar() is True
    assert await reportar() is False, "el segundo intento no debería insertar"

    consumo = await consumo_global(await rango_dias(7), tenant_id)
    assert consumo.llamadas == 1
    assert consumo.tokens_total == 920


@pytest.mark.asyncio
async def test_sin_clave_de_idempotencia_cada_reporte_cuenta(db, tenant_id):
    """
    El índice único es parcial: sin clave no hay candado, y dos llamadas
    iguales al modelo son dos consumos reales, no un duplicado.
    """
    for _ in range(2):
        assert (
            await registrar_uso_tokens(
                tenant_id,
                conversation_id=None,
                origen="herramienta",
                modelo=None,
                tokens_entrada=10,
                tokens_salida=5,
                costo_usd=None,
                idempotency_key=None,
            )
            is True
        )

    assert (await consumo_global(await rango_dias(7), tenant_id)).llamadas == 2


@pytest.mark.asyncio
async def test_n8n_reporta_consumo_por_el_endpoint_interno(http_client, tenant_id, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "N8N_INTERNAL_TOKEN", "secreto-de-prueba")
    clave = f"exec-{uuid4()}"
    cuerpo = {
        "tenant_id": str(tenant_id),
        "modelo": "gpt-4o",
        "tokens_entrada": 1200,
        "tokens_salida": 340,
        "costo_usd": "0.012000",
        "idempotency_key": clave,
    }
    headers = {"X-Internal-Token": "secreto-de-prueba"}

    primera = await http_client.post("/api/eventos/uso-tokens", json=cuerpo, headers=headers)
    assert primera.status_code == 200
    assert primera.json() == {"registrado": True, "duplicado": False}

    segunda = await http_client.post("/api/eventos/uso-tokens", json=cuerpo, headers=headers)
    assert segunda.json() == {"registrado": False, "duplicado": True}


@pytest.mark.asyncio
async def test_el_endpoint_de_consumo_exige_nivel_gerencia(http_client, headers_autenticado):
    r = await http_client.get("/api/gerencia/consumo", headers=headers_autenticado)
    assert r.status_code == 403


# ------------------------------------------------------------
# 5. Ajuste manual de créditos
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_regalar_creditos_deja_asiento_en_el_libro_mayor(
    http_client, gerente, tenant_id
):
    r = await http_client.post(
        f"/api/gerencia/tenants/{tenant_id}/creditos",
        json={"cantidad": "250.00", "motivo": "cortesía por la caída del martes"},
        headers=gerente["headers"],
    )
    assert r.status_code == 200
    assert Decimal(r.json()["creditos_disponibles"]) == Decimal("250.00")

    asiento = await fetch_one(
        """
        SELECT tipo, cantidad, saldo_anterior, saldo_nuevo, concepto
        FROM credit_transactions
        WHERE tenant_id = $1
        ORDER BY created_at DESC
        """,
        tenant_id,
    )
    assert asiento["tipo"] == "ajuste"
    assert asiento["saldo_anterior"] == Decimal(0)
    assert asiento["saldo_nuevo"] == Decimal("250.00")
    assert gerente["email"] in asiento["concepto"]


@pytest.mark.asyncio
async def test_un_ajuste_no_puede_dejar_el_saldo_negativo(http_client, gerente, tenant_id):
    """
    tenant_credits tiene un CHECK que lo impide; el endpoint lo atrapa
    antes para devolver un 400 explicando cuánto hay, en vez de un 500.
    """
    await execute(
        "INSERT INTO tenant_credits (tenant_id, creditos_disponibles) VALUES ($1, 50)",
        tenant_id,
    )

    r = await http_client.post(
        f"/api/gerencia/tenants/{tenant_id}/creditos",
        json={"cantidad": "-100", "motivo": "corrección"},
        headers=gerente["headers"],
    )
    assert r.status_code == 400

    saldo = await fetch_value(
        "SELECT creditos_disponibles FROM tenant_credits WHERE tenant_id = $1", tenant_id
    )
    assert saldo == Decimal(50), "el saldo no se tocó"


# ------------------------------------------------------------
# 6. Servicios
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_apagar_un_servicio_no_toca_el_otro(http_client, gerente, tenant_id):
    await execute(
        """
        INSERT INTO tenant_servicios (tenant_id, agente_ia_activo, gestion_vendedores_activo)
        VALUES ($1, true, true)
        ON CONFLICT (tenant_id) DO UPDATE SET agente_ia_activo = true,
                                              gestion_vendedores_activo = true
        """,
        tenant_id,
    )

    r = await http_client.patch(
        f"/api/gerencia/tenants/{tenant_id}/servicios",
        json={"agente_ia_activo": False},
        headers=gerente["headers"],
    )
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["agente_ia_activo"] is False
    assert cuerpo["gestion_vendedores_activo"] is True


@pytest.mark.asyncio
async def test_un_patch_vacio_de_servicios_se_rechaza(http_client, gerente, tenant_id):
    r = await http_client.patch(
        f"/api/gerencia/tenants/{tenant_id}/servicios",
        json={},
        headers=gerente["headers"],
    )
    assert r.status_code == 422


# ------------------------------------------------------------
# 7. Las consultas pesadas del panel
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_resumen_responde_y_cuadra_con_lo_que_hay(http_client, gerente, tenant_id):
    """
    El SQL del resumen son cinco CTE cruzados; este test existe sobre todo
    para que no se quede sin ejecutar nunca hasta que alguien abra el panel.
    """
    await _estado(tenant_id, "suspendido", "prueba de humo")
    await registrar_uso_tokens(
        tenant_id,
        conversation_id=None,
        origen="agente",
        modelo="gpt-4o",
        tokens_entrada=300,
        tokens_salida=100,
        costo_usd=Decimal("0.004000"),
        idempotency_key=None,
    )

    r = await http_client.get("/api/gerencia/resumen?dias=7", headers=gerente["headers"])
    assert r.status_code == 200

    d = r.json()
    assert d["dias"] == 7
    assert d["tenants_total"] >= 2  # el del fixture y el del gerente
    assert d["tenants_suspendidos"] >= 1
    assert d["consumo"]["tokens_total"] >= 400
    # Cero es un resultado válido; lo que no puede venir es null.
    for clave in ("mrr", "ingresos_periodo"):
        assert d[clave] is not None


@pytest.mark.asyncio
async def test_el_detalle_trae_el_consumo_del_negocio(http_client, gerente, tenant_id):
    await registrar_uso_tokens(
        tenant_id,
        conversation_id=None,
        origen="herramienta",
        modelo="gpt-4o-mini",
        tokens_entrada=50,
        tokens_salida=25,
        costo_usd=None,
        idempotency_key=None,
    )

    r = await http_client.get(
        f"/api/gerencia/tenants/{tenant_id}", headers=gerente["headers"]
    )
    assert r.status_code == 200

    d = r.json()
    assert d["tenant_id"] == str(tenant_id)
    assert d["consumo"]["tokens_total"] == 75
    # Sin suscripción ni créditos: nunca pasó por pagos, así que opera.
    assert d["agente_operando"] is True


@pytest.mark.asyncio
async def test_el_detalle_de_un_tenant_inexistente_es_404(http_client, gerente):
    r = await http_client.get(f"/api/gerencia/tenants/{uuid4()}", headers=gerente["headers"])
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_la_serie_de_consumo_agrupa_por_dia_y_por_modelo(
    http_client, gerente, tenant_id
):
    for modelo, entrada in (("gpt-4o", 1000), ("gpt-4o-mini", 200), ("gpt-4o", 500)):
        await registrar_uso_tokens(
            tenant_id,
            conversation_id=None,
            origen="agente",
            modelo=modelo,
            tokens_entrada=entrada,
            tokens_salida=0,
            costo_usd=Decimal("0.001"),
            idempotency_key=None,
        )

    r = await http_client.get(
        f"/api/gerencia/consumo?dias=7&tenant_id={tenant_id}", headers=gerente["headers"]
    )
    assert r.status_code == 200

    d = r.json()
    assert d["total"]["tokens_total"] == 1700
    # Las tres llamadas son de hoy: un solo punto en la serie.
    assert len(d["por_dia"]) == 1
    assert d["por_dia"][0]["tokens_total"] == 1700
    # Dos modelos, el más caro primero.
    assert [m["modelo"] for m in d["por_modelo"]] == ["gpt-4o", "gpt-4o-mini"]
    assert d["por_modelo"][0]["tokens_total"] == 1500


@pytest.mark.asyncio
async def test_se_puede_buscar_por_nombre_y_por_uuid(http_client, gerente, tenant_id):
    por_uuid = await http_client.get(
        f"/api/gerencia/tenants?q={tenant_id}", headers=gerente["headers"]
    )
    assert [t["tenant_id"] for t in por_uuid.json()["items"]] == [str(tenant_id)]

    por_nombre = await http_client.get(
        "/api/gerencia/tenants?q=Negocio de prueba&limite=100", headers=gerente["headers"]
    )
    assert any(t["tenant_id"] == str(tenant_id) for t in por_nombre.json()["items"])

    sin_coincidencias = await http_client.get(
        "/api/gerencia/tenants?q=zzz-no-existe-zzz", headers=gerente["headers"]
    )
    cuerpo = sin_coincidencias.json()
    assert cuerpo["total"] == 0
    assert cuerpo["items"] == []


@pytest.mark.asyncio
async def test_un_orden_inventado_se_rechaza(http_client, gerente):
    """
    El orden se interpola en el SQL, así que solo puede salir del
    diccionario ORDENES. Cualquier otra cosa es un 400, no una consulta.
    """
    r = await http_client.get(
        "/api/gerencia/tenants?orden=nombre;DROP TABLE tenants", headers=gerente["headers"]
    )
    assert r.status_code == 400
