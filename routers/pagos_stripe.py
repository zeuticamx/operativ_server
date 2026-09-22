"""
Webhook de Stripe.

Vive aparte de routers/pagos.py por simetría con el de Mercado Pago: cada
pasarela tiene su URL, su formato y su firma, y mezclarlas en un mismo
endpoint obligaría a adivinar de quién es cada POST.

    Mercado Pago -> POST /api/pagos/webhook         (inhabilitado)
    Stripe       -> POST /api/pagos/stripe/webhook

Es el único endpoint del módulo sin JWT: se autentica por el HMAC de la
cabecera `Stripe-Signature`. Todo lo que entregue lo compró alguien, así
que la firma es lo único que separa un cobro real de un regalo.
"""

import json
import logging
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status

from config import settings
from services.pagos import procesar_pago_aprobado
from services.stripe_pagos import firma_valida
from session import execute, fetch_one

router = APIRouter(prefix="/pagos/stripe", tags=["pagos"])

log = logging.getLogger("operativai.pagos.stripe")

# Eventos de Checkout -> estado del CHECK de tenant_transactions.
#
# `checkout.session.completed` no siempre significa "pagado": con métodos
# asíncronos (OXXO y SPEI en México, que es la moneda por defecto) la
# sesión se completa cuando se genera el voucher y el dinero llega horas
# después, con un async_payment_succeeded aparte. Por eso ese evento se
# resuelve mirando `payment_status` y no se da por aprobado de entrada.
ESTADOS_SESION: dict[str, str] = {
    "checkout.session.async_payment_succeeded": "aprobado",
    "checkout.session.async_payment_failed": "rechazado",
    "checkout.session.expired": "cancelado",
}

# payment_status de una sesión completada -> estado nuestro.
ESTADOS_PAGO_SESION: dict[str, str] = {
    "paid": "aprobado",
    "no_payment_required": "aprobado",
    "unpaid": "pendiente",
}


@router.post("/webhook", status_code=200)
async def webhook(
    request: Request,
    tareas: BackgroundTasks,
    stripe_signature: str | None = Header(default=None, alias="stripe-signature"),
) -> dict[str, bool]:
    """
    Recibe el evento de Stripe y actualiza la transacción.

    Devuelve 200 en casi todos los caminos a propósito: Stripe reintenta
    ante cualquier respuesta que no sea 2xx, y reintentar no arregla un
    evento de un tipo que no nos interesa o de un pago que no es nuestro.
    Solo la firma inválida corta con 401.
    """
    if not settings.STRIPE_WEBHOOK_SECRET:
        # Fallar cerrado: sin secreto no hay forma de distinguir un evento
        # real de uno inventado, y este endpoint mueve dinero.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El webhook de Stripe no está configurado",
        )

    # El cuerpo crudo, no el JSON ya parseado: el HMAC se calcula sobre los
    # bytes tal cual llegaron y volver a serializarlos rompe la firma.
    cuerpo_crudo = await request.body()

    if not firma_valida(stripe_signature, cuerpo_crudo):
        log.warning("Evento de Stripe con firma inválida")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firma inválida",
        )

    try:
        evento = json.loads(cuerpo_crudo)
    except json.JSONDecodeError:
        log.warning("Evento de Stripe con cuerpo ilegible")
        return {"recibido": True}

    tipo = str(evento.get("type", ""))
    objeto = (evento.get("data") or {}).get("object") or {}

    if tipo == "charge.refunded":
        await _procesar_reembolso(objeto)
        return {"recibido": True}

    if tipo != "checkout.session.completed" and tipo not in ESTADOS_SESION:
        log.info("Evento de Stripe ignorado (type=%s)", tipo)
        return {"recibido": True}

    transaccion_id = _referencia(objeto)
    if transaccion_id is None:
        return {"recibido": True}

    if tipo == "checkout.session.completed":
        nuevo_estado = ESTADOS_PAGO_SESION.get(
            str(objeto.get("payment_status")), "pendiente"
        )
    else:
        nuevo_estado = ESTADOS_SESION[tipo]

    # payment_method_types trae "card", "oxxo", "spei"... El último 4 de la
    # tarjeta no viaja en la sesión (haría falta expandir el PaymentMethod
    # con otra llamada), así que esa columna queda en NULL con Stripe: el
    # historial del portal ya la muestra como opcional.
    metodos = objeto.get("payment_method_types") or []
    metodo = metodos[0] if metodos else None

    # El WHERE no deja retroceder de estado. Stripe no garantiza el orden de
    # entrega y reintenta lo que falla, así que un
    # `checkout.session.completed` con payment_status=unpaid puede llegar
    # DESPUÉS del async_payment_succeeded que ya aprobó el cobro: sin esta
    # guarda, ese reintento dejaría como 'pendiente' un pago ya cobrado y
    # entregado. Solo se escribe si la fila sigue pendiente, o si el evento
    # repite el estado que ya tiene.
    fila = await fetch_one(
        """
        UPDATE tenant_transactions
           SET estado_pago              = $2,
               stripe_session_id        = COALESCE(stripe_session_id, $3),
               stripe_payment_intent_id = COALESCE($4, stripe_payment_intent_id),
               metodo_pago              = COALESCE($5, metodo_pago),
               updated_at               = NOW()
         WHERE id = $1
           AND (estado_pago = 'pendiente' OR estado_pago = $2)
        RETURNING id, tenant_id
        """,
        transaccion_id,
        nuevo_estado,
        str(objeto.get("id")) if objeto.get("id") else None,
        str(objeto["payment_intent"]) if objeto.get("payment_intent") else None,
        metodo,
    )

    if fila is None:
        # O la fila no es nuestra, o el evento llegó tarde y habría hecho
        # retroceder un estado ya definitivo. Ninguno de los dos se arregla
        # reintentando.
        existente = await fetch_one(
            "SELECT estado_pago FROM tenant_transactions WHERE id = $1",
            transaccion_id,
        )
        if existente is None:
            log.warning(
                "Evento de Stripe para una transacción inexistente: %s", transaccion_id
            )
        else:
            log.info(
                "Evento de Stripe descartado por llegar tarde (%s): %s -> %s",
                transaccion_id, existente["estado_pago"], nuevo_estado,
            )
        return {"recibido": True}

    if nuevo_estado == "aprobado":
        # En background para contestarle rápido a Stripe: si tardamos, da el
        # evento por fallido y lo reintenta.
        tareas.add_task(procesar_pago_aprobado, transaccion_id)

    return {"recibido": True}


def _referencia(sesion: dict) -> UUID | None:
    """
    Saca el id de nuestra transacción de la sesión de Stripe.

    `client_reference_id` es lo que mandamos al crear el Checkout; metadata
    es el respaldo por si alguna sesión se creó desde el panel de Stripe a
    mano.
    """
    crudo = sesion.get("client_reference_id") or (sesion.get("metadata") or {}).get(
        "transaccion_id"
    )
    if not crudo:
        log.warning("Sesión de Stripe sin client_reference_id: %s", sesion.get("id"))
        return None

    try:
        return UUID(str(crudo))
    except ValueError:
        log.warning("client_reference_id no es un UUID: %r", crudo)
        return None


async def _procesar_reembolso(cargo: dict) -> None:
    """
    Marca como reembolsada la transacción del cargo devuelto.

    Se busca por el PaymentIntent y no por metadata: el intent se guarda
    cuando el pago se confirma, así que es un dato nuestro y no depende de
    que Stripe propague la metadata al cargo.

    No devuelve créditos ni apaga el plan: quitar algo ya entregado es una
    decisión de negocio que nadie tomó todavía. Lo que sí queda es el
    estado real en el historial.
    """
    intent = cargo.get("payment_intent")
    if not intent:
        return

    resultado = await execute(
        """
        UPDATE tenant_transactions
           SET estado_pago = 'reembolsado', updated_at = NOW()
         WHERE stripe_payment_intent_id = $1
        """,
        str(intent),
    )
    log.info("Reembolso de Stripe aplicado (intent=%s): %s", intent, resultado)
