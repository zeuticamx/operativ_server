"""
Módulo de calendarios (reservas para barberías/salones).

Opcional por tenant: todo lo de acá exige `calendario_activo`. A diferencia
de vendedores.py (que reparte sus rutas en varios APIRouter por sustantivo),
todo este módulo cuelga de un único router bajo /tenants/{tenant_id}/calendario/...
— así cada consulta tiene siempre tenant_id disponible para filtrar, que es
lo que sostiene la disciplina 404-en-vez-de-403 en cada lookup por id.
"""

from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from realtime import broadcast_alerta
from deps import (
    UsuarioActual,
    gerencia_actual,
    tenant_en_ruta,
    verificar_acceso_tenant,
)
from services import calendario
from services.calendario import actor_desde_portal, verificar_calendario_activo
from schemas import (
    CambiarEstadoReservaIn,
    CancelarReservaIn,
    CorteDiarioOut,
    DescansoCrearIn,
    DescansoOut,
    EstadoReserva,
    ExcepcionCrearIn,
    ExcepcionOut,
    HorarioSemanalOut,
    ProveedorActualizarIn,
    ProveedorCrearIn,
    ProveedorOut,
    ReasignarReservaIn,
    ReemplazarHorariosIn,
    ReemplazarHorariosOut,
    ReprogramarReservaIn,
    ReservaAuditoriaOut,
    ReservaCrearIn,
    ReservaOut,
    ServicioActualizarIn,
    ServicioCrearIn,
    ServicioOut,
)
from session import fetch_all, fetch_one

router_calendario = APIRouter(prefix="/tenants", tags=["calendario"])


# ============================================================
# Gate del módulo
# ============================================================
async def modulo_en_ruta(tenant_id: UUID = Depends(tenant_en_ruta)) -> UUID:
    await verificar_calendario_activo(tenant_id)
    return tenant_id


def _a_reserva_out(r: calendario.Reserva) -> ReservaOut:
    return ReservaOut(**vars(r))


def _a_proveedor_out(p: calendario.Proveedor) -> ProveedorOut:
    return ProveedorOut(**vars(p))


def _a_servicio_out(s: calendario.Servicio) -> ServicioOut:
    return ServicioOut(**vars(s))


# ============================================================
# Proveedores
# ============================================================
@router_calendario.get("/{tenant_id}/calendario/proveedores", response_model=list[ProveedorOut])
async def listar_proveedores(
    tenant_id: UUID = Depends(modulo_en_ruta),
    activo: bool | None = Query(None),
):
    filas = await fetch_all(
        """
        SELECT id, tenant_id, nombre, color, activo, orden, creado_en
        FROM proveedores
        WHERE tenant_id = $1 AND ($2::boolean IS NULL OR activo = $2)
        ORDER BY orden, creado_en
        """,
        tenant_id,
        activo,
    )
    return [_a_proveedor_out(calendario.Proveedor.desde_fila(f)) for f in filas]


@router_calendario.post(
    "/{tenant_id}/calendario/proveedores", response_model=ProveedorOut, status_code=201
)
async def crear_proveedor(
    datos: ProveedorCrearIn,
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    verificar_acceso_tenant(usuario, tenant_id)
    await verificar_calendario_activo(tenant_id)

    fila = await fetch_one(
        """
        INSERT INTO proveedores (tenant_id, nombre, color, orden)
        VALUES ($1, $2, $3, $4)
        RETURNING id, tenant_id, nombre, color, activo, orden, creado_en
        """,
        tenant_id,
        datos.nombre,
        datos.color,
        datos.orden,
    )
    return _a_proveedor_out(calendario.Proveedor.desde_fila(fila))


@router_calendario.patch(
    "/{tenant_id}/calendario/proveedores/{proveedor_id}", response_model=ProveedorOut
)
async def actualizar_proveedor(
    proveedor_id: UUID,
    datos: ProveedorActualizarIn,
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    verificar_acceso_tenant(usuario, tenant_id)
    await verificar_calendario_activo(tenant_id)
    await _exigir_proveedor(proveedor_id, tenant_id)

    fila = await fetch_one(
        """
        UPDATE proveedores SET
            nombre = COALESCE($3, nombre),
            color  = COALESCE($4, color),
            activo = COALESCE($5, activo),
            orden  = COALESCE($6, orden)
        WHERE id = $1 AND tenant_id = $2
        RETURNING id, tenant_id, nombre, color, activo, orden, creado_en
        """,
        proveedor_id,
        tenant_id,
        datos.nombre,
        datos.color,
        datos.activo,
        datos.orden,
    )
    return _a_proveedor_out(calendario.Proveedor.desde_fila(fila))


async def _exigir_proveedor(proveedor_id: UUID, tenant_id: UUID) -> calendario.Proveedor:
    """El filtro por tenant va en el WHERE del SELECT: un proveedor ajeno da 404, no 403."""
    proveedor = await calendario.proveedor_del_tenant(proveedor_id, tenant_id)
    if proveedor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proveedor no encontrado")
    return proveedor


async def _exigir_servicio(servicio_id: UUID, tenant_id: UUID) -> calendario.Servicio:
    servicio = await calendario.servicio_del_tenant(servicio_id, tenant_id)
    if servicio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Servicio no encontrado")
    return servicio


# 422 para lo que hace inválida a la solicitud en sí (referencia mala,
# jornada que no admite ese horario); 409 para "esto entra en conflicto con
# una reserva ya existente" — son dos categorías distintas de rechazo.
_MOTIVOS_RECHAZO: dict[str, tuple[int, str]] = {
    "proveedor_invalido": (
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "El proveedor no existe o no está activo en este negocio",
    ),
    "servicio_invalido": (
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "El servicio no existe o no está activo en este negocio",
    ),
    "fuera_de_horario": (
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "El horario seleccionado está fuera de la jornada de atención del barbero",
    ),
    "horario_ocupado": (
        status.HTTP_409_CONFLICT,
        "Ese horario ya está ocupado para este proveedor",
    ),
}


# Mensajes exactos pedidos por la historia de usuario del barbero
# (Escenarios 2 y 5): 422 para "el descanso en sí es inválido" (no cabe en
# la jornada), 409 para "choca con algo que ya existe" (una cita
# confirmada) — misma separación de categorías que _MOTIVOS_RECHAZO.
_MOTIVOS_RECHAZO_DESCANSO: dict[str, tuple[int, str]] = {
    "fuera_de_jornada": (
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "El descanso debe estar dentro de tu jornada laboral",
    ),
    "con_citas": (
        status.HTTP_409_CONFLICT,
        "Tienes citas confirmadas en ese horario. Cancela o reprograma la cita antes de fijar el descanso",
    ),
}


async def _exigir_reserva(reserva_id: UUID, tenant_id: UUID) -> calendario.Reserva:
    reserva = await calendario.reserva_del_tenant(reserva_id, tenant_id)
    if reserva is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reserva no encontrada")
    return reserva


# ============================================================
# Servicios
# ============================================================
@router_calendario.get("/{tenant_id}/calendario/servicios", response_model=list[ServicioOut])
async def listar_servicios(
    tenant_id: UUID = Depends(modulo_en_ruta),
    activo: bool | None = Query(None),
):
    from session import fetch_all

    filas = await fetch_all(
        """
        SELECT id, tenant_id, nombre, duracion_minutos, precio, activo, creado_en
        FROM servicios
        WHERE tenant_id = $1 AND ($2::boolean IS NULL OR activo = $2)
        ORDER BY activo DESC, nombre
        """,
        tenant_id,
        activo,
    )
    return [_a_servicio_out(calendario.Servicio.desde_fila(f)) for f in filas]


@router_calendario.post(
    "/{tenant_id}/calendario/servicios", response_model=ServicioOut, status_code=201
)
async def crear_servicio(
    datos: ServicioCrearIn,
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    verificar_acceso_tenant(usuario, tenant_id)
    await verificar_calendario_activo(tenant_id)

    fila = await fetch_one(
        """
        INSERT INTO servicios (tenant_id, nombre, duracion_minutos, precio)
        VALUES ($1, $2, $3, $4)
        RETURNING id, tenant_id, nombre, duracion_minutos, precio, activo, creado_en
        """,
        tenant_id,
        datos.nombre,
        datos.duracion_minutos,
        datos.precio,
    )
    return _a_servicio_out(calendario.Servicio.desde_fila(fila))


@router_calendario.patch(
    "/{tenant_id}/calendario/servicios/{servicio_id}", response_model=ServicioOut
)
async def actualizar_servicio(
    servicio_id: UUID,
    datos: ServicioActualizarIn,
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    verificar_acceso_tenant(usuario, tenant_id)
    await verificar_calendario_activo(tenant_id)
    anterior = await _exigir_servicio(servicio_id, tenant_id)

    fila = await fetch_one(
        """
        UPDATE servicios SET
            nombre           = COALESCE($3, nombre),
            duracion_minutos = COALESCE($4, duracion_minutos),
            precio           = COALESCE($5, precio),
            activo           = COALESCE($6, activo)
        WHERE id = $1 AND tenant_id = $2
        RETURNING id, tenant_id, nombre, duracion_minutos, precio, activo, creado_en
        """,
        servicio_id,
        tenant_id,
        datos.nombre,
        datos.duracion_minutos,
        datos.precio,
        datos.activo,
    )

    # Las citas ya agendadas no se enteran de este cambio: hora_fin quedó
    # fija en `reservas` desde que se crearon (16_calendarios.sql) y
    # precio_cobrado se snapshotea aparte al completar (19_calendario_cobros.sql)
    # -- editar el catálogo aquí solo afecta a las reservas nuevas.
    await calendario.registrar_edicion_servicio(
        tenant_id,
        servicio_id,
        anterior,
        datos,
        actor_email=usuario.email,
        actor_portal_user_id=usuario.id,
    )

    return _a_servicio_out(calendario.Servicio.desde_fila(fila))


# ============================================================
# Horarios semanales
# ============================================================
@router_calendario.get(
    "/{tenant_id}/calendario/proveedores/{proveedor_id}/horarios",
    response_model=list[HorarioSemanalOut],
)
async def leer_horarios(proveedor_id: UUID, tenant_id: UUID = Depends(modulo_en_ruta)):
    await _exigir_proveedor(proveedor_id, tenant_id)
    filas = await calendario.listar_horarios_out(proveedor_id)
    return [HorarioSemanalOut(**f) for f in filas]


@router_calendario.put(
    "/{tenant_id}/calendario/proveedores/{proveedor_id}/horarios",
    response_model=ReemplazarHorariosOut,
)
async def reemplazar_horarios(
    proveedor_id: UUID,
    datos: ReemplazarHorariosIn,
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    """
    El guardado nunca falla por esto, pero un horario más corto puede dejar
    citas futuras fuera de la nueva jornada — `reservas_en_conflicto` se
    calcula ANTES de escribir (sobre el horario viejo todavía en BD para
    esas citas, contra los bloques nuevos en memoria) y viaja en la
    respuesta para que el portal avise a gerencia.
    """
    verificar_acceso_tenant(usuario, tenant_id)
    await verificar_calendario_activo(tenant_id)
    await _exigir_proveedor(proveedor_id, tenant_id)

    bloques = [(b.dia_semana, b.hora_inicio, b.hora_fin) for b in datos.bloques]
    conflictos = await calendario.reservas_fuera_de_nuevo_horario(proveedor_id, tenant_id, bloques)
    filas = await calendario.reemplazar_horarios(proveedor_id, tenant_id, bloques)
    return ReemplazarHorariosOut(
        horarios=[HorarioSemanalOut(**f) for f in filas],
        reservas_en_conflicto=[_a_reserva_out(r) for r in conflictos],
    )


# ============================================================
# Excepciones puntuales
# ============================================================
@router_calendario.get(
    "/{tenant_id}/calendario/proveedores/{proveedor_id}/excepciones",
    response_model=list[ExcepcionOut],
)
async def leer_excepciones(
    proveedor_id: UUID,
    desde: date = Query(...),
    hasta: date = Query(...),
    tenant_id: UUID = Depends(modulo_en_ruta),
):
    await _exigir_proveedor(proveedor_id, tenant_id)
    filas = await calendario.listar_excepciones_out(proveedor_id, desde, hasta)
    return [ExcepcionOut(**f) for f in filas]


@router_calendario.post(
    "/{tenant_id}/calendario/proveedores/{proveedor_id}/excepciones",
    response_model=ExcepcionOut,
)
async def crear_excepcion(
    proveedor_id: UUID,
    datos: ExcepcionCrearIn,
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    verificar_acceso_tenant(usuario, tenant_id)
    await verificar_calendario_activo(tenant_id)
    await _exigir_proveedor(proveedor_id, tenant_id)

    fila = await calendario.guardar_excepcion(
        proveedor_id, tenant_id, datos.fecha, datos.disponible, datos.hora_inicio, datos.hora_fin
    )
    return ExcepcionOut(**fila)


@router_calendario.delete(
    "/{tenant_id}/calendario/excepciones/{excepcion_id}", status_code=204
)
async def eliminar_excepcion(
    excepcion_id: UUID,
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    verificar_acceso_tenant(usuario, tenant_id)
    await verificar_calendario_activo(tenant_id)
    borrado = await calendario.eliminar_excepcion(excepcion_id, tenant_id)
    if not borrado:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Excepción no encontrada")


# ============================================================
# Descansos (pausas dentro de la jornada)
# ============================================================
@router_calendario.get(
    "/{tenant_id}/calendario/proveedores/{proveedor_id}/descansos",
    response_model=list[DescansoOut],
)
async def leer_descansos(proveedor_id: UUID, tenant_id: UUID = Depends(modulo_en_ruta)):
    await _exigir_proveedor(proveedor_id, tenant_id)
    filas = await calendario.listar_descansos_out(proveedor_id)
    return [DescansoOut(**f) for f in filas]


@router_calendario.post(
    "/{tenant_id}/calendario/proveedores/{proveedor_id}/descansos",
    response_model=DescansoOut,
    status_code=201,
)
async def crear_descanso(
    proveedor_id: UUID,
    datos: DescansoCrearIn,
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    verificar_acceso_tenant(usuario, tenant_id)
    await verificar_calendario_activo(tenant_id)
    await _exigir_proveedor(proveedor_id, tenant_id)

    fila, motivo = await calendario.guardar_descanso(
        proveedor_id,
        tenant_id,
        dia_semana=datos.dia_semana,
        fecha=datos.fecha,
        hora_inicio=datos.hora_inicio,
        hora_fin=datos.hora_fin,
        etiqueta=datos.etiqueta,
    )
    if fila is None:
        codigo, detalle = _MOTIVOS_RECHAZO_DESCANSO[motivo]
        raise HTTPException(status_code=codigo, detail=detalle)
    return DescansoOut(**fila)


@router_calendario.delete(
    "/{tenant_id}/calendario/descansos/{descanso_id}", status_code=204
)
async def eliminar_descanso(
    descanso_id: UUID,
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    verificar_acceso_tenant(usuario, tenant_id)
    await verificar_calendario_activo(tenant_id)
    borrado = await calendario.eliminar_descanso(descanso_id, tenant_id)
    if not borrado:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Descanso no encontrado")


# ============================================================
# Reservas
# ============================================================
@router_calendario.get("/{tenant_id}/calendario/reservas", response_model=list[ReservaOut])
async def listar_reservas(
    desde: datetime = Query(...),
    hasta: datetime = Query(...),
    proveedor_id: UUID | None = Query(None),
    tenant_id: UUID = Depends(modulo_en_ruta),
):
    reservas = await calendario.listar_reservas(tenant_id, desde, hasta, proveedor_id)
    return [_a_reserva_out(r) for r in reservas]


@router_calendario.post("/{tenant_id}/calendario/reservas", response_model=ReservaOut)
async def crear_reserva_manual(
    datos: ReservaCrearIn,
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    """Alta manual desde el portal: walk-in o reserva telefónica."""
    verificar_acceso_tenant(usuario, tenant_id)
    await verificar_calendario_activo(tenant_id)

    reserva, motivo = await calendario.crear_reserva(
        tenant_id,
        datos.proveedor_id,
        datos.servicio_id,
        datos.hora_inicio,
        user_id=datos.user_id,
        cliente_nombre=datos.cliente_nombre,
        cliente_telefono=datos.cliente_telefono,
        notas=datos.notas,
        actor=actor_desde_portal(usuario.id, usuario.email),
    )
    if reserva is None:
        codigo, detalle = _MOTIVOS_RECHAZO[motivo]
        raise HTTPException(status_code=codigo, detail=detalle)

    # Sin broadcast_alerta acá a propósito: esta es el alta manual de
    # gerencia (walk-in/teléfono). Notificarle a gerencia de una reserva que
    # gerencia misma acaba de crear no informa nada -- la alerta
    # "reserva_creada" es solo para las que llegan solas por el chat (ver
    # routers/eventos.py::calendario_crear_reserva).
    return _a_reserva_out(reserva)


@router_calendario.patch(
    "/{tenant_id}/calendario/reservas/{reserva_id}/reprogramar", response_model=ReservaOut
)
async def reprogramar_reserva(
    reserva_id: UUID,
    datos: ReprogramarReservaIn,
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    verificar_acceso_tenant(usuario, tenant_id)
    await verificar_calendario_activo(tenant_id)
    await _exigir_reserva(reserva_id, tenant_id)

    reserva, motivo = await calendario.reprogramar_reserva(
        reserva_id, tenant_id, datos.hora_inicio, actor_desde_portal(usuario.id, usuario.email)
    )
    if reserva is None:
        if motivo == "no_encontrada":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reserva no encontrada")
        codigo, detalle = _MOTIVOS_RECHAZO[motivo]
        raise HTTPException(status_code=codigo, detail=detalle)
    return _a_reserva_out(reserva)


@router_calendario.patch(
    "/{tenant_id}/calendario/reservas/{reserva_id}/reasignar", response_model=ReservaOut
)
async def reasignar_reserva(
    reserva_id: UUID,
    datos: ReasignarReservaIn,
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    """Cambia el barbero de una cita ya agendada, conservando su horario."""
    verificar_acceso_tenant(usuario, tenant_id)
    await verificar_calendario_activo(tenant_id)
    await _exigir_reserva(reserva_id, tenant_id)

    reserva, motivo = await calendario.reasignar_reserva(
        reserva_id, tenant_id, datos.proveedor_id, actor_desde_portal(usuario.id, usuario.email)
    )
    if reserva is None:
        if motivo == "no_encontrada":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reserva no encontrada")
        codigo, detalle = _MOTIVOS_RECHAZO[motivo]
        raise HTTPException(status_code=codigo, detail=detalle)
    return _a_reserva_out(reserva)


@router_calendario.post(
    "/{tenant_id}/calendario/reservas/{reserva_id}/cancelar", response_model=ReservaOut
)
async def cancelar_reserva(
    reserva_id: UUID,
    datos: CancelarReservaIn,
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    verificar_acceso_tenant(usuario, tenant_id)
    await verificar_calendario_activo(tenant_id)
    await _exigir_reserva(reserva_id, tenant_id)

    reserva = await calendario.cancelar_reserva(
        reserva_id, tenant_id, datos.motivo, actor_desde_portal(usuario.id, usuario.email)
    )
    if reserva is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reserva no encontrada")

    await broadcast_alerta(
        tenant_id,
        "reserva_cancelada",
        "Reserva cancelada",
        f"Se canceló la cita de {reserva.cliente_nombre or 'un cliente'} con {reserva.proveedor_nombre}",
        datos={"reserva_id": str(reserva.id)},
    )
    return _a_reserva_out(reserva)


@router_calendario.patch(
    "/{tenant_id}/calendario/reservas/{reserva_id}/estado", response_model=ReservaOut
)
async def cambiar_estado_reserva(
    reserva_id: UUID,
    datos: CambiarEstadoReservaIn,
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    verificar_acceso_tenant(usuario, tenant_id)
    await verificar_calendario_activo(tenant_id)
    await _exigir_reserva(reserva_id, tenant_id)

    reserva = await calendario.cambiar_estado_reserva(
        reserva_id,
        tenant_id,
        datos.estado,
        actor_desde_portal(usuario.id, usuario.email),
        precio_cobrado=datos.precio_cobrado,
        metodo_pago=datos.metodo_pago,
    )
    if reserva is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reserva no encontrada")
    return _a_reserva_out(reserva)


@router_calendario.get(
    "/{tenant_id}/calendario/corte-diario", response_model=CorteDiarioOut
)
async def leer_corte_diario(
    tenant_id: UUID = Depends(modulo_en_ruta),
    fecha: date = Query(default_factory=date.today),
    proveedor_id: UUID | None = Query(None),
):
    """
    Corte de caja del día: cuántos servicios se completaron y cuánto se
    cobró. Cuenta solo 'completada' (Escenario 4) — confirmada/cancelada/
    no_asistio no facturaron nada ese día.
    """
    resumen = await calendario.corte_diario(tenant_id, fecha, proveedor_id)
    return CorteDiarioOut(
        fecha=resumen["fecha"],
        total_servicios=resumen["total_servicios"],
        total_cobrado=resumen["total_cobrado"],
        servicios=[_a_reserva_out(r) for r in resumen["servicios"]],
    )


# ============================================================
# Bitácora (solo lectura — historia de usuario del dueño de la estética)
# ============================================================
def _a_auditoria_out(fila: dict) -> ReservaAuditoriaOut:
    return ReservaAuditoriaOut(**fila)


@router_calendario.get(
    "/{tenant_id}/calendario/auditoria", response_model=list[ReservaAuditoriaOut]
)
async def leer_auditoria(
    tenant_id: UUID = Depends(modulo_en_ruta),
    desde: datetime | None = Query(None),
    hasta: datetime | None = Query(None),
    proveedor_id: UUID | None = Query(None),
    cliente: str | None = Query(None, max_length=255),
    reserva_id: UUID | None = Query(None),
    estado: EstadoReserva | None = Query(None),
    limite: int = Query(100, ge=1, le=500),
):
    """
    Escenarios 2 y 3: tabla cronológica (más reciente primero) con filtros
    por rango de fechas, barbero, cliente, cita puntual y estatus resultante.
    Solo lectura — no hay POST/PATCH/DELETE en esta sección (Escenario 5):
    cada fila la escribe el propio backend al procesar el evento, nunca a
    mano.
    """
    filas = await calendario.listar_auditoria(
        tenant_id,
        desde=desde,
        hasta=hasta,
        proveedor_id=proveedor_id,
        cliente=cliente,
        reserva_id=reserva_id,
        estado=estado,
        limite=limite,
    )
    return [_a_auditoria_out(f) for f in filas]


@router_calendario.get(
    "/{tenant_id}/calendario/reservas/{reserva_id}/auditoria",
    response_model=list[ReservaAuditoriaOut],
)
async def leer_auditoria_de_reserva(
    reserva_id: UUID, tenant_id: UUID = Depends(modulo_en_ruta)
):
    """Escenario 4: línea de tiempo completa de UNA cita, de su creación al estado actual."""
    await _exigir_reserva(reserva_id, tenant_id)
    filas = await calendario.listar_auditoria(
        tenant_id, reserva_id=reserva_id, limite=500, orden_asc=True
    )
    return [_a_auditoria_out(f) for f in filas]
