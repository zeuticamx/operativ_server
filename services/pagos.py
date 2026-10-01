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

from config import settings
from schemas import CrearPagoIn
from services.creditos import cargar_creditos_plan
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

    Los precios son netos: el monto cotizado es exactamente el de la tabla
    (el mismo que muestra el catálogo), sin ningún impuesto encima.
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
            await activar_suscripcion(
                fila["tenant_id"], fila["plan_nombre"], referencia=f"tx:{transaccion_id}"
            )
        elif fila["tipo"] == "credit_purchase":
            await acreditar_creditos(
                fila["tenant_id"],
                transaccion_id,
                fila["creditos_comprados"],
                fila["concepto"],
            )
    except Exception:  # noqa: BLE001 - es un background task: nada puede escapar
        log.exception("Falló el procesamiento del pago %s", transaccion_id)


async def activar_suscripcion(
    tenant_id: UUID,
    plan: str,
    *,
    fecha_renovacion: datetime | None = None,
    stripe_customer_id: str | None = None,
    stripe_subscription_id: str | None = None,
    referencia: str | None = None,
    conn: asyncpg.Connection | None = None,
) -> bool:
    """
    Deja el plan vigente y prende los servicios que ese plan incluye.
    Devuelve False si el plan no existe (no activa nada).

    Los parámetros opcionales son del cobro recurrente de Stripe
    (services/stripe_suscripciones.py):
      - fecha_renovacion: el fin del período que Stripe acaba de cobrar. Sin
        ella se usan DIAS_CICLO desde hoy, como con el pago único.
      - stripe_*_id: dejan la fila enlazada a la Subscription, que es como
        llegan los eventos siguientes (renovación, fallo, cancelación).
      - conn: para correr dentro de la transacción de quien llama, junto con
        el registro del cobro — si una de las dos cosas falla, el webhook
        responde 500, Stripe reintenta y no queda un cobro sin entregar.

    `referencia` identifica el cobro ('tx:<uuid>', 'stripe_invoice:<id>') y
    con ella se carga la cuota de créditos del plan (services/creditos.py).
    Es lo que hace idempotente la carga: un aviso repetido de la pasarela
    vuelve a escribir el mismo estado de la suscripción, pero no le devuelve
    al tenant los créditos que ya gastó en el ciclo.

    Activar limpia cualquier rastro de cancelación o cobro fallido: un pago
    aprobado es la prueba de que la suscripción está al día.
    """
    if conn is None:
        async with transaccion() as nueva:
            return await activar_suscripcion(
                tenant_id,
                plan,
                fecha_renovacion=fecha_renovacion,
                stripe_customer_id=stripe_customer_id,
                stripe_subscription_id=stripe_subscription_id,
                referencia=referencia,
                conn=nueva,
            )

    plan_fila = await conn.fetchrow(
        """
        SELECT precio_monthly, agente_ia_activo, gestion_vendedores_activo
        FROM planes
        WHERE nombre = $1
        """,
        plan,
    )
    if plan_fila is None:
        log.error("Pago aprobado de un plan inexistente: %s", plan)
        return False

    renovacion = fecha_renovacion or datetime.now(timezone.utc) + timedelta(days=DIAS_CICLO)

    await conn.execute(
        """
        INSERT INTO tenant_subscriptions
            (tenant_id, plan, estado, precio_monthly, fecha_inicio,
             fecha_renovacion, intentos_fallidos, stripe_customer_id,
             stripe_subscription_id)
        VALUES ($1, $2, 'activa', $3, NOW(), $4, 0, $5, $6)
        ON CONFLICT (tenant_id) DO UPDATE SET
            plan                   = EXCLUDED.plan,
            estado                 = 'activa',
            precio_monthly         = EXCLUDED.precio_monthly,
            fecha_renovacion       = EXCLUDED.fecha_renovacion,
            intentos_fallidos      = 0,
            fecha_proximo_intento  = NULL,
            cancela_al_vencer      = false,
            cancelada_en           = NULL,
            stripe_customer_id     = COALESCE(EXCLUDED.stripe_customer_id,
                                              tenant_subscriptions.stripe_customer_id),
            stripe_subscription_id = COALESCE(EXCLUDED.stripe_subscription_id,
                                              tenant_subscriptions.stripe_subscription_id),
            -- Pagar durante (o después de) una prueba de gerencia la
            -- convierte en un plan pagado (26_plan_prueba.sql).
            origen                 = 'pago',
            otorgada_por           = NULL,
            updated_at             = NOW()
        """,
        tenant_id,
        plan,
        plan_fila["precio_monthly"],
        renovacion,
        stripe_customer_id,
        stripe_subscription_id,
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

    if referencia is not None:
        await cargar_creditos_plan(
            conn, tenant_id, plan, renovacion, referencia, f"Créditos del plan {plan}"
        )

    log.info("Suscripción %s activada para el tenant %s", plan, tenant_id)
    return True


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
