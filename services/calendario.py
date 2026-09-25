"""
Acceso a datos del módulo de calendarios (reservas para barberías/salones).

Separado de calendario_slots.py a propósito: acá vive todo lo que sabe de
asyncpg y de las reglas de negocio (qué proveedor/servicio es válido, cómo
se resuelve un traslape), y calendario_slots.py hace el cálculo puro de
horarios libres sin tocar la base de datos.

`verificar_calendario_activo` vive acá y no en routers/calendario.py porque
routers/eventos.py también la necesita: a n8n el tenant_id le llega en el
body de una llamada POST, no en la URL, así que no puede colgarse de un
Depends de ruta como hace vendedores.py con _exigir_modulo.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import HTTPException, status

from schemas import ServicioActualizarIn
from services import calendario_slots
from services.acceso_pagos import acceso_pagos
from services.pipeline import get_tenant_servicios
from session import conexion, execute, fetch_all, fetch_one, transaccion


async def verificar_calendario_activo(tenant_id: UUID) -> None:
    """
    409 si el negocio no tiene el módulo de calendarios encendido; 402 si lo
    tiene encendido pero no puede pagarlo. Mismo criterio y mismo orden que
    routers/vendedores.py::_exigir_modulo: el 409 es la decisión del propio
    dueño (apagado a propósito) y no depende de pagos.
    """
    servicios = await get_tenant_servicios(tenant_id)
    if not servicios.calendario_activo:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "El módulo de calendarios no está activo para este negocio. "
                "Actívalo en /tenants/{tenant_id}/servicios."
            ),
        )

    acceso = await acceso_pagos(tenant_id)
    if not acceso.permitido:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                "La suscripción venció y no quedan créditos disponibles. "
                "Renueva el plan o compra créditos en /pagos/crear-pago para "
                "seguir usando el calendario."
            ),
        )


# ============================================================
# Dataclasses
# ============================================================
@dataclass(frozen=True)
class Proveedor:
    id: UUID
    tenant_id: UUID
    nombre: str
    color: str
    activo: bool
    orden: int
    creado_en: datetime

    @classmethod
    def desde_fila(cls, fila) -> "Proveedor":
        return cls(**dict(fila))


@dataclass(frozen=True)
class Servicio:
    id: UUID
    tenant_id: UUID
    nombre: str
    duracion_minutos: int
    precio: Optional[Decimal]
    activo: bool
    creado_en: datetime

    @classmethod
    def desde_fila(cls, fila) -> "Servicio":
        return cls(**dict(fila))


@dataclass(frozen=True)
class Reserva:
    id: UUID
    tenant_id: UUID
    proveedor_id: UUID
    proveedor_nombre: str
    proveedor_color: str
    servicio_id: UUID
    servicio_nombre: str
    hora_inicio: datetime
    hora_fin: datetime
    estado: str
    user_id: Optional[UUID]
    cliente_nombre: Optional[str]
    cliente_telefono: Optional[str]
    notas: Optional[str]
    precio_cobrado: Optional[Decimal]
    metodo_pago: Optional[str]
    creado_en: datetime

    @classmethod
    def desde_fila(cls, fila) -> "Reserva":
        """
        `fila` puede ser un asyncpg.Record con las columnas ya unidas
        (listar_reservas) o un dict armado a mano fusionando el resultado
        de un INSERT/UPDATE sobre `reservas` con los datos de Proveedor y
        Servicio ya leídos aparte (crear_reserva, cancelar_reserva, etc.) —
        en ambos casos el acceso por clave es igual.
        """
        return cls(
            id=fila["id"],
            tenant_id=fila["tenant_id"],
            proveedor_id=fila["proveedor_id"],
            proveedor_nombre=fila["proveedor_nombre"],
            proveedor_color=fila["proveedor_color"],
            servicio_id=fila["servicio_id"],
            servicio_nombre=fila["servicio_nombre"],
            hora_inicio=fila["hora_inicio"],
            hora_fin=fila["hora_fin"],
            estado=fila["estado"],
            user_id=fila["user_id"],
            cliente_nombre=fila["cliente_nombre"],
            cliente_telefono=fila["cliente_telefono"],
            notas=fila["notas"],
            precio_cobrado=fila["precio_cobrado"],
            metodo_pago=fila["metodo_pago"],
            creado_en=fila["creado_en"],
        )


def _con_proveedor_y_servicio(fila: asyncpg.Record, proveedor: Proveedor, servicio: Servicio) -> dict:
    return {
        **dict(fila),
        "proveedor_nombre": proveedor.nombre,
        "proveedor_color": proveedor.color,
        "servicio_nombre": servicio.nombre,
    }


# ============================================================
# Bitácora de reservas (reserva_auditoria)
# ============================================================
@dataclass(frozen=True)
class ActorAuditoria:
    """
    Quién disparó el evento. Los proveedores nunca son actor: no tienen
    cuenta propia (decisión de producto — ver plan del módulo), así que todo
    lo que les pasa a sus citas lo dispara gerencia desde el portal o un
    cliente que escribió por WhatsApp/IG/FB y por el que actuó n8n.
    """

    origen: str  # 'portal' | 'n8n'
    actor: str  # texto legible: el email de gerencia, o "Cliente: <nombre>"
    portal_user_id: Optional[UUID] = None
    user_id: Optional[UUID] = None


def actor_desde_portal(portal_user_id: UUID, email: str) -> ActorAuditoria:
    return ActorAuditoria(origen="portal", actor=email, portal_user_id=portal_user_id)


def actor_desde_chat(user_id: Optional[UUID], cliente_nombre: Optional[str]) -> ActorAuditoria:
    return ActorAuditoria(
        origen="n8n",
        actor=f"Cliente: {cliente_nombre}" if cliente_nombre else "Cliente (WhatsApp/IG/FB)",
        user_id=user_id,
    )


async def _registrar_auditoria(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    reserva_id: UUID,
    evento: str,
    estado_anterior: Optional[str],
    estado_nuevo: str,
    motivo: Optional[str],
    datos_anteriores: Optional[dict],
    datos_nuevos: Optional[dict],
    actor: ActorAuditoria,
) -> None:
    """
    Deja constancia de un evento de una reserva, en la MISMA conexión (y
    transacción) que el cambio que documenta — mismo criterio que
    services/gerencia.py::registrar_auditoria: una reprogramación que se
    aplica pero no se registra es peor que una que falló.

    `reprogramada` y `cambio_barbero` no cambian el estado de la reserva
    (sigue 'confirmada'): ahí estado_anterior == estado_nuevo a propósito, y
    el cambio real que importa auditar vive en datos_anteriores/datos_nuevos.
    """
    await conn.execute(
        """
        INSERT INTO reserva_auditoria (
            tenant_id, reserva_id, evento, estado_anterior, estado_nuevo, motivo,
            datos_anteriores, datos_nuevos, origen, actor, actor_portal_user_id, actor_user_id
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10, $11, $12)
        """,
        tenant_id,
        reserva_id,
        evento,
        estado_anterior,
        estado_nuevo,
        motivo,
        datos_anteriores,
        datos_nuevos or {},
        actor.origen,
        actor.actor,
        actor.portal_user_id,
        actor.user_id,
    )


async def listar_auditoria(
    tenant_id: UUID,
    *,
    desde: Optional[datetime] = None,
    hasta: Optional[datetime] = None,
    proveedor_id: Optional[UUID] = None,
    cliente: Optional[str] = None,
    reserva_id: Optional[UUID] = None,
    estado: Optional[str] = None,
    limite: int = 100,
    orden_asc: bool = False,
) -> list[dict]:
    """
    Para el módulo de auditoría/bitácora del portal (Escenarios 2 y 3) y
    para el historial de una cita puntual (Escenario 4, con reserva_id fijo
    y orden_asc=True: de su creación a su estado actual).

    `cliente` busca en cliente_nombre o, si la reserva es de un contacto de
    chat sin nombre propio guardado, en users.display_name — mismo criterio
    de "el nombre que haya" que usa ReservaOut.
    """
    orden = "ASC" if orden_asc else "DESC"
    filas = await fetch_all(
        f"""
        SELECT
            a.id, a.tenant_id, a.reserva_id, a.evento, a.estado_anterior, a.estado_nuevo,
            a.motivo, a.datos_anteriores, a.datos_nuevos, a.origen, a.actor,
            a.actor_portal_user_id, a.actor_user_id, a.creado_en,
            p.nombre AS proveedor_nombre, s.nombre AS servicio_nombre,
            COALESCE(r.cliente_nombre, u.display_name) AS cliente_nombre
        FROM reserva_auditoria a
        JOIN reservas r ON r.id = a.reserva_id
        JOIN proveedores p ON p.id = r.proveedor_id
        JOIN servicios s ON s.id = r.servicio_id
        LEFT JOIN users u ON u.id = r.user_id
        WHERE a.tenant_id = $1
          AND ($2::timestamptz IS NULL OR a.creado_en >= $2)
          AND ($3::timestamptz IS NULL OR a.creado_en <= $3)
          AND ($4::uuid IS NULL OR r.proveedor_id = $4)
          AND ($5::text IS NULL OR COALESCE(r.cliente_nombre, u.display_name) ILIKE '%' || $5 || '%')
          AND ($6::uuid IS NULL OR a.reserva_id = $6)
          AND ($7::text IS NULL OR a.estado_nuevo = $7)
        ORDER BY a.creado_en {orden}
        LIMIT $8
        """,
        tenant_id,
        desde,
        hasta,
        proveedor_id,
        cliente,
        reserva_id,
        estado,
        limite,
    )
    return [dict(f) for f in filas]


# ============================================================
# Lecturas de catálogo (proveedores / servicios)
# ============================================================
async def proveedor_del_tenant(
    proveedor_id: UUID, tenant_id: UUID, conn: Optional[asyncpg.Connection] = None
) -> Optional[Proveedor]:
    sql = """
        SELECT id, tenant_id, nombre, color, activo, orden, creado_en
        FROM proveedores
        WHERE id = $1 AND tenant_id = $2
    """
    fila = (
        await conn.fetchrow(sql, proveedor_id, tenant_id)
        if conn is not None
        else await fetch_one(sql, proveedor_id, tenant_id)
    )
    return Proveedor.desde_fila(fila) if fila else None


async def servicio_del_tenant(
    servicio_id: UUID, tenant_id: UUID, conn: Optional[asyncpg.Connection] = None
) -> Optional[Servicio]:
    sql = """
        SELECT id, tenant_id, nombre, duracion_minutos, precio, activo, creado_en
        FROM servicios
        WHERE id = $1 AND tenant_id = $2
    """
    fila = (
        await conn.fetchrow(sql, servicio_id, tenant_id)
        if conn is not None
        else await fetch_one(sql, servicio_id, tenant_id)
    )
    return Servicio.desde_fila(fila) if fila else None


async def proveedores_activos(
    tenant_id: UUID, conn: Optional[asyncpg.Connection] = None
) -> list[Proveedor]:
    sql = """
        SELECT id, tenant_id, nombre, color, activo, orden, creado_en
        FROM proveedores
        WHERE tenant_id = $1 AND activo = true
        ORDER BY orden, creado_en
    """
    filas = await (conn.fetch(sql, tenant_id) if conn is not None else fetch_all(sql, tenant_id))
    return [Proveedor.desde_fila(f) for f in filas]


async def servicios_activos(
    tenant_id: UUID, conn: Optional[asyncpg.Connection] = None
) -> list[Servicio]:
    """
    Catálogo que el agente de n8n puede ofrecer por chat -- mismo criterio
    que proveedores_activos: solo lo que hoy se puede reservar, nunca lo
    desactivado (gerencia lo apagó a propósito, ver ServicioActualizarIn).
    """
    sql = """
        SELECT id, tenant_id, nombre, duracion_minutos, precio, activo, creado_en
        FROM servicios
        WHERE tenant_id = $1 AND activo = true
        ORDER BY nombre
    """
    filas = await (conn.fetch(sql, tenant_id) if conn is not None else fetch_all(sql, tenant_id))
    return [Servicio.desde_fila(f) for f in filas]


# ============================================================
# Disponibilidad
# ============================================================
async def horarios_de_proveedor(
    proveedor_id: UUID, conn: Optional[asyncpg.Connection] = None
) -> list[calendario_slots.BloqueHorario]:
    sql = """
        SELECT dia_semana, hora_inicio, hora_fin
        FROM proveedor_horarios
        WHERE proveedor_id = $1
        ORDER BY dia_semana, hora_inicio
    """
    filas = await (
        conn.fetch(sql, proveedor_id) if conn is not None else fetch_all(sql, proveedor_id)
    )
    return [
        calendario_slots.BloqueHorario(
            dia_semana=f["dia_semana"], hora_inicio=f["hora_inicio"], hora_fin=f["hora_fin"]
        )
        for f in filas
    ]


async def excepciones_de_proveedor(
    proveedor_id: UUID,
    desde: date,
    hasta: date,
    conn: Optional[asyncpg.Connection] = None,
) -> list[calendario_slots.Excepcion]:
    sql = """
        SELECT fecha, disponible, hora_inicio, hora_fin
        FROM proveedor_excepciones
        WHERE proveedor_id = $1 AND fecha BETWEEN $2 AND $3
    """
    filas = await (
        conn.fetch(sql, proveedor_id, desde, hasta)
        if conn is not None
        else fetch_all(sql, proveedor_id, desde, hasta)
    )
    return [
        calendario_slots.Excepcion(
            fecha=f["fecha"],
            disponible=f["disponible"],
            hora_inicio=f["hora_inicio"],
            hora_fin=f["hora_fin"],
        )
        for f in filas
    ]


async def descansos_de_proveedor(
    proveedor_id: UUID,
    desde: Optional[date] = None,
    hasta: Optional[date] = None,
    conn: Optional[asyncpg.Connection] = None,
) -> list[calendario_slots.Descanso]:
    """
    Los recurrentes (dia_semana) siempre se incluyen; los puntuales (fecha)
    solo si caen en [desde, hasta] cuando se da un rango — mismo criterio
    que excepciones_de_proveedor, para no traer años de descansos sueltos
    al calcular la disponibilidad de un solo día.
    """
    sql = """
        SELECT dia_semana, fecha, hora_inicio, hora_fin
        FROM proveedor_descansos
        WHERE proveedor_id = $1
          AND (dia_semana IS NOT NULL OR $2::date IS NULL OR fecha BETWEEN $2 AND $3)
    """
    filas = await (
        conn.fetch(sql, proveedor_id, desde, hasta)
        if conn is not None
        else fetch_all(sql, proveedor_id, desde, hasta)
    )
    return [
        calendario_slots.Descanso(
            dia_semana=f["dia_semana"],
            fecha=f["fecha"],
            hora_inicio=f["hora_inicio"],
            hora_fin=f["hora_fin"],
        )
        for f in filas
    ]


async def reservas_ocupadas(
    tenant_id: UUID,
    proveedor_id: UUID,
    desde_utc: datetime,
    hasta_utc: datetime,
    conn: Optional[asyncpg.Connection] = None,
) -> list[calendario_slots.RangoOcupado]:
    """
    Reservas activas (no canceladas/no-asistió) del proveedor que
    traslapan [desde_utc, hasta_utc). Mismo criterio de estados que la
    condición WHERE del EXCLUDE en 16_calendarios.sql: una cita cancelada o
    a la que no llegó el cliente no ocupa el horario.
    """
    sql = """
        SELECT hora_inicio, hora_fin
        FROM reservas
        WHERE tenant_id = $1 AND proveedor_id = $2
          AND estado NOT IN ('cancelada', 'no_asistio')
          AND hora_inicio < $4 AND hora_fin > $3
    """
    filas = await (
        conn.fetch(sql, tenant_id, proveedor_id, desde_utc, hasta_utc)
        if conn is not None
        else fetch_all(sql, tenant_id, proveedor_id, desde_utc, hasta_utc)
    )
    return [calendario_slots.RangoOcupado(inicio=f["hora_inicio"], fin=f["hora_fin"]) for f in filas]


async def consultar_disponibilidad(
    tenant_id: UUID,
    servicio_id: UUID,
    proveedor_id: Optional[UUID],
    fecha_desde: date,
    fecha_hasta: date,
) -> list[dict]:
    """
    Slots libres para `servicio_id` entre fecha_desde y fecha_hasta.

    Si `proveedor_id` es None se consultan todos los proveedores activos
    del tenant; si el servicio no existe o está inactivo se devuelve una
    lista vacía (no es un error de n8n, simplemente no hay nada que
    ofrecer).
    """
    async with conexion() as conn:
        servicio = await servicio_del_tenant(servicio_id, tenant_id, conn)
        if servicio is None or not servicio.activo:
            return []

        servicios_tenant = await get_tenant_servicios(tenant_id, conn)
        zona = servicios_tenant.zona_horaria

        if proveedor_id is not None:
            proveedor = await proveedor_del_tenant(proveedor_id, tenant_id, conn)
            proveedores = [proveedor] if proveedor and proveedor.activo else []
        else:
            proveedores = await proveedores_activos(tenant_id, conn)

        rango_desde_utc = datetime.combine(fecha_desde, time.min, tzinfo=timezone.utc)
        rango_hasta_utc = datetime.combine(
            fecha_hasta + timedelta(days=1), time.min, tzinfo=timezone.utc
        )

        resultados: list[dict] = []
        for proveedor in proveedores:
            horarios = await horarios_de_proveedor(proveedor.id, conn)
            excepciones = await excepciones_de_proveedor(proveedor.id, fecha_desde, fecha_hasta, conn)
            descansos = await descansos_de_proveedor(proveedor.id, fecha_desde, fecha_hasta, conn)
            ocupados = await reservas_ocupadas(
                tenant_id, proveedor.id, rango_desde_utc, rango_hasta_utc, conn
            )
            inicios = calendario_slots.generar_slots(
                fecha_desde,
                fecha_hasta,
                servicio.duracion_minutos,
                horarios,
                excepciones,
                ocupados,
                zona,
                ahora_utc=datetime.now(timezone.utc),
                descansos=descansos,
            )
            duracion = timedelta(minutes=servicio.duracion_minutos)
            for inicio in inicios:
                resultados.append(
                    {
                        "proveedor_id": proveedor.id,
                        "proveedor_nombre": proveedor.nombre,
                        "hora_inicio": inicio,
                        "hora_fin": inicio + duracion,
                    }
                )

        resultados.sort(key=lambda r: r["hora_inicio"])
        return resultados


# ============================================================
# Reservas
# ============================================================
_COLUMNAS_RESERVA = """
    r.id, r.tenant_id, r.proveedor_id, p.nombre AS proveedor_nombre, p.color AS proveedor_color,
    r.servicio_id, s.nombre AS servicio_nombre, r.hora_inicio, r.hora_fin, r.estado,
    r.user_id, r.cliente_nombre, r.cliente_telefono, r.notas, r.precio_cobrado, r.metodo_pago, r.creado_en
"""


async def listar_reservas(
    tenant_id: UUID,
    desde: datetime,
    hasta: datetime,
    proveedor_id: Optional[UUID] = None,
) -> list[Reserva]:
    """Reservas que traslapan [desde, hasta), para alimentar la grilla del portal."""
    filas = await fetch_all(
        f"""
        SELECT {_COLUMNAS_RESERVA}
        FROM reservas r
        JOIN proveedores p ON p.id = r.proveedor_id
        JOIN servicios s ON s.id = r.servicio_id
        WHERE r.tenant_id = $1
          AND r.hora_inicio < $3 AND r.hora_fin > $2
          AND ($4::uuid IS NULL OR r.proveedor_id = $4)
        ORDER BY r.hora_inicio
        """,
        tenant_id,
        desde,
        hasta,
        proveedor_id,
    )
    return [Reserva.desde_fila(f) for f in filas]


async def reserva_del_tenant(reserva_id: UUID, tenant_id: UUID) -> Optional[Reserva]:
    fila = await fetch_one(
        f"""
        SELECT {_COLUMNAS_RESERVA}
        FROM reservas r
        JOIN proveedores p ON p.id = r.proveedor_id
        JOIN servicios s ON s.id = r.servicio_id
        WHERE r.id = $1 AND r.tenant_id = $2
        """,
        reserva_id,
        tenant_id,
    )
    return Reserva.desde_fila(fila) if fila else None


async def _dentro_de_horario_del_proveedor(
    proveedor_id: UUID, tenant_id: UUID, hora_inicio: datetime, hora_fin: datetime
) -> bool:
    """
    Valida la cita contra la jornada configurada del proveedor: horario
    semanal + excepción del día (si la hay). Cubre de un mismo golpe la
    jornada completa, el fin del turno (una cita que empieza dentro pero
    termina después del cierre no cabe entera en la ventana) y las pausas
    de un turno partido (el hueco entre dos bloques no es ninguna ventana).

    Se corre para CUALQUIER alta o cambio de horario, sea por el portal o
    por el agente de n8n: la jornada del barbero no es una sugerencia que
    solo respeta el cálculo de disponibilidad, es una regla de negocio que
    tiene que cumplir cualquier camino que llegue a escribir en `reservas`.
    """
    servicios_tenant = await get_tenant_servicios(tenant_id)
    fecha_local = hora_inicio.astimezone(ZoneInfo(servicios_tenant.zona_horaria)).date()
    horarios = await horarios_de_proveedor(proveedor_id)
    excepciones = await excepciones_de_proveedor(proveedor_id, fecha_local, fecha_local)
    descansos = await descansos_de_proveedor(proveedor_id, fecha_local, fecha_local)
    return calendario_slots.dentro_de_horario(
        hora_inicio, hora_fin, horarios, excepciones, servicios_tenant.zona_horaria, descansos
    )


async def crear_reserva(
    tenant_id: UUID,
    proveedor_id: UUID,
    servicio_id: UUID,
    hora_inicio: datetime,
    *,
    user_id: Optional[UUID],
    cliente_nombre: Optional[str],
    cliente_telefono: Optional[str],
    notas: Optional[str],
    actor: ActorAuditoria,
    idempotency_key: Optional[str] = None,
) -> tuple[Optional[Reserva], str]:
    """
    Devuelve (reserva, motivo) donde motivo es uno de:
    'creada', 'duplicado' (mismo idempotency_key de un intento anterior),
    'horario_ocupado', 'fuera_de_horario', 'proveedor_invalido',
    'servicio_invalido'.

    La fuente de verdad contra el traslape es el EXCLUDE de la tabla
    (reservas_sin_traslape), no un SELECT previo: entre leer y escribir cabe
    perfectamente otra reserva concurrente, y el candado de Postgres es el
    único que de verdad no se puede saltar. La jornada laboral, en cambio,
    no tiene un candado equivalente en la tabla (no hay forma limpia de
    expresarla como constraint), así que sí se valida antes con un SELECT.
    """
    proveedor = await proveedor_del_tenant(proveedor_id, tenant_id)
    if proveedor is None or not proveedor.activo:
        return None, "proveedor_invalido"

    servicio = await servicio_del_tenant(servicio_id, tenant_id)
    if servicio is None or not servicio.activo:
        return None, "servicio_invalido"

    hora_fin = hora_inicio + timedelta(minutes=servicio.duracion_minutos)

    if not await _dentro_de_horario_del_proveedor(proveedor_id, tenant_id, hora_inicio, hora_fin):
        return None, "fuera_de_horario"

    try:
        # transaccion() y no conexion(): el alta de la bitácora va en el
        # mismo commit que la reserva (ver _registrar_auditoria) — si algo
        # falla al escribir el rastro, la reserva tampoco debe quedar.
        async with transaccion() as conn:
            fila = await conn.fetchrow(
                """
                INSERT INTO reservas (
                    tenant_id, proveedor_id, servicio_id, user_id,
                    cliente_nombre, cliente_telefono, hora_inicio, hora_fin,
                    notas, idempotency_key
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT ON CONSTRAINT reservas_sin_traslape DO NOTHING
                RETURNING id, tenant_id, proveedor_id, servicio_id, user_id,
                          cliente_nombre, cliente_telefono, hora_inicio, hora_fin,
                          estado, notas, precio_cobrado, metodo_pago, creado_en
                """,
                tenant_id,
                proveedor_id,
                servicio_id,
                user_id,
                cliente_nombre,
                cliente_telefono,
                hora_inicio,
                hora_fin,
                notas,
                idempotency_key,
            )
            if fila is not None:
                await _registrar_auditoria(
                    conn,
                    tenant_id=tenant_id,
                    reserva_id=fila["id"],
                    evento="creada",
                    estado_anterior=None,
                    estado_nuevo="confirmada",
                    motivo=None,
                    datos_anteriores=None,
                    datos_nuevos={
                        "proveedor_id": str(proveedor_id),
                        "proveedor_nombre": proveedor.nombre,
                        "servicio_id": str(servicio_id),
                        "servicio_nombre": servicio.nombre,
                        "hora_inicio": hora_inicio.isoformat(),
                        "hora_fin": hora_fin.isoformat(),
                    },
                    actor=actor,
                )
    except asyncpg.exceptions.UniqueViolationError:
        # Reintento de n8n con el mismo idempotency_key: no es un rechazo,
        # es la misma reserva que ya se había creado en un intento previo.
        existente = await fetch_one(
            """
            SELECT id, tenant_id, proveedor_id, servicio_id, user_id,
                   cliente_nombre, cliente_telefono, hora_inicio, hora_fin,
                   estado, notas, precio_cobrado, metodo_pago, creado_en
            FROM reservas
            WHERE tenant_id = $1 AND idempotency_key = $2
            """,
            tenant_id,
            idempotency_key,
        )
        if existente is None:
            raise
        return Reserva.desde_fila(_con_proveedor_y_servicio(existente, proveedor, servicio)), "duplicado"

    if fila is None:
        # El INSERT chocó con reservas_sin_traslape (el arbitro del ON
        # CONFLICT). Un reintento de n8n con la MISMA reserva (mismo
        # proveedor y mismo horario) también cae por acá, porque su propio
        # rango se traslapa consigo mismo — nunca llega a evaluarse el
        # índice único de idempotency_key, así que ese chequeo no basta por
        # sí solo (ver el `except` de abajo, que cubre el caso contrario:
        # misma clave, horario distinto). Hay que distinguir "ya existe con
        # esta clave" (duplicado) de "otra reserva ya tiene ese horario"
        # (rechazo real) antes de decidir.
        if idempotency_key is not None:
            existente = await fetch_one(
                """
                SELECT id, tenant_id, proveedor_id, servicio_id, user_id,
                       cliente_nombre, cliente_telefono, hora_inicio, hora_fin,
                       estado, notas, precio_cobrado, metodo_pago, creado_en
                FROM reservas
                WHERE tenant_id = $1 AND idempotency_key = $2
                """,
                tenant_id,
                idempotency_key,
            )
            if existente is not None:
                return (
                    Reserva.desde_fila(_con_proveedor_y_servicio(existente, proveedor, servicio)),
                    "duplicado",
                )
        return None, "horario_ocupado"

    return Reserva.desde_fila(_con_proveedor_y_servicio(fila, proveedor, servicio)), "creada"


async def cancelar_reserva(
    reserva_id: UUID, tenant_id: UUID, motivo: Optional[str], actor: ActorAuditoria
) -> Optional[Reserva]:
    """None si la reserva no existe o no es de este tenant (404 lo decide el caller)."""
    async with transaccion() as conn:
        anterior = await conn.fetchrow(
            "SELECT estado FROM reservas WHERE id = $1 AND tenant_id = $2", reserva_id, tenant_id
        )
        if anterior is None:
            return None

        fila = await conn.fetchrow(
            """
            UPDATE reservas
            SET estado = 'cancelada', motivo_cancelacion = $3, actualizado_en = NOW()
            WHERE id = $1 AND tenant_id = $2
            RETURNING id, tenant_id, proveedor_id, servicio_id, user_id,
                      cliente_nombre, cliente_telefono, hora_inicio, hora_fin,
                      estado, notas, precio_cobrado, metodo_pago, creado_en
            """,
            reserva_id,
            tenant_id,
            motivo,
        )
        await _registrar_auditoria(
            conn,
            tenant_id=tenant_id,
            reserva_id=reserva_id,
            evento="cancelada",
            estado_anterior=anterior["estado"],
            estado_nuevo="cancelada",
            motivo=motivo,
            datos_anteriores=None,
            datos_nuevos=None,
            actor=actor,
        )

    proveedor = await proveedor_del_tenant(fila["proveedor_id"], tenant_id)
    servicio = await servicio_del_tenant(fila["servicio_id"], tenant_id)
    return Reserva.desde_fila(_con_proveedor_y_servicio(fila, proveedor, servicio))


async def reprogramar_reserva(
    reserva_id: UUID, tenant_id: UUID, nueva_hora_inicio: datetime, actor: ActorAuditoria
) -> tuple[Optional[Reserva], str]:
    """
    Devuelve (reserva, motivo) con motivo en 'reprogramada', 'horario_ocupado',
    'fuera_de_horario' o 'no_encontrada'. La duración se toma de la reserva
    ya guardada (no del servicio actual): si el servicio cambió de duración
    después, esta cita no se mueve retroactivamente.
    """
    actual = await fetch_one(
        "SELECT proveedor_id, estado, hora_inicio, hora_fin FROM reservas WHERE id = $1 AND tenant_id = $2",
        reserva_id,
        tenant_id,
    )
    if actual is None:
        return None, "no_encontrada"

    duracion = actual["hora_fin"] - actual["hora_inicio"]
    nueva_hora_fin = nueva_hora_inicio + duracion

    if not await _dentro_de_horario_del_proveedor(
        actual["proveedor_id"], tenant_id, nueva_hora_inicio, nueva_hora_fin
    ):
        return None, "fuera_de_horario"

    try:
        async with transaccion() as conn:
            fila = await conn.fetchrow(
                """
                UPDATE reservas
                SET hora_inicio = $3, hora_fin = $4, actualizado_en = NOW()
                WHERE id = $1 AND tenant_id = $2
                RETURNING id, tenant_id, proveedor_id, servicio_id, user_id,
                          cliente_nombre, cliente_telefono, hora_inicio, hora_fin,
                          estado, notas, precio_cobrado, metodo_pago, creado_en
                """,
                reserva_id,
                tenant_id,
                nueva_hora_inicio,
                nueva_hora_fin,
            )
            if fila is None:
                return None, "no_encontrada"

            await _registrar_auditoria(
                conn,
                tenant_id=tenant_id,
                reserva_id=reserva_id,
                evento="reprogramada",
                estado_anterior=actual["estado"],
                estado_nuevo=actual["estado"],
                motivo=None,
                datos_anteriores={
                    "hora_inicio": actual["hora_inicio"].isoformat(),
                    "hora_fin": actual["hora_fin"].isoformat(),
                },
                datos_nuevos={
                    "hora_inicio": nueva_hora_inicio.isoformat(),
                    "hora_fin": nueva_hora_fin.isoformat(),
                },
                actor=actor,
            )
    except asyncpg.exceptions.ExclusionViolationError:
        return None, "horario_ocupado"

    proveedor = await proveedor_del_tenant(fila["proveedor_id"], tenant_id)
    servicio = await servicio_del_tenant(fila["servicio_id"], tenant_id)
    return Reserva.desde_fila(_con_proveedor_y_servicio(fila, proveedor, servicio)), "reprogramada"


async def reasignar_reserva(
    reserva_id: UUID, tenant_id: UUID, nuevo_proveedor_id: UUID, actor: ActorAuditoria
) -> tuple[Optional[Reserva], str]:
    """
    Cambia el barbero de una cita ya agendada sin tocar su horario.
    Devuelve (reserva, motivo) con motivo en 'reasignada', 'proveedor_invalido',
    'fuera_de_horario', 'horario_ocupado' o 'no_encontrada'. Mismo criterio
    de validación que reprogramar_reserva, pero contra la jornada del NUEVO
    proveedor en vez de la del mismo.
    """
    actual = await fetch_one(
        "SELECT proveedor_id, servicio_id, estado, hora_inicio, hora_fin FROM reservas WHERE id = $1 AND tenant_id = $2",
        reserva_id,
        tenant_id,
    )
    if actual is None:
        return None, "no_encontrada"

    nuevo_proveedor = await proveedor_del_tenant(nuevo_proveedor_id, tenant_id)
    if nuevo_proveedor is None or not nuevo_proveedor.activo:
        return None, "proveedor_invalido"

    if actual["proveedor_id"] == nuevo_proveedor_id:
        # Nada que cambiar ni que auditar: es el mismo proveedor de siempre.
        return await reserva_del_tenant(reserva_id, tenant_id), "reasignada"

    if not await _dentro_de_horario_del_proveedor(
        nuevo_proveedor_id, tenant_id, actual["hora_inicio"], actual["hora_fin"]
    ):
        return None, "fuera_de_horario"

    proveedor_anterior = await proveedor_del_tenant(actual["proveedor_id"], tenant_id)

    try:
        async with transaccion() as conn:
            fila = await conn.fetchrow(
                """
                UPDATE reservas
                SET proveedor_id = $3, actualizado_en = NOW()
                WHERE id = $1 AND tenant_id = $2
                RETURNING id, tenant_id, proveedor_id, servicio_id, user_id,
                          cliente_nombre, cliente_telefono, hora_inicio, hora_fin,
                          estado, notas, precio_cobrado, metodo_pago, creado_en
                """,
                reserva_id,
                tenant_id,
                nuevo_proveedor_id,
            )
            if fila is None:
                return None, "no_encontrada"

            await _registrar_auditoria(
                conn,
                tenant_id=tenant_id,
                reserva_id=reserva_id,
                evento="cambio_barbero",
                estado_anterior=actual["estado"],
                estado_nuevo=actual["estado"],
                motivo=None,
                datos_anteriores={
                    "proveedor_id": str(actual["proveedor_id"]),
                    "proveedor_nombre": proveedor_anterior.nombre if proveedor_anterior else None,
                },
                datos_nuevos={
                    "proveedor_id": str(nuevo_proveedor_id),
                    "proveedor_nombre": nuevo_proveedor.nombre,
                },
                actor=actor,
            )
    except asyncpg.exceptions.ExclusionViolationError:
        return None, "horario_ocupado"

    servicio = await servicio_del_tenant(fila["servicio_id"], tenant_id)
    return Reserva.desde_fila(_con_proveedor_y_servicio(fila, nuevo_proveedor, servicio)), "reasignada"


async def cambiar_estado_reserva(
    reserva_id: UUID,
    tenant_id: UUID,
    estado: str,
    actor: ActorAuditoria,
    *,
    precio_cobrado: Optional[Decimal] = None,
    metodo_pago: Optional[str] = None,
) -> Optional[Reserva]:
    """
    Marca la reserva como 'completada' o 'no_asistio' después de la hora de
    la cita. `precio_cobrado`/`metodo_pago` solo aplican a 'completada' (el
    router ya lo valida antes de llegar acá) — quedan grabados en la propia
    reserva para el corte de caja diario, snapshot del momento del cobro y
    no del precio de catálogo (que puede cambiar después).
    """
    async with transaccion() as conn:
        anterior = await conn.fetchrow(
            "SELECT estado FROM reservas WHERE id = $1 AND tenant_id = $2", reserva_id, tenant_id
        )
        if anterior is None:
            return None

        fila = await conn.fetchrow(
            """
            UPDATE reservas
            SET estado = $3, precio_cobrado = $4, metodo_pago = $5, actualizado_en = NOW()
            WHERE id = $1 AND tenant_id = $2
            RETURNING id, tenant_id, proveedor_id, servicio_id, user_id,
                      cliente_nombre, cliente_telefono, hora_inicio, hora_fin,
                      estado, notas, precio_cobrado, metodo_pago, creado_en
            """,
            reserva_id,
            tenant_id,
            estado,
            precio_cobrado,
            metodo_pago,
        )
        await _registrar_auditoria(
            conn,
            tenant_id=tenant_id,
            reserva_id=reserva_id,
            evento=estado,
            estado_anterior=anterior["estado"],
            estado_nuevo=estado,
            motivo=None,
            datos_anteriores=None,
            datos_nuevos=(
                {"precio_cobrado": str(precio_cobrado), "metodo_pago": metodo_pago}
                if estado == "completada"
                else None
            ),
            actor=actor,
        )

    proveedor = await proveedor_del_tenant(fila["proveedor_id"], tenant_id)
    servicio = await servicio_del_tenant(fila["servicio_id"], tenant_id)
    return Reserva.desde_fila(_con_proveedor_y_servicio(fila, proveedor, servicio))


async def corte_diario(
    tenant_id: UUID, fecha: date, proveedor_id: Optional[UUID] = None
) -> dict:
    """
    Resumen de caja de UN día local del negocio: cuántos servicios se
    dieron y cuánto se cobró, contando solo 'completada' (Escenario 4 de la
    historia del corte diario) — 'confirmada' todavía no se prestó,
    'cancelada'/'no_asistio' no generaron ingreso.

    El "día" se mide en hora LOCAL del negocio (zona_horaria de
    tenant_servicios), no UTC: un corte de las 23:50 locales no debe
    aparecer en el día siguiente solo porque en UTC ya cruzó la medianoche.
    La suma usa Decimal de un tirón sobre lo que ya trajo Postgres, nunca
    float, por la consideración técnica de precisión monetaria.
    """
    servicios_tenant = await get_tenant_servicios(tenant_id)
    zona = ZoneInfo(servicios_tenant.zona_horaria)
    desde_utc = datetime.combine(fecha, time.min, tzinfo=zona).astimezone(timezone.utc)
    hasta_utc = datetime.combine(fecha + timedelta(days=1), time.min, tzinfo=zona).astimezone(timezone.utc)

    filas = await fetch_all(
        f"""
        SELECT {_COLUMNAS_RESERVA}
        FROM reservas r
        JOIN proveedores p ON p.id = r.proveedor_id
        JOIN servicios s ON s.id = r.servicio_id
        WHERE r.tenant_id = $1 AND r.estado = 'completada'
          AND r.hora_inicio >= $2 AND r.hora_inicio < $3
          AND ($4::uuid IS NULL OR r.proveedor_id = $4)
        ORDER BY r.hora_inicio
        """,
        tenant_id,
        desde_utc,
        hasta_utc,
        proveedor_id,
    )
    servicios_del_dia = [Reserva.desde_fila(f) for f in filas]
    total_cobrado = sum((r.precio_cobrado or Decimal("0") for r in servicios_del_dia), Decimal("0"))

    return {
        "fecha": fecha,
        "total_servicios": len(servicios_del_dia),
        "total_cobrado": total_cobrado,
        "servicios": servicios_del_dia,
    }


async def reservas_fuera_de_nuevo_horario(
    proveedor_id: UUID,
    tenant_id: UUID,
    bloques_nuevos: list[tuple[int, time, time]],
) -> list[Reserva]:
    """
    Citas futuras y todavía 'confirmada' del proveedor que YA NO caben en el
    horario que se está por guardar. Se calcula sobre `bloques_nuevos` en
    memoria, ANTES de escribir nada — releer proveedor_horarios acá seguiría
    trayendo el horario viejo.

    No cancela ni bloquea el guardado: gerencia decide qué hacer con cada
    cita (avisar al cliente, reprogramarla a mano, o dejar el horario como
    estaba). Devolver la lista es lo que le permite al portal avisar en vez
    de dejar citas huérfanas sin que nadie se entere.
    """
    servicios_tenant = await get_tenant_servicios(tenant_id)
    zona = servicios_tenant.zona_horaria
    horarios_nuevos = [
        calendario_slots.BloqueHorario(dia_semana=d, hora_inicio=hi, hora_fin=hf)
        for d, hi, hf in bloques_nuevos
    ]

    horizonte = datetime.now(timezone.utc) + timedelta(days=3650)
    futuras = await listar_reservas(tenant_id, datetime.now(timezone.utc), horizonte, proveedor_id)

    conflictivas = []
    for r in futuras:
        if r.estado != "confirmada":
            continue
        fecha_local = r.hora_inicio.astimezone(ZoneInfo(zona)).date()
        excepciones = await excepciones_de_proveedor(proveedor_id, fecha_local, fecha_local)
        if not calendario_slots.dentro_de_horario(
            r.hora_inicio, r.hora_fin, horarios_nuevos, excepciones, zona
        ):
            conflictivas.append(r)
    return conflictivas


# ============================================================
# Horarios semanales y excepciones (escritura)
# ============================================================
async def listar_horarios_out(proveedor_id: UUID) -> list[dict]:
    """Para el router del portal (necesita `id` y `proveedor_id`, que el cálculo puro no usa)."""
    filas = await fetch_all(
        """
        SELECT id, proveedor_id, dia_semana, hora_inicio, hora_fin
        FROM proveedor_horarios
        WHERE proveedor_id = $1
        ORDER BY dia_semana, hora_inicio
        """,
        proveedor_id,
    )
    return [dict(f) for f in filas]


async def reemplazar_horarios(
    proveedor_id: UUID, tenant_id: UUID, bloques: list[tuple[int, time, time]]
) -> list[dict]:
    """Reemplaza TODA la semana del proveedor de una vez (borra + inserta en una transacción)."""
    async with transaccion() as conn:
        await conn.execute("DELETE FROM proveedor_horarios WHERE proveedor_id = $1", proveedor_id)
        filas = []
        for dia_semana, hora_inicio, hora_fin in bloques:
            fila = await conn.fetchrow(
                """
                INSERT INTO proveedor_horarios (tenant_id, proveedor_id, dia_semana, hora_inicio, hora_fin)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, proveedor_id, dia_semana, hora_inicio, hora_fin
                """,
                tenant_id,
                proveedor_id,
                dia_semana,
                hora_inicio,
                hora_fin,
            )
            filas.append(dict(fila))
    return filas


async def listar_excepciones_out(proveedor_id: UUID, desde: date, hasta: date) -> list[dict]:
    """
    Para el router del portal (necesita `id` para poder borrar una
    excepción), a diferencia de `excepciones_de_proveedor` que solo
    alimenta el cálculo puro de calendario_slots y no expone id.
    """
    filas = await fetch_all(
        """
        SELECT id, proveedor_id, fecha, disponible, hora_inicio, hora_fin
        FROM proveedor_excepciones
        WHERE proveedor_id = $1 AND fecha BETWEEN $2 AND $3
        ORDER BY fecha
        """,
        proveedor_id,
        desde,
        hasta,
    )
    return [dict(f) for f in filas]


async def eliminar_excepcion(excepcion_id: UUID, tenant_id: UUID) -> bool:
    resultado = await execute(
        "DELETE FROM proveedor_excepciones WHERE id = $1 AND tenant_id = $2",
        excepcion_id,
        tenant_id,
    )
    return resultado.endswith(" 1")


async def guardar_excepcion(
    proveedor_id: UUID,
    tenant_id: UUID,
    fecha: date,
    disponible: bool,
    hora_inicio: Optional[time],
    hora_fin: Optional[time],
) -> dict:
    fila = await fetch_one(
        """
        INSERT INTO proveedor_excepciones (tenant_id, proveedor_id, fecha, disponible, hora_inicio, hora_fin)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (proveedor_id, fecha) DO UPDATE SET
            disponible  = EXCLUDED.disponible,
            hora_inicio = EXCLUDED.hora_inicio,
            hora_fin    = EXCLUDED.hora_fin
        RETURNING id, proveedor_id, fecha, disponible, hora_inicio, hora_fin
        """,
        tenant_id,
        proveedor_id,
        fecha,
        disponible,
        hora_inicio,
        hora_fin,
    )
    return dict(fila)


# ============================================================
# Descansos (pausas dentro de la jornada)
# ============================================================
async def _ventanas_base_para_descanso(
    proveedor_id: UUID, *, dia_semana: Optional[int], fecha: Optional[date]
) -> list[tuple[time, time]]:
    """
    Ventanas contra las que se valida un descanso NUEVO — la jornada
    configurada, antes de restarle ningún otro descanso ya guardado (si no,
    un segundo descanso quedaría medido contra el hueco que dejó el primero
    en vez de contra la jornada real).

    Para uno recurrente (dia_semana) es el horario semanal de ese día. Para
    uno puntual (fecha) es la excepción de esa fecha si existe (reemplaza al
    horario semanal, igual que en ventanas_del_dia) o si no el horario
    semanal del día de la semana que le toca.
    """
    if dia_semana is not None:
        horarios = await horarios_de_proveedor(proveedor_id)
        return [(h.hora_inicio, h.hora_fin) for h in horarios if h.dia_semana == dia_semana]

    excepciones = await excepciones_de_proveedor(proveedor_id, fecha, fecha)
    if excepciones:
        excepcion = excepciones[0]
        if not excepcion.disponible:
            return []
        return [(excepcion.hora_inicio, excepcion.hora_fin)]

    horarios = await horarios_de_proveedor(proveedor_id)
    return [(h.hora_inicio, h.hora_fin) for h in horarios if h.dia_semana == fecha.weekday()]


async def reservas_en_conflicto_con_descanso(
    proveedor_id: UUID,
    tenant_id: UUID,
    *,
    dia_semana: Optional[int],
    fecha: Optional[date],
    hora_inicio: time,
    hora_fin: time,
) -> list[Reserva]:
    """
    Citas futuras y todavía 'confirmada' que caen dentro del descanso que se
    está por guardar. A diferencia de un horario más corto
    (reservas_fuera_de_nuevo_horario, que solo avisa), acá SÍ bloquea el
    guardado — historia de usuario del barbero, Escenario 5: el descanso no
    se fija hasta que gerencia resuelva esa cita.
    """
    servicios_tenant = await get_tenant_servicios(tenant_id)
    zona = ZoneInfo(servicios_tenant.zona_horaria)

    horizonte = datetime.now(timezone.utc) + timedelta(days=3650)
    futuras = await listar_reservas(tenant_id, datetime.now(timezone.utc), horizonte, proveedor_id)

    conflictivas = []
    for r in futuras:
        if r.estado != "confirmada":
            continue
        fecha_local = r.hora_inicio.astimezone(zona).date()
        if fecha is not None and fecha_local != fecha:
            continue
        if fecha is None and fecha_local.weekday() != dia_semana:
            continue
        descanso_inicio = datetime.combine(fecha_local, hora_inicio, tzinfo=zona)
        descanso_fin = datetime.combine(fecha_local, hora_fin, tzinfo=zona)
        if calendario_slots.se_traslapan(r.hora_inicio, r.hora_fin, descanso_inicio, descanso_fin):
            conflictivas.append(r)
    return conflictivas


async def guardar_descanso(
    proveedor_id: UUID,
    tenant_id: UUID,
    *,
    dia_semana: Optional[int],
    fecha: Optional[date],
    hora_inicio: time,
    hora_fin: time,
    etiqueta: Optional[str],
) -> tuple[Optional[dict], str]:
    """
    Devuelve (fila, motivo) con motivo en 'creado', 'fuera_de_jornada' (no
    cabe en la jornada configurada ese día) o 'con_citas' (choca con una
    cita ya confirmada).
    """
    ventanas_base = await _ventanas_base_para_descanso(proveedor_id, dia_semana=dia_semana, fecha=fecha)
    if not calendario_slots.descanso_dentro_de_jornada(hora_inicio, hora_fin, ventanas_base):
        return None, "fuera_de_jornada"

    conflictos = await reservas_en_conflicto_con_descanso(
        proveedor_id,
        tenant_id,
        dia_semana=dia_semana,
        fecha=fecha,
        hora_inicio=hora_inicio,
        hora_fin=hora_fin,
    )
    if conflictos:
        return None, "con_citas"

    fila = await fetch_one(
        """
        INSERT INTO proveedor_descansos (tenant_id, proveedor_id, dia_semana, fecha, hora_inicio, hora_fin, etiqueta)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id, proveedor_id, dia_semana, fecha, hora_inicio, hora_fin, etiqueta
        """,
        tenant_id,
        proveedor_id,
        dia_semana,
        fecha,
        hora_inicio,
        hora_fin,
        etiqueta,
    )
    return dict(fila), "creado"


async def listar_descansos_out(proveedor_id: UUID) -> list[dict]:
    filas = await fetch_all(
        """
        SELECT id, proveedor_id, dia_semana, fecha, hora_inicio, hora_fin, etiqueta
        FROM proveedor_descansos
        WHERE proveedor_id = $1
        ORDER BY dia_semana NULLS LAST, fecha NULLS LAST, hora_inicio
        """,
        proveedor_id,
    )
    return [dict(f) for f in filas]


async def eliminar_descanso(descanso_id: UUID, tenant_id: UUID) -> bool:
    resultado = await execute(
        "DELETE FROM proveedor_descansos WHERE id = $1 AND tenant_id = $2",
        descanso_id,
        tenant_id,
    )
    return resultado.endswith(" 1")


# ============================================================
# Bitácora de ediciones de servicios (servicio_auditoria)
# ============================================================
def _valor_auditable(valor):
    """Decimal no es serializable a JSON tal cual: lo demás (str/bool/int) sí."""
    return str(valor) if isinstance(valor, Decimal) else valor


async def registrar_edicion_servicio(
    tenant_id: UUID,
    servicio_id: UUID,
    anterior: "Servicio",
    datos: ServicioActualizarIn,
    *,
    actor_email: str,
    actor_portal_user_id: UUID,
) -> None:
    """
    Compara `datos` (los campos que el PATCH quiso tocar, `None` = sin
    cambio) contra `anterior` y, si de verdad cambió algo, deja una fila en
    servicio_auditoria con solo esos campos -- no el servicio completo, mismo
    criterio que reserva_auditoria. Un PATCH que reenvía el mismo valor que
    ya tenía (o que solo trae `activo`) no genera ruido en la bitácora.
    """
    anteriores: dict = {}
    nuevos: dict = {}
    for campo in ("nombre", "duracion_minutos", "precio"):
        valor_nuevo = getattr(datos, campo)
        if valor_nuevo is None:
            continue
        valor_anterior = getattr(anterior, campo)
        if valor_anterior != valor_nuevo:
            anteriores[campo] = _valor_auditable(valor_anterior)
            nuevos[campo] = _valor_auditable(valor_nuevo)

    if not nuevos:
        return

    await execute(
        """
        INSERT INTO servicio_auditoria (
            tenant_id, servicio_id, datos_anteriores, datos_nuevos,
            actor_email, actor_portal_user_id
        )
        VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6)
        """,
        tenant_id,
        servicio_id,
        anteriores,
        nuevos,
        actor_email,
        actor_portal_user_id,
    )
