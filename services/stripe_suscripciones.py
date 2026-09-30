"""
Ciclo de vida de las suscripciones recurrentes de Stripe.

El plan se contrata con un Checkout en modo `subscription` sobre el Price
recurrente del plan (`planes.stripe_price_id`). A partir de ahí Stripe cobra
solo cada mes y avisa por webhook; este módulo traduce esos avisos a
`tenant_subscriptions` / `tenant_transactions` y decide qué se le avisa a
gerencia (services/notificaciones_gerencia.py).

    invoice.payment_succeeded       -> alta o renovación: extiende la vigencia
                                       hasta el fin del período cobrado
    invoice.payment_failed          -> cuenta el intento; NO pausa: si le
                                       quedan días pagados los conserva, y el
                                       job de pagos la pausa al vencer
    customer.subscription.updated   -> cancelación programada / revertida
    customer.subscription.deleted   -> baja: si le quedan días sigue vigente
                                       hasta fecha_renovacion y después la
                                       pausa el job; si no, se pausa ya

Nada de esto cambia el modelo de acceso: services/acceso_plan.py y
acceso_pagos.py siguen leyendo `estado` + `fecha_renovacion`, y
jobs/pagos_background.py sigue siendo quien pasa a 'pausada' lo que venció.

Idempotencia (Stripe reintenta y no garantiza orden):
  - cobros exitosos: una fila de tenant_transactions por factura
    (índice único de stripe_invoice_id, 27_stripe_suscripciones.sql);
  - cobros fallidos: `intentos_fallidos` guarda el attempt_count de Stripe y
    solo avanza, así que el mismo intento repetido no se cuenta dos veces;
  - cancelaciones: el UPDATE solo matchea si el estado cambia.
Solo se notifica cuando la escritura de verdad ocurrió.

Los procesadores escriben dentro de una transacción y dejan escapar las
excepciones: el webhook responde 500 y Stripe reintenta, en vez de dar por
bueno un cobro que no se entregó.

Formato de los objetos: desde la versión de API 2025-03-31 ("basil") la
Invoice ya no trae `subscription` ni `payment_intent` en la raíz, sino en
`parent.subscription_details` y `payments`. Los lectores de abajo aceptan
los dos formatos, así que no dependen de la versión configurada en el
endpoint del webhook.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable
from uuid import UUID

import asyncpg
from fastapi import HTTPException, status

from config import settings
from services.notificaciones_gerencia import EventoSuscripcion
from services.pagos import activar_suscripcion
from services.stripe_pagos import de_unidad_minima
from session import fetch_one, transaccion

log = logging.getLogger("operativai.pagos.stripe.suscripciones")


# ============================================================
# Antes del checkout
# ============================================================
@dataclass
class PreparacionCheckout:
    price_id: str
    # Customer de Stripe del tenant si ya fue cliente; None la primera vez.
    customer_id: str | None


async def preparar_checkout_suscripcion(tenant_id: UUID, plan: str) -> PreparacionCheckout:
    """
    Valida que el plan se pueda contratar en línea y que el tenant no tenga
    ya una suscripción recurrente viva. 409 en los dos casos, antes de crear
    la transacción pendiente.

    Una segunda Subscription encima de la primera cobraría dos veces cada
    mes. El cambio de plan no se hace desde acá (se acordó 409): se hace
    desde el panel de Stripe.
    """
    fila_plan = await fetch_one("SELECT stripe_price_id FROM planes WHERE nombre = $1", plan)
    if fila_plan is None or not fila_plan["stripe_price_id"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Este plan todavía no se puede contratar en línea. Escríbenos para activarlo.",
        )

    sub = await fetch_one(
        """
        SELECT plan, stripe_customer_id, stripe_subscription_id, cancelada_en,
               cancela_al_vencer
        FROM tenant_subscriptions
        WHERE tenant_id = $1
        """,
        tenant_id,
    )
    if sub is not None and sub["stripe_subscription_id"] and sub["cancelada_en"] is None:
        if sub["cancela_al_vencer"]:
            detalle = (
                "Tu suscripción está programada para cancelarse al final del período. "
                "Podrás contratar de nuevo cuando termine."
            )
        elif sub["plan"] == plan:
            detalle = "Ya tienes este plan con renovación automática: no hace falta renovarlo a mano."
        else:
            detalle = (
                "Ya tienes una suscripción con renovación automática. El cambio de plan "
                "todavía no está disponible en línea: escríbenos para hacerlo."
            )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detalle)

    return PreparacionCheckout(
        price_id=fila_plan["stripe_price_id"],
        customer_id=sub["stripe_customer_id"] if sub is not None else None,
    )


# ============================================================
# Lectura de los objetos de Stripe
# ============================================================
def _id(valor: Any) -> str | None:
    """Un campo de Stripe puede venir como id ("cus_...") o expandido ({id: ...})."""
    if isinstance(valor, dict):
        valor = valor.get("id")
    return str(valor) if valor else None


def _ts(valor: Any) -> datetime | None:
    """Timestamp unix de Stripe -> datetime con zona."""
    if valor in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(valor), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _uuid(valor: Any) -> UUID | None:
    try:
        return UUID(str(valor)) if valor else None
    except ValueError:
        return None


@dataclass
class Factura:
    id: str | None
    subscription_id: str | None
    customer_id: str | None
    billing_reason: str | None
    monto: Decimal | None
    moneda: str
    periodo_fin: datetime | None
    price_id: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    intento: int = 0
    proximo_intento: datetime | None = None
    payment_intent_id: str | None = None
    motivo: str | None = None


def leer_factura(obj: dict[str, Any], *, pagada: bool) -> Factura:
    """
    Normaliza una Invoice (formato viejo y "basil").

    `pagada` elige el monto: amount_paid para un cobro exitoso, amount_due
    para uno fallido (el que no se pudo cobrar).
    """
    detalles_nuevos = (obj.get("parent") or {}).get("subscription_details") or {}
    detalles_viejos = obj.get("subscription_details") or {}
    lineas = (obj.get("lines") or {}).get("data") or []

    # La línea del período más lejano es el servicio que se acaba de
    # cobrar. (Con prorrateos puede haber varias; la del plan es la última.)
    linea: dict[str, Any] = {}
    if lineas:
        linea = max(lineas, key=lambda l: int((l.get("period") or {}).get("end") or 0))

    metadata = (
        detalles_nuevos.get("metadata")
        or detalles_viejos.get("metadata")
        or linea.get("metadata")
        or {}
    )

    price_id = _id(linea.get("price")) or (
        ((linea.get("pricing") or {}).get("price_details") or {}).get("price")
    )

    payment_intent = _id(obj.get("payment_intent"))
    if payment_intent is None:
        pagos = (obj.get("payments") or {}).get("data") or []
        if pagos:
            payment_intent = _id((pagos[0].get("payment") or {}).get("payment_intent"))

    moneda = str(obj.get("currency") or settings.STRIPE_CURRENCY).lower()
    crudo = obj.get("amount_paid") if pagada else obj.get("amount_due")
    monto = de_unidad_minima(int(crudo), moneda) if crudo is not None else None

    return Factura(
        id=_id(obj.get("id")),
        subscription_id=_id(obj.get("subscription")) or _id(detalles_nuevos.get("subscription")),
        customer_id=_id(obj.get("customer")),
        billing_reason=obj.get("billing_reason"),
        monto=monto,
        moneda=moneda,
        periodo_fin=_ts((linea.get("period") or {}).get("end")),
        price_id=str(price_id) if price_id else None,
        metadata=dict(metadata),
        intento=int(obj.get("attempt_count") or 0),
        proximo_intento=_ts(obj.get("next_payment_attempt")),
        payment_intent_id=payment_intent,
        motivo=(obj.get("last_finalization_error") or {}).get("message"),
    )


@dataclass
class Suscripcion:
    id: str | None
    customer_id: str | None
    status: str | None
    # cancel_at_period_end o una fecha de baja programada (cancel_at).
    cancela: bool
    cancela_en: datetime | None
    motivo: str | None


def leer_suscripcion(obj: dict[str, Any]) -> Suscripcion:
    detalles = obj.get("cancellation_details") or {}
    motivo = ", ".join(
        str(v) for v in (detalles.get("reason"), detalles.get("feedback"), detalles.get("comment")) if v
    )
    cancel_at = _ts(obj.get("cancel_at"))
    return Suscripcion(
        id=_id(obj.get("id")),
        customer_id=_id(obj.get("customer")),
        status=obj.get("status"),
        cancela=bool(obj.get("cancel_at_period_end")) or cancel_at is not None,
        cancela_en=cancel_at,
        motivo=motivo or None,
    )


# ============================================================
# De qué tenant y de qué plan es
# ============================================================
async def _tenant_de(
    conn: asyncpg.Connection,
    metadata: dict[str, Any],
    subscription_id: str | None,
    customer_id: str | None,
) -> UUID | None:
    """
    Primero la metadata que pusimos al crear el checkout; si no está (una
    Subscription creada a mano en el panel de Stripe), los ids ya enlazados.
    """
    desde_metadata = _uuid(metadata.get("tenant_id"))
    if desde_metadata is not None:
        existe = await conn.fetchval("SELECT 1 FROM tenants WHERE id = $1", desde_metadata)
        if existe:
            return desde_metadata

    if subscription_id:
        tenant = await conn.fetchval(
            "SELECT tenant_id FROM tenant_subscriptions WHERE stripe_subscription_id = $1",
            subscription_id,
        )
        if tenant:
            return tenant

    if customer_id:
        return await conn.fetchval(
            "SELECT tenant_id FROM tenant_subscriptions WHERE stripe_customer_id = $1 LIMIT 1",
            customer_id,
        )
    return None


async def _plan_de(
    conn: asyncpg.Connection, price_id: str | None, metadata: dict[str, Any], tenant_id: UUID
) -> str | None:
    """
    El Price cobrado manda: si alguien cambió el plan desde el panel de
    Stripe, la factura trae el Price nuevo aunque la metadata siga diciendo
    el viejo.
    """
    if price_id:
        plan = await conn.fetchval(
            "SELECT nombre FROM planes WHERE stripe_price_id = $1 ORDER BY activo DESC LIMIT 1",
            price_id,
        )
        if plan:
            return plan

    del_metadata = metadata.get("plan")
    if del_metadata:
        plan = await conn.fetchval("SELECT nombre FROM planes WHERE nombre = $1", del_metadata)
        if plan:
            return plan

    return await conn.fetchval("SELECT plan FROM tenant_subscriptions WHERE tenant_id = $1", tenant_id)


# ============================================================
# invoice.payment_succeeded
# ============================================================
async def _registrar_cobro_aprobado(
    conn: asyncpg.Connection,
    factura: Factura,
    tenant_id: UUID,
    plan: str,
    transaccion_inicial: UUID | None,
) -> bool:
    """
    Deja la factura como cobro aprobado. False = esta factura ya estaba
    registrada como aprobada (evento repetido): no hay que hacer nada más.
    """
    monto = factura.monto if factura.monto is not None else Decimal(0)

    # El primer cobro ya tiene fila: la creó crear-pago antes de abrir el
    # checkout. Se completa esa en vez de crear otra, para que el historial
    # muestre un solo cobro por el alta.
    if transaccion_inicial is not None:
        fila = await conn.fetchrow(
            """
            UPDATE tenant_transactions
               SET estado_pago              = 'aprobado',
                   stripe_invoice_id        = $3,
                   monto                    = $4,
                   stripe_payment_intent_id = COALESCE($5, stripe_payment_intent_id),
                   updated_at               = NOW()
             WHERE id = $1 AND tenant_id = $2 AND tipo = 'subscription'
               AND (stripe_invoice_id IS NULL
                    OR (stripe_invoice_id = $3 AND estado_pago <> 'aprobado'))
            RETURNING id
            """,
            transaccion_inicial,
            tenant_id,
            factura.id,
            monto,
            factura.payment_intent_id,
        )
        if fila is not None:
            return True
        # Si no matcheó, o ya estaba procesada (el upsert de abajo lo
        # detecta por el índice único) o la fila no existe (se registra como
        # un cobro nuevo).

    concepto = (
        f"Renovación {plan}" if factura.billing_reason == "subscription_cycle" else f"Suscripción {plan}"
    )
    # ON CONFLICT ... DO UPDATE: una factura que antes falló (fila
    # 'rechazado') y ahora se cobró en un reintento pasa a 'aprobado'. Una
    # que ya estaba aprobada no matchea el WHERE y no devuelve fila.
    fila = await conn.fetchrow(
        """
        INSERT INTO tenant_transactions
            (tenant_id, tipo, concepto, monto, estado_pago, plan_nombre,
             stripe_invoice_id, stripe_payment_intent_id)
        VALUES ($1, 'subscription', $2, $3, 'aprobado', $4, $5, $6)
        ON CONFLICT (stripe_invoice_id) WHERE stripe_invoice_id IS NOT NULL
        DO UPDATE SET
            estado_pago              = 'aprobado',
            monto                    = EXCLUDED.monto,
            stripe_payment_intent_id = COALESCE(EXCLUDED.stripe_payment_intent_id,
                                                tenant_transactions.stripe_payment_intent_id),
            updated_at               = NOW()
        WHERE tenant_transactions.estado_pago <> 'aprobado'
        RETURNING id
        """,
        tenant_id,
        concepto,
        monto,
        plan,
        factura.id,
        factura.payment_intent_id,
    )
    return fila is not None


async def procesar_factura_pagada(obj: dict[str, Any]) -> EventoSuscripcion | None:
    """
    Alta o renovación cobrada: registra el cobro y extiende la vigencia
    hasta el fin del período que Stripe acaba de cobrar. Si la suscripción
    estaba pausada por cobros fallidos, esto la reactiva.
    """
    factura = leer_factura(obj, pagada=True)
    if not factura.subscription_id or not factura.id:
        # Factura suelta (no de una suscripción): no es nuestra.
        log.info("Factura de Stripe sin suscripción, ignorada: %s", factura.id)
        return None

    es_alta = factura.billing_reason == "subscription_create"

    async with transaccion() as conn:
        tenant_id = await _tenant_de(
            conn, factura.metadata, factura.subscription_id, factura.customer_id
        )
        if tenant_id is None:
            log.warning(
                "Factura pagada de una suscripción que no es de ningún tenant: %s (%s)",
                factura.id, factura.subscription_id,
            )
            return None

        # Una factura tardía de una Subscription vieja no puede pisar la
        # vigente (p. ej. la última factura de una ya reemplazada).
        actual = await conn.fetchrow(
            "SELECT stripe_subscription_id, cancelada_en FROM tenant_subscriptions WHERE tenant_id = $1",
            tenant_id,
        )
        if (
            not es_alta
            and actual is not None
            and actual["stripe_subscription_id"]
            and actual["stripe_subscription_id"] != factura.subscription_id
            and actual["cancelada_en"] is None
        ):
            log.warning(
                "Factura %s de la suscripción %s, pero la vigente del tenant %s es %s: ignorada",
                factura.id, factura.subscription_id, tenant_id, actual["stripe_subscription_id"],
            )
            return None

        plan = await _plan_de(conn, factura.price_id, factura.metadata, tenant_id)
        if plan is None:
            log.error("Factura pagada %s sin plan reconocible (price=%s)", factura.id, factura.price_id)
            return None

        transaccion_inicial = _uuid(factura.metadata.get("transaccion_id")) if es_alta else None
        nueva = await _registrar_cobro_aprobado(conn, factura, tenant_id, plan, transaccion_inicial)
        if not nueva:
            log.info("Factura de Stripe %s ya procesada; no se repite", factura.id)
            return None

        await activar_suscripcion(
            tenant_id,
            plan,
            fecha_renovacion=factura.periodo_fin,
            stripe_customer_id=factura.customer_id,
            stripe_subscription_id=factura.subscription_id,
            conn=conn,
        )
        fecha_renovacion = await conn.fetchval(
            "SELECT fecha_renovacion FROM tenant_subscriptions WHERE tenant_id = $1", tenant_id
        )

    return EventoSuscripcion(
        tipo="alta" if es_alta else "renovacion",
        tenant_id=tenant_id,
        plan=plan,
        monto=factura.monto,
        moneda=factura.moneda,
        fecha_renovacion=fecha_renovacion,
        accion=f"Plan activo; se vuelve a cobrar solo el {fecha_renovacion:%d/%m/%Y}."
        if fecha_renovacion
        else "Plan activo.",
        stripe_customer_id=factura.customer_id,
        stripe_subscription_id=factura.subscription_id,
        stripe_invoice_id=factura.id,
    )


# ============================================================
# invoice.payment_failed
# ============================================================
async def procesar_factura_fallida(obj: dict[str, Any]) -> EventoSuscripcion | None:
    """
    Cobro de renovación rechazado. No se toca `estado` ni `fecha_renovacion`:
    lo que ya pagó lo conserva. Cuando la fecha pasa sin un cobro exitoso,
    jobs/pagos_background.py la pausa como a cualquier vencida; si un
    reintento de Stripe sale bien, procesar_factura_pagada la reactiva.
    """
    factura = leer_factura(obj, pagada=False)
    if not factura.subscription_id or not factura.id:
        return None

    if factura.billing_reason == "subscription_create":
        # El primer cobro se hace en el checkout, con el dueño mirando: si
        # falla, Stripe se lo dice ahí y la sesión sigue abierta para otra
        # tarjeta. La transacción queda pendiente hasta que pague o la
        # sesión venza (checkout.session.expired la cancela).
        log.info("Falló el primer cobro de %s en el checkout; se espera", factura.subscription_id)
        return None

    intento = max(factura.intento, 1)

    async with transaccion() as conn:
        tenant_id = await _tenant_de(
            conn, factura.metadata, factura.subscription_id, factura.customer_id
        )
        if tenant_id is None:
            log.warning("Cobro fallido de una suscripción ajena: %s", factura.subscription_id)
            return None

        # `intentos_fallidos < $3`: el mismo intento reenviado por Stripe no
        # vuelve a matchear. Solo sobre la Subscription vigente del tenant.
        sub = await conn.fetchrow(
            """
            UPDATE tenant_subscriptions
               SET intentos_fallidos     = $3,
                   fecha_proximo_intento = $4,
                   updated_at            = NOW()
             WHERE tenant_id = $1
               AND stripe_subscription_id = $2
               AND intentos_fallidos < $3
            RETURNING plan, estado, fecha_renovacion, stripe_customer_id
            """,
            tenant_id,
            factura.subscription_id,
            intento,
            factura.proximo_intento,
        )
        if sub is None:
            log.info(
                "Cobro fallido %s (intento %s) ya registrado o de otra suscripción",
                factura.id, intento,
            )
            return None

        # Una fila por factura en el historial. Si ya existía (intento
        # anterior), solo se toca updated_at: sigue 'rechazado'.
        await conn.execute(
            """
            INSERT INTO tenant_transactions
                (tenant_id, tipo, concepto, monto, estado_pago, plan_nombre,
                 stripe_invoice_id)
            VALUES ($1, 'subscription', $2, $3, 'rechazado', $4, $5)
            ON CONFLICT (stripe_invoice_id) WHERE stripe_invoice_id IS NOT NULL
            DO UPDATE SET updated_at = NOW()
            WHERE tenant_transactions.estado_pago <> 'aprobado'
            """,
            tenant_id,
            f"Renovación {sub['plan']}",
            factura.monto if factura.monto is not None else Decimal(0),
            sub["plan"],
            factura.id,
        )

    vence = sub["fecha_renovacion"]
    if sub["estado"] == "activa" and vence and vence > datetime.now(timezone.utc):
        accion = (
            f"Sigue vigente hasta el {vence:%d/%m/%Y}. Si Stripe no logra cobrar antes, "
            "se pausa automáticamente al vencer."
        )
    else:
        accion = (
            "Ya no le quedan días pagados: queda (o quedará en la próxima pasada del job) "
            "pausada. Se reactiva sola si Stripe logra cobrar en un reintento."
        )
    if factura.proximo_intento is None:
        accion += " Stripe no va a reintentar el cobro."

    return EventoSuscripcion(
        tipo="pago_fallido",
        tenant_id=tenant_id,
        plan=sub["plan"],
        monto=factura.monto,
        moneda=factura.moneda,
        fecha_renovacion=vence,
        intento=intento,
        proximo_intento=factura.proximo_intento,
        motivo=factura.motivo,
        accion=accion,
        stripe_customer_id=factura.customer_id or sub["stripe_customer_id"],
        stripe_subscription_id=factura.subscription_id,
        stripe_invoice_id=factura.id,
    )


# ============================================================
# customer.subscription.updated
# ============================================================
async def procesar_suscripcion_actualizada(obj: dict[str, Any]) -> EventoSuscripcion | None:
    """
    Solo interesa la cancelación programada (o que se deshaga). El resto de
    los cambios (status past_due, etc.) ya llegan como eventos de factura.
    La suscripción sigue 'activa' hasta fecha_renovacion en los dos casos.
    """
    s = leer_suscripcion(obj)
    if not s.id:
        return None

    fila = await fetch_one(
        """
        UPDATE tenant_subscriptions
           SET cancela_al_vencer = $2, updated_at = NOW()
         WHERE stripe_subscription_id = $1
           AND cancelada_en IS NULL
           AND cancela_al_vencer IS DISTINCT FROM $2
        RETURNING tenant_id, plan, fecha_renovacion, stripe_customer_id
        """,
        s.id,
        s.cancela,
    )
    if fila is None:
        return None

    vence = fila["fecha_renovacion"]
    if s.cancela:
        accion = (
            f"Sigue vigente hasta el {vence:%d/%m/%Y} y después se pausa automáticamente."
            if vence
            else "Se pausa automáticamente al vencer."
        )
        if s.cancela_en and vence and s.cancela_en < vence:
            accion += f" Stripe la da de baja el {s.cancela_en:%d/%m/%Y}, pero se respetan los días pagados."
    else:
        accion = "Vuelve a renovarse automáticamente."

    return EventoSuscripcion(
        tipo="cancelacion_programada" if s.cancela else "cancelacion_revertida",
        tenant_id=fila["tenant_id"],
        plan=fila["plan"],
        fecha_renovacion=vence,
        motivo=s.motivo,
        accion=accion,
        stripe_customer_id=s.customer_id or fila["stripe_customer_id"],
        stripe_subscription_id=s.id,
    )


# ============================================================
# customer.subscription.deleted
# ============================================================
async def procesar_suscripcion_eliminada(obj: dict[str, Any]) -> EventoSuscripcion | None:
    """
    Stripe dio de baja la Subscription (al final del período, de inmediato
    desde el panel, o por agotar los reintentos de cobro).

    Si todavía le quedan días pagados (fecha_renovacion en el futuro), sigue
    'activa' hasta entonces y la pausa el job al vencer. Si no, se pausa
    ahora. Nunca 'cancelada': se acordó que una suscripción cancelada que
    vence queda pausada, igual que una vencida sin pagar.
    """
    s = leer_suscripcion(obj)
    if not s.id:
        return None

    fila = await fetch_one(
        """
        UPDATE tenant_subscriptions
           SET cancelada_en      = NOW(),
               cancela_al_vencer = true,
               estado = CASE
                   WHEN estado = 'activa'
                        AND (fecha_renovacion IS NULL OR fecha_renovacion <= NOW())
                   THEN 'pausada'
                   ELSE estado
               END,
               updated_at = NOW()
         WHERE stripe_subscription_id = $1
           AND cancelada_en IS NULL
        RETURNING tenant_id, plan, estado, fecha_renovacion, stripe_customer_id
        """,
        s.id,
    )
    if fila is None:
        # Nunca se enlazó (el primer cobro no llegó a hacerse) o ya estaba
        # procesada.
        return None

    vence = fila["fecha_renovacion"]
    if fila["estado"] == "activa":
        accion = f"Le quedan días pagados: sigue vigente hasta el {vence:%d/%m/%Y} y después se pausa automáticamente."
    else:
        accion = "No le quedaban días pagados: la suscripción se pausó de inmediato."

    return EventoSuscripcion(
        tipo="cancelada",
        tenant_id=fila["tenant_id"],
        plan=fila["plan"],
        fecha_renovacion=vence,
        motivo=s.motivo,
        accion=accion,
        stripe_customer_id=s.customer_id or fila["stripe_customer_id"],
        stripe_subscription_id=s.id,
    )


# Tipo de evento -> procesador. Lo que no esté acá lo sigue manejando el
# webhook como antes (checkout.session.*, charge.refunded) o lo ignora.
PROCESADORES: dict[str, Callable[[dict[str, Any]], Awaitable[EventoSuscripcion | None]]] = {
    "invoice.payment_succeeded": procesar_factura_pagada,
    "invoice.payment_failed": procesar_factura_fallida,
    "customer.subscription.updated": procesar_suscripcion_actualizada,
    "customer.subscription.deleted": procesar_suscripcion_eliminada,
}
