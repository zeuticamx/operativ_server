"""
Límites del agente por plan: qué modelos puede elegir el negocio y cuántos
mensajes de historial (`tenant_agent_config.history_window`) puede darle.

Los topes viven en la tabla `planes` (45_limites_agente_plan.sql), editables
desde /gerencia/planes:

    planes.modelos_permitidos  NULL = sin límite; el primero es el modelo por
                               defecto (al que se recorta).
    planes.historial_maximo    NULL = sin límite.

Dos capas, a propósito:

    1. Acá (y PUT /api/agente): rechaza con 402 lo que el dueño intenta
       guardar fuera del plan. Es lo que ve el usuario.
    2. Triggers de 45_limites_agente_plan.sql: recortan la fila cuando
       cambia el plan del negocio o los topes del plan. n8n lee
       tenant_agent_config directo, así que la fila tiene que estar SIEMPRE
       dentro del plan, no solo cuando pasa por esta API.

Igual que acceso_plan: la prueba sin plan asociado y la falta total de planes
activos no tienen topes. Una prueba con plan asociado usa los límites de ese
plan, y un negocio sin suscripción vigente usa los del plan más bajo activo
(spec 005a RF-10, D14, CL-24). Sin suscripción, además, ya lo frena
exigir_herramienta("agente") para los módulos del portal.
"""

from dataclasses import dataclass
from uuid import UUID

import asyncpg
from fastapi import HTTPException, status

from session import fetch_one

# Catálogo de modelos que el agente sabe ejecutar, del más barato al más caro.
# Es la única lista: agente.py, la validación de planes y el panel la leen de
# acá. Un modelo nuevo se suma aquí y en services/agente/modelo.py, que decide
# qué parámetros admite (no todas las generaciones aceptan los mismos).
MODELOS_AGENTE: tuple[str, ...] = (
    "claude-haiku-4-5-20251001",
    "claude-sonnet-5",
    "claude-opus-5",
)

HISTORIAL_MIN = 5
HISTORIAL_MAX = 200


@dataclass(frozen=True)
class LimitesAgente:
    plan: str | None
    # None = cualquier modelo del catálogo. Si hay lista, el primero es el default.
    modelos: tuple[str, ...] | None
    # None = hasta HISTORIAL_MAX.
    historial_maximo: int | None

    @property
    def modelo_por_defecto(self) -> str | None:
        return self.modelos[0] if self.modelos else None

    def modelos_disponibles(self) -> tuple[str, ...]:
        return self.modelos if self.modelos is not None else MODELOS_AGENTE

    def tope_historial(self) -> int:
        return self.historial_maximo if self.historial_maximo is not None else HISTORIAL_MAX

    def modelo_cabe(self, modelo: str | None) -> bool:
        """None (el default de n8n) solo cabe si el plan no limita modelos."""
        if self.modelos is None:
            return True
        return modelo is not None and modelo in self.modelos

    def historial_cabe(self, historial: int) -> bool:
        return historial <= self.tope_historial()


SIN_LIMITES = LimitesAgente(plan=None, modelos=None, historial_maximo=None)


def ordenar_modelos(modelos: list[str]) -> list[str]:
    """Sin repetidos y en el orden del catálogo (barato → caro): el primero queda de default."""
    return [m for m in MODELOS_AGENTE if m in set(modelos)]


def evaluar(plan: str | None, modelos, historial_maximo, estado_plataforma: str | None) -> LimitesAgente:
    """La regla sin base de datos: el piloto y el negocio sin plan no tienen topes."""
    if plan is None or (estado_plataforma or "activo") == "prueba":
        return SIN_LIMITES
    return LimitesAgente(
        plan=plan,
        modelos=tuple(modelos) if modelos else None,
        historial_maximo=historial_maximo,
    )


async def limites_de_tenant(tenant_id: UUID) -> LimitesAgente:
    """Límites del plan que rige hoy al negocio (spec 005a RF-10, D14, D20, CL-24).

    Orden: prueba (con plan asociado -> sus límites; sin plan -> sin límites),
    suscripción vigente (sus límites aunque el plan esté desactivado),
    sin suscripción vigente (los del plan más bajo activo),
    sin ningún plan activo (sin límites).
    """
    from services.numeros_reglas import PlanCatalogo, Suscripcion, reglas_de_negocio, situacion_comercial
    from datetime import datetime, timezone

    fila = await fetch_one(
        """
        SELECT s.plan, s.estado, s.origen, s.fecha_renovacion,
               ep.estado AS estado_plataforma
        FROM tenants t
        LEFT JOIN tenant_subscriptions     s  ON  s.tenant_id = t.id
        LEFT JOIN tenant_estado_plataforma ep ON ep.tenant_id = t.id
        WHERE t.id = $1
        """,
        tenant_id,
    )
    if fila is None:
        return SIN_LIMITES
    from session import fetch_all as _fetch_all

    planes_db = await _fetch_all(
        """
        SELECT nombre, activo, precio_monthly, max_numeros_whatsapp,
               modelos_permitidos, historial_maximo
        FROM planes
        """
    )
    suscripcion = None
    if fila["plan"] is not None or fila["estado"] is not None:
        suscripcion = Suscripcion(
            plan=fila["plan"],
            estado=fila["estado"] or "",
            origen=fila["origen"] or "pago",
            fecha_renovacion=fila["fecha_renovacion"],
        )
    ahora = datetime.now(timezone.utc)
    situacion, plan_asociado = situacion_comercial(
        suscripcion=suscripcion,
        estado_plataforma=fila["estado_plataforma"],
        ahora=ahora,
    )
    planes = [
        PlanCatalogo(
            nombre=p["nombre"],
            activo=bool(p["activo"]),
            precio_mensual=float(p["precio_monthly"]),
            max_numeros=p["max_numeros_whatsapp"],
            modelos=tuple(p["modelos_permitidos"]) if p["modelos_permitidos"] else None,
            historial_maximo=p["historial_maximo"],
            familias=frozenset(),
        )
        for p in planes_db
    ]
    reglas = reglas_de_negocio(situacion=situacion, plan_asociado=plan_asociado, planes=planes)
    if reglas.modelos is None and reglas.historial_maximo is None:
        return SIN_LIMITES
    return LimitesAgente(
        plan=reglas.plan,
        modelos=reglas.modelos,
        historial_maximo=reglas.historial_maximo,
    )


def exigir_dentro_del_plan(limites: LimitesAgente, modelo: str | None, historial: int) -> None:
    """402 si el modelo o el historial pedidos no caben en el plan."""
    plan = limites.plan.capitalize() if limites.plan else "actual"

    if not limites.modelo_cabe(modelo):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "codigo": "modelo_no_incluido_en_plan",
                "mensaje": (
                    f"El modelo {modelo or 'por defecto'} no está incluido en tu plan {plan}. "
                    f"Modelos disponibles: {', '.join(limites.modelos_disponibles())}. "
                    "Mejora tu plan para usar otro."
                ),
                "plan_actual": limites.plan,
                "modelos_permitidos": list(limites.modelos_disponibles()),
            },
        )

    if not limites.historial_cabe(historial):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "codigo": "historial_excede_plan",
                "mensaje": (
                    f"Tu plan {plan} permite hasta {limites.tope_historial()} mensajes de historial "
                    f"y pediste {historial}. Mejora tu plan para dar más contexto al agente."
                ),
                "plan_actual": limites.plan,
                "historial_maximo": limites.tope_historial(),
            },
        )


async def contar_fuera_de_limite(
    conn: asyncpg.Connection,
    plan: str,
    modelos: list[str] | None,
    historial_maximo: int | None,
) -> int:
    """
    Cuántos negocios con `plan` quedarían recortados con estos topes. Sirve para
    dejarlo en la auditoría de gerencia: se llama ANTES de actualizar el plan,
    porque después el trigger ya los recortó. Misma condición que
    recortar_agente_a_plan (excluye pilotos).
    """
    return await conn.fetchval(
        """
        SELECT COUNT(*)
        FROM tenant_subscriptions s
        JOIN tenant_agent_config ac ON ac.tenant_id = s.tenant_id
        LEFT JOIN tenant_estado_plataforma ep ON ep.tenant_id = s.tenant_id
        WHERE s.plan = $1
          AND COALESCE(ep.estado, 'activo') <> 'prueba'
          AND (
               ($2::text[] IS NOT NULL AND (ac.model IS NULL OR NOT (ac.model = ANY ($2::text[]))))
               OR ($3::int IS NOT NULL AND ac.history_window > $3::int)
          )
        """,
        plan,
        modelos,
        historial_maximo,
    )
