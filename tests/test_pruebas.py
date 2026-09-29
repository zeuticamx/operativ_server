"""
Plan de prueba otorgado por gerencia (services/pruebas.py).

En orden:
1. El cálculo de la expiración (puro, con `ahora` fijo): días, semanas,
   fecha límite y el tope de 3 meses calendario.
2. La puerta: solo gerencia de plataforma otorga o revoca.
3. La asignación: qué queda escrito y qué no se deja pisar.
4. El acceso durante la prueba y su corte al vencer — al vuelo en el gate
   del portal y persistido por el job.
5. La revocación manual.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from jobs.pagos_background import job_pausar_suscripciones_vencidas
from security import crear_access_token, hash_password
from services.acceso_pagos import acceso_pagos
from services.acceso_plan import acceso_plan
from services.pagos import activar_suscripcion
from services.pruebas import calcular_expiracion, sumar_meses
from session import execute, fetch_one, fetch_value

AHORA = datetime(2026, 1, 31, 15, 0, tzinfo=timezone.utc)


# ============================================================
# 1. Cálculo de la expiración
# ============================================================
def test_por_dias():
    assert calcular_expiracion("dias", 10, None, AHORA) == AHORA + timedelta(days=10)


def test_por_semanas():
    assert calcular_expiracion("semanas", 2, None, AHORA) == AHORA + timedelta(days=14)


def test_por_fecha_limite_se_pasa_a_utc():
    mexico = timezone(timedelta(hours=-6))
    fecha = datetime(2026, 2, 15, 23, 59, tzinfo=mexico)

    expiracion = calcular_expiracion("fecha", None, fecha, AHORA)

    assert expiracion == datetime(2026, 2, 16, 5, 59, tzinfo=timezone.utc)
    assert expiracion.tzinfo == timezone.utc


def test_fecha_en_el_pasado_da_400():
    with pytest.raises(HTTPException) as e:
        calcular_expiracion("fecha", None, AHORA - timedelta(minutes=1), AHORA)
    assert e.value.status_code == 400


def test_fecha_sin_zona_horaria_da_400():
    with pytest.raises(HTTPException) as e:
        calcular_expiracion("fecha", None, datetime(2026, 2, 15), AHORA)
    assert e.value.status_code == 400


def test_tres_meses_calendario_exactos_es_el_tope():
    # 31 ene + 3 meses = 30 abr (abril no tiene 31).
    limite = datetime(2026, 4, 30, 15, 0, tzinfo=timezone.utc)
    assert calcular_expiracion("fecha", None, limite, AHORA) == limite

    with pytest.raises(HTTPException) as e:
        calcular_expiracion("fecha", None, limite + timedelta(seconds=1), AHORA)
    assert e.value.status_code == 400


def test_mas_de_tres_meses_por_dias_da_400():
    # 31 ene -> 30 abr son 89 días.
    assert calcular_expiracion("dias", 89, None, AHORA)
    with pytest.raises(HTTPException):
        calcular_expiracion("dias", 90, None, AHORA)


@pytest.mark.parametrize(
    "fecha, meses, esperada",
    [
        (datetime(2026, 1, 31), 1, datetime(2026, 2, 28)),
        (datetime(2028, 1, 31), 1, datetime(2028, 2, 29)),  # bisiesto
        (datetime(2026, 11, 15), 3, datetime(2027, 2, 15)),  # cambio de año
        (datetime(2026, 3, 10), 3, datetime(2026, 6, 10)),
    ],
)
def test_sumar_meses(fecha, meses, esperada):
    assert sumar_meses(fecha, meses) == esperada


# ============================================================
# Fixtures
# ============================================================
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


@pytest.fixture
def auth(tenant_y_usuario):
    return {"Authorization": f"Bearer {tenant_y_usuario['token']}"}


@pytest.fixture
async def plan_retirado(db):
    nombre = f"retirado-{uuid4().hex[:10]}"
    await execute(
        "INSERT INTO planes (nombre, precio_monthly, activo, orden) VALUES ($1, 10, false, 99)",
        nombre,
    )
    yield nombre
    await execute("DELETE FROM planes WHERE nombre = $1", nombre)


def _ruta(tenant_id) -> str:
    return f"/api/gerencia/tenants/{tenant_id}/prueba"


def _cuerpo(plan: str = "pro", **duracion) -> dict:
    return {"plan": plan, "motivo": "Demo comercial", **(duracion or {"unidad": "dias", "cantidad": 14})}


async def _suscripcion(tenant_id) -> dict:
    return await fetch_one(
        """
        SELECT plan, estado, origen, otorgada_por, precio_monthly,
               fecha_inicio, fecha_renovacion
        FROM tenant_subscriptions WHERE tenant_id = $1
        """,
        tenant_id,
    )


async def _vencer(tenant_id) -> None:
    """Simula el paso del tiempo: la prueba terminó hace un minuto."""
    await execute(
        """
        UPDATE tenant_subscriptions
           SET fecha_renovacion = NOW() - INTERVAL '1 minute'
         WHERE tenant_id = $1
        """,
        tenant_id,
    )


# ============================================================
# 2. La puerta
# ============================================================
@pytest.mark.asyncio
async def test_sin_token_no_otorga(http_client, tenant_y_usuario):
    r = await http_client.post(_ruta(tenant_y_usuario["tenant_id"]), json=_cuerpo())
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_el_dueno_de_un_negocio_no_se_otorga_una_prueba(http_client, tenant_y_usuario, auth):
    """Un owner con JWT válido es dueño de SU negocio, no de la plataforma."""
    tenant_id = tenant_y_usuario["tenant_id"]

    r = await http_client.post(_ruta(tenant_id), json=_cuerpo(), headers=auth)
    revocar = await http_client.post(
        f"{_ruta(tenant_id)}/revocar", json={"motivo": "intento"}, headers=auth
    )

    assert r.status_code == 403
    assert revocar.status_code == 403
    assert await _suscripcion(tenant_id) is None


# ============================================================
# 3. Asignación
# ============================================================
@pytest.mark.asyncio
async def test_otorga_la_prueba_y_deja_todo_escrito(http_client, gerente, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    antes = datetime.now(timezone.utc)

    r = await http_client.post(
        _ruta(tenant_id),
        json=_cuerpo("pro", unidad="semanas", cantidad=2),
        headers=gerente["headers"],
    )

    assert r.status_code == 200, r.text
    ficha = r.json()
    assert ficha["plan"] == "pro"
    assert ficha["estado_suscripcion"] == "activa"
    assert ficha["origen_suscripcion"] == "prueba"

    s = await _suscripcion(tenant_id)
    assert s["origen"] == "prueba"
    assert s["otorgada_por"] == gerente["email"]
    # Una prueba no es ingreso: no puede inflar el MRR de gerencia.
    assert s["precio_monthly"] == 0
    esperada = antes + timedelta(weeks=2)
    assert abs(s["fecha_renovacion"] - esperada) < timedelta(minutes=1)

    servicios = await fetch_one(
        "SELECT agente_ia_activo, gestion_vendedores_activo FROM tenant_servicios WHERE tenant_id = $1",
        tenant_id,
    )
    assert servicios["agente_ia_activo"] is True

    auditoria = await fetch_one(
        """
        SELECT detalle FROM gerencia_auditoria
        WHERE tenant_id = $1 AND accion = 'prueba_otorgada'
        """,
        tenant_id,
    )
    assert auditoria is not None


@pytest.mark.asyncio
async def test_otorga_hasta_una_fecha_limite(http_client, gerente, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    limite = (datetime.now(timezone.utc) + timedelta(days=20)).replace(microsecond=0)

    r = await http_client.post(
        _ruta(tenant_id),
        json=_cuerpo("starter", unidad="fecha", fecha_expiracion=limite.isoformat()),
        headers=gerente["headers"],
    )

    assert r.status_code == 200, r.text
    assert (await _suscripcion(tenant_id))["fecha_renovacion"] == limite


@pytest.mark.asyncio
async def test_mas_de_tres_meses_da_400(http_client, gerente, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    lejos = datetime.now(timezone.utc) + timedelta(days=100)

    por_fecha = await http_client.post(
        _ruta(tenant_id),
        json=_cuerpo(unidad="fecha", fecha_expiracion=lejos.isoformat()),
        headers=gerente["headers"],
    )
    por_semanas = await http_client.post(
        _ruta(tenant_id), json=_cuerpo(unidad="semanas", cantidad=14), headers=gerente["headers"]
    )

    assert por_fecha.status_code == 400
    assert por_semanas.status_code == 422
    assert await _suscripcion(tenant_id) is None


@pytest.mark.asyncio
async def test_plan_inexistente_da_404(http_client, gerente, tenant_y_usuario):
    r = await http_client.post(
        _ruta(tenant_y_usuario["tenant_id"]),
        json=_cuerpo("plan-que-no-existe"),
        headers=gerente["headers"],
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_plan_retirado_no_se_otorga(http_client, gerente, tenant_y_usuario, plan_retirado):
    r = await http_client.post(
        _ruta(tenant_y_usuario["tenant_id"]),
        json=_cuerpo(plan_retirado),
        headers=gerente["headers"],
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_negocio_inexistente_da_404(http_client, gerente):
    r = await http_client.post(_ruta(uuid4()), json=_cuerpo(), headers=gerente["headers"])
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_no_pisa_un_plan_pagado_vigente(http_client, gerente, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    await activar_suscripcion(tenant_id, "enterprise")

    r = await http_client.post(_ruta(tenant_id), json=_cuerpo("pro"), headers=gerente["headers"])

    assert r.status_code == 409
    s = await _suscripcion(tenant_id)
    assert (s["plan"], s["origen"]) == ("enterprise", "pago")


@pytest.mark.asyncio
async def test_si_reemplaza_un_plan_pagado_vencido(http_client, gerente, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    await activar_suscripcion(tenant_id, "starter")
    await execute(
        "UPDATE tenant_subscriptions SET estado = 'pausada' WHERE tenant_id = $1", tenant_id
    )

    r = await http_client.post(_ruta(tenant_id), json=_cuerpo("pro"), headers=gerente["headers"])

    assert r.status_code == 200, r.text
    s = await _suscripcion(tenant_id)
    assert (s["plan"], s["estado"], s["origen"]) == ("pro", "activa", "prueba")


@pytest.mark.asyncio
async def test_una_prueba_se_puede_extender(http_client, gerente, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    headers = gerente["headers"]
    await http_client.post(_ruta(tenant_id), json=_cuerpo(unidad="dias", cantidad=3), headers=headers)

    r = await http_client.post(
        _ruta(tenant_id), json=_cuerpo(unidad="dias", cantidad=30), headers=headers
    )

    assert r.status_code == 200
    restante = (await _suscripcion(tenant_id))["fecha_renovacion"] - datetime.now(timezone.utc)
    assert restante > timedelta(days=29)


@pytest.mark.asyncio
async def test_pagar_durante_la_prueba_la_convierte_en_plan_pagado(
    http_client, gerente, tenant_y_usuario
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await http_client.post(_ruta(tenant_id), json=_cuerpo("pro"), headers=gerente["headers"])

    await activar_suscripcion(tenant_id, "pro")

    s = await _suscripcion(tenant_id)
    assert s["origen"] == "pago"
    assert s["otorgada_por"] is None
    assert s["precio_monthly"] > 0


# ============================================================
# 4. Acceso durante la prueba y al vencer
# ============================================================
@pytest.mark.asyncio
async def test_durante_la_prueba_usa_las_herramientas_del_plan(
    http_client, gerente, tenant_y_usuario, auth
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await http_client.post(_ruta(tenant_id), json=_cuerpo("pro"), headers=gerente["headers"])

    assert (await http_client.get("/api/herramientas", headers=auth)).status_code == 200
    assert (await acceso_pagos(tenant_id)).permitido is True


@pytest.mark.asyncio
async def test_la_prueba_solo_da_lo_que_incluye_su_plan(
    http_client, gerente, tenant_y_usuario, auth
):
    await http_client.post(
        _ruta(tenant_y_usuario["tenant_id"]), json=_cuerpo("starter"), headers=gerente["headers"]
    )

    r = await http_client.get("/api/herramientas", headers=auth)

    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "plan_insuficiente"


@pytest.mark.asyncio
async def test_al_vencer_el_portal_corta_al_instante_sin_esperar_al_job(
    http_client, gerente, tenant_y_usuario, auth
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await http_client.post(_ruta(tenant_id), json=_cuerpo("pro"), headers=gerente["headers"])
    await _vencer(tenant_id)

    # En la base sigue 'activa' (el job todavía no pasó)...
    assert (await _suscripcion(tenant_id))["estado"] == "activa"
    # ...pero el gate ya la lee vencida.
    assert (await acceso_plan(tenant_id)).estado == "vencido"
    r = await http_client.get("/api/herramientas", headers=auth)
    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "plan_requerido"


@pytest.mark.asyncio
async def test_al_vencer_el_job_la_pausa_y_se_aplican_las_restricciones_sin_plan(
    http_client, gerente, tenant_y_usuario, auth
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await http_client.post(_ruta(tenant_id), json=_cuerpo("pro"), headers=gerente["headers"])
    await _vencer(tenant_id)

    await job_pausar_suscripciones_vencidas()

    assert (await _suscripcion(tenant_id))["estado"] == "pausada"
    assert (await http_client.get("/api/canales", headers=auth)).status_code == 402
    # Sin plan y sin créditos, el agente de n8n también se apaga.
    assert (await acceso_pagos(tenant_id)).permitido is False


# ============================================================
# 4b. Módulo de calendario: el plan da el derecho, el interruptor lo prende
# ============================================================
async def _calendario_encendido(tenant_id) -> bool:
    fila = await fetch_one(
        "SELECT calendario_activo FROM tenant_servicios WHERE tenant_id = $1", tenant_id
    )
    return bool(fila and fila["calendario_activo"])


@pytest.mark.asyncio
async def test_una_prueba_con_calendario_deja_el_modulo_encendido(
    http_client, gerente, tenant_y_usuario, auth
):
    """Sin el interruptor el agente recibe 409 aunque el plan incluya calendario."""
    tenant_id = tenant_y_usuario["tenant_id"]
    assert await _calendario_encendido(tenant_id) is False

    r = await http_client.post(_ruta(tenant_id), json=_cuerpo("pro"), headers=gerente["headers"])

    assert r.status_code == 200, r.text
    assert r.json()["calendario_activo"] is True
    assert await _calendario_encendido(tenant_id) is True
    ruta = f"/api/tenants/{tenant_id}/calendario/proveedores"
    assert (await http_client.get(ruta, headers=auth)).status_code == 200
    # Y el agente ya tiene sus herramientas para consultar y reservar.
    herramientas = await fetch_value(
        "SELECT COUNT(*) FROM tenant_tools WHERE tenant_id = $1 AND is_enabled", tenant_id
    )
    assert herramientas == 5


@pytest.mark.asyncio
async def test_una_prueba_sin_calendario_no_lo_enciende(http_client, gerente, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]

    r = await http_client.post(_ruta(tenant_id), json=_cuerpo("starter"), headers=gerente["headers"])

    assert r.status_code == 200, r.text
    assert await _calendario_encendido(tenant_id) is False


@pytest.mark.asyncio
async def test_gerencia_enciende_y_apaga_el_calendario_a_mano(
    http_client, gerente, tenant_y_usuario
):
    tenant_id = tenant_y_usuario["tenant_id"]
    ruta = f"/api/gerencia/tenants/{tenant_id}/servicios"

    encender = await http_client.patch(
        ruta, json={"calendario_activo": True, "motivo": "Alta"}, headers=gerente["headers"]
    )
    assert encender.status_code == 200, encender.text
    assert encender.json()["calendario_activo"] is True
    assert await _calendario_encendido(tenant_id) is True

    apagar = await http_client.patch(
        ruta, json={"calendario_activo": False}, headers=gerente["headers"]
    )
    assert apagar.json()["calendario_activo"] is False
    # Apagarlo pausa las herramientas del agente.
    assert await fetch_value(
        "SELECT COUNT(*) FROM tenant_tools WHERE tenant_id = $1 AND is_enabled", tenant_id
    ) == 0
    # Tocar un interruptor no mueve los otros.
    assert apagar.json()["agente_ia_activo"] == encender.json()["agente_ia_activo"]


@pytest.mark.asyncio
async def test_el_dueno_no_usa_el_endpoint_de_servicios_de_gerencia(
    http_client, tenant_y_usuario, auth
):
    r = await http_client.patch(
        f"/api/gerencia/tenants/{tenant_y_usuario['tenant_id']}/servicios",
        json={"calendario_activo": True},
        headers=auth,
    )
    assert r.status_code == 403


# ============================================================
# 5. Revocación
# ============================================================
@pytest.mark.asyncio
async def test_revocar_corta_la_prueba_ya(http_client, gerente, tenant_y_usuario, auth):
    tenant_id = tenant_y_usuario["tenant_id"]
    await http_client.post(_ruta(tenant_id), json=_cuerpo("pro"), headers=gerente["headers"])

    r = await http_client.post(
        f"{_ruta(tenant_id)}/revocar",
        json={"motivo": "Terminó la demo"},
        headers=gerente["headers"],
    )

    assert r.status_code == 200, r.text
    assert r.json()["estado_suscripcion"] == "cancelada"
    assert (await http_client.get("/api/herramientas", headers=auth)).status_code == 402
    # Queda la fila (cancelada): sin ella el negocio contaría como "nunca
    # pagó" y acceso_pagos dejaría al agente contestando.
    assert (await acceso_pagos(tenant_id)).permitido is False

    auditoria = await fetch_one(
        "SELECT 1 FROM gerencia_auditoria WHERE tenant_id = $1 AND accion = 'prueba_revocada'",
        tenant_id,
    )
    assert auditoria is not None


@pytest.mark.asyncio
async def test_no_revoca_un_plan_pagado(http_client, gerente, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    await activar_suscripcion(tenant_id, "pro")

    r = await http_client.post(
        f"{_ruta(tenant_id)}/revocar", json={"motivo": "error"}, headers=gerente["headers"]
    )

    assert r.status_code == 409
    assert (await _suscripcion(tenant_id))["estado"] == "activa"


@pytest.mark.asyncio
async def test_revocar_sin_prueba_da_409(http_client, gerente, tenant_y_usuario):
    r = await http_client.post(
        f"{_ruta(tenant_y_usuario['tenant_id'])}/revocar",
        json={"motivo": "nada"},
        headers=gerente["headers"],
    )
    assert r.status_code == 409
