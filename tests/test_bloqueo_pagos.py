"""
Tests de integración HTTP del bloqueo por pagos sobre el CRM de vendedores.

_exigir_modulo (routers/vendedores.py) es el choke point que ya usaban
modulo_actual/modulo_en_ruta para el 409 de "módulo apagado"; ahí mismo se
sumó el 402 de "no se puede pagar". Cubre routers/vendedores.py Y
routers/pipeline_config.py, porque los dos importan la misma función.

Se prueba contra GET /api/tenants/{id}/pipeline: cualquier otro endpoint
gateado por modulo_actual/modulo_en_ruta pasa por el mismo camino.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services.pipeline import set_tenant_servicios
from session import execute

RUTA_PIPELINE = "/api/tenants/{tenant_id}/pipeline"


async def _encender_modulo(tenant_id) -> None:
    await set_tenant_servicios(tenant_id, agente_ia_activo=None, gestion_vendedores_activo=True)


async def _suscripcion(tenant_id, estado: str, fecha_renovacion=None) -> None:
    await execute(
        """
        INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly, fecha_renovacion)
        VALUES ($1, 'pro', $2, 99.99, $3)
        """,
        tenant_id,
        estado,
        fecha_renovacion,
    )


async def _creditos(tenant_id, cantidad: Decimal) -> None:
    await execute(
        "INSERT INTO tenant_credits (tenant_id, creditos_disponibles) VALUES ($1, $2)",
        tenant_id,
        cantidad,
    )


@pytest.mark.asyncio
async def test_sin_haber_pagado_nunca_el_crm_funciona(http_client, tenant_y_usuario):
    """Un tenant que nunca tocó /api/pagos no queda bloqueado de entrada."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_modulo(tenant_id)

    respuesta = await http_client.get(
        RUTA_PIPELINE.format(tenant_id=tenant_id),
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 200


@pytest.mark.asyncio
async def test_suscripcion_vencida_y_sin_creditos_bloquea_con_402(
    http_client, tenant_y_usuario
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_modulo(tenant_id)
    await _suscripcion(tenant_id, "pausada")
    await _creditos(tenant_id, Decimal(0))

    respuesta = await http_client.get(
        RUTA_PIPELINE.format(tenant_id=tenant_id),
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 402
    assert "créditos" in respuesta.json()["detail"] or "suscripción" in respuesta.json()["detail"]


@pytest.mark.asyncio
async def test_con_creditos_disponibles_no_bloquea_aunque_la_suscripcion_este_pausada(
    http_client, tenant_y_usuario
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_modulo(tenant_id)
    await _suscripcion(tenant_id, "pausada")
    await _creditos(tenant_id, Decimal(50))

    respuesta = await http_client.get(
        RUTA_PIPELINE.format(tenant_id=tenant_id),
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 200


@pytest.mark.asyncio
async def test_con_suscripcion_activa_no_bloquea_aunque_no_tenga_creditos(
    http_client, tenant_y_usuario
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_modulo(tenant_id)
    await _suscripcion(tenant_id, "activa")

    respuesta = await http_client.get(
        RUTA_PIPELINE.format(tenant_id=tenant_id),
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 200


@pytest.mark.asyncio
async def test_modulo_apagado_da_409_incluso_con_pagos_al_dia(
    http_client, tenant_y_usuario
):
    """El 409 (módulo apagado a propósito) no lo tapa el 402: son casos distintos."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _suscripcion(tenant_id, "activa")
    # Nunca se llama a _encender_modulo: el módulo queda apagado (default).

    respuesta = await http_client.get(
        RUTA_PIPELINE.format(tenant_id=tenant_id),
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 409


@pytest.mark.asyncio
async def test_pipeline_config_tambien_queda_cubierto_por_el_mismo_gate(
    http_client, tenant_y_usuario
):
    """pipeline_config.py importa _exigir_modulo del mismo sitio que vendedores.py."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_modulo(tenant_id)
    await _suscripcion(tenant_id, "cancelada")
    await _creditos(tenant_id, Decimal(0))

    respuesta = await http_client.get(
        f"/api/tenants/{tenant_id}/pipeline-config",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert respuesta.status_code == 402


@pytest.mark.asyncio
async def test_el_job_de_vencimiento_mas_el_gate_bloquean_de_punta_a_punta(
    http_client, tenant_y_usuario
):
    """
    Simula el paso del tiempo: una suscripción que vence, el job que la
    pasa a 'pausada', y recién ahí el 402.
    """
    from jobs.pagos_background import job_pausar_suscripciones_vencidas

    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_modulo(tenant_id)
    ayer = datetime.now(timezone.utc) - timedelta(days=1)
    await _suscripcion(tenant_id, "activa", ayer)
    await _creditos(tenant_id, Decimal(0))

    # Todavía no corrió el job: el estado en BD sigue diciendo 'activa'.
    antes = await http_client.get(
        RUTA_PIPELINE.format(tenant_id=tenant_id),
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert antes.status_code == 200

    await job_pausar_suscripciones_vencidas()

    despues = await http_client.get(
        RUTA_PIPELINE.format(tenant_id=tenant_id),
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert despues.status_code == 402
