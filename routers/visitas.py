"""
Check-ins con geocerca y sincronización de la cola offline.

El veredicto de la geocerca se calcula en el servidor con las coordenadas
guardadas del cliente (`geo.evaluar_geocerca`); nunca se acepta un
`dentro_de_geocerca` que venga del teléfono. La distancia y el veredicto
se persisten: si después mueven el pin del cliente o cambian el radio, las
visitas ya registradas conservan el resultado que tuvieron.
"""

from datetime import datetime
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from services import geo
from services.crm import AccesoCRM, acceso_crm, cargar_cliente
from deps import VendedorActual, vendedor_actual
from schemas import (
    CheckinIn,
    CheckinOut,
    CheckinSyncIn,
    SyncIn,
    SyncOut,
    SyncResultado,
    VisitaOut,
)
from session import conexion, fetch_all, transaccion

router = APIRouter(prefix="/visitas", tags=["crm"])

SELECT_VISITA = """
    SELECT
        vi.id, vi.tenant_id, vi.vendedor_id, vi.cliente_id,
        vi.latitud, vi.longitud, vi.accuracy_metros,
        vi.distancia_calculada_metros, vi.dentro_de_geocerca,
        vi.foto_url, vi.comentario, vi.timestamp_dispositivo,
        vi.timestamp_servidor, vi.cliente_uuid_offline, vi.creado_en,
        ve.nombre AS vendedor_nombre,
        c.nombre_negocio AS cliente_nombre_negocio
    FROM visitas vi
    JOIN vendedores ve ON ve.id = vi.vendedor_id
    JOIN clientes  c  ON c.id  = vi.cliente_id
"""


def _mensaje(resultado: geo.ResultadoGeocerca, negocio: str) -> str:
    if resultado.dentro:
        return f"Visita validada en {negocio} ({resultado.distancia_metros:.0f} m del punto)."
    return (
        f"Visita registrada FUERA de la geocerca de {negocio}: "
        f"{resultado.distancia_metros:.0f} m del punto, "
        f"{resultado.exceso_metros:.0f} m más que la tolerancia de "
        f"{resultado.radio_metros} m."
    )


# ============================================================
# Escritura (el punto que se sustituye en los tests)
# ============================================================
async def insertar_visita(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    vendedor_id: UUID,
    cliente_id: UUID,
    datos: CheckinIn,
    resultado: geo.ResultadoGeocerca,
) -> tuple[asyncpg.Record, bool]:
    """
    Guarda la visita. Devuelve (fila, creada).

    `creada=False` significa que ese `cliente_uuid_offline` ya se había
    procesado: se devuelve la visita que ya estaba, sin tocar nada. Es lo
    que hace idempotente a /sync — la app puede reenviar su cola completa
    después de un corte sin miedo a duplicar.

    El `WHERE cliente_uuid_offline IS NOT NULL` del ON CONFLICT no es
    decorativo: el índice único es parcial y Postgres necesita el mismo
    predicado para reconocerlo como árbitro del conflicto.
    """
    fila = await conn.fetchrow(
        """
        INSERT INTO visitas (
            tenant_id, vendedor_id, cliente_id, latitud, longitud,
            accuracy_metros, distancia_calculada_metros, dentro_de_geocerca,
            foto_url, comentario, timestamp_dispositivo, cliente_uuid_offline
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        ON CONFLICT (cliente_uuid_offline) WHERE cliente_uuid_offline IS NOT NULL
        DO NOTHING
        RETURNING id
        """,
        tenant_id,
        vendedor_id,
        cliente_id,
        datos.latitud,
        datos.longitud,
        datos.accuracy_metros,
        resultado.distancia_metros,
        resultado.dentro,
        datos.foto_url,
        datos.comentario,
        datos.timestamp_dispositivo,
        datos.cliente_uuid_offline,
    )

    if fila is not None:
        completa = await conn.fetchrow(f"{SELECT_VISITA} WHERE vi.id = $1", fila["id"])
        return completa, True

    # Hubo conflicto: la visita ya existía. Se recupera acotando por tenant
    # para no devolver la de otra cuenta si dos apps generaran el mismo
    # UUID (improbable, pero el índice único es global a la tabla).
    existente = await conn.fetchrow(
        f"{SELECT_VISITA} WHERE vi.cliente_uuid_offline = $1 AND vi.tenant_id = $2",
        datos.cliente_uuid_offline,
        tenant_id,
    )
    return existente, False


async def registrar_checkin(
    conn: asyncpg.Connection,
    vendedor: VendedorActual,
    cliente: asyncpg.Record,
    datos: CheckinIn,
) -> tuple[asyncpg.Record, bool, geo.ResultadoGeocerca]:
    """Evalúa la geocerca y persiste. Devuelve (fila, creada, resultado)."""
    resultado = geo.evaluar_geocerca(
        lat_cliente=cliente["latitud"],
        lon_cliente=cliente["longitud"],
        lat_visita=datos.latitud,
        lon_visita=datos.longitud,
        radio_metros=cliente["radio_tolerancia_metros"],
    )
    fila, creada = await insertar_visita(
        conn, vendedor.tenant_id, vendedor.id, cliente["id"], datos, resultado
    )
    return fila, creada, resultado


# ============================================================
# POST /visitas/checkin
# ============================================================
@router.post("/checkin", response_model=CheckinOut, status_code=201)
async def checkin(
    datos: CheckinIn,
    vendedor: VendedorActual = Depends(vendedor_actual),
):
    """
    Registra una visita en el momento.

    Un check-in fuera de la geocerca **se guarda igual y responde 201**.
    No es un error del cliente: es un hecho que gerencia necesita ver en
    el reporte. Lo que cambia es `dentro_de_geocerca`, y el mensaje dice
    cuánto se pasó.

    El vendedor y el tenant salen del JWT. El cliente tiene que ser de su
    cartera: uno de otro vendedor da 403, uno de otro negocio da 404.
    """
    acceso = AccesoCRM(
        tenant_id=vendedor.tenant_id,
        vendedor_id=vendedor.id,
        es_gerencia=False,
        etiqueta=vendedor.nombre,
    )

    async with transaccion() as conn:
        cliente = await cargar_cliente(datos.cliente_id, acceso, conn)
        fila, creada, resultado = await registrar_checkin(conn, vendedor, cliente, datos)

    return CheckinOut(
        visita=VisitaOut(**dict(fila)),
        dentro_de_geocerca=resultado.dentro,
        distancia_metros=round(resultado.distancia_metros, 1),
        radio_metros=resultado.radio_metros,
        exceso_metros=round(resultado.exceso_metros, 1),
        mensaje=_mensaje(resultado, cliente["nombre_negocio"]),
        duplicada=not creada,
    )


# ============================================================
# POST /visitas/sync
# ============================================================
def dedupe_lote(
    visitas: list[CheckinSyncIn],
) -> tuple[list[CheckinSyncIn], list[CheckinSyncIn]]:
    """
    Separa el lote en (a procesar, repetidos dentro del propio lote).

    Se conserva la PRIMERA aparición de cada `cliente_uuid_offline`. Una
    cola local puede traer el mismo elemento dos veces si la app reintentó
    y guardó de más; sin esto, el segundo chocaría contra el índice único
    en medio de la transacción.
    """
    vistos: set[UUID] = set()
    unicas: list[CheckinSyncIn] = []
    repetidas: list[CheckinSyncIn] = []

    for v in visitas:
        if v.cliente_uuid_offline in vistos:
            repetidas.append(v)
        else:
            vistos.add(v.cliente_uuid_offline)
            unicas.append(v)

    return unicas, repetidas


@router.post("/sync", response_model=SyncOut)
async def sync(
    datos: SyncIn,
    vendedor: VendedorActual = Depends(vendedor_actual),
):
    """
    Sube la cola de check-ins que la app acumuló sin señal.

    Idempotente por `cliente_uuid_offline`: reenviar el mismo lote no crea
    visitas nuevas, devuelve las que ya estaban marcadas como
    `duplicada`. La app puede reintentar sin llevar cuenta de qué llegó.

    Responde 200 aunque haya elementos rechazados. El resultado va por
    elemento para que la app sepa cuáles borrar de su cola y cuáles
    conservar; un 4xx global la obligaría a descartar el lote entero por
    un solo check-in malo.

    Cada elemento va en su propia transacción: uno que falle no puede
    tumbar los demás, que es justo lo que se necesita en una cola que se
    sube después de dos días sin cobertura.
    """
    acceso = AccesoCRM(
        tenant_id=vendedor.tenant_id,
        vendedor_id=vendedor.id,
        es_gerencia=False,
        etiqueta=vendedor.nombre,
    )

    unicas, repetidas = dedupe_lote(datos.visitas)
    resultados: list[SyncResultado] = []
    creadas = duplicadas = rechazadas = 0

    for v in unicas:
        try:
            async with transaccion() as conn:
                cliente = await cargar_cliente(v.cliente_id, acceso, conn)
                fila, creada, resultado = await registrar_checkin(
                    conn, vendedor, cliente, v
                )
        except Exception as e:  # noqa: BLE001 — se reporta por elemento
            rechazadas += 1
            resultados.append(
                SyncResultado(
                    cliente_uuid_offline=v.cliente_uuid_offline,
                    aceptada=False,
                    error=_detalle_error(e),
                )
            )
            continue

        if creada:
            creadas += 1
        else:
            duplicadas += 1

        resultados.append(
            SyncResultado(
                cliente_uuid_offline=v.cliente_uuid_offline,
                visita_id=fila["id"],
                aceptada=True,
                duplicada=not creada,
                dentro_de_geocerca=resultado.dentro,
                distancia_metros=round(resultado.distancia_metros, 1),
            )
        )

    # Los repetidos dentro del propio lote se reportan como duplicados y
    # apuntan a la visita que sí se procesó, para que la app los borre.
    por_uuid = {r.cliente_uuid_offline: r for r in resultados}
    for v in repetidas:
        original = por_uuid.get(v.cliente_uuid_offline)
        duplicadas += 1
        acepto = bool(original and original.aceptada)
        resultados.append(
            SyncResultado(
                cliente_uuid_offline=v.cliente_uuid_offline,
                visita_id=original.visita_id if original else None,
                aceptada=acepto,
                duplicada=True,
                dentro_de_geocerca=original.dentro_de_geocerca if original else None,
                distancia_metros=original.distancia_metros if original else None,
                error=None if acepto else "Repetido dentro del mismo lote",
            )
        )

    return SyncOut(
        recibidas=len(datos.visitas),
        creadas=creadas,
        duplicadas=duplicadas,
        rechazadas=rechazadas,
        resultados=resultados,
    )


def _detalle_error(e: Exception) -> str:
    """Mensaje legible para el resultado por elemento del lote."""
    detail = getattr(e, "detail", None)
    if isinstance(detail, str):
        return detail
    if detail is not None:
        return str(detail)
    return "No se pudo registrar la visita"


# ============================================================
# GET /visitas
# ============================================================
@router.get("", response_model=list[VisitaOut])
async def historial(
    acceso: AccesoCRM = Depends(acceso_crm),
    cliente_id: UUID | None = Query(None),
    vendedor_id: UUID | None = Query(None, description="Solo gerencia; un vendedor ve las suyas"),
    desde: datetime | None = Query(None),
    hasta: datetime | None = Query(None, description="Exclusivo"),
    solo_fuera_geocerca: bool = Query(False),
    limite: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """
    Historial de visitas.

    Un vendedor solo ve las suyas: el filtro se fuerza a su id y se ignora
    el `vendedor_id` del query.
    """
    filtro_vendedor = acceso.vendedor_id if acceso.es_vendedor else vendedor_id

    filas = await fetch_all(
        f"""
        {SELECT_VISITA}
        WHERE vi.tenant_id = $1
          AND ($2::uuid IS NULL OR vi.vendedor_id = $2)
          AND ($3::uuid IS NULL OR vi.cliente_id = $3)
          AND ($4::timestamptz IS NULL OR vi.timestamp_servidor >= $4)
          AND ($5::timestamptz IS NULL OR vi.timestamp_servidor <  $5)
          AND (NOT $6::boolean OR vi.dentro_de_geocerca = false)
        ORDER BY vi.timestamp_servidor DESC
        LIMIT $7 OFFSET $8
        """,
        acceso.tenant_id,
        filtro_vendedor,
        cliente_id,
        desde,
        hasta,
        solo_fuera_geocerca,
        limite,
        offset,
    )
    return [VisitaOut(**dict(f)) for f in filas]


@router.get("/{visita_id}", response_model=VisitaOut)
async def detalle(
    visita_id: UUID,
    acceso: AccesoCRM = Depends(acceso_crm),
):
    filtro_vendedor = acceso.vendedor_id if acceso.es_vendedor else None
    async with conexion() as conn:
        fila = await conn.fetchrow(
            f"""
            {SELECT_VISITA}
            WHERE vi.id = $1
              AND vi.tenant_id = $2
              AND ($3::uuid IS NULL OR vi.vendedor_id = $3)
            """,
            visita_id,
            acceso.tenant_id,
            filtro_vendedor,
        )

    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Visita no encontrada",
        )
    return VisitaOut(**dict(fila))
