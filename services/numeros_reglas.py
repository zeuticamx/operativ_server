"""
Reglas puras de varios números de WhatsApp por negocio (spec 005a).

Todas las decisiones de la spec que no necesitan base de datos: la situación
comercial del negocio, qué reglas (cupo, límites, herramientas) le tocan, el
orden del alta de un número, qué números se pausan al bajar el cupo, quién es
el principal, cuándo responde un número y qué recorte aplica un plan.

Sin acceso a la base de datos ni al reloj: "ahora" llega siempre por parámetro.
Así se prueba con tablas de casos (tests/test_numeros_reglas.py). La función
SQL `limites_efectivos` (sql/58) tiene que dar lo mismo con los mismos casos;
un test de paridad de T2 lo comprueba.

Estas funciones no escriben ni lanzan HTTPException: devuelven la decisión y
los servicios (numeros_whatsapp, numeros_reevaluacion) la aplican.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable, Literal, Sequence
from uuid import UUID

# Las cinco familias del catálogo base de herramientas del agente (P5). La
# lista de herramientas de un plan se expresa con estas familias.
FAMILIAS_HERRAMIENTAS: tuple[str, ...] = (
    "calendario",
    "sql_lectura",
    "http",
    "google_sheets",
    "google_docs",
)

# RF-2 paso 1: cinco altas ajenas rechazadas en una ventana deslizante de 24 h.
MAX_INTENTOS_ALTA_AJENA = 5
VENTANA_INTENTOS = timedelta(hours=24)

EstadoNumero = Literal["activo", "pausado", "desvinculado"]
RelacionNumero = Literal["libre", "propio", "ajeno"]
CodigoRespuesta = Literal["numero_desvinculado", "numero_no_activo", "configuracion_incompleta"]


class Situacion(str, Enum):
    """Situación comercial del negocio (RF-9 a RF-11), evaluada en este orden."""

    PRUEBA = "prueba"
    SUSCRIPCION = "suscripcion"
    SIN_SUSCRIPCION = "sin_suscripcion"


@dataclass(frozen=True)
class Suscripcion:
    """Lo que las reglas necesitan de una fila de `tenant_subscriptions`."""

    plan: str | None
    estado: str  # activa | pausada | cancelada
    origen: str  # pago | prueba
    # None = sin fecha de fin (por ejemplo, una prueba sin vencimiento).
    fecha_renovacion: datetime | None


@dataclass(frozen=True)
class PlanCatalogo:
    """Un plan del catálogo con lo que importa a esta spec. `nombre` lo identifica."""

    nombre: str
    activo: bool
    precio_mensual: float
    # None = sin tope de números.
    max_numeros: int | None
    # None = cualquier modelo; si hay lista, el primero es el default del plan.
    modelos: tuple[str, ...] | None
    # None = sin límite de historial.
    historial_maximo: int | None
    familias: frozenset[str]


@dataclass(frozen=True)
class ReglasNegocio:
    """Reglas vigentes de un negocio: cupo, límites del agente y herramientas."""

    # Plan de donde salen las reglas (None si no hay ninguno que aplique).
    plan: str | None
    tope: int | None
    modelos: tuple[str, ...] | None
    historial_maximo: int | None
    familias: frozenset[str]
    # CL-20: negocio sin suscripción vigente y sin ningún plan activo. La
    # reevaluación lo deja en la bitácora de la plataforma.
    sin_plan_activo: bool = False


@dataclass(frozen=True)
class NumeroCanal:
    """Un número de WhatsApp tal como lo ven las reglas (fila de `agentes_canal`)."""

    id: UUID
    estado: EstadoNumero
    vinculado_en: datetime
    es_principal: bool
    es_linea_avisos: bool
    agent_name: str | None
    system_prompt: str | None
    agente_activo: bool


@dataclass(frozen=True)
class ResultadoAlta:
    permitido: bool
    # 201 si se da de alta; si no, 429, 402, 409 o 404.
    estado_http: int
    codigo: str | None
    # Solo con 429: cuándo sale de la ventana el intento que desbloquea.
    libera_en: datetime | None = None
    # Solo el 404 de alta ajena cuenta para el bloqueo del paso 1.
    cuenta_como_intento: bool = False


# ------------------------------------------------------------
# Situación comercial y reglas del negocio
# ------------------------------------------------------------
def _vigente(suscripcion: Suscripcion, ahora: datetime) -> bool:
    """Activa y sin la fecha pasada. Una 'activa' vencida se lee como no vigente sin esperar al job."""
    if suscripcion.estado != "activa":
        return False
    return suscripcion.fecha_renovacion is None or suscripcion.fecha_renovacion >= ahora


def situacion_comercial(
    *,
    suscripcion: Suscripcion | None,
    estado_plataforma: str | None,
    ahora: datetime,
) -> tuple[Situacion, str | None]:
    """
    La situación del negocio y el plan asociado a la prueba o a la suscripción.

    La prueba va primero (D36): una suscripción de origen prueba cuenta como
    prueba, y el piloto (`tenant_estado_plataforma.estado = 'prueba'`) también.
    Una prueba pausada, cancelada o vencida, y una suscripción pausada,
    cancelada o vencida, no son vigentes (A75, A30). Sin nada, sin suscripción
    vigente (CL-24).

    El plan devuelto es el de la suscripción cuando la situación es prueba o
    suscripción; en un piloto sin suscripción no hay plan asociado.
    """
    if (estado_plataforma or "activo") == "prueba":
        return Situacion.PRUEBA, suscripcion.plan if suscripcion else None

    if suscripcion is not None and _vigente(suscripcion, ahora):
        if suscripcion.origen == "prueba":
            return Situacion.PRUEBA, suscripcion.plan
        return Situacion.SUSCRIPCION, suscripcion.plan

    return Situacion.SIN_SUSCRIPCION, None


def plan_mas_bajo_activo(planes: Iterable[PlanCatalogo]) -> PlanCatalogo | None:
    """
    El plan activo de menor precio mensual. Con empate gana el de menor cupo
    (sin tope cuenta como mayor) y después el de nombre más bajo (D11, D31).
    """
    activos = [p for p in planes if p.activo]
    if not activos:
        return None
    return min(
        activos,
        key=lambda p: (
            p.precio_mensual,
            p.max_numeros is None,
            p.max_numeros if p.max_numeros is not None else 0,
            p.nombre,
        ),
    )


def _reglas_de_plan(plan: PlanCatalogo, *, tope: int | None) -> ReglasNegocio:
    return ReglasNegocio(
        plan=plan.nombre,
        tope=tope,
        modelos=plan.modelos or None,
        historial_maximo=plan.historial_maximo,
        familias=plan.familias,
    )


def _sin_limites(plan: str | None = None, *, sin_plan_activo: bool = False) -> ReglasNegocio:
    return ReglasNegocio(
        plan=plan,
        tope=None,
        modelos=None,
        historial_maximo=None,
        familias=frozenset(FAMILIAS_HERRAMIENTAS),
        sin_plan_activo=sin_plan_activo,
    )


def reglas_de_negocio(
    *,
    situacion: Situacion,
    plan_asociado: str | None,
    planes: Sequence[PlanCatalogo],
) -> ReglasNegocio:
    """
    Cupo, límites de modelo e historial y familias de herramientas que aplican
    (RF-9, RF-10, RF-11), en el mismo orden:

      - prueba: sin tope; límites y familias del plan asociado, o sin límites y
        catálogo completo si no tiene plan asociado (D14);
      - suscripción: todo del plan contratado, aunque esté desactivado (D20);
      - sin suscripción: todo del plan más bajo activo; sin ningún plan
        activo, sin tope, sin límites y catálogo completo (CL-20).

    Un plan asociado que ya no está en el catálogo se trata como "sin plan":
    el catálogo completo y sin límites.
    """
    por_nombre = {p.nombre: p for p in planes}

    if situacion is Situacion.PRUEBA:
        plan = por_nombre.get(plan_asociado) if plan_asociado else None
        if plan is None:
            return _sin_limites()
        return _reglas_de_plan(plan, tope=None)

    if situacion is Situacion.SUSCRIPCION:
        plan = por_nombre.get(plan_asociado) if plan_asociado else None
        if plan is None:
            return _sin_limites()
        return _reglas_de_plan(plan, tope=plan.max_numeros)

    plan = plan_mas_bajo_activo(planes)
    if plan is None:
        return _sin_limites(sin_plan_activo=True)
    return _reglas_de_plan(plan, tope=plan.max_numeros)


# ------------------------------------------------------------
# Estado de un número
# ------------------------------------------------------------
def configuracion_incompleta(agent_name: str | None, system_prompt: str | None) -> bool:
    """RF-8: sin prompt, con el prompt en blanco o sin nombre de agente."""
    return not (system_prompt or "").strip() or not (agent_name or "").strip()


def responde(numero: NumeroCanal) -> bool:
    """RF-7: activo, agente encendido y configuración completa."""
    return (
        numero.estado == "activo"
        and numero.agente_activo
        and not configuracion_incompleta(numero.agent_name, numero.system_prompt)
    )


def orden_respuesta_portal(numero: NumeroCanal) -> CodigoRespuesta | None:
    """
    RF-12: por qué no se puede responder a mano desde el portal, o None si se
    puede. El primero que aplica decide: desvinculado, pausado (aunque también
    esté incompleto, CL-29), activo incompleto. Con el agente apagado la
    respuesta humana se permite (D44).
    """
    if numero.estado == "desvinculado":
        return "numero_desvinculado"
    if numero.estado == "pausado":
        return "numero_no_activo"
    if configuracion_incompleta(numero.agent_name, numero.system_prompt):
        return "configuracion_incompleta"
    return None


# ------------------------------------------------------------
# Alta, pausa y principal
# ------------------------------------------------------------
def evaluar_alta(
    *,
    intentos_recientes: Sequence[datetime],
    ahora: datetime,
    plazas_usadas: int,
    tope: int | None,
    relacion_numero: RelacionNumero,
) -> ResultadoAlta:
    """
    RF-2: el orden del alta; el primer paso que aplica decide (A68).

      1. 429 `demasiados_intentos`: cinco altas ajenas rechazadas en las
         últimas 24 h (ventana deslizante). Alcanza también a los números libres.
      2. 402 `cupo_numeros_whatsapp`: cupo lleno, sea cual sea el número.
      3. 409 `numero_ya_vinculado`: número propio, activo o pausado.
      4. 404 `numero_no_encontrado`: número ajeno. Cuenta como intento.
      5. Número libre: se da de alta.

    `intentos_recientes` son las horas (`creado_en`) de las respuestas 404 de
    alta ajena del negocio; las que ya salieron de la ventana se ignoran.
    """
    desde = ahora - VENTANA_INTENTOS
    en_ventana = sorted((i for i in intentos_recientes if i > desde), reverse=True)
    if len(en_ventana) >= MAX_INTENTOS_ALTA_AJENA:
        # Se libera cuando sale de la ventana el más antiguo de los cinco más
        # recientes: ahí quedan menos de cinco.
        liberador = en_ventana[MAX_INTENTOS_ALTA_AJENA - 1]
        return ResultadoAlta(False, 429, "demasiados_intentos", libera_en=liberador + VENTANA_INTENTOS)

    if tope is not None and plazas_usadas >= tope:
        return ResultadoAlta(False, 402, "cupo_numeros_whatsapp")

    if relacion_numero == "propio":
        return ResultadoAlta(False, 409, "numero_ya_vinculado")

    if relacion_numero == "ajeno":
        return ResultadoAlta(False, 404, "numero_no_encontrado", cuenta_como_intento=True)

    return ResultadoAlta(True, 201, None)


def _por_antiguedad(numero: NumeroCanal) -> tuple[datetime, UUID]:
    """Orden de antigüedad: fecha de vinculación y, si empatan, identificador más bajo (D17)."""
    return (numero.vinculado_en, numero.id)


def numeros_a_pausar(activos: Sequence[NumeroCanal], tope: int | None) -> list[UUID]:
    """
    RF-11, D15: los números que sobran del cupo, del más reciente al más
    antiguo. El principal también puede entrar. La línea de avisos no cuenta ni
    se pausa. Sin tope no se pausa nada.
    """
    if tope is None:
        return []
    cuentan = [n for n in activos if n.estado == "activo" and not n.es_linea_avisos]
    sobran = len(cuentan) - tope
    if sobran <= 0:
        return []
    mas_recientes_primero = sorted(cuentan, key=_por_antiguedad, reverse=True)
    return [n.id for n in mas_recientes_primero[:sobran]]


def _puede_ser_principal(numero: NumeroCanal) -> bool:
    return (
        numero.estado == "activo"
        and not numero.es_linea_avisos
        and not configuracion_incompleta(numero.agent_name, numero.system_prompt)
    )


def principal_resultante(numeros: Sequence[NumeroCanal], principal_actual: UUID | None) -> UUID | None:
    """
    Regla única de asignación del principal (D34, D33).

    Un principal vigente (activo, completo y que no es la línea de avisos) se
    conserva. Si no hay o dejó de serlo, pasa a serlo el activo completo más
    antiguo (desempate: identificador más bajo). Un número reactivado no
    recupera el papel si ya hay principal. Sin ningún candidato, None.
    """
    candidatos = [n for n in numeros if _puede_ser_principal(n)]
    if principal_actual is not None and any(n.id == principal_actual for n in candidatos):
        return principal_actual
    if not candidatos:
        return None
    return min(candidatos, key=_por_antiguedad).id


# ------------------------------------------------------------
# Límites del plan sobre la configuración
# ------------------------------------------------------------
def modelo_efectivo(
    modelo: str | None,
    modelos_permitidos: Sequence[str] | None,
    modelo_plataforma: str,
) -> str:
    """
    Modelo con el que responde un número (D32, RF-10). Sin restricción del plan:
    el suyo o, si no tiene, el de la plataforma. Con restricción: el suyo si el
    plan lo permite; si no (o si no tiene), el default del plan (el primero).
    """
    if not modelos_permitidos:
        return modelo or modelo_plataforma
    if modelo is not None and modelo in modelos_permitidos:
        return modelo
    return modelos_permitidos[0]


def recorte(valor: int, tope: int | None) -> int:
    """RF-10: recorta al tope sin subir nunca un valor. Sin tope no cambia."""
    if tope is None:
        return valor
    return min(valor, tope)


def permite_activar(tool_familia: str, familias_aplicables: frozenset[str]) -> bool:
    """RF-6: una herramienta solo se activa si su familia está en la lista del plan."""
    return tool_familia in familias_aplicables
