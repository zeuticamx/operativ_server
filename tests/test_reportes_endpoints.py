"""
Tests del endpoint de reportes de actividad de campo.

GET /api/reportes/actividad es el único endpoint que expone routers/reportes.py
hoy (visitas y tareas completadas por vendedor, en un rango de fechas).
No existen /embudo, /tendencias, /comparativa-vendedores ni /conversion.

Se prueba vía HTTP (http_client + tenant_y_usuario), igual que
test_websocket_alertas.py: lo que interesa es el contrato completo
(auth -> permisos -> SQL -> forma de la respuesta), no solo la función.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from session import execute, fetch_value
from security import crear_access_token

RUTA = "/api/reportes/actividad"


# ============================================================
# Helpers de datos: vendedor, cliente, visita, tarea
# ============================================================
async def crear_vendedor(tenant_id, nombre="Ana", activo=True) -> str:
    return await fetch_value(
        """
        INSERT INTO vendedores (tenant_id, nombre, activo)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        tenant_id,
        nombre,
        activo,
    )


async def crear_cliente(tenant_id, vendedor_id) -> str:
    return await fetch_value(
        """
        INSERT INTO clientes
            (tenant_id, vendedor_id, nombre_negocio, latitud, longitud)
        VALUES ($1, $2, 'Abarrotes La Esquina', 19.432608, -99.133209)
        RETURNING id
        """,
        tenant_id,
        vendedor_id,
    )


async def crear_visita(
    tenant_id,
    vendedor_id,
    cliente_id,
    dentro=True,
    cuando: datetime | None = None,
) -> None:
    await execute(
        """
        INSERT INTO visitas
            (tenant_id, vendedor_id, cliente_id, latitud, longitud,
             distancia_calculada_metros, dentro_de_geocerca, timestamp_servidor)
        VALUES ($1, $2, $3, 19.432608, -99.133209, $4, $5, $6)
        """,
        tenant_id,
        vendedor_id,
        cliente_id,
        0.0 if dentro else 5000.0,
        dentro,
        cuando or datetime.now(timezone.utc),
    )


async def crear_tarea(
    tenant_id,
    vendedor_id,
    cliente_id,
    estado="pendiente",
    completado_en: datetime | None = None,
) -> None:
    await execute(
        """
        INSERT INTO tareas_seguimiento
            (tenant_id, vendedor_id, cliente_id, titulo, fecha_programada,
             estado, completado_en)
        VALUES ($1, $2, $3, 'Dar seguimiento', now(), $4, $5)
        """,
        tenant_id,
        vendedor_id,
        cliente_id,
        estado,
        completado_en,
    )


async def crear_usuario_member(tenant_id) -> str:
    """Cuenta del mismo tenant sin permisos de gerencia (role='member')."""
    from security import hash_password

    usuario_id = uuid4()
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, 'member', true)
        """,
        usuario_id,
        tenant_id,
        f"member-{usuario_id}@ejemplo.com",
        hash_password("test_password"),
    )
    return crear_access_token(usuario_id, tenant_id, "member")


# ============================================================
# Autenticación y permisos
# ============================================================
@pytest.mark.asyncio
async def test_actividad_sin_token_rechazado(http_client, tenant_y_usuario):
    response = await http_client.get(RUTA)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_actividad_token_invalido_rechazado(http_client, tenant_y_usuario):
    response = await http_client.get(
        RUTA, headers={"Authorization": "Bearer invalido.no.sirve"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_actividad_rechaza_a_quien_no_es_gerencia(http_client, tenant_y_usuario):
    """'member' ve el pipeline pero no reportes de gerencia."""
    token_member = await crear_usuario_member(tenant_y_usuario["tenant_id"])

    response = await http_client.get(
        RUTA, headers={"Authorization": f"Bearer {token_member}"}
    )
    assert response.status_code == 403


# ============================================================
# Forma de la respuesta y métricas
# ============================================================
@pytest.mark.asyncio
async def test_actividad_devuelve_metricas_por_vendedor(http_client, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    token = tenant_y_usuario["token"]

    vendedor_id = await crear_vendedor(tenant_id, "Ana")
    cliente_id = await crear_cliente(tenant_id, vendedor_id)

    await crear_visita(tenant_id, vendedor_id, cliente_id, dentro=True)
    await crear_visita(tenant_id, vendedor_id, cliente_id, dentro=True)
    await crear_visita(tenant_id, vendedor_id, cliente_id, dentro=False)

    await crear_tarea(
        tenant_id, vendedor_id, cliente_id,
        estado="completada", completado_en=datetime.now(timezone.utc),
    )
    await crear_tarea(tenant_id, vendedor_id, cliente_id, estado="pendiente")

    response = await http_client.get(
        RUTA, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    data = response.json()

    assert data["total_visitas"] == 3
    assert data["total_tareas_completadas"] == 1
    assert len(data["vendedores"]) == 1

    v = data["vendedores"][0]
    assert v["vendedor_id"] == str(vendedor_id)
    assert v["nombre"] == "Ana"
    assert v["activo"] is True
    assert v["visitas"] == 3
    assert v["visitas_validadas"] == 2
    assert v["visitas_fuera_geocerca"] == 1
    assert v["clientes_visitados"] == 1
    assert v["tareas_completadas"] == 1
    assert v["tareas_pendientes"] == 1
    assert v["porcentaje_validadas"] == pytest.approx(66.67, abs=0.01)


@pytest.mark.asyncio
async def test_actividad_incluye_vendedores_sin_actividad(http_client, tenant_y_usuario):
    """Un vendedor sin visitas ni tareas aparece en ceros, no se omite."""
    tenant_id = tenant_y_usuario["tenant_id"]
    token = tenant_y_usuario["token"]

    await crear_vendedor(tenant_id, "Sin Actividad")

    response = await http_client.get(
        RUTA, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    data = response.json()

    assert len(data["vendedores"]) == 1
    v = data["vendedores"][0]
    assert v["visitas"] == 0
    assert v["tareas_completadas"] == 0
    assert v["tareas_pendientes"] == 0
    # None y no 0.0: no hubo visitas que dieran porcentaje.
    assert v["porcentaje_validadas"] is None


@pytest.mark.asyncio
async def test_actividad_incluye_vendedores_desactivados(http_client, tenant_y_usuario):
    """Gerencia necesita ver los huecos, incluidos los vendedores dados de baja."""
    tenant_id = tenant_y_usuario["tenant_id"]
    token = tenant_y_usuario["token"]

    await crear_vendedor(tenant_id, "Baja", activo=False)

    response = await http_client.get(
        RUTA, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    data = response.json()

    assert len(data["vendedores"]) == 1
    assert data["vendedores"][0]["activo"] is False


# ============================================================
# Rango de fechas
# ============================================================
@pytest.mark.asyncio
async def test_actividad_no_cuenta_visitas_fuera_del_rango_por_defecto(
    http_client, tenant_y_usuario
):
    """Sin desde/hasta, la ventana es de los últimos 30 días."""
    tenant_id = tenant_y_usuario["tenant_id"]
    token = tenant_y_usuario["token"]

    vendedor_id = await crear_vendedor(tenant_id)
    cliente_id = await crear_cliente(tenant_id, vendedor_id)

    hace_40_dias = datetime.now(timezone.utc) - timedelta(days=40)
    await crear_visita(tenant_id, vendedor_id, cliente_id, cuando=hace_40_dias)
    await crear_visita(tenant_id, vendedor_id, cliente_id)  # hoy

    response = await http_client.get(
        RUTA, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json()["total_visitas"] == 1


@pytest.mark.asyncio
async def test_actividad_respeta_rango_explicito(http_client, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    token = tenant_y_usuario["token"]

    vendedor_id = await crear_vendedor(tenant_id)
    cliente_id = await crear_cliente(tenant_id, vendedor_id)

    hace_40_dias = datetime.now(timezone.utc) - timedelta(days=40)
    await crear_visita(tenant_id, vendedor_id, cliente_id, cuando=hace_40_dias)

    ahora = datetime.now(timezone.utc)
    response = await http_client.get(
        RUTA,
        params={
            "desde": (ahora - timedelta(days=50)).isoformat(),
            "hasta": ahora.isoformat(),
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["total_visitas"] == 1


@pytest.mark.asyncio
async def test_actividad_rango_invertido_devuelve_400(http_client, tenant_y_usuario):
    token = tenant_y_usuario["token"]
    ahora = datetime.now(timezone.utc)

    response = await http_client.get(
        RUTA,
        params={
            "desde": ahora.isoformat(),
            "hasta": (ahora - timedelta(days=1)).isoformat(),
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_actividad_rango_mayor_al_maximo_devuelve_400(http_client, tenant_y_usuario):
    token = tenant_y_usuario["token"]
    ahora = datetime.now(timezone.utc)

    response = await http_client.get(
        RUTA,
        params={
            "desde": (ahora - timedelta(days=400)).isoformat(),
            "hasta": ahora.isoformat(),
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400


# ============================================================
# Filtro por vendedor
# ============================================================
@pytest.mark.asyncio
async def test_actividad_filtra_por_vendedor_id(http_client, tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    token = tenant_y_usuario["token"]

    vendedor_1 = await crear_vendedor(tenant_id, "Ana")
    vendedor_2 = await crear_vendedor(tenant_id, "Beto")
    cliente_1 = await crear_cliente(tenant_id, vendedor_1)
    cliente_2 = await crear_cliente(tenant_id, vendedor_2)

    await crear_visita(tenant_id, vendedor_1, cliente_1)
    await crear_visita(tenant_id, vendedor_2, cliente_2)

    response = await http_client.get(
        RUTA,
        params={"vendedor_id": vendedor_1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    data = response.json()

    assert len(data["vendedores"]) == 1
    assert data["vendedores"][0]["vendedor_id"] == str(vendedor_1)


@pytest.mark.asyncio
async def test_actividad_no_ve_vendedores_de_otro_tenant(http_client, tenant_y_usuario):
    """Aislamiento entre negocios: un tenant no ve la actividad de otro."""
    tenant_id = tenant_y_usuario["tenant_id"]
    token = tenant_y_usuario["token"]
    otro_tenant_id = uuid4()

    await execute(
        "INSERT INTO tenants (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
        otro_tenant_id,
        "Otro Tenant",
    )

    await crear_vendedor(tenant_id, "De este tenant")
    vendedor_ajeno = await crear_vendedor(otro_tenant_id, "De otro tenant")
    cliente_ajeno = await crear_cliente(otro_tenant_id, vendedor_ajeno)
    await crear_visita(otro_tenant_id, vendedor_ajeno, cliente_ajeno)

    response = await http_client.get(
        RUTA, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    data = response.json()

    assert len(data["vendedores"]) == 1
    assert data["vendedores"][0]["nombre"] == "De este tenant"

    await execute("DELETE FROM tenants WHERE id = $1", otro_tenant_id)
