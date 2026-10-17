"""
Planes y límites por situación comercial (spec 005a T4, RF-1, RF-9, RF-10, RF-11).

Cubre `limites_de_tenant` (services/limites_agente.py) y `cupo_numeros`
(services/acceso_plan.py) en sus cuatro situaciones, con base de datos:

  - prueba con plan asociado -> límites de ese plan, sin tope;
  - prueba sin plan -> sin límites;
  - suscripción vigente (aunque el plan esté desactivado) -> su plan;
  - sin suscripción -> plan más bajo activo;
  - sin ningún plan activo -> sin límites ni tope.
"""

from uuid import uuid4

import pytest

from services.acceso_plan import cupo_numeros
from services.limites_agente import MODELOS_AGENTE, limites_de_tenant
from session import execute, fetch_one

HAIKU, SONNET, OPUS = MODELOS_AGENTE


@pytest.fixture
async def plan_limitado(db):
    nombre = f"plan-t4-{uuid4().hex[:12]}"
    await execute(
        """
        INSERT INTO planes (nombre, precio_monthly, modelos_permitidos, historial_maximo,
                            max_numeros_whatsapp, activo)
        VALUES ($1, 10, ARRAY[$2, $3], 60, 1, true)
        """,
        nombre,
        HAIKU,
        SONNET,
    )
    await execute(
        "INSERT INTO plan_herramientas (plan_nombre, familia) VALUES ($1, 'http')", nombre
    )
    yield nombre
    await execute("DELETE FROM tenant_subscriptions WHERE plan = $1", nombre)
    await execute("DELETE FROM plan_herramientas WHERE plan_nombre = $1", nombre)
    await execute("DELETE FROM planes WHERE nombre = $1", nombre)


async def _suscribir(tid, plan, origen="pago", estado="activa"):
    await execute(
        """
        INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly, origen)
        VALUES ($1, $2, $3, 10, $4)
        ON CONFLICT (tenant_id) DO UPDATE SET plan = EXCLUDED.plan,
                                              estado = EXCLUDED.estado,
                                              origen = EXCLUDED.origen
        """,
        tid,
        plan,
        estado,
        origen,
    )


async def _piloto(tid):
    await execute(
        """
        INSERT INTO tenant_estado_plataforma (tenant_id, estado) VALUES ($1, 'prueba')
        ON CONFLICT (tenant_id) DO UPDATE SET estado = 'prueba'
        """,
        tid,
    )


@pytest.mark.asyncio
async def test_prueba_con_plan_usa_sus_limites_sin_tope(tenant_y_usuario, plan_limitado):
    tid = tenant_y_usuario["tenant_id"]
    await _suscribir(tid, plan_limitado, origen="prueba")

    lim = await limites_de_tenant(tid)

    assert lim.plan == plan_limitado
    assert lim.modelo_cabe(HAIKU) and not lim.modelo_cabe(OPUS)
    assert lim.tope_historial() == 60

    cupo = await cupo_numeros(tid)
    assert cupo.maximo is None


@pytest.mark.asyncio
async def test_prueba_sin_plan_no_tiene_limites(tenant_y_usuario):
    tid = tenant_y_usuario["tenant_id"]
    await _piloto(tid)

    lim = await limites_de_tenant(tid)

    assert lim.plan is None
    assert lim.modelo_cabe(OPUS)
    assert lim.tope_historial() == 200


@pytest.mark.asyncio
async def test_suscripcion_vigente_usa_su_plan_aunque_este_desactivado(
    tenant_y_usuario, plan_limitado
):
    tid = tenant_y_usuario["tenant_id"]
    await _suscribir(tid, plan_limitado, origen="pago")
    await execute("UPDATE planes SET activo = false WHERE nombre = $1", plan_limitado)
    try:
        lim = await limites_de_tenant(tid)
        assert lim.plan == plan_limitado
        assert lim.tope_historial() == 60
    finally:
        await execute("UPDATE planes SET activo = true WHERE nombre = $1", plan_limitado)


@pytest.mark.asyncio
async def test_sin_suscripcion_usa_el_plan_mas_bajo(tenant_y_usuario):
    tid = tenant_y_usuario["tenant_id"]

    lim = await limites_de_tenant(tid)

    bajo = await fetch_one(
        """
        SELECT nombre FROM planes WHERE activo ORDER BY precio_monthly,
            (max_numeros_whatsapp IS NULL), COALESCE(max_numeros_whatsapp, 0),
            nombre COLLATE "C" LIMIT 1
        """
    )
    assert bajo is not None
    assert lim.plan == bajo["nombre"]

    cupo = await cupo_numeros(tid)
    fila = await fetch_one("SELECT max_numeros_whatsapp FROM planes WHERE nombre = $1", bajo["nombre"])
    assert cupo.maximo == fila["max_numeros_whatsapp"]
    assert cupo.activos == 0 and cupo.disponible


@pytest.mark.asyncio
async def test_sin_ningun_plan_activo_no_hay_limites_ni_tope(tenant_y_usuario):
    tid = tenant_y_usuario["tenant_id"]
    estados = await execute("SELECT 1")  # mantiene el pool antes del cambio masivo
    del estados
    from session import fetch_all as _fetch_all

    filas = await _fetch_all("SELECT nombre, activo FROM planes")
    await execute("UPDATE planes SET activo = false")
    try:
        lim = await limites_de_tenant(tid)
        assert lim.plan is None
        assert lim.tope_historial() == 200

        cupo = await cupo_numeros(tid)
        assert cupo.maximo is None
    finally:
        for f in filas:
            await execute("UPDATE planes SET activo = $2 WHERE nombre = $1", f["nombre"], f["activo"])


@pytest.mark.asyncio
async def test_cupo_cuenta_solo_activos_sin_linea_de_avisos(tenant_y_usuario, plan_limitado):
    tid = tenant_y_usuario["tenant_id"]
    await _suscribir(tid, plan_limitado, origen="pago")
    await execute(
        "INSERT INTO agentes_canal (tenant_id, phone_number_id, estado) VALUES ($1, $2, 'activo')",
        tid,
        f"pn-{uuid4().hex[:8]}",
    )
    await execute(
        "INSERT INTO agentes_canal (tenant_id, phone_number_id, estado, es_linea_avisos) "
        "VALUES ($1, $2, 'activo', true)",
        tid,
        f"pn-{uuid4().hex[:8]}",
    )
    await execute(
        "INSERT INTO agentes_canal (tenant_id, phone_number_id, estado) VALUES ($1, $2, 'pausado')",
        tid,
        f"pn-{uuid4().hex[:8]}",
    )

    cupo = await cupo_numeros(tid)

    assert cupo.plan == plan_limitado
    assert cupo.maximo == 1
    assert cupo.activos == 1
    assert not cupo.disponible
