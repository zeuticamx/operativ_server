"""
Servicios de números de WhatsApp (spec 005a, T3): alta, pausa, reactivación,
principal, baja, configuración, herramientas por plan, bitácora y avisos.

Dos partes, como test_numeros_sql.py:

1. PURAS (siempre corren, sin base de datos): bitácora (motivos y autor), familias
   de herramientas, contenido de los avisos, regla de responsable de las
   conversaciones ocultas, validaciones previas a tocar la base.

2. CON BASE DE DATOS (`requiere_bd`). Solo corren con NUMEROS_SQL_TEST_DATABASE_URL
   apuntando a una Postgres DE PRUEBA con las migraciones 00 a 55 aplicadas (las
   56 a 59 las aplica el fixture `bd` de test_numeros_sql dentro de una
   transacción que se revierte). Los servicios reciben la conexión, así que
   cada test trabaja sobre esa transacción y no deja nada. Nunca usan el
   DATABASE_URL del .env (las mismas salvaguardas que test_numeros_sql.py).
   Excepción: los tests de CONCURRENCIA (`bd_confirmada`) necesitan dos conexiones
   que se vean entre sí, así que aplican 56 a 59 y escriben DE VERDAD en esa base
   de prueba y limpian lo suyo al terminar.

Cada test lleva en el nombre el criterio de aceptación (A), el requisito (RF) o
el caso límite (CL) de la spec que cubre.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi import HTTPException

from services import (
    conversaciones_ocultas,
    herramientas_plan,
    herramientas_calendario,
    numeros_avisos,
    numeros_bitacora,
    numeros_whatsapp,
)
from services.conversaciones_ocultas import CuentaGerencia, elegir_responsable
from services.numeros_bitacora import SISTEMA, Autor
from services.numeros_reglas import FAMILIAS_HERRAMIENTAS
from test_numeros_sql import (  # noqa: F401 - `bd` es un fixture
    ARCHIVOS,
    MOTIVOS_BITACORA,
    URL_BD,
    _estado_plataforma,
    _herramienta,
    _leer,
    _numero,
    _suscripcion,
    _tenant,
    _vigente,
    bd,
    requiere_bd,
)

BACKEND = Path(__file__).resolve().parent.parent
AUT = Autor.gerencia(uuid4(), "owner@t3.test")


# ============================================================
# 1. PURAS
# ============================================================
# ------------------------------------------------------------
# Bitácora del negocio (RF-21; A58, A59)
# ------------------------------------------------------------
def test_RF21_los_motivos_de_bitacora_son_los_del_contrato() -> None:
    assert numeros_bitacora.MOTIVOS == MOTIVOS_BITACORA


def test_RF21_el_autor_es_gerencia_plataforma_o_sistema() -> None:
    assert Autor.gerencia(uuid4(), "a@b.c").tipo == "gerencia"
    assert Autor.plataforma("p@b.c").tipo == "plataforma"
    assert Autor.sistema() == SISTEMA and SISTEMA.tipo == "sistema"
    with pytest.raises(ValueError):
        Autor("vendedor")


async def test_RF21_un_motivo_fuera_de_la_lista_cerrada_es_error_de_programacion() -> None:
    with pytest.raises(ValueError):
        await numeros_bitacora.registrar(None, uuid4(), None, "motivo_inventado", SISTEMA)  # type: ignore[arg-type]


def test_P9_solo_se_dejan_los_ultimos_digitos_del_numero() -> None:
    assert numeros_bitacora.ultimos_digitos("5215512345678") == "5678"
    assert numeros_bitacora.ultimos_digitos("abc") == "abc"
    assert numeros_bitacora.ultimos_digitos("") == ""


# ------------------------------------------------------------
# Familias de herramientas (RF-6, RF-9)
# ------------------------------------------------------------
@pytest.mark.parametrize(
    ("tipo", "clave", "familia"),
    [
        ("http_post_json", "crear_reserva", "calendario"),
        ("http_generico", "consultar_servicios", "calendario"),
        ("http_post_json", "reagendar_reserva", "calendario"),
        ("sql_lectura", "inventario", "sql_lectura"),
        ("http_generico", "catalogo", "http"),
        ("http_post_json", "enviar_pedido", "http"),
        ("google_sheets", "hoja", "google_sheets"),
        ("google_docs", "doc", "google_docs"),
        ("tipo_desconocido", "x", None),
    ],
)
def test_RF9_familia_de_cada_herramienta(tipo: str, clave: str, familia: str | None) -> None:
    assert herramientas_plan.familia_de(tipo, clave) == familia


def test_RF9_las_claves_del_calendario_son_las_del_modulo_y_las_de_la_funcion_sql() -> None:
    assert herramientas_plan.CLAVES_CALENDARIO == herramientas_calendario.claves()
    sql = (BACKEND / "sql" / ARCHIVOS[57]).read_text(encoding="utf-8")
    bloque = re.search(r"WHEN p_tool_key IN \((.*?)\)\s+THEN 'calendario'", sql, re.S)
    assert bloque
    assert set(re.findall(r"'([a-z_]+)'", bloque.group(1))) == herramientas_plan.CLAVES_CALENDARIO


def test_P5_las_cinco_familias_son_las_de_las_reglas_puras() -> None:
    for tipo in ("sql_lectura", "http_generico", "http_post_json", "google_sheets", "google_docs"):
        assert herramientas_plan.familia_de(tipo, "k") in FAMILIAS_HERRAMIENTAS


# ------------------------------------------------------------
# Avisos (RF-20; A56, A57)
# ------------------------------------------------------------
def test_RF20_los_tipos_de_alerta_existen_en_el_esquema_y_en_la_base() -> None:
    from schemas import TipoAlerta

    for tipo in numeros_avisos.TIPOS_ALERTA:
        assert TipoAlerta(tipo).value == tipo
    sql = (BACKEND / "sql" / "60_alertas_numeros.sql").read_text(encoding="utf-8")
    for tipo in numeros_avisos.TIPOS_ALERTA:
        assert f"'{tipo}'" in sql


def test_RF20_el_aviso_va_a_owner_y_superadmin() -> None:
    from deps import ROLES_GERENCIA

    assert set(numeros_avisos.ROLES_AVISO) == set(ROLES_GERENCIA)


def test_RF20_los_motivos_de_pausa_del_aviso_son_los_de_la_bitacora() -> None:
    pausas = {m for m in MOTIVOS_BITACORA if m.startswith("pausa_por_")}
    assert set(numeros_avisos.MOTIVOS_PAUSA) == pausas


def _afectados() -> list[numeros_avisos.NumeroAfectado]:
    return [
        numeros_avisos.NumeroAfectado("+52 55 1111 2222", "pausado", uuid4()),
        numeros_avisos.NumeroAfectado("…9999", "pausado", uuid4()),
    ]


def test_A56_el_aviso_de_pausa_agrupa_los_numeros_con_negocio_estado_motivo_y_accion() -> None:
    aviso = numeros_avisos.construir_aviso_pausa(
        "Panadería Luna", _afectados(), "pausa_por_cambio_de_plan", tenant_id=uuid4()
    )
    assert aviso.tipo == "numeros_pausados"
    assert "Panadería Luna" in aviso.mensaje
    assert "+52 55 1111 2222 (pausado)" in aviso.mensaje and "…9999 (pausado)" in aviso.mensaje
    assert "cambio de plan" in aviso.mensaje
    assert "reactivarlos" in aviso.mensaje
    assert aviso.datos["negocio"] == "Panadería Luna"
    assert aviso.datos["motivo"] == "pausa_por_cambio_de_plan"
    assert [n["estado"] for n in aviso.datos["numeros"]] == ["pausado", "pausado"]
    assert aviso.datos["accion"]
    assert len(aviso.mensaje) <= 1000


def test_A57_el_aviso_de_recorte_lista_tambien_los_pausados() -> None:
    aviso = numeros_avisos.construir_aviso_recorte("Luna", _afectados())
    assert aviso.tipo == "numeros_recortados"
    assert "(pausado)" in aviso.mensaje
    assert aviso.datos["motivo"] == "recorte_por_plan"


@pytest.mark.parametrize("motivo", sorted(numeros_avisos.MOTIVOS_INCOMPLETO))
def test_RF20_el_aviso_de_incompleto_cubre_edicion_reactivacion_plan_y_despliegue(motivo: str) -> None:
    aviso = numeros_avisos.construir_aviso_incompleto(
        "Luna", [numeros_avisos.NumeroAfectado("Instagram y Facebook", "activo")], motivo
    )
    assert aviso.tipo == "numero_configuracion_incompleta"
    assert "Instagram y Facebook (activo)" in aviso.mensaje
    assert "system prompt" in aviso.mensaje


def test_RF20_un_motivo_desconocido_en_un_aviso_es_error_de_programacion() -> None:
    with pytest.raises(ValueError):
        numeros_avisos.construir_aviso_pausa("L", _afectados(), "pausa_manual")  # la manual no avisa
    with pytest.raises(ValueError):
        numeros_avisos.construir_aviso_incompleto("L", _afectados(), "otro")


def test_RF20_sin_telefono_visible_el_numero_se_nombra_por_sus_ultimos_caracteres() -> None:
    assert numeros_avisos.etiqueta_numero("+52 1", "123456789") == "+52 1"
    assert numeros_avisos.etiqueta_numero(None, "123456789") == "…6789"
    assert numeros_avisos.etiqueta_numero("  ", "123456789") == "…6789"


async def test_A56_sin_numeros_no_se_avisa_a_nadie() -> None:
    assert await numeros_avisos.avisar_pausa(None, uuid4(), [], "pausa_por_cambio_de_plan") == 0  # type: ignore[arg-type]
    assert await numeros_avisos.avisar_recorte(None, uuid4(), []) == 0  # type: ignore[arg-type]
    assert await numeros_avisos.avisar_incompleto(None, uuid4(), [], "edicion") == 0  # type: ignore[arg-type]


# ------------------------------------------------------------
# Responsable de las conversaciones ocultas (RF-14, D23; A41)
# ------------------------------------------------------------
_T0 = datetime(2026, 1, 1)


def _cuenta(rol: str, dias: int, activa: bool = True) -> CuentaGerencia:
    return CuentaGerencia(uuid4(), rol, activa, _T0 + timedelta(days=dias))


def test_A41_responsable_es_el_owner_que_creo_el_negocio_aunque_haya_mas_owners() -> None:
    creador, otro, sup = _cuenta("owner", 0), _cuenta("owner", 5), _cuenta("superadmin", 1)
    assert elegir_responsable([otro, sup, creador]) == creador.id


def test_A41_si_el_creador_esta_desactivado_lo_es_otro_owner_activo() -> None:
    creador, otro, tercero = _cuenta("owner", 0, False), _cuenta("owner", 5), _cuenta("owner", 9)
    assert elegir_responsable([tercero, creador, otro]) == otro.id


def test_A41_si_no_hay_owner_activo_lo_es_el_superadmin() -> None:
    sup1, sup2 = _cuenta("superadmin", 3), _cuenta("superadmin", 1)
    assert elegir_responsable([_cuenta("owner", 0, False), sup1, sup2]) == sup2.id
    assert elegir_responsable([_cuenta("owner", 0, False), _cuenta("superadmin", 1, False)]) is None
    assert elegir_responsable([]) is None


def test_D23_un_member_nunca_es_responsable() -> None:
    assert elegir_responsable([CuentaGerencia(uuid4(), "member", True, _T0)]) is None


# ------------------------------------------------------------
# Validaciones que no necesitan la base (RF-5, RF-2)
# ------------------------------------------------------------
@pytest.mark.parametrize("valor", [-0.01, 1.01, 2, -1, float("nan"), float("inf"), "0.5", None, True])
def test_A11_temperatura_invalida_responde_400(valor) -> None:
    with pytest.raises(HTTPException) as e:
        numeros_whatsapp.validar_temperatura(valor)
    assert e.value.status_code == 400
    assert e.value.detail["codigo"] == "temperatura_fuera_de_rango"


@pytest.mark.parametrize("valor", [0, 1, 0.0, 1.0, 0.7, 0.25])
def test_A11_temperatura_entre_0_y_1_se_acepta(valor) -> None:
    assert 0 <= numeros_whatsapp.validar_temperatura(valor) <= 1


async def test_A11_la_temperatura_invalida_se_rechaza_antes_de_tocar_la_base() -> None:
    with pytest.raises(HTTPException) as e:
        await numeros_whatsapp.editar_configuracion(
            None, uuid4(), uuid4(), {"temperature": 1.5}, AUT  # type: ignore[arg-type]
        )
    assert e.value.detail["codigo"] == "temperatura_fuera_de_rango"


async def test_RF5_un_campo_que_no_es_de_la_configuracion_es_error_de_programacion() -> None:
    with pytest.raises(ValueError):
        await numeros_whatsapp.editar_configuracion(None, uuid4(), uuid4(), {"es_principal": True}, AUT)  # type: ignore[arg-type]


@pytest.mark.parametrize("identificador", ["", "   ", None])
async def test_RF2_un_identificador_vacio_responde_400(identificador) -> None:
    with pytest.raises(HTTPException) as e:
        await numeros_whatsapp.vincular(None, uuid4(), identificador, AUT)  # type: ignore[arg-type]
    assert e.value.status_code == 400 and e.value.detail["codigo"] == "numero_no_valido"


def test_contrato_error_http_lleva_codigo_y_mensaje() -> None:
    e = numeros_whatsapp.error_http(402, "cupo_numeros_whatsapp")
    assert e.status_code == 402
    assert e.detail["codigo"] == "cupo_numeros_whatsapp" and e.detail["mensaje"]


# ============================================================
# 2. CON BASE DE DATOS DE PRUEBA
# ============================================================
@pytest.fixture
async def bds(bd):
    """La conexión de prueba (56 a 59 aplicados, revertida al final) con el códec jsonb del pool."""
    from session import _configurar_conexion

    await _configurar_conexion(bd.conn)
    return bd.conn


@pytest.fixture
def avisos(monkeypatch):
    """Los avisos que habrían salido por el portal (sin broadcast_alerta real)."""
    enviados: list[SimpleNamespace] = []

    async def falso(tenant_id, aviso, portal_user_id):
        enviados.append(SimpleNamespace(tenant=tenant_id, aviso=aviso, cuenta=portal_user_id))

    monkeypatch.setattr(numeros_avisos, "_emitir", falso)
    return enviados


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


async def _negocio(c, *, tope=None, familias=FAMILIAS_HERRAMIENTAS, modelos=None, historial=None) -> UUID:
    """
    Un negocio con suscripción de pago vigente a un plan PROPIO: inactivo (D20: lo
    contratado se conserva aunque esté desactivado) para no mover el catálogo ni el
    plan más bajo de nadie.
    """
    t = await _tenant(c)
    plan = f"plan-t3-{uuid4().hex[:10]}"
    await c.execute(
        """
        INSERT INTO planes (nombre, precio_monthly, activo, max_numeros_whatsapp, modelos_permitidos, historial_maximo)
        VALUES ($1, 9999, false, $2, $3, $4)
        """,
        plan, tope, modelos, historial,
    )
    for familia in familias:
        await c.execute("INSERT INTO plan_herramientas (plan_nombre, familia) VALUES ($1, $2)", plan, familia)
    await _suscripcion(c, t, plan, "activa", "pago", _vigente())
    return t


async def _plan_de(c, tenant: UUID) -> str:
    return await c.fetchval("SELECT plan FROM tenant_subscriptions WHERE tenant_id = $1", tenant)


async def _cuenta_portal(c, tenant: UUID, rol: str = "owner", *, activa: bool = True, dias: int = 0) -> UUID:
    uid = uuid4()
    await c.execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active, created_at)
        VALUES ($1, $2, $3, 'x', $4, $5, $6)
        """,
        uid, tenant, f"{uid}@t3.test", rol, activa, datetime.now() - timedelta(days=dias),
    )
    return uid


async def _cliente(c, tenant: UUID) -> UUID:
    uid = uuid4()
    await c.execute("INSERT INTO users (id, tenant_id, display_name) VALUES ($1, $2, 'Cliente')", uid, tenant)
    return uid


async def _conversacion(c, tenant: UUID, numero: UUID | None, *, status: str = "active", asignado: UUID | None = None) -> UUID:
    cid = uuid4()
    await c.execute(
        """
        INSERT INTO conversations (id, tenant_id, user_id, channel_type, status, agente_canal_id, asignado_a)
        VALUES ($1, $2, $3, 'whatsapp', $4, $5, $6)
        """,
        cid, tenant, await _cliente(c, tenant), status, numero, asignado,
    )
    return cid


async def _config_negocio(c, tenant: UUID, nombre: str = "Ana", prompt: str = "Eres un asistente.") -> None:
    await c.execute(
        "INSERT INTO tenant_agent_config (tenant_id, agent_name, system_prompt) VALUES ($1, $2, $3)",
        tenant, nombre, prompt,
    )


async def _fila(c, numero: UUID):
    return await c.fetchrow("SELECT * FROM agentes_canal WHERE id = $1", numero)


async def _bitacora(c, tenant: UUID, motivo: str | None = None):
    return await c.fetch(
        "SELECT * FROM numeros_bitacora WHERE tenant_id = $1 AND ($2::text IS NULL OR motivo = $2) ORDER BY id",
        tenant, motivo,
    )


async def _auditoria(c, tenant: UUID, accion: str):
    return await c.fetch(
        "SELECT * FROM gerencia_auditoria WHERE tenant_id = $1 AND accion = $2 ORDER BY created_at", tenant, accion
    )


async def _http(coro, estado: int, codigo: str) -> HTTPException:
    with pytest.raises(HTTPException) as e:
        await coro
    assert e.value.status_code == estado, e.value.detail
    assert e.value.detail["codigo"] == codigo
    return e.value


async def _principal(c, tenant: UUID) -> UUID | None:
    return await c.fetchval("SELECT id FROM agentes_canal WHERE tenant_id = $1 AND es_principal", tenant)


# ------------------------------------------------------------
# Alta (RF-2, RF-5, RF-6; A3, A4, A6, A7, A10, A12, A13, A66, A67, A68)
# ------------------------------------------------------------
@requiere_bd
async def test_A7_un_numero_libre_se_da_de_alta_activo_y_queda_en_la_bitacora_con_su_autor(bds) -> None:
    c = bds
    t = await _negocio(c)
    n = await numeros_whatsapp.vincular(c, t, "pn-libre-1", AUT, telefono_visible="+52 55 0000 0001")
    assert n["estado"] == "activo" and n["tenant_id"] == t and n["phone_number_id"] == "pn-libre-1"
    # Sin configuración de referencia ni del negocio: nace incompleto y por eso sin principal (D33).
    assert n["es_principal"] is False
    entradas = await _bitacora(c, t, "vinculacion")
    assert len(entradas) == 1
    e = entradas[0]
    assert (e["autor_tipo"], e["autor_portal_user_id"], e["autor_email"]) == ("gerencia", AUT.portal_user_id, AUT.email)
    assert e["agente_canal_id"] == n["id"] and e["detalle"] == {"ultimos_digitos": "re-1"}


@requiere_bd
async def test_A12_CL16_el_primer_numero_hereda_la_referencia_y_queda_principal(bds) -> None:
    c = bds
    t = await _negocio(c)
    await _herramienta(c, t, "consulta")
    await c.execute(
        """
        INSERT INTO config_referencia_negocio
            (tenant_id, agent_name, system_prompt, temperature, history_window, agente_activo, herramientas)
        VALUES ($1, 'Luna', 'Vende pan.', 0.30, 20, false, ARRAY['consulta', 'ya_no_existe'])
        """,
        t,
    )
    n = await numeros_whatsapp.vincular(c, t, "pn-ref", AUT)
    assert (n["agent_name"], n["system_prompt"], n["history_window"], n["agente_activo"]) == ("Luna", "Vende pan.", 20, False)
    assert float(n["temperature"]) == pytest.approx(0.3)
    assert n["es_principal"] is True
    # La clave que ya no existe en el negocio no se copia.
    assert await herramientas_plan.herramientas_de_numero(c, t, n["id"]) == ["consulta"]
    assert len(await _bitacora(c, t, "cambio_de_principal")) == 1


@requiere_bd
async def test_A12_sin_referencia_guardada_nace_de_la_configuracion_del_negocio_y_sus_herramientas(bds) -> None:
    c = bds
    t = await _negocio(c)
    await _config_negocio(c, t, "Mía", "Hola.")
    await _herramienta(c, t, "h1")
    await _herramienta(c, t, "h2", habilitada=False)
    n = await numeros_whatsapp.vincular(c, t, "pn-base", AUT)
    assert (n["agent_name"], n["system_prompt"]) == ("Mía", "Hola.")
    assert n["es_principal"] is True
    assert await herramientas_plan.herramientas_de_numero(c, t, n["id"]) == ["h1"]
    # Y la referencia queda guardada con el principal.
    ref = await c.fetchrow("SELECT * FROM config_referencia_negocio WHERE tenant_id = $1", t)
    assert ref["agent_name"] == "Mía" and list(ref["herramientas"]) == ["h1"]


@requiere_bd
async def test_A10_A13_el_numero_nuevo_copia_al_principal_incluido_el_estado_de_agente_y_no_lo_afecta(bds) -> None:
    c = bds
    t = await _negocio(c)
    await _herramienta(c, t, "k1")
    await _herramienta(c, t, "k2")
    await c.execute(
        "INSERT INTO config_referencia_negocio (tenant_id, agent_name, system_prompt) VALUES ($1, 'Otra', 'otra cosa')", t
    )
    p = await _numero(c, t, "pn-p", principal=True, nombre="Pri", prompt="Soy el principal.", agente=False)
    await c.execute("INSERT INTO agente_canal_herramientas VALUES ($1, $2, 'k1')", p, t)
    n = await numeros_whatsapp.vincular(c, t, "pn-2", AUT)
    assert (n["agent_name"], n["system_prompt"], n["agente_activo"], n["es_principal"]) == (
        "Pri", "Soy el principal.", False, False,
    )
    assert await herramientas_plan.herramientas_de_numero(c, t, n["id"]) == ["k1"]
    # Una herramienta nueva del negocio nace desactivada en todos los números (A13, D27).
    assert await herramientas_plan.herramientas_de_numero(c, t, p) == ["k1"]
    # Editar el nuevo no toca al principal.
    await numeros_whatsapp.editar_configuracion(c, t, n["id"], {"agent_name": "Nuevo", "system_prompt": "Distinto"}, AUT)
    assert (await _fila(c, p))["agent_name"] == "Pri"


@requiere_bd
async def test_RF6_el_numero_nuevo_solo_copia_herramientas_cuya_familia_incluye_el_plan(bds) -> None:
    c = bds
    t = await _negocio(c, familias=("http",))
    await _herramienta(c, t, "web", "http_generico")
    await _herramienta(c, t, "hoja", "google_sheets")
    await c.execute(
        """
        INSERT INTO config_referencia_negocio (tenant_id, agent_name, system_prompt, herramientas)
        VALUES ($1, 'L', 'p', ARRAY['web', 'hoja'])
        """,
        t,
    )
    n = await numeros_whatsapp.vincular(c, t, "pn-fam", AUT)
    assert await herramientas_plan.herramientas_de_numero(c, t, n["id"]) == ["web"]


@requiere_bd
async def test_A3_con_el_cupo_lleno_el_alta_responde_402_sea_libre_ajeno_o_propio(bds) -> None:
    c = bds
    t = await _negocio(c, tope=2)
    otro = await _tenant(c)
    await _numero(c, t, "pn-a")
    await _numero(c, t, "pn-b")
    await _numero(c, t, "pn-pausado", estado="pausado")
    await _numero(c, otro, "pn-ajeno")
    for identificador in ("pn-libre", "pn-pausado", "pn-a", "pn-ajeno"):
        await _http(numeros_whatsapp.vincular(c, t, identificador, AUT), 402, "cupo_numeros_whatsapp")
    assert await c.fetchval("SELECT count(*) FROM agentes_canal WHERE tenant_id = $1", t) == 3
    assert await c.fetchval("SELECT count(*) FROM agentes_canal WHERE phone_number_id = 'pn-libre'") == 0


@requiere_bd
async def test_A4_un_numero_propio_responde_409_sin_duplicarlo_ni_reactivarlo(bds) -> None:
    c = bds
    t = await _negocio(c, tope=5)
    activo = await _numero(c, t, "pn-activo")
    pausado = await _numero(c, t, "pn-pausado", estado="pausado")
    await _http(numeros_whatsapp.vincular(c, t, "pn-activo", AUT), 409, "numero_ya_vinculado")
    await _http(numeros_whatsapp.vincular(c, t, "pn-pausado", AUT), 409, "numero_ya_vinculado")
    assert (await _fila(c, activo))["estado"] == "activo"
    assert (await _fila(c, pausado))["estado"] == "pausado"
    assert await c.fetchval("SELECT count(*) FROM agentes_canal WHERE tenant_id = $1", t) == 2


@requiere_bd
async def test_A7_A8_un_numero_ajeno_responde_404_igual_cuenta_intento_y_queda_en_la_auditoria(bds) -> None:
    c = bds
    t = await _negocio(c, tope=9)
    otro = await _tenant(c)
    await _numero(c, otro, "pn-ajeno-activo")
    await _numero(c, otro, "pn-ajeno-pausado", estado="pausado")
    e1 = await _http(numeros_whatsapp.vincular(c, t, "pn-ajeno-activo", AUT), 404, "numero_no_encontrado")
    e2 = await _http(numeros_whatsapp.vincular(c, t, "pn-ajeno-pausado", AUT), 404, "numero_no_encontrado")
    assert e1.detail == e2.detail
    # No revela de quién es el número.
    assert str(otro) not in json.dumps(e1.detail)
    assert await c.fetchval("SELECT count(*) FROM intentos_alta_ajena WHERE tenant_id = $1", t) == 2
    rechazos = await _auditoria(c, t, "alta_ajena_rechazada")
    assert len(rechazos) == 2
    # Dentro de una transacción NOW() es el mismo para ambas filas: el orden por fecha no es
    # determinista, así que se comprueba cada fila y el conjunto, no la posición.
    assert all(r["actor_email"] == AUT.email and r["actor_portal_user_id"] == AUT.portal_user_id for r in rechazos)
    assert all("pn-ajeno" not in json.dumps(r["detalle"]) for r in rechazos)  # solo los últimos dígitos
    assert {r["detalle"]["ultimos_digitos"] for r in rechazos} == {"tivo", "sado"}
    assert await c.fetchval("SELECT count(*) FROM agentes_canal WHERE tenant_id = $1", t) == 0


@requiere_bd
async def test_A6_cinco_altas_ajenas_bloquean_con_429_incluso_un_numero_libre_hasta_que_sale_la_primera(bds) -> None:
    c = bds
    t = await _negocio(c)
    otro = await _tenant(c)
    await _numero(c, otro, "pn-ajeno")
    base = _ahora()
    for i in range(5):
        await _http(
            numeros_whatsapp.vincular(c, t, "pn-ajeno", AUT, ahora=base + timedelta(minutes=i)), 404, "numero_no_encontrado"
        )
    ahora = base + timedelta(minutes=10)
    e = await _http(numeros_whatsapp.vincular(c, t, "pn-libre", AUT, ahora=ahora), 429, "demasiados_intentos")
    libera = base + timedelta(hours=24)
    assert e.detail["libera_en"] == libera.isoformat()
    assert e.headers["Retry-After"] == str(int((libera - ahora).total_seconds()))
    # El bloqueo empieza una sola vez en la auditoría de la plataforma, y el 429 no cuenta como intento.
    assert len(await _auditoria(c, t, "altas_bloqueadas")) == 1
    assert await c.fetchval("SELECT count(*) FROM intentos_alta_ajena WHERE tenant_id = $1", t) == 5
    assert await c.fetchval("SELECT count(*) FROM agentes_canal WHERE phone_number_id = 'pn-libre'") == 0
    # Sale de la ventana la primera: ya no hay cinco y el número libre se da de alta.
    n = await numeros_whatsapp.vincular(c, t, "pn-libre", AUT, ahora=libera + timedelta(seconds=30))
    assert n["estado"] == "activo"


@requiere_bd
async def test_A68_el_bloqueo_por_intentos_va_antes_que_el_cupo_lleno(bds) -> None:
    c = bds
    t = await _negocio(c, tope=1)
    await _numero(c, t, "pn-uno")
    base = _ahora()
    for i in range(5):
        await c.execute("INSERT INTO intentos_alta_ajena (tenant_id, creado_en) VALUES ($1, $2)", t, base - timedelta(minutes=i))
    await _http(numeros_whatsapp.vincular(c, t, "pn-libre", AUT, ahora=base), 429, "demasiados_intentos")


@requiere_bd
async def test_A68_el_cupo_lleno_va_antes_que_el_numero_propio_y_el_ajeno(bds) -> None:
    c = bds
    t = await _negocio(c, tope=1)
    await _numero(c, t, "pn-uno")
    await _http(numeros_whatsapp.vincular(c, t, "pn-uno", AUT), 402, "cupo_numeros_whatsapp")


@requiere_bd
async def test_A67_CL12_sin_tope_no_se_rechaza_por_cupo_pero_sigue_el_404_de_alta_ajena(bds) -> None:
    c = bds
    t = await _tenant(c)
    await _estado_plataforma(c, t, "prueba")  # prueba vigente sin plan: sin tope
    otro = await _tenant(c)
    await _numero(c, otro, "pn-ajeno")
    for i in range(4):
        await numeros_whatsapp.vincular(c, t, f"pn-{i}", AUT)
    assert await c.fetchval("SELECT count(*) FROM agentes_canal WHERE tenant_id = $1", t) == 4
    await _http(numeros_whatsapp.vincular(c, t, "pn-ajeno", AUT), 404, "numero_no_encontrado")


@requiere_bd
async def test_A66_un_negocio_existente_vincula_un_segundo_numero_con_las_reglas_de_cupo(bds) -> None:
    c = bds
    t = await _negocio(c, tope=2)
    await _numero(c, t, "pn-migrado", principal=True)
    n = await numeros_whatsapp.vincular(c, t, "pn-segundo", AUT)
    assert n["estado"] == "activo"
    await _http(numeros_whatsapp.vincular(c, t, "pn-tercero", AUT), 402, "cupo_numeros_whatsapp")


@requiere_bd
async def test_RF16_un_numero_desvinculado_se_puede_vincular_en_otro_negocio(bds) -> None:
    c = bds
    a, b = await _negocio(c), await _negocio(c)
    n = await numeros_whatsapp.vincular(c, a, "pn-viaja", AUT)
    await numeros_whatsapp.desvincular(c, a, n["id"], AUT)
    nuevo = await numeros_whatsapp.vincular(c, b, "pn-viaja", AUT)
    assert nuevo["tenant_id"] == b and nuevo["estado"] == "activo"


# ------------------------------------------------------------
# Concurrencia (CL-5, A3, A47): dos conexiones que se ven entre sí
# ------------------------------------------------------------
@pytest.fixture
async def bd_confirmada():
    """
    Dos conexiones a la base de PRUEBA con 56 a 59 aplicados DE VERDAD y un negocio
    con cupo 1. Escribe y confirma: solo para la base temporal; limpia lo suyo.
    """
    from config import settings
    from session import _configurar_conexion

    if not URL_BD:
        pytest.skip("sin NUMEROS_SQL_TEST_DATABASE_URL")
    if URL_BD == settings.DATABASE_URL and os.environ.get("NUMEROS_SQL_TEST_PERMITIR_BD_DE_ENV") != "1":
        pytest.skip("NUMEROS_SQL_TEST_DATABASE_URL es el DATABASE_URL del backend; usa otra base")
    c1 = await asyncpg.connect(dsn=URL_BD)
    c2 = await asyncpg.connect(dsn=URL_BD)
    tenant: UUID | None = None
    plan: str | None = None
    try:
        for conn in (c1, c2):
            await _configurar_conexion(conn)
        if await c1.fetchval("SELECT to_regclass('public.tenant_subscriptions') IS NULL"):
            pytest.skip("la base de prueba no tiene aplicadas las migraciones 00 a 55")
        for numero in sorted(ARCHIVOS):
            await c1.execute(_leer(numero))
        tenant = await _negocio(c1, tope=1)
        plan = await _plan_de(c1, tenant)
        yield SimpleNamespace(c1=c1, c2=c2, tenant=tenant)
    finally:
        if tenant is not None:
            await c1.execute("DELETE FROM tenants WHERE id = $1", tenant)
            await c1.execute("DELETE FROM gerencia_auditoria WHERE tenant_id = $1", tenant)
        if plan is not None:
            await c1.execute("DELETE FROM planes WHERE nombre = $1", plan)
        await c1.close()
        await c2.close()


@requiere_bd
async def test_CL5_A3_dos_altas_simultaneas_con_una_plaza_solo_completan_una(bd_confirmada) -> None:
    x = bd_confirmada
    resultados = await asyncio.gather(
        numeros_whatsapp.vincular(x.c1, x.tenant, f"pn-a-{uuid4().hex[:6]}", AUT),
        numeros_whatsapp.vincular(x.c2, x.tenant, f"pn-b-{uuid4().hex[:6]}", AUT),
        return_exceptions=True,
    )
    exitos = [r for r in resultados if not isinstance(r, Exception)]
    fallos = [r for r in resultados if isinstance(r, HTTPException)]
    assert len(exitos) == 1 and len(fallos) == 1, resultados
    assert fallos[0].status_code == 402 and fallos[0].detail["codigo"] == "cupo_numeros_whatsapp"
    assert await x.c1.fetchval("SELECT count(*) FROM agentes_canal WHERE tenant_id = $1 AND estado = 'activo'", x.tenant) == 1


@requiere_bd
async def test_A47_dos_reactivaciones_simultaneas_con_una_plaza_dejan_una_sola_activa(bd_confirmada) -> None:
    x = bd_confirmada
    a = await _numero(x.c1, x.tenant, f"pn-{uuid4().hex[:8]}", estado="pausado")
    b = await _numero(x.c1, x.tenant, f"pn-{uuid4().hex[:8]}", estado="pausado")
    resultados = await asyncio.gather(
        numeros_whatsapp.reactivar(x.c1, x.tenant, a, AUT),
        numeros_whatsapp.reactivar(x.c2, x.tenant, b, AUT),
        return_exceptions=True,
    )
    fallos = [r for r in resultados if isinstance(r, HTTPException)]
    assert len(fallos) == 1 and fallos[0].detail["codigo"] == "cupo_numeros_whatsapp", resultados
    assert await x.c1.fetchval("SELECT count(*) FROM agentes_canal WHERE tenant_id = $1 AND estado = 'activo'", x.tenant) == 1


# ------------------------------------------------------------
# Pausa manual (RF-15; A44, CL-14, A74)
# ------------------------------------------------------------
@requiere_bd
async def test_A44_CL14_la_pausa_manual_libera_la_plaza_no_avisa_y_se_permite_pausar_el_ultimo(bds, avisos) -> None:
    c = bds
    t = await _negocio(c, tope=3)
    await _cuenta_portal(c, t, "owner")
    p = await _numero(c, t, principal=True, dias=3)
    b = await _numero(c, t, dias=2)
    r = await numeros_whatsapp.pausar_manual(c, t, b, AUT)
    assert r.numero["estado"] == "pausado" and r.numero["motivo_pausa"] == "pausa_manual" and r.numero["pausado_en"]
    assert r.sin_numero_activo is False
    assert (await numeros_whatsapp.listar(c, t)).plazas_usadas == 1
    e = (await _bitacora(c, t, "pausa_manual"))[0]
    assert (e["autor_tipo"], e["autor_portal_user_id"], e["agente_canal_id"]) == ("gerencia", AUT.portal_user_id, b)
    # El último número activo también se puede pausar: advierte, y el negocio queda sin principal.
    r2 = await numeros_whatsapp.pausar_manual(c, t, p, AUT)
    assert r2.sin_numero_activo is True
    assert await _principal(c, t) is None
    assert len(await _bitacora(c, t, "principal_sin_numero_activo")) == 1
    assert avisos == []  # RF-20: la pausa manual no genera aviso


@requiere_bd
async def test_A44_pausar_un_numero_ya_pausado_no_cambia_nada(bds) -> None:
    c = bds
    t = await _negocio(c)
    n = await _numero(c, t, estado="pausado")
    r = await numeros_whatsapp.pausar_manual(c, t, n, AUT)
    assert r.numero["estado"] == "pausado"
    assert await _bitacora(c, t, "pausa_manual") == []


@requiere_bd
async def test_A74_la_gerencia_no_puede_pausar_desvincular_ni_elegir_principal_la_linea_de_avisos(bds) -> None:
    c = bds
    t = await _negocio(c)
    linea = await _numero(c, t, "pn-avisos", avisos=True)
    await _http(numeros_whatsapp.pausar_manual(c, t, linea, AUT), 403, "linea_avisos_gestionada_por_plataforma")
    await _http(numeros_whatsapp.desvincular(c, t, linea, AUT), 403, "linea_avisos_gestionada_por_plataforma")
    await _http(numeros_whatsapp.elegir_principal(c, t, linea, AUT), 403, "linea_avisos_gestionada_por_plataforma")
    fila = await _fila(c, linea)
    assert fila["estado"] == "activo" and fila["es_linea_avisos"] is True and fila["es_principal"] is False


# ------------------------------------------------------------
# 404 igual para número ajeno, pausado ajeno e inexistente (RF-4; A5)
# ------------------------------------------------------------
_ACCIONES = {
    "pausar": lambda c, t, n: numeros_whatsapp.pausar_manual(c, t, n, AUT),
    "reactivar": lambda c, t, n: numeros_whatsapp.reactivar(c, t, n, AUT),
    "desvincular": lambda c, t, n: numeros_whatsapp.desvincular(c, t, n, AUT),
    "principal": lambda c, t, n: numeros_whatsapp.elegir_principal(c, t, n, AUT),
    "configuracion": lambda c, t, n: numeros_whatsapp.editar_configuracion(c, t, n, {"agent_name": "X"}, AUT),
    "activar_herramientas": lambda c, t, n: herramientas_plan.activar_herramientas_de_numero(c, t, n, [], AUT),
    "desactivar_herramientas": lambda c, t, n: herramientas_plan.desactivar_herramientas_de_numero(c, t, n, [], AUT),
}


@requiere_bd
@pytest.mark.parametrize("accion", sorted(_ACCIONES))
async def test_A5_sobre_un_numero_ajeno_pausado_ajeno_inexistente_o_desvinculado_la_respuesta_es_la_misma(bds, accion) -> None:
    c = bds
    t, otro = await _negocio(c), await _negocio(c)
    ajeno = await _numero(c, otro)
    ajeno_pausado = await _numero(c, otro, estado="pausado")
    desvinculado_propio = await _numero(c, t, estado="desvinculado")
    detalles = []
    for numero in (ajeno, ajeno_pausado, uuid4(), desvinculado_propio):
        e = await _http(_ACCIONES[accion](c, t, numero), 404, "numero_no_encontrado")
        detalles.append(e.detail)
    assert all(d == detalles[0] for d in detalles)
    assert (await _fila(c, ajeno))["estado"] == "activo"


# ------------------------------------------------------------
# Reactivar (RF-15; A21, A37, CL-2, CL-3, CL-30)
# ------------------------------------------------------------
@requiere_bd
async def test_A37_CL3_reactivar_sin_cupo_responde_402_y_subir_el_cupo_no_reactiva_nada(bds) -> None:
    c = bds
    t = await _negocio(c, tope=1)
    await _numero(c, t, principal=True)
    p = await _numero(c, t, estado="pausado")
    await _http(numeros_whatsapp.reactivar(c, t, p, AUT), 402, "cupo_numeros_whatsapp")
    assert (await _fila(c, p))["estado"] == "pausado"
    await c.execute("UPDATE planes SET max_numeros_whatsapp = 3 WHERE nombre = $1", await _plan_de(c, t))
    assert (await _fila(c, p))["estado"] == "pausado"  # CL-2: nada se reactiva solo
    r = await numeros_whatsapp.reactivar(c, t, p, AUT)
    assert r["estado"] == "activo" and r["pausado_en"] is None and r["motivo_pausa"] is None
    e = (await _bitacora(c, t, "reactivacion_manual"))[0]
    assert (e["autor_tipo"], e["agente_canal_id"]) == ("gerencia", p)


@requiere_bd
async def test_A21_reactivar_un_numero_incompleto_lo_deja_activo_ocupa_plaza_y_avisa_a_la_gerencia(bds, avisos) -> None:
    c = bds
    t = await _negocio(c, tope=2)
    owner = await _cuenta_portal(c, t, "owner")
    sup = await _cuenta_portal(c, t, "superadmin")
    await _cuenta_portal(c, t, "member")
    await _cuenta_portal(c, t, "owner", activa=False)
    n = await _numero(c, t, estado="pausado", prompt="")
    r = await numeros_whatsapp.reactivar(c, t, n, AUT)
    assert r["estado"] == "activo"
    assert (await numeros_whatsapp.listar(c, t)).plazas_usadas == 1
    assert {a.cuenta for a in avisos} == {owner, sup}
    assert all(a.aviso.tipo == "numero_configuracion_incompleta" for a in avisos)
    assert avisos[0].aviso.datos["numeros"][0]["estado"] == "activo"
    assert len(await _bitacora(c, t, "configuracion_incompleta")) == 1
    assert r["es_principal"] is False  # un incompleto no puede ser principal (D33)


@requiere_bd
async def test_A46_un_numero_reactivado_conserva_su_antiguedad_pero_no_recupera_el_principal(bds) -> None:
    c = bds
    t = await _negocio(c)
    a = await _numero(c, t, principal=True, dias=10)
    b = await _numero(c, t, dias=5)
    await _numero(c, t, dias=3)
    await numeros_whatsapp.pausar_manual(c, t, a, AUT)
    assert await _principal(c, t) == b  # el activo completo más antiguo
    antes = (await _fila(c, a))["vinculado_en"]
    await numeros_whatsapp.reactivar(c, t, a, AUT)
    assert await _principal(c, t) == b
    assert (await _fila(c, a))["vinculado_en"] == antes


@requiere_bd
async def test_CL30_reactivar_sin_principal_asigna_al_mas_antiguo_activo_completo_no_al_reactivado(bds) -> None:
    c = bds
    t = await _negocio(c)
    antiguo = await _numero(c, t, dias=5)  # activo, completo, pero el negocio no tiene principal
    nuevo = await _numero(c, t, estado="pausado", dias=1)
    assert await _principal(c, t) is None
    await numeros_whatsapp.reactivar(c, t, nuevo, AUT)
    assert await _principal(c, t) == antiguo
    assert len(await _bitacora(c, t, "cambio_de_principal")) == 1


# ------------------------------------------------------------
# Principal (RF-15, D34; A45, A46, A64, CL-4, CL-15, CL-31)
# ------------------------------------------------------------
@requiere_bd
async def test_A45_CL15_elegir_principal_409_si_pausado_409_si_incompleto_y_el_cambio_queda_guardado(bds) -> None:
    c = bds
    t = await _negocio(c, tope=9)
    a = await _numero(c, t, principal=True, nombre="Ana", dias=9)
    b = await _numero(c, t, nombre="Bruno", prompt="Soy Bruno.", dias=8)
    pausado = await _numero(c, t, estado="pausado")
    incompleto = await _numero(c, t, prompt="  ")
    await _http(numeros_whatsapp.elegir_principal(c, t, pausado, AUT), 409, "numero_no_activo")
    await _http(numeros_whatsapp.elegir_principal(c, t, incompleto, AUT), 409, "configuracion_incompleta")
    assert await _principal(c, t) == a
    r = await numeros_whatsapp.elegir_principal(c, t, b, AUT)
    assert r["es_principal"] is True and await _principal(c, t) == b
    assert (await _fila(c, a))["es_principal"] is False
    e = (await _bitacora(c, t, "cambio_de_principal"))[-1]
    assert (e["autor_tipo"], e["agente_canal_id"], e["detalle"]["anterior"]) == ("gerencia", b, str(a))
    # La referencia del negocio pasó a ser la del nuevo principal.
    ref = await c.fetchrow("SELECT * FROM config_referencia_negocio WHERE tenant_id = $1", t)
    assert (ref["agent_name"], ref["system_prompt"]) == ("Bruno", "Soy Bruno.")


@requiere_bd
async def test_A46_si_el_principal_se_pausa_lo_sustituye_el_activo_completo_mas_antiguo_y_el_empate_lo_gana_el_id_mas_bajo(bds) -> None:
    c = bds
    t = await _negocio(c, tope=9)
    a = await _numero(c, t, principal=True, dias=10)
    b = await _numero(c, t, dias=2)
    d = await _numero(c, t, dias=2)
    mismo = await c.fetchval("SELECT vinculado_en FROM agentes_canal WHERE id = $1", b)
    await c.execute("UPDATE agentes_canal SET vinculado_en = $2 WHERE id = ANY($1::uuid[])", [b, d], mismo)
    await numeros_whatsapp.pausar_manual(c, t, a, AUT)
    assert await _principal(c, t) == min(b, d, key=str)
    e = (await _bitacora(c, t, "cambio_de_principal"))[-1]
    assert e["detalle"]["anterior"] == str(a) and e["autor_tipo"] == "gerencia"


@requiere_bd
async def test_CL31_un_principal_que_pasa_a_incompleto_deja_de_serlo_y_sin_sustituto_el_nuevo_copia_la_referencia(bds) -> None:
    c = bds
    t = await _negocio(c)
    await _config_negocio(c, t, "Ana", "Eres amable.")
    n1 = await numeros_whatsapp.vincular(c, t, "pn-1", AUT)
    assert n1["es_principal"] is True
    await numeros_whatsapp.editar_configuracion(c, t, n1["id"], {"system_prompt": "   "}, AUT)
    fila = await _fila(c, n1["id"])
    assert fila["es_principal"] is False and fila["estado"] == "activo"
    assert await _principal(c, t) is None
    assert len(await _bitacora(c, t, "principal_sin_numero_activo")) == 1
    # La referencia se conserva (RF-5) y el número nuevo nace con ella, y queda principal.
    n2 = await numeros_whatsapp.vincular(c, t, "pn-2", AUT)
    assert (n2["agent_name"], n2["system_prompt"]) == ("Ana", "Eres amable.")
    assert await _principal(c, t) == n2["id"]


@requiere_bd
async def test_CL31_con_otro_activo_completo_este_sustituye_al_principal_que_queda_incompleto(bds) -> None:
    c = bds
    t = await _negocio(c)
    a = await _numero(c, t, principal=True, dias=9)
    b = await _numero(c, t, dias=3)
    await numeros_whatsapp.editar_configuracion(c, t, a, {"agent_name": ""}, AUT)
    assert await _principal(c, t) == b


@requiere_bd
async def test_A64_la_configuracion_que_pasa_a_completa_dispara_la_regla_y_queda_en_la_bitacora(bds) -> None:
    c = bds
    t = await _negocio(c)
    n = await _numero(c, t, nombre="", prompt="")
    assert await _principal(c, t) is None
    r = await numeros_whatsapp.editar_configuracion(c, t, n, {"agent_name": "Ana", "system_prompt": "Hola."}, AUT)
    assert r["es_principal"] is True
    assert len(await _bitacora(c, t, "configuracion_completada")) == 1
    assert len(await _bitacora(c, t, "cambio_de_principal")) == 1


@requiere_bd
async def test_A64_sin_ningun_activo_completo_el_negocio_no_tiene_principal(bds) -> None:
    c = bds
    t = await _negocio(c)
    await _numero(c, t, prompt="")
    await _numero(c, t, estado="pausado")
    await numeros_whatsapp.aplicar_regla_principal(c, t)
    assert await _principal(c, t) is None


# ------------------------------------------------------------
# Línea de avisos (P11, RF-22; A49, A70)
# ------------------------------------------------------------
async def _credencial_whatsapp(c, tenant: UUID, phone_number_id: str) -> None:
    await c.execute(
        "INSERT INTO channel_credentials (tenant_id, channel_type, phone_number_id, is_active) VALUES ($1, 'whatsapp', $2, true)",
        tenant, phone_number_id,
    )


@requiere_bd
async def test_P11_la_sincronizacion_quita_el_principal_a_la_linea_de_avisos_y_aplica_la_regla_despues(bds) -> None:
    c = bds
    t = await _negocio(c, tope=9)
    await _credencial_whatsapp(c, t, "pn-avisos")
    # Como la deja la migración de datos: la línea de avisos, principal de su negocio.
    linea = await _numero(c, t, "pn-avisos", principal=True, nombre="Aviso", prompt="Aviso.", dias=20)
    otro = await _numero(c, t, "pn-otro", nombre="Otro", prompt="Otro.", dias=5)
    marcada = await numeros_whatsapp.sincronizar_linea_avisos(c, t)
    assert marcada == linea
    fila = await _fila(c, linea)
    assert fila["es_linea_avisos"] is True and fila["es_principal"] is False
    assert await _principal(c, t) == otro
    ref = await c.fetchrow("SELECT agent_name FROM config_referencia_negocio WHERE tenant_id = $1", t)
    assert ref["agent_name"] == "Otro"  # la referencia nunca viene de la línea de avisos
    assert (await numeros_whatsapp.listar(c, t)).plazas_usadas == 1  # no cuenta para el cupo
    # Idempotente.
    assert await numeros_whatsapp.sincronizar_linea_avisos(c, t) == linea
    assert await _principal(c, t) == otro


@requiere_bd
async def test_P11_si_la_linea_de_avisos_era_el_unico_completo_el_negocio_queda_sin_principal_ni_referencia_suya(bds) -> None:
    c = bds
    t = await _negocio(c)
    await _credencial_whatsapp(c, t, "pn-avisos")
    linea = await _numero(c, t, "pn-avisos", principal=True)
    await c.execute(
        "INSERT INTO config_referencia_negocio (tenant_id, agent_name, system_prompt) VALUES ($1, 'Ana', 'copiada de la línea')", t
    )
    await numeros_whatsapp.sincronizar_linea_avisos(c, t)
    assert await _principal(c, t) is None
    assert len(await _bitacora(c, t, "principal_sin_numero_activo")) == 1
    assert await c.fetchval("SELECT count(*) FROM config_referencia_negocio WHERE tenant_id = $1", t) == 0
    assert (await _fila(c, linea))["es_linea_avisos"] is True


@requiere_bd
async def test_P11_una_marca_vieja_se_quita_cuando_cambia_el_numero_de_la_linea(bds) -> None:
    c = bds
    t = await _negocio(c)
    await _credencial_whatsapp(c, t, "pn-nueva")
    vieja = await _numero(c, t, "pn-vieja", avisos=True, dias=9)
    nueva = await _numero(c, t, "pn-nueva", dias=2)
    assert await numeros_whatsapp.sincronizar_linea_avisos(c, t) == nueva
    assert (await _fila(c, vieja))["es_linea_avisos"] is False
    assert (await _fila(c, nueva))["es_linea_avisos"] is True
    assert await _principal(c, t) == vieja  # ya sin marca, es un número como los demás


@requiere_bd
async def test_P11_sin_negocio_configurado_o_sin_numero_vinculado_no_marca_nada(bds, monkeypatch) -> None:
    c = bds
    t = await _negocio(c)
    monkeypatch.setattr(numeros_whatsapp.settings, "AVISOS_CUENTA_GLOBAL_TENANT_ID", "")
    assert await numeros_whatsapp.sincronizar_linea_avisos(c) is None
    await _credencial_whatsapp(c, t, "pn-sin-vincular")
    assert await numeros_whatsapp.sincronizar_linea_avisos(c, t) is None


@requiere_bd
async def test_A49_A70_al_vincular_la_linea_de_avisos_queda_marcada_fuera_del_cupo_y_sin_ser_principal(bds, monkeypatch) -> None:
    c = bds
    t = await _negocio(c, tope=1)
    await _config_negocio(c, t)
    await _credencial_whatsapp(c, t, "pn-avisos")
    monkeypatch.setattr(numeros_whatsapp, "tenant_de_avisos", lambda: t)
    linea = await numeros_whatsapp.vincular(c, t, "pn-avisos", AUT)
    assert linea["es_linea_avisos"] is True and linea["es_principal"] is False
    lista = await numeros_whatsapp.listar(c, t)
    assert lista.plazas_usadas == 0 and lista.plazas_libres == 1
    # Con la plaza libre, otro número completo se vincula y es el principal (la línea no puede serlo).
    otro = await numeros_whatsapp.vincular(c, t, "pn-otro", AUT)
    assert otro["es_principal"] is True


@requiere_bd
async def test_A70_la_configuracion_de_la_linea_de_avisos_se_edita_y_le_aplican_las_reglas_de_incompleta(bds, avisos) -> None:
    c = bds
    t = await _negocio(c)
    owner = await _cuenta_portal(c, t, "owner")
    linea = await _numero(c, t, "pn-avisos", avisos=True)
    r = await numeros_whatsapp.editar_configuracion(c, t, linea, {"system_prompt": "", "agente_activo": False}, AUT)
    assert r["es_linea_avisos"] is True and r["es_principal"] is False
    assert [a.cuenta for a in avisos] == [owner]
    assert {b["motivo"] for b in await _bitacora(c, t)} >= {"configuracion_incompleta", "agente_apagado", "prompt_cambiado"}


# ------------------------------------------------------------
# Desvincular y conversaciones ocultas (RF-14, RF-16; A40 a A43, CL-22, CL-23, CL-28)
# ------------------------------------------------------------
@requiere_bd
async def test_A40_A41_CL22_desvincular_libera_la_plaza_oculta_y_asigna_al_owner_creador(bds, avisos) -> None:
    c = bds
    t = await _negocio(c, tope=2)
    creador = await _cuenta_portal(c, t, "owner", dias=30)
    await _cuenta_portal(c, t, "owner", dias=2)
    await _cuenta_portal(c, t, "superadmin", dias=20)
    member = await _cuenta_portal(c, t, "member")
    n = await _numero(c, t, "pn-baja", principal=True, dias=9)
    n2 = await _numero(c, t, "pn-queda", dias=3)
    c1 = await _conversacion(c, t, n)
    c2 = await _conversacion(c, t, n, status="transferred", asignado=member)
    c3 = await _conversacion(c, t, n2)

    ocultas = await numeros_whatsapp.desvincular(c, t, n, AUT)
    assert ocultas == 2
    fila = await _fila(c, n)
    assert fila["estado"] == "desvinculado" and fila["desvinculado_en"] and fila["es_principal"] is False
    assert (await numeros_whatsapp.listar(c, t)).plazas_usadas == 1
    for cid in (c1, c2):
        conv = await c.fetchrow("SELECT asignado_a, asignado_origen FROM conversations WHERE id = $1", cid)
        assert conv["asignado_a"] == creador and conv["asignado_origen"] == "sistema"
    assert (await c.fetchrow("SELECT asignado_a FROM conversations WHERE id = $1", c3))["asignado_a"] is None
    historial = await c.fetch(
        "SELECT de_portal_user_id, a_portal_user_id, origen FROM conversacion_asignaciones WHERE conversation_id = ANY($1::uuid[])",
        [c1, c2],
    )
    assert len(historial) == 2 and {h["a_portal_user_id"] for h in historial} == {creador}
    assert {h["de_portal_user_id"] for h in historial} == {None, member}
    # Ocultas en las listas del negocio y visibles solo en la lista de ocultas.
    visibles = await c.fetch(
        f"SELECT c.id FROM conversations c WHERE c.tenant_id = $1 AND {conversaciones_ocultas.FILTRO_NO_OCULTA}", t
    )
    assert {v["id"] for v in visibles} == {c3}
    assert {o["id"] for o in await conversaciones_ocultas.listar_ocultas(c, t)} == {c1, c2}
    # Se anota, sin aviso (la pidió la gerencia), y el principal pasa al otro número (regla única).
    e = (await _bitacora(c, t, "desvinculacion"))[0]
    assert e["detalle"]["conversaciones_ocultas"] == 2 and e["autor_tipo"] == "gerencia"
    assert avisos == []
    assert await _principal(c, t) == n2


@requiere_bd
async def test_D23_el_responsable_pasa_a_otro_owner_activo_y_si_no_hay_al_superadmin(bds) -> None:
    c = bds
    t = await _negocio(c)
    creador = await _cuenta_portal(c, t, "owner", dias=30)
    otro = await _cuenta_portal(c, t, "owner", dias=2)
    sup = await _cuenta_portal(c, t, "superadmin", dias=20)
    assert await conversaciones_ocultas.responsable_de_ocultas(c, t) == creador
    await c.execute("UPDATE portal_users SET is_active = false WHERE id = $1", creador)
    assert await conversaciones_ocultas.responsable_de_ocultas(c, t) == otro
    await c.execute("UPDATE portal_users SET is_active = false WHERE id = $1", otro)
    assert await conversaciones_ocultas.responsable_de_ocultas(c, t) == sup
    await c.execute("UPDATE portal_users SET is_active = false WHERE id = $1", sup)
    assert await conversaciones_ocultas.responsable_de_ocultas(c, t) is None


@requiere_bd
async def test_A42_CL23_CL28_los_mensajes_posteriores_quedan_ocultos_y_al_revincular_no_reaparecen(bds) -> None:
    c = bds
    t = await _negocio(c)
    creador = await _cuenta_portal(c, t, "owner")
    n = await numeros_whatsapp.vincular(c, t, "pn-vuelve", AUT)
    cliente = await _cliente(c, t)
    ahora = _ahora()
    await numeros_whatsapp.desvincular(c, t, n["id"], AUT, ahora=ahora)

    # Un mensaje que llega después por ese identificador se guarda oculto, asignado, sin respuesta.
    posterior = await conversaciones_ocultas.conversacion_para_mensaje_oculto(c, t, n["id"], cliente, ahora)
    assert await conversaciones_ocultas.conversacion_para_mensaje_oculto(c, t, n["id"], cliente, ahora) == posterior
    conv = await c.fetchrow("SELECT asignado_a, agente_canal_id FROM conversations WHERE id = $1", posterior)
    assert conv["asignado_a"] == creador and conv["agente_canal_id"] == n["id"]
    assert posterior in {o["id"] for o in await conversaciones_ocultas.listar_ocultas(c, t)}

    # Volver a vincular el número a este negocio no trae las conversaciones de vuelta.
    nuevo = await numeros_whatsapp.vincular(c, t, "pn-vuelve", AUT)
    assert nuevo["id"] != n["id"]
    assert await c.fetchval("SELECT count(*) FROM conversations WHERE agente_canal_id = $1", nuevo["id"]) == 0
    assert posterior in {o["id"] for o in await conversaciones_ocultas.listar_ocultas(c, t)}


@requiere_bd
async def test_CL23_el_numero_revinculado_en_otro_negocio_empieza_sin_conversaciones_y_el_anterior_las_conserva_ocultas(bds) -> None:
    c = bds
    a, b = await _negocio(c), await _negocio(c)
    await _cuenta_portal(c, a, "owner")
    n = await numeros_whatsapp.vincular(c, a, "pn-viaja", AUT)
    conv = await _conversacion(c, a, n["id"])
    await numeros_whatsapp.desvincular(c, a, n["id"], AUT)
    nuevo = await numeros_whatsapp.vincular(c, b, "pn-viaja", AUT)
    assert await c.fetchval("SELECT count(*) FROM conversations WHERE agente_canal_id = $1", nuevo["id"]) == 0
    assert await conversaciones_ocultas.listar_ocultas(c, b) == []
    assert [o["id"] for o in await conversaciones_ocultas.listar_ocultas(c, a)] == [conv]


@requiere_bd
async def test_A41_el_detalle_de_una_oculta_es_de_solo_lectura_y_solo_para_su_negocio(bds) -> None:
    c = bds
    t, otro = await _negocio(c), await _negocio(c)
    await _cuenta_portal(c, t, "owner")
    n = await _numero(c, t)
    oculta = await _conversacion(c, t, n)
    visible = await _conversacion(c, t, await _numero(c, t))
    await c.execute(
        "INSERT INTO messages (tenant_id, conversation_id, role, content) VALUES ($1, $2, 'user', 'hola')", t, oculta
    )
    await numeros_whatsapp.desvincular(c, t, n, AUT)
    detalle = await conversaciones_ocultas.detalle_oculta(c, t, oculta)
    assert detalle is not None and [m["content"] for m in detalle["mensajes"]] == ["hola"]
    assert await conversaciones_ocultas.detalle_oculta(c, otro, oculta) is None  # otro negocio
    assert await conversaciones_ocultas.detalle_oculta(c, t, visible) is None  # no está oculta
    assert await conversaciones_ocultas.detalle_oculta(c, t, uuid4()) is None


@requiere_bd
async def test_A43_las_conversaciones_ocultas_se_eliminan_a_los_12_meses_de_desvincular(bds) -> None:
    c = bds
    t = await _negocio(c, tope=9)
    await _cuenta_portal(c, t, "owner")
    ahora = _ahora()
    viejo = await _numero(c, t, "pn-viejo")
    reciente = await _numero(c, t, "pn-reciente")
    conv_vieja = await _conversacion(c, t, viejo)
    conv_reciente = await _conversacion(c, t, reciente)
    await c.execute(
        "INSERT INTO messages (tenant_id, conversation_id, role, content) VALUES ($1, $2, 'user', 'x')", t, conv_vieja
    )
    await numeros_whatsapp.desvincular(c, t, viejo, AUT, ahora=ahora - timedelta(days=400))
    await numeros_whatsapp.desvincular(c, t, reciente, AUT, ahora=ahora - timedelta(days=30))
    assert await conversaciones_ocultas.eliminar_vencidas(c, ahora) == (1, 1)
    assert await c.fetchval("SELECT count(*) FROM conversations WHERE id = $1", conv_vieja) == 0
    assert await c.fetchval("SELECT count(*) FROM messages WHERE conversation_id = $1", conv_vieja) == 0
    assert await c.fetchval("SELECT count(*) FROM conversations WHERE id = $1", conv_reciente) == 1
    assert await c.fetchval("SELECT count(*) FROM agentes_canal WHERE id = $1", viejo) == 0
    assert await conversaciones_ocultas.eliminar_vencidas(c, ahora) == (0, 0)  # idempotente


# ------------------------------------------------------------
# Configuración del agente de un número (RF-5, RF-7, RF-8; A10, A11, A19, A20, A27, A59, A72)
# ------------------------------------------------------------
@requiere_bd
async def test_A11_la_temperatura_fuera_de_rango_responde_400_sin_cambiar_y_0_y_1_se_aceptan(bds) -> None:
    c = bds
    t = await _negocio(c)
    n = await _numero(c, t)
    antes = float((await _fila(c, n))["temperature"])
    for malo in (-0.1, 1.1):
        await _http(numeros_whatsapp.editar_configuracion(c, t, n, {"temperature": malo}, AUT), 400, "temperatura_fuera_de_rango")
        assert float((await _fila(c, n))["temperature"]) == antes
    for bueno in (0, 1):
        r = await numeros_whatsapp.editar_configuracion(c, t, n, {"temperature": bueno}, AUT)
        assert float(r["temperature"]) == bueno


@requiere_bd
async def test_A10_un_numero_pausado_se_edita_y_conserva_su_configuracion_al_reactivarse(bds) -> None:
    c = bds
    t = await _negocio(c)
    n = await _numero(c, t)
    await numeros_whatsapp.pausar_manual(c, t, n, AUT)
    await numeros_whatsapp.editar_configuracion(c, t, n, {"agent_name": "Editado", "history_window": 11}, AUT)
    r = await numeros_whatsapp.reactivar(c, t, n, AUT)
    assert (r["agent_name"], r["history_window"], r["estado"]) == ("Editado", 11, "activo")


@requiere_bd
async def test_A19_A59_encender_apagar_y_cambiar_el_prompt_quedan_en_la_bitacora_sin_cambiar_la_plaza(bds) -> None:
    c = bds
    t = await _negocio(c)
    n = await _numero(c, t, prompt="Uno.")
    usadas = (await numeros_whatsapp.listar(c, t)).plazas_usadas
    await numeros_whatsapp.editar_configuracion(c, t, n, {"agente_activo": False}, AUT)
    await numeros_whatsapp.editar_configuracion(c, t, n, {"agente_activo": True, "system_prompt": "Dos dos."}, AUT)
    await numeros_whatsapp.editar_configuracion(c, t, n, {"system_prompt": "Dos dos."}, AUT)  # igual: no se anota
    assert [b["motivo"] for b in await _bitacora(c, t)] == ["agente_apagado", "prompt_cambiado", "agente_encendido"]
    prompt = (await _bitacora(c, t, "prompt_cambiado"))[0]
    assert prompt["detalle"] == {"longitud_anterior": 4, "longitud_nueva": 8}
    assert (prompt["autor_tipo"], prompt["autor_portal_user_id"]) == ("gerencia", AUT.portal_user_id)
    assert (await numeros_whatsapp.listar(c, t)).plazas_usadas == usadas
    # Un agente apagado sigue ocupando su plaza y no está incompleto.
    assert (await _fila(c, n))["estado"] == "activo"


@requiere_bd
async def test_A20_CL10_un_activo_que_pasa_a_incompleto_avisa_a_owner_y_superadmin_y_un_pausado_no(bds, avisos) -> None:
    c = bds
    t = await _negocio(c)
    owner = await _cuenta_portal(c, t, "owner")
    sup = await _cuenta_portal(c, t, "superadmin")
    await _cuenta_portal(c, t, "member")
    activo = await _numero(c, t)
    pausado = await _numero(c, t, estado="pausado")
    await numeros_whatsapp.editar_configuracion(c, t, pausado, {"system_prompt": ""}, AUT)
    assert avisos == []
    await numeros_whatsapp.editar_configuracion(c, t, activo, {"system_prompt": "  "}, AUT)
    assert {a.cuenta for a in avisos} == {owner, sup} and len(avisos) == 2
    assert avisos[0].aviso.tipo == "numero_configuracion_incompleta"
    assert {b["motivo"] for b in await _bitacora(c, t)} >= {"configuracion_incompleta"}
    # Se corrige: responde sin alta nueva y se anota.
    await numeros_whatsapp.editar_configuracion(c, t, activo, {"system_prompt": "Listo."}, AUT)
    assert len(await _bitacora(c, t, "configuracion_completada")) == 1
    assert (await _fila(c, activo))["estado"] == "activo"


@requiere_bd
async def test_A27_A72_el_modelo_y_el_historial_se_validan_contra_el_plan_y_un_modelo_nulo_siempre_cabe(bds) -> None:
    c = bds
    t = await _negocio(c, modelos=["claude-haiku-4-5-20251001"], historial=20)
    n = await _numero(c, t)
    await _http(
        numeros_whatsapp.editar_configuracion(c, t, n, {"model": "claude-opus-5"}, AUT), 402, "modelo_no_incluido_en_plan"
    )
    await _http(
        numeros_whatsapp.editar_configuracion(c, t, n, {"history_window": 21}, AUT), 402, "historial_excede_plan"
    )
    r = await numeros_whatsapp.editar_configuracion(c, t, n, {"model": None, "history_window": 20}, AUT)
    assert r["model"] is None and r["history_window"] == 20
    r = await numeros_whatsapp.editar_configuracion(c, t, n, {"model": "claude-haiku-4-5-20251001"}, AUT)
    assert r["model"] == "claude-haiku-4-5-20251001"


# ------------------------------------------------------------
# Herramientas por número y por plan (RF-6, RF-9; A13 a A17, A25, A59)
# ------------------------------------------------------------
@requiere_bd
async def test_A16_una_herramienta_fuera_del_plan_responde_402_y_la_activacion_es_todo_o_nada(bds) -> None:
    c = bds
    t = await _negocio(c, familias=("http",))
    n = await _numero(c, t)
    await _herramienta(c, t, "web", "http_generico")
    await _herramienta(c, t, "hoja", "google_sheets")
    await _herramienta(c, t, "apagada", "http_generico", habilitada=False)
    await _http(
        herramientas_plan.activar_herramientas_de_numero(c, t, n, ["web", "hoja"], AUT), 402, "herramienta_no_incluida_en_plan"
    )
    assert await herramientas_plan.herramientas_de_numero(c, t, n) == []
    await _http(
        herramientas_plan.activar_herramientas_de_numero(c, t, n, ["web", "no_existe"], AUT), 404, "herramienta_no_encontrada"
    )
    await _http(
        herramientas_plan.activar_herramientas_de_numero(c, t, n, ["apagada"], AUT), 404, "herramienta_no_encontrada"
    )
    assert await herramientas_plan.activar_herramientas_de_numero(c, t, n, ["web"], AUT) == ["web"]
    assert await herramientas_plan.activar_herramientas_de_numero(c, t, n, ["web"], AUT) == []  # ya estaba
    assert await herramientas_plan.desactivar_herramientas_de_numero(c, t, n, ["web", "hoja"], AUT) == ["web"]
    assert [b["motivo"] for b in await _bitacora(c, t)] == ["herramienta_activada", "herramienta_desactivada"]
    e = (await _bitacora(c, t, "herramienta_activada"))[0]
    assert (e["detalle"], e["autor_tipo"], e["agente_canal_id"]) == ({"tool_key": "web"}, "gerencia", n)


@requiere_bd
async def test_A13_una_herramienta_nueva_del_negocio_nace_desactivada_en_todos_los_numeros(bds) -> None:
    c = bds
    t = await _negocio(c)
    n = await _numero(c, t)
    await _herramienta(c, t, "vieja")
    await herramientas_plan.activar_herramientas_de_numero(c, t, n, ["vieja"], AUT)
    await _herramienta(c, t, "nueva")
    assert await herramientas_plan.herramientas_de_numero(c, t, n) == ["vieja"]
    cat = {h["tool_key"]: h for h in await herramientas_plan.catalogo(c, t, n)}
    assert cat["vieja"]["activa"] is True and cat["nueva"]["activa"] is False
    assert cat["nueva"]["permitida_por_plan"] is True
    social = {h["tool_key"]: h["activa"] for h in await herramientas_plan.catalogo(c, t, None)}
    assert social == {"vieja": False, "nueva": False}


@requiere_bd
async def test_A14_CL7_quitar_la_herramienta_del_negocio_la_retira_de_todos_los_numeros_y_la_configuracion_sigue_valida(bds) -> None:
    c = bds
    t = await _negocio(c)
    a, b = await _numero(c, t, nombre="A"), await _numero(c, t, nombre="B")
    await _herramienta(c, t, "tool")
    await herramientas_plan.activar_herramientas_de_numero(c, t, a, ["tool"], AUT)
    await herramientas_plan.activar_herramientas_de_numero(c, t, b, ["tool"], AUT)
    await herramientas_plan.activar_herramientas_social(c, t, ["tool"], AUT)
    await c.execute("DELETE FROM tenant_tools WHERE tenant_id = $1 AND tool_key = 'tool'", t)
    assert await herramientas_plan.herramientas_de_numero(c, t, a) == []
    assert await herramientas_plan.herramientas_de_numero(c, t, b) == []
    assert await herramientas_plan.herramientas_sociales(c, t) == []
    assert (await _fila(c, a))["agent_name"] == "A" and (await _fila(c, b))["agent_name"] == "B"


@requiere_bd
async def test_A25_A59_A17_el_plan_que_retira_una_familia_desactiva_la_herramienta_sin_borrar_nada_y_no_vuelve_sola(bds) -> None:
    c = bds
    t = await _negocio(c)
    plan = await _plan_de(c, t)
    p = await _numero(c, t, principal=True, nombre="Pri", dias=5)
    pausado = await _numero(c, t, estado="pausado", dias=2)
    await _herramienta(c, t, "web", "http_generico")
    await _herramienta(c, t, "hoja", "google_sheets")
    for numero in (p, pausado):
        await herramientas_plan.activar_herramientas_de_numero(c, t, numero, ["web", "hoja"], AUT)
    await herramientas_plan.activar_herramientas_social(c, t, ["web", "hoja"], AUT)
    # El principal refresca la referencia del negocio al cambiar sus herramientas.
    ref = await c.fetchrow("SELECT herramientas FROM config_referencia_negocio WHERE tenant_id = $1", t)
    assert list(ref["herramientas"]) == ["hoja", "web"]

    await c.execute("DELETE FROM plan_herramientas WHERE plan_nombre = $1 AND familia = 'google_sheets'", plan)
    retiradas = await herramientas_plan.retirar_herramientas_por_plan(c, t)
    assert sorted(retiradas, key=lambda r: (str(r[0]), r[1])) == sorted(
        [(p, "hoja"), (pausado, "hoja"), (None, "hoja")], key=lambda r: (str(r[0]), r[1])
    )
    assert await herramientas_plan.herramientas_de_numero(c, t, p) == ["web"]
    assert await herramientas_plan.herramientas_de_numero(c, t, pausado) == ["web"]
    assert await herramientas_plan.herramientas_sociales(c, t) == ["web"]
    assert (await _fila(c, p))["agent_name"] == "Pri"  # la configuración no se toca
    ref = await c.fetchrow("SELECT herramientas FROM config_referencia_negocio WHERE tenant_id = $1", t)
    assert list(ref["herramientas"]) == ["web"]
    entradas = await _bitacora(c, t, "herramienta_retirada_por_plan")
    assert len(entradas) == 3 and {e["autor_tipo"] for e in entradas} == {"sistema"}
    assert {e["agente_canal_id"] for e in entradas} == {p, pausado, None}
    # No puede activarse mientras el plan no la incluya...
    await _http(herramientas_plan.activar_herramientas_de_numero(c, t, p, ["hoja"], AUT), 402, "herramienta_no_incluida_en_plan")
    # ... idempotente, y cuando el plan vuelve a incluirla ningún número la recupera solo.
    assert await herramientas_plan.retirar_herramientas_por_plan(c, t) == []
    await c.execute("INSERT INTO plan_herramientas (plan_nombre, familia) VALUES ($1, 'google_sheets')", plan)
    assert await herramientas_plan.retirar_herramientas_por_plan(c, t) == []
    assert await herramientas_plan.herramientas_de_numero(c, t, p) == ["web"]


@requiere_bd
async def test_A17_las_herramientas_de_instagram_y_facebook_siguen_las_mismas_reglas_del_plan(bds) -> None:
    c = bds
    t = await _negocio(c, familias=("http",))
    await _herramienta(c, t, "web", "http_generico")
    await _herramienta(c, t, "doc", "google_docs")
    await _http(
        herramientas_plan.activar_herramientas_social(c, t, ["doc"], AUT), 402, "herramienta_no_incluida_en_plan"
    )
    assert await herramientas_plan.activar_herramientas_social(c, t, ["web"], AUT) == ["web"]
    assert await herramientas_plan.desactivar_herramientas_social(c, t, ["web"], AUT) == ["web"]
    assert [b["agente_canal_id"] for b in await _bitacora(c, t)] == [None, None]


@requiere_bd
async def test_RF9_una_prueba_sin_plan_asociado_puede_activar_cualquier_familia(bds) -> None:
    c = bds
    t = await _tenant(c)
    await _estado_plataforma(c, t, "prueba")
    assert await herramientas_plan.familias_aplicables(c, t) == frozenset(FAMILIAS_HERRAMIENTAS)


@requiere_bd
@pytest.mark.parametrize(
    ("tipo", "clave"),
    [
        ("http_post_json", "crear_reserva"),
        ("http_generico", "consultar_servicios"),
        ("sql_lectura", "inv"),
        ("http_generico", "web"),
        ("http_post_json", "enviar"),
        ("google_sheets", "h"),
        ("google_docs", "d"),
        ("raro", "x"),
    ],
)
async def test_RF9_familia_de_python_y_la_funcion_sql_dan_lo_mismo(bds, tipo, clave) -> None:
    sql = await bds.fetchval("SELECT familia_de_herramienta($1, $2)", tipo, clave)
    assert sql == herramientas_plan.familia_de(tipo, clave)


# ------------------------------------------------------------
# Lista de números y avisos agrupados (P17, RF-20; A56, A57)
# ------------------------------------------------------------
@requiere_bd
async def test_P17_la_lista_trae_tope_plazas_usadas_y_libres_sin_contar_pausados_ni_la_linea_de_avisos(bds) -> None:
    c = bds
    t = await _negocio(c, tope=3)
    await _numero(c, t, principal=True)
    await _numero(c, t, estado="pausado")
    await _numero(c, t, avisos=True)
    await _numero(c, t, estado="desvinculado")
    lista = await numeros_whatsapp.listar(c, t)
    assert (lista.tope, lista.plazas_usadas, lista.plazas_libres) == (3, 1, 2)
    assert len(lista.numeros) == 3  # el desvinculado ya no es del negocio
    assert {n["estado"] for n in lista.numeros} == {"activo", "pausado"}
    assert all("configuracion_incompleta" in n and "herramientas" in n for n in lista.numeros)


@requiere_bd
async def test_P17_sin_tope_las_plazas_libres_son_nulas(bds) -> None:
    c = bds
    t = await _tenant(c)
    await _estado_plataforma(c, t, "prueba")
    lista = await numeros_whatsapp.listar(c, t)
    assert lista.tope is None and lista.plazas_libres is None


@requiere_bd
async def test_A56_A57_el_aviso_agrupado_sale_a_owner_y_superadmin_activos_con_todos_los_numeros_pausados_incluidos(bds, avisos) -> None:
    c = bds
    t = await _negocio(c)
    owner = await _cuenta_portal(c, t, "owner")
    sup = await _cuenta_portal(c, t, "superadmin")
    await _cuenta_portal(c, t, "member")
    await _cuenta_portal(c, t, "owner", activa=False)
    await _cuenta_portal(c, t, "vendedor")
    activo = await _numero(c, t, "pn-111122")
    pausado = await _numero(c, t, "pn-333344", estado="pausado")
    filas = [await _fila(c, activo), await _fila(c, pausado)]
    assert await numeros_avisos.avisar_recorte(c, t, filas) == 2
    assert {a.cuenta for a in avisos} == {owner, sup}
    uno = avisos[0].aviso
    assert uno.tipo == "numeros_recortados"
    assert [n["estado"] for n in uno.datos["numeros"]] == ["activo", "pausado"]  # una sola alerta por cuenta, agrupada
    assert "…1122" in uno.mensaje and "…3344" in uno.mensaje

    avisos.clear()
    assert await numeros_avisos.avisar_pausa(c, t, [filas[1]], "pausa_por_cupo_bajado_por_plataforma") == 2
    assert avisos[0].aviso.tipo == "numeros_pausados" and "…3344" in avisos[0].aviso.mensaje


@requiere_bd
async def test_RF20_un_fallo_al_emitir_no_rompe_la_accion_ni_corta_los_demas_avisos(bds, monkeypatch) -> None:
    c = bds
    t = await _negocio(c)
    await _cuenta_portal(c, t, "owner", dias=2)
    sup = await _cuenta_portal(c, t, "superadmin", dias=1)
    recibidos: list[UUID] = []

    async def a_veces(tenant_id, aviso, portal_user_id):
        if portal_user_id != sup:
            raise RuntimeError("push caído")
        recibidos.append(portal_user_id)

    monkeypatch.setattr(numeros_avisos, "_emitir", a_veces)
    n = await _numero(c, t, estado="pausado", prompt="")
    r = await numeros_whatsapp.reactivar(c, t, n, AUT)  # no lanza
    assert r["estado"] == "activo" and recibidos == [sup]


def test_A41_el_detalle_de_una_oculta_devuelve_sus_mensajes() -> None:
    from datetime import datetime, timezone

    from schemas import ConversacionOcultaDetalleOut, MensajeOut

    cabecera = {"id": uuid4(), "channel_type": "whatsapp", "status": "active"}
    mensaje = {"id": uuid4(), "role": "user", "content": "Hola", "created_at": datetime.now(timezone.utc)}
    salida = ConversacionOcultaDetalleOut(**cabecera, mensajes=[MensajeOut(**mensaje)])
    assert [m.content for m in salida.mensajes] == ["Hola"]
    assert "mensajes" in salida.model_dump()
