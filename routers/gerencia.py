"""
Panel de plataforma: lo que ve el equipo de OperativAI, no un negocio.

Todo el router exige nivel gerencia (tabla gerencia_users, ver deps.py).
No confundirlo con el rol owner/superadmin: ese es "manda dentro de SU
negocio" y está limitado a su tenant_id. Este nivel no tiene tenant y ve a
todos, así que ninguna consulta de acá filtra por tenant salvo cuando la
pantalla lo pide explícitamente.

Las tres acciones que cambian algo (estado, servicios, créditos) escriben
en gerencia_auditoria dentro de la misma transacción que el cambio: una
suspensión aplicada que no quedó registrada es peor que una que no se
aplicó.
"""

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from deps import UsuarioActual, gerencia_plataforma_actual
from schemas import (
    AjusteCreditosIn,
    AjusteCreditosOut,
    CambiarEstadoTenantIn,
    CambiarServiciosTenantIn,
    ConsumoModeloOut,
    ConsumoOut,
    ConsumoTenantOut,
    EntradaAuditoriaOut,
    EstadoTenantPlataforma,
    PuntoConsumoOut,
    ResumenGerenciaOut,
    TenantGerenciaOut,
    TenantsGerenciaOut,
    TransaccionOut,
)
from services.gerencia import (
    DIAS_MAXIMO,
    DIAS_POR_DEFECTO,
    Consumo,
    consumo_global,
    consumo_por_dia,
    consumo_por_modelo,
    rango_dias,
    registrar_auditoria,
)
from session import fetch_all, fetch_one, transaccion

router = APIRouter(
    prefix="/gerencia",
    tags=["gerencia"],
    dependencies=[Depends(gerencia_plataforma_actual)],
)

# Cuántos tenants trae una página del listado.
LIMITE_POR_DEFECTO = 25
LIMITE_MAXIMO = 100

# Columnas por las que se puede ordenar el listado. Diccionario y no
# interpolación del parámetro: el valor que llega por querystring nunca
# toca el SQL, solo elige una entrada de acá.
ORDENES = {
    "consumo": "tokens_total DESC NULLS LAST, nombre ASC",
    "gasto": "costo_usd DESC NULLS LAST, nombre ASC",
    "alta": "alta DESC NULLS LAST",
    "nombre": "nombre ASC",
    "actividad": "ultimo_mensaje DESC NULLS LAST",
    "ingreso": "precio_monthly DESC NULLS LAST, nombre ASC",
}

# El gate de pagos escrito en SQL, para poder resolverlo de una sola vez
# para toda la página en vez de una consulta por fila.
#
# OJO: es un espejo de services/acceso_pagos.py, que sigue siendo la
# fuente de verdad (es la que responde n8n en /api/eventos). Si cambia la
# regla allá, hay que cambiarla acá — tests/test_gerencia.py compara las
# dos implementaciones justamente para que no se separen en silencio.
#
# IS NOT DISTINCT FROM y no `= 'activa'`: un tenant sin suscripción tiene
# estado_suscripcion NULL, y `NULL = 'activa'` da NULL, no FALSE. Ese NULL
# se propaga por el OR y el resultado entero sale NULL — que en JSON viaja
# como `null` y rompe el booleano del schema.
SQL_AGENTE_OPERANDO = """
    g.agente_ia_activo
    AND g.estado NOT IN ('suspendido', 'baja')
    AND (
        (g.estado_suscripcion IS NULL AND cr.creditos_disponibles IS NULL)
        OR g.estado_suscripcion IS NOT DISTINCT FROM 'activa'
        OR COALESCE(cr.creditos_disponibles, 0) > 0
    )
"""


def _filtros(*, p_estado: int, p_patron: int) -> str:
    """
    WHERE del listado, con los números de parámetro que le toquen.

    Los números salen de acá y no del querystring: lo único que interpola
    esta función son dos enteros que pone el propio código.
    """
    return f"""
        WHERE (${p_estado}::text IS NULL OR g.estado = ${p_estado})
          AND (
            ${p_patron}::text IS NULL
            OR g.nombre ILIKE ${p_patron}
            OR g.tenant_id::text ILIKE ${p_patron}
            OR EXISTS (
                SELECT 1 FROM portal_users pu
                WHERE pu.tenant_id = g.tenant_id AND pu.email ILIKE ${p_patron}
            )
          )
    """


def _consumo_out(c: Consumo) -> ConsumoTenantOut:
    return ConsumoTenantOut(
        tokens_entrada=c.tokens_entrada,
        tokens_salida=c.tokens_salida,
        tokens_total=c.tokens_total,
        costo_usd=c.costo_usd,
        llamadas=c.llamadas,
    )


# ============================================================
# Resumen
# ============================================================
@router.get("/resumen", response_model=ResumenGerenciaOut)
async def resumen(
    dias: int = Query(DIAS_POR_DEFECTO, ge=1, le=DIAS_MAXIMO),
):
    """
    La portada del panel: cuántos negocios hay, en qué estado, cuánto se
    consumió y cuánto entró en la ventana.

    `tenants_en_riesgo` es el número que justifica la pantalla: negocios
    con suscripción activa y cero mensajes en el período. Pagan y no usan,
    que es exactamente el que se da de baja el mes que viene.
    """
    r = await rango_dias(dias)

    fila = await fetch_one(
        """
        -- $1/$2 llevan ::timestamptz en TODOS sus usos a propósito.
        -- `tenants.created_at` y `messages.created_at` son TIMESTAMP sin
        -- zona (son tablas de n8n, ver 00_base_local.sql) y
        -- `tenant_transactions.created_at` es TIMESTAMPTZ. Sin el cast,
        -- Postgres infiere un único tipo para el parámetro a partir de la
        -- primera comparación, se queda con `timestamp`, y asyncpg no
        -- puede mandarle un datetime con zona.
        WITH estados AS (
            SELECT
                COUNT(*)                                             AS total,
                COUNT(*) FILTER (WHERE estado = 'activo')            AS activos,
                COUNT(*) FILTER (WHERE estado = 'prueba')            AS prueba,
                COUNT(*) FILTER (WHERE estado = 'suspendido')        AS suspendidos,
                COUNT(*) FILTER (WHERE estado = 'baja')              AS baja,
                COUNT(*) FILTER (WHERE alta >= $1::timestamptz)      AS nuevos,
                COUNT(*) FILTER (WHERE estado_suscripcion = 'activa') AS suscritos,
                COALESCE(SUM(precio_monthly)
                         FILTER (WHERE estado_suscripcion = 'activa'), 0) AS mrr
            FROM v_gerencia_tenants
        ),
        actividad AS (
            SELECT
                COUNT(DISTINCT tenant_id) AS con_actividad,
                COUNT(*)                  AS mensajes
            FROM messages
            WHERE created_at >= $1::timestamptz AND created_at < $2::timestamptz
        ),
        riesgo AS (
            -- Suscripción activa pero sin un solo mensaje en la ventana.
            SELECT COUNT(*) AS n
            FROM v_gerencia_tenants g
            WHERE g.estado_suscripcion = 'activa'
              AND NOT EXISTS (
                  SELECT 1 FROM messages m
                  WHERE m.tenant_id = g.tenant_id
                    AND m.created_at >= $1::timestamptz
                    AND m.created_at < $2::timestamptz
              )
        ),
        ingresos AS (
            SELECT COALESCE(SUM(monto), 0) AS cobrado
            FROM tenant_transactions
            WHERE estado_pago = 'aprobado'
              AND created_at >= $1::timestamptz
              AND created_at < $2::timestamptz
        )
        SELECT e.*, a.con_actividad, a.mensajes, ri.n AS en_riesgo, i.cobrado
        FROM estados e, actividad a, riesgo ri, ingresos i
        """,
        r.desde,
        r.hasta,
    )

    consumo = await consumo_global(r)

    return ResumenGerenciaOut(
        dias=r.dias,
        tenants_total=fila["total"],
        tenants_activos=fila["activos"],
        tenants_prueba=fila["prueba"],
        tenants_suspendidos=fila["suspendidos"],
        tenants_baja=fila["baja"],
        tenants_nuevos=fila["nuevos"],
        tenants_con_actividad=fila["con_actividad"],
        tenants_en_riesgo=fila["en_riesgo"],
        suscripciones_activas=fila["suscritos"],
        mrr=fila["mrr"],
        mensajes=fila["mensajes"],
        consumo=_consumo_out(consumo),
        ingresos_periodo=fila["cobrado"],
    )


# ============================================================
# Listado de tenants
# ============================================================
@router.get("/tenants", response_model=TenantsGerenciaOut)
async def listar_tenants(
    dias: int = Query(DIAS_POR_DEFECTO, ge=1, le=DIAS_MAXIMO),
    q: str | None = Query(None, description="Nombre del negocio, correo de un usuario o UUID"),
    estado: EstadoTenantPlataforma | None = Query(None),
    orden: str = Query("consumo"),
    limite: int = Query(LIMITE_POR_DEFECTO, ge=1, le=LIMITE_MAXIMO),
    offset: int = Query(0, ge=0),
):
    """
    Todos los negocios de la plataforma, con su estado, su plan y lo que
    consumieron en la ventana.

    El consumo se agrega en un CTE aparte y se une por LEFT JOIN: un tenant
    que no consumió nada tiene que aparecer igual (en cero), porque "alta
    hace tres semanas y nunca mandó un token" es justo lo que hay que ver.
    """
    if orden not in ORDENES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Orden no válido. Opciones: {', '.join(sorted(ORDENES))}",
        )

    r = await rango_dias(dias)
    patron = f"%{q.strip()}%" if q and q.strip() else None

    # El mismo WHERE para la página y para el total, pero las dos consultas
    # numeran sus parámetros distinto (la de la página gasta $1/$2 en el
    # rango de fechas). De ahí que el bloque se arme con los números como
    # argumento en vez de estar escrito dos veces y quedar desincronizado.
    filtros = _filtros(p_estado=3, p_patron=4)
    filtros_count = _filtros(p_estado=1, p_patron=2)

    filas = await fetch_all(
        f"""
        WITH consumo AS (
            SELECT
                tenant_id,
                SUM(tokens_entrada) AS tokens_entrada,
                SUM(tokens_salida)  AS tokens_salida,
                SUM(tokens_total)   AS tokens_total,
                SUM(costo_usd)      AS costo_usd,
                COUNT(*)            AS llamadas
            FROM tenant_token_usage
            WHERE created_at >= $1 AND created_at < $2
            GROUP BY tenant_id
        )
        SELECT
            g.*,
            ({SQL_AGENTE_OPERANDO}) AS agente_operando,
            COALESCE(co.tokens_entrada, 0) AS tokens_entrada,
            COALESCE(co.tokens_salida, 0)  AS tokens_salida,
            COALESCE(co.tokens_total, 0)   AS tokens_total,
            COALESCE(co.costo_usd, 0)      AS costo_usd,
            COALESCE(co.llamadas, 0)       AS llamadas
        FROM v_gerencia_tenants g
        LEFT JOIN consumo        co ON co.tenant_id = g.tenant_id
        LEFT JOIN tenant_credits cr ON cr.tenant_id = g.tenant_id
        {filtros}
        ORDER BY {ORDENES[orden]}
        LIMIT $5 OFFSET $6
        """,
        r.desde,
        r.hasta,
        estado,
        patron,
        limite,
        offset,
    )

    total = await fetch_one(
        f"""
        SELECT COUNT(*) AS n
        FROM v_gerencia_tenants g
        {filtros_count}
        """,
        estado,
        patron,
    )

    return TenantsGerenciaOut(
        total=total["n"],
        dias=r.dias,
        items=[_tenant_out(f) for f in filas],
    )


def _tenant_out(f) -> TenantGerenciaOut:
    return TenantGerenciaOut(
        tenant_id=f["tenant_id"],
        nombre=f["nombre"],
        alta=f["alta"],
        estado=f["estado"],
        estado_motivo=f["estado_motivo"],
        estado_actualizado_en=f["estado_actualizado_en"],
        estado_actualizado_por=f["estado_actualizado_por"],
        agente_ia_activo=f["agente_ia_activo"],
        gestion_vendedores_activo=f["gestion_vendedores_activo"],
        agente_operando=f["agente_operando"],
        plan=f["plan"],
        estado_suscripcion=f["estado_suscripcion"],
        fecha_renovacion=f["fecha_renovacion"],
        precio_monthly=f["precio_monthly"],
        creditos_disponibles=f["creditos_disponibles"],
        creditos_gastados=f["creditos_gastados"],
        usuarios_portal=f["usuarios_portal"],
        vendedores_activos=f["vendedores_activos"],
        canales_activos=f["canales_activos"],
        ultimo_mensaje=f["ultimo_mensaje"],
        consumo=ConsumoTenantOut(
            tokens_entrada=f["tokens_entrada"],
            tokens_salida=f["tokens_salida"],
            tokens_total=f["tokens_total"],
            costo_usd=f["costo_usd"],
            llamadas=f["llamadas"],
        ),
    )


async def _traer_tenant(tenant_id: UUID, dias: int) -> TenantGerenciaOut:
    r = await rango_dias(dias)
    fila = await fetch_one(
        f"""
        WITH consumo AS (
            SELECT
                SUM(tokens_entrada) AS tokens_entrada,
                SUM(tokens_salida)  AS tokens_salida,
                SUM(tokens_total)   AS tokens_total,
                SUM(costo_usd)      AS costo_usd,
                COUNT(*)            AS llamadas
            FROM tenant_token_usage
            WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3
        )
        SELECT
            g.*,
            ({SQL_AGENTE_OPERANDO}) AS agente_operando,
            COALESCE(co.tokens_entrada, 0) AS tokens_entrada,
            COALESCE(co.tokens_salida, 0)  AS tokens_salida,
            COALESCE(co.tokens_total, 0)   AS tokens_total,
            COALESCE(co.costo_usd, 0)      AS costo_usd,
            COALESCE(co.llamadas, 0)       AS llamadas
        FROM v_gerencia_tenants g
        LEFT JOIN tenant_credits cr ON cr.tenant_id = g.tenant_id
        CROSS JOIN consumo co
        WHERE g.tenant_id = $1
        """,
        tenant_id,
        r.desde,
        r.hasta,
    )

    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Negocio no encontrado",
        )

    return _tenant_out(fila)


@router.get("/tenants/{tenant_id}", response_model=TenantGerenciaOut)
async def detalle_tenant(
    tenant_id: UUID,
    dias: int = Query(DIAS_POR_DEFECTO, ge=1, le=DIAS_MAXIMO),
):
    return await _traer_tenant(tenant_id, dias)


@router.get("/tenants/{tenant_id}/transacciones", response_model=list[TransaccionOut])
async def transacciones_tenant(
    tenant_id: UUID,
    limite: int = Query(20, ge=1, le=100),
):
    """
    El historial de cobros de un negocio. Existe aparte de /api/pagos
    porque aquel sale del tenant del JWT, y gerencia no tiene tenant.
    """
    filas = await fetch_all(
        """
        SELECT id, tipo, concepto, monto, estado_pago, metodo_pago,
               ultimos_4_digitos, created_at
        FROM tenant_transactions
        WHERE tenant_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        tenant_id,
        limite,
    )
    return [
        TransaccionOut(
            id=f["id"],
            tipo=f["tipo"],
            concepto=f["concepto"],
            monto=f["monto"],
            estado_pago=f["estado_pago"],
            metodo_pago=f["metodo_pago"],
            ultimos_4_digitos=f["ultimos_4_digitos"],
            creado_en=f["created_at"],
        )
        for f in filas
    ]


# ============================================================
# Acciones sobre un tenant
# ============================================================
@router.patch("/tenants/{tenant_id}/estado", response_model=TenantGerenciaOut)
async def cambiar_estado_tenant(
    tenant_id: UUID,
    datos: CambiarEstadoTenantIn,
    gerente: UsuarioActual = Depends(gerencia_plataforma_actual),
):
    """
    Suspende, reactiva o da de baja a un negocio.

    'suspendido' y 'baja' apagan el agente de verdad: services/
    acceso_pagos.py los lee, y n8n recibe `agente_ia_activo=false` en el
    siguiente mensaje. Por eso el motivo es obligatorio cuando el estado no
    es 'activo' — dentro de tres meses, "¿por qué está apagado este
    negocio?" tiene que tener respuesta sin preguntarle a nadie.
    """
    if datos.estado != "activo" and not (datos.motivo or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Hace falta un motivo para dejar el negocio en este estado",
        )

    async with transaccion() as conn:
        anterior = await conn.fetchrow(
            """
            SELECT COALESCE(ep.estado, 'activo') AS estado
            FROM tenants t
            LEFT JOIN tenant_estado_plataforma ep ON ep.tenant_id = t.id
            WHERE t.id = $1
            """,
            tenant_id,
        )
        if anterior is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Negocio no encontrado",
            )

        await conn.execute(
            """
            INSERT INTO tenant_estado_plataforma
                (tenant_id, estado, motivo, notas, actualizado_por, actualizado_en)
            VALUES ($1, $2, $3, $4, $5, NOW())
            ON CONFLICT (tenant_id) DO UPDATE SET
                estado          = EXCLUDED.estado,
                motivo          = EXCLUDED.motivo,
                notas           = COALESCE(EXCLUDED.notas, tenant_estado_plataforma.notas),
                actualizado_por = EXCLUDED.actualizado_por,
                actualizado_en  = NOW()
            """,
            tenant_id,
            datos.estado,
            datos.motivo,
            datos.notas,
            gerente.email,
        )

        await registrar_auditoria(
            actor_email=gerente.email,
            actor_portal_user_id=gerente.id,
            accion="estado_tenant",
            tenant_id=tenant_id,
            detalle={
                "antes": anterior["estado"],
                "despues": datos.estado,
                "motivo": datos.motivo,
            },
            conn=conn,
        )

    return await _traer_tenant(tenant_id, DIAS_POR_DEFECTO)


@router.patch("/tenants/{tenant_id}/servicios", response_model=TenantGerenciaOut)
async def cambiar_servicios_tenant(
    tenant_id: UUID,
    datos: CambiarServiciosTenantIn,
    gerente: UsuarioActual = Depends(gerencia_plataforma_actual),
):
    """
    Enciende o apaga los módulos de un negocio desde plataforma.

    Es el mismo interruptor que el dueño ve en su portal; la diferencia es
    quién lo toca y que acá queda en la bitácora. Útil para habilitar el
    CRM de campo a un cliente que lo acaba de contratar sin esperar a que
    lo encuentre en su pantalla.
    """
    async with transaccion() as conn:
        anterior = await conn.fetchrow(
            """
            SELECT
                COALESCE(ts.agente_ia_activo, true)           AS agente_ia_activo,
                COALESCE(ts.gestion_vendedores_activo, false) AS gestion_vendedores_activo
            FROM tenants t
            LEFT JOIN tenant_servicios ts ON ts.tenant_id = t.id
            WHERE t.id = $1
            """,
            tenant_id,
        )
        if anterior is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Negocio no encontrado",
            )

        agente = (
            anterior["agente_ia_activo"]
            if datos.agente_ia_activo is None
            else datos.agente_ia_activo
        )
        vendedores = (
            anterior["gestion_vendedores_activo"]
            if datos.gestion_vendedores_activo is None
            else datos.gestion_vendedores_activo
        )

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
            agente,
            vendedores,
        )

        await registrar_auditoria(
            actor_email=gerente.email,
            actor_portal_user_id=gerente.id,
            accion="servicios_tenant",
            tenant_id=tenant_id,
            detalle={
                "antes": {
                    "agente_ia_activo": anterior["agente_ia_activo"],
                    "gestion_vendedores_activo": anterior["gestion_vendedores_activo"],
                },
                "despues": {
                    "agente_ia_activo": agente,
                    "gestion_vendedores_activo": vendedores,
                },
                "motivo": datos.motivo,
            },
            conn=conn,
        )

    return await _traer_tenant(tenant_id, DIAS_POR_DEFECTO)


@router.post("/tenants/{tenant_id}/creditos", response_model=AjusteCreditosOut)
async def ajustar_creditos(
    tenant_id: UUID,
    datos: AjusteCreditosIn,
    gerente: UsuarioActual = Depends(gerencia_plataforma_actual),
):
    """
    Suma o resta créditos a mano (cortesía por una caída, corrección de un
    cobro mal acreditado, saldo de bienvenida).

    Pasa por credit_transactions con tipo 'ajuste' y guarda saldo anterior
    y nuevo, igual que una compra: el libro mayor tiene que cuadrar mire
    quien mire, y un regalo que no deja asiento es un saldo que no se puede
    explicar después.
    """
    async with transaccion() as conn:
        existe = await conn.fetchval("SELECT 1 FROM tenants WHERE id = $1", tenant_id)
        if existe is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Negocio no encontrado",
            )

        # FOR UPDATE: dos ajustes simultáneos sobre el mismo tenant tienen
        # que serializarse, o el segundo escribiría un saldo_anterior que
        # ya no era el vigente.
        saldo_anterior: Decimal = (
            await conn.fetchval(
                "SELECT creditos_disponibles FROM tenant_credits WHERE tenant_id = $1 FOR UPDATE",
                tenant_id,
            )
            or Decimal(0)
        )

        saldo_nuevo = saldo_anterior + datos.cantidad
        if saldo_nuevo < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"El ajuste dejaría el saldo en {saldo_nuevo}. "
                    f"Disponible actual: {saldo_anterior}"
                ),
            )

        await conn.execute(
            """
            INSERT INTO tenant_credits (tenant_id, creditos_disponibles, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (tenant_id) DO UPDATE SET
                creditos_disponibles = EXCLUDED.creditos_disponibles,
                updated_at           = NOW()
            """,
            tenant_id,
            saldo_nuevo,
        )

        await conn.execute(
            """
            INSERT INTO credit_transactions
                (tenant_id, tipo, cantidad, concepto, saldo_anterior, saldo_nuevo)
            VALUES ($1, 'ajuste', $2, $3, $4, $5)
            """,
            tenant_id,
            datos.cantidad,
            f"Ajuste manual de {gerente.email}: {datos.motivo}",
            saldo_anterior,
            saldo_nuevo,
        )

        await registrar_auditoria(
            actor_email=gerente.email,
            actor_portal_user_id=gerente.id,
            accion="ajuste_creditos",
            tenant_id=tenant_id,
            detalle={
                "cantidad": str(datos.cantidad),
                "saldo_anterior": str(saldo_anterior),
                "saldo_nuevo": str(saldo_nuevo),
                "motivo": datos.motivo,
            },
            conn=conn,
        )

    return AjusteCreditosOut(creditos_disponibles=saldo_nuevo)


# ============================================================
# Consumo
# ============================================================
@router.get("/consumo", response_model=ConsumoOut)
async def consumo(
    dias: int = Query(DIAS_POR_DEFECTO, ge=1, le=DIAS_MAXIMO),
    tenant_id: UUID | None = Query(None, description="Sin él, toda la plataforma"),
):
    """Serie diaria y desglose por modelo, de toda la plataforma o de uno."""
    r = await rango_dias(dias)

    total = await consumo_global(r, tenant_id)
    dias_filas = await consumo_por_dia(r, tenant_id)
    modelos = await consumo_por_modelo(r, tenant_id)

    return ConsumoOut(
        desde=r.desde,
        hasta=r.hasta,
        total=_consumo_out(total),
        por_dia=[
            PuntoConsumoOut(
                dia=f["dia"],
                tokens_total=f["tokens_total"] or 0,
                costo_usd=f["costo_usd"] or Decimal(0),
                llamadas=f["llamadas"],
            )
            for f in dias_filas
        ],
        por_modelo=[
            ConsumoModeloOut(
                modelo=f["modelo"],
                tokens_total=f["tokens_total"] or 0,
                costo_usd=f["costo_usd"] or Decimal(0),
                llamadas=f["llamadas"],
            )
            for f in modelos
        ],
    )


# ============================================================
# Bitácora
# ============================================================
@router.get("/auditoria", response_model=list[EntradaAuditoriaOut])
async def auditoria(
    tenant_id: UUID | None = Query(None),
    accion: str | None = Query(None),
    limite: int = Query(50, ge=1, le=200),
):
    """
    Qué tocó gerencia y cuándo. Solo lectura: no hay endpoint para borrar
    ni editar entradas, que es la única forma de que una bitácora sirva.
    """
    filas = await fetch_all(
        """
        SELECT a.id, a.actor_email, a.accion, a.tenant_id,
               t.name AS tenant_nombre, a.detalle, a.created_at
        FROM gerencia_auditoria a
        LEFT JOIN tenants t ON t.id = a.tenant_id
        WHERE ($1::uuid IS NULL OR a.tenant_id = $1)
          AND ($2::text IS NULL OR a.accion = $2)
        ORDER BY a.created_at DESC
        LIMIT $3
        """,
        tenant_id,
        accion,
        limite,
    )

    return [
        EntradaAuditoriaOut(
            id=f["id"],
            actor_email=f["actor_email"],
            accion=f["accion"],
            tenant_id=f["tenant_id"],
            tenant_nombre=f["tenant_nombre"],
            detalle=f["detalle"],
            creado_en=f["created_at"],
        )
        for f in filas
    ]
