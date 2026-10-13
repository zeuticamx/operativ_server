"""
Alertas: notificaciones de eventos del tenant (cambios de leads, asignaciones, etc).
Guarda historial en la tabla `alertas`; se envían en tiempo real por WebSocket
(ver realtime.py, que llama a `crear_alerta` y transmite el resultado).

Una alerta con `portal_user_id` es PERSONAL (29_perfil_usuario.sql): solo
la ve y la marca leída ese usuario, aunque sea de su mismo negocio. Las que
no lo tienen son del negocio, como siempre.
"""

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from deps import (
    ROLES_GERENCIA,
    ROLES_NEGOCIO,
    UsuarioActual,
    gerencia_actual,
    usuario_actual,
    verificar_acceso_tenant,
)
from schemas import AlertaOut, ConteoAlertasOut, MarcarTodasLeidasOut
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
        SELECT id, tenant_id, tipo, titulo, mensaje, datos, leido, creado_en,
               portal_user_id
        FROM alertas
        WHERE tenant_id = $1
          AND (portal_user_id IS NULL OR portal_user_id = $2)
    """
    params: list = [tenant_id, usuario.id]

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


@router.get("/alertas/mias", response_model=list[AlertaOut])
async def mis_alertas(
    leido: Optional[bool] = None,
    limite: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    usuario: UsuarioActual = Depends(usuario_actual),
):
    """
    Las alertas que este usuario recibe en vivo, para cualquier rol: las
    personales y, si está en la room del negocio (deps.ROLES_NEGOCIO), las
    del negocio. Mismo criterio que `realtime.connect`, para que lo que se
    lista por HTTP coincida con lo que llega por WebSocket y push.

    Existe aparte de GET /tenants/{id}/alertas porque aquel es solo de
    gerencia: un vendedor no tenía cómo leer sus propias alertas.
    """
    if usuario.tenant_id is None:
        return []

    query = f"""
        SELECT id, tenant_id, tipo, titulo, mensaje, datos, leido, creado_en,
               portal_user_id
        FROM alertas
        WHERE {_VISIBLES_PARA_USUARIO}
    """
    params: list = _params_visibles(usuario)

    if leido is not None:
        params.append(leido)
        query += f" AND leido = ${len(params)}"

    params.extend([limite, offset])
    query += f" ORDER BY creado_en DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}"

    return [dict(a) for a in await fetch_all(query, *params)]


# Lo que ve un usuario en /alertas/mias y en el socket: sus personales y, si su
# rol está en la room del negocio, las del negocio. $1 tenant, $2 usuario,
# $3 "ve las del negocio". Compartido por los tres endpoints de /alertas/mias
# para que listado, contador y "marcar todas" no puedan desalinearse.
_VISIBLES_PARA_USUARIO = """
    tenant_id = $1
    AND (portal_user_id = $2 OR (portal_user_id IS NULL AND $3))
"""


def _params_visibles(usuario: UsuarioActual) -> list:
    return [usuario.tenant_id, usuario.id, usuario.role in ROLES_NEGOCIO]


@router.get("/alertas/mias/no-leidas", response_model=ConteoAlertasOut)
async def contar_mis_no_leidas(usuario: UsuarioActual = Depends(usuario_actual)):
    """
    Cuántas alertas sin leer tiene el usuario, sin tope: la campana de la app
    lo muestra tal cual. El listado de /alertas/mias está paginado (máx. 100),
    así que contarlo en el cliente daría un número falso con muchas pendientes.
    """
    if usuario.tenant_id is None:
        return ConteoAlertasOut(no_leidas=0)
    total = await fetch_one(
        f"SELECT COUNT(*)::int AS total FROM alertas WHERE {_VISIBLES_PARA_USUARIO} AND NOT leido",
        *_params_visibles(usuario),
    )
    return ConteoAlertasOut(no_leidas=total["total"] if total else 0)


@router.patch("/alertas/mias/marcar-todas-leidas", response_model=MarcarTodasLeidasOut)
async def marcar_todas_mis_leidas(usuario: UsuarioActual = Depends(usuario_actual)):
    """
    Marca leídas, de una vez, todas las alertas que el usuario ve (mismo
    criterio que /alertas/mias). Una sola consulta en vez de un evento de
    socket por alerta.

    Las del negocio quedan leídas para todo el negocio, igual que al marcar
    una por el socket (`realtime.marcar_leida`): `leido` es una columna de la
    alerta, no por usuario. Avisa a las otras sesiones con `alerta_leida`.
    """
    if usuario.tenant_id is None:
        return MarcarTodasLeidasOut(marcadas=0)
    filas = await fetch_all(
        f"""
        UPDATE alertas SET leido = true
         WHERE {_VISIBLES_PARA_USUARIO} AND NOT leido
        RETURNING id, portal_user_id
        """,
        *_params_visibles(usuario),
    )
    if filas:
        # Import perezoso: realtime importa este módulo (crear_alerta).
        from realtime import emitir_alertas_leidas

        await emitir_alertas_leidas(usuario.tenant_id, filas)
    return MarcarTodasLeidasOut(marcadas=len(filas))


@router.patch("/alertas/{alerta_id}/marcar-leida")
async def marcar_alerta_leida(
    alerta_id: UUID,
    usuario: UsuarioActual = Depends(usuario_actual),
):
    """
    Marcar una alerta como leída.

    Una alerta del negocio la marca solo gerencia (owner/superadmin), como
    siempre. Una personal la marca su dueño, sea del rol que sea — un
    'member' también recibe el recordatorio de su perfil.
    """
    # Filtra por tenant_id del propio token (no de la URL): sin esto,
    # cualquier gerencia autenticada podría marcar leída una alerta de
    # otro negocio con solo adivinar el UUID. Y la personal, además, por el
    # usuario: la de un compañero da 404 igual que una inexistente.
    resultado = await execute(
        """
        UPDATE alertas SET leido = true
         WHERE id = $1 AND tenant_id = $2
           AND (portal_user_id = $3 OR (portal_user_id IS NULL AND $4))
        """,
        alerta_id,
        usuario.tenant_id,
        usuario.id,
        usuario.role in ROLES_GERENCIA,
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
          AND (portal_user_id IS NULL OR portal_user_id = $2)
        GROUP BY tipo
        """,
        tenant_id,
        usuario.id,
    )

    total = await fetch_one(
        """
        SELECT COUNT(*)::int as total FROM alertas
        WHERE tenant_id = $1 AND leido = false
          AND (portal_user_id IS NULL OR portal_user_id = $2)
        """,
        tenant_id,
        usuario.id,
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
    portal_user_id: Optional[UUID] = None,
) -> AlertaOut:
    """
    `portal_user_id` la hace personal (ver el docstring del módulo).

    Crea la alerta en BD y devuelve la fila completa, lista para transmitir
    por WebSocket. No emite el WebSocket por sí sola: quien la llama decide
    cuándo y a quién avisar ahí (ver `realtime.broadcast_alerta`).

    El correo inmediato para TIPOS_CORREO_INMEDIATO sí sale desde acá, no
    desde `broadcast_alerta`: así llega también si en algún momento se crea
    una alerta sin pasar por el WebSocket (un job, por ejemplo).
    """
    fila = await fetch_one(
        """
        INSERT INTO alertas (tenant_id, tipo, titulo, mensaje, datos, portal_user_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id, tenant_id, tipo, titulo, mensaje, datos, leido, creado_en,
                  portal_user_id
        """,
        tenant_id,
        tipo,
        titulo,
        mensaje,
        datos or {},
        portal_user_id,
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
