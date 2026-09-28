"""
La regla del gate por plan (services/acceso_plan.py), sin base de datos.

Recorre la matriz entera: cada plan de fábrica × cada herramienta × cada
estado de cuenta. Si alguien cambia el orden de los candados (suspensión >
piloto > suscripción) o lo que cuenta como "vigente", esto lo marca antes de
que llegue a un endpoint.
"""

import pytest

from services.acceso_plan import (
    HERRAMIENTAS,
    AccesoPlan,
    detalle_bloqueo,
    evaluar,
    incluidas_en,
)

# Lo que deja 25_planes_herramientas.sql en los tres planes de fábrica.
MATRIZ = {
    "starter": frozenset({"agente", "vendedores"}),
    "pro": frozenset(HERRAMIENTAS),
    "enterprise": frozenset(HERRAMIENTAS),
}
CATALOGO = list(MATRIZ.items())


# ------------------------------------------------------------
# Plan vigente: solo lo que incluye su nivel
# ------------------------------------------------------------
@pytest.mark.parametrize("plan", list(MATRIZ))
@pytest.mark.parametrize("herramienta", HERRAMIENTAS)
def test_plan_vigente_permite_exactamente_lo_que_incluye(plan, herramienta):
    acceso = evaluar("activa", "activo", MATRIZ[plan], plan=plan)

    assert acceso.estado == "vigente"
    assert acceso.permite(herramienta) is (herramienta in MATRIZ[plan])
    if not acceso.permite(herramienta):
        assert acceso.codigo_bloqueo(herramienta) == "plan_insuficiente"
    else:
        assert acceso.codigo_bloqueo(herramienta) is None


def test_starter_no_incluye_calendario_crm_ni_herramientas():
    acceso = evaluar("activa", "activo", MATRIZ["starter"], plan="starter")
    for herramienta in ("calendario", "crm_campo", "herramientas"):
        assert not acceso.permite(herramienta)


# ------------------------------------------------------------
# Cuenta no vigente: nada, tenga el plan que tenga
# ------------------------------------------------------------
@pytest.mark.parametrize(
    "estado_suscripcion, estado_esperado",
    [("pausada", "vencido"), ("cancelada", "cancelado"), (None, "sin_plan")],
)
@pytest.mark.parametrize("herramienta", HERRAMIENTAS)
def test_sin_plan_vigente_bloquea_todo_con_plan_requerido(
    estado_suscripcion, estado_esperado, herramienta
):
    # Aunque el plan (vencido) fuera enterprise: lo que vale es que no está vigente.
    acceso = evaluar(estado_suscripcion, "activo", MATRIZ["enterprise"], plan="enterprise")

    assert acceso.estado == estado_esperado
    assert acceso.herramientas == frozenset()
    assert acceso.codigo_bloqueo(herramienta) == "plan_requerido"


def test_estado_de_suscripcion_desconocido_cuenta_como_sin_plan():
    assert evaluar("rara", "activo", MATRIZ["pro"]).estado == "sin_plan"


def test_sin_fila_de_estado_de_plataforma_cuenta_como_activo():
    """Tenants anteriores a 14_gerencia_auditoria.sql no tienen fila."""
    assert evaluar("activa", None, MATRIZ["pro"]).estado == "vigente"


# ------------------------------------------------------------
# Decisiones de gerencia: van antes que la suscripción
# ------------------------------------------------------------
@pytest.mark.parametrize("estado_plataforma", ["suspendido", "baja"])
@pytest.mark.parametrize("herramienta", HERRAMIENTAS)
def test_suspension_gana_aunque_el_plan_este_al_dia(estado_plataforma, herramienta):
    acceso = evaluar("activa", estado_plataforma, MATRIZ["enterprise"], plan="enterprise")

    assert acceso.estado == "suspendido"
    assert acceso.codigo_bloqueo(herramienta) == "cuenta_suspendida"


@pytest.mark.parametrize("estado_suscripcion", ["activa", "pausada", "cancelada", None])
def test_piloto_tiene_todo_aunque_no_tenga_plan(estado_suscripcion):
    acceso = evaluar(estado_suscripcion, "prueba", MATRIZ["starter"])

    assert acceso.estado == "prueba"
    assert acceso.herramientas == frozenset(HERRAMIENTAS)


# ------------------------------------------------------------
# Lectura de la matriz desde una fila de `planes`
# ------------------------------------------------------------
def test_incluidas_en_lee_las_columnas_del_plan():
    fila = {
        "agente_ia_activo": True,
        "gestion_vendedores_activo": False,
        "herramientas_activo": None,  # plan borrado: LEFT JOIN sin match
        "crm_campo_activo": True,
        "calendario_activo": False,
    }
    assert incluidas_en(fila) == frozenset({"agente", "crm_campo"})
    assert incluidas_en(None) == frozenset()


# ------------------------------------------------------------
# El detalle del 402, que es lo que lee el portal
# ------------------------------------------------------------
def test_detalle_de_plan_insuficiente_dice_que_planes_la_incluyen():
    acceso = evaluar("activa", "activo", MATRIZ["starter"], plan="starter")
    detalle = detalle_bloqueo(acceso, "calendario", CATALOGO)

    assert detalle == {
        "codigo": "plan_insuficiente",
        "herramienta": "calendario",
        "estado": "vigente",
        "plan_actual": "starter",
        "planes_que_la_incluyen": ["pro", "enterprise"],
        "mensaje": "El calendario está incluido en los planes Pro y Enterprise.",
    }


def test_detalle_con_un_solo_plan_usa_singular():
    catalogo = [("starter", MATRIZ["starter"]), ("pro", frozenset({"calendario"}))]
    acceso = evaluar("activa", "activo", MATRIZ["starter"], plan="starter")

    assert detalle_bloqueo(acceso, "calendario", catalogo)["mensaje"] == (
        "El calendario está incluido en el plan Pro."
    )


def test_detalle_de_plan_requerido_invita_a_renovar():
    detalle = detalle_bloqueo(evaluar("pausada", "activo", MATRIZ["pro"]), "agente", CATALOGO)

    assert detalle["codigo"] == "plan_requerido"
    assert detalle["estado"] == "vencido"
    assert "Renueva" in detalle["mensaje"]


def test_detalle_de_cuenta_suspendida_no_ofrece_pagar():
    detalle = detalle_bloqueo(evaluar("activa", "suspendido", MATRIZ["pro"]), "agente", CATALOGO)

    assert detalle["codigo"] == "cuenta_suspendida"
    assert "plan" not in detalle["mensaje"].lower()


def test_herramienta_que_ningun_plan_incluye():
    catalogo = [("starter", frozenset({"agente"}))]
    acceso = AccesoPlan(estado="vigente", plan="starter", herramientas=frozenset({"agente"}))

    detalle = detalle_bloqueo(acceso, "calendario", catalogo)
    assert detalle["planes_que_la_incluyen"] == []
    assert detalle["mensaje"] == "El calendario no está incluido en tu plan."
