"""
Núcleo de cobros que NO depende de la pasarela.

Acá vive lo que vale igual con Stripe, con Mercado Pago o con lo que venga
después: cuánto cuesta lo que se está comprando, y qué se entrega cuando un
pago queda aprobado. Los routers de cada proveedor se ocupan solo de su
propia API y de su propia firma de webhook, y terminan llamando siempre a
`procesar_pago_aprobado`.

Dos reglas que valen para todo el módulo:

1. El precio NUNCA llega del cliente. `cotizar` recibe qué se quiere comprar
   (un plan o un paquete de créditos) y el monto sale de las tablas
   `planes` / `paquetes_creditos`. Si el monto viajara en el body, cualquiera
   podría contratar enterprise por un peso.

2. Lo que da por bueno un pago es el webhook, no el regreso del navegador.
   Las URLs de retorno solo sirven para mostrarle algo al usuario; alguien
   puede abrir /pagos/exito a mano. El estado real se escribe cuando la
   pasarela avisa por su webhook.
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import asyncpg
from fastapi import HTTPException, status

from schemas import CrearPagoIn
from session import execute, fetch_one, transaccion

log = logging.getLogger("operativai.pagos")

# Cuántos días dura un ciclo. 30 fijos y no "el mismo día del mes que
# viene": sin dateutil, sumar un mes calendario a un 31 de enero es un caso
# borde que no vale la pena resolver a mano hasta que el negocio lo pida.
DIAS_CICLO = 30


async def cotizar(datos: CrearPagoIn) -> tuple[Decimal, str]:
    """
    Traduce "qué quiere comprar" a (monto, concepto), leyendo el precio de
    la base. Un plan o un paquete que no exista (o esté dado de baja) es un
    404 y no un cobro por un monto inventado.
    """
    if datos.tipo == "subscription":
        fila = await fetch_one(
            "SELECT precio_monthly, descripcion FROM planes WHERE nombre = $1 AND activo",
            datos.plan,
        )
        if fila is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Ese plan no existe o no está disponible",
            )
        return fila["precio_monthly"], f"Suscripción {datos.plan}"

    fila = await fetch_one(
        "SELECT precio FROM paquetes_creditos WHERE creditos = $1 AND activo",
        datos.creditos,
    )
    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay un paquete de créditos de esa cantidad",
        )
    return fila["precio"], f"{int(datos.creditos)} créditos"


async def marcar_cancelada(transaccion_id: UUID) -> None:
    """
    Cierra una transacción que nunca llegó a tener checkout.

    Solo toca las 'pendiente': si el webhook ya la movió a aprobada
    mientras tanto, esto no puede pisarla.
    """
    await execute(
        """
        UPDATE tenant_transactions
           SET estado_pago = 'cancelado', updated_at = NOW()
         WHERE id = $1 AND estado_pago = 'pendiente'
        """,
        transaccion_id,
    )


async def procesar_pago_aprobado(transaccion_id: UUID) -> None:
    """
    Entrega lo que se pagó: activa el plan o acredita los créditos.

    Corre fuera del request (BackgroundTask), así que no puede devolver un
    error al cliente: todo lo que falle se registra en el log.

    Es idempotente porque toda pasarela reintenta el mismo aviso:
      - suscripción -> UPSERT, escribe siempre el mismo estado final
      - créditos    -> el índice único parcial de credit_transactions
                       (una fila 'compra' por transacción) hace que el
                       segundo intento aborte la transacción entera antes de
                       tocar el saldo
    """
    try:
        fila = await fetch_one(
            """
            SELECT tenant_id, tipo, plan_nombre, creditos_comprados, concepto
            FROM tenant_transactions
            WHERE id = $1 AND estado_pago = 'aprobado'
            """,
            transaccion_id,
        )
        if fila is None:
            return

        if fila["tipo"] == "subscription":
            await activar_suscripcion(fila["tenant_id"], fila["plan_nombre"])
        elif fila["tipo"] == "credit_purchase":
            await acreditar_creditos(
                fila["tenant_id"],
                transaccion_id,
                fila["creditos_comprados"],
                fila["concepto"],
            )
    except Exception:  # noqa: BLE001 - es un background task: nada puede escapar
        log.exception("Falló el procesamiento del pago %s", transaccion_id)


async def activar_suscripcion(tenant_id: UUID, plan: str) -> None:
    """Deja el plan vigente y prende los servicios que ese plan incluye."""
    plan_fila = await fetch_one(
        """
        SELECT precio_monthly, agente_ia_activo, gestion_vendedores_activo
        FROM planes
        WHERE nombre = $1
        """,
        plan,
    )
    if plan_fila is None:
        log.error("Pago aprobado de un plan inexistente: %s", plan)
        return

    renovacion = datetime.now(timezone.utc) + timedelta(days=DIAS_CICLO)

    async with transaccion() as conn:
        await conn.execute(
            """
            INSERT INTO tenant_subscriptions
                (tenant_id, plan, estado, precio_monthly, fecha_inicio,
                 fecha_renovacion, intentos_fallidos)
            VALUES ($1, $2, 'activa', $3, NOW(), $4, 0)
            ON CONFLICT (tenant_id) DO UPDATE SET
                plan              = EXCLUDED.plan,
                estado            = 'activa',
                precio_monthly    = EXCLUDED.precio_monthly,
                fecha_renovacion  = EXCLUDED.fecha_renovacion,
                intentos_fallidos = 0,
                updated_at        = NOW()
            """,
            tenant_id,
            plan,
            plan_fila["precio_monthly"],
            renovacion,
        )

        # tenant_servicios es la tabla que miran el resto de los módulos
        # para saber si el agente / el CRM están encendidos. El plan es
        # quien manda sobre esos flags.
        await conn.execute(
            """
            INSERT INTO tenant_servicios
                (tenant_id, agente_ia_activo, gestion_vendedores_activo, actualizado_en)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (tenant_id) DO UPDATE SET
                agente_ia_activo          = EXCLUDED.agente_ia_activo,
                gestion_vendedores_activo = EXCLUDED.gestion_vendedores_activo,
                actualizado_en            = NOW()
            """,
            tenant_id,
            plan_fila["agente_ia_activo"],
            plan_fila["gestion_vendedores_activo"],
        )

    log.info("Suscripción %s activada para el tenant %s", plan, tenant_id)


async def acreditar_creditos(
    tenant_id: UUID,
    transaccion_id: UUID,
    creditos: Decimal | None,
    concepto: str | None,
) -> None:
    if creditos is None or creditos <= 0:
        log.error("Compra de créditos sin cantidad: %s", transaccion_id)
        return

    try:
        async with transaccion() as conn:
            await conn.execute(
                """
                INSERT INTO tenant_credits (tenant_id)
                VALUES ($1)
                ON CONFLICT (tenant_id) DO NOTHING
                """,
                tenant_id,
            )

            # FOR UPDATE: dos webhooks del mismo tenant a la vez se serializan
            # acá en vez de pisarse el saldo.
            saldo_anterior = await conn.fetchval(
                "SELECT creditos_disponibles FROM tenant_credits WHERE tenant_id = $1 FOR UPDATE",
                tenant_id,
            )
            saldo_nuevo = (saldo_anterior or Decimal(0)) + creditos

            # Va ANTES de tocar el saldo: si este pago ya se acreditó, el
            # índice único parcial lanza acá y la transacción entera se
            # descarta sin haber sumado nada.
            await conn.execute(
                """
                INSERT INTO credit_transactions
                    (tenant_id, tipo, cantidad, concepto, tenant_transaction_id,
                     saldo_anterior, saldo_nuevo)
                VALUES ($1, 'compra', $2, $3, $4, $5, $6)
                """,
                tenant_id,
                creditos,
                concepto,
                transaccion_id,
                saldo_anterior or Decimal(0),
                saldo_nuevo,
            )

            await conn.execute(
                """
                UPDATE tenant_credits
                   SET creditos_disponibles = $2,
                       fecha_ultima_compra  = NOW(),
                       updated_at           = NOW()
                 WHERE tenant_id = $1
                """,
                tenant_id,
                saldo_nuevo,
            )
    except asyncpg.UniqueViolationError:
        # El aviso repetido de siempre. No es un error.
        log.info("El pago %s ya estaba acreditado; no se repite", transaccion_id)
        return

    log.info("Acreditados %s créditos al tenant %s", creditos, tenant_id)
