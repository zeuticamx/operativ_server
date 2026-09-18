"""
Tests de services/acceso_pagos.py: la regla es "bloquea solo si le faltan
las DOS cosas a la vez" (créditos y suscripción activa), y un tenant que
nunca pagó nada no se bloquea por eso.
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from services.acceso_pagos import acceso_pagos
from session import execute


@pytest.fixture
async def tenant_id(db):
    """Un tenant desnudo (sin portal_user): acá no hace falta iniciar sesión."""
    tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Test')", tid)
    yield tid
    await execute("DELETE FROM tenants WHERE id = $1", tid)


async def _con_suscripcion(tenant_id, estado: str, precio=Decimal("99.99")) -> None:
    await execute(
        """
        INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly)
        VALUES ($1, 'pro', $2, $3)
        """,
        tenant_id,
        estado,
        precio,
    )


async def _con_creditos(tenant_id, cantidad: Decimal) -> None:
    await execute(
        "INSERT INTO tenant_credits (tenant_id, creditos_disponibles) VALUES ($1, $2)",
        tenant_id,
        cantidad,
    )


@pytest.mark.asyncio
async def test_tenant_que_nunca_pago_nada_no_se_bloquea(db, tenant_id):
    """
    Sin fila en tenant_subscriptions ni en tenant_credits: es un tenant que
    nunca tocó /api/pagos. Exigirle pago desde el día uno no es una decisión
    que se haya tomado, así que no se bloquea.
    """
    acceso = await acceso_pagos(tenant_id)
    assert acceso.permitido is True


@pytest.mark.asyncio
async def test_suscripcion_activa_alcanza_aunque_no_tenga_creditos(db, tenant_id):
    await _con_suscripcion(tenant_id, "activa")

    acceso = await acceso_pagos(tenant_id)
    assert acceso.permitido is True
    assert acceso.suscripcion_activa is True
    assert acceso.tiene_creditos is False


@pytest.mark.asyncio
async def test_creditos_disponibles_alcanzan_aunque_la_suscripcion_este_pausada(db, tenant_id):
    await _con_suscripcion(tenant_id, "pausada")
    await _con_creditos(tenant_id, Decimal(100))

    acceso = await acceso_pagos(tenant_id)
    assert acceso.permitido is True
    assert acceso.tiene_creditos is True


@pytest.mark.asyncio
async def test_bloquea_si_le_faltan_las_dos_cosas(db, tenant_id):
    await _con_suscripcion(tenant_id, "pausada")
    await _con_creditos(tenant_id, Decimal(0))

    acceso = await acceso_pagos(tenant_id)
    assert acceso.permitido is False


@pytest.mark.asyncio
async def test_suscripcion_cancelada_sin_creditos_bloquea(db, tenant_id):
    await _con_suscripcion(tenant_id, "cancelada")

    acceso = await acceso_pagos(tenant_id)
    assert acceso.permitido is False


@pytest.mark.asyncio
async def test_gasto_todos_los_creditos_sin_suscripcion_bloquea(db, tenant_id):
    """
    Compró créditos alguna vez, nunca se suscribió, y ya se los gastó: es
    el mismo caso que 'se le venció y no renovó', solo que del lado de los
    créditos en vez de la suscripción.
    """
    await _con_creditos(tenant_id, Decimal(0))

    acceso = await acceso_pagos(tenant_id)
    assert acceso.permitido is False


@pytest.mark.asyncio
async def test_un_tenant_inexistente_no_se_bloquea(db):
    """No es este módulo el que decide qué hacer con un tenant que no existe."""
    acceso = await acceso_pagos(uuid4())
    assert acceso.permitido is True
