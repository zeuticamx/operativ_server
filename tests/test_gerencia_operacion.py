"""
Panel de plataforma, segunda parte: salud, alertas de consumo, equipo,
cohortes, margen e impersonación.

La impersonación va primero y con más tests: es la única pieza de todo el
panel que le da a alguien acceso a los datos de un cliente con otra
identidad, así que cada garantía que promete tiene su test.
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from config import settings
from security import crear_access_token, hash_password
from services.gerencia_salud import (
    TIPO_ALERTA_CONSUMO,
    detectar_consumo_anomalo,
    registrar_alertas_consumo,
)
from session import execute, fetch_one, fetch_value


# ------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------
async def _crear_portal_user(tenant_id, rol: str = "owner", email: str | None = None):
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


@pytest.fixture
async def negocio(db):
    """Un tenant con su dueño."""
    tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Ferretería de prueba')", tid)
    uid, email = await _crear_portal_user(tid, "owner")
    yield {"tenant_id": tid, "owner_id": uid, "owner_email": email}
    await execute("DELETE FROM gerencia_alertas WHERE tenant_id = $1", tid)
    await execute("DELETE FROM gerencia_auditoria WHERE tenant_id = $1", tid)
    await execute("DELETE FROM tenants WHERE id = $1", tid)


@pytest.fixture
async def gerente(db):
    tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Interno')", tid)
    uid, email = await _crear_portal_user(tid, "owner", f"gerente-{uuid4()}@operativai.test")
    await execute(
        "INSERT INTO gerencia_users (email, full_name, cargo) VALUES ($1, 'Test', 'QA')",
        email,
    )
    yield {
        "id": uid,
        "email": email,
        "headers": {"Authorization": f"Bearer {crear_access_token(uid, tid, 'owner')}"},
    }
    await execute("DELETE FROM gerencia_users WHERE LOWER(email) = LOWER($1)", email)
    await execute("DELETE FROM gerencia_auditoria WHERE actor_email = $1", email)
    await execute("DELETE FROM tenants WHERE id = $1", tid)


async def _impersonar(http_client, gerente, tenant_id, motivo="soporte: el cliente no ve sus canales"):
    return await http_client.post(
        f"/api/gerencia/tenants/{tenant_id}/impersonar",
        json={"motivo": motivo},
        headers=gerente["headers"],
    )


# ------------------------------------------------------------
# 1. Impersonación
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_ver_como_entra_como_el_dueno_y_queda_en_la_bitacora(
    http_client, gerente, negocio
):
    r = await _impersonar(http_client, gerente, negocio["tenant_id"])
    assert r.status_code == 200
    assert r.json()["email_usuario"] == negocio["owner_email"]

    yo = await http_client.get(
        "/api/auth/yo", headers={"Authorization": f"Bearer {r.json()['access_token']}"}
    )
    assert yo.status_code == 200
    cuerpo = yo.json()
    assert cuerpo["email"] == negocio["owner_email"]
    assert cuerpo["tenant_id"] == str(negocio["tenant_id"])
    assert cuerpo["impersonado_por"] == gerente["email"]
    assert cuerpo["impersonacion_expira"] is not None

    asiento = await fetch_one(
        "SELECT actor_email, detalle FROM gerencia_auditoria "
        "WHERE tenant_id = $1 AND accion = 'impersonacion'",
        negocio["tenant_id"],
    )
    assert asiento["actor_email"] == gerente["email"]
    assert asiento["detalle"]["como"] == negocio["owner_email"]
    assert "canales" in asiento["detalle"]["motivo"]


@pytest.mark.asyncio
async def test_ver_como_es_solo_lectura(http_client, gerente, negocio):
    """
    Cualquier método que no sea de lectura se rechaza en deps.usuario_actual,
    antes de llegar al endpoint. Se prueba contra un endpoint real que el
    dueño sí podría usar, para que el 403 venga de la impersonación y no de
    un permiso que igual le faltaba.
    """
    token = (await _impersonar(http_client, gerente, negocio["tenant_id"])).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    lectura = await http_client.get(
        f"/api/tenants/{negocio['tenant_id']}/servicios", headers=headers
    )
    assert lectura.status_code == 200

    escritura = await http_client.patch(
        f"/api/tenants/{negocio['tenant_id']}/servicios",
        json={"agente_ia_activo": False},
        headers=headers,
    )
    assert escritura.status_code == 403
    assert "solo lectura" in escritura.json()["detail"]

    # Y de verdad no cambió nada.
    sigue = await fetch_value(
        "SELECT COALESCE(agente_ia_activo, true) FROM tenant_servicios WHERE tenant_id = $1",
        negocio["tenant_id"],
    )
    assert sigue is not False


@pytest.mark.asyncio
async def test_desde_ver_como_no_se_vuelve_a_entrar_a_plataforma(http_client, gerente, negocio):
    """
    Aunque el dueño impersonado fuera él mismo de gerencia, la sesión es la
    del negocio: el panel de plataforma queda cerrado.
    """
    await execute(
        "INSERT INTO gerencia_users (email, full_name, cargo) VALUES ($1, 'Dueño', 'x')",
        negocio["owner_email"],
    )
    try:
        token = (await _impersonar(http_client, gerente, negocio["tenant_id"])).json()[
            "access_token"
        ]
        r = await http_client.get(
            "/api/gerencia/resumen", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 403
    finally:
        await execute("DELETE FROM gerencia_users WHERE email = $1", negocio["owner_email"])


@pytest.mark.asyncio
async def test_si_al_gerente_lo_sacan_la_sesion_muere(http_client, gerente, negocio):
    token = (await _impersonar(http_client, gerente, negocio["tenant_id"])).json()["access_token"]
    await execute("DELETE FROM gerencia_users WHERE LOWER(email) = LOWER($1)", gerente["email"])

    r = await http_client.get("/api/auth/yo", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_el_token_de_ver_como_no_se_puede_refrescar(http_client, gerente, negocio):
    token = (await _impersonar(http_client, gerente, negocio["tenant_id"])).json()["access_token"]
    r = await http_client.post("/api/auth/refresh", json={"refresh_token": token})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_ver_como_exige_motivo(http_client, gerente, negocio):
    r = await _impersonar(http_client, gerente, negocio["tenant_id"], motivo="")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_nunca_entra_como_vendedor(http_client, gerente, db):
    """Un negocio cuyo único usuario es un vendedor no tiene portal que ver."""
    tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Solo vendedores')", tid)
    try:
        await _crear_portal_user(tid, "vendedor")
        r = await _impersonar(http_client, gerente, tid)
        assert r.status_code == 409
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", tid)


@pytest.mark.asyncio
async def test_un_owner_comun_no_puede_impersonar(http_client, headers_autenticado, negocio):
    r = await http_client.post(
        f"/api/gerencia/tenants/{negocio['tenant_id']}/impersonar",
        json={"motivo": "quiero ver a la competencia"},
        headers=headers_autenticado,
    )
    assert r.status_code == 403


# ------------------------------------------------------------
# 2. Salud y consumo anómalo
# ------------------------------------------------------------
async def _consumo(tenant_id, tokens: int, hace_dias: float = 0) -> None:
    await execute(
        """
        INSERT INTO tenant_token_usage (tenant_id, tokens_entrada, tokens_salida, created_at)
        VALUES ($1, $2, 0, NOW() - make_interval(secs => $3))
        """,
        tenant_id,
        tokens,
        hace_dias * 86400,
    )


@pytest.mark.asyncio
async def test_un_pico_sobre_el_promedio_es_anomalo(db, negocio):
    tid = negocio["tenant_id"]
    # Historia estable: 30 000 diarios durante la semana previa.
    for dia in range(2, 9):
        await _consumo(tid, 30_000, hace_dias=dia)
    # Hoy: 10 veces eso.
    await _consumo(tid, 300_000)

    anomalos = {f["tenant_id"] for f in await detectar_consumo_anomalo()}
    assert tid in anomalos


@pytest.mark.asyncio
async def test_crecer_dentro_de_lo_normal_no_es_anomalo(db, negocio):
    tid = negocio["tenant_id"]
    for dia in range(2, 9):
        await _consumo(tid, 30_000, hace_dias=dia)
    await _consumo(tid, 60_000)  # el doble: crecimiento, no incidente

    assert tid not in {f["tenant_id"] for f in await detectar_consumo_anomalo()}


@pytest.mark.asyncio
async def test_un_negocio_nuevo_no_dispara_con_poco_consumo(db, negocio):
    """Promedio 0: sin el piso, la primera respuesta ya sería 'infinitas veces' lo normal."""
    await _consumo(negocio["tenant_id"], 5_000)
    assert negocio["tenant_id"] not in {f["tenant_id"] for f in await detectar_consumo_anomalo()}


@pytest.mark.asyncio
async def test_la_alerta_de_consumo_no_se_repite_mientras_siga_abierta(db, negocio):
    tid = negocio["tenant_id"]
    await _consumo(tid, settings.CONSUMO_ANOMALO_PISO_TOKENS * 10)

    primera = await registrar_alertas_consumo()
    segunda = await registrar_alertas_consumo()

    assert any(a["nombre"] == "Ferretería de prueba" for a in primera)
    assert not any(a["nombre"] == "Ferretería de prueba" for a in segunda)
    abiertas = await fetch_value(
        "SELECT COUNT(*) FROM gerencia_alertas WHERE tenant_id = $1 AND tipo = $2 "
        "AND revisada_en IS NULL",
        tid,
        TIPO_ALERTA_CONSUMO,
    )
    assert abiertas == 1


@pytest.mark.asyncio
async def test_revisar_la_alerta_permite_que_un_pico_nuevo_abra_otra(
    http_client, gerente, negocio
):
    tid = negocio["tenant_id"]
    await _consumo(tid, settings.CONSUMO_ANOMALO_PISO_TOKENS * 10)
    await registrar_alertas_consumo()

    alerta_id = await fetch_value(
        "SELECT id FROM gerencia_alertas WHERE tenant_id = $1 AND revisada_en IS NULL", tid
    )
    r = await http_client.patch(
        f"/api/gerencia/alertas/{alerta_id}/revisar", headers=gerente["headers"]
    )
    assert r.status_code == 200
    assert r.json()["revisada_por"] == gerente["email"]

    otra_vez = await http_client.patch(
        f"/api/gerencia/alertas/{alerta_id}/revisar", headers=gerente["headers"]
    )
    assert otra_vez.status_code == 404

    nuevas = await registrar_alertas_consumo()
    assert any(a["nombre"] == "Ferretería de prueba" for a in nuevas)


@pytest.mark.asyncio
async def test_la_pantalla_de_salud_junta_todo(http_client, gerente, negocio):
    tid = negocio["tenant_id"]
    # Agente encendido, pero sin plan vigente ni créditos: bloqueado por pago.
    await execute(
        """
        INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly)
        VALUES ($1, 'pro', 'pausada', 99.99)
        """,
        tid,
    )
    # Token de Meta que vence mañana.
    await execute(
        """
        INSERT INTO meta_connections (tenant_id, user_token, token_expires_at)
        VALUES ($1, 'x'::bytea, NOW() + INTERVAL '1 day')
        """,
        tid,
    )

    r = await http_client.get("/api/gerencia/salud", headers=gerente["headers"])
    assert r.status_code == 200

    tipos = {p["tipo"] for p in r.json()["problemas"] if p["tenant_id"] == str(tid)}
    assert "agente_bloqueado" in tipos
    assert "token_meta" in tipos
    assert not any(p["tipo"] == "detector_fallido" for p in r.json()["problemas"])


# ------------------------------------------------------------
# 3. Equipo de plataforma
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_alta_y_baja_de_gerencia_con_bitacora(http_client, gerente):
    email = f"nuevo-{uuid4()}@ejemplo.com"
    try:
        alta = await http_client.post(
            "/api/gerencia/usuarios",
            json={"email": email.upper(), "full_name": "Nueva Persona", "cargo": "Soporte"},
            headers=gerente["headers"],
        )
        assert alta.status_code == 201
        # Se normaliza a minúsculas: el LEFT JOIN de deps compara con LOWER
        # igual, pero dos filas que solo difieren en mayúsculas no deberían
        # poder existir.
        assert alta.json()["email"] == email
        assert alta.json()["tiene_cuenta_portal"] is False

        repetida = await http_client.post(
            "/api/gerencia/usuarios",
            json={"email": email, "full_name": "Otra vez", "cargo": "Soporte"},
            headers=gerente["headers"],
        )
        assert repetida.status_code == 409

        baja = await http_client.delete(
            f"/api/gerencia/usuarios/{alta.json()['id']}", headers=gerente["headers"]
        )
        assert baja.status_code == 204

        acciones = [
            f["accion"]
            for f in await fetch_all_acciones(gerente["email"])
        ]
        assert "gerencia_alta" in acciones and "gerencia_baja" in acciones
    finally:
        await execute("DELETE FROM gerencia_users WHERE email = $1", email)


async def fetch_all_acciones(actor_email):
    from session import fetch_all

    return await fetch_all(
        "SELECT accion FROM gerencia_auditoria WHERE actor_email = $1", actor_email
    )


@pytest.mark.asyncio
async def test_nadie_se_quita_el_nivel_a_si_mismo(http_client, gerente):
    lista = await http_client.get("/api/gerencia/usuarios", headers=gerente["headers"])
    yo = next(u for u in lista.json() if u["es_yo"])

    r = await http_client.delete(f"/api/gerencia/usuarios/{yo['id']}", headers=gerente["headers"])
    assert r.status_code == 400
    assert await fetch_value(
        "SELECT 1 FROM gerencia_users WHERE id = $1", yo["id"]
    ) == 1


# ------------------------------------------------------------
# 4. Cohortes
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_cohorte_del_mes_actual_cuenta_al_negocio_activo(http_client, gerente, negocio):
    tid = negocio["tenant_id"]
    uid = uuid4()
    cid = uuid4()
    await execute("INSERT INTO users (id, tenant_id) VALUES ($1, $2)", uid, tid)
    await execute(
        "INSERT INTO conversations (id, tenant_id, user_id, channel_type) VALUES ($1, $2, $3, 'whatsapp')",
        cid,
        tid,
        uid,
    )
    await execute(
        "INSERT INTO messages (tenant_id, conversation_id, role, content) VALUES ($1, $2, 'user', 'hola')",
        tid,
        cid,
    )

    r = await http_client.get("/api/gerencia/cohortes?meses=1", headers=gerente["headers"])
    assert r.status_code == 200

    cohortes = r.json()["cohortes"]
    assert len(cohortes) == 1
    actual = cohortes[0]
    # El del fixture y el del gerente se dieron de alta este mes.
    assert actual["tamano"] >= 2
    assert len(actual["retencion"]) == 1
    assert actual["activos"][0] >= 1
    assert 0 < actual["retencion"][0] <= 100


# ------------------------------------------------------------
# 5. Margen
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_sin_tipo_de_cambio_no_se_inventa_margen(http_client, gerente, negocio, monkeypatch):
    monkeypatch.setattr(settings, "TIPO_CAMBIO_USD", "")
    # Banxico apagado a propósito: este test es del camino "sin ninguna
    # fuente", y si el .env real de quien corre los tests trae un
    # BANXICO_TOKEN de verdad, obtener_tipo_cambio() lo usaría primero y
    # el margen dejaría de salir None (ver services/banxico.py).
    monkeypatch.setattr(settings, "BANXICO_TOKEN", "")
    monkeypatch.setattr(settings, "STRIPE_CURRENCY", "mxn")
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "stripe")

    r = await http_client.get(
        f"/api/gerencia/tenants/{negocio['tenant_id']}", headers=gerente["headers"]
    )
    assert r.status_code == 200
    assert r.json()["margen"] is None
    assert r.json()["costo_moneda"] is None


@pytest.mark.asyncio
async def test_el_margen_resta_el_costo_convertido_al_ingreso_prorrateado(
    http_client, gerente, negocio, monkeypatch
):
    monkeypatch.setattr(settings, "TIPO_CAMBIO_USD", "20")
    # Banxico apagado: este test fija el tipo de cambio a mano (20) para
    # que la cuenta sea predecible, y Banxico tiene prioridad si está
    # configurado — ver el comentario en el test de arriba.
    monkeypatch.setattr(settings, "BANXICO_TOKEN", "")
    monkeypatch.setattr(settings, "STRIPE_CURRENCY", "mxn")
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "stripe")

    tid = negocio["tenant_id"]
    await execute(
        """
        INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly)
        VALUES ($1, 'pro', 'activa', 300)
        """,
        tid,
    )
    await execute(
        """
        INSERT INTO tenant_token_usage (tenant_id, tokens_entrada, costo_usd)
        VALUES ($1, 1000, 2.5)
        """,
        tid,
    )

    r = await http_client.get(f"/api/gerencia/tenants/{tid}?dias=30", headers=gerente["headers"])
    d = r.json()
    # 300 al mes, ventana de 30 días → 300 de ingreso. 2.5 USD × 20 = 50.
    assert Decimal(d["ingreso_periodo"]) == Decimal("300.00")
    assert Decimal(d["costo_moneda"]) == Decimal("50.00")
    assert Decimal(d["margen"]) == Decimal("250.00")


@pytest.mark.asyncio
async def test_ordenar_por_margen_pone_primero_al_que_pierde_plata(
    http_client, gerente, negocio, monkeypatch
):
    monkeypatch.setattr(settings, "TIPO_CAMBIO_USD", "20")
    monkeypatch.setattr(settings, "BANXICO_TOKEN", "")
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "stripe")
    monkeypatch.setattr(settings, "STRIPE_CURRENCY", "mxn")

    # Sin plan y con 100 USD de consumo: margen −2000, el peor posible acá.
    await execute(
        "INSERT INTO tenant_token_usage (tenant_id, tokens_entrada, costo_usd) VALUES ($1, 1, 100)",
        negocio["tenant_id"],
    )
    r = await http_client.get(
        "/api/gerencia/tenants?orden=margen&limite=1", headers=gerente["headers"]
    )
    assert r.json()["items"][0]["tenant_id"] == str(negocio["tenant_id"])
    assert r.json()["tipo_cambio_configurado"] is True
