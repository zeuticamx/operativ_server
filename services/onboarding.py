"""
Cuestionario de bienvenida: de "qué negocio tienes" a una configuración de
arranque (sql/42_onboarding.sql).

Tres salidas, todas a partir de las respuestas del dueño:

  1. Un system prompt armado con plantillas fijas por giro (sin LLM: es
     determinista, no cuesta créditos y se puede probar). El dueño lo sigue
     editando en /agente como siempre.
  2. Los módulos que le sirven (`modulos_sugeridos`), con los mismos nombres
     de herramienta que services/acceso_plan.py.
  3. El plan más barato del catálogo que cubre esos módulos y el tamaño de
     su equipo (`recomendar_plan`). Lee la matriz de la tabla `planes`, no
     una copia en código: gerencia la mueve desde /gerencia/planes.

Aplicar (`aplicar`) escribe el prompt, le da al negocio el plan recomendado
de prueba si nunca tuvo suscripción, y enciende los módulos que su plan
vigente permite. Nunca cobra ni contrata: el plan se recomienda y el portal
manda a /suscripcion.

Reglas propias:
  - El prompt no se pisa si el dueño lo editó a mano después (ni el inicial
    del alta ni el último que escribió el onboarding): hace falta
    `sobrescribir_prompt`.
  - La prueba del onboarding es una sola vez por negocio y solo para quien
    nunca tuvo fila en tenant_subscriptions: un negocio que pagó y canceló
    no la renueva gratis contestando otra vez el cuestionario.
  - Solo enciende módulos, nunca apaga.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import asyncpg
from fastapi import HTTPException, status

from schemas import (
    EstadoOnboarding,
    OnboardingAplicadoOut,
    OnboardingGerenciaOut,
    OnboardingOut,
    OnboardingRecomendacionOut,
    OnboardingRespuestasIn,
    PlanRecomendadoOut,
    PruebaOnboardingOut,
)
from services import herramientas_calendario
from services.acceso_plan import COLUMNA_PLAN, HERRAMIENTAS, NOMBRE_HERRAMIENTA, acceso_plan
from services.creditos import cargar_creditos_plan
from services.gerencia import registrar_auditoria
from services.pipeline import set_tenant_servicios
from session import conexion, fetch_one, transaccion

# Prompt con el que nace toda cuenta (routers/auth._crear_cuenta). Vive acá
# porque el onboarding necesita reconocerlo para saber si el dueño ya
# escribió el suyo.
PROMPT_INICIAL = """Eres el asistente virtual de {negocio}.

# TONO
Cercano, profesional y resolutivo. Español neutro.

# FORMATO
- Máximo 2 o 3 líneas por respuesta. Es un chat, no un correo.
- Ve directo a resolver, sin fórmulas de cortesía largas.

# REGLAS
- Si no sabes algo, dilo. No inventes datos ni precios.
- Usa el historial: no vuelvas a preguntar lo que el cliente ya te dijo.
- Si preguntan si eres una IA, confírmalo con naturalidad."""

DIAS_PRUEBA = 14

# Va en tenant_subscriptions.otorgada_por: gerencia distingue así una prueba
# del onboarding de una que otorgó una persona.
OTORGADA_POR = "onboarding"

# Mismo criterio que acceso_plan._ESTADOS_PLATAFORMA_BLOQUEANTES.
_ESTADOS_SIN_PRUEBA = frozenset({"suspendido", "baja"})

# Módulos con interruptor en tenant_servicios. Los demás (herramientas,
# CRM de campo) solo dependen del plan; el agente nace encendido.
_INTERRUPTOR = {"vendedores": "gestion_vendedores_activo", "calendario": "calendario_activo"}


# ============================================================
# Plantillas
# ============================================================
@dataclass(frozen=True)
class PlantillaGiro:
    # Cómo se nombra el giro dentro del prompt ("Eres el asistente de X, {nombre}").
    nombre: str
    tareas: tuple[str, ...]
    reglas: tuple[str, ...] = ()


# Claves = schemas.GiroNegocio.
PLANTILLAS_GIRO: dict[str, PlantillaGiro] = {
    "salon_belleza": PlantillaGiro(
        nombre="una barbería, salón de belleza o spa",
        tareas=(
            "Informa servicios, duración aproximada y precios que conozcas.",
            "Ayuda al cliente a elegir servicio y, si lo pide, con quién atenderse.",
        ),
    ),
    "salud": PlantillaGiro(
        nombre="un consultorio o clínica",
        tareas=(
            "Informa especialidades, servicios y requisitos para la consulta.",
            "Orienta al paciente sobre cómo y cuándo puede atenderse.",
        ),
        reglas=(
            "No des diagnósticos, indicaciones médicas ni dosis: ofrece una consulta.",
            "Ante una urgencia, indica que acuda a un servicio de emergencias.",
        ),
    ),
    "restaurante": PlantillaGiro(
        nombre="un restaurante o negocio de comida",
        tareas=(
            "Informa menú, precios, horario y zonas o formas de entrega que conozcas.",
            "Toma los datos de pedidos o reservaciones cuando el cliente lo pida.",
        ),
    ),
    "tienda": PlantillaGiro(
        nombre="una tienda",
        tareas=(
            "Resuelve dudas de productos, disponibilidad, precios y formas de pago.",
            "Si el cliente quiere comprar, junta lo que necesita para cerrar la venta.",
        ),
        reglas=("No confirmes existencias que no puedas verificar.",),
    ),
    "inmobiliaria": PlantillaGiro(
        nombre="una inmobiliaria",
        tareas=(
            "Pregunta qué busca el cliente: compra o renta, zona, presupuesto y tamaño.",
            "Comparte la información de los inmuebles que conozcas y ofrece una visita.",
        ),
        reglas=("No prometas que un inmueble sigue disponible sin confirmarlo.",),
    ),
    "servicios_profesionales": PlantillaGiro(
        nombre="un despacho de servicios profesionales",
        tareas=(
            "Explica los servicios que ofrece el despacho y cómo se contratan.",
            "Entiende el caso del cliente con pocas preguntas y ofrece una cita o llamada.",
        ),
        reglas=("No des asesoría formal por chat: el detalle lo ve un especialista.",),
    ),
    "educacion": PlantillaGiro(
        nombre="una escuela o centro de cursos",
        tareas=(
            "Informa cursos, horarios, modalidades, costos e inscripciones.",
            "Ayuda al interesado a elegir el curso que le conviene.",
        ),
    ),
    "otro": PlantillaGiro(
        nombre="un negocio",
        tareas=("Resuelve las dudas de los clientes sobre lo que ofrece el negocio.",),
    ),
}

TONOS: dict[str, str] = {
    "cercano": "Cercano, profesional y resolutivo. Trata de tú. Español neutro.",
    "formal": "Formal y respetuoso. Trata de usted. Español neutro.",
    "juvenil": "Relajado y amigable. Trata de tú; algún emoji ocasional si encaja. Español neutro.",
}

_TAREA_MODULO: dict[str, str] = {
    "calendario": (
        "Agenda, cambia o cancela citas con las herramientas del calendario. "
        "Nunca confirmes una cita que no hayas creado."
    ),
    "vendedores": (
        "Si el cliente quiere comprar o pide una cotización, toma su nombre y qué "
        "necesita para que un asesor le dé seguimiento."
    ),
    "herramientas": (
        "Antes de dar datos que cambian (precios, existencias, pedidos), consulta "
        "las herramientas conectadas."
    ),
}


def modulos_sugeridos(r: OnboardingRespuestasIn) -> list[str]:
    """Los módulos que le sirven, en el orden de acceso_plan.HERRAMIENTAS."""
    quiere = {
        "agente": True,
        "vendedores": r.vende_por_chat,
        "herramientas": r.consulta_sistemas,
        "crm_campo": r.visitas_campo,
        "calendario": r.agenda_citas,
    }
    return [h for h in HERRAMIENTAS if quiere.get(h)]


def generar_prompt(r: OnboardingRespuestasIn, negocio: str) -> str:
    plantilla = PLANTILLAS_GIRO[r.giro]
    modulos = modulos_sugeridos(r)

    lineas = [f"Eres el asistente virtual de {negocio}, {plantilla.nombre}.", ""]

    negocio_info = []
    if r.descripcion:
        negocio_info.append(f"- Qué ofrece, en palabras del dueño: «{r.descripcion}».")
    if r.horario:
        negocio_info.append(f"- Horario de atención: {r.horario}.")
    if negocio_info:
        lineas += ["# NEGOCIO", *negocio_info, ""]

    tareas = [*plantilla.tareas, *(_TAREA_MODULO[m] for m in modulos if m in _TAREA_MODULO)]
    lineas += ["# QUÉ HACES", *(f"- {t}" for t in tareas), ""]

    lineas += ["# TONO", TONOS[r.tono], ""]

    lineas += [
        "# FORMATO",
        "- Máximo 2 o 3 líneas por respuesta. Es un chat, no un correo.",
        "- Ve directo a resolver, sin fórmulas de cortesía largas.",
        "",
        "# REGLAS",
        "- Si no sabes algo, dilo. No inventes datos ni precios.",
        "- Usa el historial: no vuelvas a preguntar lo que el cliente ya te dijo.",
        "- Si preguntan si eres una IA, confírmalo con naturalidad.",
        *(f"- {regla}" for regla in plantilla.reglas),
    ]
    return "\n".join(lineas)


# ============================================================
# Plan recomendado
# ============================================================
@dataclass(frozen=True)
class PlanCatalogo:
    nombre: str
    precio_monthly: Decimal
    herramientas: frozenset[str]
    # None = sin tope.
    max_vendedores: int | None
    max_proveedores: int | None


def _faltantes(plan: PlanCatalogo, modulos: list[str], vendedores: int, proveedores: int) -> list[str]:
    """Lo que `plan` no cubre, en texto para mostrar."""
    faltan = [NOMBRE_HERRAMIENTA[m] for m in modulos if m not in plan.herramientas]
    if plan.max_vendedores is not None and vendedores > plan.max_vendedores:
        faltan.append(f"{vendedores} vendedores (permite {plan.max_vendedores})")
    if plan.max_proveedores is not None and proveedores > plan.max_proveedores:
        faltan.append(f"{proveedores} personas que atienden citas (permite {plan.max_proveedores})")
    return faltan


def recomendar_plan(
    modulos: list[str],
    vendedores: int,
    proveedores: int,
    catalogo: list[PlanCatalogo],
) -> PlanRecomendadoOut | None:
    """
    El primer plan del catálogo (ordenado de más barato a más caro) que
    cubre módulos y cupos. Si ninguno, el que deja menos afuera — empate:
    el más barato — con `cubre_todo=False`.

    Los cupos solo cuentan si el módulo se usa: tener 20 vendedores no pide
    un plan grande si no va a usar ni el embudo ni el CRM de campo.
    """
    if not catalogo:
        return None

    usa_vendedores = "vendedores" in modulos or "crm_campo" in modulos
    usa_proveedores = "calendario" in modulos
    v = vendedores if usa_vendedores else 0
    p = proveedores if usa_proveedores else 0

    mejor: tuple[PlanCatalogo, list[str]] | None = None
    for plan in catalogo:
        faltan = _faltantes(plan, modulos, v, p)
        if mejor is None or len(faltan) < len(mejor[1]):
            mejor = (plan, faltan)
        if not faltan:
            break

    plan, faltan = mejor
    return PlanRecomendadoOut(
        nombre=plan.nombre,
        precio_monthly=plan.precio_monthly,
        herramientas=[h for h in HERRAMIENTAS if h in plan.herramientas],
        max_vendedores=plan.max_vendedores,
        max_proveedores=plan.max_proveedores,
        cubre_todo=not faltan,
        faltantes=faltan,
    )


_COLUMNAS_HERRAMIENTAS = ", ".join(COLUMNA_PLAN.values())


async def catalogo(conn: asyncpg.Connection) -> list[PlanCatalogo]:
    """Planes contratables, en el orden de la grilla de /suscripcion."""
    filas = await conn.fetch(
        f"""
        SELECT nombre, precio_monthly, max_vendedores, max_proveedores,
               {_COLUMNAS_HERRAMIENTAS}
        FROM planes
        WHERE activo
        ORDER BY orden, precio_monthly
        """
    )
    return [
        PlanCatalogo(
            nombre=f["nombre"],
            precio_monthly=f["precio_monthly"],
            herramientas=frozenset(h for h, col in COLUMNA_PLAN.items() if f[col]),
            max_vendedores=f["max_vendedores"],
            max_proveedores=f["max_proveedores"],
        )
        for f in filas
    ]


def recomendar(
    r: OnboardingRespuestasIn, negocio: str, planes: list[PlanCatalogo]
) -> OnboardingRecomendacionOut:
    modulos = modulos_sugeridos(r)
    return OnboardingRecomendacionOut(
        system_prompt=generar_prompt(r, negocio),
        modulos=modulos,
        plan=recomendar_plan(modulos, r.num_vendedores, r.num_proveedores, planes),
    )


# ============================================================
# Estado
# ============================================================
def estado_de(fila) -> EstadoOnboarding:
    if fila is None:
        return "sin_iniciar"
    if fila["completado_en"] is not None:
        return "completado"
    if fila["omitido_en"] is not None:
        return "omitido"
    return "pendiente"


def prompt_editado(actual: str | None, negocio: str, prompt_generado: str | None) -> bool:
    """¿El prompt vigente lo escribió el dueño? (y no el alta ni el onboarding)"""
    if not actual:
        return False
    propios = {PROMPT_INICIAL.format(negocio=negocio).strip()}
    if prompt_generado:
        propios.add(prompt_generado.strip())
    return actual.strip() not in propios


async def pendiente(tenant_id: UUID) -> bool:
    """Para GET /auth/yo: cuenta nueva que no contestó ni omitió el cuestionario."""
    fila = await fetch_one(
        """
        SELECT 1 FROM tenant_onboarding
        WHERE tenant_id = $1 AND completado_en IS NULL AND omitido_en IS NULL
        """,
        tenant_id,
    )
    return fila is not None


_SQL_CONTEXTO = """
    SELECT t.name AS negocio,
           o.tenant_id IS NOT NULL AS tiene_fila,
           o.respuestas, o.plan_recomendado, o.prompt_generado,
           o.completado_en, o.omitido_en, o.prueba_otorgada_en, o.actualizado_en,
           ac.system_prompt,
           s.tenant_id IS NOT NULL AS tuvo_suscripcion,
           ep.estado AS estado_plataforma
    FROM tenants t
    LEFT JOIN tenant_onboarding        o  ON  o.tenant_id = t.id
    LEFT JOIN tenant_agent_config      ac ON ac.tenant_id = t.id
    LEFT JOIN tenant_subscriptions     s  ON  s.tenant_id = t.id
    LEFT JOIN tenant_estado_plataforma ep ON ep.tenant_id = t.id
    WHERE t.id = $1
"""


def _prueba_disponible(ctx) -> bool:
    return (
        not ctx["tuvo_suscripcion"]
        and ctx["prueba_otorgada_en"] is None
        and (ctx["estado_plataforma"] or "activo") not in _ESTADOS_SIN_PRUEBA
    )


def _respuestas(ctx) -> OnboardingRespuestasIn | None:
    if ctx["respuestas"] is None:
        return None
    return OnboardingRespuestasIn.model_validate(ctx["respuestas"])


def _no_encontrado() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Negocio no encontrado")


async def obtener(tenant_id: UUID) -> OnboardingOut:
    async with conexion() as conn:
        ctx = await conn.fetchrow(_SQL_CONTEXTO, tenant_id)
        if ctx is None:
            raise _no_encontrado()
        respuestas = _respuestas(ctx)
        recomendacion = (
            recomendar(respuestas, ctx["negocio"], await catalogo(conn)) if respuestas else None
        )

    return OnboardingOut(
        estado=estado_de(ctx if ctx["tiene_fila"] else None),
        respuestas=respuestas,
        recomendacion=recomendacion,
        prueba_disponible=_prueba_disponible(ctx),
        dias_prueba=DIAS_PRUEBA,
        prompt_editado=prompt_editado(ctx["system_prompt"], ctx["negocio"], ctx["prompt_generado"]),
    )


async def guardar_respuestas(tenant_id: UUID, r: OnboardingRespuestasIn) -> OnboardingOut:
    """Guarda (o reemplaza) las respuestas. No aplica nada todavía."""
    async with conexion() as conn:
        await conn.execute(
            """
            INSERT INTO tenant_onboarding (tenant_id, respuestas)
            VALUES ($1, $2::jsonb)
            ON CONFLICT (tenant_id) DO UPDATE SET
                respuestas     = EXCLUDED.respuestas,
                actualizado_en = NOW()
            """,
            tenant_id,
            r.model_dump(),
        )
    return await obtener(tenant_id)


async def omitir(tenant_id: UUID) -> None:
    """'Omitir por ahora'. No toca un cuestionario ya aplicado."""
    async with conexion() as conn:
        await conn.execute(
            """
            INSERT INTO tenant_onboarding (tenant_id, omitido_en)
            VALUES ($1, NOW())
            ON CONFLICT (tenant_id) DO UPDATE SET
                omitido_en     = COALESCE(tenant_onboarding.omitido_en, NOW()),
                actualizado_en = NOW()
            WHERE tenant_onboarding.completado_en IS NULL
            """,
            tenant_id,
        )


# ============================================================
# Aplicar
# ============================================================
async def _otorgar_prueba(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    plan: str,
    ahora: datetime,
    actor_email: str,
    actor_id: UUID,
) -> datetime | None:
    """
    Misma forma que services/pruebas.otorgar_prueba (una fila 'activa' con
    origen 'prueba'), así el gate de plan, el job que la pausa al vencer y
    un pago que la reemplaza la tratan igual.

    `ON CONFLICT DO NOTHING`: si entre la lectura y acá apareció una
    suscripción (un pago que se aprobó en ese instante), gana esa y no hay
    prueba. Devuelve None en ese caso.
    """
    vence = ahora + timedelta(days=DIAS_PRUEBA)
    insertada = await conn.fetchval(
        """
        INSERT INTO tenant_subscriptions
            (tenant_id, plan, estado, precio_monthly, fecha_inicio,
             fecha_renovacion, intentos_fallidos, origen, otorgada_por)
        VALUES ($1, $2, 'activa', $3, $4, $5, 0, 'prueba', $6)
        ON CONFLICT (tenant_id) DO NOTHING
        RETURNING true
        """,
        tenant_id,
        plan,
        Decimal(0),
        ahora,
        vence,
        OTORGADA_POR,
    )
    if not insertada:
        return None
    await cargar_creditos_plan(
        conn,
        tenant_id,
        plan,
        vence,
        f"onboarding:{uuid4()}",
        f"Créditos del plan {plan} (prueba de bienvenida)",
    )
    await registrar_auditoria(
        actor_email=actor_email,
        actor_portal_user_id=actor_id,
        accion="prueba_onboarding",
        tenant_id=tenant_id,
        detalle={"plan": plan, "vence": vence.isoformat(), "dias": DIAS_PRUEBA},
        conn=conn,
    )
    return vence


async def aplicar(
    tenant_id: UUID,
    *,
    sobrescribir_prompt: bool,
    actor_email: str,
    actor_id: UUID,
    ahora: datetime | None = None,
) -> OnboardingAplicadoOut:
    ahora = ahora or datetime.now(timezone.utc)

    async with transaccion() as conn:
        # FOR UPDATE de la fila del onboarding: dos "Aplicar" a la vez (doble
        # clic, dos pestañas) se serializan acá y el segundo ya ve la prueba
        # otorgada.
        await conn.execute(
            "SELECT 1 FROM tenant_onboarding WHERE tenant_id = $1 FOR UPDATE", tenant_id
        )
        ctx = await conn.fetchrow(_SQL_CONTEXTO, tenant_id)
        if ctx is None:
            raise _no_encontrado()
        respuestas = _respuestas(ctx)
        if respuestas is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Primero contesta el cuestionario",
            )

        reco = recomendar(respuestas, ctx["negocio"], await catalogo(conn))

        editado = prompt_editado(ctx["system_prompt"], ctx["negocio"], ctx["prompt_generado"])
        prompt_actualizado = not editado or sobrescribir_prompt
        if prompt_actualizado:
            await conn.execute(
                """
                INSERT INTO tenant_agent_config (tenant_id, agent_name, system_prompt)
                VALUES ($1, 'Asistente', $2)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    system_prompt = EXCLUDED.system_prompt,
                    updated_at    = NOW()
                """,
                tenant_id,
                reco.system_prompt,
            )

        prueba = None
        if reco.plan and _prueba_disponible(ctx):
            vence = await _otorgar_prueba(
                conn, tenant_id, reco.plan.nombre, ahora, actor_email, actor_id
            )
            if vence is not None:
                prueba = PruebaOnboardingOut(plan=reco.plan.nombre, vence=vence)

        await conn.execute(
            """
            UPDATE tenant_onboarding SET
                completado_en      = NOW(),
                plan_recomendado   = $2,
                prompt_generado    = CASE WHEN $3 THEN $4 ELSE prompt_generado END,
                prueba_otorgada_en = CASE WHEN $5 THEN NOW() ELSE prueba_otorgada_en END,
                actualizado_en     = NOW()
            WHERE tenant_id = $1
            """,
            tenant_id,
            reco.plan.nombre if reco.plan else None,
            prompt_actualizado,
            reco.system_prompt,
            prueba is not None,
        )

    # Después del commit: acceso_plan lee con su propia conexión y tiene que
    # ver la prueba recién otorgada. Si esto falla, el prompt y la prueba ya
    # quedaron y el dueño enciende los módulos desde su pantalla.
    acceso = await acceso_plan(tenant_id)
    encendidos = [m for m in reco.modulos if acceso.permite(m)]
    pendientes = [m for m in reco.modulos if not acceso.permite(m)]

    a_encender = {m for m in encendidos if m in _INTERRUPTOR}
    if a_encender:
        await set_tenant_servicios(
            tenant_id,
            None,
            True if "vendedores" in a_encender else None,
            True if "calendario" in a_encender else None,
        )
    if "calendario" in a_encender:
        await herramientas_calendario.sincronizar(tenant_id, True)

    return OnboardingAplicadoOut(
        prompt_actualizado=prompt_actualizado,
        modulos_encendidos=encendidos,
        modulos_pendientes=pendientes,
        prueba=prueba,
        plan_recomendado=reco.plan.nombre if reco.plan else None,
    )


async def para_gerencia(tenant_id: UUID) -> OnboardingGerenciaOut:
    ctx = await fetch_one(_SQL_CONTEXTO, tenant_id)
    if ctx is None:
        raise _no_encontrado()
    return OnboardingGerenciaOut(
        estado=estado_de(ctx if ctx["tiene_fila"] else None),
        respuestas=_respuestas(ctx),
        plan_recomendado=ctx["plan_recomendado"],
        completado_en=ctx["completado_en"],
        omitido_en=ctx["omitido_en"],
        prueba_otorgada_en=ctx["prueba_otorgada_en"],
        actualizado_en=ctx["actualizado_en"],
    )
