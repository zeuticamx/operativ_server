"""
Alertas: notificaciones de eventos del tenant (cambios de leads, asignaciones, etc).
Guarda historial en la tabla `alertas`; se envían en tiempo real por WebSocket
(ver realtime.py, que llama a `crear_alerta` y transmite el resultado).
"""

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from deps import ROLES_GERENCIA, UsuarioActual, gerencia_actual, verificar_acceso_tenant
from schemas import AlertaOut
from services.correo import ErrorEnvioCorreo, enviar_alerta_critica
from session import execute, fetch_all, fetch_one

log = logging.getLogger("operativai.alertas")

router = APIRouter()

# Tipos que además de ir por WebSocket disparan un correo inmediato a
# gerencia. El resto (sin_actividad, nuevo_lead, ...) se acumula y va en
# el resumen diario — ver jobs.alertas_background.job_resumen_diario_alertas.
TIPOS_CORREO_INMEDIATO = frozenset({"cuota_excedida"})


@router.get("/tenants/{tenant_id}/alertas", response_model=list[AlertaOut])
async def obtener_alertas(
    tenant_id: UUID,
    skip: int = 0,
    limit: int = 50,
    tipo: Optional[str] = None,
    leido: Optional[bool] = None,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    """Historial de alertas del tenant."""
    verificar_acceso_tenant(usuario, tenant_id)

    query = """
        SELECT id, tenant_id, tipo, titulo, mensaje, datos, leido, creado_en
        FROM alertas
        WHERE tenant_id = $1
    """
    params: list = [tenant_id]

    if tipo:
        params.append(tipo)
        query += f" AND tipo = ${len(params)}"

    if leido is not None:
        params.append(leido)
        query += f" AND leido = ${len(params)}"

    params.extend([limit, skip])
    query += f" ORDER BY creado_en DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}"

    alertas = await fetch_all(query, *params)
    return [dict(a) for a in alertas]


@router.patch("/alertas/{alerta_id}/marcar-leida")
async def marcar_alerta_leida(
    alerta_id: UUID,
    gerencia: UsuarioActual = Depends(gerencia_actual),
):
    """Marcar una alerta como leída."""
    # Filtra por tenant_id del propio token (no de la URL): sin esto,
    # cualquier gerencia autenticada podría marcar leída una alerta de
    # otro negocio con solo adivinar el UUID.
    resultado = await execute(
        "UPDATE alertas SET leido = true WHERE id = $1 AND tenant_id = $2",
        alerta_id,
        gerencia.tenant_id,
    )

    # Si no fue actualizada (0 filas), es porque no existe o es de otro tenant
    if resultado == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Alerta no encontrada")

    return {"status": "ok"}


@router.get("/tenants/{tenant_id}/alertas/estadisticas")
async def estadisticas_alertas(
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    """Conteo de alertas no leídas por tipo."""
    verificar_acceso_tenant(usuario, tenant_id)

    por_tipo = await fetch_all(
        """
        SELECT tipo, COUNT(*)::int as cantidad
        FROM alertas
        WHERE tenant_id = $1 AND leido = false
        GROUP BY tipo
        """,
        tenant_id,
    )

    total = await fetch_one(
        "SELECT COUNT(*)::int as total FROM alertas WHERE tenant_id = $1 AND leido = false",
        tenant_id,
    )

    return {
        "total": sum(f["cantidad"] for f in por_tipo) if por_tipo else 0,
        "por_tipo": {f["tipo"]: f["cantidad"] for f in por_tipo},
    }


async def _emails_gerencia(tenant_id: UUID) -> list[str]:
    filas = await fetch_all(
        """
        SELECT email FROM portal_users
        WHERE tenant_id = $1 AND role = ANY($2::text[]) AND is_active = true
        """,
        tenant_id,
        list(ROLES_GERENCIA),
    )
    return [f["email"] for f in filas]


async def crear_alerta(
    tenant_id: UUID,
    tipo: str,
    titulo: str,
    mensaje: str,
    datos: Optional[dict] = None,
) -> AlertaOut:
    """
    Crea la alerta en BD y devuelve la fila completa, lista para transmitir
    por WebSocket. No emite el WebSocket por sí sola: quien la llama decide
    cuándo y a quién avisar ahí (ver `realtime.broadcast_alerta`).

    El correo inmediato para TIPOS_CORREO_INMEDIATO sí sale desde acá, no
    desde `broadcast_alerta`: así llega también si en algún momento se crea
    una alerta sin pasar por el WebSocket (un job, por ejemplo).
    """
    fila = await fetch_one(
        """
        INSERT INTO alertas (tenant_id, tipo, titulo, mensaje, datos)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id, tenant_id, tipo, titulo, mensaje, datos, leido, creado_en
        """,
        tenant_id,
        tipo,
        titulo,
        mensaje,
        datos or {},
    )
    alerta = AlertaOut(**fila)

    if tipo in TIPOS_CORREO_INMEDIATO:
        for destino in await _emails_gerencia(tenant_id):
            try:
                await enviar_alerta_critica(destino, alerta.titulo, alerta.mensaje)
            except ErrorEnvioCorreo:
                # Ya quedó en el log de services.correo. Que falle el correo
                # no puede tumbar la creación de la alerta: ya se guardó y ya
                # se ve en el panel por WebSocket.
                log.error(
                    "No se pudo mandar el correo de alerta crítica a %s (tenant=%s)",
                    destino,
                    tenant_id,
                )

    return alerta
