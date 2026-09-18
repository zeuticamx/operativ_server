"""
Tests de jobs/pagos_background.py: el job que pasa una suscripción vencida
de 'activa' a 'pausada'. Es lo único que hace que
services/acceso_pagos.py tenga un `estado` correcto para leer.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from jobs.pagos_background import job_pausar_suscripciones_vencidas
from session import execute, fetch_value


@pytest.fixture
async def tenant_id(db):
    tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Test')", tid)
    yield tid
    await execute("DELETE FROM tenants WHERE id = $1", tid)


async def _suscripcion(tenant_id, estado: str, fecha_renovacion) -> None:
    await execute(
        """
        INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly, fecha_renovacion)
        VALUES ($1, 'pro', $2, 99.99, $3)
        """,
        tenant_id,
        estado,
        fecha_renovacion,
    )


async def _estado_de(tenant_id) -> str:
    return await fetch_value(
        "SELECT estado FROM tenant_subscriptions WHERE tenant_id = $1", tenant_id
    )


@pytest.mark.asyncio
async def test_pausa_una_suscripcion_activa_ya_vencida(db, tenant_id):
    ayer = datetime.now(timezone.utc) - timedelta(days=1)
    await _suscripcion(tenant_id, "activa", ayer)

    await job_pausar_suscripciones_vencidas()

    assert await _estado_de(tenant_id) == "pausada"


@pytest.mark.asyncio
async def test_no_toca_una_suscripcion_activa_que_todavia_no_vence(db, tenant_id):
    en_20_dias = datetime.now(timezone.utc) + timedelta(days=20)
    await _suscripcion(tenant_id, "activa", en_20_dias)

    await job_pausar_suscripciones_vencidas()

    assert await _estado_de(tenant_id) == "activa"


@pytest.mark.asyncio
async def test_no_toca_una_ya_pausada(db, tenant_id):
    """Idempotente: no hay nada que 'volver a pausar'."""
    ayer = datetime.now(timezone.utc) - timedelta(days=1)
    await _suscripcion(tenant_id, "pausada", ayer)

    await job_pausar_suscripciones_vencidas()

    assert await _estado_de(tenant_id) == "pausada"


@pytest.mark.asyncio
async def test_no_toca_una_cancelada_aunque_este_vencida(db, tenant_id):
    """Cancelada es una decisión explícita; el job no la reactiva ni la muta."""
    ayer = datetime.now(timezone.utc) - timedelta(days=1)
    await _suscripcion(tenant_id, "cancelada", ayer)

    await job_pausar_suscripciones_vencidas()

    assert await _estado_de(tenant_id) == "cancelada"


@pytest.mark.asyncio
async def test_no_toca_una_activa_sin_fecha_de_renovacion(db, tenant_id):
    """NULL no es 'ya venció': no hay nada contra qué comparar."""
    await _suscripcion(tenant_id, "activa", None)

    await job_pausar_suscripciones_vencidas()

    assert await _estado_de(tenant_id) == "activa"


@pytest.mark.asyncio
async def test_correrlo_dos_veces_seguidas_no_falla(db, tenant_id):
    ayer = datetime.now(timezone.utc) - timedelta(days=1)
    await _suscripcion(tenant_id, "activa", ayer)

    await job_pausar_suscripciones_vencidas()
    await job_pausar_suscripciones_vencidas()

    assert await _estado_de(tenant_id) == "pausada"


@pytest.mark.asyncio
async def test_pausar_bloquea_el_acceso_cuando_tampoco_hay_creditos(db, tenant_id):
    """De punta a punta: el job + acceso_pagos.py dejan al tenant bloqueado."""
    from services.acceso_pagos import acceso_pagos

    ayer = datetime.now(timezone.utc) - timedelta(days=1)
    await _suscripcion(tenant_id, "activa", ayer)
    await execute(
        "INSERT INTO tenant_credits (tenant_id, creditos_disponibles) VALUES ($1, 0)",
        tenant_id,
    )

    await job_pausar_suscripciones_vencidas()

    acceso = await acceso_pagos(tenant_id)
    assert acceso.permitido is False
