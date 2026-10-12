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


# ------------------------------------------------------------
# 6. Reporte de cancelaciones tardias (spec 001, R34-R46)
# ------------------------------------------------------------
import itertools
from datetime import datetime as _datetime, timedelta as _timedelta, timezone as _timezone

RUTA_CANCELACIONES = "/api/gerencia/cancelaciones-tardias"
_HORA_CITA = itertools.count()


async def _negocio_con_citas(zona: str | None = "America/Mexico_City", nombre: str = "Peluquería"):
    """Tenant + proveedor + servicio (+ zona horaria opcional). Devuelve ids para insertar reservas."""
    tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, $2)", tid, f"{nombre} {tid}")
    if zona is not None:
        await execute(
            "INSERT INTO tenant_servicios (tenant_id, calendario_activo, zona_horaria) "
            "VALUES ($1, true, $2)",
            tid,
            zona,
        )
    proveedor = await fetch_value(
        "INSERT INTO proveedores (tenant_id, nombre) VALUES ($1, 'Ana') RETURNING id", tid
    )
    servicio = await fetch_value(
        "INSERT INTO servicios (tenant_id, nombre, duracion_minutos) VALUES ($1, 'Corte', 30) RETURNING id",
        tid,
    )
    return {"tid": tid, "proveedor": proveedor, "servicio": servicio}


async def _cancelada(
    n,
    cancelada_en,
    por: str | None = "cliente",
    tardia: bool = False,
    servicio=None,
    estado: str = "cancelada",
):
    # Una hora de inicio distinta por cita: el proveedor no admite citas solapadas.
    inicio = _datetime(2040, 1, 1, tzinfo=_timezone.utc) + _timedelta(hours=next(_HORA_CITA))
    return await fetch_value(
        """
        INSERT INTO reservas (tenant_id, proveedor_id, servicio_id, hora_inicio, hora_fin, estado,
                              cliente_nombre, cliente_telefono, cancelada_por, cancelacion_tardia,
                              cancelada_en)
        VALUES ($1, $2, $3, $4, $5, $6, 'Cliente Secreto', '+5215500000000', $7, $8, $9)
        RETURNING id
        """,
        n["tid"],
        n["proveedor"],
        servicio or n["servicio"],
        inicio,
        inicio + _timedelta(minutes=30),
        estado,
        por,
        tardia,
        cancelada_en,
    )


async def _borrar_negocio_citas(*negocios) -> None:
    for n in negocios:
        await execute("DELETE FROM reservas WHERE tenant_id = $1", n["tid"])
        await execute("DELETE FROM tenants WHERE id = $1", n["tid"])


def _utc(*args) -> _datetime:
    return _datetime(*args, tzinfo=_timezone.utc)


async def _reporte(http_client, headers, **params):
    return await http_client.get(RUTA_CANCELACIONES, params=params, headers=headers)


@pytest.mark.asyncio
async def test_reporte_gerencia_200_con_filas_del_contrato_y_solo_cliente(http_client, gerente):
    n = await _negocio_con_citas()
    try:
        await _cancelada(n, _utc(2031, 3, 10, 18), "cliente", tardia=True)
        await _cancelada(n, _utc(2031, 3, 10, 19), "cliente", tardia=False)
        await _cancelada(n, _utc(2031, 3, 10, 20), "equipo", tardia=False)  # no cuenta
        await _cancelada(n, _utc(2031, 3, 10, 21), None, estado="confirmada")  # no cuenta

        r = await _reporte(
            http_client, gerente["headers"], desde="2031-03-01", hasta="2031-03-31",
            tenant_id=str(n["tid"]),
        )
        assert r.status_code == 200, r.text
        filas = r.json()
        assert len(filas) == 1
        fila = filas[0]
        assert set(fila) == {
            "tenant_id", "tenant_nombre", "servicio_id", "servicio_nombre",
            "periodo", "tardias", "total_cliente",
        }
        assert fila["tenant_id"] == str(n["tid"])
        assert fila["servicio_id"] == str(n["servicio"])
        assert fila["servicio_nombre"] == "Corte"
        assert fila["periodo"] == "2031-03-10"
        assert (fila["tardias"], fila["total_cliente"]) == (1, 2)
        # R39: nada del cliente final.
        assert "Secreto" not in r.text and "5500000000" not in r.text
    finally:
        await _borrar_negocio_citas(n)


@pytest.mark.asyncio
async def test_reporte_cerrado_a_quien_no_es_gerencia(
    http_client, gerente, negocio, headers_autenticado
):
    # owner comun
    r = await _reporte(http_client, headers_autenticado)
    assert r.status_code == 403
    assert r.json()["detail"] == "Esta acción requiere nivel gerencia"

    # member de un negocio
    uid, _ = await _crear_portal_user(negocio["tenant_id"], "member")
    h_member = {"Authorization": f"Bearer {crear_access_token(uid, negocio['tenant_id'], 'member')}"}
    r = await _reporte(http_client, h_member)
    assert r.status_code == 403
    assert r.json()["detail"] == "Esta acción requiere nivel gerencia"

    # "ver como" (R36)
    token = (await _impersonar(http_client, gerente, negocio["tenant_id"])).json()["access_token"]
    r = await _reporte(http_client, {"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert r.json()["detail"] == "Esta acción requiere nivel gerencia"

    # sin token (R37)
    r = await _reporte(http_client, {})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_reporte_es_solo_lectura(http_client, gerente):
    for metodo in ("post", "put", "patch", "delete"):
        r = await getattr(http_client, metodo)(RUTA_CANCELACIONES, headers=gerente["headers"])
        assert r.status_code == 405, metodo


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"desde": "2030-01-01", "hasta": "2031-01-02"},  # 367 dias
        {"desde": "2031-02-02", "hasta": "2031-02-01"},  # desde > hasta
        {"granularidad": "semana"},
        {"desde": "no-es-fecha"},
        {"hasta": "2031-13-45"},
    ],
)
async def test_reporte_422_por_parametros_invalidos(http_client, gerente, params):
    r = await _reporte(http_client, gerente["headers"], **params)
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_reporte_rango_de_366_dias_es_valido(http_client, gerente):
    r = await _reporte(http_client, gerente["headers"], desde="2030-01-01", hasta="2031-01-01")
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_reporte_sin_rango_usa_los_ultimos_30_dias(http_client, gerente):
    n = await _negocio_con_citas(zona="UTC")
    try:
        ahora = _datetime.now(_timezone.utc)
        await _cancelada(n, ahora - _timedelta(days=2), "cliente")
        await _cancelada(n, ahora - _timedelta(days=40), "cliente")  # fuera de los 30 dias
        r = await _reporte(http_client, gerente["headers"], tenant_id=str(n["tid"]))
        assert r.status_code == 200
        assert [f["total_cliente"] for f in r.json()] == [1]
    finally:
        await _borrar_negocio_citas(n)


@pytest.mark.asyncio
async def test_reporte_vacio_es_200_con_lista_vacia(http_client, gerente):
    n = await _negocio_con_citas()
    try:
        await _cancelada(n, _utc(2031, 3, 10, 18), "cliente")
        base = {"desde": "2031-03-01", "hasta": "2031-03-31"}
        # periodo sin cancelaciones
        r = await _reporte(http_client, gerente["headers"], desde="2031-05-01", hasta="2031-05-31",
                           tenant_id=str(n["tid"]))
        assert (r.status_code, r.json()) == (200, [])
        # tenant_id y servicio_id inexistentes
        r = await _reporte(http_client, gerente["headers"], tenant_id=str(uuid4()), **base)
        assert (r.status_code, r.json()) == (200, [])
        r = await _reporte(http_client, gerente["headers"], servicio_id=str(uuid4()),
                           tenant_id=str(n["tid"]), **base)
        assert (r.status_code, r.json()) == (200, [])
    finally:
        await _borrar_negocio_citas(n)


@pytest.mark.asyncio
async def test_reporte_incluye_tenants_y_servicios_inactivos(http_client, gerente):
    n = await _negocio_con_citas()
    try:
        await _cancelada(n, _utc(2031, 3, 10, 18), "cliente", tardia=True)
        await execute("UPDATE tenant_servicios SET calendario_activo = false WHERE tenant_id = $1", n["tid"])
        await execute("UPDATE servicios SET activo = false WHERE id = $1", n["servicio"])
        await execute("UPDATE proveedores SET activo = false WHERE id = $1", n["proveedor"])
        r = await _reporte(http_client, gerente["headers"], desde="2031-03-01", hasta="2031-03-31",
                           tenant_id=str(n["tid"]))
        assert r.status_code == 200
        assert [(f["tardias"], f["total_cliente"]) for f in r.json()] == [(1, 1)]
    finally:
        await _borrar_negocio_citas(n)


@pytest.mark.asyncio
async def test_reporte_zona_por_tenant_y_borde_de_medianoche(http_client, gerente):
    mexico = await _negocio_con_citas("America/Mexico_City", "Mexico")  # UTC-6 en enero
    tokio = await _negocio_con_citas("Asia/Tokyo", "Tokio")  # UTC+9
    ids = (str(mexico["tid"]), str(tokio["tid"]))
    try:
        # 2031-01-01 05:59 UTC = 2030-12-31 23:59 en Mexico; 06:01 UTC = 2031-01-01 00:01
        await _cancelada(mexico, _utc(2031, 1, 1, 5, 59), "cliente")
        await _cancelada(mexico, _utc(2031, 1, 1, 6, 1), "cliente")
        # 14:59 UTC = 23:59 en Tokio del 1 de enero; 15:01 UTC = 2 de enero 00:01
        await _cancelada(tokio, _utc(2031, 1, 1, 14, 59), "cliente")
        await _cancelada(tokio, _utc(2031, 1, 1, 15, 1), "cliente")

        r = await _reporte(http_client, gerente["headers"], desde="2030-12-30", hasta="2031-01-03")
        filas = {(f["tenant_id"], f["periodo"]): f["total_cliente"] for f in r.json()
                 if f["tenant_id"] in ids}
        assert filas == {
            (ids[0], "2030-12-31"): 1,
            (ids[0], "2031-01-01"): 1,
            (ids[1], "2031-01-01"): 1,
            (ids[1], "2031-01-02"): 1,
        }

        # El rango tambien se interpreta en la zona de cada tenant (R40).
        r = await _reporte(http_client, gerente["headers"], desde="2030-12-31", hasta="2030-12-31")
        assert [(f["tenant_id"], f["total_cliente"]) for f in r.json()
                if f["tenant_id"] in ids] == [(ids[0], 1)]
        r = await _reporte(http_client, gerente["headers"], desde="2031-01-02", hasta="2031-01-02",
                           tenant_id=ids[1])
        assert [f["periodo"] for f in r.json()] == ["2031-01-02"]

        # Por mes, Mexico = 2 filas (dic y ene): una cancelacion nunca cuenta en dos periodos.
        r = await _reporte(http_client, gerente["headers"], desde="2030-12-01", hasta="2031-01-31",
                           granularidad="mes", tenant_id=ids[0])
        assert {f["periodo"]: f["total_cliente"] for f in r.json()} == {"2030-12": 1, "2031-01": 1}
    finally:
        await _borrar_negocio_citas(mexico, tokio)


@pytest.mark.asyncio
async def test_reporte_servicio_renombrado_una_fila_y_sin_servicio(http_client, gerente):
    n = await _negocio_con_citas()
    ajeno = await _negocio_con_citas(nombre="Otro")
    try:
        await _cancelada(n, _utc(2031, 3, 10, 18), "cliente")
        await execute("UPDATE servicios SET nombre = 'Corte premium' WHERE id = $1", n["servicio"])
        await _cancelada(n, _utc(2031, 3, 10, 19), "cliente")
        # Servicio que no es del tenant de la cita (o que ya no existe): cae en "Sin servicio".
        await _cancelada(n, _utc(2031, 3, 10, 20), "cliente", tardia=True, servicio=ajeno["servicio"])
        await _cancelada(n, _utc(2031, 3, 10, 21), "cliente", servicio=ajeno["servicio"])

        r = await _reporte(http_client, gerente["headers"], desde="2031-03-01", hasta="2031-03-31",
                           tenant_id=str(n["tid"]))
        filas = {(f["servicio_id"], f["servicio_nombre"]): (f["tardias"], f["total_cliente"])
                 for f in r.json()}
        assert filas == {
            (str(n["servicio"]), "Corte premium"): (0, 2),
            (None, "Sin servicio"): (1, 2),
        }

        # Filtrar por servicio_id deja solo ese servicio.
        r = await _reporte(http_client, gerente["headers"], desde="2031-03-01", hasta="2031-03-31",
                           tenant_id=str(n["tid"]), servicio_id=str(n["servicio"]))
        assert [f["servicio_nombre"] for f in r.json()] == ["Corte premium"]
    finally:
        await _borrar_negocio_citas(n, ajeno)


@pytest.mark.asyncio
async def test_reporte_tenant_sin_fila_de_servicios_usa_zona_por_defecto(http_client, gerente):
    n = await _negocio_con_citas(zona=None)
    try:
        # 2031-01-01 03:00 UTC = 2030-12-31 21:00 en Mexico (zona por defecto).
        await _cancelada(n, _utc(2031, 1, 1, 3), "cliente")
        r = await _reporte(http_client, gerente["headers"], desde="2030-12-31", hasta="2031-01-01",
                           tenant_id=str(n["tid"]))
        assert [f["periodo"] for f in r.json()] == ["2030-12-31"]
    finally:
        await _borrar_negocio_citas(n)
