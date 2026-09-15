"""
Fixtures compartidas para tests.

Maneja:
- Inicialización y limpieza del pool de BD
- Creación de usuarios/tenants de prueba
- JWT válidos para tests
- AsyncClient para llamadas HTTP
"""

from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from main import app, socket_app
from security import crear_access_token, hash_password
from session import (
    close_pool,
    execute,
    fetch_one,
    fetch_value,
    init_pool,
)


@pytest.fixture
async def db():
    """
    Inicializa el pool para cada test.

    Nota: los tests son lentos porque cada uno reinicia el pool.
    Si necesitas mejor performance, cambiar a scope="session" después.
    """
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def tenant_y_usuario(db):
    """
    Crea un tenant y un usuario de prueba en BD.
    Devuelve dict con: tenant_id, usuario_id, email, role, token.

    Borra el tenant al terminar (CASCADE se lleva portal_users, alertas,
    etc.): sin esto, cada corrida de tests deja un tenant huérfano en la
    base — así se acumularon ~70 tenants "test-*@ejemplo.com" en desarrollo
    antes de que esto tuviera teardown.
    """
    tenant_id = uuid4()
    usuario_id = uuid4()
    email = f"test-{usuario_id}@ejemplo.com"
    role = "owner"

    # Crear tenant
    await execute(
        """
        INSERT INTO tenants (id, name)
        VALUES ($1, $2)
        ON CONFLICT (id) DO NOTHING
        """,
        tenant_id,
        "Test Tenant",
    )

    # Crear usuario del portal
    password_hash = hash_password("test_password")
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (id) DO NOTHING
        """,
        usuario_id,
        tenant_id,
        email,
        password_hash,
        role,
        True,
    )

    # Generar token
    token = crear_access_token(usuario_id, tenant_id, role)

    yield {
        "tenant_id": tenant_id,
        "usuario_id": usuario_id,
        "email": email,
        "role": role,
        "token": token,
    }

    await execute("DELETE FROM tenants WHERE id = $1", tenant_id)


@pytest.fixture
async def http_client():
    """AsyncClient para hacer requests HTTP a la app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def headers_autenticado(tenant_y_usuario):
    """Headers HTTP con token válido."""
    return {"Authorization": f"Bearer {tenant_y_usuario['token']}"}


@pytest.fixture
async def clean_alertas(db, tenant_y_usuario):
    """
    Limpia la tabla de alertas para el tenant de prueba.
    Útil al inicio y fin de cada test.
    """
    tenant_id = tenant_y_usuario["tenant_id"]
    await execute("DELETE FROM alertas WHERE tenant_id = $1", tenant_id)
    yield
    # Limpiar después del test también
    await execute("DELETE FROM alertas WHERE tenant_id = $1", tenant_id)
