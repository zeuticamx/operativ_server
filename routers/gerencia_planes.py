"""
Catálogo de planes (tabla `planes`), administrado desde el panel de
plataforma.

Es la misma tabla que lee `GET /api/pagos/catalogo` para pintar la
pantalla de suscripción del portal — aquella filtra `WHERE activo` porque
es lo que un tenant ve al contratar; acá gerencia necesita ver y tocar
también los planes apagados, así que no hay ese filtro.

Separado de routers/gerencia.py por tamaño, mismo nivel de acceso
(gerencia_users) y misma convención de bitácora: crear o actualizar un
plan deja asiento en gerencia_auditoria dentro de la misma transacción.

`nombre` es el identificador de la URL, no `id`: es UNIQUE en la tabla
(igual que en 11_mercado_pago.sql) y es lo único que el resto del sistema
usa para referenciar un plan — `tenant_subscriptions.plan` lo guarda como
texto plano, no como FK. Por eso no se puede renombrar un plan desde acá
(ver el docstring de PlanActualizarIn): haría huérfanas las suscripciones
que ya lo referencian sin que nada avise.
"""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status

from deps import UsuarioActual, gerencia_plataforma_actual
from schemas import PlanActualizarIn, PlanCrearIn, PlanGerenciaOut
from services.gerencia import registrar_auditoria
from session import fetch_all, transaccion

router = APIRouter(
    prefix="/gerencia/planes",
    tags=["gerencia"],
    dependencies=[Depends(gerencia_plataforma_actual)],
)

# alias a los nombres de columna que espera PlanGerenciaOut.
_COLUMNAS = """
    nombre, descripcion, precio_monthly, precio_annual,
    max_vendedores, max_leads_mensuales, creditos_incluidos_mensual,
    agente_ia_activo, gestion_vendedores_activo, activo, orden,
    created_at AS creado_en, updated_at AS actualizado_en
"""


def _out(f) -> PlanGerenciaOut:
    return PlanGerenciaOut(**dict(f))


@router.get("", response_model=list[PlanGerenciaOut])
async def listar_planes():
    """
    Todos los planes, activos e inactivos — a diferencia de
    /api/pagos/catalogo, que solo trae los que un tenant puede contratar
    hoy. Gerencia necesita ver los apagados también, para poder
    reactivarlos o confirmar que de verdad están fuera.
    """
    filas = await fetch_all(f"SELECT {_COLUMNAS} FROM planes ORDER BY orden, precio_monthly")
    return [_out(f) for f in filas]


@router.post("", response_model=PlanGerenciaOut, status_code=status.HTTP_201_CREATED)
async def crear_plan(
    datos: PlanCrearIn,
    gerente: UsuarioActual = Depends(gerencia_plataforma_actual),
):
    async with transaccion() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO planes
                    (nombre, descripcion, precio_monthly, precio_annual,
                     max_vendedores, max_leads_mensuales, creditos_incluidos_mensual,
                     agente_ia_activo, gestion_vendedores_activo, activo, orden)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                datos.nombre,
                datos.descripcion,
                datos.precio_monthly,
                datos.precio_annual,
                datos.max_vendedores,
                datos.max_leads_mensuales,
                datos.creditos_incluidos_mensual,
                datos.agente_ia_activo,
                datos.gestion_vendedores_activo,
                datos.activo,
                datos.orden,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Ya existe un plan con ese nombre",
            )

        await registrar_auditoria(
            actor_email=gerente.email,
            actor_portal_user_id=gerente.id,
            accion="plan_creado",
            tenant_id=None,
            detalle={
                "nombre": datos.nombre,
                "precio_monthly": str(datos.precio_monthly),
                "activo": datos.activo,
            },
            conn=conn,
        )

        fila = await conn.fetchrow(f"SELECT {_COLUMNAS} FROM planes WHERE nombre = $1", datos.nombre)

    return _out(fila)


@router.patch("/{nombre}", response_model=PlanGerenciaOut)
async def actualizar_plan(
    nombre: str,
    datos: PlanActualizarIn,
    gerente: UsuarioActual = Depends(gerencia_plataforma_actual),
):
    """
    Apagar un plan (`activo=false`) no toca a los negocios que ya lo
    tienen contratado: `tenant_subscriptions` no lo referencia por FK, así
    que una suscripción vigente sigue vigente. Solo deja de ofrecerse a
    quien contrate desde cero (`/api/pagos/catalogo` filtra por `activo`).
    """
    async with transaccion() as conn:
        anterior = await conn.fetchrow(f"SELECT {_COLUMNAS} FROM planes WHERE nombre = $1", nombre)
        if anterior is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Plan no encontrado",
            )

        # None = no tocar ese campo (ver el docstring de PlanActualizarIn).
        nuevo = {
            "descripcion": anterior["descripcion"] if datos.descripcion is None else datos.descripcion,
            "precio_monthly": (
                anterior["precio_monthly"] if datos.precio_monthly is None else datos.precio_monthly
            ),
            "precio_annual": (
                anterior["precio_annual"] if datos.precio_annual is None else datos.precio_annual
            ),
            "max_vendedores": (
                anterior["max_vendedores"] if datos.max_vendedores is None else datos.max_vendedores
            ),
            "max_leads_mensuales": (
                anterior["max_leads_mensuales"]
                if datos.max_leads_mensuales is None
                else datos.max_leads_mensuales
            ),
            "creditos_incluidos_mensual": (
                anterior["creditos_incluidos_mensual"]
                if datos.creditos_incluidos_mensual is None
                else datos.creditos_incluidos_mensual
            ),
            "agente_ia_activo": (
                anterior["agente_ia_activo"]
                if datos.agente_ia_activo is None
                else datos.agente_ia_activo
            ),
            "gestion_vendedores_activo": (
                anterior["gestion_vendedores_activo"]
                if datos.gestion_vendedores_activo is None
                else datos.gestion_vendedores_activo
            ),
            "activo": anterior["activo"] if datos.activo is None else datos.activo,
            "orden": anterior["orden"] if datos.orden is None else datos.orden,
        }

        await conn.execute(
            """
            UPDATE planes SET
                descripcion = $2,
                precio_monthly = $3,
                precio_annual = $4,
                max_vendedores = $5,
                max_leads_mensuales = $6,
                creditos_incluidos_mensual = $7,
                agente_ia_activo = $8,
                gestion_vendedores_activo = $9,
                activo = $10,
                orden = $11,
                updated_at = NOW()
            WHERE nombre = $1
            """,
            nombre,
            nuevo["descripcion"],
            nuevo["precio_monthly"],
            nuevo["precio_annual"],
            nuevo["max_vendedores"],
            nuevo["max_leads_mensuales"],
            nuevo["creditos_incluidos_mensual"],
            nuevo["agente_ia_activo"],
            nuevo["gestion_vendedores_activo"],
            nuevo["activo"],
            nuevo["orden"],
        )

        cambios = datos.model_dump(exclude_none=True)
        await registrar_auditoria(
            actor_email=gerente.email,
            actor_portal_user_id=gerente.id,
            accion="plan_actualizado",
            tenant_id=None,
            detalle={
                "nombre": nombre,
                # Decimal no es serializable a JSON tal cual: se pasa a
                # string, mismo criterio que el resto de las auditorías de
                # este módulo (ver AjusteCreditosIn en routers/gerencia.py).
                "cambios": {
                    k: (str(v) if hasattr(v, "quantize") else v) for k, v in cambios.items()
                },
            },
            conn=conn,
        )

        fila = await conn.fetchrow(f"SELECT {_COLUMNAS} FROM planes WHERE nombre = $1", nombre)

    return _out(fila)
