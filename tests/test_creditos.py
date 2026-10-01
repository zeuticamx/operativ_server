"""
Créditos del plan y consumo por llamada a herramienta (services/creditos.py).

Lo que importa si falla:

  1. Carga: al activarse un plan (compra, renovación o prueba de gerencia)
     la bolsa del plan queda en su cuota; un aviso repetido de la pasarela
     no la vuelve a llenar, una renovación la reinicia sin acumular, y
     nunca toca los créditos comprados.
  2. Bloqueo: sin saldo (o con la bolsa del plan vencida) n8n recibe
     permitido=false y no se descuenta ni se escribe nada.
  3. Atomicidad: N llamadas simultáneas con saldo S descuentan exactamente
     min(N, S); un reintento con la misma idempotency_key no cobra dos veces.
  4. Los créditos del plan no reemplazan a la suscripción en el gate de
     acceso: un plan vencido no sigue operando con lo que le sobró.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from config import settings
from jobs.pagos_background import job_pausar_suscripciones_vencidas
from schemas import OtorgarPruebaIn
from security import crear_access_token, hash_password
from services import creditos, pruebas
from services.acceso_pagos import acceso_pagos
from services.pagos import activar_suscripcion, procesar_pago_aprobado
from services.stripe_suscripciones import procesar_factura_pagada
from session import execute, fetch_all, fetch_one, fetch_value

TOKEN = "token-interno-de-prueba"
URL = "/api/eventos/creditos/consumir"


@pytest.fixture
def con_token(monkeypatch):
    monkeypatch.setattr(settings, "N8N_INTERNAL_TOKEN", TOKEN)


async def _cuota(plan: str) -> Decimal:
    return await fetch_value("SELECT creditos_incluidos_mensual FROM planes WHERE nombre = $1", plan)


async def _bolsas(tenant_id) -> dict:
    return dict(
        await fetch_one(
            """
            SELECT creditos_plan, creditos_plan_vence, creditos_disponibles, creditos_gastados
              FROM tenant_credits WHERE tenant_id = $1
            """,
            tenant_id,
        )
    )


async def _saldo(tenant_id, *, plan=0, comprados=0, vence: datetime | None = None) -> None:
    """Deja las dos bolsas en un valor conocido, sin pasar por el libro."""
    vence = vence or datetime.now(timezone.utc) + timedelta(days=30)
    await execute(
        """
        INSERT INTO tenant_credits (tenant_id, creditos_plan, creditos_plan_vence, creditos_disponibles)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (tenant_id) DO UPDATE SET
            creditos_plan = EXCLUDED.creditos_plan,
            creditos_plan_vence = EXCLUDED.creditos_plan_vence,
            creditos_disponibles = EXCLUDED.creditos_disponibles
        """,
        tenant_id,
        Decimal(plan),
        vence,
        Decimal(comprados),
    )


async def _movimientos(tenant_id, tipo: str) -> list:
    return await fetch_all(
        "SELECT * FROM credit_transactions WHERE tenant_id = $1 AND tipo = $2 ORDER BY created_at",
        tenant_id,
        tipo,
    )


async def _consumir(http_client, tenant_id, herramienta="consultar_stock", clave=None, **extra):
    return await http_client.post(
        URL,
        json={
            "tenant_id": str(tenant_id),
            "herramienta": herramienta,
            "idempotency_key": clave or f"exec-{uuid4()}",
            **extra,
        },
        headers={"X-Internal-Token": TOKEN},
    )


# ============================================================
# 1. Carga de la cuota del plan
# ============================================================
@pytest.mark.asyncio
async def test_activar_un_plan_carga_su_cuota(tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]

    await activar_suscripcion(tenant_id, "pro", referencia="tx:carga")

    bolsas = await _bolsas(tenant_id)
    assert bolsas["creditos_plan"] == await _cuota("pro")
    renovacion = await fetch_value(
        "SELECT fecha_renovacion FROM tenant_subscriptions WHERE tenant_id = $1", tenant_id
    )
    assert bolsas["creditos_plan_vence"] == renovacion

    [asiento] = await _movimientos(tenant_id, "asignacion_plan")
    assert asiento["bolsa"] == "plan"
    assert asiento["referencia"] == "tx:carga"
    assert asiento["saldo_nuevo"] == await _cuota("pro")


@pytest.mark.asyncio
async def test_un_pago_de_mercado_pago_aprobado_carga_la_cuota(tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    transaccion_id = await fetch_value(
        """
        INSERT INTO tenant_transactions (tenant_id, tipo, concepto, monto, estado_pago, plan_nombre)
        VALUES ($1, 'subscription', 'Suscripción starter', 29.99, 'aprobado', 'starter')
        RETURNING id
        """,
        tenant_id,
    )

    await procesar_pago_aprobado(transaccion_id)

    assert (await _bolsas(tenant_id))["creditos_plan"] == await _cuota("starter")
    [asiento] = await _movimientos(tenant_id, "asignacion_plan")
    assert asiento["referencia"] == f"tx:{transaccion_id}"


@pytest.mark.asyncio
async def test_una_factura_de_stripe_pagada_carga_la_cuota(tenant_y_usuario, precios_stripe):
    tenant_id = tenant_y_usuario["tenant_id"]
    fin = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=30)
    linea = {"period": {"start": int(datetime.now(timezone.utc).timestamp()), "end": int(fin.timestamp())},
             "pricing": {"price_details": {"price": "price_test_pro"}}}
    factura = {
        "id": "in_creditos_1",
        "object": "invoice",
        "customer": "cus_test_1",
        "currency": "mxn",
        "amount_paid": 19900,
        "amount_due": 19900,
        "billing_reason": "subscription_cycle",
        "attempt_count": 0,
        "parent": {
            "type": "subscription_details",
            "subscription_details": {
                "subscription": "sub_creditos_1",
                "metadata": {"tenant_id": str(tenant_id), "plan": "pro"},
            },
        },
        "lines": {"data": [linea]},
    }

    await procesar_factura_pagada(factura)
    await procesar_factura_pagada(factura)  # Stripe reintenta el mismo evento

    bolsas = await _bolsas(tenant_id)
    assert bolsas["creditos_plan"] == await _cuota("pro")
    assert bolsas["creditos_plan_vence"] == fin
    assert len(await _movimientos(tenant_id, "asignacion_plan")) == 1


@pytest.mark.asyncio
async def test_un_aviso_repetido_no_devuelve_lo_ya_gastado(tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    await activar_suscripcion(tenant_id, "pro", referencia="tx:mismo")
    await creditos.consumir_credito(tenant_id, "consultar_stock", "exec-1")

    await activar_suscripcion(tenant_id, "pro", referencia="tx:mismo")

    assert (await _bolsas(tenant_id))["creditos_plan"] == await _cuota("pro") - 1
    assert len(await _movimientos(tenant_id, "asignacion_plan")) == 1


@pytest.mark.asyncio
async def test_la_renovacion_reinicia_la_cuota_sin_tocar_los_comprados(tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _saldo(tenant_id, plan=0, comprados=7)
    await activar_suscripcion(tenant_id, "starter", referencia="tx:ciclo-1")
    for i in range(3):
        await creditos.consumir_credito(tenant_id, "consultar_stock", f"exec-{i}")

    await activar_suscripcion(tenant_id, "starter", referencia="tx:ciclo-2")

    bolsas = await _bolsas(tenant_id)
    assert bolsas["creditos_plan"] == await _cuota("starter")  # reinicia, no acumula
    assert bolsas["creditos_disponibles"] == 7


@pytest.mark.asyncio
async def test_cambiar_de_plan_carga_la_cuota_del_nuevo(tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    await activar_suscripcion(tenant_id, "starter", referencia="tx:starter")

    await activar_suscripcion(tenant_id, "enterprise", referencia="tx:enterprise")

    assert (await _bolsas(tenant_id))["creditos_plan"] == await _cuota("enterprise")


@pytest.fixture
async def gerente(db):
    """Un portal_user cuyo correo también está en gerencia_users."""
    tid, uid = uuid4(), uuid4()
    email = f"gerente-{uid}@operativai.test"
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Interno')", tid)
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, 'owner', true)
        """,
        uid, tid, email, hash_password("x" * 12),
    )
    await execute(
        "INSERT INTO gerencia_users (email, full_name, cargo) VALUES ($1, 'Test', 'QA')", email
    )
    yield {
        "id": uid,
        "email": email,
        "headers": {"Authorization": f"Bearer {crear_access_token(uid, tid, 'owner')}"},
    }
    await execute("DELETE FROM gerencia_users WHERE email = $1", email)
    await execute("DELETE FROM gerencia_auditoria WHERE actor_email = $1", email)
    await execute("DELETE FROM tenants WHERE id = $1", tid)


@pytest.mark.asyncio
async def test_un_plan_otorgado_por_gerencia_carga_su_cuota(http_client, tenant_y_usuario, gerente):
    tenant_id = tenant_y_usuario["tenant_id"]

    r = await http_client.post(
        f"/api/gerencia/tenants/{tenant_id}/prueba",
        json={"plan": "pro", "motivo": "Piloto", "unidad": "dias", "cantidad": 14},
        headers=gerente["headers"],
    )
    assert r.status_code == 200

    bolsas = await _bolsas(tenant_id)
    assert bolsas["creditos_plan"] == await _cuota("pro")
    vence = await fetch_value(
        "SELECT fecha_renovacion FROM tenant_subscriptions WHERE tenant_id = $1", tenant_id
    )
    assert bolsas["creditos_plan_vence"] == vence
    [asiento] = await _movimientos(tenant_id, "asignacion_plan")
    assert gerente["email"] in asiento["concepto"]


@pytest.mark.asyncio
async def test_revocar_la_prueba_expira_la_cuota(tenant_y_usuario, gerente):
    tenant_id = tenant_y_usuario["tenant_id"]
    await pruebas.otorgar_prueba(
        tenant_id,
        OtorgarPruebaIn(plan="pro", motivo="Piloto", unidad="dias", cantidad=14),
        gerente_email=gerente["email"],
        gerente_id=gerente["id"],
    )

    await pruebas.revocar_prueba(
        tenant_id, "Fin del piloto", gerente_email=gerente["email"], gerente_id=gerente["id"]
    )

    assert (await _bolsas(tenant_id))["creditos_plan"] == 0
    [asiento] = await _movimientos(tenant_id, "expiracion_plan")
    assert asiento["cantidad"] == -(await _cuota("pro"))


@pytest.mark.asyncio
async def test_el_job_de_pausa_expira_la_cuota_del_plan_vencido(tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    await activar_suscripcion(tenant_id, "pro", referencia="tx:vence")
    await execute(
        "UPDATE tenant_subscriptions SET fecha_renovacion = NOW() - INTERVAL '1 minute' WHERE tenant_id = $1",
        tenant_id,
    )

    await job_pausar_suscripciones_vencidas()

    assert (await _bolsas(tenant_id))["creditos_plan"] == 0


# ============================================================
# 2. Consumo y bloqueo
# ============================================================
@pytest.mark.asyncio
async def test_consumir_descuenta_uno_y_deja_asiento(http_client, tenant_y_usuario, con_token):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _saldo(tenant_id, plan=3)
    conversacion = uuid4()

    r = await _consumir(http_client, tenant_id, "crear_reserva", conversation_id=str(conversacion))

    assert r.status_code == 200
    assert r.json() == {
        "permitido": True, "motivo": None, "saldo_restante": "2.00",
        "bolsa": "plan", "duplicado": False,
    }
    bolsas = await _bolsas(tenant_id)
    assert bolsas["creditos_plan"] == 2
    assert bolsas["creditos_gastados"] == 1

    [asiento] = await _movimientos(tenant_id, "gasto")
    assert asiento["herramienta"] == "crear_reserva"
    assert asiento["conversation_id"] == conversacion
    assert asiento["cantidad"] == -1
    assert asiento["saldo_anterior"] == 3
    assert asiento["saldo_nuevo"] == 2
    assert asiento["created_at"] is not None


@pytest.mark.asyncio
async def test_se_gasta_primero_el_plan_y_despues_lo_comprado(tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _saldo(tenant_id, plan=1, comprados=2)

    bolsas_usadas = [
        (await creditos.consumir_credito(tenant_id, "t", f"exec-{i}")).bolsa for i in range(3)
    ]

    assert bolsas_usadas == ["plan", "comprados", "comprados"]
    bolsas = await _bolsas(tenant_id)
    assert (bolsas["creditos_plan"], bolsas["creditos_disponibles"]) == (0, 0)


@pytest.mark.asyncio
async def test_sin_saldo_se_bloquea_y_no_se_escribe_nada(http_client, tenant_y_usuario, con_token):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _saldo(tenant_id, plan=0, comprados=0)

    r = await _consumir(http_client, tenant_id)

    assert r.status_code == 200
    assert r.json()["permitido"] is False
    assert r.json()["motivo"] == "sin_creditos"
    assert r.json()["saldo_restante"] == "0.00"
    assert (await _bolsas(tenant_id))["creditos_gastados"] == 0
    assert await _movimientos(tenant_id, "gasto") == []


@pytest.mark.asyncio
async def test_un_tenant_que_nunca_tuvo_creditos_se_bloquea(http_client, tenant_y_usuario, con_token):
    r = await _consumir(http_client, tenant_y_usuario["tenant_id"])

    assert r.json()["permitido"] is False


@pytest.mark.asyncio
async def test_la_cuota_de_un_plan_vencido_no_se_puede_gastar(tenant_y_usuario):
    """Aunque el job de pausa todavía no haya pasado a ponerla en cero."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _saldo(tenant_id, plan=50, vence=datetime.now(timezone.utc) - timedelta(minutes=1))

    r = await creditos.consumir_credito(tenant_id, "t", "exec-vencido")

    assert r.permitido is False
    assert (await _bolsas(tenant_id))["creditos_plan"] == 50


@pytest.mark.asyncio
async def test_un_reintento_con_la_misma_clave_no_cobra_dos_veces(http_client, tenant_y_usuario, con_token):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _saldo(tenant_id, plan=5)

    primera = await _consumir(http_client, tenant_id, clave="exec-42")
    segunda = await _consumir(http_client, tenant_id, clave="exec-42")

    assert primera.json()["duplicado"] is False
    assert segunda.json()["permitido"] is True
    assert segunda.json()["duplicado"] is True
    assert (await _bolsas(tenant_id))["creditos_plan"] == 4
    assert len(await _movimientos(tenant_id, "gasto")) == 1


@pytest.mark.asyncio
async def test_sin_token_interno_no_atiende(http_client, tenant_y_usuario, con_token):
    r = await http_client.post(
        URL,
        json={"tenant_id": str(tenant_y_usuario["tenant_id"]), "herramienta": "t", "idempotency_key": "x"},
    )
    assert r.status_code == 401


# ============================================================
# 3. Atomicidad
# ============================================================
@pytest.mark.asyncio
async def test_llamadas_simultaneas_no_gastan_mas_de_lo_que_hay(tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _saldo(tenant_id, plan=3, comprados=2)

    resultados = await asyncio.gather(
        *(creditos.consumir_credito(tenant_id, "t", f"exec-paralelo-{i}") for i in range(20))
    )

    assert sum(r.permitido for r in resultados) == 5
    bolsas = await _bolsas(tenant_id)
    assert (bolsas["creditos_plan"], bolsas["creditos_disponibles"]) == (0, 0)
    assert bolsas["creditos_gastados"] == 5
    gastos = await _movimientos(tenant_id, "gasto")
    assert len(gastos) == 5
    # El libro cuadra: cada asiento parte de donde dejó el anterior de su bolsa.
    assert sorted(g["saldo_nuevo"] for g in gastos if g["bolsa"] == "plan") == [0, 1, 2]
    assert sorted(g["saldo_nuevo"] for g in gastos if g["bolsa"] == "comprados") == [0, 1]


@pytest.mark.asyncio
async def test_reintentos_simultaneos_con_la_misma_clave_cobran_una_vez(tenant_y_usuario):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _saldo(tenant_id, plan=10)

    resultados = await asyncio.gather(
        *(creditos.consumir_credito(tenant_id, "t", "exec-repetida") for _ in range(5))
    )

    assert all(r.permitido for r in resultados)
    assert sum(not r.duplicado for r in resultados) == 1
    assert (await _bolsas(tenant_id))["creditos_plan"] == 9


# ============================================================
# 4. El gate de acceso no cambia
# ============================================================
@pytest.mark.asyncio
async def test_la_cuota_del_plan_no_mantiene_el_acceso_de_un_plan_vencido(tenant_y_usuario):
    """Solo los créditos comprados dejan operar sin suscripción (modelo híbrido)."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await activar_suscripcion(tenant_id, "pro", referencia="tx:gate")
    await execute(
        "UPDATE tenant_subscriptions SET estado = 'pausada' WHERE tenant_id = $1", tenant_id
    )

    acceso = await acceso_pagos(tenant_id)

    assert (await _bolsas(tenant_id))["creditos_plan"] > 0
    assert acceso.permitido is False
