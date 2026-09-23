"""
Catálogo de planes (routers/gerencia_planes.py), administrado desde el
panel de plataforma.

Lo que se cubre:
1. La puerta — mismo nivel que el resto de /gerencia/*.
2. Alta: valida, guarda con los defaults correctos y rechaza nombres
   repetidos.
3. Actualización parcial: None = no tocar, y hace falta al menos un campo.
4. Que el plan que ve un tenant en /api/pagos/catalogo (filtrado a activos)
   siga viendo lo mismo después de que gerencia edite el catálogo.
5. Bitácora.
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from security import crear_access_token, hash_password
from session import execute, fetch_one


# ------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------
@pytest.fixture
async def gerente(db):
    tid = uuid4()
    uid = uuid4()
    email = f"gerente-{uid}@ejemplo.com"

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

    await execute("DELETE FROM gerencia_users WHERE LOWER(email) = LOWER($1)", email)
    await execute("DELETE FROM gerencia_auditoria WHERE actor_email = $1", email)
    await execute("DELETE FROM tenants WHERE id = $1", tid)


@pytest.fixture
async def nombre_plan(db):
    """Nombre único por test; el teardown lo borra pase lo que pase."""
    nombre = f"plan-de-prueba-{uuid4()}"
    yield nombre
    await execute("DELETE FROM planes WHERE nombre = $1", nombre)


async def _crear(headers_o_client, headers=None, **overrides):
    """Atajo: crea un plan con valores razonables por defecto."""
    http_client = headers_o_client
    body = {
        "nombre": overrides.pop("nombre"),
        "precio_monthly": overrides.pop("precio_monthly", "199.00"),
        **overrides,
    }
    return await http_client.post("/api/gerencia/planes", json=body, headers=headers)


# ------------------------------------------------------------
# 1. La puerta
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_un_owner_de_tenant_no_entra(http_client, headers_autenticado):
    for metodo, ruta in (
        ("get", "/api/gerencia/planes"),
        ("post", "/api/gerencia/planes"),
        ("patch", "/api/gerencia/planes/lo-que-sea"),
    ):
        r = await getattr(http_client, metodo)(ruta, headers=headers_autenticado)
        assert r.status_code == 403, ruta


@pytest.mark.asyncio
async def test_sin_token_no_entra(http_client):
    r = await http_client.get("/api/gerencia/planes")
    assert r.status_code == 401


# ------------------------------------------------------------
# 2. Alta
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_crear_un_plan_con_los_defaults(http_client, gerente, nombre_plan):
    r = await _crear(http_client, gerente["headers"], nombre=nombre_plan, precio_monthly="49.90")
    assert r.status_code == 201

    d = r.json()
    assert d["nombre"] == nombre_plan
    assert Decimal(d["precio_monthly"]) == Decimal("49.90")
    assert d["precio_annual"] is None
    assert d["activo"] is True
    assert d["agente_ia_activo"] is True
    assert d["gestion_vendedores_activo"] is True
    assert Decimal(d["creditos_incluidos_mensual"]) == Decimal("100")
    assert d["orden"] == 0
    assert d["max_vendedores"] is None  # sin tope, como enterprise


@pytest.mark.asyncio
async def test_no_se_puede_repetir_nombre(http_client, gerente, nombre_plan):
    primero = await _crear(http_client, gerente["headers"], nombre=nombre_plan)
    assert primero.status_code == 201

    repetido = await _crear(http_client, gerente["headers"], nombre=nombre_plan)
    assert repetido.status_code == 409


@pytest.mark.asyncio
async def test_precio_negativo_se_rechaza(http_client, gerente, nombre_plan):
    r = await _crear(http_client, gerente["headers"], nombre=nombre_plan, precio_monthly="-1")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_el_alta_queda_en_la_bitacora(http_client, gerente, nombre_plan):
    await _crear(http_client, gerente["headers"], nombre=nombre_plan, precio_monthly="99")

    asiento = await fetch_one(
        "SELECT actor_email, detalle FROM gerencia_auditoria "
        "WHERE accion = 'plan_creado' ORDER BY created_at DESC LIMIT 1",
    )
    assert asiento["actor_email"] == gerente["email"]
    assert asiento["detalle"]["nombre"] == nombre_plan


# ------------------------------------------------------------
# 3. Actualización parcial
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_actualizar_solo_toca_los_campos_enviados(http_client, gerente, nombre_plan):
    await _crear(
        http_client,
        gerente["headers"],
        nombre=nombre_plan,
        precio_monthly="199",
        descripcion="original",
        orden=5,
    )

    r = await http_client.patch(
        f"/api/gerencia/planes/{nombre_plan}",
        json={"precio_monthly": "249.00"},
        headers=gerente["headers"],
    )
    assert r.status_code == 200

    d = r.json()
    assert Decimal(d["precio_monthly"]) == Decimal("249.00")
    # No se tocaron: siguen igual que al crear.
    assert d["descripcion"] == "original"
    assert d["orden"] == 5


@pytest.mark.asyncio
async def test_apagar_un_plan_no_lo_borra(http_client, gerente, nombre_plan):
    await _crear(http_client, gerente["headers"], nombre=nombre_plan)

    r = await http_client.patch(
        f"/api/gerencia/planes/{nombre_plan}",
        json={"activo": False},
        headers=gerente["headers"],
    )
    assert r.status_code == 200
    assert r.json()["activo"] is False

    # Sigue en el listado de gerencia (que trae activos e inactivos).
    listado = await http_client.get("/api/gerencia/planes", headers=gerente["headers"])
    assert any(p["nombre"] == nombre_plan for p in listado.json())


@pytest.mark.asyncio
async def test_un_patch_vacio_se_rechaza(http_client, gerente, nombre_plan):
    await _crear(http_client, gerente["headers"], nombre=nombre_plan)

    r = await http_client.patch(
        f"/api/gerencia/planes/{nombre_plan}", json={}, headers=gerente["headers"]
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_actualizar_un_plan_inexistente_es_404(http_client, gerente):
    r = await http_client.patch(
        "/api/gerencia/planes/no-existe-este-plan",
        json={"activo": False},
        headers=gerente["headers"],
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_la_actualizacion_queda_en_la_bitacora(http_client, gerente, nombre_plan):
    await _crear(http_client, gerente["headers"], nombre=nombre_plan)
    await http_client.patch(
        f"/api/gerencia/planes/{nombre_plan}",
        json={"precio_monthly": "10.00"},
        headers=gerente["headers"],
    )

    asiento = await fetch_one(
        "SELECT actor_email, detalle FROM gerencia_auditoria "
        "WHERE accion = 'plan_actualizado' ORDER BY created_at DESC LIMIT 1",
    )
    assert asiento["actor_email"] == gerente["email"]
    assert asiento["detalle"]["nombre"] == nombre_plan
    assert asiento["detalle"]["cambios"]["precio_monthly"] == "10.00"


# ------------------------------------------------------------
# 4. Lo que ve un tenant contratando no se rompe
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_catalogo_publico_refleja_los_cambios_de_gerencia(
    http_client, gerente, nombre_plan, tenant_y_usuario
):
    await _crear(
        http_client,
        gerente["headers"],
        nombre=nombre_plan,
        precio_monthly="77.00",
        orden=1,
    )

    catalogo = await http_client.get(
        "/api/pagos/catalogo",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert catalogo.status_code == 200
    encontrado = next(p for p in catalogo.json()["planes"] if p["nombre"] == nombre_plan)
    assert Decimal(encontrado["precio_monthly"]) == Decimal("77.00")

    # Al apagarlo, desaparece del catálogo público (pero sigue en el de gerencia).
    await http_client.patch(
        f"/api/gerencia/planes/{nombre_plan}",
        json={"activo": False},
        headers=gerente["headers"],
    )
    catalogo_2 = await http_client.get(
        "/api/pagos/catalogo",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    assert not any(p["nombre"] == nombre_plan for p in catalogo_2.json()["planes"])
