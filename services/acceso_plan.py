"""
Gate de herramientas del portal por plan de suscripción.

Responde "¿este negocio puede usar ESTA herramienta ahora?", que no es la
misma pregunta que services/acceso_pagos.py. Conviven a propósito:

    acceso_pagos  ¿el agente de n8n sigue contestando a los clientes
                  finales? Modelo híbrido: basta con suscripción vigente O
                  créditos, y un negocio que nunca pagó no se apaga. No se
                  toca acá: cambiarlo cambiaría lo que ve el cliente final
                  en WhatsApp, y tiene un espejo en SQL (SQL_AGENTE_OPERANDO).

    acceso_plan   ¿el dueño puede entrar a Calendario, al CRM de campo, a
                  Herramientas...? Solo cuenta el plan: los créditos son
                  consumo, no un derecho a herramientas, y sin suscripción
                  vigente no hay herramientas (salvo piloto, ver abajo).

La matriz plan → herramientas vive en la tabla `planes`
(25_planes_herramientas.sql), editable desde /gerencia/planes. Acá solo se
evalúa.

Acceso efectivo a una herramienta = el plan la incluye Y la cuenta está
vigente. El tercer factor — que el dueño la tenga encendida en
tenant_servicios — lo siguen comprobando los choke points de cada módulo
(_exigir_modulo, verificar_calendario_activo) con su 409, que va antes.

Todos los rechazos son 402 con un `detail` estructurado (ver
`detalle_bloqueo`): el portal usa `codigo` para decidir qué vista amigable
mostrar. No 403: ese ya significa "tu rol no puede" (vendedor, "ver
como"), y el portal tiene que distinguir "no te toca" de "tu plan no lo
incluye".
"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, status

from session import fetch_all, fetch_one

Herramienta = Literal["agente", "vendedores", "herramientas", "crm_campo", "calendario"]

# Herramienta → columna de `planes` que dice si el plan la incluye. Es la
# única lista de herramientas: sumar una es agregar la columna en SQL y la
# entrada acá.
COLUMNA_PLAN: dict[str, str] = {
    "agente": "agente_ia_activo",
    "vendedores": "gestion_vendedores_activo",
    "herramientas": "herramientas_activo",
    "crm_campo": "crm_campo_activo",
    "calendario": "calendario_activo",
}
HERRAMIENTAS: tuple[str, ...] = tuple(COLUMNA_PLAN)

NOMBRE_HERRAMIENTA: dict[str, str] = {
    "agente": "El agente de IA",
    "vendedores": "La gestión de vendedores",
    "herramientas": "Las herramientas del agente",
    "crm_campo": "El CRM de campo",
    "calendario": "El calendario",
}

EstadoCuenta = Literal["vigente", "vencido", "cancelado", "sin_plan", "prueba", "suspendido"]
CodigoBloqueo = Literal["plan_insuficiente", "plan_requerido", "cuenta_suspendida"]

# Mismo criterio que acceso_pagos.ESTADOS_BLOQUEANTES: la decisión de
# gerencia gana sobre cualquier pago.
_ESTADOS_PLATAFORMA_BLOQUEANTES = frozenset({"suspendido", "baja"})

_ESTADO_POR_SUSCRIPCION: dict[str | None, EstadoCuenta] = {
    "activa": "vigente",
    "pausada": "vencido",
    "cancelada": "cancelado",
    None: "sin_plan",
}


@dataclass(frozen=True)
class AccesoPlan:
    estado: EstadoCuenta
    plan: str | None
    # Lo que se puede usar AHORA, no lo que el plan incluye en abstracto:
    # un plan vencido deja esto vacío aunque el plan tenga de todo.
    herramientas: frozenset[str]

    def permite(self, herramienta: str) -> bool:
        return herramienta in self.herramientas

    def codigo_bloqueo(self, herramienta: str) -> CodigoBloqueo | None:
        """None si puede usarla; si no, por qué no."""
        if self.permite(herramienta):
            return None
        if self.estado == "suspendido":
            return "cuenta_suspendida"
        if self.estado == "vigente":
            return "plan_insuficiente"
        return "plan_requerido"


def incluidas_en(fila_plan) -> frozenset[str]:
    """Las herramientas que marca una fila de `planes` (o un dict con esas columnas)."""
    if fila_plan is None:
        return frozenset()
    return frozenset(h for h, col in COLUMNA_PLAN.items() if fila_plan.get(col))


def evaluar(
    estado_suscripcion: str | None,
    estado_plataforma: str | None,
    incluidas: frozenset[str],
    plan: str | None = None,
) -> AccesoPlan:
    """
    La regla entera, sin base de datos (así la prueba una matriz de casos).

    Orden, del candado más fuerte al más débil:
      1. Suspensión/baja de gerencia: nada, pase lo que pase con el pago.
      2. Piloto ('prueba'): todo, aunque no tenga plan — es la idea del
         piloto, y es como se deja operar a un negocio de demo o desarrollo.
      3. Suscripción 'activa': lo que incluya su plan.
      4. Pausada (venció sin renovar), cancelada o nunca contrató: nada.
    """
    plataforma = estado_plataforma or "activo"

    if plataforma in _ESTADOS_PLATAFORMA_BLOQUEANTES:
        return AccesoPlan(estado="suspendido", plan=plan, herramientas=frozenset())

    if plataforma == "prueba":
        return AccesoPlan(estado="prueba", plan=plan, herramientas=frozenset(HERRAMIENTAS))

    estado = _ESTADO_POR_SUSCRIPCION.get(estado_suscripcion, "sin_plan")
    if estado == "vigente":
        return AccesoPlan(estado=estado, plan=plan, herramientas=incluidas)
    return AccesoPlan(estado=estado, plan=plan, herramientas=frozenset())


_COLUMNAS_HERRAMIENTAS = ", ".join(f"p.{c}" for c in COLUMNA_PLAN.values())


async def acceso_plan(tenant_id: UUID) -> AccesoPlan:
    fila = await fetch_one(
        f"""
        SELECT
            s.plan,
            s.estado AS estado_suscripcion,
            ep.estado AS estado_plataforma,
            {_COLUMNAS_HERRAMIENTAS}
        FROM tenants t
        LEFT JOIN tenant_subscriptions     s  ON  s.tenant_id = t.id
        LEFT JOIN planes                   p  ON  p.nombre    = s.plan
        LEFT JOIN tenant_estado_plataforma ep ON ep.tenant_id = t.id
        WHERE t.id = $1
        """,
        tenant_id,
    )
    if fila is None:
        return evaluar(None, None, frozenset())

    return evaluar(
        fila["estado_suscripcion"],
        fila["estado_plataforma"],
        incluidas_en(dict(fila)),
        plan=fila["plan"],
    )


async def herramientas_por_plan() -> list[tuple[str, frozenset[str]]]:
    """Planes que hoy se pueden contratar, en el orden de la grilla, con lo que incluye cada uno."""
    filas = await fetch_all(
        f"""
        SELECT p.nombre, {_COLUMNAS_HERRAMIENTAS}
        FROM planes p
        WHERE p.activo
        ORDER BY p.orden, p.precio_monthly
        """
    )
    return [(f["nombre"], incluidas_en(dict(f))) for f in filas]


def _enumerar(nombres: list[str]) -> str:
    """['pro', 'enterprise'] → 'Pro y Enterprise'."""
    nombres = [n.capitalize() for n in nombres]
    if len(nombres) <= 1:
        return "".join(nombres)
    return ", ".join(nombres[:-1]) + f" y {nombres[-1]}"


def _mensaje(codigo: CodigoBloqueo, herramienta: str, planes: list[str]) -> str:
    nombre = NOMBRE_HERRAMIENTA.get(herramienta, "Esta herramienta")
    if codigo == "cuenta_suspendida":
        return "Tu cuenta está en pausa. Escríbenos y te ayudamos a reactivarla."
    if codigo == "plan_requerido":
        return f"{nombre} necesita un plan activo. Renueva o elige un plan para seguir."
    if not planes:
        return f"{nombre} no está incluido en tu plan."
    articulo = "el plan" if len(planes) == 1 else "los planes"
    return f"{nombre} está incluido en {articulo} {_enumerar(planes)}."


def detalle_bloqueo(
    acceso: AccesoPlan,
    herramienta: str,
    catalogo: list[tuple[str, frozenset[str]]],
) -> dict:
    codigo = acceso.codigo_bloqueo(herramienta)
    planes = [nombre for nombre, incluidas in catalogo if herramienta in incluidas]
    return {
        "codigo": codigo,
        "herramienta": herramienta,
        "estado": acceso.estado,
        "plan_actual": acceso.plan,
        "planes_que_la_incluyen": planes,
        "mensaje": _mensaje(codigo, herramienta, planes) if codigo else "",
    }


async def exigir_herramienta(tenant_id: UUID, herramienta: str) -> AccesoPlan:
    """402 con detalle estructurado si el negocio no puede usar `herramienta`."""
    acceso = await acceso_plan(tenant_id)
    if acceso.permite(herramienta):
        return acceso

    catalogo = await herramientas_por_plan()
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail=detalle_bloqueo(acceso, herramienta, catalogo),
    )
