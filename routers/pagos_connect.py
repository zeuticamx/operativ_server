"""
Stripe Connect: cada owner conecta su propia cuenta de Stripe y genera
links de pago para cobrarle a SUS clientes (no a OperativAI).

Separado de routers/pagos.py (la facturación de la plataforma a los
tenants) a propósito: son dos flujos de dinero distintos. Montado bajo
/pagos para heredar gratis el bloqueo de "ver como" con escritura —
services/acceso_soporte.RUTAS_SENSIBLES ya incluye "/api/pagos" con match
por prefijo.

La lógica de negocio vive en services/stripe_connect.py; este archivo solo
resuelve el tenant, aplica los apagadores de configuración y traduce a
HTTP. La misma herramienta la puede generar el agente de IA dentro de una
conversación: ver routers/eventos.py ("pagos/connect/crear-cobro").
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from config import settings
from deps import UsuarioActual, gerencia_actual, tenant_actual
from schemas import (
    CobroConnectOut,
    ConnectEstadoOut,
    ConnectOnboardingOut,
    CrearCobroConnectIn,
)
from services.stripe_connect import (
    CuentaNoConectada,
    crear_payment_link_cobro,
    desactivar_cobro,
    iniciar_onboarding,
    obtener_estado,
)
from session import fetch_all, fetch_one

log = logging.getLogger("operativai.pagos.connect")

router = APIRouter(prefix="/pagos/connect", tags=["pagos"])


def _exigir_connect() -> None:
    if not settings.STRIPE_CONNECT_ACTIVO:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Los cobros propios no están activados"
        )
    if not settings.stripe_connect_configurado:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Falta configurar STRIPE_SECRET_KEY"
        )


def _a_cobro_out(fila) -> CobroConnectOut:
    return CobroConnectOut(
        id=fila["id"],
        descripcion=fila["descripcion"],
        monto=fila["monto"],
        moneda=fila["moneda"],
        url_cobro=fila["url_cobro"],
        estado=fila["estado"],
        application_fee_monto=fila["application_fee_monto"],
        generado_por_agente=fila["conversation_id"] is not None,
        creado_en=fila["creado_en"],
        pagado_en=fila["pagado_en"],
    )


@router.get("/estado", response_model=ConnectEstadoOut)
async def estado(
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
) -> ConnectEstadoOut:
    """Si el tenant nunca conectó, todo en false/vacío — no es un error."""
    fila = await obtener_estado(tenant_id)
    if fila is None:
        return ConnectEstadoOut(
            conectado=False, charges_habilitado=False, payouts_habilitado=False,
            detalles_enviados=False, requisitos_pendientes=[],
        )
    return ConnectEstadoOut(
        conectado=True,
        charges_habilitado=fila["charges_habilitado"],
        payouts_habilitado=fila["payouts_habilitado"],
        detalles_enviados=fila["detalles_enviados"],
        requisitos_pendientes=list(fila["requisitos_pendientes"] or []),
    )


@router.post("/onboarding", response_model=ConnectOnboardingOut, status_code=201)
async def onboarding(
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
) -> ConnectOnboardingOut:
    """
    Crea la cuenta Express si no existe (idempotente) y devuelve la URL de
    Account Link para redirigir ahí. Sirve igual para empezar el onboarding
    que para "retomarlo" (requisitos pendientes): Stripe siempre da un
    Account Link nuevo, de un solo uso.
    """
    _exigir_connect()
    url = await iniciar_onboarding(tenant_id)
    return ConnectOnboardingOut(url=url)


@router.get("/cobros", response_model=list[CobroConnectOut])
async def listar_cobros(
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
) -> list[CobroConnectOut]:
    filas = await fetch_all(
        "SELECT * FROM tenant_cobros_connect WHERE tenant_id = $1 ORDER BY creado_en DESC",
        tenant_id,
    )
    return [_a_cobro_out(f) for f in filas]


@router.post("/cobros", response_model=CobroConnectOut, status_code=201)
async def crear_cobro(
    datos: CrearCobroConnectIn,
    tenant_id: UUID = Depends(tenant_actual),
    usuario: UsuarioActual = Depends(gerencia_actual),
) -> CobroConnectOut:
    """409 si el negocio todavía no completó el onboarding de Connect."""
    _exigir_connect()
    try:
        fila, _duplicado = await crear_payment_link_cobro(
            tenant_id, datos.descripcion, datos.monto, creado_por=usuario.id
        )
    except CuentaNoConectada:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Conecta tu cuenta de Stripe antes de generar un cobro",
        )
    return _a_cobro_out(fila)


@router.post("/cobros/{cobro_id}/desactivar", response_model=CobroConnectOut)
async def desactivar(
    cobro_id: UUID,
    tenant_id: UUID = Depends(tenant_actual),
    _: UsuarioActual = Depends(gerencia_actual),
) -> CobroConnectOut:
    _exigir_connect()
    fila = await desactivar_cobro(tenant_id, cobro_id)
    if fila is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cobro no encontrado")
    return _a_cobro_out(fila)
