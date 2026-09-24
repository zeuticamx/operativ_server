"""
Módulo de gestión de vendedores (mini-CRM).

Opcional por tenant: todo lo de acá exige `gestion_vendedores_activo`. Un
tenant que solo usa el agente de IA nunca toca estos endpoints, y uno que
solo usa vendedores no necesita tener el agente encendido.

Se apoya en tres piezas separadas a propósito:
    pipeline_estados.py  qué movimientos del embudo son legales
    asignacion.py        a quién le toca el próximo lead
    pipeline.py          leer y escribir el embudo
"""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from realtime import broadcast_alerta
from services import pipeline_estados
from services.acceso_pagos import acceso_pagos
from services.asignacion import asignar_vendedor_automatico, leer_config
from deps import (
    UsuarioActual,
    gerencia_actual,
    tenant_actual,
    tenant_en_ruta,
    usuario_actual,
    verificar_acceso_tenant,
)
from services.pipeline import (
    asignar_vendedor,
    get_or_create_pipeline,
    get_tenant_servicios,
    set_tenant_servicios,
)
from services.pipeline_estados import ESTADOS_CERRADOS
from schemas import (
    AsignarClienteIn,
    CambiarEstadoIn,
    ConfigAsignacionIn,
    ConfigAsignacionOut,
    ConversionEtapaOut,
    ConversionPipelineOut,
    CrearClienteIn,
    HistorialOut,
    HistorialVendedorOut,
    MetricaEtapaOut,
    MetricasPipelineOut,
    PipelineOut,
    RankingVendedorOut,
    ReasignacionOut,
    ServiciosIn,
    ServiciosOut,
    TendenciaMesOut,
    TendenciaPipelineOut,
    VendedorActualizarIn,
    VendedorCrearIn,
    VendedorOut,
)
from session import conexion, fetch_all, fetch_one, transaccion

router_vendedores = APIRouter(prefix="/vendedores", tags=["vendedores"])
router_tenants = APIRouter(prefix="/tenants", tags=["vendedores"])

# /pipeline y no /clientes: el CRM de campo (clientes.py) ya usa
# /api/clientes para los negocios físicos que se visitan, y ahí el
# identificador es un `clientes.id`. Acá el identificador es un `users.id`
# —un contacto que escribió por WhatsApp o Instagram— y son cosas
# distintas. Compartir el sustantivo hacía que pasar el UUID equivocado
# diera un 404 sin ninguna pista de por qué.
router_pipeline = APIRouter(prefix="/pipeline", tags=["vendedores"])

# Crear nuevos clientes en el embudo (ruta separada para claridad).
router_clientes = APIRouter(prefix="/clientes", tags=["vendedores"])


# ============================================================
# Helpers
# ============================================================
# Mismo tratamiento del texto "null" que en conversaciones.py: n8n interpola
# undefined como esa cadena literal al insertar en `users`.
_NOMBRE_CLIENTE = "NULLIF(NULLIF(TRIM(u.display_name), ''), 'null')"
_HANDLE_CLIENTE = (
    "COALESCE("
    "NULLIF(NULLIF(TRIM(u.whatsapp_id), ''), 'null'),"
    "NULLIF(NULLIF(TRIM(u.instagram_id), ''), 'null'),"
    "NULLIF(NULLIF(TRIM(u.facebook_id), ''), 'null'))"
)

_SELECT_PIPELINE = f"""
    SELECT
        p.id, p.user_id,
        {_NOMBRE_CLIENTE} AS cliente_nombre,
        {_HANDLE_CLIENTE} AS cliente_handle,
        p.vendedor_id,
        v.nombre AS vendedor_nombre,
        p.estado, p.monto_estimado, p.motivo_perdida, p.actualizado_en
    FROM client_pipeline p
    JOIN users u ON u.id = p.user_id
    LEFT JOIN vendedores v ON v.id = p.vendedor_id
"""


def _a_pipeline_out(fila) -> PipelineOut:
    return PipelineOut(
        **dict(fila),
        transiciones_posibles=pipeline_estados.transiciones_desde(fila["estado"]),
    )


async def _exigir_modulo(tenant_id: UUID) -> UUID:
    """
    409 si el negocio no tiene el módulo encendido; 402 si lo tiene
    encendido pero no puede pagarlo.

    Se comprueban las dos cosas en cada endpoint y no una sola vez al
    entrar: el flag se puede apagar (o la suscripción vencer) mientras
    alguien tiene el panel abierto.

    Ojo con el orden: el 409 va primero porque es la decisión del propio
    dueño (el módulo está apagado a propósito) y no depende de pagos; el
    402 es "está prendido, pero no se puede seguir usando gratis".
    """
    servicios = await get_tenant_servicios(tenant_id)
    if not servicios.gestion_vendedores_activo:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "El módulo de gestión de vendedores no está activo para este "
                "negocio. Actívalo en /tenants/{tenant_id}/servicios."
            ),
        )

    acceso = await acceso_pagos(tenant_id)
    if not acceso.permitido:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                "La suscripción venció y no quedan créditos disponibles. "
                "Renueva el plan o compra créditos en /pagos/crear-pago para "
                "seguir usando el CRM de vendedores."
            ),
        )

    return tenant_id


async def modulo_en_ruta(tenant_id: UUID = Depends(tenant_en_ruta)) -> UUID:
    return await _exigir_modulo(tenant_id)


async def modulo_actual(tenant_id: UUID = Depends(tenant_actual)) -> UUID:
    return await _exigir_modulo(tenant_id)


async def _vendedor_del_tenant(vendedor_id: UUID, tenant_id: UUID):
    """
    El filtro por tenant va en el WHERE y no como chequeo aparte: así el
    UUID de un vendedor ajeno da 404 en vez de dejar leerlo.
    """
    fila = await fetch_one(
        """
        SELECT id, tenant_id, portal_user_id, nombre, telefono, activo, creado_en
        FROM vendedores
        WHERE id = $1 AND tenant_id = $2
        """,
        vendedor_id,
        tenant_id,
    )
    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendedor no encontrado",
        )
    return fila


# ============================================================
# SERVICIOS DEL TENANT
# ============================================================
# Estos dos NO exigen el módulo activo: son justamente los que lo encienden.
@router_tenants.get("/{tenant_id}/servicios", response_model=ServiciosOut)
async def leer_servicios(tenant_id: UUID = Depends(tenant_en_ruta)):
    servicios = await get_tenant_servicios(tenant_id)
    return ServiciosOut(**vars(servicios))


@router_tenants.patch("/{tenant_id}/servicios", response_model=ServiciosOut)
async def actualizar_servicios(
    datos: ServiciosIn,
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    verificar_acceso_tenant(usuario, tenant_id)
    servicios = await set_tenant_servicios(
        tenant_id,
        datos.agente_ia_activo,
        datos.gestion_vendedores_activo,
        datos.calendario_activo,
        datos.zona_horaria,
    )
    return ServiciosOut(**vars(servicios))


# ============================================================
# CONFIG DE ASIGNACIÓN
# ============================================================
@router_tenants.get("/{tenant_id}/config-vendedores", response_model=ConfigAsignacionOut)
async def leer_config_asignacion(tenant_id: UUID = Depends(modulo_en_ruta)):
    async with conexion() as conn:
        config = await leer_config(conn, tenant_id)
    return ConfigAsignacionOut(
        tenant_id=tenant_id,
        estrategia_asignacion=config.estrategia,
        ultimo_vendedor_asignado_id=config.ultimo_vendedor_asignado_id,
    )


@router_tenants.patch("/{tenant_id}/config-vendedores", response_model=ConfigAsignacionOut)
async def actualizar_config_asignacion(
    datos: ConfigAsignacionIn,
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    verificar_acceso_tenant(usuario, tenant_id)
    await _exigir_modulo(tenant_id)

    fila = await fetch_one(
        """
        INSERT INTO tenant_vendedor_config (tenant_id, estrategia_asignacion)
        VALUES ($1, $2)
        ON CONFLICT (tenant_id) DO UPDATE SET
            estrategia_asignacion = EXCLUDED.estrategia_asignacion
        RETURNING tenant_id, estrategia_asignacion, ultimo_vendedor_asignado_id
        """,
        tenant_id,
        datos.estrategia_asignacion,
    )
    return ConfigAsignacionOut(**dict(fila))


# ============================================================
# VENDEDORES
# ============================================================
@router_vendedores.post("", response_model=VendedorOut, status_code=201)
async def crear_vendedor(
    datos: VendedorCrearIn,
    usuario: UsuarioActual = Depends(usuario_actual),
):
    # El tenant sale del token salvo que venga explícito en el body, que es
    # el caso de un superadmin dando de alta en nombre de otro negocio.
    if datos.tenant_id is not None:
        tenant_id = verificar_acceso_tenant(usuario, datos.tenant_id)
    elif usuario.tenant_id is not None:
        tenant_id = usuario.tenant_id
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El usuario no tiene un negocio asociado todavía",
        )

    await _exigir_modulo(tenant_id)

    # Un portal_user de otro negocio no puede quedar detrás de este vendedor:
    # le daría acceso a la cartera de un tenant que no es el suyo.
    if datos.portal_user_id is not None:
        duenio = await fetch_one(
            "SELECT tenant_id FROM portal_users WHERE id = $1",
            datos.portal_user_id,
        )
        if duenio is None or duenio["tenant_id"] != tenant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Ese usuario de portal no pertenece a este negocio",
            )

    fila = await fetch_one(
        """
        INSERT INTO vendedores (tenant_id, portal_user_id, nombre, telefono)
        VALUES ($1, $2, $3, $4)
        RETURNING id, tenant_id, portal_user_id, nombre, telefono, activo, creado_en
        """,
        tenant_id,
        datos.portal_user_id,
        datos.nombre,
        datos.telefono,
    )
    return VendedorOut(**dict(fila), clientes_activos=0)


@router_tenants.get("/{tenant_id}/vendedores", response_model=list[VendedorOut])
async def listar_vendedores(
    # Sin exigir el módulo: leer el equipo es inofensivo y lo necesitan los
    # DOS módulos que cuelgan de `vendedores` — el embudo de chat (que sí
    # depende del flag) y el CRM de campo (que no). Gatearlo dejaba la
    # pantalla de cartera sin poder llenar su filtro de vendedor.
    tenant_id: UUID = Depends(tenant_en_ruta),
    activo: bool | None = Query(None),
):
    filas = await fetch_all(
        """
        SELECT
            v.id, v.tenant_id, v.portal_user_id, v.nombre, v.telefono,
            v.activo, v.creado_en,
            (SELECT COUNT(*)
               FROM client_pipeline p
              WHERE p.vendedor_id = v.id
                AND p.estado <> ALL($3::text[])) AS clientes_activos
        FROM vendedores v
        WHERE v.tenant_id = $1
          AND ($2::boolean IS NULL OR v.activo = $2)
        ORDER BY v.activo DESC, v.creado_en
        """,
        tenant_id,
        activo,
        list(ESTADOS_CERRADOS),
    )
    return [VendedorOut(**dict(f)) for f in filas]


@router_vendedores.patch("/{vendedor_id}", response_model=VendedorOut)
async def actualizar_vendedor(
    vendedor_id: UUID,
    datos: VendedorActualizarIn,
    tenant_id: UUID = Depends(modulo_actual),
):
    """
    Activa, desactiva o corrige los datos de un vendedor.

    Desactivar NO reparte su cartera: sus leads siguen siendo suyos hasta
    que alguien llame a /vendedores/{id}/reasignar-pendientes. Es a
    propósito — mover leads de dueño es visible para el equipo y no algo
    que deba pasar de callado por tocar un switch. Lo que sí cambia de
    inmediato es que deja de recibir leads nuevos.
    """
    await _vendedor_del_tenant(vendedor_id, tenant_id)

    fila = await fetch_one(
        """
        UPDATE vendedores SET
            nombre   = COALESCE($3, nombre),
            telefono = COALESCE($4, telefono),
            activo   = COALESCE($5, activo)
        WHERE id = $1 AND tenant_id = $2
        RETURNING id, tenant_id, portal_user_id, nombre, telefono, activo, creado_en,
                  (SELECT COUNT(*)
                     FROM client_pipeline p
                    WHERE p.vendedor_id = vendedores.id
                      AND p.estado <> ALL($6::text[])) AS clientes_activos
        """,
        vendedor_id,
        tenant_id,
        datos.nombre,
        datos.telefono,
        datos.activo,
        list(ESTADOS_CERRADOS),
    )
    return VendedorOut(**dict(fila))


@router_vendedores.get("/{vendedor_id}/pipeline", response_model=list[PipelineOut])
async def pipeline_de_vendedor(
    vendedor_id: UUID,
    tenant_id: UUID = Depends(modulo_actual),
    estado: str | None = Query(None),
    incluir_cerrados: bool = Query(True),
):
    await _vendedor_del_tenant(vendedor_id, tenant_id)

    filas = await fetch_all(
        f"""
        {_SELECT_PIPELINE}
        WHERE p.tenant_id = $1
          AND p.vendedor_id = $2
          AND ($3::text IS NULL OR p.estado = $3)
          AND ($4::boolean OR p.estado <> ALL($5::text[]))
        ORDER BY p.actualizado_en DESC
        """,
        tenant_id,
        vendedor_id,
        estado,
        incluir_cerrados,
        list(ESTADOS_CERRADOS),
    )
    return [_a_pipeline_out(f) for f in filas]


@router_vendedores.get("/{vendedor_id}/historial", response_model=list[HistorialVendedorOut])
async def historial_de_vendedor(
    vendedor_id: UUID,
    tenant_id: UUID = Depends(modulo_actual),
    limite: int = Query(50, ge=1, le=200),
):
    """
    Bitácora completa del vendedor: cada cambio de estado o asignación que
    le tocó, sin importar el cliente. Usa `pipeline_historial.vendedor_id`
    y no el `vendedor_id` actual de `client_pipeline`, así que un lead que
    ya se reasignó a otra persona sigue apareciendo con lo que pasó
    mientras fue de este vendedor.
    """
    await _vendedor_del_tenant(vendedor_id, tenant_id)

    filas = await fetch_all(
        f"""
        SELECT
            p.user_id,
            {_NOMBRE_CLIENTE} AS cliente_nombre,
            {_HANDLE_CLIENTE} AS cliente_handle,
            h.estado_anterior, h.estado_nuevo, h.nota, h.creado_en
        FROM pipeline_historial h
        JOIN client_pipeline p ON p.id = h.client_pipeline_id
        JOIN users u ON u.id = p.user_id
        WHERE p.tenant_id = $1 AND h.vendedor_id = $2
        ORDER BY h.creado_en DESC
        LIMIT $3
        """,
        tenant_id,
        vendedor_id,
        limite,
    )
    return [HistorialVendedorOut(**dict(f)) for f in filas]


@router_vendedores.post("/{vendedor_id}/reasignar-pendientes", response_model=ReasignacionOut)
async def reasignar_pendientes(
    vendedor_id: UUID,
    tenant_id: UUID = Depends(modulo_actual),
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    """
    Reparte los leads abiertos de un vendedor entre los demás activos.

    Explícito y separado del PATCH de desactivación: quien desactiva decide
    después qué hacer con la cartera, y puede desactivar sin mover nada.

    Solo se mueven los estados no cerrados. Un 'ganado' o un 'perdido' son
    historia del vendedor que los cerró y cambiarles el dueño falsearía el
    ranking.

    Si no queda ningún otro vendedor activo, los leads se quedan donde
    están y se informa en `sin_destino` — antes huérfanos que perdidos.
    """
    await _vendedor_del_tenant(vendedor_id, tenant_id)

    reasignados = 0
    sin_destino = 0
    destinos: dict[UUID, int] = {}
    movimientos: list[dict] = []

    async with transaccion() as conn:
        config = await leer_config(conn, tenant_id)

        # FOR UPDATE OF p y no FOR UPDATE a secas: el join a users es solo
        # para el nombre de la alerta, no hace falta bloquear esas filas
        # también. Nadie puede cambiarle el estado a estos leads mientras
        # se los reparte, o uno podría cerrarse y terminar reasignado igual.
        pendientes = await conn.fetch(
            f"""
            SELECT p.id, p.user_id, {_NOMBRE_CLIENTE} AS cliente_nombre
            FROM client_pipeline p
            JOIN users u ON u.id = p.user_id
            WHERE p.tenant_id = $1
              AND p.vendedor_id = $2
              AND p.estado <> ALL($3::text[])
            ORDER BY p.actualizado_en
            FOR UPDATE OF p
            """,
            tenant_id,
            vendedor_id,
            list(ESTADOS_CERRADOS),
        )

        for fila in pendientes:
            # Uno por uno y no todos al mismo destino: cada llamada vuelve a
            # mirar la carga (o mueve el puntero del round-robin), así los
            # leads se reparten en vez de amontonarse en el primero.
            destino = await asignar_vendedor_automatico(
                tenant_id,
                conn=conn,
                excluir_id=vendedor_id,
            )

            if destino is None:
                sin_destino += 1
                continue

            await asignar_vendedor(
                fila["id"],
                destino,
                conn,
                nota=f"Reasignación de pendientes ({usuario.email})",
            )
            reasignados += 1
            destinos[destino] = destinos.get(destino, 0) + 1
            movimientos.append(
                {
                    "cliente_id": fila["user_id"],
                    "cliente_nombre": fila["cliente_nombre"],
                    "vendedor_nuevo_id": destino,
                }
            )

    # Fuera de la transacción, igual que en crear_cliente/cambiar_estado.
    # Una alerta por cliente y no un resumen: es lo que se pidió, pero ojo
    # si algún día esto reparte carteras de cientos de leads — eso serían
    # cientos de toasts encadenados en el panel de gerencia.
    if movimientos:
        nombres_destino = {
            f["id"]: f["nombre"]
            for f in await fetch_all(
                "SELECT id, nombre FROM vendedores WHERE id = ANY($1::uuid[])",
                list({m["vendedor_nuevo_id"] for m in movimientos}),
            )
        }
        for m in movimientos:
            nombre_destino = nombres_destino.get(m["vendedor_nuevo_id"], "otro vendedor")
            await broadcast_alerta(
                tenant_id,
                tipo="cambio_etapa",
                titulo="🔄 Cliente reasignado",
                mensaje=f"{m['cliente_nombre'] or 'Un cliente'} fue reasignado a {nombre_destino}",
                datos={
                    "cliente_id": str(m["cliente_id"]),
                    "vendedor_anterior_id": str(vendedor_id),
                    "vendedor_nuevo_id": str(m["vendedor_nuevo_id"]),
                    "vendedor_nuevo_nombre": nombre_destino,
                },
            )

    return ReasignacionOut(
        vendedor_id=vendedor_id,
        estrategia=config.estrategia,
        reasignados=reasignados,
        sin_destino=sin_destino,
        destinos=destinos,
    )


# ============================================================
# CLIENTES EN EL EMBUDO
# ============================================================
@router_pipeline.post("/{user_id}/asignar", response_model=PipelineOut)
async def asignar_cliente(
    user_id: UUID,
    datos: AsignarClienteIn,
    tenant_id: UUID = Depends(modulo_actual),
    usuario: UsuarioActual = Depends(usuario_actual),
):
    """
    Pone (o cambia) el vendedor de un cliente.

    Reasignar sobrescribe: el UNIQUE (tenant_id, user_id) garantiza una sola
    fila de embudo por cliente, así que esto nunca duplica. Si el cliente
    todavía no estaba en el embudo, se crea en 'nuevo'.

    Sin `vendedor_id` decide la estrategia del tenant; con estrategia
    'manual' eso devuelve None y el lead queda sin dueño, que es lo
    correcto: 'manual' significa que lo elige una persona.
    """
    # El cliente tiene que ser de este negocio. Sin esto se podría meter en
    # el embudo propio el user_id de otro tenant.
    cliente = await fetch_one(
        "SELECT id FROM users WHERE id = $1 AND tenant_id = $2",
        user_id,
        tenant_id,
    )
    if cliente is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cliente no encontrado",
        )

    if datos.vendedor_id is not None:
        vendedor = await _vendedor_del_tenant(datos.vendedor_id, tenant_id)
        if not vendedor["activo"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Ese vendedor está desactivado",
            )

    async with transaccion() as conn:
        embudo = await get_or_create_pipeline(tenant_id, user_id, conn)

        destino = datos.vendedor_id
        if destino is None:
            destino = await asignar_vendedor_automatico(tenant_id, conn=conn)

        nota = datos.nota or f"Asignación manual ({usuario.email})"
        await asignar_vendedor(embudo.id, destino, conn, nota=nota)

        fila = await conn.fetchrow(
            f"{_SELECT_PIPELINE} WHERE p.id = $1",
            embudo.id,
        )

    return _a_pipeline_out(fila)


@router_pipeline.patch("/{user_id}/estado", response_model=PipelineOut)
async def cambiar_estado(
    user_id: UUID,
    datos: CambiarEstadoIn,
    tenant_id: UUID = Depends(modulo_actual),
    usuario: UsuarioActual = Depends(usuario_actual),
):
    """
    Mueve un lead de etapa, validando la transición contra la máquina de
    estados. El UPDATE y la anotación en la bitácora van en una sola
    transacción: no puede quedar un estado cambiado sin su registro.
    """
    if not pipeline_estados.es_estado_valido(datos.estado):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "mensaje": f"'{datos.estado}' no es un estado del embudo",
                "estados_validos": list(pipeline_estados.ESTADOS),
            },
        )

    async with transaccion() as conn:
        # FOR UPDATE antes de decidir: leer el estado y escribir el nuevo
        # tienen que ser un solo paso, o dos cambios simultáneos podrían
        # validarse los dos contra el mismo estado viejo y dejar el embudo
        # en uno al que no se llegaba desde ahí.
        actual = await conn.fetchrow(
            """
            SELECT id, estado FROM client_pipeline
            WHERE tenant_id = $1 AND user_id = $2
            FOR UPDATE
            """,
            tenant_id,
            user_id,
        )

        if actual is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Este cliente no está en el embudo",
            )

        estado_anterior = actual["estado"]

        if not pipeline_estados.validar_transicion(estado_anterior, datos.estado):
            posibles = pipeline_estados.transiciones_desde(estado_anterior)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "mensaje": (
                        f"No se puede pasar de '{estado_anterior}' a '{datos.estado}'"
                    ),
                    "estado_actual": estado_anterior,
                    "transiciones_permitidas": posibles,
                    "es_terminal": pipeline_estados.es_terminal(estado_anterior),
                },
            )

        # El motivo solo vive mientras el lead está perdido: si se reabre,
        # dejarlo pegado haría que la próxima pérdida mostrara el motivo
        # viejo hasta que alguien lo pise.
        motivo = datos.motivo_perdida if datos.estado == "perdido" else None

        fila = await conn.fetchrow(
            """
            UPDATE client_pipeline SET
                estado         = $3,
                monto_estimado = COALESCE($4, monto_estimado),
                motivo_perdida = $5,
                actualizado_en = NOW()
            WHERE id = $1 AND tenant_id = $2
            RETURNING id, vendedor_id
            """,
            actual["id"],
            tenant_id,
            datos.estado,
            datos.monto_estimado,
            motivo,
        )

        await conn.execute(
            """
            INSERT INTO pipeline_historial
                (client_pipeline_id, estado_anterior, estado_nuevo, vendedor_id, nota)
            VALUES ($1, $2, $3, $4, $5)
            """,
            actual["id"],
            estado_anterior,
            datos.estado,
            fila["vendedor_id"],
            datos.nota or f"Cambio de estado ({usuario.email})",
        )

        completa = await conn.fetchrow(
            f"{_SELECT_PIPELINE} WHERE p.id = $1",
            actual["id"],
        )

    resultado = _a_pipeline_out(completa)

    # "cierre" y no "cambio_etapa" al llegar a un estado terminal: son tipos
    # distintos en schemas.TipoAlerta y el centro de notificaciones ya les
    # da un color propio (ver TIPO_CLASES en centro-notificaciones.tsx).
    tipo_alerta = "cierre" if pipeline_estados.es_terminal(datos.estado) else "cambio_etapa"
    emoji = "🏆" if datos.estado == "ganado" else "📍"

    await broadcast_alerta(
        tenant_id,
        tipo=tipo_alerta,
        titulo=f"{emoji} Cliente movido",
        mensaje=f"{resultado.cliente_nombre} pasó de {estado_anterior} a {datos.estado}",
        datos={
            "cliente_id": str(user_id),
            "cliente_nombre": resultado.cliente_nombre,
            "estado_anterior": estado_anterior,
            "estado_nuevo": datos.estado,
            "vendedor_id": str(resultado.vendedor_id) if resultado.vendedor_id else None,
            "vendedor_nombre": resultado.vendedor_nombre,
            "nota": datos.nota,
        },
    )

    return resultado


@router_pipeline.get("/{user_id}/historial", response_model=list[HistorialOut])
async def historial_cliente(
    user_id: UUID,
    tenant_id: UUID = Depends(modulo_actual),
):
    filas = await fetch_all(
        """
        SELECT h.estado_anterior, h.estado_nuevo, h.vendedor_id,
               v.nombre AS vendedor_nombre, h.nota, h.creado_en
        FROM pipeline_historial h
        JOIN client_pipeline p ON p.id = h.client_pipeline_id
        LEFT JOIN vendedores v ON v.id = h.vendedor_id
        WHERE p.tenant_id = $1 AND p.user_id = $2
        ORDER BY h.creado_en
        """,
        tenant_id,
        user_id,
    )
    return [HistorialOut(**dict(f)) for f in filas]


# ============================================================
# VISTA DE GERENCIA
# ============================================================
@router_tenants.get("/{tenant_id}/pipeline", response_model=list[PipelineOut])
async def pipeline_completo(
    tenant_id: UUID = Depends(modulo_en_ruta),
    estado: str | None = Query(None),
    vendedor_id: UUID | None = Query(None),
    sin_vendedor: bool = Query(False, description="Solo leads sin vendedor asignado"),
    limite: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    filas = await fetch_all(
        f"""
        {_SELECT_PIPELINE}
        WHERE p.tenant_id = $1
          AND ($2::text IS NULL OR p.estado = $2)
          AND ($3::uuid IS NULL OR p.vendedor_id = $3)
          AND (NOT $4::boolean OR p.vendedor_id IS NULL)
        ORDER BY p.actualizado_en DESC
        LIMIT $5 OFFSET $6
        """,
        tenant_id,
        estado,
        vendedor_id,
        sin_vendedor,
        limite,
        offset,
    )
    return [_a_pipeline_out(f) for f in filas]


@router_tenants.get("/{tenant_id}/metricas", response_model=MetricasPipelineOut)
async def metricas_pipeline(
    tenant_id: UUID = Depends(modulo_en_ruta),
    desde: datetime | None = Query(
        None, description="Inclusivo. Filtra por fecha de alta en el embudo, no por actualizado_en"
    ),
    hasta: datetime | None = Query(None, description="Exclusivo. Ver `desde`"),
):
    """
    Agregados del embudo: cuántos hay en cada etapa, cuánto tarda cada una
    y quién cierra más.

    Sin `desde`/`hasta` es la foto de siempre: todo el pipeline del tenant.
    Con rango, cuenta solo los leads dados de alta en esa ventana — la
    fecha de alta es el primer renglón de su bitácora (estado_anterior IS
    NULL, ver get_or_create_pipeline), no `actualizado_en`: esa cambia con
    cualquier movimiento y no serviría para "qué entró en este período".
    """
    cerrados = list(ESTADOS_CERRADOS)

    por_estado = await fetch_all(
        """
        SELECT p.estado, COUNT(*) AS total
        FROM client_pipeline p
        JOIN pipeline_historial h
          ON h.client_pipeline_id = p.id AND h.estado_anterior IS NULL
        WHERE p.tenant_id = $1
          AND ($2::timestamptz IS NULL OR h.creado_en >= $2)
          AND ($3::timestamptz IS NULL OR h.creado_en <  $3)
        GROUP BY p.estado
        """,
        tenant_id,
        desde,
        hasta,
    )
    conteos = {f["estado"]: f["total"] for f in por_estado}
    total = sum(conteos.values())

    # Tiempo por etapa: cuánto pasa entre que un lead entra a una etapa y
    # sale hacia la siguiente. Se leen solo los cambios de estado reales
    # (estado_anterior <> estado_nuevo); las anotaciones de asignación
    # comparten tabla pero no mueven al lead de columna, y contarlas
    # partiría cada etapa en tramos falsos. Restringido a los mismos leads
    # del rango que `por_estado`, para que ambas cuenten la misma cohorte.
    tiempos = await fetch_all(
        """
        WITH leads_rango AS (
            SELECT h.client_pipeline_id
            FROM pipeline_historial h
            JOIN client_pipeline p ON p.id = h.client_pipeline_id
            WHERE p.tenant_id = $1
              AND h.estado_anterior IS NULL
              AND ($2::timestamptz IS NULL OR h.creado_en >= $2)
              AND ($3::timestamptz IS NULL OR h.creado_en <  $3)
        ),
        cambios AS (
            SELECT h.client_pipeline_id, h.estado_nuevo AS estado, h.creado_en
            FROM pipeline_historial h
            JOIN leads_rango lr ON lr.client_pipeline_id = h.client_pipeline_id
            WHERE h.estado_anterior IS DISTINCT FROM h.estado_nuevo
        ),
        tramos AS (
            SELECT
                estado,
                LEAD(creado_en) OVER (
                    PARTITION BY client_pipeline_id ORDER BY creado_en
                ) - creado_en AS duracion
            FROM cambios
        )
        SELECT estado, AVG(EXTRACT(EPOCH FROM duracion)) / 3600.0 AS horas
        FROM tramos
        WHERE duracion IS NOT NULL
        GROUP BY estado
        """,
        tenant_id,
        desde,
        hasta,
    )
    horas = {f["estado"]: float(f["horas"]) for f in tiempos if f["horas"] is not None}

    etapas = [
        MetricaEtapaOut(
            estado=estado,
            total=conteos.get(estado, 0),
            porcentaje=round(100.0 * conteos.get(estado, 0) / total, 2) if total else 0.0,
            horas_promedio=round(horas[estado], 2) if estado in horas else None,
        )
        # Se recorre la máquina de estados y no lo que devolvió el GROUP BY,
        # para que las etapas vacías salgan en 0 y en el orden del embudo en
        # vez de desaparecer del panel.
        for estado in pipeline_estados.ESTADOS
    ]

    ganados = conteos.get("ganado", 0)
    perdidos = conteos.get("perdido", 0)
    cerrados_total = ganados + perdidos

    filas_ranking = await fetch_all(
        """
        WITH leads_rango AS (
            SELECT h.client_pipeline_id
            FROM pipeline_historial h
            JOIN client_pipeline p ON p.id = h.client_pipeline_id
            WHERE p.tenant_id = $1
              AND h.estado_anterior IS NULL
              AND ($3::timestamptz IS NULL OR h.creado_en >= $3)
              AND ($4::timestamptz IS NULL OR h.creado_en <  $4)
        )
        SELECT
            v.id AS vendedor_id, v.nombre, v.activo,
            COUNT(p.id) FILTER (WHERE p.estado <> ALL($2::text[])) AS abiertos,
            COUNT(p.id) FILTER (WHERE p.estado = 'ganado')  AS ganados,
            COUNT(p.id) FILTER (WHERE p.estado = 'perdido') AS perdidos,
            COALESCE(SUM(p.monto_estimado) FILTER (WHERE p.estado = 'ganado'), 0)
                AS monto_ganado
        FROM vendedores v
        LEFT JOIN client_pipeline p
               ON p.vendedor_id = v.id
              AND p.id IN (SELECT client_pipeline_id FROM leads_rango)
        WHERE v.tenant_id = $1
        GROUP BY v.id, v.nombre, v.activo
        ORDER BY ganados DESC, monto_ganado DESC, v.nombre
        """,
        tenant_id,
        cerrados,
        desde,
        hasta,
    )

    ranking = []
    for f in filas_ranking:
        cierres = f["ganados"] + f["perdidos"]
        ranking.append(
            RankingVendedorOut(
                vendedor_id=f["vendedor_id"],
                nombre=f["nombre"],
                activo=f["activo"],
                abiertos=f["abiertos"],
                ganados=f["ganados"],
                perdidos=f["perdidos"],
                monto_ganado=Decimal(f["monto_ganado"]),
                tasa_cierre=round(100.0 * f["ganados"] / cierres, 2) if cierres else None,
            )
        )

    return MetricasPipelineOut(
        total_clientes=total,
        etapas=etapas,
        tasa_conversion_global=(
            round(100.0 * ganados / cerrados_total, 2) if cerrados_total else None
        ),
        ranking=ranking,
    )


def _restar_meses(fecha: datetime, meses: int) -> datetime:
    """Primer día del mes que queda `meses` atrás de `fecha`, a medianoche."""
    indice = fecha.month - 1 - meses
    anio = fecha.year + indice // 12
    mes = indice % 12 + 1
    return fecha.replace(
        year=anio, month=mes, day=1, hour=0, minute=0, second=0, microsecond=0
    )


@router_tenants.get("/{tenant_id}/metricas/tendencia", response_model=TendenciaPipelineOut)
async def tendencia_pipeline(
    tenant_id: UUID = Depends(modulo_en_ruta),
    meses: int = Query(12, ge=1, le=24, description="Cuántos meses hacia atrás, incluido el actual"),
):
    """
    Serie mensual: leads dados de alta, ganados y perdidos por mes.

    "Dado de alta" y "ganado/perdido" salen los dos de `pipeline_historial`
    (alta = primer renglón; cierre = transición a ese estado), agrupados por
    mes en Python-side boundaries para no depender de la zona horaria del
    servidor de Postgres.
    """
    ahora = datetime.now(timezone.utc)
    inicio = _restar_meses(ahora, meses - 1)

    altas = await fetch_all(
        """
        SELECT date_trunc('month', h.creado_en) AS periodo,
               COUNT(DISTINCT h.client_pipeline_id) AS nuevos
        FROM pipeline_historial h
        JOIN client_pipeline p ON p.id = h.client_pipeline_id
        WHERE p.tenant_id = $1
          AND h.estado_anterior IS NULL
          AND h.creado_en >= $2
        GROUP BY 1
        """,
        tenant_id,
        inicio,
    )
    cierres = await fetch_all(
        """
        SELECT date_trunc('month', h.creado_en) AS periodo,
               COUNT(*) FILTER (WHERE h.estado_nuevo = 'ganado')  AS ganados,
               COUNT(*) FILTER (WHERE h.estado_nuevo = 'perdido') AS perdidos,
               COALESCE(SUM(p.monto_estimado) FILTER (WHERE h.estado_nuevo = 'ganado'), 0)
                   AS monto_ganado
        FROM pipeline_historial h
        JOIN client_pipeline p ON p.id = h.client_pipeline_id
        WHERE p.tenant_id = $1
          AND h.estado_anterior IS DISTINCT FROM h.estado_nuevo
          AND h.estado_nuevo IN ('ganado', 'perdido')
          AND h.creado_en >= $2
        GROUP BY 1
        """,
        tenant_id,
        inicio,
    )

    altas_por_mes = {f["periodo"]: f["nuevos"] for f in altas}
    cierres_por_mes = {f["periodo"]: f for f in cierres}

    resultado: list[TendenciaMesOut] = []
    for i in range(meses):
        mes_inicio = _restar_meses(ahora, meses - 1 - i)
        c = cierres_por_mes.get(mes_inicio)
        resultado.append(
            TendenciaMesOut(
                periodo=mes_inicio.strftime("%Y-%m"),
                nuevos=altas_por_mes.get(mes_inicio, 0),
                ganados=c["ganados"] if c else 0,
                perdidos=c["perdidos"] if c else 0,
                monto_ganado=Decimal(c["monto_ganado"]) if c else Decimal(0),
            )
        )

    return TendenciaPipelineOut(meses=resultado)


# Mismo recorte que exportar-embudo-pdf.tsx: 'perdido' es una salida del
# embudo, no una etapa secuencial más, así que no forma parte del waterfall.
_ETAPAS_WATERFALL = tuple(e for e in pipeline_estados.ESTADOS if e != "perdido")


@router_tenants.get("/{tenant_id}/metricas/conversion", response_model=ConversionPipelineOut)
async def conversion_pipeline(
    tenant_id: UUID = Depends(modulo_en_ruta),
    desde: datetime | None = Query(None, description="Inclusivo. Filtra por fecha de alta en el embudo"),
    hasta: datetime | None = Query(None, description="Exclusivo. Ver `desde`"),
):
    """
    Waterfall de conversión: de los leads dados de alta en el rango,
    cuántos llegaron a pisar cada etapa alguna vez (no cuántos están ahí
    ahora — eso ya lo da /metricas). 'nuevo' es siempre el 100%: todo lead
    pasa por ahí al entrar.
    """
    filas = await fetch_all(
        """
        WITH leads_rango AS (
            SELECT h.client_pipeline_id
            FROM pipeline_historial h
            JOIN client_pipeline p ON p.id = h.client_pipeline_id
            WHERE p.tenant_id = $1
              AND h.estado_anterior IS NULL
              AND ($2::timestamptz IS NULL OR h.creado_en >= $2)
              AND ($3::timestamptz IS NULL OR h.creado_en <  $3)
        )
        SELECT h.estado_nuevo AS estado, COUNT(DISTINCT h.client_pipeline_id) AS total
        FROM pipeline_historial h
        JOIN leads_rango lr ON lr.client_pipeline_id = h.client_pipeline_id
        WHERE h.estado_anterior IS DISTINCT FROM h.estado_nuevo
        GROUP BY h.estado_nuevo
        """,
        tenant_id,
        desde,
        hasta,
    )
    alcanzados = {f["estado"]: f["total"] for f in filas}
    total_leads = alcanzados.get("nuevo", 0)

    etapas = [
        ConversionEtapaOut(
            estado=estado,
            alcanzados=alcanzados.get(estado, 0),
            porcentaje=(
                round(100.0 * alcanzados.get(estado, 0) / total_leads, 2)
                if total_leads
                else 0.0
            ),
        )
        for estado in _ETAPAS_WATERFALL
    ]

    return ConversionPipelineOut(
        desde=desde,
        hasta=hasta,
        total_leads=total_leads,
        etapas=etapas,
    )


# ============================================================
# CREAR CLIENTE MANUALMENTE EN EL EMBUDO
# ============================================================
@router_clientes.post("", response_model=PipelineOut, status_code=201)
async def crear_cliente(
    datos: CrearClienteIn,
    usuario: UsuarioActual = Depends(usuario_actual),
):
    """
    Crea un nuevo cliente en el embudo con asignación automática según
    la estrategia del tenant. Genera un user_id, lo mete en users, y
    luego lo asigna al embudo.
    """
    tenant_id = datos.tenant_id
    verificar_acceso_tenant(usuario, tenant_id)
    await _exigir_modulo(tenant_id)

    async with transaccion() as conn:
        # Crear el usuario (contacto)
        user_id = None
        fila_usuario = await conn.fetchrow(
            """
            INSERT INTO users (tenant_id, display_name)
            VALUES ($1, $2)
            RETURNING id
            """,
            tenant_id,
            datos.nombre,
        )
        if fila_usuario:
            user_id = fila_usuario["id"]

        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="No se pudo crear el usuario en la base de datos",
            )

        # Crear el pipeline (embudo) en estado 'nuevo'
        pipeline_fila = await conn.fetchrow(
            """
            INSERT INTO client_pipeline (tenant_id, user_id, estado, monto_estimado)
            VALUES ($1, $2, 'nuevo', $3)
            RETURNING id
            """,
            tenant_id,
            user_id,
            datos.monto_estimado,
        )

        if pipeline_fila is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="No se pudo crear el cliente en el embudo",
            )

        pipeline_id = pipeline_fila["id"]

        # Primer punto de la línea de tiempo, igual que en
        # get_or_create_pipeline (el otro camino que crea filas de
        # client_pipeline). Sin esto el lead queda sin fecha de alta: no
        # cuenta en /metricas/tendencia ni en ningún filtro de rango, y la
        # asignación de abajo dejaría como único rastro un renglón con
        # estado_anterior = estado_nuevo, que es la convención de
        # "cambio de dueño", no la de "entró al embudo".
        await conn.execute(
            """
            INSERT INTO pipeline_historial
                (client_pipeline_id, estado_anterior, estado_nuevo, nota)
            VALUES ($1, NULL, 'nuevo', 'Alta en el embudo')
            """,
            pipeline_id,
        )

        # Asignar vendedor automáticamente según la estrategia
        vendedor_id = await asignar_vendedor_automatico(tenant_id, conn=conn)
        nota = f"Creación manual por {usuario.email}"
        if datos.nota:
            nota += f" — {datos.nota}"

        await asignar_vendedor(pipeline_id, vendedor_id, conn, nota=nota)

        # Seleccionar el resultado final
        fila = await conn.fetchrow(
            f"{_SELECT_PIPELINE} WHERE p.id = $1",
            pipeline_id,
        )

    resultado = _a_pipeline_out(fila)

    # Fuera de la transacción: crear_alerta hace su propia escritura y
    # sio.emit es una llamada de red, ninguna de las dos debería demorar un
    # commit (mismo criterio que eventos.on_mensaje_entrante).
    await broadcast_alerta(
        tenant_id,
        tipo="nuevo_lead",
        titulo="🎉 Nuevo cliente",
        mensaje=(
            f"{resultado.cliente_nombre} llegó al embudo — asignado a {resultado.vendedor_nombre}"
            if resultado.vendedor_nombre
            else f"{resultado.cliente_nombre} llegó al embudo, sin vendedor disponible"
        ),
        datos={
            "cliente_id": str(user_id),
            "cliente_nombre": resultado.cliente_nombre,
            "vendedor_id": str(vendedor_id) if vendedor_id else None,
            "vendedor_nombre": resultado.vendedor_nombre,
            "monto": str(resultado.monto_estimado) if resultado.monto_estimado is not None else None,
            "canal": datos.canal,
        },
    )

    return resultado
