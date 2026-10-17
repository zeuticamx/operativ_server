"""
Reevaluación del cupo, jobs y despliegue (spec 005a, T8): RF-11 en sus siete
casos, retención de 12 meses (RF-14), limpieza de intentos (RF-2) y despliegue
(RF-22).

Dos partes, como test_numeros_servicio.py:

1. PURAS (siempre corren, sin base de datos): elección del motivo, motivos
   cerrados, registro del trabajo en el planificador, que los trabajos no
   propaguen errores y que las vías de Python que cambian la suscripción llamen
   a la reevaluación.

2. CON BASE DE DATOS (`requiere_bd`): solo corren con
   NUMEROS_SQL_TEST_DATABASE_URL apuntando a una Postgres DE PRUEBA con las
   migraciones aplicadas (ver test_numeros_sql.py). Cada test trabaja dentro de
   una transacción que se revierte. Excepción: la CONCURRENCIA (`bd_confirmada`)
   necesita dos conexiones que se vean entre sí y escribe de verdad en esa base.

Cada test lleva en el nombre el criterio de aceptación (A), el requisito (RF) o
el caso límite (CL) de la spec que cubre.
"""

import asyncio
import inspect
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from services import numeros_avisos, numeros_reevaluacion
from services.numeros_bitacora import MOTIVOS as MOTIVOS_BITACORA
from services.numeros_reevaluacion import (
    MOTIVO_PAUSA,
    drenar_cola,
    ejecutar_despliegue_una_vez,
    elegir_motivo,
    reevaluar_negocio,
    reevaluar_vencidos,
)
from services.numeros_reglas import FAMILIAS_HERRAMIENTAS
from test_numeros_reglas import PLANES, _plan
from test_numeros_servicio import (  # noqa: F401 - `avisos`, `bds` y `bd_confirmada` son fixtures
    _auditoria,
    _bitacora,
    _conversacion,
    _cuenta_portal,
    _fila,
    _negocio,
    _plan_de,
    avisos,
    bd_confirmada,
    bds,
)
from test_numeros_sql import (  # noqa: F401
    URL_BD,
    _catalogo,
    _estado_plataforma,
    _herramienta,
    _numero,
    _pendientes,
    _suscripcion,
    _tenant,
    _vaciar_cola,
    _vigente,
    bd,
    requiere_bd,
)

BACKEND = Path(__file__).resolve().parent.parent


# ============================================================
# 1. PURAS
# ============================================================
def test_RF11_cada_motivo_de_la_cola_tiene_su_motivo_de_pausa_de_la_bitacora() -> None:
    # Los motivos con los que encolan los triggers de 59 (todos menos el que no pausa el cupo).
    assert MOTIVO_PAUSA == {
        "cambio_de_plan": "pausa_por_cambio_de_plan",
        "fin_de_suscripcion": "pausa_por_fin_de_suscripcion",
        "fin_de_prueba": "pausa_por_fin_de_prueba",
        "cupo_plataforma": "pausa_por_cupo_bajado_por_plataforma",
        "plan_mas_bajo": "pausa_por_plan_mas_bajo_recalculado",
        "pago_de_prueba": "pausa_por_pago_de_prueba",
        "reactivacion_negocio": "pausa_por_cambio_de_plan",
        "despliegue": "pausa_por_despliegue",
    }
    assert set(MOTIVO_PAUSA.values()) <= MOTIVOS_BITACORA
    assert set(MOTIVO_PAUSA.values()) <= set(numeros_avisos.MOTIVOS_PAUSA)


def test_RF11_el_motivo_sale_de_la_cola_y_el_que_pausa_el_cupo_gana_a_herramientas() -> None:
    assert elegir_motivo(["herramientas_del_plan", "cupo_plataforma"], "cambio_de_plan") == "cupo_plataforma"
    assert elegir_motivo(["plan_mas_bajo", "cupo_plataforma"], "cambio_de_plan") == "plan_mas_bajo"
    assert elegir_motivo(["herramientas_del_plan"], "cambio_de_plan") == "herramientas_del_plan"
    assert elegir_motivo([], "fin_de_prueba") == "fin_de_prueba"
    assert elegir_motivo(["motivo_desconocido"], "fin_de_suscripcion") == "fin_de_suscripcion"


async def test_RF11_un_motivo_desconocido_es_error_de_programacion() -> None:
    with pytest.raises(ValueError):
        await reevaluar_negocio(None, uuid4(), "inventado")  # type: ignore[arg-type]


def test_A33_la_pausa_por_cupo_no_aplica_a_un_cambio_de_herramientas() -> None:
    assert "herramientas_del_plan" not in MOTIVO_PAUSA


def test_RF14_RF2_las_funciones_de_mantenimiento_existen_con_la_conexion_como_primer_argumento() -> None:
    for nombre in ("aplicar_retencion", "limpiar_intentos", "drenar_cola", "reevaluar_vencidos"):
        funcion = getattr(numeros_reevaluacion, nombre)
        assert next(iter(inspect.signature(funcion).parameters)) == "conn"


# ------------------------------------------------------------
# Las vías de Python que cambian la suscripción llaman a la reevaluación
# ------------------------------------------------------------
@pytest.mark.parametrize(
    "archivo",
    ["services/pruebas.py", "services/pagos.py", "services/stripe_suscripciones.py", "jobs/pagos_background.py"],
)
def test_RF11_P6_las_vias_de_python_llaman_a_la_reevaluacion_tras_confirmar(archivo: str) -> None:
    fuente = (BACKEND / archivo).read_text(encoding="utf-8")
    assert "numeros_reevaluacion" in fuente
    assert "reevaluar_tras_cambio" in fuente


async def test_RF11_reevaluar_tras_cambio_nunca_propaga_un_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def explota(*a: Any, **k: Any) -> None:
        raise RuntimeError("todo roto")

    @asynccontextmanager
    async def falsa():
        yield object()

    monkeypatch.setattr(numeros_reevaluacion, "conexion", falsa)
    monkeypatch.setattr(numeros_reevaluacion, "reevaluar_negocio", explota)
    await numeros_reevaluacion.reevaluar_tras_cambio(uuid4(), "fin_de_prueba")  # no debe propagar


# ------------------------------------------------------------
# Trabajos y planificador
# ------------------------------------------------------------
def test_RF11_RF14_los_trabajos_quedan_registrados_con_sus_garantias(monkeypatch: pytest.MonkeyPatch) -> None:
    from apscheduler.triggers.cron import CronTrigger

    from config import settings
    from jobs import alertas_background, numeros_background as nb

    monkeypatch.setattr(alertas_background.AsyncIOScheduler, "start", lambda self: None)
    scheduler = alertas_background.iniciar_scheduler()

    reevaluacion = scheduler.get_job(nb.JOB_ID_REEVALUACION)
    assert reevaluacion is not None and reevaluacion.func is nb.job_numeros_reevaluacion
    assert reevaluacion.trigger.interval == timedelta(minutes=settings.NUMEROS_REEVALUACION_INTERVALO_MINUTOS)
    assert settings.NUMEROS_REEVALUACION_INTERVALO_MINUTOS == 1

    intentos = scheduler.get_job(nb.JOB_ID_INTENTOS)
    assert intentos is not None and intentos.func is nb.job_limpiar_intentos
    assert intentos.trigger.interval == timedelta(hours=1)

    retencion = scheduler.get_job(nb.JOB_ID_RETENCION)
    assert retencion is not None and retencion.func is nb.job_retencion_ocultas
    assert isinstance(retencion.trigger, CronTrigger)

    for trabajo in (reevaluacion, intentos, retencion):
        assert trabajo.max_instances == 1
        assert trabajo.coalesce is True


def _conexion_falsa(monkeypatch: pytest.MonkeyPatch) -> None:
    from jobs import numeros_background as nb

    @asynccontextmanager
    async def falsa():
        yield object()

    monkeypatch.setattr(nb, "conexion", falsa)


async def test_RF11_los_trabajos_no_propagan_un_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from jobs import numeros_background as nb

    async def explota(*a: Any, **k: Any) -> None:
        raise RuntimeError("todo roto")

    _conexion_falsa(monkeypatch)
    for nombre in ("drenar_cola", "reevaluar_vencidos", "ejecutar_despliegue_una_vez", "limpiar_intentos", "aplicar_retencion"):
        monkeypatch.setattr(numeros_reevaluacion, nombre, explota)
    monkeypatch.setattr(nb, "_despliegue_hecho", False)
    await nb.job_numeros_reevaluacion()
    await nb.job_limpiar_intentos()
    await nb.job_retencion_ocultas()


async def test_RF22_P19_el_despliegue_se_lanza_en_la_primera_pasada_y_no_se_repite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jobs import numeros_background as nb

    llamadas: list[str] = []

    async def despliegue(conn: Any, **k: Any) -> None:
        llamadas.append("despliegue")

    async def cola(conn: Any, **k: Any) -> int:
        llamadas.append("cola")
        return 0

    async def vencidos(conn: Any, **k: Any) -> int:
        llamadas.append("vencidos")
        return 0

    _conexion_falsa(monkeypatch)
    monkeypatch.setattr(numeros_reevaluacion, "ejecutar_despliegue_una_vez", despliegue)
    monkeypatch.setattr(numeros_reevaluacion, "drenar_cola", cola)
    monkeypatch.setattr(numeros_reevaluacion, "reevaluar_vencidos", vencidos)
    monkeypatch.setattr(nb, "_despliegue_hecho", False)

    await nb.job_numeros_reevaluacion()
    await nb.job_numeros_reevaluacion()

    assert llamadas == ["despliegue", "cola", "vencidos", "cola", "vencidos"]


async def test_RF22_si_el_despliegue_falla_se_reintenta_en_la_siguiente_pasada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jobs import numeros_background as nb

    intentos: list[int] = []

    async def despliegue(conn: Any, **k: Any) -> None:
        intentos.append(1)
        if len(intentos) == 1:
            raise RuntimeError("falla la primera vez")

    async def nada(conn: Any, **k: Any) -> int:
        return 0

    _conexion_falsa(monkeypatch)
    monkeypatch.setattr(numeros_reevaluacion, "ejecutar_despliegue_una_vez", despliegue)
    monkeypatch.setattr(numeros_reevaluacion, "drenar_cola", nada)
    monkeypatch.setattr(numeros_reevaluacion, "reevaluar_vencidos", nada)
    monkeypatch.setattr(nb, "_despliegue_hecho", False)

    await nb.job_numeros_reevaluacion()
    await nb.job_numeros_reevaluacion()
    await nb.job_numeros_reevaluacion()

    assert len(intentos) == 2  # reintentó una vez y, ya hecho, no volvió a llamar


# ------------------------------------------------------------
# Disparo inmediato desde Python (caso 3 y prueba pausada o cancelada)
# ------------------------------------------------------------
@pytest.fixture
def llamadas_de_reevaluacion(monkeypatch: pytest.MonkeyPatch) -> list[tuple[UUID, str]]:
    llamadas: list[tuple[UUID, str]] = []

    async def espia(tenant_id: UUID, motivo: str = "cambio_de_plan", **_: Any) -> None:
        llamadas.append((tenant_id, motivo))

    monkeypatch.setattr(numeros_reevaluacion, "reevaluar_tras_cambio", espia)
    return llamadas


async def test_A31_A75_revocar_una_prueba_reevalua_en_la_misma_peticion(db, llamadas_de_reevaluacion) -> None:
    from services.pruebas import revocar_prueba
    from session import execute

    tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Test')", tid)
    try:
        await execute(
            """
            INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly, origen, fecha_renovacion)
            VALUES ($1, 'pro', 'activa', 0, 'prueba', NOW() + INTERVAL '10 days')
            """,
            tid,
        )
        await revocar_prueba(tid, "fin de la prueba", gerente_email="g@x.test", gerente_id=uuid4())
        assert llamadas_de_reevaluacion == [(tid, "fin_de_prueba")]
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", tid)


async def test_A31_otorgar_una_prueba_reevalua_sus_limites(db, llamadas_de_reevaluacion) -> None:
    from schemas import OtorgarPruebaIn
    from services.pruebas import otorgar_prueba
    from session import execute

    tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Test')", tid)
    try:
        await otorgar_prueba(
            tid,
            OtorgarPruebaIn(plan="pro", unidad="dias", cantidad=5, motivo="prueba de T8"),
            gerente_email="g@x.test",
            gerente_id=uuid4(),
        )
        assert [t for t, _ in llamadas_de_reevaluacion] == [tid]
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", tid)


async def test_A34_activar_una_suscripcion_reevalua_tras_confirmar(db, llamadas_de_reevaluacion) -> None:
    from services.pagos import activar_suscripcion
    from session import execute

    tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Test')", tid)
    try:
        assert await activar_suscripcion(tid, "pro") is True
        assert [t for t, _ in llamadas_de_reevaluacion] == [tid]
        # Un plan inexistente no activa nada, así que no hay nada que reevaluar.
        llamadas_de_reevaluacion.clear()
        assert await activar_suscripcion(tid, "plan_que_no_existe") is False
        assert llamadas_de_reevaluacion == []
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", tid)


async def test_A30_el_job_de_pagos_reevalua_las_suscripciones_que_acaba_de_pausar(
    db, llamadas_de_reevaluacion
) -> None:
    from jobs.pagos_background import job_pausar_suscripciones_vencidas
    from session import execute

    tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Test')", tid)
    try:
        await execute(
            """
            INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly, fecha_renovacion)
            VALUES ($1, 'pro', 'activa', 99, NOW() - INTERVAL '1 day')
            """,
            tid,
        )
        await job_pausar_suscripciones_vencidas()
        assert (tid, "fin_de_suscripcion") in llamadas_de_reevaluacion
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", tid)


# ============================================================
# 2. CON BASE DE DATOS DE PRUEBA
# ============================================================
async def _estados(c, tenant: UUID) -> dict[UUID, str]:
    filas = await c.fetch("SELECT id, estado FROM agentes_canal WHERE tenant_id = $1", tenant)
    return {f["id"]: f["estado"] for f in filas}


async def _plan_nuevo(c, *, tope: int | None, familias=FAMILIAS_HERRAMIENTAS) -> str:
    """Un plan propio inactivo (no mueve el catálogo ni el plan más bajo de nadie)."""
    nombre = f"plan-t8-{uuid4().hex[:10]}"
    await c.execute(
        "INSERT INTO planes (nombre, precio_monthly, activo, max_numeros_whatsapp) VALUES ($1, 9999, false, $2)",
        nombre, tope,
    )
    for familia in familias:
        await c.execute("INSERT INTO plan_herramientas (plan_nombre, familia) VALUES ($1, $2)", nombre, familia)
    return nombre


async def _tres_numeros(c, t: UUID) -> tuple[UUID, UUID, UUID]:
    """Del más antiguo al más reciente; el más antiguo es el principal."""
    a = await _numero(c, t, principal=True, dias=30)
    b = await _numero(c, t, dias=20)
    cc = await _numero(c, t, dias=10)
    return a, b, cc


def _avisos_de(avisos, tenant: UUID, tipo: str):
    return [a for a in avisos if a.tenant == tenant and a.aviso.tipo == tipo]


async def _limpiar_despliegue(c) -> None:
    await c.execute("DELETE FROM numeros_despliegue WHERE clave = $1", numeros_reevaluacion.CLAVE_DESPLIEGUE)


# ------------------------------------------------------------
# Caso 1: cambio de plan a menos cupo (A29)
# ------------------------------------------------------------
@requiere_bd
async def test_A29_CL1_de_tres_a_uno_pausa_los_dos_mas_recientes_con_un_solo_aviso(bds, avisos) -> None:
    c = bds
    t = await _negocio(c, tope=3)
    await _cuenta_portal(c, t, "owner")
    a, b, cc = await _tres_numeros(c, t)
    await c.execute("UPDATE tenant_subscriptions SET plan = $2 WHERE tenant_id = $1", t, await _plan_nuevo(c, tope=1))

    r = await reevaluar_negocio(c, t, "cambio_de_plan")

    assert await _estados(c, t) == {a: "activo", b: "pausado", cc: "pausado"}
    assert {p["id"] for p in r.pausados} == {b, cc}
    pausa = await _bitacora(c, t, "pausa_por_cambio_de_plan")
    assert {e["agente_canal_id"] for e in pausa} == {b, cc}
    assert all(e["autor_tipo"] == "sistema" for e in pausa)
    fila = await _fila(c, cc)
    assert fila["motivo_pausa"] == "pausa_por_cambio_de_plan" and fila["pausado_en"] is not None
    # Un solo aviso agrupado, con los dos números y su estado, y el principal sigue siendo el más antiguo.
    pausados = _avisos_de(avisos, t, "numeros_pausados")
    assert len(pausados) == 1 and len(pausados[0].aviso.datos["numeros"]) == 2
    assert {n["estado"] for n in pausados[0].aviso.datos["numeros"]} == {"pausado"}
    assert await c.fetchval("SELECT id FROM agentes_canal WHERE tenant_id = $1 AND es_principal", t) == a


@requiere_bd
async def test_A29_si_el_principal_es_de_los_mas_recientes_se_pausa_y_lo_sustituye_el_mas_antiguo(bds, avisos) -> None:
    c = bds
    t = await _negocio(c, tope=3)
    await _cuenta_portal(c, t, "owner")
    viejo = await _numero(c, t, dias=30)
    principal = await _numero(c, t, principal=True, dias=5)
    await c.execute("UPDATE tenant_subscriptions SET plan = $2 WHERE tenant_id = $1", t, await _plan_nuevo(c, tope=1))

    await reevaluar_negocio(c, t, "cambio_de_plan")

    assert (await _estados(c, t))[principal] == "pausado"
    assert await c.fetchval("SELECT id FROM agentes_canal WHERE tenant_id = $1 AND es_principal", t) == viejo
    cambio = (await _bitacora(c, t, "cambio_de_principal"))[0]
    assert cambio["agente_canal_id"] == viejo


@requiere_bd
async def test_A49_la_linea_de_avisos_no_cuenta_para_el_cupo_ni_se_pausa(bds, avisos) -> None:
    c = bds
    t = await _negocio(c, tope=1)
    await _cuenta_portal(c, t, "owner")
    linea = await _numero(c, t, "pn-linea", avisos=True, dias=40)
    a = await _numero(c, t, principal=True, dias=30)
    b = await _numero(c, t, dias=10)

    await reevaluar_negocio(c, t, "cupo_plataforma")

    assert await _estados(c, t) == {linea: "activo", a: "activo", b: "pausado"}


@requiere_bd
async def test_A37_subir_el_cupo_no_reactiva_ningun_numero(bds, avisos) -> None:
    c = bds
    t = await _negocio(c, tope=1)
    await _cuenta_portal(c, t, "owner")
    a, b, cc = await _tres_numeros(c, t)
    await reevaluar_negocio(c, t, "cupo_plataforma")
    plan = await _plan_de(c, t)

    await c.execute("UPDATE planes SET max_numeros_whatsapp = 10 WHERE nombre = $1", plan)
    await drenar_cola(c)

    assert await _estados(c, t) == {a: "activo", b: "pausado", cc: "pausado"}


# ------------------------------------------------------------
# Caso 2: fin de suscripción (A30)
# ------------------------------------------------------------
@requiere_bd
async def test_A30_una_cancelacion_programada_mantiene_el_cupo_hasta_el_fin_y_al_vencer_aplica_el_plan_mas_bajo(
    bds, avisos
) -> None:
    c = bds
    await _catalogo(c, PLANES)  # starter (el más bajo) tiene cupo 1; pro tiene 3
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    await _suscripcion(c, t, "pro", "activa", "pago", _vigente())
    a, b, cc = await _tres_numeros(c, t)

    # Cancelación programada: sigue vigente, el cupo del plan contratado se mantiene.
    await c.execute("UPDATE tenant_subscriptions SET cancela_al_vencer = true WHERE tenant_id = $1", t)
    await drenar_cola(c)
    assert set((await _estados(c, t)).values()) == {"activo"}

    # El periodo se acaba SIN que nadie escriba la suscripción (el reloj pasa): solo el trabajo lo ve.
    await c.execute("ALTER TABLE tenant_subscriptions DISABLE TRIGGER USER")
    await c.execute(
        "UPDATE tenant_subscriptions SET fecha_renovacion = NOW() - INTERVAL '1 minute' WHERE tenant_id = $1", t
    )
    await c.execute("ALTER TABLE tenant_subscriptions ENABLE TRIGGER USER")
    await _vaciar_cola(c, t)
    assert await _pendientes(c, t) == []

    assert await reevaluar_vencidos(c) >= 1

    assert await _estados(c, t) == {a: "activo", b: "pausado", cc: "pausado"}
    assert len(await _bitacora(c, t, "pausa_por_fin_de_suscripcion")) == 2
    assert len(_avisos_de(avisos, t, "numeros_pausados")) == 1

    # Idempotente: otra pasada no repite la bitácora ni el aviso.
    await reevaluar_vencidos(c)
    assert len(await _bitacora(c, t, "pausa_por_fin_de_suscripcion")) == 2
    assert len(_avisos_de(avisos, t, "numeros_pausados")) == 1


@requiere_bd
async def test_A30_una_suscripcion_pausada_se_reevalua_en_el_acto_por_la_cola(bds, avisos) -> None:
    c = bds
    await _catalogo(c, PLANES)
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    await _suscripcion(c, t, "pro", "activa", "pago", _vigente())
    a, b, cc = await _tres_numeros(c, t)
    await _vaciar_cola(c, t)

    await c.execute("UPDATE tenant_subscriptions SET estado = 'pausada' WHERE tenant_id = $1", t)
    assert await _pendientes(c, t) == ["fin_de_suscripcion"]
    await drenar_cola(c)

    assert await _estados(c, t) == {a: "activo", b: "pausado", cc: "pausado"}
    assert len(await _bitacora(c, t, "pausa_por_fin_de_suscripcion")) == 2


# ------------------------------------------------------------
# Caso 3: prueba terminada sin pago (A31, A75)
# ------------------------------------------------------------
@requiere_bd
@pytest.mark.parametrize("estado", ["pausada", "cancelada"])
async def test_A31_A75_una_prueba_pausada_o_cancelada_aplica_el_plan_mas_bajo_por_la_cola(
    bds, avisos, estado: str
) -> None:
    c = bds
    await _catalogo(c, PLANES)
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    await _suscripcion(c, t, "pro", "activa", "prueba", _vigente())
    a, b, cc = await _tres_numeros(c, t)
    assert set((await _estados(c, t)).values()) == {"activo"}  # con prueba vigente no hay tope
    await _vaciar_cola(c, t)

    await c.execute("UPDATE tenant_subscriptions SET estado = $2 WHERE tenant_id = $1", t, estado)
    await drenar_cola(c)

    assert await _estados(c, t) == {a: "activo", b: "pausado", cc: "pausado"}
    assert len(await _bitacora(c, t, "pausa_por_fin_de_prueba")) == 2


@requiere_bd
async def test_A31_el_piloto_que_termina_aplica_el_plan_mas_bajo(bds, avisos) -> None:
    c = bds
    await _catalogo(c, PLANES)
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    await _estado_plataforma(c, t, "prueba")
    a, b, cc = await _tres_numeros(c, t)
    await _vaciar_cola(c, t)

    await c.execute("UPDATE tenant_estado_plataforma SET estado = 'activo' WHERE tenant_id = $1", t)
    await drenar_cola(c)

    assert await _estados(c, t) == {a: "activo", b: "pausado", cc: "pausado"}
    assert len(await _bitacora(c, t, "pausa_por_fin_de_prueba")) == 2


@requiere_bd
async def test_A31_la_reevaluacion_directa_deja_la_cola_procesada_y_la_pasada_siguiente_no_repite(bds, avisos) -> None:
    c = bds
    await _catalogo(c, PLANES)
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    await _suscripcion(c, t, "pro", "activa", "prueba", _vigente())
    await _tres_numeros(c, t)
    await _vaciar_cola(c, t)
    await c.execute("UPDATE tenant_subscriptions SET estado = 'cancelada' WHERE tenant_id = $1", t)
    assert await _pendientes(c, t) == ["fin_de_prueba"]

    await reevaluar_negocio(c, t, None, por_defecto="fin_de_prueba")  # la vía de Python, en la misma petición

    assert await _pendientes(c, t) == []
    assert len(_avisos_de(avisos, t, "numeros_pausados")) == 1
    await drenar_cola(c)  # la pasada del trabajo ya no encuentra nada de este negocio
    assert len(_avisos_de(avisos, t, "numeros_pausados")) == 1
    assert len(await _bitacora(c, t, "pausa_por_fin_de_prueba")) == 2


# ------------------------------------------------------------
# Caso 4: la plataforma cambia el cupo del plan contratado (A32)
# ------------------------------------------------------------
@requiere_bd
@pytest.mark.parametrize("antes", [None, 3], ids=["de_sin_tope_a_entero", "de_3_a_1"])
async def test_A32_CL1_un_cambio_de_cupo_del_plan_contratado_pausa_a_los_mas_recientes(
    bds, avisos, antes: int | None
) -> None:
    c = bds
    t = await _negocio(c, tope=antes)
    await _cuenta_portal(c, t, "owner")
    a, b, cc = await _tres_numeros(c, t)
    await _vaciar_cola(c, t)
    plan = await _plan_de(c, t)

    await c.execute("UPDATE planes SET max_numeros_whatsapp = 1 WHERE nombre = $1", plan)
    assert "cupo_plataforma" in await _pendientes(c, t)
    await drenar_cola(c)

    assert await _estados(c, t) == {a: "activo", b: "pausado", cc: "pausado"}
    assert len(await _bitacora(c, t, "pausa_por_cupo_bajado_por_plataforma")) == 2
    assert len(_avisos_de(avisos, t, "numeros_pausados")) == 1


# ------------------------------------------------------------
# Caso 5: cambia el plan más bajo activo (A33)
# ------------------------------------------------------------
async def _negocio_sin_suscripcion(c, avisos, *, tope_bajo: int | None):
    """Catálogo de un solo plan activo (`basico`) y un negocio sin suscripción con tres números."""
    await _catalogo(c, [_plan("basico", 10, tope=tope_bajo)])
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    a, b, cc = await _tres_numeros(c, t)
    await _vaciar_cola(c, t)
    return t, a, b, cc


@requiere_bd
async def test_A33_cambia_el_cupo_del_plan_mas_bajo(bds, avisos) -> None:
    c = bds
    t, a, b, cc = await _negocio_sin_suscripcion(c, avisos, tope_bajo=None)
    await c.execute("UPDATE planes SET max_numeros_whatsapp = 1 WHERE nombre = 'basico'")
    assert await _pendientes(c, t) == ["plan_mas_bajo"]
    await drenar_cola(c)
    assert await _estados(c, t) == {a: "activo", b: "pausado", cc: "pausado"}
    assert len(await _bitacora(c, t, "pausa_por_plan_mas_bajo_recalculado")) == 2


@requiere_bd
async def test_A33_un_plan_nuevo_mas_barato_pasa_a_ser_el_mas_bajo(bds, avisos) -> None:
    c = bds
    t, a, b, cc = await _negocio_sin_suscripcion(c, avisos, tope_bajo=None)
    await c.execute("INSERT INTO planes (nombre, precio_monthly, activo, max_numeros_whatsapp) VALUES ('barato', 1, true, 2)")
    await drenar_cola(c)
    assert await _estados(c, t) == {a: "activo", b: "activo", cc: "pausado"}


@requiere_bd
async def test_A33_desactivar_el_plan_mas_bajo_aplica_el_siguiente(bds, avisos) -> None:
    c = bds
    await _catalogo(c, [_plan("barato", 5, tope=None), _plan("caro", 50, tope=1)])
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    a, b, cc = await _tres_numeros(c, t)
    await _vaciar_cola(c, t)

    await c.execute("UPDATE planes SET activo = false WHERE nombre = 'barato'")
    await drenar_cola(c)

    assert await _estados(c, t) == {a: "activo", b: "pausado", cc: "pausado"}


@requiere_bd
async def test_A33_un_cambio_de_precio_que_cambia_cual_es_el_mas_bajo_recalcula_el_cupo(bds, avisos) -> None:
    c = bds
    await _catalogo(c, [_plan("a", 10, tope=None), _plan("b", 20, tope=1)])
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    a, b, cc = await _tres_numeros(c, t)
    await _vaciar_cola(c, t)

    await c.execute("UPDATE planes SET precio_monthly = 5 WHERE nombre = 'b'")  # b pasa a ser el más bajo
    await drenar_cola(c)

    assert await _estados(c, t) == {a: "activo", b: "pausado", cc: "pausado"}


@requiere_bd
async def test_A33_un_cambio_de_limites_o_de_herramientas_no_toca_el_cupo(bds, avisos) -> None:
    c = bds
    # Con cupo 1 y tres números: el cupo ya se había excedido y nadie lo reevaluó.
    t, a, b, cc = await _negocio_sin_suscripcion(c, avisos, tope_bajo=1)
    await _herramienta(c, t, "hoja", "google_sheets")
    await c.execute("INSERT INTO agente_canal_herramientas (agente_canal_id, tenant_id, tool_key) VALUES ($1, $2, 'hoja')", b, t)
    await c.execute("INSERT INTO social_herramientas (tenant_id, tool_key) VALUES ($1, 'hoja')", t)
    await _vaciar_cola(c, t)

    # basico pierde la familia google_sheets: cambia la lista, no el cupo.
    await c.execute("DELETE FROM plan_herramientas WHERE plan_nombre = 'basico' AND familia = 'google_sheets'")
    assert await _pendientes(c, t) == ["herramientas_del_plan"]
    await drenar_cola(c)

    assert set((await _estados(c, t)).values()) == {"activo"}  # no se pausó nada
    assert await c.fetchval("SELECT count(*) FROM agente_canal_herramientas WHERE tenant_id = $1", t) == 0
    assert await c.fetchval("SELECT count(*) FROM social_herramientas WHERE tenant_id = $1", t) == 0
    retiradas = await _bitacora(c, t, "herramienta_retirada_por_plan")
    assert {e["agente_canal_id"] for e in retiradas} == {b, None}


@requiere_bd
async def test_A33_un_negocio_con_suscripcion_vigente_no_se_reevalua_por_el_plan_mas_bajo(bds, avisos) -> None:
    c = bds
    await _catalogo(c, [_plan("basico", 10, tope=None), _plan("pro", 50, tope=5)])
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    await _suscripcion(c, t, "pro", "activa", "pago", _vigente())
    await _tres_numeros(c, t)
    await _vaciar_cola(c, t)

    await c.execute("UPDATE planes SET max_numeros_whatsapp = 1 WHERE nombre = 'basico'")
    await drenar_cola(c)

    assert set((await _estados(c, t)).values()) == {"activo"}


# ------------------------------------------------------------
# Caso 6: prueba que pasa a plan de pago (A34)
# ------------------------------------------------------------
@requiere_bd
async def test_A34_CL9_una_prueba_que_pasa_a_pago_aplica_el_cupo_del_plan_contratado(bds, avisos) -> None:
    c = bds
    await _catalogo(c, PLANES)
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    await _suscripcion(c, t, "pro", "activa", "prueba", _vigente())
    a, b, cc = await _tres_numeros(c, t)
    await _vaciar_cola(c, t)

    await c.execute(
        "UPDATE tenant_subscriptions SET origen = 'pago', plan = 'starter', fecha_renovacion = $2 WHERE tenant_id = $1",
        t, _vigente(),
    )
    assert await _pendientes(c, t) == ["pago_de_prueba"]
    await drenar_cola(c)

    assert await _estados(c, t) == {a: "activo", b: "pausado", cc: "pausado"}
    assert len(await _bitacora(c, t, "pausa_por_pago_de_prueba")) == 2


# ------------------------------------------------------------
# Caso 7: despliegue (A35, RF-22)
# ------------------------------------------------------------
@requiere_bd
async def test_A35_el_despliegue_pausa_solo_si_el_plan_mas_bajo_tiene_tope(bds, avisos) -> None:
    c = bds
    await _limpiar_despliegue(c)
    await _catalogo(c, [_plan("basico", 10, tope=1)])
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    a, b, cc = await _tres_numeros(c, t)

    resultado = await ejecutar_despliegue_una_vez(c)

    assert resultado is not None
    assert await _estados(c, t) == {a: "activo", b: "pausado", cc: "pausado"}
    pausas = await _bitacora(c, t, "pausa_por_despliegue")
    assert len(pausas) == 2 and all(e["autor_tipo"] == "sistema" for e in pausas)
    assert len(_avisos_de(avisos, t, "numeros_pausados")) == 1


@requiere_bd
async def test_A35_el_despliegue_no_pausa_si_el_plan_mas_bajo_no_tiene_tope(bds, avisos) -> None:
    c = bds
    await _limpiar_despliegue(c)
    await _catalogo(c, [_plan("basico", 10, tope=None)])
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    await _tres_numeros(c, t)

    await ejecutar_despliegue_una_vez(c)

    assert set((await _estados(c, t)).values()) == {"activo"}
    assert await _bitacora(c, t, "pausa_por_despliegue") == []


@requiere_bd
async def test_A35_el_despliegue_no_cambia_a_los_negocios_con_suscripcion_vigente(bds, avisos) -> None:
    c = bds
    await _limpiar_despliegue(c)
    await _catalogo(c, [_plan("basico", 10, tope=1), _plan("pro", 50, tope=1)])
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    await _suscripcion(c, t, "pro", "activa", "pago", _vigente())
    await _tres_numeros(c, t)

    await ejecutar_despliegue_una_vez(c)

    assert set((await _estados(c, t)).values()) == {"activo"}


@requiere_bd
async def test_A35_el_despliegue_es_idempotente_y_no_repite_pausas_ni_avisos(bds, avisos) -> None:
    c = bds
    await _limpiar_despliegue(c)
    await _catalogo(c, [_plan("basico", 10, tope=1)])
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    a, b, cc = await _tres_numeros(c, t)

    assert await ejecutar_despliegue_una_vez(c) is not None
    assert await c.fetchval("SELECT count(*) FROM numeros_despliegue WHERE clave = $1", numeros_reevaluacion.CLAVE_DESPLIEGUE) == 1
    # Un humano reactiva uno (sube el cupo); una segunda ejecución no vuelve a pausarlo ni a avisar.
    await c.execute("UPDATE planes SET max_numeros_whatsapp = 2 WHERE nombre = 'basico'")
    await c.execute("UPDATE agentes_canal SET estado = 'activo', motivo_pausa = NULL, pausado_en = NULL WHERE id = $1", b)
    avisos.clear()

    assert await ejecutar_despliegue_una_vez(c) is None

    assert (await _estados(c, t))[b] == "activo"
    assert avisos == []
    assert len(await _bitacora(c, t, "pausa_por_despliegue")) == 2


@requiere_bd
async def test_A71_el_despliegue_aplica_los_limites_del_plan_asociado_a_una_prueba(bds, avisos) -> None:
    c = bds
    await _limpiar_despliegue(c)
    await _catalogo(c, [_plan("basico", 10, tope=None, historial=20)])
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    await _suscripcion(c, t, "basico", "activa", "prueba", _vigente())
    n = await _numero(c, t, principal=True, historial=150)

    await ejecutar_despliegue_una_vez(c)

    assert (await _fila(c, n))["history_window"] == 20
    assert (await _fila(c, n))["estado"] == "activo"  # una prueba no tiene tope
    assert len(await _bitacora(c, t, "recorte_por_plan")) == 1
    recortados = _avisos_de(avisos, t, "numeros_recortados")
    assert len(recortados) == 1


# ------------------------------------------------------------
# Suspendidos, sin plan activo (P8, CL-20, A36)
# ------------------------------------------------------------
@requiere_bd
@pytest.mark.parametrize("estado", ["suspendido", "baja"])
async def test_P8_un_negocio_suspendido_o_de_baja_no_se_reevalua(bds, avisos, estado: str) -> None:
    c = bds
    t, a, b, cc = await _negocio_sin_suscripcion(c, avisos, tope_bajo=1)
    await _estado_plataforma(c, t, estado)

    r = await reevaluar_negocio(c, t, "plan_mas_bajo")

    assert r.omitido is True and r.pausados == []
    assert set((await _estados(c, t)).values()) == {"activo"}
    assert avisos == []


@requiere_bd
async def test_P8_al_reactivarse_un_negocio_suspendido_se_reevalua(bds, avisos) -> None:
    c = bds
    await _catalogo(c, [_plan("basico", 10, tope=1)])
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    await _estado_plataforma(c, t, "suspendido")
    a, b, cc = await _tres_numeros(c, t)
    await _vaciar_cola(c, t)

    await c.execute("UPDATE tenant_estado_plataforma SET estado = 'activo' WHERE tenant_id = $1", t)
    assert await _pendientes(c, t) == ["reactivacion_negocio"]
    await drenar_cola(c)

    assert await _estados(c, t) == {a: "activo", b: "pausado", cc: "pausado"}


@requiere_bd
async def test_A36_CL20_sin_ningun_plan_activo_no_se_pausa_nada_y_queda_registrado(bds, avisos) -> None:
    c = bds
    await _catalogo(c, [])
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    await _tres_numeros(c, t)

    r = await reevaluar_negocio(c, t, "plan_mas_bajo")

    assert r.sin_plan_activo is True and r.pausados == []
    assert set((await _estados(c, t)).values()) == {"activo"}
    registros = await _auditoria(c, t, "plan_sin_tope_por_falta_de_plan_activo")
    assert len(registros) == 1 and registros[0]["detalle"]["motivo"] == "plan_mas_bajo"
    # Reevaluar otra vez no llena la bitácora de la plataforma.
    await reevaluar_negocio(c, t, "plan_mas_bajo")
    assert len(await _auditoria(c, t, "plan_sin_tope_por_falta_de_plan_activo")) == 1


# ------------------------------------------------------------
# Recorte de límites y aviso (A26, A57, CL-13)
# ------------------------------------------------------------
@requiere_bd
async def test_A26_A57_CL13_el_recorte_hecho_por_un_trigger_tambien_deja_bitacora_y_aviso(bds, avisos) -> None:
    c = bds
    await _catalogo(c, [_plan("basico", 10, tope=None, historial=20)])
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    activo = await _numero(c, t, principal=True, historial=100, dias=10)
    # Con el trigger activo, escribir la suscripción recorta ANTES de que la reevaluación mire.
    await _suscripcion(c, t, "basico", "activa", "pago", _vigente())
    assert (await _fila(c, activo))["history_window"] == 20

    r = await reevaluar_negocio(c, t, "cambio_de_plan")

    assert {x["id"] for x in r.recortados} == {activo}
    entradas = await _bitacora(c, t, "recorte_por_plan")
    assert [e["agente_canal_id"] for e in entradas] == [activo]
    assert len(_avisos_de(avisos, t, "numeros_recortados")) == 1
    assert await c.fetchval("SELECT count(*) FROM numeros_recortes_pendientes WHERE tenant_id = $1", t) == 0

    # Idempotente: una segunda reevaluación no repite bitácora ni aviso.
    avisos.clear()
    await reevaluar_negocio(c, t, "cambio_de_plan")
    assert len(await _bitacora(c, t, "recorte_por_plan")) == 1
    assert _avisos_de(avisos, t, "numeros_recortados") == []


@requiere_bd
async def test_A26_A57_CL13_el_recorte_hecho_por_la_reevaluacion_avisa_con_los_numeros_incluidos_los_pausados(
    bds, avisos
) -> None:
    c = bds
    await _catalogo(c, [_plan("basico", 10, tope=None, historial=20)])
    t = await _tenant(c)
    await _cuenta_portal(c, t, "owner")
    activo = await _numero(c, t, principal=True, historial=100, dias=10)
    pausado = await _numero(c, t, estado="pausado", historial=90, dias=5)
    # La suscripción vencida por el reloj (sin escritura): el trigger no recortó, la reevaluación sí.
    await c.execute("ALTER TABLE tenant_subscriptions DISABLE TRIGGER USER")
    await _suscripcion(c, t, "basico", "activa", "pago", datetime.now(timezone.utc) - timedelta(minutes=1))
    await c.execute("ALTER TABLE tenant_subscriptions ENABLE TRIGGER USER")

    r = await reevaluar_negocio(c, t, "fin_de_suscripcion")

    assert (await _fila(c, activo))["history_window"] == 20 and (await _fila(c, pausado))["history_window"] == 20
    assert {x["id"] for x in r.recortados} == {activo, pausado}
    assert {e["agente_canal_id"] for e in await _bitacora(c, t, "recorte_por_plan")} == {activo, pausado}
    aviso = _avisos_de(avisos, t, "numeros_recortados")
    assert len(aviso) == 1
    assert {n["estado"] for n in aviso[0].aviso.datos["numeros"]} == {"activo", "pausado"}


# ------------------------------------------------------------
# Número incompleto (A21, A63, A73)
# ------------------------------------------------------------
@requiere_bd
async def test_CL10_un_cambio_de_plan_que_mantiene_un_numero_incompleto_avisa_de_nuevo(bds, avisos) -> None:
    c = bds
    t = await _negocio(c, tope=5)
    await _cuenta_portal(c, t, "owner")
    incompleto = await _numero(c, t, nombre="", prompt="", dias=10)
    await _numero(c, t, principal=True, dias=20)
    pausado_incompleto = await _numero(c, t, estado="pausado", nombre="", prompt="", dias=5)
    await c.execute("UPDATE tenant_subscriptions SET plan = $2 WHERE tenant_id = $1", t, await _plan_nuevo(c, tope=5))

    await reevaluar_negocio(c, t, "cambio_de_plan")

    avisados = _avisos_de(avisos, t, "numero_configuracion_incompleta")
    assert len(avisados) == 1
    ids = {n["id"] for n in avisados[0].aviso.datos["numeros"]}
    assert ids == {str(incompleto)} and str(pausado_incompleto) not in ids
    assert "plan" in avisados[0].aviso.mensaje.lower()


@requiere_bd
async def test_CL10_un_cambio_del_plan_mas_bajo_no_avisa_de_numeros_incompletos(bds, avisos) -> None:
    c = bds
    t, a, b, cc = await _negocio_sin_suscripcion(c, avisos, tope_bajo=None)
    await c.execute("UPDATE agentes_canal SET agent_name = '' WHERE id = $1", b)
    await reevaluar_negocio(c, t, "plan_mas_bajo")
    assert _avisos_de(avisos, t, "numero_configuracion_incompleta") == []


# ------------------------------------------------------------
# Despliegue: configuración incompleta (A63, A73)
# ------------------------------------------------------------
@requiere_bd
async def test_A63_A73_el_despliegue_evalua_todos_los_numeros_la_linea_de_avisos_y_instagram_facebook(bds, avisos) -> None:
    c = bds
    await _limpiar_despliegue(c)
    await _catalogo(c, [_plan("basico", 10, tope=None)])

    # Negocio A: un número completo, uno incompleto, la línea de avisos incompleta e Instagram sin configurar.
    a = await _tenant(c)
    await _cuenta_portal(c, a, "owner")
    await _cuenta_portal(c, a, "member")
    await _numero(c, a, principal=True, dias=30)
    incompleto = await _numero(c, a, nombre="Ana", prompt="   ", dias=20)
    linea = await _numero(c, a, "pn-linea-t8", avisos=True, nombre="", prompt="", dias=40)
    await c.execute(
        "INSERT INTO channel_credentials (tenant_id, channel_type, page_id, is_active) VALUES ($1, 'instagram', 'pg-1', true)", a
    )
    # Negocio B: todo completo, Instagram con configuración completa: no recibe nada.
    b = await _tenant(c)
    await _cuenta_portal(c, b, "owner")
    await _numero(c, b, principal=True)
    await c.execute(
        "INSERT INTO channel_credentials (tenant_id, channel_type, page_id, is_active) VALUES ($1, 'facebook', 'pg-2', true)", b
    )
    await c.execute("INSERT INTO tenant_agent_config (tenant_id, agent_name, system_prompt) VALUES ($1, 'Bea', 'Hola')", b)

    await ejecutar_despliegue_una_vez(c)

    delta = _avisos_de(avisos, a, "numero_configuracion_incompleta")
    assert len(delta) == 1  # un solo aviso agrupado (a la cuenta owner; el member no recibe)
    etiquetas = {n["id"] for n in delta[0].aviso.datos["numeros"]}
    assert {str(incompleto), str(linea)} <= etiquetas
    assert any("Instagram" in n["etiqueta"] for n in delta[0].aviso.datos["numeros"])
    entradas = await _bitacora(c, a, "configuracion_incompleta")
    assert {e["agente_canal_id"] for e in entradas} == {incompleto, linea, None}
    assert all(e["detalle"]["origen"] == "despliegue" for e in entradas)
    # Ninguno pasa a principal ni se pausa; el negocio B no recibe nada.
    assert await c.fetchval("SELECT count(*) FROM agentes_canal WHERE tenant_id = $1 AND es_principal", a) == 1
    assert _avisos_de(avisos, b, "numero_configuracion_incompleta") == []


# ------------------------------------------------------------
# Retención y limpieza (RF-14 A43, RF-2 P9)
# ------------------------------------------------------------
@requiere_bd
async def test_A43_la_retencion_borra_las_conversaciones_de_numeros_desvinculados_hace_mas_de_12_meses(bds) -> None:
    c = bds
    t = await _negocio(c, tope=9)
    ahora = datetime.now(timezone.utc)
    viejo = await _numero(c, t, "pn-t8-viejo")
    reciente = await _numero(c, t, "pn-t8-reciente")
    conv_vieja = await _conversacion(c, t, viejo)
    conv_reciente = await _conversacion(c, t, reciente)
    await c.execute(
        "INSERT INTO messages (tenant_id, conversation_id, role, content) VALUES ($1, $2, 'user', 'x')", t, conv_vieja
    )
    await c.execute(
        "UPDATE agentes_canal SET estado = 'desvinculado', es_principal = false, desvinculado_en = $2 WHERE id = $1",
        viejo, ahora - timedelta(days=400),
    )
    await c.execute(
        "UPDATE agentes_canal SET estado = 'desvinculado', es_principal = false, desvinculado_en = $2 WHERE id = $1",
        reciente, ahora - timedelta(days=364 - 1),
    )

    borradas, numeros = await numeros_reevaluacion.aplicar_retencion(c, ahora)

    assert (borradas, numeros) == (1, 1)
    assert await c.fetchval("SELECT count(*) FROM conversations WHERE id = $1", conv_vieja) == 0
    assert await c.fetchval("SELECT count(*) FROM messages WHERE conversation_id = $1", conv_vieja) == 0
    assert await c.fetchval("SELECT count(*) FROM conversations WHERE id = $1", conv_reciente) == 1
    assert await numeros_reevaluacion.aplicar_retencion(c, ahora) == (0, 0)  # idempotente


@requiere_bd
async def test_P9_la_limpieza_borra_los_intentos_de_mas_de_25_horas(bds) -> None:
    c = bds
    t = await _negocio(c)
    ahora = datetime.now(timezone.utc)
    for horas in (30, 26, 24, 1):
        await c.execute(
            "INSERT INTO intentos_alta_ajena (tenant_id, creado_en) VALUES ($1, $2)", t, ahora - timedelta(hours=horas)
        )

    borrados = await numeros_reevaluacion.limpiar_intentos(c, ahora)

    assert borrados >= 2
    restantes = await c.fetch("SELECT creado_en FROM intentos_alta_ajena WHERE tenant_id = $1", t)
    assert len(restantes) == 2
    assert all(ahora - r["creado_en"] <= timedelta(hours=25) for r in restantes)


# ------------------------------------------------------------
# Concurrencia (A47, D30)
# ------------------------------------------------------------
@requiere_bd
async def test_A47_D30_dos_reevaluaciones_a_la_vez_no_pausan_de_mas(bd_confirmada, avisos) -> None:
    x = bd_confirmada  # negocio con plan de cupo 1
    await _cuenta_portal(x.c1, x.tenant, "owner")
    a = await _numero(x.c1, x.tenant, principal=True, dias=30)
    await _numero(x.c1, x.tenant, dias=20)
    await _numero(x.c1, x.tenant, dias=10)

    resultados = await asyncio.gather(
        reevaluar_negocio(x.c1, x.tenant, "cupo_plataforma"),
        reevaluar_negocio(x.c2, x.tenant, "cupo_plataforma"),
        return_exceptions=True,
    )

    assert not [r for r in resultados if isinstance(r, Exception)], resultados
    estados = await _estados(x.c1, x.tenant)
    assert estados[a] == "activo"
    assert sorted(v for k, v in estados.items() if k != a) == ["pausado", "pausado"]
    assert await x.c1.fetchval(
        "SELECT count(*) FROM agentes_canal WHERE tenant_id = $1 AND estado = 'activo'", x.tenant
    ) == 1
    assert len(await _bitacora(x.c1, x.tenant, "pausa_por_cupo_bajado_por_plataforma")) == 2
    assert len(_avisos_de(avisos, x.tenant, "numeros_pausados")) == 1
