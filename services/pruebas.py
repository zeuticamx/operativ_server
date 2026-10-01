"""
Plan de prueba otorgado por gerencia de plataforma.

Una prueba no es un mecanismo aparte: es una fila de tenant_subscriptions
'activa' con `origen = 'prueba'` y `fecha_renovacion` = fin de la prueba
(ver 26_plan_prueba.sql). De ahí en adelante todo lo existente la trata
como a un plan pagado:

  - services/acceso_plan.py da las herramientas que incluye el plan, y
    corta en el instante en que pasa `fecha_renovacion` (lo lee al vuelo);
  - jobs/pagos_background.py la pasa a 'pausada' en su siguiente pasada,
    y desde ahí también services/acceso_pagos.py (el agente de n8n) la ve
    vencida;
  - un pago aprobado (services/pagos.activar_suscripcion) la reemplaza por
    un plan pagado.

Reglas propias de la prueba:
  - Dura como máximo 3 meses calendario desde que se otorga.
  - Solo planes activos del catálogo (los retirados no se ofrecen).
  - No pisa un plan pagado vigente (409): se perdería lo que el negocio
    pagó. Sí reemplaza a otra prueba (para extenderla o cambiar de plan) y
    a una suscripción pausada o cancelada.
  - `precio_monthly = 0`: el MRR y los márgenes de gerencia suman
    `precio_monthly` de las activas, y una prueba no es ingreso.
"""

import calendar
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException, status

from schemas import OtorgarPruebaIn, UnidadDuracionPrueba
from services import herramientas_calendario
from services.creditos import cargar_creditos_plan, expirar_creditos_plan
from services.gerencia import registrar_auditoria
from session import transaccion

MESES_MAXIMO_PRUEBA = 3


def sumar_meses(fecha: datetime, meses: int) -> datetime:
    """
    Mismo día `meses` después; si ese mes es más corto, su último día
    (31 ene + 1 mes = 28/29 feb). Sin dateutil: es la única cuenta de
    calendario que hace falta y no justifica una dependencia.
    """
    indice = fecha.month - 1 + meses
    anio = fecha.year + indice // 12
    mes = indice % 12 + 1
    dia = min(fecha.day, calendar.monthrange(anio, mes)[1])
    return fecha.replace(year=anio, month=mes, day=dia)


def limite_prueba(ahora: datetime) -> datetime:
    return sumar_meses(ahora, MESES_MAXIMO_PRUEBA)


def calcular_expiracion(
    unidad: UnidadDuracionPrueba,
    cantidad: int | None,
    fecha_expiracion: datetime | None,
    ahora: datetime,
) -> datetime:
    """
    Cuándo vence la prueba, en UTC. `ahora` se inyecta para poder probar
    el cálculo sin depender del reloj.

    400 si la fecha cae en el pasado o más allá de los 3 meses. La forma
    del pedido (qué campo va con qué unidad) ya la validó OtorgarPruebaIn.
    """
    ahora = ahora.astimezone(timezone.utc)

    if unidad == "dias":
        expiracion = ahora + timedelta(days=cantidad or 0)
    elif unidad == "semanas":
        expiracion = ahora + timedelta(weeks=cantidad or 0)
    else:
        if fecha_expiracion is None or fecha_expiracion.tzinfo is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La fecha de expiración tiene que incluir zona horaria",
            )
        expiracion = fecha_expiracion.astimezone(timezone.utc)

    if expiracion <= ahora:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La fecha de expiración ya pasó",
        )

    limite = limite_prueba(ahora)
    if expiracion > limite:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"La prueba dura como máximo {MESES_MAXIMO_PRUEBA} meses "
                f"(hasta el {limite:%d/%m/%Y})"
            ),
        )

    return expiracion


async def otorgar_prueba(
    tenant_id: UUID,
    datos: OtorgarPruebaIn,
    *,
    gerente_email: str,
    gerente_id: UUID,
    ahora: datetime | None = None,
) -> datetime:
    """Deja el plan de prueba vigente y devuelve cuándo vence."""
    ahora = ahora or datetime.now(timezone.utc)
    expiracion = calcular_expiracion(datos.unidad, datos.cantidad, datos.fecha_expiracion, ahora)

    async with transaccion() as conn:
        existe = await conn.fetchval("SELECT 1 FROM tenants WHERE id = $1", tenant_id)
        if existe is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Negocio no encontrado",
            )

        plan = await conn.fetchrow(
            """
            SELECT nombre, activo, agente_ia_activo, gestion_vendedores_activo,
                   calendario_activo
            FROM planes
            WHERE nombre = $1
            """,
            datos.plan,
        )
        if plan is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Plan no encontrado",
            )
        if not plan["activo"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Ese plan está retirado del catálogo: no se puede otorgar como prueba",
            )

        # FOR UPDATE: un pago que se aprueba mientras gerencia otorga la
        # prueba se serializa acá en vez de pisarse con ella.
        actual = await conn.fetchrow(
            """
            SELECT plan, estado, origen, fecha_renovacion
            FROM tenant_subscriptions
            WHERE tenant_id = $1
            FOR UPDATE
            """,
            tenant_id,
        )
        if (
            actual is not None
            and actual["origen"] == "pago"
            and actual["estado"] == "activa"
            and (actual["fecha_renovacion"] is None or actual["fecha_renovacion"] > ahora)
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"El negocio tiene un plan pagado vigente ({actual['plan']}). "
                    "Una prueba lo reemplazaría y perdería lo que ya pagó."
                ),
            )

        await conn.execute(
            """
            INSERT INTO tenant_subscriptions
                (tenant_id, plan, estado, precio_monthly, fecha_inicio,
                 fecha_renovacion, intentos_fallidos, origen, otorgada_por)
            VALUES ($1, $2, 'activa', $3, $4, $5, 0, 'prueba', $6)
            ON CONFLICT (tenant_id) DO UPDATE SET
                plan              = EXCLUDED.plan,
                estado            = 'activa',
                precio_monthly    = EXCLUDED.precio_monthly,
                fecha_inicio      = EXCLUDED.fecha_inicio,
                fecha_renovacion  = EXCLUDED.fecha_renovacion,
                intentos_fallidos = 0,
                origen            = 'prueba',
                otorgada_por      = EXCLUDED.otorgada_por,
                updated_at        = NOW()
            """,
            tenant_id,
            plan["nombre"],
            Decimal(0),
            ahora,
            expiracion,
            gerente_email,
        )

        # Mismo criterio que activar_suscripcion: el plan manda sobre los
        # dos interruptores de tenant_servicios. Además, si el plan incluye
        # calendario se deja encendido: el plan es solo el derecho, y sin
        # `calendario_activo` el agente no puede consultar ni reservar (409).
        # Solo se enciende, nunca se apaga: si ya lo tenía prendido, sigue.
        await conn.execute(
            """
            INSERT INTO tenant_servicios
                (tenant_id, agente_ia_activo, gestion_vendedores_activo,
                 calendario_activo, actualizado_en)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (tenant_id) DO UPDATE SET
                agente_ia_activo          = EXCLUDED.agente_ia_activo,
                gestion_vendedores_activo = EXCLUDED.gestion_vendedores_activo,
                calendario_activo         = tenant_servicios.calendario_activo
                                            OR EXCLUDED.calendario_activo,
                actualizado_en            = NOW()
            """,
            tenant_id,
            plan["agente_ia_activo"],
            plan["gestion_vendedores_activo"],
            plan["calendario_activo"],
        )

        # Cuota de créditos del plan, hasta el fin de la prueba. Cada
        # otorgamiento es una carga nueva (referencia propia): extender o
        # cambiar de plan reinicia la bolsa, igual que una renovación pagada.
        await cargar_creditos_plan(
            conn,
            tenant_id,
            plan["nombre"],
            expiracion,
            f"prueba:{uuid4()}",
            f"Créditos del plan {plan['nombre']} (prueba otorgada por {gerente_email})",
        )

        await registrar_auditoria(
            actor_email=gerente_email,
            actor_portal_user_id=gerente_id,
            accion="prueba_otorgada",
            tenant_id=tenant_id,
            detalle={
                "plan": plan["nombre"],
                "vence": expiracion.isoformat(),
                "duracion": (
                    f"{datos.cantidad} {datos.unidad}" if datos.unidad != "fecha" else "fecha límite"
                ),
                "reemplaza": (
                    f"{actual['plan']} ({actual['origen']}, {actual['estado']})"
                    if actual is not None
                    else None
                ),
                "calendario_encendido": plan["calendario_activo"],
                "motivo": datos.motivo,
            },
            conn=conn,
        )

    # Fuera de la transacción de la prueba: si esto falla no puede
    # deshacerla (sincronizar nunca propaga errores).
    if plan["calendario_activo"]:
        await herramientas_calendario.sincronizar(tenant_id, True)

    return expiracion


async def revocar_prueba(
    tenant_id: UUID,
    motivo: str,
    *,
    gerente_email: str,
    gerente_id: UUID,
) -> None:
    """
    Termina una prueba antes de tiempo. Queda 'cancelada' (no se borra la
    fila): un negocio sin fila en tenant_subscriptions ni créditos cuenta
    como "nunca pagó" en acceso_pagos y el agente seguiría contestando.
    """
    async with transaccion() as conn:
        actual = await conn.fetchrow(
            """
            SELECT t.id, s.plan, s.estado, s.origen, s.fecha_renovacion
            FROM tenants t
            LEFT JOIN tenant_subscriptions s ON s.tenant_id = t.id
            WHERE t.id = $1
            FOR UPDATE OF t
            """,
            tenant_id,
        )
        if actual is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Negocio no encontrado",
            )
        if actual["origen"] != "prueba" or actual["estado"] != "activa":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El negocio no tiene una prueba vigente",
            )

        await conn.execute(
            """
            UPDATE tenant_subscriptions
               SET estado = 'cancelada', fecha_renovacion = NOW(), updated_at = NOW()
             WHERE tenant_id = $1
            """,
            tenant_id,
        )
        await expirar_creditos_plan(
            conn, tenant_id, f"Prueba revocada por {gerente_email}: {motivo}"
        )

        await registrar_auditoria(
            actor_email=gerente_email,
            actor_portal_user_id=gerente_id,
            accion="prueba_revocada",
            tenant_id=tenant_id,
            detalle={
                "plan": actual["plan"],
                "vencia": actual["fecha_renovacion"].isoformat()
                if actual["fecha_renovacion"]
                else None,
                "motivo": motivo,
            },
            conn=conn,
        )
