"""
Créditos del plan y consumo por llamada a herramienta.

Regla de negocio: 1 crédito = 1 llamada a herramienta ejecutada por el
agente. La herramienta es el sub-workflow `ejecutar-herramienta-tenant` de
n8n: antes de ejecutar llama a POST /api/eventos/creditos/consumir, y si no
hay saldo no ejecuta y le devuelve al agente un resultado controlado.
`escalar_humano` no cobra: sin saldo, quien pide una persona tiene que
poder llegar a ella.

Dos bolsas por tenant (ver sql/31_creditos_herramientas.sql):
  - `creditos_plan`: cuota del ciclo. Se REINICIA a
    `planes.creditos_incluidos_mensual` cada vez que el plan se activa o
    renueva (pago o prueba de gerencia) y solo vale hasta
    `creditos_plan_vence`. Se gasta primero.
  - `creditos_disponibles`: comprados o ajustados por gerencia. No vencen,
    y siguen siendo lo único que services/acceso_pagos.py mira para dejar
    operar sin suscripción. Se gastan cuando la bolsa del plan se acaba.

Todo movimiento deja asiento en credit_transactions (saldo anterior y
nuevo de la bolsa tocada). La idempotencia la da el índice único
(tenant_id, tipo, referencia).
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import asyncpg

from session import fetch_one, transaccion

log = logging.getLogger("operativai.creditos")

UNO = Decimal(1)


def _plan_vigente(creditos_plan: Decimal, vence: datetime | None, ahora: datetime) -> Decimal:
    """Lo que se puede gastar de la bolsa del plan ahora mismo."""
    if vence is not None and vence <= ahora:
        return Decimal(0)
    return creditos_plan


async def cargar_creditos_plan(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    plan: str,
    vence: datetime | None,
    referencia: str,
    concepto: str,
) -> bool:
    """
    Deja la bolsa del plan en la cuota de `plan`, venciendo en `vence`.

    Corre dentro de la transacción de quien activa el plan: si esto falla,
    el plan tampoco queda activado. Reinicia, no suma: lo que sobró del
    ciclo anterior no se acumula.

    Devuelve False si `referencia` ya estaba cargada (aviso repetido de la
    pasarela) o si el plan no existe; en ambos casos no toca nada.
    """
    cuota = await conn.fetchval(
        "SELECT creditos_incluidos_mensual FROM planes WHERE nombre = $1", plan
    )
    if cuota is None:
        log.error("Carga de créditos de un plan inexistente: %s (tenant %s)", plan, tenant_id)
        return False

    ya_cargada = await conn.fetchval(
        """
        SELECT 1 FROM credit_transactions
         WHERE tenant_id = $1 AND tipo = 'asignacion_plan' AND referencia = $2
        """,
        tenant_id,
        referencia,
    )
    if ya_cargada:
        log.info("Créditos del plan ya cargados para %s (%s); no se repite", tenant_id, referencia)
        return False

    await conn.execute(
        "INSERT INTO tenant_credits (tenant_id) VALUES ($1) ON CONFLICT (tenant_id) DO NOTHING",
        tenant_id,
    )
    # FOR UPDATE: una carga y un consumo simultáneos se serializan acá.
    anterior = await conn.fetchval(
        "SELECT creditos_plan FROM tenant_credits WHERE tenant_id = $1 FOR UPDATE",
        tenant_id,
    )

    # El asiento va antes que el saldo: si otra carga con la misma
    # referencia ganó la carrera, el índice único aborta aquí.
    await conn.execute(
        """
        INSERT INTO credit_transactions
            (tenant_id, tipo, cantidad, concepto, bolsa, referencia, saldo_anterior, saldo_nuevo)
        VALUES ($1, 'asignacion_plan', $2, $3, 'plan', $4, $5, $2)
        """,
        tenant_id,
        cuota,
        concepto,
        referencia,
        anterior,
    )
    await conn.execute(
        """
        UPDATE tenant_credits
           SET creditos_plan = $2, creditos_plan_vence = $3, updated_at = NOW()
         WHERE tenant_id = $1
        """,
        tenant_id,
        cuota,
        vence,
    )
    log.info("Cargados %s créditos del plan %s al tenant %s", cuota, plan, tenant_id)
    return True


async def expirar_creditos_plan(conn: asyncpg.Connection, tenant_id: UUID, concepto: str) -> None:
    """
    Pone en cero la bolsa del plan (prueba revocada, plan pausado).

    El consumo ya ignora una bolsa vencida por fecha; esto es para que el
    saldo mostrado y el libro cuadren. Sin saldo que quitar no deja asiento.
    """
    anterior = await conn.fetchval(
        "SELECT creditos_plan FROM tenant_credits WHERE tenant_id = $1 FOR UPDATE",
        tenant_id,
    )
    if not anterior:
        return

    await conn.execute(
        """
        INSERT INTO credit_transactions
            (tenant_id, tipo, cantidad, concepto, bolsa, saldo_anterior, saldo_nuevo)
        VALUES ($1, 'expiracion_plan', $2, $3, 'plan', $4, 0)
        """,
        tenant_id,
        -anterior,
        concepto,
        anterior,
    )
    await conn.execute(
        "UPDATE tenant_credits SET creditos_plan = 0, updated_at = NOW() WHERE tenant_id = $1",
        tenant_id,
    )


@dataclass
class ResultadoConsumo:
    permitido: bool
    saldo_restante: Decimal
    bolsa: str | None = None
    duplicado: bool = False


async def _consumo_previo(tenant_id: UUID, referencia: str) -> ResultadoConsumo | None:
    fila = await fetch_one(
        """
        SELECT bolsa FROM credit_transactions
         WHERE tenant_id = $1 AND tipo = 'gasto' AND referencia = $2
        """,
        tenant_id,
        referencia,
    )
    if fila is None:
        return None
    return ResultadoConsumo(
        permitido=True,
        saldo_restante=await saldo_total(tenant_id),
        bolsa=fila["bolsa"],
        duplicado=True,
    )


async def saldo_total(tenant_id: UUID) -> Decimal:
    """Créditos gastables ahora: plan vigente + comprados."""
    fila = await fetch_one(
        """
        SELECT creditos_plan, creditos_plan_vence, creditos_disponibles, NOW() AS ahora
          FROM tenant_credits WHERE tenant_id = $1
        """,
        tenant_id,
    )
    if fila is None:
        return Decimal(0)
    return (
        _plan_vigente(fila["creditos_plan"], fila["creditos_plan_vence"], fila["ahora"])
        + fila["creditos_disponibles"]
    )


async def consumir_credito(
    tenant_id: UUID,
    herramienta: str,
    referencia: str,
    conversation_id: UUID | None = None,
) -> ResultadoConsumo:
    """
    Descuenta exactamente 1 crédito por una llamada a herramienta.

    Atómico: la fila de tenant_credits se bloquea (FOR UPDATE) durante la
    transacción, así que N llamadas en paralelo del mismo tenant se
    serializan y nunca gastan más de lo que hay (los CHECK >= 0 son el
    segundo candado). Con la misma `referencia` (id de la ejecución de n8n)
    un reintento devuelve el resultado original sin volver a descontar.

    Sin saldo no escribe nada y devuelve permitido=False.
    """
    previo = await _consumo_previo(tenant_id, referencia)
    if previo is not None:
        return previo

    try:
        async with transaccion() as conn:
            fila = await conn.fetchrow(
                """
                SELECT creditos_plan, creditos_plan_vence, creditos_disponibles, NOW() AS ahora
                  FROM tenant_credits
                 WHERE tenant_id = $1
                 FOR UPDATE
                """,
                tenant_id,
            )
            if fila is None:
                return ResultadoConsumo(permitido=False, saldo_restante=Decimal(0))

            plan = _plan_vigente(fila["creditos_plan"], fila["creditos_plan_vence"], fila["ahora"])
            comprados = fila["creditos_disponibles"]

            if plan >= UNO:
                bolsa, anterior, columna = "plan", fila["creditos_plan"], "creditos_plan"
            elif comprados >= UNO:
                bolsa, anterior, columna = "comprados", comprados, "creditos_disponibles"
            else:
                return ResultadoConsumo(permitido=False, saldo_restante=plan + comprados)

            nuevo = anterior - UNO
            await conn.execute(
                """
                INSERT INTO credit_transactions
                    (tenant_id, tipo, cantidad, concepto, bolsa, herramienta,
                     conversation_id, referencia, saldo_anterior, saldo_nuevo)
                VALUES ($1, 'gasto', -1, $2, $3, $4, $5, $6, $7, $8)
                """,
                tenant_id,
                f"Herramienta {herramienta}",
                bolsa,
                herramienta,
                conversation_id,
                referencia,
                anterior,
                nuevo,
            )
            # `columna` sale de los dos literales de arriba, nunca de la entrada.
            await conn.execute(
                f"""
                UPDATE tenant_credits
                   SET {columna} = $2,
                       creditos_gastados = creditos_gastados + 1,
                       updated_at = NOW()
                 WHERE tenant_id = $1
                """,
                tenant_id,
                nuevo,
            )
    except asyncpg.UniqueViolationError:
        # Dos reintentos con la misma referencia a la vez: ganó el otro.
        previo = await _consumo_previo(tenant_id, referencia)
        if previo is not None:
            return previo
        raise

    restante = (nuevo if bolsa == "plan" else plan) + (nuevo if bolsa == "comprados" else comprados)
    return ResultadoConsumo(permitido=True, saldo_restante=restante, bolsa=bolsa)
