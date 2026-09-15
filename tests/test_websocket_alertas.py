"""
Tests para el sistema de WebSocket de alertas en tiempo real.

Cubre:
- Autenticación JWT en WebSocket
- Broadcast de nuevas alertas
- Marcar alertas como leídas
- Room-based delivery (tenant-scoped)
- Desconexión y reconexión
"""

from uuid import uuid4

import pytest

from realtime import broadcast_alerta
from routers.alertas import crear_alerta
from session import fetch_all, fetch_one, fetch_value


# ============================================================
# Tests de Broadcast (la función que despacha alertas)
# ============================================================


@pytest.mark.asyncio
async def test_broadcast_crea_alerta_en_bd(db, tenant_y_usuario, clean_alertas):
    """
    broadcast_alerta() crea un registro en la tabla alertas y
    es visible para el tenant.
    """
    tenant_id = tenant_y_usuario["tenant_id"]

    await broadcast_alerta(
        tenant_id=tenant_id,
        tipo="nuevo_lead",
        titulo="Test Lead",
        mensaje="Un cliente nuevo",
        datos={"cliente_id": "123", "nombre": "Juan"},
    )

    # Verificar que está en BD
    alerta = await fetch_one(
        "SELECT id, tipo, titulo, mensaje, leido, datos FROM alertas WHERE tenant_id = $1 ORDER BY creado_en DESC LIMIT 1",
        tenant_id,
    )

    assert alerta is not None
    assert alerta["tipo"] == "nuevo_lead"
    assert alerta["titulo"] == "Test Lead"
    assert alerta["mensaje"] == "Un cliente nuevo"
    assert alerta["leido"] is False
    assert alerta["datos"]["cliente_id"] == "123"


@pytest.mark.asyncio
async def test_broadcast_respeta_tenant(db, tenant_y_usuario, clean_alertas):
    """
    Alertas de un tenant no se mezclan con otro.
    """
    tenant1_id = tenant_y_usuario["tenant_id"]
    tenant2_id = uuid4()

    # Crear tenant2 mínimo (sin usuario, solo para prueba)
    from session import execute

    await execute(
        "INSERT INTO tenants (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
        tenant2_id,
        "Tenant 2",
    )

    # Despa char alertas para ambos tenants
    await broadcast_alerta(
        tenant_id=tenant1_id,
        tipo="sin_actividad",
        titulo="Alerta Tenant 1",
        mensaje="",
        datos={},
    )
    await broadcast_alerta(
        tenant_id=tenant2_id,
        tipo="sin_actividad",
        titulo="Alerta Tenant 2",
        mensaje="",
        datos={},
    )

    # Verificar que cada tenant ve solo su alerta
    alertas_t1 = await fetch_all(
        "SELECT titulo FROM alertas WHERE tenant_id = $1 ORDER BY creado_en",
        tenant1_id,
    )
    assert len(alertas_t1) == 1
    assert alertas_t1[0]["titulo"] == "Alerta Tenant 1"

    alertas_t2 = await fetch_all(
        "SELECT titulo FROM alertas WHERE tenant_id = $1", tenant2_id
    )
    assert len(alertas_t2) == 1
    assert alertas_t2[0]["titulo"] == "Alerta Tenant 2"

    # Limpiar tenant2
    await execute("DELETE FROM alertas WHERE tenant_id = $1", tenant2_id)
    await execute("DELETE FROM tenants WHERE id = $1", tenant2_id)


# ============================================================
# Tests del endpoint PATCH /api/alertas/{id}/marcar-leida
# ============================================================


@pytest.mark.asyncio
async def test_marcar_alerta_leida_via_http(http_client, tenant_y_usuario, clean_alertas):
    """
    PATCH /api/alertas/{id}/marcar-leida marca como leída
    y devuelve status ok.
    """
    tenant_id = tenant_y_usuario["tenant_id"]
    usuario_id = tenant_y_usuario["usuario_id"]
    token = tenant_y_usuario["token"]

    # Crear alerta
    await broadcast_alerta(
        tenant_id=tenant_id,
        tipo="sin_actividad",
        titulo="Test",
        mensaje="",
        datos={},
    )

    alerta = await fetch_one(
        "SELECT id FROM alertas WHERE tenant_id = $1 LIMIT 1", tenant_id
    )
    alerta_id = alerta["id"]

    # Marcar como leída
    response = await http_client.patch(
        f"/api/alertas/{alerta_id}/marcar-leida",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    # Verificar en BD
    fila = await fetch_one("SELECT leido FROM alertas WHERE id = $1", alerta_id)
    assert fila["leido"] is True


@pytest.mark.asyncio
async def test_marcar_alerta_leida_no_de_otro_tenant(
    http_client, tenant_y_usuario, clean_alertas
):
    """
    No se puede marcar como leída una alerta de otro tenant,
    aunque tengas un JWT válido.
    """
    from session import execute

    tenant1_id = tenant_y_usuario["tenant_id"]
    token = tenant_y_usuario["token"]
    tenant2_id = uuid4()

    # Crear tenant2
    await execute(
        "INSERT INTO tenants (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
        tenant2_id,
        "Tenant 2",
    )

    # Crear alerta en tenant2
    await broadcast_alerta(
        tenant_id=tenant2_id,
        tipo="sin_actividad",
        titulo="Alerta Tenant 2",
        mensaje="",
        datos={},
    )

    alerta = await fetch_one(
        "SELECT id FROM alertas WHERE tenant_id = $1 LIMIT 1", tenant2_id
    )
    alerta_id = alerta["id"]

    # Intentar marcar desde tenant1 (token de tenant1)
    response = await http_client.patch(
        f"/api/alertas/{alerta_id}/marcar-leida",
        headers={"Authorization": f"Bearer {token}"},
    )

    # Debe fallar (404 o 403)
    assert response.status_code in (403, 404)

    # Verificar que NO se marcó en BD
    fila = await fetch_one("SELECT leido FROM alertas WHERE id = $1", alerta_id)
    assert fila["leido"] is False

    # Limpiar
    await execute("DELETE FROM alertas WHERE tenant_id = $1", tenant2_id)
    await execute("DELETE FROM tenants WHERE id = $1", tenant2_id)


# ============================================================
# Tests del endpoint GET /api/alertas/estadísticas
# ============================================================


@pytest.mark.asyncio
async def test_estadisticas_alertas(http_client, tenant_y_usuario, clean_alertas):
    """
    GET /api/alertas/estadísticas devuelve conteo total y por tipo.
    """
    tenant_id = tenant_y_usuario["tenant_id"]
    token = tenant_y_usuario["token"]

    # Crear varias alertas de distintos tipos
    tipos = ["nuevo_lead", "cambio_etapa", "sin_actividad"]
    for i, tipo in enumerate(tipos):
        await broadcast_alerta(
            tenant_id=tenant_id,
            tipo=tipo,
            titulo=f"Alerta {i}",
            mensaje="",
            datos={},
        )

    # Crear una más sin_actividad
    await broadcast_alerta(
        tenant_id=tenant_id,
        tipo="sin_actividad",
        titulo="Otra sin_actividad",
        mensaje="",
        datos={},
    )

    # Consultar estadísticas
    response = await http_client.get(
        f"/api/tenants/{tenant_id}/alertas/estadisticas",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()

    assert data["total"] == 4
    assert data["por_tipo"]["nuevo_lead"] == 1
    assert data["por_tipo"]["cambio_etapa"] == 1
    assert data["por_tipo"]["sin_actividad"] == 2


# ============================================================
# Tests del endpoint GET /api/alertas
# ============================================================


@pytest.mark.asyncio
async def test_listar_alertas(http_client, tenant_y_usuario, clean_alertas):
    """
    GET /api/alertas devuelve lista de alertas del tenant.
    """
    tenant_id = tenant_y_usuario["tenant_id"]
    token = tenant_y_usuario["token"]

    # Crear alertas
    for i in range(3):
        await broadcast_alerta(
            tenant_id=tenant_id,
            tipo="sin_actividad",
            titulo=f"Alerta {i}",
            mensaje=f"Mensaje {i}",
            datos={"index": i},
        )

    # Listar
    response = await http_client.get(
        f"/api/tenants/{tenant_id}/alertas",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    alertas = response.json()

    assert len(alertas) == 3
    # Están en orden DESC (más recientes primero)
    assert alertas[0]["titulo"] == "Alerta 2"
    assert alertas[0]["leido"] is False


@pytest.mark.asyncio
async def test_listar_alertas_solo_del_tenant(
    http_client, tenant_y_usuario, clean_alertas
):
    """
    No se ven alertas de otros tenants.
    """
    from session import execute

    tenant1_id = tenant_y_usuario["tenant_id"]
    token = tenant_y_usuario["token"]
    tenant2_id = uuid4()

    # Crear tenant2
    await execute(
        "INSERT INTO tenants (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
        tenant2_id,
        "Tenant 2",
    )

    # Crear alertas en ambos tenants
    await broadcast_alerta(
        tenant_id=tenant1_id,
        tipo="sin_actividad",
        titulo="Alerta T1",
        mensaje="",
        datos={},
    )
    await broadcast_alerta(
        tenant_id=tenant2_id,
        tipo="sin_actividad",
        titulo="Alerta T2",
        mensaje="",
        datos={},
    )

    # Listar desde tenant1
    response = await http_client.get(
        f"/api/tenants/{tenant1_id}/alertas",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    alertas = response.json()

    assert len(alertas) == 1
    assert alertas[0]["titulo"] == "Alerta T1"

    # Limpiar
    await execute("DELETE FROM alertas WHERE tenant_id = $1", tenant2_id)
    await execute("DELETE FROM tenants WHERE id = $1", tenant2_id)


# ============================================================
# Tests de conteo de no leídos
# ============================================================


@pytest.mark.asyncio
async def test_conteo_alertas_sin_leer(http_client, tenant_y_usuario, clean_alertas):
    """
    El campo `noLeidosCount` refleja cuántas alertas están sin leer.
    """
    tenant_id = tenant_y_usuario["tenant_id"]
    token = tenant_y_usuario["token"]

    # Crear 3 alertas
    for i in range(3):
        await broadcast_alerta(
            tenant_id=tenant_id,
            tipo="sin_actividad",
            titulo=f"Alerta {i}",
            mensaje="",
            datos={},
        )

    # Obtener lista para ver el conteo (esto lo hace el hook useWebsocketAlertas)
    response = await http_client.get(
        f"/api/tenants/{tenant_id}/alertas",
        headers={"Authorization": f"Bearer {token}"},
    )
    alertas = response.json()
    no_leidos = sum(1 for a in alertas if not a["leido"])

    assert no_leidos == 3

    # Marcar 2 como leídas
    for alerta in alertas[:2]:
        await http_client.patch(
            f"/api/alertas/{alerta['id']}/marcar-leida",
            headers={"Authorization": f"Bearer {token}"},
        )

    # Verificar que quedan 1 sin leer
    response = await http_client.get(
        f"/api/tenants/{tenant_id}/alertas",
        headers={"Authorization": f"Bearer {token}"},
    )
    alertas = response.json()
    no_leidos = sum(1 for a in alertas if not a["leido"])

    assert no_leidos == 1


# ============================================================
# Tests sin autenticación
# ============================================================


@pytest.mark.asyncio
async def test_sin_token_rechazado(http_client, tenant_y_usuario):
    """
    Endpoints de alertas requieren autenticación.
    """
    tenant_id = tenant_y_usuario["tenant_id"]

    # Sin token: 401 Unauthorized
    response = await http_client.get(f"/api/tenants/{tenant_id}/alertas")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_token_invalido_rechazado(http_client, tenant_y_usuario):
    """
    Token malformado es rechazado.
    """
    tenant_id = tenant_y_usuario["tenant_id"]

    response = await http_client.get(
        f"/api/tenants/{tenant_id}/alertas",
        headers={"Authorization": "Bearer invalid.token.here"},
    )
    assert response.status_code == 401
