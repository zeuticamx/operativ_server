"""
Panel del dueño para configurar las etapas del embudo.

OJO — esto es una capa de configuración, no el motor del embudo real:
    * `client_pipeline.estado` sigue restringido al CHECK de 06_vendedores.sql
    * las transiciones legales del embudo real las sigue validando
      `services/pipeline_estados.py` (hardcodeado, sin BD, a propósito)

Lo que se guarda acá es "cómo le gustaría al dueño que funcionara su
embudo": etapas con nombre/color/descripción/orden libres, y una matriz de
qué movimiento hacia dónde quiere permitir. El único cruce con la realidad
es de solo lectura (`leads_activos`, comparando por nombre contra
`client_pipeline.estado`) para poder avisar antes de borrar una etapa que
coincide con una etapa real en uso. Conectar esta configuración al motor
real del embudo es trabajo aparte.
"""

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status

from deps import UsuarioActual, gerencia_actual, usuario_actual, verificar_acceso_tenant
from routers.vendedores import _exigir_modulo, modulo_en_ruta
from services import pipeline_estados
from services.pipeline_estados import ESTADOS_CERRADOS
from schemas import (
    PipelineConfigOut,
    PipelineEtapaActualizarIn,
    PipelineEtapaCrearIn,
    PipelineEtapaOut,
    PipelineTransicionIn,
    PipelineTransicionOut,
)
from session import execute, fetch_all, fetch_one, fetch_value, transaccion

router = APIRouter(tags=["vendedores"])

_SELECT_ETAPA = """
    SELECT id, tenant_id, nombre, color, descripcion, orden, creado_en
    FROM pipeline_etapas
"""

_SELECT_TRANSICION = """
    SELECT
        t.id, t.tenant_id, t.permitida,
        o.id AS etapa_origen_id, o.nombre AS etapa_origen_nombre,
        d.id AS etapa_destino_id, d.nombre AS etapa_destino_nombre
    FROM pipeline_transiciones t
    JOIN pipeline_etapas o ON o.id = t.etapa_origen_id
    JOIN pipeline_etapas d ON d.id = t.etapa_destino_id
"""

# Color y descripción de cada etapa REAL de hoy (services/pipeline_estados.py),
# para que la primera vez que el dueño abre el panel vea su embudo actual y
# no una lista vacía. Mismos colores que `lib/colores-etapa.ts` en el
# frontend, para que no se vean distintos en dos pantallas.
_DEFAULT_ETAPAS: dict[str, tuple[str, str]] = {
    "nuevo": ("#3987E5", "Recién entró al embudo, todavía sin contactar."),
    "contactado": ("#E2E8F0", "Ya se le habló al menos una vez."),
    "en_seguimiento": ("#E2E8F0", "En seguimiento, sin cotización todavía."),
    "cotizado": ("#F59E0B", "Se le envió una cotización o propuesta."),
    "negociacion": ("#F59E0B", "Negociando precio, términos o condiciones."),
    "ganado": ("#22C55E", "Cerrado como venta."),
    "perdido": ("#EF4444", "Se descartó o no avanzó. Se puede retomar."),
}


def _a_etapa_out(fila, leads_activos: int = 0) -> PipelineEtapaOut:
    return PipelineEtapaOut(**dict(fila), leads_activos=leads_activos)


def _a_transicion_out(fila) -> PipelineTransicionOut:
    return PipelineTransicionOut(**dict(fila))


async def _etapa_o_404(etapa_id: UUID) -> asyncpg.Record:
    fila = await fetch_one(f"{_SELECT_ETAPA} WHERE id = $1", etapa_id)
    if fila is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Etapa no encontrada")
    return fila


async def _leads_activos(tenant_id: UUID, nombre: str) -> int:
    return await fetch_value(
        """
        SELECT COUNT(*) FROM client_pipeline
        WHERE tenant_id = $1 AND estado = $2 AND estado <> ALL($3::text[])
        """,
        tenant_id,
        nombre,
        list(ESTADOS_CERRADOS),
    )


async def _seed_si_vacio(tenant_id: UUID) -> list[asyncpg.Record]:
    """
    Si el tenant todavía no configuró ninguna etapa, se le arma un punto de
    partida con las etapas y transiciones del embudo real de hoy — así el
    panel arranca mostrando lo que ya existe en vez de una pantalla vacía.
    """
    etapas = await fetch_all(f"{_SELECT_ETAPA} WHERE tenant_id = $1 ORDER BY orden", tenant_id)
    if etapas:
        return etapas

    async with transaccion() as conn:
        nuevas = []
        for orden, nombre in enumerate(pipeline_estados.ESTADOS):
            color, descripcion = _DEFAULT_ETAPAS.get(nombre, ("#E2E8F0", None))
            fila = await conn.fetchrow(
                """
                INSERT INTO pipeline_etapas (tenant_id, nombre, color, descripcion, orden)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (tenant_id, nombre) DO NOTHING
                RETURNING id, tenant_id, nombre, color, descripcion, orden, creado_en
                """,
                tenant_id,
                nombre,
                color,
                descripcion,
                orden,
            )
            if fila:
                nuevas.append(fila)

        # Transiciones por defecto = las que hoy son legales en el embudo
        # real (services/pipeline_estados.TRANSICIONES_VALIDAS).
        por_nombre = {f["nombre"]: f["id"] for f in nuevas}
        filas_transicion = [
            (tenant_id, por_nombre[origen], por_nombre[destino], True)
            for origen, destinos in pipeline_estados.TRANSICIONES_VALIDAS.items()
            for destino in destinos
            if origen in por_nombre and destino in por_nombre
        ]
        if filas_transicion:
            await conn.executemany(
                """
                INSERT INTO pipeline_transiciones (tenant_id, etapa_origen_id, etapa_destino_id, permitida)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (tenant_id, etapa_origen_id, etapa_destino_id) DO NOTHING
                """,
                filas_transicion,
            )

    return nuevas


# ============================================================
# LEER CONFIGURACIÓN COMPLETA
# ============================================================
@router.get("/tenants/{tenant_id}/pipeline-config", response_model=PipelineConfigOut)
async def leer_pipeline_config(tenant_id: UUID = Depends(modulo_en_ruta)):
    etapas = await _seed_si_vacio(tenant_id)

    conteos_filas = await fetch_all(
        """
        SELECT estado, COUNT(*) AS total
        FROM client_pipeline
        WHERE tenant_id = $1 AND estado <> ALL($2::text[])
        GROUP BY estado
        """,
        tenant_id,
        list(ESTADOS_CERRADOS),
    )
    conteos = {f["estado"]: f["total"] for f in conteos_filas}

    transiciones = await fetch_all(
        f"{_SELECT_TRANSICION} WHERE t.tenant_id = $1 ORDER BY o.orden, d.orden",
        tenant_id,
    )

    return PipelineConfigOut(
        etapas=[_a_etapa_out(e, conteos.get(e["nombre"], 0)) for e in etapas],
        transiciones=[_a_transicion_out(t) for t in transiciones],
    )


# ============================================================
# ETAPAS
# ============================================================
@router.post("/pipeline-etapas", response_model=PipelineEtapaOut, status_code=201)
async def crear_etapa(
    datos: PipelineEtapaCrearIn,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    tenant_id = verificar_acceso_tenant(usuario, datos.tenant_id)
    await _exigir_modulo(tenant_id)

    async with transaccion() as conn:
        existentes = await conn.fetch(
            "SELECT id FROM pipeline_etapas WHERE tenant_id = $1",
            tenant_id,
        )
        try:
            fila = await conn.fetchrow(
                """
                INSERT INTO pipeline_etapas (tenant_id, nombre, color, descripcion, orden)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, tenant_id, nombre, color, descripcion, orden, creado_en
                """,
                tenant_id,
                datos.nombre,
                datos.color,
                datos.descripcion,
                datos.orden,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Ya existe una etapa llamada «{datos.nombre}» en este negocio",
            )

        # Por defecto todas las transiciones son válidas: la etapa nueva
        # queda conectada en ambos sentidos con cada etapa que ya existía.
        if existentes:
            nueva_id = fila["id"]
            pares = [(tenant_id, nueva_id, e["id"], True) for e in existentes] + [
                (tenant_id, e["id"], nueva_id, True) for e in existentes
            ]
            await conn.executemany(
                """
                INSERT INTO pipeline_transiciones (tenant_id, etapa_origen_id, etapa_destino_id, permitida)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (tenant_id, etapa_origen_id, etapa_destino_id) DO NOTHING
                """,
                pares,
            )

    # Casi siempre 0 para una etapa recién creada, pero si el nombre elegido
    # coincide con una etapa real que ya tiene leads (p.ej. el dueño crea una
    # etapa llamada igual que una del embudo de hoy), esto lo refleja.
    leads_activos = await _leads_activos(tenant_id, fila["nombre"])
    return _a_etapa_out(fila, leads_activos)


@router.patch("/pipeline-etapas/{etapa_id}", response_model=PipelineEtapaOut)
async def actualizar_etapa(
    etapa_id: UUID,
    datos: PipelineEtapaActualizarIn,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    etapa = await _etapa_o_404(etapa_id)
    tenant_id = verificar_acceso_tenant(usuario, etapa["tenant_id"])

    try:
        fila = await fetch_one(
            """
            UPDATE pipeline_etapas SET
                nombre      = COALESCE($3, nombre),
                color       = COALESCE($4, color),
                descripcion = COALESCE($5, descripcion),
                orden       = COALESCE($6, orden)
            WHERE id = $1 AND tenant_id = $2
            RETURNING id, tenant_id, nombre, color, descripcion, orden, creado_en
            """,
            etapa_id,
            tenant_id,
            datos.nombre,
            datos.color,
            datos.descripcion,
            datos.orden,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya existe una etapa llamada «{datos.nombre}» en este negocio",
        )

    leads_activos = await _leads_activos(tenant_id, fila["nombre"])
    return _a_etapa_out(fila, leads_activos)


@router.delete("/pipeline-etapas/{etapa_id}", status_code=204)
async def eliminar_etapa(
    etapa_id: UUID,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    etapa = await _etapa_o_404(etapa_id)
    tenant_id = verificar_acceso_tenant(usuario, etapa["tenant_id"])

    # No es la etapa real del embudo (ver docstring del módulo), pero si el
    # nombre coincide con una etapa que sí tiene leads activos hoy, borrarla
    # sin avisar sería confuso: el dueño perdería de vista esa etapa en su
    # panel mientras el embudo real la sigue usando.
    activos = await _leads_activos(tenant_id, etapa["nombre"])
    if activos:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"«{etapa['nombre']}» tiene {activos} lead(s) activo(s) en el embudo real. "
                "No se puede eliminar."
            ),
        )

    await execute("DELETE FROM pipeline_etapas WHERE id = $1", etapa_id)


# ============================================================
# TRANSICIONES
# ============================================================
@router.get("/pipeline-transiciones", response_model=list[PipelineTransicionOut])
async def listar_transiciones(
    tenant_id: UUID,
    usuario: UsuarioActual = Depends(usuario_actual),
):
    verificar_acceso_tenant(usuario, tenant_id)
    filas = await fetch_all(
        f"{_SELECT_TRANSICION} WHERE t.tenant_id = $1 ORDER BY o.orden, d.orden",
        tenant_id,
    )
    return [_a_transicion_out(f) for f in filas]


@router.post("/pipeline-transiciones", response_model=PipelineTransicionOut)
async def guardar_transicion(
    datos: PipelineTransicionIn,
    usuario: UsuarioActual = Depends(gerencia_actual),
):
    """Crea o actualiza (toggle) si un movimiento de una etapa a otra está permitido."""
    tenant_id = verificar_acceso_tenant(usuario, datos.tenant_id)

    if datos.etapa_origen_id == datos.etapa_destino_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Una etapa no puede transicionar hacia sí misma",
        )

    # Las dos etapas tienen que ser de este tenant: sin esto se podría armar
    # una transición que cruza el embudo de otro negocio.
    ids_validos = await fetch_value(
        "SELECT COUNT(*) FROM pipeline_etapas WHERE tenant_id = $1 AND id = ANY($2::uuid[])",
        tenant_id,
        [datos.etapa_origen_id, datos.etapa_destino_id],
    )
    if ids_validos != 2:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Alguna de las etapas no existe en este negocio",
        )

    fila = await fetch_one(
        """
        INSERT INTO pipeline_transiciones (tenant_id, etapa_origen_id, etapa_destino_id, permitida)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (tenant_id, etapa_origen_id, etapa_destino_id)
            DO UPDATE SET permitida = EXCLUDED.permitida
        RETURNING id
        """,
        tenant_id,
        datos.etapa_origen_id,
        datos.etapa_destino_id,
        datos.permitida,
    )
    transicion = await fetch_one(f"{_SELECT_TRANSICION} WHERE t.id = $1", fila["id"])
    return _a_transicion_out(transicion)
