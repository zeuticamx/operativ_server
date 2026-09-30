"""
Eliminación definitiva de un negocio (services/eliminacion_tenant.py).

Las reglas, en orden de importancia:

1. Un negocio que no está en 'baja' NO se borra. Ni 'activo', ni 'prueba',
   ni 'suspendido', ni uno sin fila de estado (= activo). Primero se da de
   baja con PATCH .../estado, que es la inactivación de siempre.
2. Uno con suscripción vigente tampoco, aunque esté en 'baja': 'activa',
   días pagados por delante, o una Subscription de Stripe que Stripe sigue
   cobrando.
3. Solo gerencia de plataforma. Un owner, un member o el "ver como" → 403.
4. Cuando cumple todo, se borra de verdad y queda la foto en la bitácora.

En cada bloqueo se verifica además que el negocio siga existiendo: un 409
que igual borró sería el peor bug posible acá.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from security import crear_access_token, hash_password
from session import execute, fetch_one, fetch_value


# ------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------
NOMBRE = "Ferretería a eliminar"


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
    """Un tenant con dueño, un member y una conversación."""
    tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, $2)", tid, NOMBRE)
    owner_id, owner_email = await _crear_portal_user(tid, "owner")
    member_id, _ = await _crear_portal_user(tid, "member")
    cliente = await fetch_value(
        "INSERT INTO users (tenant_id, display_name) VALUES ($1, 'Cliente') RETURNING id", tid
    )
    await execute(
        "INSERT INTO conversations (tenant_id, user_id, channel_type) VALUES ($1, $2, 'whatsapp')",
        tid,
        cliente,
    )
    yield {
        "tenant_id": tid,
        "owner_id": owner_id,
        "owner_email": owner_email,
        "owner_headers": {"Authorization": f"Bearer {crear_access_token(owner_id, tid, 'owner')}"},
        "member_headers": {
            "Authorization": f"Bearer {crear_access_token(member_id, tid, 'member')}"
        },
    }
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
        "tenant_id": tid,
        "email": email,
        "headers": {"Authorization": f"Bearer {crear_access_token(uid, tid, 'owner')}"},
    }
    await execute("DELETE FROM gerencia_users WHERE LOWER(email) = LOWER($1)", email)
    await execute("DELETE FROM gerencia_auditoria WHERE actor_email = $1", email)
    await execute("DELETE FROM tenants WHERE id = $1", tid)


async def _estado(tenant_id, estado: str) -> None:
    await execute(
        """
        INSERT INTO tenant_estado_plataforma (tenant_id, estado, motivo)
        VALUES ($1, $2, 'cierre a pedido del cliente')
        ON CONFLICT (tenant_id) DO UPDATE SET estado = EXCLUDED.estado,
                                              motivo = EXCLUDED.motivo
        """,
        tenant_id,
        estado,
    )


async def _suscripcion(
    tenant_id,
    estado: str,
    *,
    renovacion: datetime | None = None,
    stripe_sub: str | None = None,
    cancelada_en: datetime | None = None,
) -> None:
    await execute(
        """
        INSERT INTO tenant_subscriptions
            (tenant_id, plan, estado, precio_monthly, fecha_renovacion,
             stripe_subscription_id, cancelada_en)
        VALUES ($1, 'pro', $2, 99.99, $3, $4, $5)
        """,
        tenant_id,
        estado,
        renovacion,
        stripe_sub,
        cancelada_en,
    )


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


async def _eliminar(http_client, headers, tenant_id, confirmacion: str = NOMBRE):
    return await http_client.post(
        f"/api/gerencia/tenants/{tenant_id}/eliminar",
        json={"confirmacion": confirmacion},
        headers=headers,
    )


async def _existe(tenant_id) -> bool:
    return await fetch_value("SELECT EXISTS (SELECT 1 FROM tenants WHERE id = $1)", tenant_id)


# ------------------------------------------------------------
# 1. Solo un negocio dado de baja
# ------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("estado", [None, "activo", "prueba", "suspendido"])
async def test_un_negocio_que_no_esta_en_baja_no_se_elimina(http_client, gerente, negocio, estado):
    tid = negocio["tenant_id"]
    if estado is not None:
        await _estado(tid, estado)

    r = await _eliminar(http_client, gerente["headers"], tid)

    assert r.status_code == 409
    detalle = r.json()["detail"]
    assert detalle["codigo"] == "cuenta_activa"
    assert "Primero hay que dar de baja" in detalle["mensaje"]
    assert await _existe(tid)


@pytest.mark.asyncio
async def test_la_revision_explica_que_falta_darlo_de_baja(http_client, gerente, negocio):
    r = await http_client.get(
        f"/api/gerencia/tenants/{negocio['tenant_id']}/eliminacion", headers=gerente["headers"]
    )

    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["eliminable"] is False
    assert [b["codigo"] for b in cuerpo["bloqueos"]] == ["cuenta_activa"]
    assert cuerpo["usuarios_portal"] == 2
    assert cuerpo["conversaciones"] == 1


# ------------------------------------------------------------
# 2. Sin suscripción vigente
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_en_baja_con_suscripcion_activa_no_se_elimina(http_client, gerente, negocio):
    tid = negocio["tenant_id"]
    await _estado(tid, "baja")
    await _suscripcion(tid, "activa", renovacion=_ahora() + timedelta(days=12))

    r = await _eliminar(http_client, gerente["headers"], tid)

    assert r.status_code == 409
    detalle = r.json()["detail"]
    assert detalle["codigo"] == "suscripcion_vigente"
    assert "vigente hasta el" in detalle["mensaje"]
    assert await _existe(tid)


@pytest.mark.asyncio
async def test_activo_y_con_suscripcion_informa_los_dos_bloqueos(http_client, gerente, negocio):
    tid = negocio["tenant_id"]
    await _suscripcion(tid, "activa", renovacion=_ahora() + timedelta(days=12))

    r = await _eliminar(http_client, gerente["headers"], tid)

    assert r.status_code == 409
    detalle = r.json()["detail"]
    # El primero es el que se muestra: primero hay que inactivar.
    assert detalle["codigo"] == "cuenta_activa"
    assert [b["codigo"] for b in detalle["bloqueos"]] == ["cuenta_activa", "suscripcion_vigente"]
    assert await _existe(tid)


@pytest.mark.asyncio
async def test_stripe_viva_bloquea_aunque_figure_pausada(http_client, gerente, negocio):
    """
    Cobros fallidos: acá quedó 'pausada' y vencida, pero Stripe no dio de
    baja la Subscription y la sigue reintentando.
    """
    tid = negocio["tenant_id"]
    await _estado(tid, "baja")
    await _suscripcion(
        tid, "pausada", renovacion=_ahora() - timedelta(days=3), stripe_sub="sub_viva"
    )

    r = await _eliminar(http_client, gerente["headers"], tid)

    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "suscripcion_vigente"
    assert await _existe(tid)


@pytest.mark.asyncio
async def test_cancelada_con_dias_pagados_bloquea(http_client, gerente, negocio):
    """Stripe ya la dio de baja, pero le quedan días pagados por delante."""
    tid = negocio["tenant_id"]
    await _estado(tid, "baja")
    await _suscripcion(
        tid,
        "cancelada",
        renovacion=_ahora() + timedelta(days=5),
        stripe_sub="sub_cancelada",
        cancelada_en=_ahora() - timedelta(days=1),
    )

    r = await _eliminar(http_client, gerente["headers"], tid)

    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "suscripcion_vigente"
    assert await _existe(tid)


@pytest.mark.asyncio
async def test_una_prueba_otorgada_vigente_bloquea(http_client, gerente, negocio):
    tid = negocio["tenant_id"]
    await _estado(tid, "baja")
    await _suscripcion(tid, "activa", renovacion=_ahora() + timedelta(days=7))
    await execute("UPDATE tenant_subscriptions SET origen = 'prueba' WHERE tenant_id = $1", tid)

    r = await _eliminar(http_client, gerente["headers"], tid)

    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "suscripcion_vigente"


# ------------------------------------------------------------
# 3. Quién puede
# ------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("quien", ["owner_headers", "member_headers"])
async def test_roles_del_negocio_no_pueden_eliminar(http_client, negocio, quien):
    """Ni siquiera su propio negocio, aunque esté listo para borrarse."""
    tid = negocio["tenant_id"]
    await _estado(tid, "baja")

    r = await _eliminar(http_client, negocio[quien], tid)
    assert r.status_code == 403
    r = await http_client.get(
        f"/api/gerencia/tenants/{tid}/eliminacion", headers=negocio[quien]
    )
    assert r.status_code == 403
    assert await _existe(tid)


@pytest.mark.asyncio
async def test_el_ver_como_no_puede_eliminar(http_client, gerente, negocio):
    tid = negocio["tenant_id"]
    await _estado(tid, "baja")
    imp = await http_client.post(
        f"/api/gerencia/tenants/{tid}/impersonar",
        json={"motivo": "soporte: revisar antes de borrar"},
        headers=gerente["headers"],
    )
    token = imp.json()["access_token"]

    r = await _eliminar(http_client, {"Authorization": f"Bearer {token}"}, tid)

    assert r.status_code == 403
    assert await _existe(tid)


@pytest.mark.asyncio
async def test_sin_token_401(http_client, negocio):
    r = await http_client.post(
        f"/api/gerencia/tenants/{negocio['tenant_id']}/eliminar", json={"confirmacion": NOMBRE}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_no_se_elimina_un_negocio_con_alguien_de_gerencia(http_client, gerente):
    """El tenant interno del propio gerente, aunque esté en baja y sin plan."""
    await _estado(gerente["tenant_id"], "baja")

    r = await _eliminar(http_client, gerente["headers"], gerente["tenant_id"], "Interno")

    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "cuenta_propia"
    assert await _existe(gerente["tenant_id"])


# ------------------------------------------------------------
# 4. Confirmación y casos de borde
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_confirmacion_distinta_al_nombre_no_borra(http_client, gerente, negocio):
    tid = negocio["tenant_id"]
    await _estado(tid, "baja")

    r = await _eliminar(http_client, gerente["headers"], tid, "ferreteria a eliminar")

    assert r.status_code == 400
    assert await _existe(tid)


@pytest.mark.asyncio
async def test_negocio_inexistente_404(http_client, gerente):
    otro = uuid4()
    r = await _eliminar(http_client, gerente["headers"], otro)
    assert r.status_code == 404
    r = await http_client.get(f"/api/gerencia/tenants/{otro}/eliminacion", headers=gerente["headers"])
    assert r.status_code == 404


# ------------------------------------------------------------
# 5. El borrado exitoso
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_en_baja_y_sin_suscripcion_se_elimina_con_todo(http_client, gerente, negocio):
    tid = negocio["tenant_id"]
    await _estado(tid, "baja")
    await execute(
        "INSERT INTO tenant_credits (tenant_id, creditos_disponibles) VALUES ($1, 40)", tid
    )
    await execute(
        """
        INSERT INTO tenant_transactions (tenant_id, tipo, concepto, monto, estado_pago)
        VALUES ($1, 'subscription', 'Plan pro', 99.99, 'aprobado')
        """,
        tid,
    )
    # Un calendario con citas: reservas → proveedores/servicios es RESTRICT,
    # y aun así el DELETE en cascada del tenant tiene que pasar.
    proveedor = await fetch_value(
        "INSERT INTO proveedores (tenant_id, nombre) VALUES ($1, 'Ana') RETURNING id", tid
    )
    servicio = await fetch_value(
        "INSERT INTO servicios (tenant_id, nombre, duracion_minutos) VALUES ($1, 'Corte', 30) RETURNING id",
        tid,
    )
    await execute(
        """
        INSERT INTO reservas (tenant_id, proveedor_id, servicio_id, cliente_nombre, hora_inicio, hora_fin)
        VALUES ($1, $2, $3, 'Uno', NOW() + INTERVAL '1 day', NOW() + INTERVAL '1 day 30 minutes')
        """,
        tid,
        proveedor,
        servicio,
    )

    revision = await http_client.get(
        f"/api/gerencia/tenants/{tid}/eliminacion", headers=gerente["headers"]
    )
    assert revision.json()["eliminable"] is True
    assert revision.json()["bloqueos"] == []
    # Los créditos se advierten, no bloquean.
    assert Decimal(revision.json()["creditos_disponibles"]) == 40

    r = await _eliminar(http_client, gerente["headers"], tid, f"  {NOMBRE} ")

    assert r.status_code == 204
    assert not await _existe(tid)
    for tabla in ("portal_users", "conversations", "tenant_transactions", "reservas"):
        n = await fetch_value(f"SELECT COUNT(*) FROM {tabla} WHERE tenant_id = $1", tid)
        assert n == 0, tabla

    entrada = await fetch_one(
        "SELECT actor_email, detalle FROM gerencia_auditoria "
        "WHERE tenant_id = $1 AND accion = 'eliminar_tenant'",
        tid,
    )
    assert entrada["actor_email"] == gerente["email"]
    detalle = entrada["detalle"]
    assert detalle["nombre"] == NOMBRE
    assert negocio["owner_email"] in detalle["correos"]
    assert detalle["total_cobrado"] == "99.99"
    assert Decimal(detalle["creditos_perdidos"]) == 40
    assert detalle["motivo_baja"] == "cierre a pedido del cliente"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "estado_sub, stripe_sub, cancelada",
    [
        # Vencida y pausada por el job, sin Stripe.
        ("pausada", None, False),
        # Stripe la dio de baja y los días pagados ya pasaron.
        ("pausada", "sub_terminada", True),
        ("cancelada", "sub_terminada", True),
    ],
)
async def test_con_suscripcion_terminada_se_elimina(
    http_client, gerente, negocio, estado_sub, stripe_sub, cancelada
):
    tid = negocio["tenant_id"]
    await _estado(tid, "baja")
    await _suscripcion(
        tid,
        estado_sub,
        renovacion=_ahora() - timedelta(days=2),
        stripe_sub=stripe_sub,
        cancelada_en=_ahora() - timedelta(days=10) if cancelada else None,
    )

    r = await _eliminar(http_client, gerente["headers"], tid)

    assert r.status_code == 204
    assert not await _existe(tid)


@pytest.mark.asyncio
async def test_la_bitacora_sigue_mostrando_el_nombre_del_negocio_eliminado(
    http_client, gerente, negocio
):
    tid = negocio["tenant_id"]
    await _estado(tid, "baja")
    assert (await _eliminar(http_client, gerente["headers"], tid)).status_code == 204

    r = await http_client.get(f"/api/gerencia/auditoria?tenant_id={tid}", headers=gerente["headers"])

    assert r.status_code == 200
    entradas = r.json()
    assert entradas[0]["accion"] == "eliminar_tenant"
    assert all(e["tenant_nombre"] == NOMBRE for e in entradas)
