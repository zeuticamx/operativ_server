"""
Cobros con Mercado Pago: suscripción por plan + compra de créditos.

Dos reglas que valen para todo el módulo:

1. El precio NUNCA llega del cliente. `crear-pago` recibe qué se quiere
   comprar (un plan o un paquete de créditos) y el monto sale de las tablas
   `planes` / `paquetes_creditos`. Si el monto viajara en el body, cualquiera
   podría contratar enterprise por un peso.

2. Lo que da por bueno un pago es el webhook, no el regreso del navegador.
   Las back_urls solo sirven para mostrarle algo al usuario; alguien puede
   abrir /pagos/exito a mano. El estado real se escribe cuando Mercado Pago
   avisa por /webhook y nosotros le repreguntamos por su propia API.
"""

import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg
import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status

from config import settings
from deps import UsuarioActual, gerencia_actual, tenant_actual
from schemas import (
    CatalogoPagosOut,
    CrearPagoIn,
    CrearPagoOut,
    PaqueteCreditosOut,
    PlanOut,
    PreferenciaEstadoOut,
    SuscripcionOut,
    TransaccionOut,
)
from session import execute, fetch_all, fetch_one, fetch_value, transaccion

router = APIRouter(prefix="/pagos", tags=["pagos"])

log = logging.getLogger("operativai.pagos")

MP_API = "https://api.mercadopago.com"
TIMEOUT = httpx.Timeout(15.0)

# Cuántos días dura un ciclo. 30 fijos y no "el mismo día del mes que
# viene": sin dateutil, sumar un mes calendario a un 31 de enero es un caso
# borde que no vale la pena resolver a mano hasta que el negocio lo pida.
DIAS_CICLO = 30

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


def _exigir_mercadopago() -> None:
    """503 si falta el access token, en vez de un 500 al llamar a la API."""
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
async def _cotizar(datos: CrearPagoIn) -> tuple[Decimal, str]:
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


@router.post("/crear-pago", response_model=CrearPagoOut, status_code=201)
async def crear_pago(
    datos: CrearPagoIn,
    usuario: UsuarioActual = Depends(gerencia_actual),
) -> CrearPagoOut:
    """
    Crea la preferencia en Mercado Pago y deja la transacción en 'pendiente'.

    Solo gerencia (owner/superadmin): contratar un plan cambia lo que paga
    el negocio, no es una acción de 'member'.
    """
    _exigir_mercadopago()

    if usuario.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El usuario no tiene un negocio asociado todavía",
        )

    monto, concepto = await _cotizar(datos)

    # La fila se crea antes de llamar a Mercado Pago porque su id es el
    # external_reference que le mandamos. Queda 'pendiente' hasta que el
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

    payload = _payload_preferencia(transaccion_id, concepto, monto, usuario.email)

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
        await _marcar_cancelada(transaccion_id)
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
        await _marcar_cancelada(transaccion_id)
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
        mp_preference_id=mp_preference_id,
        init_point=init_point,
        monto=monto,
        concepto=concepto,
    )


async def _marcar_cancelada(transaccion_id: UUID) -> None:
    await execute(
        """
        UPDATE tenant_transactions
           SET estado_pago = 'cancelado', updated_at = NOW()
         WHERE id = $1 AND estado_pago = 'pendiente'
        """,
        transaccion_id,
    )


# ============================================================
# WEBHOOK
# ============================================================
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
        tareas.add_task(_procesar_pago_aprobado, transaccion_id)

    return {"recibido": True}


# ============================================================
# ENTREGA DE LO COMPRADO
# ============================================================
async def _procesar_pago_aprobado(transaccion_id: UUID) -> None:
    """
    Entrega lo que se pagó: activa el plan o acredita los créditos.

    Corre fuera del request, así que no puede devolver un error al cliente:
    todo lo que falle se registra en el log.

    Es idempotente porque Mercado Pago manda el mismo aviso varias veces:
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
            await _activar_suscripcion(fila["tenant_id"], fila["plan_nombre"])
        elif fila["tipo"] == "credit_purchase":
            await _acreditar_creditos(
                fila["tenant_id"],
                transaccion_id,
                fila["creditos_comprados"],
                fila["concepto"],
            )
    except Exception:  # noqa: BLE001 - es un background task: nada puede escapar
        log.exception("Falló el procesamiento del pago %s", transaccion_id)


async def _activar_suscripcion(tenant_id: UUID, plan: str) -> None:
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


async def _acreditar_creditos(
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


@router.get("/preferencia/{mp_preference_id}", response_model=PreferenciaEstadoOut)
async def estado_preferencia(
    mp_preference_id: str,
    tenant_id: UUID = Depends(tenant_actual),
) -> PreferenciaEstadoOut:
    """
    Estado de un pago concreto. Lo usa la pantalla de retorno para decirle
    al usuario si su pago ya quedó confirmado.

    El filtro por tenant_id no es decorativo: sin él, conocer un id de
    preferencia ajeno alcanzaría para espiar los cobros de otro negocio.
    """
    fila = await fetch_one(
        """
        SELECT id, tipo, monto, estado_pago AS estado, created_at AS fecha
        FROM tenant_transactions
        WHERE mp_preference_id = $1 AND tenant_id = $2
        """,
        mp_preference_id,
        tenant_id,
    )

    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pago no encontrado",
        )

    return PreferenciaEstadoOut(**dict(fila))
