"""
Migraciones 56 a 59 (spec 005a, T2): varios números de WhatsApp por negocio.

Dos partes:

1. ESTRUCTURALES (siempre corren, sin base de datos). Leen los .sql y comprueban
   que sean idempotentes, que lleven las garantías que los servicios dan por
   hechas (unicidad parcial, un solo principal, cascadas, motivos cerrados) y
   que su lógica tenga la misma forma que las reglas puras
   (services/numeros_reglas.py).

2. CON BASE DE DATOS (marcadas `requiere_bd`). Solo corren si se pide con la
   variable NUMEROS_SQL_TEST_DATABASE_URL, que debe apuntar a una Postgres DE
   PRUEBA con las migraciones 00 a 55 aplicadas. Cada test abre su propia
   conexión, aplica los cuatro archivos DENTRO de una transacción y la revierte
   al terminar: la base queda como estaba. Por seguridad se niegan a correr
   contra la misma base que el DATABASE_URL del .env (la del backend), salvo que
   se pida expresamente con NUMEROS_SQL_TEST_PERMITIR_BD_DE_ENV=1. Nunca se
   usan contra producción.

Cada test lleva en el nombre el criterio de aceptación (A), el requisito (RF) o
el caso límite (CL) de la spec que cubre.
"""

import os
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
import pytest

from services.numeros_reglas import (
    FAMILIAS_HERRAMIENTAS,
    PlanCatalogo,
    Situacion,
    Suscripcion,
    plan_mas_bajo_activo,
    reglas_de_negocio,
    situacion_comercial,
)
from test_numeros_reglas import AHORA, FUTURO, PASADO, PLANES

BACKEND = Path(__file__).resolve().parent.parent
DIR_SQL = BACKEND / "sql"

ARCHIVOS = {
    56: "56_numeros_whatsapp.sql",
    57: "57_planes_cupo_herramientas.sql",
    58: "58_numeros_limites_y_resolucion.sql",
    59: "59_numeros_reevaluacion.sql",
    61: "61_numeros_recortes_pendientes.sql",
}

ARCHIVO_ALERTAS = "60_alertas_numeros.sql"

# Motivos de bitácora del negocio: sección "Contrato de códigos" de la spec.
MOTIVOS_BITACORA = {
    "vinculacion", "desvinculacion", "pausa_manual", "reactivacion_manual",
    "pausa_por_cambio_de_plan", "pausa_por_fin_de_suscripcion",
    "pausa_por_fin_de_prueba", "pausa_por_pago_de_prueba", "pausa_por_despliegue",
    "pausa_por_cupo_bajado_por_plataforma", "pausa_por_plan_mas_bajo_recalculado",
    "cambio_de_principal", "principal_sin_numero_activo", "configuracion_incompleta",
    "configuracion_completada", "recorte_por_plan", "herramienta_retirada_por_plan",
    "herramienta_activada", "herramienta_desactivada", "prompt_cambiado",
    "agente_encendido", "agente_apagado",
}

# Motivos con los que los triggers de 59 encolan (RF-11, siete casos).
MOTIVOS_COLA = {
    "cambio_de_plan", "fin_de_suscripcion", "fin_de_prueba", "cupo_plataforma",
    "plan_mas_bajo", "pago_de_prueba", "herramientas_del_plan", "reactivacion_negocio",
}


def _leer(numero: int) -> str:
    return (DIR_SQL / ARCHIVOS[numero]).read_text(encoding="utf-8")


def _sin_comentarios(sql: str) -> str:
    return "\n".join(l.split("--")[0] for l in sql.splitlines())


def _norm(sql: str) -> str:
    """Minúsculas, espacios colapsados y sin espacio alrededor de '(', antes de ')' ni alrededor de ','."""
    t = _sin_comentarios(sql).lower()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s*\(\s*", "(", t)
    t = re.sub(r"\s+\)", ")", t)
    t = re.sub(r"\s*,\s*", ",", t)
    return t


def _n(texto: str) -> str:
    """Normaliza un fragmento esperado igual que _norm."""
    return _norm(texto)


def _funcion(sql: str, nombre: str) -> str:
    """Texto (normalizado) de una función: desde su CREATE hasta el siguiente objeto."""
    t = _sin_comentarios(sql)
    m = re.search(rf"CREATE OR REPLACE FUNCTION\s+(?:public\.)?{nombre}\s*\(", t, re.I)
    assert m, f"no se encontró la función {nombre}"
    resto = t[m.start():]
    fin = re.search(
        r"\n\s*(CREATE OR REPLACE FUNCTION|CREATE TRIGGER|DROP TRIGGER|CREATE TABLE|CREATE UNIQUE INDEX|CREATE INDEX|DO \$\$)",
        resto[10:],
        re.I,
    )
    cuerpo = resto if not fin else resto[: fin.start() + 10]
    return _norm(cuerpo)


# ============================================================
# 1. ESTRUCTURALES
# ============================================================
@pytest.mark.parametrize("numero", sorted(ARCHIVOS))
def test_RF22_el_archivo_existe(numero: int) -> None:
    assert (DIR_SQL / ARCHIVOS[numero]).is_file()


def test_RF22_56_es_la_siguiente_migracion_libre_y_no_hay_duplicadas() -> None:
    # La 54 y la 55 existen; la 56 a la 59 y la 61 son de esta spec y no se repiten (la 60 tiene su propio test).
    # La 62 es de la spec 006 (refresh en cookie HttpOnly), ajena a esta.
    # La 63 es de las plantillas de WhatsApp por negocio, ajena a esta.
    anteriores = {int(p.name.split("_")[0]) for p in DIR_SQL.glob("[0-9][0-9]_*.sql")}
    assert {54, 55} <= anteriores
    for numero in ARCHIVOS:
        assert len(list(DIR_SQL.glob(f"{numero}_*.sql"))) == 1
    # La 64 es del modo coexistencia de WhatsApp, ajena a esta.
    assert max(anteriores) == 64


@pytest.mark.parametrize("numero", sorted(ARCHIVOS))
def test_RF22_es_idempotente(numero: int) -> None:
    sql = _sin_comentarios(_leer(numero))
    for m in re.finditer(r"CREATE\s+(UNIQUE\s+)?(TABLE|INDEX)\s+(?!IF NOT EXISTS)", sql, re.I):
        pytest.fail(f"CREATE sin IF NOT EXISTS en {ARCHIVOS[numero]}: {m.group(0)}")
    for m in re.finditer(r"CREATE\s+FUNCTION\b", sql, re.I):
        pytest.fail(f"CREATE FUNCTION sin OR REPLACE en {ARCHIVOS[numero]}")
    for m in re.finditer(r"CREATE\s+TRIGGER\s+(\w+)", sql, re.I):
        previo = re.search(rf"DROP TRIGGER IF EXISTS\s+{m.group(1)}\b", sql, re.I)
        assert previo, f"CREATE TRIGGER {m.group(1)} sin DROP TRIGGER IF EXISTS previo"
    for m in re.finditer(r"\bDROP\s+CONSTRAINT\s+(?!IF EXISTS)", sql, re.I):
        pytest.fail(f"DROP CONSTRAINT sin IF EXISTS en {ARCHIVOS[numero]}")
    for m in re.finditer(r"ADD\s+COLUMN\s+(?!IF NOT EXISTS)", sql, re.I):
        pytest.fail(f"ADD COLUMN sin IF NOT EXISTS en {ARCHIVOS[numero]}")
    assert not re.search(r"\bDROP\s+TABLE\b", sql, re.I)


@pytest.mark.parametrize("numero", sorted(ARCHIVOS))
def test_RF22_ninguna_migracion_borra_datos(numero: int) -> None:
    """Las migraciones no borran ni vacían nada: reaplicarlas no puede dañar datos."""
    sql = _sin_comentarios(_leer(numero))
    assert not re.search(r"\bTRUNCATE\b", sql, re.I)
    assert not re.search(r"\bDELETE\s+FROM\b", sql, re.I)
    assert not re.search(r"\bDROP\s+(COLUMN|SCHEMA|DATABASE)\b", sql, re.I)


@pytest.mark.parametrize("numero", [57, 59])
def test_RF22_toda_siembra_o_copia_esta_protegida(numero: int) -> None:
    """Cada INSERT de 57 y 59 se puede reaplicar: ON CONFLICT, NOT EXISTS o dentro del bucle con CONTINUE WHEN."""
    sql = _sin_comentarios(_leer(numero))
    inserts = list(re.finditer(r"INSERT\s+INTO\s+(\w+)", sql, re.I))
    assert inserts
    for m in inserts:
        sentencia = sql[m.start(): sql.index(";", m.start())]
        previo = sql[: m.start()]
        en_bucle_protegido = "LOOP" in previo and "CONTINUE WHEN" in previo[previo.rindex("LOOP"):]
        en_bloque_con_marca = "IF NOT EXISTS (SELECT 1 FROM numeros_despliegue" in previo[-400:]
        assert (
            "ON CONFLICT" in sentencia.upper()
            or "NOT EXISTS" in sentencia.upper()
            or en_bucle_protegido
            or en_bloque_con_marca
        ), f"INSERT sin protección en {ARCHIVOS[numero]}: {sentencia[:80]!r}"


# ------------------------------------------------------------
# 56: modelo de datos (RF-3, RF-6, RF-16, RF-21)
# ------------------------------------------------------------
def test_RF3_RF16_un_phone_number_id_vinculado_solo_en_un_negocio() -> None:
    sql = _norm(_leer(56))
    assert _n(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agentes_canal_numero_vinculado "
        "ON agentes_canal (phone_number_id) WHERE estado IN ('activo', 'pausado')"
    ) in sql


def test_A64_un_solo_principal_por_negocio() -> None:
    sql = _norm(_leer(56))
    assert _n(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agentes_canal_principal "
        "ON agentes_canal (tenant_id) WHERE es_principal"
    ) in sql


def test_A64_CL31_el_principal_es_activo_no_es_linea_de_avisos_y_esta_completo() -> None:
    sql = _norm(_leer(56))
    assert _n(
        "CHECK (es_principal = false OR (estado = 'activo' AND es_linea_avisos = false))"
    ) in sql
    assert _n(
        "CHECK (es_principal = false OR NOT configuracion_incompleta(agent_name, system_prompt))"
    ) in sql
    # La función de la que depende el CHECK se crea ANTES que la tabla.
    crudo = _sin_comentarios(_leer(56))
    assert crudo.index("FUNCTION configuracion_incompleta") < crudo.index("TABLE IF NOT EXISTS agentes_canal")


def test_RF8_la_funcion_de_configuracion_incompleta_cubre_prompt_y_nombre() -> None:
    f = _funcion(_leer(56), "configuracion_incompleta")
    assert "coalesce(p_system_prompt,'') ~ '^[[:space:]]*$'" in f
    assert "coalesce(p_agent_name,'') ~ '^[[:space:]]*$'" in f
    assert " or " in f
    assert "immutable" in f


def test_RF3_estados_y_proveedores_cerrados() -> None:
    sql = _norm(_leer(56))
    assert _n("CHECK (estado IN ('activo', 'pausado', 'desvinculado'))") in sql
    assert _n("CHECK (bsp_provider IN ('meta', 'neuroapi', 'kontesta'))") in sql
    assert _n("CHECK (temperature BETWEEN 0 AND 1)") in sql
    assert _n("CHECK (estado <> 'desvinculado' OR desvinculado_en IS NOT NULL)") in sql


def test_RF5_la_configuracion_del_numero_tiene_los_campos_del_agente() -> None:
    sql = _leer(56)
    cuerpo = sql[sql.index("CREATE TABLE IF NOT EXISTS agentes_canal"):]
    cuerpo = cuerpo[: cuerpo.index(");\n")]
    for col in ("agent_name", "system_prompt", "model", "temperature", "history_window", "agente_activo"):
        assert re.search(rf"^\s+{col}\s", cuerpo, re.M), col


def test_A14_CL7_borrar_una_herramienta_del_negocio_la_quita_de_todos_los_numeros_y_de_social() -> None:
    sql = _norm(_leer(56))
    # Las dos tablas de activación cuelgan de tenant_tools en cascada.
    assert sql.count("references tenant_tools(tenant_id,tool_key) on delete cascade") == 2
    # Y el número y el negocio de cada fila coinciden (FK compuesta a agentes_canal).
    assert "foreign key(agente_canal_id,tenant_id) references agentes_canal(id,tenant_id) on delete cascade" in sql
    assert "constraint agentes_canal_id_tenant_uq unique(id,tenant_id)" in sql


def test_RF21_los_motivos_de_la_bitacora_son_los_de_la_spec_y_los_autores_cerrados() -> None:
    sql = _sin_comentarios(_leer(56))
    bloque = sql[sql.index("numeros_bitacora_motivo_check"):]
    bloque = bloque[: bloque.index(";")]
    assert set(re.findall(r"'([a-z_]+)'", bloque)) == MOTIVOS_BITACORA
    n = _norm(sql)
    assert _n("CHECK (autor_tipo IN ('gerencia', 'plataforma', 'sistema'))") in n
    # El número puede borrarse (retención): la fila de bitácora se conserva.
    assert "agente_canal_id uuid references agentes_canal(id) on delete set null" in n


def test_RF21_indices_de_bitacora_e_intentos() -> None:
    sql = _norm(_leer(56))
    assert "on numeros_bitacora(tenant_id,creada_en desc)" in sql
    assert "on intentos_alta_ajena(tenant_id,creado_en desc)" in sql


def test_RF13_la_conversacion_se_asocia_al_numero() -> None:
    sql = _norm(_leer(56))
    assert (
        "alter table conversations add column if not exists agente_canal_id uuid "
        "references agentes_canal(id) on delete set null"
    ) in sql
    assert "on conversations(tenant_id,agente_canal_id,status)" in sql


def test_RF6_las_tablas_nuevas_de_56_existen() -> None:
    sql = _norm(_leer(56))
    for tabla in (
        "agentes_canal", "config_referencia_negocio", "agente_canal_herramientas",
        "social_herramientas", "numeros_bitacora", "intentos_alta_ajena",
    ):
        assert f"create table if not exists {tabla}(" in sql, tabla


def test_56_no_toca_channel_credentials_ni_tenant_channels() -> None:
    sql = _sin_comentarios(_leer(56))
    assert not re.search(r"\b(ALTER TABLE|UPDATE|INSERT INTO)\s+(channel_credentials|tenant_channels)\b", sql, re.I)


# ------------------------------------------------------------
# 57: cupo y herramientas por plan (RF-1, RF-9)
# ------------------------------------------------------------
def test_RF1_A65_el_cupo_es_nulo_por_defecto_y_si_se_fija_es_al_menos_uno() -> None:
    sql = _norm(_leer(57))
    assert "alter table planes add column if not exists max_numeros_whatsapp smallint;" in sql
    assert _n("CHECK (max_numeros_whatsapp IS NULL OR max_numeros_whatsapp >= 1)") in sql
    # Los planes existentes arrancan sin tope: la migración no fija ningún valor.
    assert not re.search(r"UPDATE\s+planes", _sin_comentarios(_leer(57)), re.I)


def test_RF9_las_familias_de_la_tabla_son_las_del_nucleo_puro() -> None:
    sql = _sin_comentarios(_leer(57))
    bloque = sql[sql.index("plan_herramientas_familia_check"):]
    bloque = bloque[: bloque.index(";")]
    assert set(re.findall(r"'([a-z_]+)'", bloque)) == set(FAMILIAS_HERRAMIENTAS)


def test_RF9_la_lista_del_plan_cuelga_del_plan_y_sigue_su_renombre() -> None:
    sql = _norm(_leer(57))
    assert "references planes(nombre) on update cascade on delete cascade" in sql
    assert "primary key(plan_nombre,familia)" in sql


def test_RF9_la_siembra_parte_de_los_flags_actuales_y_solo_si_la_tabla_esta_vacia() -> None:
    sql = _norm(_leer(57))
    assert "where p.calendario_activo" in sql
    assert "where p.herramientas_activo" in sql
    assert "where not exists(select 1 from plan_herramientas)" in sql
    # herramientas_activo -> las otras cuatro familias.
    assert "('sql_lectura'),('http'),('google_sheets'),('google_docs')" in sql


def test_RF9_la_familia_de_cada_herramienta_coincide_con_la_del_calendario() -> None:
    """Las claves fijas del calendario de SQL son exactamente las de services/herramientas_calendario."""
    from services.herramientas_calendario import claves

    f = _funcion(_leer(57), "familia_de_herramienta")
    en_sql = set(re.findall(r"'([a-z_]+)'", f[f.index("p_tool_key in("): f.index(")", f.index("p_tool_key in("))]))
    assert en_sql == set(claves())
    assert "when p_tool_type = 'sql_lectura' then 'sql_lectura'" in f
    assert "when p_tool_type in('http_generico','http_post_json') then 'http'" in f
    assert "when p_tool_type = 'google_sheets' then 'google_sheets'" in f
    assert "when p_tool_type = 'google_docs' then 'google_docs'" in f
    assert "immutable" in f


# ------------------------------------------------------------
# 58: reglas efectivas, recorte y resolución (RF-9 a RF-11, RF-16)
# ------------------------------------------------------------
def test_A23_A75_la_situacion_comercial_evalua_prueba_antes_que_suscripcion() -> None:
    f = _funcion(_leer(58), "situacion_comercial")
    assert "p_ahora timestamptz default now()" in f
    pos_piloto = f.index("c.estado_plataforma = 'prueba' then 'prueba'")
    pos_prueba_pagada = f.index("c.vigente and c.origen = 'prueba' then 'prueba'")
    pos_susc = f.index("when c.vigente then 'suscripcion'")
    assert pos_piloto < pos_prueba_pagada < pos_susc
    assert "else 'sin_suscripcion'" in f


def test_A30_una_activa_con_la_fecha_pasada_no_es_vigente() -> None:
    f = _funcion(_leer(58), "situacion_comercial")
    assert "s.estado = 'activa'" in f
    assert "(s.fecha_renovacion is null or s.fecha_renovacion >= p_ahora)" in f


def test_A28_el_plan_mas_bajo_ordena_como_el_nucleo_puro() -> None:
    f = _funcion(_leer(58), "plan_mas_bajo_activo")
    assert "where p.activo" in f
    orden = f[f.index("order by"):]
    # precio, luego sin tope como mayor, luego menor cupo, luego nombre (orden por código).
    assert orden.index("p.precio_monthly") < orden.index("p.max_numeros_whatsapp is null") \
        < orden.index("coalesce(p.max_numeros_whatsapp,0)") < orden.index('p.nombre collate "c"')
    assert "limit 1" in orden


def test_A22_A24_A28_los_limites_efectivos_siguen_el_orden_de_las_reglas() -> None:
    f = _funcion(_leer(58), "limites_efectivos")
    assert "p_ahora timestamptz default now()" in f
    # prueba: sin tope; suscripción y sin suscripción: el cupo del plan.
    assert "case when v_situacion = 'prueba' then null else v_cupo end" in f
    # sin suscripción: el plan más bajo activo; sin ninguno, sin tope ni límites y marcado.
    assert "v_asociado := plan_mas_bajo_activo()" in f
    assert "return query select null::text[],null::int,null::int,v_todas,null::text,true" in f
    # prueba sin plan asociado: catálogo completo y sin límites, sin marcar.
    assert "return query select null::text[],null::int,null::int,v_todas,null::text,false" in f
    # El plan contratado se lee de planes aunque esté desactivado (D20): no se filtra por activo.
    leido = f[f.index("from planes p where p.nombre = v_asociado"):][:60]
    assert "p.activo" not in leido
    # Una lista vacía de modelos equivale a sin restricción (como `plan.modelos or None`).
    assert "cardinality(p.modelos_permitidos) = 0 then null" in f


def test_A22_el_catalogo_completo_de_limites_efectivos_es_el_de_las_cinco_familias() -> None:
    f = _funcion(_leer(58), "limites_efectivos")
    m = re.search(r"v_todas text\[\] := array\[(.*?)\]", f)
    assert m
    assert set(re.findall(r"'([a-z_]+)'", m.group(1))) == set(FAMILIAS_HERRAMIENTAS)


def test_A26_A27_el_recorte_solo_baja_y_cubre_agente_referencia_y_numeros() -> None:
    f = _funcion(_leer(58), "recortar_agente_a_plan")
    for tabla in ("tenant_agent_config", "config_referencia_negocio", "agentes_canal"):
        assert f"update {tabla} set" in f, tabla
    assert f.count("history_window = least(history_window,coalesce(v_maximo,history_window))") == 3
    # Modelo retirado o nulo -> el default del plan (el primero); nunca otro valor.
    assert f.count("then v_modelos[1]") == 3
    # Activos y pausados; los desvinculados quedan como están.
    assert "estado in('activo','pausado')" in f
    # No toca el prompt: un modelo retirado no deja el número incompleto (RF-10).
    assert "system_prompt" not in f
    # Sin límites no hace nada.
    assert "if v_modelos is null and v_maximo is null then return" in f


def test_A26_el_recorte_ya_no_exceptua_pilotos_ni_negocios_sin_suscripcion() -> None:
    """El de la 45 se salía con un piloto o sin suscripción; el nuevo lo decide limites_efectivos."""
    f = _funcion(_leer(58), "recortar_agente_a_plan")
    assert "limites_efectivos(p_tenant)" in f
    assert "tenant_estado_plataforma" not in f
    assert "tenant_subscriptions" not in f


def test_A26_los_triggers_de_recorte_se_recrean_con_las_columnas_nuevas() -> None:
    sql = _norm(_leer(58))
    assert _n(
        "CREATE TRIGGER recortar_agente_suscripcion AFTER INSERT OR UPDATE OF plan, estado, origen, "
        "fecha_renovacion ON tenant_subscriptions"
    ) in sql
    assert _n(
        "CREATE TRIGGER recortar_agente_estado AFTER INSERT OR UPDATE OF estado ON tenant_estado_plataforma"
    ) in sql
    assert _n(
        "CREATE TRIGGER recortar_agente_plan AFTER UPDATE OF modelos_permitidos, historial_maximo, "
        "precio_monthly, activo, max_numeros_whatsapp ON planes"
    ) in sql
    assert _n("CREATE TRIGGER recortar_agente_plan_alta_baja AFTER INSERT OR DELETE ON planes") in sql
    # Un cambio del plan más bajo recorta a los negocios sin suscripción vigente.
    f = _funcion(_leer(58), "trg_recortar_agente_plan")
    assert "recortar_agentes_sin_suscripcion()" in f
    assert "new.nombre = plan_mas_bajo_activo()" in f


def test_RF16_resuelve_tenant_busca_el_whatsapp_en_agentes_canal_entre_los_vinculados() -> None:
    f = _funcion(_leer(58), "resuelve_tenant")
    assert "from agentes_canal a" in f
    assert "p_channel = 'whatsapp'" in f
    assert "a.phone_number_id = p_identifier" in f
    assert "a.estado in('activo','pausado')" in f
    # Los demás canales siguen igual; el identificador vacío no resuelve.
    assert "p_channel <> 'whatsapp'" in f
    assert "from channel_credentials c" in f
    assert f.count("nullif(p_identifier,'') is not null") == 2
    # La firma no cambia (la llama el agente y, de momento, n8n).
    assert "p_channel character varying" in f and "returns uuid" in f


def test_RF16_resuelve_numero_prefiere_el_vinculado_y_si_no_el_ultimo_dueno() -> None:
    f = _funcion(_leer(58), "resuelve_numero")
    assert "returns table(agente_canal_id uuid,tenant_id uuid,estado text)" in f
    assert f.count("a.phone_number_id = p_identifier") == 2
    assert "union all" in f
    # El desvinculado solo si no hay uno vinculado, el más reciente primero.
    assert "a.estado = 'desvinculado'" in f
    assert "not exists(select 1 from agentes_canal v where v.phone_number_id = p_identifier" in f
    assert "order by a.desvinculado_en desc,a.id limit 1" in f


# ------------------------------------------------------------
# 59: cola de reevaluación y migración de datos (RF-11, RF-22)
# ------------------------------------------------------------
def test_RF11_la_cola_y_la_marca_de_despliegue_existen_con_su_indice_parcial() -> None:
    sql = _norm(_leer(59))
    assert "create table if not exists numeros_reevaluacion_cola(" in sql
    assert "create table if not exists numeros_despliegue(" in sql
    assert "procesada_en timestamptz" in sql
    assert "on numeros_reevaluacion_cola(creada_en) where procesada_en is null" in sql
    assert "clave varchar(60) primary key" in sql


def test_RF11_los_triggers_de_cola_cubren_las_cuatro_tablas() -> None:
    sql = _norm(_leer(59))
    assert _n(
        "CREATE TRIGGER numeros_encolar_suscripcion AFTER INSERT OR UPDATE OF plan, estado, origen, "
        "fecha_renovacion, cancela_al_vencer ON tenant_subscriptions"
    ) in sql
    assert _n(
        "CREATE TRIGGER numeros_encolar_estado_plataforma AFTER UPDATE OF estado ON tenant_estado_plataforma"
    ) in sql
    assert _n(
        "CREATE TRIGGER numeros_encolar_plan AFTER UPDATE OF max_numeros_whatsapp, precio_monthly, activo, "
        "modelos_permitidos, historial_maximo ON planes"
    ) in sql
    assert _n("CREATE TRIGGER numeros_encolar_plan_alta_baja AFTER INSERT OR DELETE ON planes") in sql
    assert _n(
        "CREATE TRIGGER numeros_encolar_plan_herramientas AFTER INSERT OR DELETE ON plan_herramientas"
    ) in sql


def test_RF11_los_motivos_de_la_cola_son_los_documentados() -> None:
    sql = _sin_comentarios(_leer(59))
    usados = set(re.findall(r"(?:v_motivo := |THEN |ELSE |encolar_reevaluacion\([^,]+, |encolar_por_plan\([^,]+, )'([a-z_]+)'", sql))
    usados -= {"activo", "prueba", "pago"}
    assert usados <= MOTIVOS_COLA
    # Los siete casos de RF-11 están representados.
    assert {"cambio_de_plan", "fin_de_suscripcion", "fin_de_prueba", "cupo_plataforma",
            "plan_mas_bajo", "pago_de_prueba", "herramientas_del_plan"} <= usados


def test_CL2_los_triggers_solo_encolan_no_pausan_ni_reactivan() -> None:
    sql = _sin_comentarios(_leer(59))
    assert not re.search(r"UPDATE\s+agentes_canal", sql, re.I)
    assert not re.search(r"estado\s*=\s*'pausado'", sql, re.I)


def test_P8_los_negocios_suspendidos_o_de_baja_no_se_encolan() -> None:
    f = _funcion(_leer(59), "numeros_encolar_reevaluacion")
    assert "in('suspendido','baja')" in f
    # Sin duplicar una entrada pendiente del mismo motivo.
    assert "q.procesada_en is null" in f


def test_RF11_caso6_una_prueba_que_pasa_a_pago_usa_su_propio_motivo() -> None:
    f = _funcion(_leer(59), "trg_numeros_encolar_suscripcion")
    assert "old.origen = 'prueba' and new.origen = 'pago' then v_motivo := 'pago_de_prueba'" in f
    # La cancelación programada (cambia cancela_al_vencer, sigue activa) no encola.
    assert "cancela_al_vencer" not in f


def test_RF22_CL27_la_migracion_de_datos_solo_toca_negocios_sin_numeros_y_canales_activos() -> None:
    sql = _norm(_leer(59))
    assert "continue when exists(select 1 from agentes_canal a where a.tenant_id = v_cred.tenant_id)" in sql
    assert "where cc.channel_type = 'whatsapp' and cc.is_active and nullif(cc.phone_number_id,'') is not null" in sql
    # Un identificador ya vinculado a otro negocio se salta (no rompe el índice único).
    assert "a.phone_number_id = v_cred.phone_number_id and a.estado in('activo','pausado')" in sql
    # Un solo número por identificador aunque dos negocios lo compartieran.
    assert "select distinct on(cc.phone_number_id)" in sql


def test_RF22_CL17_CL27_principal_solo_con_configuracion_completa() -> None:
    sql = _norm(_leer(59))
    assert "not configuracion_incompleta(c.agent_name,c.system_prompt)" in sql
    # Un número sin prompt o sin nombre queda activo (no pausado ni desvinculado).
    assert "'activo',not configuracion_incompleta" in sql


def test_RF22_el_agente_apagado_se_conserva_y_la_temperatura_se_acota() -> None:
    sql = _norm(_leer(59))
    assert "coalesce(c.is_active,true)" in sql
    assert "least(greatest(coalesce(c.temperature,0.7),0),1)" in sql


def test_RF22_la_referencia_se_copia_solo_del_principal() -> None:
    sql = _norm(_leer(59))
    assert "where a.id = v_numero and a.es_principal on conflict(tenant_id) do nothing" in sql


def test_RF22_las_conversaciones_de_whatsapp_se_asocian_al_numero_migrado() -> None:
    sql = _norm(_leer(59))
    assert (
        "update conversations set agente_canal_id = v_numero where tenant_id = v_cred.tenant_id "
        "and channel_type = 'whatsapp' and agente_canal_id is null"
    ) in sql


def test_RF22_las_herramientas_habilitadas_se_copian_y_social_se_siembra_una_vez() -> None:
    sql = _norm(_leer(59))
    assert "where t.tenant_id = v_cred.tenant_id and t.is_enabled = true" in sql
    assert "if not exists(select 1 from numeros_despliegue where clave = 'siembra_social_herramientas')" in sql
    assert "insert into numeros_despliegue(clave) values('siembra_social_herramientas')" in sql


def test_RF22_la_migracion_no_modifica_las_tablas_de_credenciales_ni_la_config_actual() -> None:
    sql = _sin_comentarios(_leer(59))
    assert not re.search(
        r"\b(UPDATE|INSERT INTO|ALTER TABLE)\s+(channel_credentials|tenant_channels|tenant_agent_config)\b", sql, re.I
    )


def test_RF22_los_planes_existentes_arrancan_sin_tope() -> None:
    for numero in ARCHIVOS:
        assert not re.search(r"max_numeros_whatsapp\s*=\s*\d", _sin_comentarios(_leer(numero)))


# ------------------------------------------------------------
# 60: tipos de alerta (RF-20)
# ------------------------------------------------------------
def _tipos_alertas(nombre: str) -> list[str]:
    sql = _sin_comentarios((DIR_SQL / nombre).read_text(encoding="utf-8"))
    bloque = sql[sql.rindex("ADD CONSTRAINT alertas_tipo_check"):]
    return re.findall(r"'([a-z_]+)'", bloque[: bloque.index(";")])


def test_RF20_la_60_amplia_la_lista_de_la_51_con_los_tres_tipos_del_plan() -> None:
    previos = _tipos_alertas("51_alertas_aviso_cita.sql")
    nuevos = _tipos_alertas(ARCHIVO_ALERTAS)
    assert nuevos[: len(previos)] == previos  # no se pierde ningun tipo existente
    assert nuevos[len(previos):] == ["numeros_pausados", "numeros_recortados", "numero_configuracion_incompleta"]
    assert len(set(nuevos)) == len(nuevos)


def test_RF20_la_60_es_idempotente_y_no_toca_datos() -> None:
    sql = _sin_comentarios((DIR_SQL / ARCHIVO_ALERTAS).read_text(encoding="utf-8"))
    assert "DROP CONSTRAINT IF EXISTS alertas_tipo_check" in sql
    assert not re.search(r"(DELETE|TRUNCATE|UPDATE|INSERT)", sql, re.I)
    assert len(list(DIR_SQL.glob("60_*.sql"))) == 1


# ------------------------------------------------------------
# aplicar_sql.py lista los objetos nuevos (--revisar)
# ------------------------------------------------------------
def test_RF22_aplicar_sql_lista_las_tablas_y_funciones_nuevas() -> None:
    import aplicar_sql

    tablas = {t for t, _ in aplicar_sql.TABLAS}
    assert {
        "agentes_canal", "config_referencia_negocio", "agente_canal_herramientas",
        "social_herramientas", "numeros_bitacora", "intentos_alta_ajena",
        "plan_herramientas", "numeros_reevaluacion_cola", "numeros_despliegue",
    } <= tablas
    funciones = {f for f, _ in aplicar_sql.FUNCIONES}
    assert {
        "situacion_comercial", "plan_mas_bajo_activo", "limites_efectivos",
        "resuelve_numero", "familia_de_herramienta", "recortar_agente_a_plan",
        "configuracion_incompleta",
    } <= funciones


def test_RF22_los_archivos_se_resuelven_por_nombre_suelto() -> None:
    import aplicar_sql

    for numero, nombre in ARCHIVOS.items():
        assert aplicar_sql.resolver_ruta(nombre).is_file()
        assert aplicar_sql.resolver_ruta(f"sql/{nombre}").is_file()


# ============================================================
# 2. CON BASE DE DATOS DE PRUEBA (opcional, ver el docstring)
# ============================================================
URL_BD = os.environ.get("NUMEROS_SQL_TEST_DATABASE_URL", "")

requiere_bd = pytest.mark.skipif(
    not URL_BD,
    reason="definir NUMEROS_SQL_TEST_DATABASE_URL con una Postgres de PRUEBA (migraciones 00 a 55 aplicadas)",
)

TABLAS_FOTO = (
    "agentes_canal", "config_referencia_negocio", "agente_canal_herramientas",
    "social_herramientas", "plan_herramientas", "numeros_despliegue",
    "numeros_reevaluacion_cola", "numeros_recortes_pendientes",
)
FUNCIONES_FOTO = (
    "configuracion_incompleta", "familia_de_herramienta", "situacion_comercial",
    "plan_mas_bajo_activo", "tenants_sin_suscripcion_vigente", "limites_efectivos",
    "recortar_agente_a_plan", "recortar_agentes_sin_suscripcion", "resuelve_tenant",
    "resuelve_numero", "numeros_encolar_reevaluacion", "numeros_encolar_por_plan",
)


@pytest.fixture
async def bd():
    """Conexión a la base de prueba con 56 a 59 aplicados DENTRO de una transacción que se revierte."""
    from config import settings

    if not URL_BD:
        pytest.skip("sin NUMEROS_SQL_TEST_DATABASE_URL")
    if URL_BD == settings.DATABASE_URL and os.environ.get("NUMEROS_SQL_TEST_PERMITIR_BD_DE_ENV") != "1":
        pytest.skip(
            "NUMEROS_SQL_TEST_DATABASE_URL es el DATABASE_URL del backend; usa otra base o "
            "NUMEROS_SQL_TEST_PERMITIR_BD_DE_ENV=1 si sabes que es de prueba"
        )
    conn = await asyncpg.connect(dsn=URL_BD)
    tx = conn.transaction()
    await tx.start()
    try:
        faltan = await conn.fetchval(
            "SELECT to_regclass('public.tenant_subscriptions') IS NULL "
            "OR to_regclass('public.tenant_estado_plataforma') IS NULL "
            "OR to_regclass('public.tenant_tools') IS NULL "
            "OR to_regclass('public.planes') IS NULL"
        )
        if faltan:
            pytest.skip("la base de prueba no tiene aplicadas las migraciones 00 a 55")
        estaba = await conn.fetchval("SELECT to_regclass('public.agentes_canal') IS NOT NULL")
        for numero in sorted(ARCHIVOS):
            await conn.execute(_leer(numero))
        yield SimpleNamespace(conn=conn, estaba_migrada=bool(estaba))
    finally:
        await tx.rollback()
        await conn.close()


async def _tenant(c) -> UUID:
    tid = uuid4()
    await c.execute("INSERT INTO tenants (id, name) VALUES ($1, $2)", tid, "t-005a")
    return tid


async def _numero(
    c,
    tenant: UUID,
    pnid: str | None = None,
    *,
    estado: str = "activo",
    principal: bool = False,
    avisos: bool = False,
    nombre: str = "Ana",
    prompt: str = "Eres un asistente.",
    agente: bool = True,
    dias: int = 0,
    modelo: str | None = None,
    historial: int = 30,
) -> UUID:
    nid = uuid4()
    ahora = datetime.now(timezone.utc)
    await c.execute(
        """
        INSERT INTO agentes_canal
            (id, tenant_id, phone_number_id, estado, es_principal, es_linea_avisos,
             agent_name, system_prompt, agente_activo, vinculado_en, desvinculado_en,
             model, history_window)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        """,
        nid, tenant, pnid or f"pn-{uuid4().hex[:12]}", estado, principal, avisos, nombre, prompt,
        agente, ahora - timedelta(days=dias), ahora if estado == "desvinculado" else None,
        modelo, historial,
    )
    return nid


async def _falla(c, excepcion: type[Exception], sql: str, *args) -> None:
    """La sentencia debe fallar con esa excepción; el punto de guardado deja viva la transacción."""
    with pytest.raises(excepcion):
        async with c.transaction():
            await c.execute(sql, *args)


async def _herramienta(c, tenant: UUID, clave: str, tipo: str = "http_generico", habilitada: bool = True) -> None:
    await c.execute(
        """
        INSERT INTO tenant_tools (tenant_id, tool_key, tool_type, display_name, description, config, is_enabled)
        VALUES ($1, $2, $3, $4, $5, '{}'::jsonb, $6)
        """,
        tenant, clave, tipo, clave, clave, habilitada,
    )


async def _catalogo(c, planes: list[PlanCatalogo]) -> None:
    """
    Deja `planes` como único catálogo activo (los demás se desactivan). Se hace con
    los triggers de usuario de `planes` apagados (para no recorrer todos los
    negocios de la base) y se vuelven a encender: los tests de triggers los necesitan.
    """
    await c.execute("ALTER TABLE planes DISABLE TRIGGER USER")
    await c.execute("ALTER TABLE plan_herramientas DISABLE TRIGGER USER")
    try:
        nombres = [p.nombre for p in planes]
        await c.execute("UPDATE planes SET activo = false WHERE nombre <> ALL($1::text[])", nombres)
        for p in planes:
            await c.execute(
                """
                INSERT INTO planes (nombre, precio_monthly, activo, max_numeros_whatsapp,
                                    modelos_permitidos, historial_maximo)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (nombre) DO UPDATE SET
                    precio_monthly = EXCLUDED.precio_monthly,
                    activo = EXCLUDED.activo,
                    max_numeros_whatsapp = EXCLUDED.max_numeros_whatsapp,
                    modelos_permitidos = EXCLUDED.modelos_permitidos,
                    historial_maximo = EXCLUDED.historial_maximo
                """,
                p.nombre, Decimal(str(p.precio_mensual)), p.activo, p.max_numeros,
                list(p.modelos) if p.modelos else None, p.historial_maximo,
            )
            await c.execute("DELETE FROM plan_herramientas WHERE plan_nombre = $1", p.nombre)
            for familia in sorted(p.familias):
                await c.execute(
                    "INSERT INTO plan_herramientas (plan_nombre, familia) VALUES ($1, $2)", p.nombre, familia
                )
    finally:
        await c.execute("ALTER TABLE planes ENABLE TRIGGER USER")
        await c.execute("ALTER TABLE plan_herramientas ENABLE TRIGGER USER")


async def _suscripcion(c, tenant: UUID, plan: str, estado: str, origen: str, renovacion: datetime | None) -> None:
    await c.execute(
        """
        INSERT INTO tenant_subscriptions (tenant_id, plan, estado, origen, fecha_renovacion, precio_monthly)
        VALUES ($1, $2, $3, $4, $5, 1)
        """,
        tenant, plan, estado, origen, renovacion,
    )


def _vigente() -> datetime:
    """Fin de periodo en el futuro respecto del reloj REAL: los triggers y recortar_agente_a_plan usan NOW()."""
    return datetime.now(timezone.utc) + timedelta(days=30)


async def _estado_plataforma(c, tenant: UUID, estado: str) -> None:
    await c.execute(
        """
        INSERT INTO tenant_estado_plataforma (tenant_id, estado) VALUES ($1, $2)
        ON CONFLICT (tenant_id) DO UPDATE SET estado = EXCLUDED.estado
        """,
        tenant, estado,
    )


async def _pendientes(c, tenant: UUID) -> list[str]:
    filas = await c.fetch(
        "SELECT motivo FROM numeros_reevaluacion_cola WHERE tenant_id = $1 AND procesada_en IS NULL ORDER BY id",
        tenant,
    )
    return [f["motivo"] for f in filas]


async def _vaciar_cola(c, tenant: UUID) -> None:
    await c.execute("DELETE FROM numeros_reevaluacion_cola WHERE tenant_id = $1", tenant)


# ------------------------------------------------------------
# Unicidad, principal y cascada (RF-3, RF-6, RF-16; A14, A64)
# ------------------------------------------------------------
@requiere_bd
async def test_RF3_RF16_un_phone_number_id_vinculado_solo_en_un_negocio_db(bd) -> None:
    c = bd.conn
    a, b = await _tenant(c), await _tenant(c)
    await _numero(c, a, "pn-unico")
    with pytest.raises(asyncpg.UniqueViolationError):
        async with c.transaction():
            await _numero(c, b, "pn-unico")
    # También si el primero está pausado: sigue vinculado.
    await c.execute("UPDATE agentes_canal SET estado = 'pausado' WHERE phone_number_id = 'pn-unico'")
    with pytest.raises(asyncpg.UniqueViolationError):
        async with c.transaction():
            await _numero(c, b, "pn-unico")


@requiere_bd
async def test_RF3_un_numero_desvinculado_se_puede_vincular_de_nuevo_db(bd) -> None:
    c = bd.conn
    a, b = await _tenant(c), await _tenant(c)
    await _numero(c, a, "pn-libre", estado="desvinculado")
    await _numero(c, b, "pn-libre")  # otro negocio: permitido
    await _numero(c, a, "pn-libre", estado="desvinculado", dias=5)  # varios desvinculados del mismo número: permitido
    total = await c.fetchval("SELECT count(*) FROM agentes_canal WHERE phone_number_id = 'pn-libre'")
    assert total == 3


@requiere_bd
async def test_A64_un_solo_principal_por_negocio_db(bd) -> None:
    c = bd.conn
    t = await _tenant(c)
    await _numero(c, t, principal=True)
    with pytest.raises(asyncpg.UniqueViolationError):
        async with c.transaction():
            await _numero(c, t, principal=True)
    # Otro negocio sí puede tener el suyo.
    await _numero(c, await _tenant(c), principal=True)


@requiere_bd
@pytest.mark.parametrize(
    "kw",
    [
        {"estado": "pausado"},
        {"estado": "desvinculado"},
        {"avisos": True},
        {"nombre": ""},
        {"nombre": "   "},
        {"prompt": ""},
        {"prompt": " \n\t "},
    ],
    ids=["pausado", "desvinculado", "linea_de_avisos", "sin_nombre", "nombre_en_blanco", "sin_prompt", "prompt_en_blanco"],
)
async def test_A64_CL31_el_principal_no_puede_ser_pausado_ni_avisos_ni_incompleto_db(bd, kw) -> None:
    c = bd.conn
    t = await _tenant(c)
    with pytest.raises(asyncpg.CheckViolationError):
        async with c.transaction():
            await _numero(c, t, principal=True, **kw)


@requiere_bd
async def test_A64_un_principal_no_puede_pasar_a_incompleto_ni_a_pausado_por_sql_db(bd) -> None:
    c = bd.conn
    t = await _tenant(c)
    nid = await _numero(c, t, principal=True)
    await _falla(c, asyncpg.CheckViolationError, "UPDATE agentes_canal SET system_prompt = '  ' WHERE id = $1", nid)
    await _falla(c, asyncpg.CheckViolationError, "UPDATE agentes_canal SET estado = 'pausado' WHERE id = $1", nid)
    # Quitando primero el papel (como hará el servicio) sí se puede.
    await c.execute("UPDATE agentes_canal SET es_principal = false, estado = 'pausado' WHERE id = $1", nid)


@requiere_bd
async def test_RF3_estados_temperatura_y_desvinculado_en_validos_db(bd) -> None:
    c = bd.conn
    t = await _tenant(c)
    nid = await _numero(c, t)
    await _falla(c, asyncpg.CheckViolationError, "UPDATE agentes_canal SET estado = 'borrado' WHERE id = $1", nid)
    await _falla(c, asyncpg.CheckViolationError, "UPDATE agentes_canal SET temperature = 1.5 WHERE id = $1", nid)
    await c.execute("UPDATE agentes_canal SET temperature = 0 WHERE id = $1", nid)
    await c.execute("UPDATE agentes_canal SET temperature = 1 WHERE id = $1", nid)
    await _falla(c, asyncpg.CheckViolationError, "UPDATE agentes_canal SET estado = 'desvinculado' WHERE id = $1", nid)


@requiere_bd
async def test_A14_CL7_borrar_una_herramienta_la_quita_de_todos_los_numeros_y_de_social_db(bd) -> None:
    c = bd.conn
    t = await _tenant(c)
    n1, n2 = await _numero(c, t), await _numero(c, t)
    await _herramienta(c, t, "mi_api")
    await _herramienta(c, t, "otra_api")
    for n in (n1, n2):
        await c.execute(
            "INSERT INTO agente_canal_herramientas (agente_canal_id, tenant_id, tool_key) VALUES ($1, $2, 'mi_api')", n, t
        )
    await c.execute("INSERT INTO social_herramientas (tenant_id, tool_key) VALUES ($1, 'mi_api')", t)
    await c.execute(
        "INSERT INTO agente_canal_herramientas (agente_canal_id, tenant_id, tool_key) VALUES ($1, $2, 'otra_api')", n1, t
    )

    await c.execute("DELETE FROM tenant_tools WHERE tenant_id = $1 AND tool_key = 'mi_api'", t)

    assert await c.fetchval("SELECT count(*) FROM agente_canal_herramientas WHERE tool_key = 'mi_api'") == 0
    assert await c.fetchval("SELECT count(*) FROM social_herramientas WHERE tenant_id = $1", t) == 0
    # La otra herramienta y los números siguen: la configuración de cada número sigue siendo válida.
    assert await c.fetchval("SELECT count(*) FROM agente_canal_herramientas WHERE tool_key = 'otra_api'") == 1
    assert await c.fetchval("SELECT count(*) FROM agentes_canal WHERE tenant_id = $1", t) == 2


@requiere_bd
async def test_RF6_una_herramienta_de_otro_negocio_no_se_puede_activar_en_un_numero_db(bd) -> None:
    c = bd.conn
    a, b = await _tenant(c), await _tenant(c)
    na = await _numero(c, a)
    await _herramienta(c, b, "ajena")
    # Ni la FK a tenant_tools (a no tiene 'ajena') ni la compuesta a agentes_canal (na no es de b) lo permiten.
    await _falla(
        c, asyncpg.ForeignKeyViolationError,
        "INSERT INTO agente_canal_herramientas (agente_canal_id, tenant_id, tool_key) VALUES ($1, $2, 'ajena')", na, a,
    )
    await _falla(
        c, asyncpg.ForeignKeyViolationError,
        "INSERT INTO agente_canal_herramientas (agente_canal_id, tenant_id, tool_key) VALUES ($1, $2, 'ajena')", na, b,
    )


@requiere_bd
async def test_RF21_la_bitacora_acepta_solo_motivos_de_la_spec_y_sobrevive_al_numero_db(bd) -> None:
    c = bd.conn
    t = await _tenant(c)
    nid = await _numero(c, t)
    await c.execute(
        "INSERT INTO numeros_bitacora (tenant_id, agente_canal_id, motivo, autor_tipo) VALUES ($1, $2, 'pausa_manual', 'gerencia')",
        t, nid,
    )
    await _falla(
        c, asyncpg.CheckViolationError,
        "INSERT INTO numeros_bitacora (tenant_id, motivo, autor_tipo) VALUES ($1, 'motivo_inventado', 'sistema')", t,
    )
    await _falla(
        c, asyncpg.CheckViolationError,
        "INSERT INTO numeros_bitacora (tenant_id, motivo, autor_tipo) VALUES ($1, 'vinculacion', 'alguien')", t,
    )
    await c.execute("DELETE FROM agentes_canal WHERE id = $1", nid)
    fila = await c.fetchrow("SELECT agente_canal_id FROM numeros_bitacora WHERE tenant_id = $1", t)
    assert fila is not None and fila["agente_canal_id"] is None


@requiere_bd
async def test_RF1_cupo_nulo_o_al_menos_uno_y_planes_existentes_sin_tope_db(bd) -> None:
    c = bd.conn
    if not bd.estaba_migrada:
        # Recién añadida la columna: ningún plan existente trae tope (D21, A65).
        assert await c.fetchval("SELECT count(*) FROM planes WHERE max_numeros_whatsapp IS NOT NULL") == 0
    for invalido in (0, -1):
        await _falla(
            c, asyncpg.CheckViolationError,
            "INSERT INTO planes (nombre, precio_monthly, max_numeros_whatsapp) VALUES ('cupo_invalido', 1, $1)", invalido,
        )
    await c.execute("INSERT INTO planes (nombre, precio_monthly, max_numeros_whatsapp) VALUES ('cupo_ok', 1, 1)")
    await c.execute("INSERT INTO planes (nombre, precio_monthly, max_numeros_whatsapp) VALUES ('cupo_nulo', 1, NULL)")


# ------------------------------------------------------------
# Paridad con el núcleo puro (A22 a A28, A30, A75)
# ------------------------------------------------------------
def _susc(plan, estado, origen, renovacion=FUTURO):
    return (plan, estado, origen, renovacion)


# Misma tabla de casos que test_numeros_reglas.test_A23_A30_A75_situacion_comercial.
CASOS_SITUACION = [
    (_susc("pro", "activa", "prueba"), None),
    (_susc("pro", "activa", "prueba", FUTURO), "activo"),
    (_susc("pro", "activa", "prueba", None), None),
    (None, "prueba"),
    (_susc("starter", "activa", "pago"), "prueba"),
    (_susc("pro", "pausada", "prueba"), None),
    (_susc("pro", "cancelada", "prueba"), None),
    (_susc("pro", "activa", "prueba", PASADO), None),
    (_susc("pro", "activa", "pago"), None),
    (_susc("pro", "activa", "pago"), "activo"),
    (_susc("pro", "activa", "pago", AHORA), None),
    (_susc("pro", "activa", "pago", PASADO), None),
    (_susc("pro", "pausada", "pago"), None),
    (_susc("pro", "cancelada", "pago"), None),
    (_susc("viejo", "activa", "pago"), None),
    (None, None),
    (None, "activo"),
]


async def _armar(c, susc, estado_plataforma) -> tuple[UUID, Suscripcion | None]:
    t = await _tenant(c)
    if estado_plataforma:
        await _estado_plataforma(c, t, estado_plataforma)
    if susc:
        await _suscripcion(c, t, *susc)
    return t, (Suscripcion(plan=susc[0], estado=susc[1], origen=susc[2], fecha_renovacion=susc[3]) if susc else None)


@requiere_bd
@pytest.mark.parametrize("susc, estado_plataforma", CASOS_SITUACION)
async def test_A23_A30_A75_paridad_situacion_comercial_sql_y_python_db(bd, susc, estado_plataforma) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)
    t, suscripcion = await _armar(c, susc, estado_plataforma)
    esperado = situacion_comercial(suscripcion=suscripcion, estado_plataforma=estado_plataforma, ahora=AHORA)
    fila = await c.fetchrow("SELECT situacion, plan FROM situacion_comercial($1, $2)", t, AHORA)
    assert (fila["situacion"], fila["plan"]) == (esperado[0].value, esperado[1])


@requiere_bd
@pytest.mark.parametrize("sin_plan_activo", [False, True], ids=["catalogo_normal", "sin_plan_activo"])
@pytest.mark.parametrize("susc, estado_plataforma", CASOS_SITUACION)
async def test_A22_A24_A28_paridad_limites_efectivos_con_reglas_de_negocio_db(
    bd, susc, estado_plataforma, sin_plan_activo
) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)
    catalogo = list(PLANES)
    if sin_plan_activo:
        # Ningún plan activo (CL-20): el contratado, aunque desactivado, sigue rigiendo (D20).
        await c.execute("ALTER TABLE planes DISABLE TRIGGER USER")
        await c.execute("UPDATE planes SET activo = false")
        await c.execute("ALTER TABLE planes ENABLE TRIGGER USER")
        catalogo = [replace(p, activo=False) for p in PLANES]
    t, suscripcion = await _armar(c, susc, estado_plataforma)
    situacion, plan_asociado = situacion_comercial(
        suscripcion=suscripcion, estado_plataforma=estado_plataforma, ahora=AHORA
    )
    esperado = reglas_de_negocio(situacion=situacion, plan_asociado=plan_asociado, planes=catalogo)
    fila = await c.fetchrow(
        "SELECT modelos, historial, tope, familias, plan, sin_plan_activo FROM limites_efectivos($1, $2)", t, AHORA
    )
    assert (tuple(fila["modelos"]) if fila["modelos"] else None) == esperado.modelos
    assert fila["historial"] == esperado.historial_maximo
    assert fila["tope"] == esperado.tope
    assert frozenset(fila["familias"]) == esperado.familias
    assert fila["plan"] == esperado.plan
    assert fila["sin_plan_activo"] == esperado.sin_plan_activo


@requiere_bd
async def test_A28_paridad_del_plan_mas_bajo_con_empates_db(bd) -> None:
    c = bd.conn

    def plan(nombre, precio, tope, activo=True):
        return PlanCatalogo(
            nombre=nombre, activo=activo, precio_mensual=precio, max_numeros=tope,
            modelos=None, historial_maximo=None, familias=frozenset(),
        )

    casos = [
        [plan("b", 10, 3), plan("a", 20, 1)],
        [plan("b", 10, 3), plan("a", 10, 1)],
        [plan("b", 10, None), plan("a", 10, 5)],
        [plan("z", 10, 2), plan("y", 10, 2), plan("Y2", 10, 2)],
        [plan("x", 5, 1, activo=False), plan("y", 9, 4)],
        [plan("x", 5, 1, activo=False)],
    ]
    for catalogo in casos:
        await _catalogo(c, catalogo)
        esperado = plan_mas_bajo_activo(catalogo)
        assert await c.fetchval("SELECT plan_mas_bajo_activo()") == (esperado.nombre if esperado else None)


# ------------------------------------------------------------
# Recorte automático (RF-10; A26, A27, A28)
# ------------------------------------------------------------
HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-5"
OPUS = "claude-opus-5"


async def _config_social(c, tenant: UUID, modelo: str | None, historial: int) -> None:
    await c.execute(
        """
        INSERT INTO tenant_agent_config (tenant_id, agent_name, system_prompt, model, history_window)
        VALUES ($1, 'Ana', 'Eres un asistente.', $2, $3)
        """,
        tenant, modelo, historial,
    )


async def _referencia(c, tenant: UUID, modelo: str | None, historial: int) -> None:
    await c.execute(
        """
        INSERT INTO config_referencia_negocio (tenant_id, agent_name, system_prompt, model, history_window)
        VALUES ($1, 'Ana', 'Eres un asistente.', $2, $3)
        """,
        tenant, modelo, historial,
    )


@requiere_bd
async def test_A26_A27_el_recorte_baja_modelo_e_historial_de_social_referencia_y_numeros_db(bd) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)  # starter: solo HAIKU, historial 20
    t = await _tenant(c)
    await _suscripcion(c, t, "starter", "activa", "pago", _vigente())
    await _config_social(c, t, OPUS, 150)
    await _referencia(c, t, SONNET, 100)
    activo = await _numero(c, t, modelo=OPUS, historial=150)
    pausado = await _numero(c, t, estado="pausado", modelo=None, historial=90)
    por_debajo = await _numero(c, t, modelo=HAIKU, historial=10)
    desvinculado = await _numero(c, t, estado="desvinculado", modelo=OPUS, historial=150)

    await c.execute("SELECT recortar_agente_a_plan($1)", t)

    social = await c.fetchrow("SELECT model, history_window FROM tenant_agent_config WHERE tenant_id = $1", t)
    assert (social["model"], social["history_window"]) == (HAIKU, 20)
    ref = await c.fetchrow("SELECT model, history_window FROM config_referencia_negocio WHERE tenant_id = $1", t)
    assert (ref["model"], ref["history_window"]) == (HAIKU, 20)
    filas = {
        r["id"]: (r["model"], r["history_window"])
        for r in await c.fetch("SELECT id, model, history_window FROM agentes_canal WHERE tenant_id = $1", t)
    }
    assert filas[activo] == (HAIKU, 20)
    assert filas[pausado] == (HAIKU, 20)            # modelo nulo -> default del plan; el pausado también se recorta
    assert filas[por_debajo] == (HAIKU, 10)         # nunca sube un valor
    assert filas[desvinculado] == (OPUS, 150)       # los desvinculados no se tocan


@requiere_bd
async def test_A27_CL18_un_modelo_retirado_pasa_al_default_del_plan_sin_dejar_el_numero_incompleto_db(bd) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)
    t = await _tenant(c)
    await _suscripcion(c, t, "pro", "activa", "pago", _vigente())  # pro: HAIKU, SONNET
    nid = await _numero(c, t, principal=True, modelo=OPUS, historial=60)

    await c.execute("SELECT recortar_agente_a_plan($1)", t)

    fila = await c.fetchrow("SELECT model, system_prompt, agent_name, es_principal FROM agentes_canal WHERE id = $1", nid)
    assert fila["model"] == HAIKU           # el primero de la lista del plan
    assert fila["system_prompt"] == "Eres un asistente." and fila["agent_name"] == "Ana"
    assert fila["es_principal"] is True     # sigue siendo principal: no quedó incompleto


@requiere_bd
async def test_A28_un_negocio_sin_suscripcion_se_recorta_al_plan_mas_bajo_db(bd) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)
    t = await _tenant(c)
    nid = await _numero(c, t, modelo=OPUS, historial=150)
    await c.execute("SELECT recortar_agente_a_plan($1)", t)
    assert tuple(await c.fetchrow("SELECT model, history_window FROM agentes_canal WHERE id = $1", nid)) == (HAIKU, 20)


@requiere_bd
async def test_A28_una_prueba_sin_plan_asociado_no_se_recorta_y_una_con_plan_usa_sus_limites_db(bd) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)
    piloto = await _tenant(c)
    await _estado_plataforma(c, piloto, "prueba")
    n_piloto = await _numero(c, piloto, modelo=OPUS, historial=150)
    con_plan = await _tenant(c)
    await _suscripcion(c, con_plan, "pro", "activa", "prueba", None)
    n_con_plan = await _numero(c, con_plan, modelo=OPUS, historial=150)

    await c.execute("SELECT recortar_agente_a_plan($1)", piloto)
    await c.execute("SELECT recortar_agente_a_plan($1)", con_plan)

    assert tuple(await c.fetchrow("SELECT model, history_window FROM agentes_canal WHERE id = $1", n_piloto)) == (OPUS, 150)
    assert tuple(await c.fetchrow("SELECT model, history_window FROM agentes_canal WHERE id = $1", n_con_plan)) == (HAIKU, 60)


@requiere_bd
async def test_A28_CL20_sin_ningun_plan_activo_no_se_recorta_nada_db(bd) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)
    await c.execute("ALTER TABLE planes DISABLE TRIGGER USER")
    await c.execute("UPDATE planes SET activo = false")
    await c.execute("ALTER TABLE planes ENABLE TRIGGER USER")
    t = await _tenant(c)
    nid = await _numero(c, t, modelo=OPUS, historial=150)
    await c.execute("SELECT recortar_agente_a_plan($1)", t)
    assert tuple(await c.fetchrow("SELECT model, history_window FROM agentes_canal WHERE id = $1", nid)) == (OPUS, 150)


@requiere_bd
async def test_A26_bajar_el_limite_de_un_plan_recorta_por_trigger_a_sus_negocios_db(bd) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)
    t = await _tenant(c)
    await _suscripcion(c, t, "pro", "activa", "pago", _vigente())
    nid = await _numero(c, t, modelo=SONNET, historial=60)
    await c.execute("UPDATE planes SET historial_maximo = 30, modelos_permitidos = ARRAY[$1] WHERE nombre = 'pro'", HAIKU)
    assert tuple(await c.fetchrow("SELECT model, history_window FROM agentes_canal WHERE id = $1", nid)) == (HAIKU, 30)
    # Subir el límite no devuelve lo recortado.
    await c.execute("UPDATE planes SET historial_maximo = 200 WHERE nombre = 'pro'")
    assert tuple(await c.fetchrow("SELECT model, history_window FROM agentes_canal WHERE id = $1", nid)) == (HAIKU, 30)


@requiere_bd
async def test_A28_cambiar_el_plan_mas_bajo_recorta_a_los_negocios_sin_suscripcion_db(bd) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)
    t = await _tenant(c)
    nid = await _numero(c, t, modelo=HAIKU, historial=60)
    # starter (historial 20) sigue siendo el más bajo: baja su límite y el negocio sin suscripción lo sigue.
    await c.execute("UPDATE planes SET historial_maximo = 10 WHERE nombre = 'starter'")
    assert (await c.fetchval("SELECT history_window FROM agentes_canal WHERE id = $1", nid)) == 10


# ------------------------------------------------------------
# resuelve_tenant y resuelve_numero (RF-16)
# ------------------------------------------------------------
@requiere_bd
async def test_RF16_resuelve_tenant_y_resuelve_numero_para_vinculado_pausado_desvinculado_y_desconocido_db(bd) -> None:
    c = bd.conn
    a, b = await _tenant(c), await _tenant(c)
    activo = await _numero(c, a, "pn-activo")
    pausado = await _numero(c, a, "pn-pausado", estado="pausado")
    viejo = await _numero(c, a, "pn-desv", estado="desvinculado", dias=10)
    nuevo_dueno = await _numero(c, b, "pn-desv")  # volvió a vincularse a otro negocio

    assert await c.fetchval("SELECT resuelve_tenant('whatsapp', 'pn-activo')") == a
    assert await c.fetchval("SELECT resuelve_tenant('whatsapp', 'pn-pausado')") == a
    assert await c.fetchval("SELECT resuelve_tenant('whatsapp', 'pn-desv')") == b
    assert await c.fetchval("SELECT resuelve_tenant('whatsapp', 'pn-desconocido')") is None
    assert await c.fetchval("SELECT resuelve_tenant('whatsapp', '')") is None
    assert await c.fetchval("SELECT resuelve_tenant('whatsapp', NULL)") is None

    f = await c.fetchrow("SELECT * FROM resuelve_numero('pn-activo')")
    assert (f["agente_canal_id"], f["tenant_id"], f["estado"]) == (activo, a, "activo")
    f = await c.fetchrow("SELECT * FROM resuelve_numero('pn-pausado')")
    assert (f["agente_canal_id"], f["tenant_id"], f["estado"]) == (pausado, a, "pausado")
    # Si hay un vinculado, gana sobre los desvinculados.
    f = await c.fetchrow("SELECT * FROM resuelve_numero('pn-desv')")
    assert (f["agente_canal_id"], f["tenant_id"], f["estado"]) == (nuevo_dueno, b, "activo")
    assert await c.fetchval("SELECT count(*) FROM resuelve_numero('pn-desconocido')") == 0
    assert await c.fetchval("SELECT count(*) FROM resuelve_numero('')") == 0
    assert viejo is not None


@requiere_bd
async def test_RF16_un_numero_desvinculado_resuelve_a_su_ultimo_dueno_db(bd) -> None:
    c = bd.conn
    a, b = await _tenant(c), await _tenant(c)
    await _numero(c, a, "pn-x", estado="desvinculado", dias=10)
    reciente = await _numero(c, b, "pn-x", estado="desvinculado", dias=0)
    # Los dos están desvinculados: gana el más reciente (desvinculado_en más nuevo).
    await c.execute(
        "UPDATE agentes_canal SET desvinculado_en = NOW() - interval '30 days' WHERE tenant_id = $1 AND phone_number_id = 'pn-x'", a
    )
    f = await c.fetchrow("SELECT * FROM resuelve_numero('pn-x')")
    assert (f["agente_canal_id"], f["tenant_id"], f["estado"]) == (reciente, b, "desvinculado")
    # resuelve_tenant no atribuye un desvinculado a nadie.
    assert await c.fetchval("SELECT resuelve_tenant('whatsapp', 'pn-x')") is None


@requiere_bd
async def test_RF16_los_demas_canales_siguen_resolviendose_por_channel_credentials_db(bd) -> None:
    c = bd.conn
    t = await _tenant(c)
    await c.execute(
        "INSERT INTO channel_credentials (tenant_id, channel_type, page_id, is_active) VALUES ($1, 'facebook', 'page-005a', true)", t
    )
    assert await c.fetchval("SELECT resuelve_tenant('facebook', 'page-005a')") == t
    assert await c.fetchval("SELECT resuelve_tenant('whatsapp', 'page-005a')") is None
    assert await c.fetchval("SELECT resuelve_tenant('facebook', '')") is None


@requiere_bd
async def test_RF16_la_resolucion_del_numero_usa_el_indice_parcial_unico_db(bd) -> None:
    c = bd.conn
    await c.execute("SET LOCAL enable_seqscan = off")
    plan = await c.fetch(
        "EXPLAIN SELECT tenant_id FROM agentes_canal WHERE phone_number_id = 'pn-x' AND estado IN ('activo', 'pausado')"
    )
    assert "uq_agentes_canal_numero_vinculado" in "\n".join(r[0] for r in plan)


# ------------------------------------------------------------
# Triggers que encolan (RF-11, siete casos)
# ------------------------------------------------------------
@requiere_bd
async def test_RF11_caso1_y_2_un_pago_encola_cambio_de_plan_y_fin_de_suscripcion_db(bd) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)
    t = await _tenant(c)
    await _suscripcion(c, t, "pro", "activa", "pago", _vigente())
    assert await _pendientes(c, t) == ["cambio_de_plan"]
    await _vaciar_cola(c, t)

    await c.execute("UPDATE tenant_subscriptions SET plan = 'starter' WHERE tenant_id = $1", t)
    assert await _pendientes(c, t) == ["cambio_de_plan"]
    await _vaciar_cola(c, t)

    # Una cancelación programada mantiene el cupo hasta el fin del periodo: no encola.
    await c.execute("UPDATE tenant_subscriptions SET cancela_al_vencer = true WHERE tenant_id = $1", t)
    assert await _pendientes(c, t) == []

    await c.execute("UPDATE tenant_subscriptions SET estado = 'pausada' WHERE tenant_id = $1", t)
    assert await _pendientes(c, t) == ["fin_de_suscripcion"]


@requiere_bd
async def test_RF11_caso3_una_prueba_pausada_o_cancelada_encola_fin_de_prueba_db(bd) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)
    for estado in ("pausada", "cancelada"):
        t = await _tenant(c)
        await _suscripcion(c, t, "pro", "activa", "prueba", None)
        await _vaciar_cola(c, t)
        await c.execute("UPDATE tenant_subscriptions SET estado = $2 WHERE tenant_id = $1", t, estado)
        assert await _pendientes(c, t) == ["fin_de_prueba"], estado


@requiere_bd
async def test_RF11_caso3_el_piloto_que_termina_encola_fin_de_prueba_db(bd) -> None:
    c = bd.conn
    t = await _tenant(c)
    await _estado_plataforma(c, t, "prueba")
    await _vaciar_cola(c, t)
    await c.execute("UPDATE tenant_estado_plataforma SET estado = 'activo' WHERE tenant_id = $1", t)
    assert await _pendientes(c, t) == ["fin_de_prueba"]


@requiere_bd
async def test_RF11_caso6_una_prueba_que_pasa_a_pago_encola_pago_de_prueba_db(bd) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)
    t = await _tenant(c)
    await _suscripcion(c, t, "pro", "activa", "prueba", None)
    await _vaciar_cola(c, t)
    await c.execute("UPDATE tenant_subscriptions SET origen = 'pago', fecha_renovacion = $2 WHERE tenant_id = $1", t, _vigente())
    assert await _pendientes(c, t) == ["pago_de_prueba"]


@requiere_bd
async def test_RF11_caso4_cambiar_el_cupo_del_plan_contratado_encola_a_sus_negocios_db(bd) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)
    contratado, otro_plan, sin_susc = await _tenant(c), await _tenant(c), await _tenant(c)
    await _suscripcion(c, contratado, "pro", "activa", "pago", _vigente())
    await _suscripcion(c, otro_plan, "starter", "activa", "pago", _vigente())
    for t in (contratado, otro_plan, sin_susc):
        await _vaciar_cola(c, t)

    await c.execute("UPDATE planes SET max_numeros_whatsapp = 2 WHERE nombre = 'pro'")

    assert "cupo_plataforma" in await _pendientes(c, contratado)
    assert "cupo_plataforma" not in await _pendientes(c, otro_plan)
    assert "cupo_plataforma" not in await _pendientes(c, sin_susc)


@requiere_bd
async def test_RF11_caso5_cambiar_el_plan_mas_bajo_encola_a_los_negocios_sin_suscripcion_db(bd) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)
    sin_susc, contratado = await _tenant(c), await _tenant(c)
    await _suscripcion(c, contratado, "pro", "activa", "pago", _vigente())
    for t in (sin_susc, contratado):
        await _vaciar_cola(c, t)

    await c.execute("UPDATE planes SET max_numeros_whatsapp = 5 WHERE nombre = 'starter'")  # starter es el más bajo
    assert await _pendientes(c, sin_susc) == ["plan_mas_bajo"]
    assert "plan_mas_bajo" not in await _pendientes(c, contratado)
    await _vaciar_cola(c, sin_susc)

    await c.execute("UPDATE planes SET precio_monthly = 5 WHERE nombre = 'pro'")  # pro pasa a ser el más bajo
    assert await _pendientes(c, sin_susc) == ["plan_mas_bajo"]
    await _vaciar_cola(c, sin_susc)

    await c.execute("UPDATE planes SET activo = false WHERE nombre = 'pro'")
    assert await _pendientes(c, sin_susc) == ["plan_mas_bajo"]


@requiere_bd
async def test_RF11_caso5_un_plan_nuevo_o_borrado_que_cambia_el_mas_bajo_encola_db(bd) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)
    sin_susc = await _tenant(c)
    await _vaciar_cola(c, sin_susc)
    await c.execute("INSERT INTO planes (nombre, precio_monthly, activo) VALUES ('nuevo_barato', 1, true)")
    assert await _pendientes(c, sin_susc) == ["plan_mas_bajo"]
    await _vaciar_cola(c, sin_susc)
    await c.execute("DELETE FROM planes WHERE nombre = 'nuevo_barato'")
    assert await _pendientes(c, sin_susc) == ["plan_mas_bajo"]
    await _vaciar_cola(c, sin_susc)
    # Un plan nuevo más caro no cambia nada.
    await c.execute("INSERT INTO planes (nombre, precio_monthly, activo) VALUES ('nuevo_caro', 9999, true)")
    assert await _pendientes(c, sin_susc) == []


@requiere_bd
async def test_RF11_caso5_cambiar_la_lista_de_herramientas_de_un_plan_encola_db(bd) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)
    contratado, sin_susc = await _tenant(c), await _tenant(c)
    await _suscripcion(c, contratado, "pro", "activa", "pago", _vigente())
    for t in (contratado, sin_susc):
        await _vaciar_cola(c, t)

    await c.execute("INSERT INTO plan_herramientas (plan_nombre, familia) VALUES ('pro', 'google_docs')")
    assert await _pendientes(c, contratado) == ["herramientas_del_plan"]
    assert await _pendientes(c, sin_susc) == []  # pro no es el plan más bajo

    await c.execute("DELETE FROM plan_herramientas WHERE plan_nombre = 'starter' AND familia = 'calendario'")
    assert await _pendientes(c, sin_susc) == ["herramientas_del_plan"]


@requiere_bd
async def test_P8_un_negocio_suspendido_no_se_encola_y_al_reactivarse_si_db(bd) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)
    t = await _tenant(c)
    await _suscripcion(c, t, "pro", "activa", "pago", _vigente())
    await _estado_plataforma(c, t, "suspendido")
    await _vaciar_cola(c, t)

    await c.execute("UPDATE tenant_subscriptions SET plan = 'starter' WHERE tenant_id = $1", t)
    await c.execute("UPDATE planes SET max_numeros_whatsapp = 9 WHERE nombre = 'starter'")
    assert await _pendientes(c, t) == []

    await c.execute("UPDATE tenant_estado_plataforma SET estado = 'activo' WHERE tenant_id = $1", t)
    assert await _pendientes(c, t) == ["reactivacion_negocio"]


@requiere_bd
async def test_RF11_la_cola_no_duplica_una_entrada_pendiente_con_el_mismo_motivo_db(bd) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)
    sin_susc = await _tenant(c)
    await _vaciar_cola(c, sin_susc)
    await c.execute("UPDATE planes SET max_numeros_whatsapp = 5 WHERE nombre = 'starter'")
    await c.execute("UPDATE planes SET max_numeros_whatsapp = 6 WHERE nombre = 'starter'")
    assert await _pendientes(c, sin_susc) == ["plan_mas_bajo"]
    # Una ya procesada no cuenta como pendiente: vuelve a encolar.
    await c.execute("UPDATE numeros_reevaluacion_cola SET procesada_en = NOW() WHERE tenant_id = $1", sin_susc)
    await c.execute("UPDATE planes SET max_numeros_whatsapp = 7 WHERE nombre = 'starter'")
    assert await _pendientes(c, sin_susc) == ["plan_mas_bajo"]


@requiere_bd
async def test_CL2_los_triggers_no_pausan_ni_reactivan_ningun_numero_db(bd) -> None:
    c = bd.conn
    await _catalogo(c, PLANES)
    t = await _tenant(c)
    await _suscripcion(c, t, "starter", "activa", "pago", _vigente())
    activos = [await _numero(c, t) for _ in range(3)]
    pausado = await _numero(c, t, estado="pausado")
    await c.execute("UPDATE planes SET max_numeros_whatsapp = 1 WHERE nombre = 'starter'")
    await c.execute("UPDATE planes SET max_numeros_whatsapp = 10 WHERE nombre = 'starter'")
    estados = {r["id"]: r["estado"] for r in await c.fetch("SELECT id, estado FROM agentes_canal WHERE tenant_id = $1", t)}
    assert [estados[i] for i in activos] == ["activo"] * 3
    assert estados[pausado] == "pausado"


# ------------------------------------------------------------
# Migración de datos (RF-22; A62, A63, A64, A65, A71; CL-17, CL-18, CL-19, CL-27)
# ------------------------------------------------------------
async def _negocio_antiguo(
    c,
    pnid: str,
    *,
    nombre: str | None = "Ana",
    prompt: str | None = "Eres un asistente.",
    config: bool = True,
    agente: bool = True,
    canal_activo: bool = True,
    temperatura: str = "0.7",
    habilitada: str = "tool_on",
    deshabilitada: str = "tool_off",
) -> UUID:
    """Un negocio tal como era antes de 56 a 59: credencial de WhatsApp, config del agente, herramientas y conversaciones."""
    t = await _tenant(c)
    await c.execute(
        "INSERT INTO channel_credentials (tenant_id, channel_type, phone_number_id, is_active) VALUES ($1, 'whatsapp', $2, $3)",
        t, pnid, canal_activo,
    )
    await c.execute(
        "INSERT INTO tenant_channels (tenant_id, channel_type, is_active) VALUES ($1, 'whatsapp', $2)", t, canal_activo
    )
    if config:
        await c.execute(
            """
            INSERT INTO tenant_agent_config
                (tenant_id, agent_name, system_prompt, model, temperature, history_window, is_active)
            VALUES ($1, $2, $3, $4, $5::numeric, 40, $6)
            """,
            t, nombre or "", prompt or "", HAIKU, Decimal(temperatura), agente,
        )
    await _herramienta(c, t, habilitada, habilitada=True)
    await _herramienta(c, t, deshabilitada, habilitada=False)
    usuario = uuid4()
    await c.execute("INSERT INTO users (id, tenant_id, display_name) VALUES ($1, $2, 'cliente')", usuario, t)
    for canal in ("whatsapp", "facebook"):
        await c.execute(
            "INSERT INTO conversations (tenant_id, user_id, channel_type) VALUES ($1, $2, $3)", t, usuario, canal
        )
    return t


async def _migrar(c) -> None:
    await c.execute(_leer(59))


async def _numeros(c, t: UUID):
    return await c.fetch("SELECT * FROM agentes_canal WHERE tenant_id = $1", t)


@requiere_bd
async def test_A62_CL27_un_numero_con_configuracion_completa_pasa_a_principal_y_conserva_todo_db(bd) -> None:
    c = bd.conn
    t = await _negocio_antiguo(c, "pn-mig-ok", temperatura="0.55")
    await _migrar(c)

    filas = await _numeros(c, t)
    assert len(filas) == 1
    n = filas[0]
    assert (n["phone_number_id"], n["estado"], n["es_principal"], n["es_linea_avisos"]) == ("pn-mig-ok", "activo", True, False)
    assert (n["agent_name"], n["system_prompt"], n["model"], n["history_window"], n["agente_activo"]) == (
        "Ana", "Eres un asistente.", HAIKU, 40, True,
    )
    assert n["temperature"] == Decimal("0.55")
    # Herramientas habilitadas copiadas; las deshabilitadas no.
    claves = {r["tool_key"] for r in await c.fetch("SELECT tool_key FROM agente_canal_herramientas WHERE agente_canal_id = $1", n["id"])}
    assert claves == {"tool_on"}
    # Referencia del negocio = copia del principal.
    ref = await c.fetchrow("SELECT * FROM config_referencia_negocio WHERE tenant_id = $1", t)
    assert ref is not None and ref["system_prompt"] == "Eres un asistente." and ref["herramientas"] == ["tool_on"]
    # La credencial, el canal y la configuración actual no se tocan.
    assert await c.fetchval("SELECT phone_number_id FROM channel_credentials WHERE tenant_id = $1 AND channel_type = 'whatsapp'", t) == "pn-mig-ok"
    assert await c.fetchval("SELECT is_active FROM tenant_channels WHERE tenant_id = $1 AND channel_type = 'whatsapp'", t) is True
    assert await c.fetchval("SELECT history_window FROM tenant_agent_config WHERE tenant_id = $1", t) == 40
    # El mensaje sigue resolviendo al mismo negocio.
    assert await c.fetchval("SELECT resuelve_tenant('whatsapp', 'pn-mig-ok')") == t


@requiere_bd
@pytest.mark.parametrize(
    "kw",
    [
        {"prompt": ""},
        {"prompt": "   "},
        {"nombre": ""},
        {"config": False},
    ],
    ids=["sin_prompt", "prompt_en_blanco", "sin_nombre", "sin_configuracion"],
)
async def test_A63_CL17_un_numero_sin_configuracion_completa_queda_activo_incompleto_y_sin_principal_db(bd, kw) -> None:
    c = bd.conn
    t = await _negocio_antiguo(c, "pn-mig-inc", **kw)
    await _migrar(c)

    filas = await _numeros(c, t)
    assert len(filas) == 1
    assert (filas[0]["estado"], filas[0]["es_principal"]) == ("activo", False)
    assert await c.fetchval("SELECT configuracion_incompleta($1, $2)", filas[0]["agent_name"], filas[0]["system_prompt"]) is True
    assert await c.fetchval("SELECT count(*) FROM config_referencia_negocio WHERE tenant_id = $1", t) == 0
    # Sigue siendo del negocio (pero no responde: lo decide el agente por RF-7).
    assert await c.fetchval("SELECT resuelve_tenant('whatsapp', 'pn-mig-inc')") == t


@requiere_bd
async def test_A62_CL27_un_agente_apagado_se_conserva_apagado_db(bd) -> None:
    c = bd.conn
    t = await _negocio_antiguo(c, "pn-mig-off", agente=False)
    await _migrar(c)
    n = (await _numeros(c, t))[0]
    assert (n["agente_activo"], n["estado"], n["es_principal"]) == (False, "activo", True)


@requiere_bd
async def test_CL19_un_canal_desconectado_no_se_migra_db(bd) -> None:
    c = bd.conn
    t = await _negocio_antiguo(c, "pn-mig-off-canal", canal_activo=False)
    await _migrar(c)
    assert await _numeros(c, t) == []
    assert await c.fetchval("SELECT resuelve_tenant('whatsapp', 'pn-mig-off-canal')") is None


@requiere_bd
async def test_A62_las_conversaciones_de_whatsapp_se_asocian_al_numero_y_las_demas_no_db(bd) -> None:
    c = bd.conn
    t = await _negocio_antiguo(c, "pn-mig-conv")
    await _migrar(c)
    n = (await _numeros(c, t))[0]
    por_canal = {
        r["channel_type"]: r["agente_canal_id"]
        for r in await c.fetch("SELECT channel_type, agente_canal_id FROM conversations WHERE tenant_id = $1", t)
    }
    assert por_canal == {"whatsapp": n["id"], "facebook": None}


@requiere_bd
async def test_A71_las_herramientas_habilitadas_tambien_pasan_a_instagram_facebook_y_widget_db(bd) -> None:
    c = bd.conn
    t = await _negocio_antiguo(c, "pn-mig-social")
    # La siembra de social es de una sola vez: en una base ya migrada la marca ya existe.
    await c.execute("DELETE FROM numeros_despliegue WHERE clave = 'siembra_social_herramientas'")
    await _migrar(c)
    claves = {r["tool_key"] for r in await c.fetch("SELECT tool_key FROM social_herramientas WHERE tenant_id = $1", t)}
    assert claves == {"tool_on"}


@requiere_bd
async def test_A65_la_migracion_no_fija_ningun_tope_en_los_planes_db(bd) -> None:
    c = bd.conn
    antes = await c.fetch("SELECT nombre, max_numeros_whatsapp FROM planes ORDER BY nombre")
    await _migrar(c)
    assert await c.fetch("SELECT nombre, max_numeros_whatsapp FROM planes ORDER BY nombre") == antes


@requiere_bd
async def test_RF22_reaplicar_la_migracion_de_datos_no_duplica_ni_pisa_cambios_del_negocio_db(bd) -> None:
    c = bd.conn
    t = await _negocio_antiguo(c, "pn-mig-idem")
    await _migrar(c)
    n = (await _numeros(c, t))[0]

    # El negocio cambia cosas después de migrar: quita una herramienta, apaga el agente y cambia el prompt.
    await c.execute("DELETE FROM agente_canal_herramientas WHERE agente_canal_id = $1", n["id"])
    await c.execute("UPDATE agentes_canal SET agente_activo = false, system_prompt = 'Otro prompt' WHERE id = $1", n["id"])
    await c.execute("UPDATE conversations SET agente_canal_id = NULL WHERE tenant_id = $1", t)

    await _migrar(c)
    await _migrar(c)

    filas = await _numeros(c, t)
    assert len(filas) == 1
    assert (filas[0]["agente_activo"], filas[0]["system_prompt"]) == (False, "Otro prompt")
    assert await c.fetchval("SELECT count(*) FROM agente_canal_herramientas WHERE agente_canal_id = $1", n["id"]) == 0
    assert await c.fetchval("SELECT count(*) FROM conversations WHERE tenant_id = $1 AND agente_canal_id IS NOT NULL", t) == 0


@requiere_bd
async def test_RF22_un_numero_desvinculado_no_se_recrea_al_reaplicar_db(bd) -> None:
    c = bd.conn
    t = await _negocio_antiguo(c, "pn-mig-desv")
    await _migrar(c)
    await c.execute(
        "UPDATE agentes_canal SET es_principal = false, estado = 'desvinculado', desvinculado_en = NOW() WHERE tenant_id = $1", t
    )
    await _migrar(c)
    filas = await _numeros(c, t)
    assert [f["estado"] for f in filas] == ["desvinculado"]


@requiere_bd
async def test_RF22_un_identificador_compartido_por_dos_negocios_migra_solo_uno_y_no_rompe_db(bd) -> None:
    c = bd.conn
    viejo = await _negocio_antiguo(c, "pn-mig-dup")
    nuevo = await _negocio_antiguo(c, "pn-mig-dup-otro")
    await c.execute("UPDATE channel_credentials SET phone_number_id = 'pn-mig-dup', updated_at = NOW() WHERE tenant_id = $1", nuevo)
    await c.execute("UPDATE channel_credentials SET updated_at = NOW() - interval '30 days' WHERE tenant_id = $1", viejo)
    await _migrar(c)
    duenos = await c.fetch("SELECT tenant_id FROM agentes_canal WHERE phone_number_id = 'pn-mig-dup'")
    assert [d["tenant_id"] for d in duenos] == [nuevo]  # gana la credencial más reciente


# ------------------------------------------------------------
# Idempotencia de los cuatro archivos
# ------------------------------------------------------------
async def _foto(c) -> dict:
    conteos = {t: await c.fetchval(f"SELECT count(*) FROM {t}") for t in TABLAS_FOTO}
    funciones = {
        r["proname"]: r["h"]
        for r in await c.fetch(
            "SELECT proname, md5(string_agg(pg_get_functiondef(oid), '|' ORDER BY oid::text)) AS h "
            "FROM pg_proc WHERE proname = ANY($1::text[]) GROUP BY proname",
            list(FUNCIONES_FOTO),
        )
    }
    triggers = sorted(
        (r["tgname"], r["tabla"])
        for r in await c.fetch(
            "SELECT tgname, tgrelid::regclass::text AS tabla FROM pg_trigger "
            "WHERE NOT tgisinternal AND (tgname LIKE 'recortar_agente%' OR tgname LIKE 'numeros_encolar%')"
        )
    )
    restricciones = sorted(
        r["conname"]
        for r in await c.fetch(
            "SELECT conname FROM pg_constraint WHERE conrelid = ANY(ARRAY["
            "'agentes_canal'::regclass, 'numeros_bitacora'::regclass, 'plan_herramientas'::regclass, 'planes'::regclass])"
        )
    )
    indices = sorted(r["indexname"] for r in await c.fetch("SELECT indexname FROM pg_indexes WHERE indexname LIKE '%agentes_canal%' OR indexname LIKE '%numeros_%'"))
    return {"conteos": conteos, "funciones": funciones, "triggers": triggers, "restricciones": restricciones, "indices": indices}


@requiere_bd
async def test_RF22_reaplicar_los_cuatro_archivos_no_cambia_nada_db(bd) -> None:
    c = bd.conn
    await _negocio_antiguo(c, "pn-mig-reaplicar")
    await _migrar(c)
    antes = await _foto(c)
    assert antes["funciones"].keys() == set(FUNCIONES_FOTO)
    for numero in sorted(ARCHIVOS):
        await c.execute(_leer(numero))
    assert await _foto(c) == antes


@requiere_bd
async def test_RF20_la_bd_acepta_los_tipos_nuevos_y_rechaza_otros_db(bd) -> None:
    c = bd.conn
    await c.execute((DIR_SQL / ARCHIVO_ALERTAS).read_text(encoding="utf-8"))
    await c.execute((DIR_SQL / ARCHIVO_ALERTAS).read_text(encoding="utf-8"))  # idempotente
    cuenta = await c.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'alertas_tipo_check'")
    for tipo in ("numeros_pausados", "numeros_recortados", "numero_configuracion_incompleta", "aviso_cita_no_enviado"):
        assert f"'{tipo}'" in cuenta
