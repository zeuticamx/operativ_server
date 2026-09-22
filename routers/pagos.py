"""
Cobros: suscripción por plan + compra de créditos.

La pasarela activa la decide `PAYMENT_PROVIDER` (config.py). Hoy es
**Stripe**; Mercado Pago sigue entero en este archivo pero inhabilitado —
no se borró nada, así que volver a MP es cambiar esa variable de entorno.

Qué vive dónde:

    services/pagos.py        cotizar + entregar lo comprado (sin proveedor)
    services/stripe_pagos.py llamadas a la API de Stripe + firma del webhook
    este archivo             endpoints del portal, y el camino de Mercado
                             Pago (crear preferencia + su webhook)
    routers/pagos_stripe.py  el webhook de Stripe

Las dos reglas del módulo (el precio sale de la base, y lo que da por bueno
un pago es el webhook y no el regreso del navegador) están explicadas en el
docstring de services/pagos.py.
"""

import hashlib
import hmac
import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status

from config import settings
from deps import UsuarioActual, gerencia_actual, tenant_actual
from schemas import (
    CatalogoPagosOut,
    CheckoutEstadoOut,
    CrearPagoIn,
    CrearPagoOut,
    PaqueteCreditosOut,
    PlanOut,
    SuscripcionOut,
    TransaccionOut,
)
from services.pagos import (
    cotizar,
    marcar_cancelada,
    procesar_pago_aprobado,
)
from services.stripe_pagos import crear_checkout_session
from session import execute, fetch_all, fetch_one, fetch_value

router = APIRouter(prefix="/pagos", tags=["pagos"])

log = logging.getLogger("operativai.pagos")

MP_API = "https://api.mercadopago.com"
TIMEOUT = httpx.Timeout(15.0)

HISTORIAL_LIMITE = 50

# Estados de Mercado Pago -> los del CHECK de tenant_transactions.
# 'authorized' y 'in_process' cuentan como pendientes: el dinero todavía no
# está. 'in_mediation' también: hay una disputa abierta y el desenlace llega
# después como approved o refunded.
ESTADOS_MP: dict[str, str] = {
    "approved": "aprobado",
    "pending": "pendiente",
    "in_process": "pendiente",
    "authorized": "pendiente",
    "in_mediation": "pendiente",
    "rejected": "rechazado",
    "cancelled": "cancelado",
    "refunded": "reembolsado",
    "charged_back": "reembolsado",
}


def _exigir_pasarela() -> None:
    """
    503 si la pasarela activa no tiene credenciales, en vez de un 500 al
    llamar a su API.
    """
    if settings.stripe_activo:
        if not settings.stripe_configurado:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Los cobros no están configurados (falta STRIPE_SECRET_KEY)",
            )
        return

    if not settings.mercadopago_configurado:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Los cobros no están configurados (falta MERCADOPAGO_ACCESS_TOKEN)",
        )


def _headers_mp() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.MERCADOPAGO_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


# ============================================================
# CATÁLOGO
# ============================================================
@router.get("/catalogo", response_model=CatalogoPagosOut)
async def catalogo(_tenant_id: UUID = Depends(tenant_actual)) -> CatalogoPagosOut:
    """Planes y paquetes de créditos para pintar la pantalla de suscripción."""
    filas_planes = await fetch_all(
        """
        SELECT nombre, descripcion, precio_monthly, precio_annual,
               max_vendedores, max_leads_mensuales, creditos_incluidos_mensual,
               agente_ia_activo, gestion_vendedores_activo
        FROM planes
        WHERE activo
        ORDER BY orden, precio_monthly
        """
    )
    filas_paquetes = await fetch_all(
        """
        SELECT creditos, precio
        FROM paquetes_creditos
        WHERE activo
        ORDER BY orden, creditos
        """
    )

    return CatalogoPagosOut(
        planes=[PlanOut(**dict(f)) for f in filas_planes],
        paquetes=[PaqueteCreditosOut(**dict(f)) for f in filas_paquetes],
    )


# ============================================================
# CREAR PAGO
# ============================================================
@router.post("/crear-pago", response_model=CrearPagoOut, status_code=201)
async def crear_pago(
    datos: CrearPagoIn,
    usuario: UsuarioActual = Depends(gerencia_actual),
) -> CrearPagoOut:
    """
    Abre el checkout de la pasarela activa y deja la transacción en
    'pendiente'.

    Solo gerencia (owner/superadmin): contratar un plan cambia lo que paga
    el negocio, no es una acción de 'member'.
    """
    _exigir_pasarela()

    if usuario.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El usuario no tiene un negocio asociado todavía",
        )

    monto, concepto = await cotizar(datos)

    # La fila se crea antes de llamar a la pasarela porque su id es la
    # referencia que le mandamos (client_reference_id en Stripe,
    # external_reference en Mercado Pago). Queda 'pendiente' hasta que el
    # webhook diga otra cosa.
    transaccion_id: UUID = await fetch_value(
        """
        INSERT INTO tenant_transactions
            (tenant_id, tipo, concepto, monto, estado_pago, plan_nombre, creditos_comprados)
        VALUES ($1, $2, $3, $4, 'pendiente', $5, $6)
        RETURNING id
        """,
        usuario.tenant_id,
        datos.tipo,
        concepto,
        monto,
        datos.plan,
        datos.creditos,
    )

    if settings.stripe_activo:
        return await _crear_pago_stripe(transaccion_id, concepto, monto, usuario.email)
    return await _crear_pago_mercadopago(transaccion_id, concepto, monto, usuario.email)


async def _crear_pago_stripe(
    transaccion_id: UUID, concepto: str, monto: Decimal, email: str
) -> CrearPagoOut:
    try:
        sesion = await crear_checkout_session(transaccion_id, concepto, monto, email)
    except HTTPException:
        # Sin checkout no hay nada que cobrar: la fila pendiente se cierra
        # para que no quede colgada en el historial del cliente para siempre.
        await marcar_cancelada(transaccion_id)
        raise

    session_id = str(sesion.get("id", ""))
    url = sesion.get("url")
    if not session_id or not url:
        await marcar_cancelada(transaccion_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Stripe no devolvió un checkout válido",
        )

    await execute(
        """
        UPDATE tenant_transactions
           SET stripe_session_id = $2, updated_at = NOW()
         WHERE id = $1
        """,
        transaccion_id,
        session_id,
    )

    return CrearPagoOut(
        transaccion_id=transaccion_id,
        proveedor="stripe",
        referencia=session_id,
        checkout_url=url,
        monto=monto,
        concepto=concepto,
    )


# ============================================================
# MERCADO PAGO (inhabilitado; ver PAYMENT_PROVIDER)
# ============================================================
# Nada de acá para abajo corre mientras PAYMENT_PROVIDER sea "stripe". Se
# conserva completo y funcionando a propósito: es el camino de vuelta si
# Stripe no sirve para algún mercado, y las transacciones viejas de MP
# siguen en el historial.
def _payload_preferencia(
    transaccion_id: UUID, concepto: str, monto: Decimal, email: str
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "items": [
            {
                "id": str(transaccion_id),
                "title": concepto,
                "quantity": 1,
                "unit_price": float(monto),
                "currency_id": settings.MERCADOPAGO_CURRENCY,
            }
        ],
        "payer": {"email": email},
        "back_urls": {
            "success": f"{settings.BASE_URL_FRONTEND}/pagos/exito",
            "pending": f"{settings.BASE_URL_FRONTEND}/pagos/exito",
            "failure": f"{settings.BASE_URL_FRONTEND}/pagos/error",
        },
        "notification_url": f"{settings.BASE_URL_BACKEND}/api/pagos/webhook",
        # El hilo que une el pago de Mercado Pago con nuestra fila. El
        # webhook no trae tenant_id: lo reconstruye desde acá.
        "external_reference": str(transaccion_id),
        "statement_descriptor": "OPERATIVAI",
    }

    # `auto_return` exige que back_urls.success sea una URL pública de
    # verdad: con localhost, Mercado Pago lo rechaza con un 400 confuso
    # ("auto_return invalid. back_url.success must be defined", aunque sí
    # está definida — es la validación de auto_return la que falla, no la
    # ausencia del campo). En local (BASE_URL_FRONTEND sin https) se omite
    # el campo: la preferencia se crea igual, solo que Mercado Pago no
    # redirige solo al terminar — el comprador vuelve a mano.
    if settings.BASE_URL_FRONTEND.startswith("https://"):
        payload["auto_return"] = "approved"

    return payload


async def _crear_pago_mercadopago(
    transaccion_id: UUID, concepto: str, monto: Decimal, email: str
) -> CrearPagoOut:
    payload = _payload_preferencia(transaccion_id, concepto, monto, email)

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
            respuesta = await cliente.post(
                f"{MP_API}/checkout/preferences",
                headers=_headers_mp(),
                json=payload,
            )
        respuesta.raise_for_status()
        preferencia = respuesta.json()
    except httpx.HTTPError as e:
        # Sin preferencia no hay nada que cobrar: la fila pendiente se cierra
        # para que no quede colgada en el historial del cliente para siempre.
        await marcar_cancelada(transaccion_id)
        # `str(e)` de un HTTPStatusError no trae el body de la respuesta, y
        # ahí es donde Mercado Pago dice la causa real (p. ej. "auto_return
        # invalid. back_url.success must be defined"). Sin esto, cada 400 de
        # Mercado Pago hay que reproducirlo a mano para saber qué pasó.
        cuerpo = e.response.text if isinstance(e, httpx.HTTPStatusError) else ""
        log.error(
            "Mercado Pago rechazó la preferencia (%s): %s | body=%s",
            transaccion_id, e, cuerpo,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo iniciar el pago con Mercado Pago. Inténtalo de nuevo.",
        )

    mp_preference_id = str(preferencia.get("id", ""))
    # sandbox_init_point solo viene poblado con credenciales de prueba; con
    # credenciales productivas el que sirve es init_point.
    init_point = preferencia.get("init_point") or preferencia.get("sandbox_init_point")
    if not mp_preference_id or not init_point:
        await marcar_cancelada(transaccion_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Mercado Pago no devolvió un checkout válido",
        )

    await execute(
        """
        UPDATE tenant_transactions
           SET mp_preference_id = $2, updated_at = NOW()
         WHERE id = $1
        """,
        transaccion_id,
        mp_preference_id,
    )

    return CrearPagoOut(
        transaccion_id=transaccion_id,
        proveedor="mercadopago",
        referencia=mp_preference_id,
        checkout_url=init_point,
        monto=monto,
        concepto=concepto,
    )


def _firma_valida(x_signature: str | None, x_request_id: str | None, data_id: str) -> bool:
    """
    Valida el HMAC que manda Mercado Pago en `x-signature`.

    La cabecera viene como "ts=1704908010,v1=abc123...". El manifiesto que
    se firma es exactamente `id:{data.id};request-id:{x-request-id};ts:{ts};`
    y la clave es la del panel de MP (MERCADOPAGO_WEBHOOK_SECRET), que no es
    el access token.

    Sin esto, cualquiera que descubra la URL puede mandar
    {"type":"payment","data":{"id":"..."}} y regalarse créditos.
    """
    if not x_signature:
        return False

    partes: dict[str, str] = {}
    for trozo in x_signature.split(","):
        if "=" not in trozo:
            continue
        clave, _, valor = trozo.partition("=")
        partes[clave.strip()] = valor.strip()

    ts = partes.get("ts")
    v1 = partes.get("v1")
    if not ts or not v1:
        return False

    # MP documenta que el id alfanumérico va en minúsculas en el manifiesto.
    manifiesto = f"id:{data_id.lower()};request-id:{x_request_id or ''};ts:{ts};"
    esperado = hmac.new(
        settings.MERCADOPAGO_WEBHOOK_SECRET.encode(),
        manifiesto.encode(),
        hashlib.sha256,
    ).hexdigest()

    # compare_digest y no ==: comparación en tiempo constante, para que no
    # se pueda ir adivinando la firma midiendo cuánto tarda la respuesta.
    return hmac.compare_digest(esperado, v1)


@router.post("/webhook", status_code=200)
async def webhook(
    request: Request,
    tareas: BackgroundTasks,
    x_signature: str | None = Header(default=None, alias="x-signature"),
    x_request_id: str | None = Header(default=None, alias="x-request-id"),
) -> dict[str, bool]:
    """
    Recibe el aviso de Mercado Pago y actualiza la transacción.

    Devuelve 200 en casi todos los caminos a propósito: Mercado Pago
    reintenta ante cualquier respuesta que no sea 2xx, y reintentar no
    arregla un aviso de un tipo que no nos interesa o de un pago que no es
    nuestro. Solo la firma inválida corta con 401.
    """
    # Con Stripe activo este endpoint no debería recibir nada. Si Mercado
    # Pago todavía tiene la URL configurada y sigue avisando, se rechaza
    # explícitamente en vez de acreditar por un canal que ya no se usa.
    if not settings.mercadopago_activo:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Mercado Pago no es la pasarela activa",
        )

    if not settings.MERCADOPAGO_WEBHOOK_SECRET:
        # Fallar cerrado: sin secreto no hay forma de distinguir un aviso
        # real de uno inventado, y este endpoint mueve dinero.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El webhook de pagos no está configurado",
        )

    cuerpo = await request.json()
    tipo = cuerpo.get("type") or cuerpo.get("topic")
    data_id = str((cuerpo.get("data") or {}).get("id") or cuerpo.get("id") or "")

    if not data_id:
        return {"recibido": True}

    if not _firma_valida(x_signature, x_request_id, data_id):
        log.warning("Webhook con firma inválida (data.id=%s)", data_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firma inválida",
        )

    # 'plan'/'subscription_*' se aceptan pero todavía no se procesan: hoy el
    # cobro recurrente se hace con preferencias sueltas, no con preapproval.
    if tipo != "payment":
        log.info("Webhook ignorado (type=%s)", tipo)
        return {"recibido": True}

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
            respuesta = await cliente.get(
                f"{MP_API}/v1/payments/{data_id}", headers=_headers_mp()
            )
        respuesta.raise_for_status()
        pago = respuesta.json()
    except httpx.HTTPError as e:
        # Acá sí conviene un 5xx: el aviso era legítimo y no pudimos
        # consultarlo, así que queremos que Mercado Pago reintente.
        log.error("No se pudo consultar el pago %s: %s", data_id, e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo consultar el pago",
        )

    referencia = pago.get("external_reference")
    if not referencia:
        return {"recibido": True}

    try:
        transaccion_id = UUID(str(referencia))
    except ValueError:
        log.warning("external_reference no es un UUID: %r", referencia)
        return {"recibido": True}

    nuevo_estado = ESTADOS_MP.get(str(pago.get("status")), "pendiente")
    tarjeta = (pago.get("card") or {}).get("last_four_digits")

    fila = await fetch_one(
        """
        UPDATE tenant_transactions
           SET estado_pago       = $2,
               mp_payment_id     = $3,
               metodo_pago       = COALESCE($4, metodo_pago),
               ultimos_4_digitos = COALESCE($5, ultimos_4_digitos),
               updated_at        = NOW()
         WHERE id = $1
        RETURNING id, tenant_id
        """,
        transaccion_id,
        nuevo_estado,
        str(pago.get("id")),
        pago.get("payment_type_id"),
        tarjeta,
    )

    if fila is None:
        # Un pago que no es nuestro, o una fila borrada. No hay nada que
        # reintentar.
        log.warning("Webhook para una transacción inexistente: %s", transaccion_id)
        return {"recibido": True}

    if nuevo_estado == "aprobado":
        # En background para contestarle rápido a Mercado Pago: si tardamos,
        # da el aviso por fallido y lo reintenta.
        tareas.add_task(procesar_pago_aprobado, transaccion_id)

    return {"recibido": True}


# ============================================================
# CONSULTAS DEL PORTAL
# ============================================================
@router.get("/suscripcion", response_model=SuscripcionOut)
async def suscripcion(tenant_id: UUID = Depends(tenant_actual)) -> SuscripcionOut:
    """Estado de cobros del negocio: plan vigente y saldo de créditos."""
    fila = await fetch_one(
        """
        SELECT s.plan, s.estado AS estado_suscripcion, s.fecha_renovacion,
               s.precio_monthly,
               COALESCE(c.creditos_disponibles, 0) AS creditos_disponibles,
               COALESCE(c.creditos_gastados, 0)    AS creditos_gastados
        FROM tenants t
        LEFT JOIN tenant_subscriptions s ON s.tenant_id = t.id
        LEFT JOIN tenant_credits      c ON c.tenant_id = t.id
        WHERE t.id = $1
        """,
        tenant_id,
    )

    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Negocio no encontrado",
        )

    return SuscripcionOut(**dict(fila))


@router.get("/historial", response_model=list[TransaccionOut])
async def historial(tenant_id: UUID = Depends(tenant_actual)) -> list[TransaccionOut]:
    """Los últimos cobros del negocio, del más reciente al más viejo."""
    filas = await fetch_all(
        """
        SELECT id, tipo, concepto, monto, estado_pago, metodo_pago,
               ultimos_4_digitos, created_at AS creado_en
        FROM tenant_transactions
        WHERE tenant_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        tenant_id,
        HISTORIAL_LIMITE,
    )
    return [TransaccionOut(**dict(f)) for f in filas]


@router.get("/checkout/{referencia}", response_model=CheckoutEstadoOut)
async def estado_checkout(
    referencia: str,
    tenant_id: UUID = Depends(tenant_actual),
) -> CheckoutEstadoOut:
    """
    Estado de un pago concreto. Lo usa la pantalla de retorno para decirle
    al usuario si su pago ya quedó confirmado.

    Busca por los dos proveedores: `referencia` es una Checkout Session de
    Stripe (cs_...) o una preferencia de Mercado Pago. Así un pago viejo de
    MP se sigue pudiendo consultar con el mismo endpoint.

    El filtro por tenant_id no es decorativo: sin él, conocer una referencia
    ajena alcanzaría para espiar los cobros de otro negocio.
    """
    fila = await fetch_one(
        """
        SELECT id, tipo, monto, estado_pago AS estado, created_at AS fecha
        FROM tenant_transactions
        WHERE tenant_id = $2
          AND (stripe_session_id = $1 OR mp_preference_id = $1)
        """,
        referencia,
        tenant_id,
    )

    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pago no encontrado",
        )

    return CheckoutEstadoOut(**dict(fila))
