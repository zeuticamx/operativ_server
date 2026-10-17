"""
Reglas puras de varios números de WhatsApp por negocio (spec 005a, T1):
services/numeros_reglas.py. Sin base de datos ni servidor.

Cada test lleva en el nombre el criterio de aceptación (A) de la spec que cubre.
El mismo cuadro de casos de la situación comercial y de las reglas del negocio
se repite después contra la función SQL `limites_efectivos` (test de paridad
de T2).
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from services.numeros_reglas import (
    FAMILIAS_HERRAMIENTAS,
    NumeroCanal,
    PlanCatalogo,
    Situacion,
    Suscripcion,
    configuracion_incompleta,
    evaluar_alta,
    modelo_efectivo,
    numeros_a_pausar,
    orden_respuesta_portal,
    permite_activar,
    plan_mas_bajo_activo,
    principal_resultante,
    recorte,
    reglas_de_negocio,
    responde,
    situacion_comercial,
)

AHORA = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
FUTURO = AHORA + timedelta(days=10)
PASADO = AHORA - timedelta(days=1)

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-5"
OPUS = "claude-opus-5"
TODAS = frozenset(FAMILIAS_HERRAMIENTAS)


def _plan(
    nombre: str,
    precio: float = 10,
    tope: int | None = None,
    activo: bool = True,
    modelos: tuple[str, ...] | None = None,
    historial: int | None = None,
    familias: frozenset[str] = TODAS,
) -> PlanCatalogo:
    return PlanCatalogo(
        nombre=nombre,
        activo=activo,
        precio_mensual=precio,
        max_numeros=tope,
        modelos=modelos,
        historial_maximo=historial,
        familias=familias,
    )


def _susc(plan: str | None, estado: str, origen: str, renovacion: datetime | None = FUTURO) -> Suscripcion:
    return Suscripcion(plan=plan, estado=estado, origen=origen, fecha_renovacion=renovacion)


def _id(n: int) -> UUID:
    return UUID(int=n)


def _num(
    n: int,
    dias: int = 0,
    estado: str = "activo",
    principal: bool = False,
    avisos: bool = False,
    nombre: str = "Ana",
    prompt: str = "Eres un asistente.",
    agente: bool = True,
) -> NumeroCanal:
    """`dias` = antigüedad: más grande = más antiguo."""
    return NumeroCanal(
        id=_id(n),
        estado=estado,
        vinculado_en=AHORA - timedelta(days=dias),
        es_principal=principal,
        es_linea_avisos=avisos,
        agent_name=nombre,
        system_prompt=prompt,
        agente_activo=agente,
    )


# ------------------------------------------------------------
# situacion_comercial (RF-9 a RF-11, D36, CL-24)
# ------------------------------------------------------------
@pytest.mark.parametrize(
    "suscripcion, estado_plataforma, esperado, plan",
    [
        # A23: prueba vigente con plan asociado
        (_susc("pro", "activa", "prueba"), None, Situacion.PRUEBA, "pro"),
        # A23: prueba antes que suscripción (D36): origen prueba no es suscripción
        (_susc("pro", "activa", "prueba", FUTURO), "activo", Situacion.PRUEBA, "pro"),
        # A23: prueba sin fecha de fin (piloto de la suscripción) sigue vigente
        (_susc("pro", "activa", "prueba", None), None, Situacion.PRUEBA, "pro"),
        # A23: piloto por estado de plataforma, sin suscripción: prueba sin plan asociado
        (None, "prueba", Situacion.PRUEBA, None),
        # A23: piloto por estado de plataforma, con suscripción: prueba con su plan
        (_susc("starter", "activa", "pago"), "prueba", Situacion.PRUEBA, "starter"),
        # A75: prueba pausada, cancelada o vencida no es vigente
        (_susc("pro", "pausada", "prueba"), None, Situacion.SIN_SUSCRIPCION, None),
        (_susc("pro", "cancelada", "prueba"), None, Situacion.SIN_SUSCRIPCION, None),
        (_susc("pro", "activa", "prueba", PASADO), None, Situacion.SIN_SUSCRIPCION, None),
        # A30: suscripción de pago activa y vigente
        (_susc("pro", "activa", "pago"), None, Situacion.SUSCRIPCION, "pro"),
        (_susc("pro", "activa", "pago"), "activo", Situacion.SUSCRIPCION, "pro"),
        # A30: cancelación programada = activa hasta el fin del periodo pagado
        (_susc("pro", "activa", "pago", AHORA), None, Situacion.SUSCRIPCION, "pro"),
        # A30: activa con la fecha pasada se lee como no vigente, sin esperar al job
        (_susc("pro", "activa", "pago", PASADO), None, Situacion.SIN_SUSCRIPCION, None),
        # A30: suscripción pausada o cancelada: sin suscripción vigente
        (_susc("pro", "pausada", "pago"), None, Situacion.SIN_SUSCRIPCION, None),
        (_susc("pro", "cancelada", "pago"), None, Situacion.SIN_SUSCRIPCION, None),
        # CL-24: negocio que nunca tuvo nada
        (None, None, Situacion.SIN_SUSCRIPCION, None),
        (None, "activo", Situacion.SIN_SUSCRIPCION, None),
    ],
)
def test_A23_A30_A75_situacion_comercial(suscripcion, estado_plataforma, esperado, plan):
    assert situacion_comercial(
        suscripcion=suscripcion, estado_plataforma=estado_plataforma, ahora=AHORA
    ) == (esperado, plan)


# ------------------------------------------------------------
# plan_mas_bajo_activo (D11, D31)
# ------------------------------------------------------------
def test_A28_plan_mas_bajo_es_el_de_menor_precio():
    planes = [_plan("pro", 50), _plan("starter", 10), _plan("max", 90)]
    assert plan_mas_bajo_activo(planes).nombre == "starter"


def test_A28_plan_mas_bajo_ignora_los_inactivos():
    planes = [_plan("gratis", 0, activo=False), _plan("starter", 10), _plan("pro", 50)]
    assert plan_mas_bajo_activo(planes).nombre == "starter"


def test_A28_empate_de_precio_gana_el_menor_cupo():
    planes = [_plan("b", 10, tope=3), _plan("a", 10, tope=2)]
    assert plan_mas_bajo_activo(planes).nombre == "a"


def test_A28_empate_de_precio_sin_tope_cuenta_como_mayor():
    planes = [_plan("a", 10, tope=None), _plan("z", 10, tope=5)]
    assert plan_mas_bajo_activo(planes).nombre == "z"


def test_A28_empate_de_precio_y_cupo_gana_el_nombre_ascendente():
    planes = [_plan("beta", 10, tope=2), _plan("alfa", 10, tope=2)]
    assert plan_mas_bajo_activo(planes).nombre == "alfa"
    # Mismo resultado con dos planes sin tope.
    assert plan_mas_bajo_activo([_plan("beta"), _plan("alfa")]).nombre == "alfa"


def test_A36_sin_planes_activos_no_hay_plan_mas_bajo():
    assert plan_mas_bajo_activo([]) is None
    assert plan_mas_bajo_activo([_plan("viejo", activo=False)]) is None


# ------------------------------------------------------------
# reglas_de_negocio (RF-9, RF-10, RF-11)
# ------------------------------------------------------------
PLANES = [
    _plan("starter", 10, tope=1, modelos=(HAIKU,), historial=20, familias=frozenset({"calendario"})),
    _plan(
        "pro", 50, tope=3, modelos=(HAIKU, SONNET), historial=60, familias=frozenset({"calendario", "http"})
    ),
    _plan("viejo", 99, tope=7, activo=False, modelos=(OPUS,), historial=150, familias=frozenset({"sql_lectura"})),
]


def test_A23_prueba_con_plan_asociado_sin_tope_y_limites_del_plan():
    r = reglas_de_negocio(situacion=Situacion.PRUEBA, plan_asociado="pro", planes=PLANES)
    assert r.tope is None
    assert r.modelos == (HAIKU, SONNET)
    assert r.historial_maximo == 60
    assert r.familias == frozenset({"calendario", "http"})


def test_A23_prueba_sin_plan_asociado_catalogo_completo_y_sin_limites():
    r = reglas_de_negocio(situacion=Situacion.PRUEBA, plan_asociado=None, planes=PLANES)
    assert (r.tope, r.modelos, r.historial_maximo) == (None, None, None)
    assert r.familias == TODAS


def test_A23_prueba_con_plan_asociado_que_ya_no_existe_se_trata_como_sin_plan():
    r = reglas_de_negocio(situacion=Situacion.PRUEBA, plan_asociado="borrado", planes=PLANES)
    assert (r.tope, r.modelos, r.historial_maximo) == (None, None, None)
    assert r.familias == TODAS


def test_A24_suscripcion_vigente_usa_cupo_y_limites_de_su_plan():
    r = reglas_de_negocio(situacion=Situacion.SUSCRIPCION, plan_asociado="pro", planes=PLANES)
    assert r.tope == 3
    assert r.modelos == (HAIKU, SONNET)
    assert r.historial_maximo == 60
    assert r.familias == frozenset({"calendario", "http"})


def test_A24_plan_contratado_desactivado_conserva_su_lista_limites_y_cupo():
    r = reglas_de_negocio(situacion=Situacion.SUSCRIPCION, plan_asociado="viejo", planes=PLANES)
    assert r.tope == 7
    assert r.modelos == (OPUS,)
    assert r.historial_maximo == 150
    assert r.familias == frozenset({"sql_lectura"})


def test_A28_sin_suscripcion_usa_el_plan_mas_bajo_activo():
    r = reglas_de_negocio(situacion=Situacion.SIN_SUSCRIPCION, plan_asociado=None, planes=PLANES)
    assert r.plan == "starter"
    assert r.tope == 1
    assert r.modelos == (HAIKU,)
    assert r.historial_maximo == 20
    assert r.familias == frozenset({"calendario"})
    assert r.sin_plan_activo is False


def test_A36_sin_plan_activo_sin_tope_ni_limites_y_queda_marcado():
    solo_inactivos = [p for p in PLANES if not p.activo]
    for planes in ([], solo_inactivos):
        r = reglas_de_negocio(situacion=Situacion.SIN_SUSCRIPCION, plan_asociado=None, planes=planes)
        assert (r.tope, r.modelos, r.historial_maximo) == (None, None, None)
        assert r.familias == TODAS
        assert r.sin_plan_activo is True


def test_A36_prueba_y_suscripcion_no_se_marcan_sin_plan_activo():
    r = reglas_de_negocio(situacion=Situacion.PRUEBA, plan_asociado=None, planes=[])
    assert r.sin_plan_activo is False


def test_A67_plan_sin_tope_deja_el_cupo_en_none():
    r = reglas_de_negocio(
        situacion=Situacion.SIN_SUSCRIPCION, plan_asociado=None, planes=[_plan("x", tope=None)]
    )
    assert r.tope is None


# ------------------------------------------------------------
# evaluar_alta (RF-2, A68)
# ------------------------------------------------------------
def _ventana(n: int, hace_horas: float = 1) -> list[datetime]:
    """n intentos, el más viejo hace `hace_horas`, separados por minutos."""
    return [AHORA - timedelta(hours=hace_horas) + timedelta(minutes=i) for i in range(n)]


def _alta(**kw):
    base = dict(intentos_recientes=[], ahora=AHORA, plazas_usadas=0, tope=None, relacion_numero="libre")
    base.update(kw)
    return evaluar_alta(**base)


def test_A7_numero_libre_con_cupo_se_da_de_alta():
    r = _alta(plazas_usadas=1, tope=2)
    assert r.permitido and r.estado_http == 201 and r.codigo is None
    assert r.cuenta_como_intento is False


def test_A67_sin_tope_ninguna_alta_se_rechaza_por_cupo():
    r = _alta(plazas_usadas=500, tope=None)
    assert r.permitido


def test_A3_cupo_lleno_da_402_sea_libre_ajeno_o_propio():
    for relacion in ("libre", "ajeno", "propio"):
        r = _alta(plazas_usadas=2, tope=2, relacion_numero=relacion)
        assert (r.permitido, r.estado_http, r.codigo) == (False, 402, "cupo_numeros_whatsapp")
        assert r.cuenta_como_intento is False


def test_A3_un_propio_pausado_con_cupo_lleno_da_402_no_409():
    # El propio pausado llega como relación "propio": el cupo se evalúa antes.
    r = _alta(plazas_usadas=1, tope=1, relacion_numero="propio")
    assert r.estado_http == 402


def test_A4_numero_propio_con_cupo_da_409_sin_contar_intento():
    r = _alta(plazas_usadas=1, tope=2, relacion_numero="propio")
    assert (r.permitido, r.estado_http, r.codigo) == (False, 409, "numero_ya_vinculado")
    assert r.cuenta_como_intento is False


def test_A7_numero_ajeno_con_cupo_da_404_y_cuenta_como_intento():
    r = _alta(plazas_usadas=1, tope=2, relacion_numero="ajeno")
    assert (r.permitido, r.estado_http, r.codigo) == (False, 404, "numero_no_encontrado")
    assert r.cuenta_como_intento is True


def test_A6_cinco_intentos_en_24h_bloquean_cualquier_alta_con_hora_de_liberacion():
    intentos = _ventana(5, hace_horas=2)
    for relacion in ("libre", "ajeno", "propio"):
        r = _alta(intentos_recientes=intentos, relacion_numero=relacion)
        assert (r.permitido, r.estado_http, r.codigo) == (False, 429, "demasiados_intentos")
        assert r.libera_en == min(intentos) + timedelta(hours=24)
        assert r.cuenta_como_intento is False


def test_A6_cuatro_intentos_no_bloquean():
    assert _alta(intentos_recientes=_ventana(4)).permitido


def test_A6_ventana_deslizante_el_intento_que_sale_de_la_ventana_libera():
    # El más viejo está a exactamente 24 h: ya salió de la ventana (quedan 4).
    intentos = [AHORA - timedelta(hours=24)] + _ventana(4, hace_horas=3)
    assert _alta(intentos_recientes=intentos).permitido
    # Si todavía no ha salido (23 h 59 min), siguen siendo 5 y bloquea.
    intentos = [AHORA - timedelta(hours=23, minutes=59)] + _ventana(4, hace_horas=3)
    r = _alta(intentos_recientes=intentos)
    assert r.estado_http == 429
    assert r.libera_en == AHORA + timedelta(minutes=1)


def test_A6_intentos_fuera_de_la_ventana_no_cuentan():
    viejos = [AHORA - timedelta(hours=30, minutes=i) for i in range(5)]
    assert _alta(intentos_recientes=viejos).permitido


def test_A6_hora_de_liberacion_con_mas_de_cinco_es_la_de_la_quinta_mas_reciente():
    # 7 intentos: para bajar de 5 hace falta que salga el tercero más antiguo.
    intentos = [AHORA - timedelta(hours=h) for h in (10, 9, 8, 7, 6, 5, 4)]
    r = _alta(intentos_recientes=intentos)
    assert r.estado_http == 429
    assert r.libera_en == AHORA - timedelta(hours=8) + timedelta(hours=24)


def test_A68_orden_del_alta_429_antes_que_402_antes_que_409_antes_que_404():
    bloqueado = _ventana(5)
    # 429 gana a un cupo lleno y a un ajeno.
    assert _alta(intentos_recientes=bloqueado, plazas_usadas=2, tope=2, relacion_numero="ajeno").estado_http == 429
    # 402 gana a un propio y a un ajeno.
    assert _alta(plazas_usadas=2, tope=2, relacion_numero="propio").estado_http == 402
    assert _alta(plazas_usadas=2, tope=2, relacion_numero="ajeno").estado_http == 402
    # 409 gana al alta libre; 404 sale solo si el número es ajeno.
    assert _alta(relacion_numero="propio").estado_http == 409
    assert _alta(relacion_numero="ajeno").estado_http == 404
    assert _alta(relacion_numero="libre").estado_http == 201


# ------------------------------------------------------------
# numeros_a_pausar (RF-11, D15)
# ------------------------------------------------------------
def test_A29_de_tres_a_uno_pausa_los_dos_mas_recientes_primero_el_mas_reciente():
    a, b, c = _num(1, dias=30), _num(2, dias=20), _num(3, dias=10)
    assert numeros_a_pausar([a, b, c], tope=1) == [c.id, b.id]


def test_A29_la_entrada_desordenada_da_el_mismo_resultado():
    a, b, c = _num(1, dias=30), _num(2, dias=20), _num(3, dias=10)
    assert numeros_a_pausar([c, a, b], tope=1) == [c.id, b.id]


def test_A32_el_principal_tambien_puede_pausarse():
    viejo = _num(1, dias=30)
    nuevo = _num(2, dias=1, principal=True)
    assert numeros_a_pausar([viejo, nuevo], tope=1) == [nuevo.id]


def test_A49_la_linea_de_avisos_no_cuenta_ni_se_pausa():
    avisos = _num(9, dias=1, avisos=True)
    a, b = _num(1, dias=30), _num(2, dias=20)
    assert numeros_a_pausar([a, b, avisos], tope=2) == []
    assert numeros_a_pausar([a, b, avisos], tope=1) == [b.id]


def test_A29_sin_tope_o_dentro_del_cupo_no_se_pausa_nada():
    nums = [_num(1, dias=3), _num(2, dias=2), _num(3, dias=1)]
    assert numeros_a_pausar(nums, tope=None) == []
    assert numeros_a_pausar(nums, tope=3) == []
    assert numeros_a_pausar(nums, tope=10) == []


def test_A46_misma_fecha_se_pausa_primero_el_de_identificador_mas_alto():
    a, b, c = _num(1, dias=5), _num(2, dias=5), _num(3, dias=5)
    assert numeros_a_pausar([a, b, c], tope=1) == [c.id, b.id]


def test_A29_solo_se_consideran_los_activos():
    a, b = _num(1, dias=30), _num(2, dias=20)
    pausado = _num(3, dias=1, estado="pausado")
    assert numeros_a_pausar([a, b, pausado], tope=2) == []


# ------------------------------------------------------------
# principal_resultante (D34, D33, CL-4, CL-30, CL-31)
# ------------------------------------------------------------
def test_A64_sin_principal_pasa_a_serlo_el_activo_completo_mas_antiguo():
    a, b = _num(1, dias=30), _num(2, dias=10)
    assert principal_resultante([b, a], principal_actual=None) == a.id


def test_A46_un_principal_vigente_se_conserva_aunque_haya_uno_mas_antiguo():
    viejo, principal = _num(1, dias=30), _num(2, dias=5, principal=True)
    assert principal_resultante([viejo, principal], principal_actual=principal.id) == principal.id


def test_A46_si_el_principal_se_pausa_lo_sustituye_el_activo_completo_mas_antiguo():
    pausado = _num(1, dias=30, estado="pausado", principal=True)
    a, b = _num(2, dias=20), _num(3, dias=10)
    assert principal_resultante([pausado, a, b], principal_actual=pausado.id) == a.id


def test_A64_si_el_principal_queda_incompleto_deja_de_serlo():
    incompleto = _num(1, dias=30, prompt="   ", principal=True)
    otro = _num(2, dias=10)
    assert principal_resultante([incompleto, otro], principal_actual=incompleto.id) == otro.id


def test_A46_un_reactivado_no_recupera_el_papel_si_ya_hay_principal():
    principal = _num(2, dias=5, principal=True)
    reactivado = _num(1, dias=60)  # el más antiguo, recién reactivado
    assert principal_resultante([reactivado, principal], principal_actual=principal.id) == principal.id


def test_A46_un_reactivado_pasa_a_principal_si_el_negocio_no_tenia():
    otro_incompleto = _num(2, dias=20, nombre="")
    reactivado = _num(1, dias=60)
    assert principal_resultante([reactivado, otro_incompleto], principal_actual=None) == reactivado.id


def test_A64_los_incompletos_se_excluyen():
    sin_prompt = _num(1, dias=30, prompt="")
    sin_nombre = _num(2, dias=20, nombre="  ")
    completo = _num(3, dias=1)
    assert principal_resultante([sin_prompt, sin_nombre, completo], None) == completo.id


def test_A49_la_linea_de_avisos_nunca_es_principal():
    avisos = _num(1, dias=90, avisos=True)
    otro = _num(2, dias=1)
    assert principal_resultante([avisos, otro], None) == otro.id
    assert principal_resultante([avisos], None) is None
    assert principal_resultante([avisos, otro], principal_actual=avisos.id) == otro.id


def test_A46_empate_de_antiguedad_gana_el_identificador_mas_bajo():
    a, b = _num(5, dias=7), _num(2, dias=7)
    assert principal_resultante([a, b], None) == b.id


def test_A64_sin_activos_completos_no_hay_principal():
    assert principal_resultante([], None) is None
    pausado = _num(1, estado="pausado")
    desvinculado = _num(2, estado="desvinculado")
    incompleto = _num(3, prompt="")
    assert principal_resultante([pausado, desvinculado, incompleto], None) is None


def test_A64_el_agente_apagado_no_quita_el_papel_de_principal():
    apagado = _num(1, agente=False, principal=True)
    assert principal_resultante([apagado], principal_actual=apagado.id) == apagado.id


# ------------------------------------------------------------
# configuracion_incompleta y responde (RF-7, RF-8)
# ------------------------------------------------------------
@pytest.mark.parametrize(
    "nombre, prompt, incompleta",
    [
        ("Ana", "Eres un asistente.", False),
        ("Ana", "", True),
        ("Ana", "   \n\t ", True),
        ("Ana", None, True),
        ("", "Eres un asistente.", True),
        ("   ", "Eres un asistente.", True),
        (None, "Eres un asistente.", True),
        ("", "", True),
    ],
)
def test_A20_configuracion_incompleta(nombre, prompt, incompleta):
    assert configuracion_incompleta(nombre, prompt) is incompleta


def test_A18_responde_solo_si_activo_agente_encendido_y_completo():
    assert responde(_num(1)) is True
    assert responde(_num(1, estado="pausado")) is False
    assert responde(_num(1, estado="desvinculado")) is False
    assert responde(_num(1, agente=False)) is False
    assert responde(_num(1, prompt="")) is False
    assert responde(_num(1, nombre="")) is False


def test_A19_apagar_el_agente_no_cambia_la_plaza_ni_la_regla_de_pausa():
    apagado = _num(1, agente=False)
    # Sigue siendo un número activo: cuenta para el cupo.
    assert numeros_a_pausar([apagado, _num(2, dias=-1)], tope=1) == [_id(2)]


# ------------------------------------------------------------
# orden_respuesta_portal (RF-12, A39, CL-29, D44)
# ------------------------------------------------------------
def test_A39_numero_desvinculado_da_numero_desvinculado():
    assert orden_respuesta_portal(_num(1, estado="desvinculado")) == "numero_desvinculado"
    # Aunque además esté incompleto.
    assert orden_respuesta_portal(_num(1, estado="desvinculado", prompt="")) == "numero_desvinculado"


def test_A39_numero_pausado_da_numero_no_activo_incluso_si_esta_incompleto():
    assert orden_respuesta_portal(_num(1, estado="pausado")) == "numero_no_activo"
    assert orden_respuesta_portal(_num(1, estado="pausado", prompt="")) == "numero_no_activo"  # CL-29


def test_A39_activo_incompleto_da_configuracion_incompleta():
    assert orden_respuesta_portal(_num(1, prompt="  ")) == "configuracion_incompleta"
    assert orden_respuesta_portal(_num(1, nombre="")) == "configuracion_incompleta"


def test_A39_con_agente_apagado_la_respuesta_humana_se_permite():
    assert orden_respuesta_portal(_num(1, agente=False)) is None


def test_A39_activo_y_completo_se_permite():
    assert orden_respuesta_portal(_num(1)) is None


def test_A39_agente_apagado_e_incompleto_sigue_bloqueado_por_incompleto():
    assert orden_respuesta_portal(_num(1, agente=False, prompt="")) == "configuracion_incompleta"


# ------------------------------------------------------------
# modelo_efectivo (RF-5 D32, RF-10, A27, A72)
# ------------------------------------------------------------
def test_A72_sin_modelo_y_plan_sin_restriccion_usa_el_de_la_plataforma():
    assert modelo_efectivo(None, None, SONNET) == SONNET


def test_A72_sin_modelo_y_plan_restringido_usa_el_primero_del_plan():
    assert modelo_efectivo(None, (HAIKU, SONNET), OPUS) == HAIKU


def test_A27_modelo_retirado_por_el_plan_pasa_al_default_del_plan():
    assert modelo_efectivo(OPUS, (HAIKU, SONNET), SONNET) == HAIKU


def test_A27_modelo_permitido_se_conserva():
    assert modelo_efectivo(SONNET, (HAIKU, SONNET), HAIKU) == SONNET
    assert modelo_efectivo(OPUS, None, HAIKU) == OPUS


def test_A27_lista_vacia_equivale_a_sin_restriccion():
    assert modelo_efectivo(None, (), SONNET) == SONNET
    assert modelo_efectivo(OPUS, (), SONNET) == OPUS


# ------------------------------------------------------------
# recorte (RF-10, A26: nunca sube)
# ------------------------------------------------------------
@pytest.mark.parametrize(
    "valor, tope, esperado",
    [(100, 50, 50), (50, 50, 50), (30, 50, 30), (10, 50, 10), (100, None, 100), (0, 5, 0)],
)
def test_A26_recorte_nunca_sube_un_valor(valor, tope, esperado):
    assert recorte(valor, tope) == esperado


# ------------------------------------------------------------
# permite_activar y catálogo (RF-6, A16)
# ------------------------------------------------------------
def test_A16_el_catalogo_base_tiene_cinco_familias():
    assert set(FAMILIAS_HERRAMIENTAS) == {"calendario", "sql_lectura", "http", "google_sheets", "google_docs"}


def test_A16_solo_se_activa_una_herramienta_de_una_familia_del_plan():
    assert permite_activar("http", frozenset({"calendario", "http"})) is True
    assert permite_activar("google_docs", frozenset({"calendario", "http"})) is False
    assert permite_activar("calendario", frozenset()) is False
