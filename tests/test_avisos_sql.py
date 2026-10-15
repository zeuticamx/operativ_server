"""
Migraciones 48 a 51 (spec 004: avisos de citas por WhatsApp y correo espejo).

Pruebas estructurales: leen los .sql sin base de datos y comprueban que sean
idempotentes y que lleven las garantías que el servicio da por hechas
(unicidad del aviso, CHECK de estados y motivos, siembra de plantillas, lista
de tipos de alertas).
"""

import re
from pathlib import Path

import pytest

DIR_SQL = Path(__file__).resolve().parent.parent / "sql"

ARCHIVOS = {
    48: "48_avisos_cita.sql",
    49: "49_plantillas_whatsapp_globales.sql",
    50: "50_contexto_agente_avisos.sql",
    51: "51_alertas_aviso_cita.sql",
}

MOTIVOS = [
    "sin_telefono", "telefono_invalido", "canal_no_soportado",
    "desactivado_por_configuracion", "plantilla_no_aprobada",
    "plantilla_no_disponible", "cita_no_confirmada", "hora_cambiada",
    "cita_ya_iniciada", "error_proveedor", "resultado_desconocido",
]


def _leer(numero: int) -> str:
    return (DIR_SQL / ARCHIVOS[numero]).read_text(encoding="utf-8")


def _sin_comentarios(sql: str) -> str:
    return "\n".join(l.split("--")[0] for l in sql.splitlines())


def _tipos_alertas(sql: str) -> list[str]:
    """Lista de tipos del último CHECK alertas_tipo_check de un archivo."""
    sql = _sin_comentarios(sql)
    bloque = sql[sql.rindex("ADD CONSTRAINT alertas_tipo_check"):]
    bloque = bloque[: bloque.index(";")]
    return re.findall(r"'([a-z_]+)'", bloque)


@pytest.mark.parametrize("numero", sorted(ARCHIVOS))
def test_archivo_existe(numero: int) -> None:
    assert (DIR_SQL / ARCHIVOS[numero]).is_file()


def test_no_hay_migracion_47_duplicada() -> None:
    # La 47 es de la spec 003: esta spec no crea otra con ese número.
    assert len(list(DIR_SQL.glob("47_*.sql"))) <= 1


@pytest.mark.parametrize("numero", sorted(ARCHIVOS))
def test_es_idempotente(numero: int) -> None:
    sql = _sin_comentarios(_leer(numero))
    for m in re.finditer(r"CREATE\s+(UNIQUE\s+)?(TABLE|INDEX)\s+(?!IF NOT EXISTS)", sql, re.I):
        pytest.fail(f"CREATE sin IF NOT EXISTS en {ARCHIVOS[numero]}: {m.group(0)}")
    assert not re.search(r"\bDROP\s+TABLE\b", sql, re.I)
    for m in re.finditer(r"\bDROP\s+CONSTRAINT\s+(?!IF EXISTS)", sql, re.I):
        pytest.fail(f"DROP CONSTRAINT sin IF EXISTS en {ARCHIVOS[numero]}")
    for m in re.finditer(r"ADD\s+COLUMN\s+(?!IF NOT EXISTS)", sql, re.I):
        pytest.fail(f"ADD COLUMN sin IF NOT EXISTS en {ARCHIVOS[numero]}")


def test_48_tablas_y_unicidad() -> None:
    sql = _sin_comentarios(_leer(48))
    for tabla in ("avisos_cita_ejecuciones", "avisos_cita", "cuenta_global_respuestas"):
        assert re.search(rf"CREATE TABLE IF NOT EXISTS {tabla}\b", sql), tabla
    # D6: la unicidad la da la BD sobre (reserva, tipo, hora de inicio).
    assert re.search(r"UNIQUE\s*\(\s*reserva_id\s*,\s*tipo\s*,\s*hora_inicio\s*\)", sql)
    assert "telefono_destino" in sql
    assert re.search(r"reserva_id\s+UUID\s+NOT NULL\s+REFERENCES reservas\(id\)\s+ON DELETE CASCADE", sql)
    assert re.search(r"tenant_id\s+UUID\s+NOT NULL\s+REFERENCES tenants\(id\)\s+ON DELETE CASCADE", sql)


def test_48_check_de_tipo_estado_y_motivos() -> None:
    sql = _sin_comentarios(_leer(48))
    assert re.search(r"tipo\s+IN\s*\(\s*'cancelacion'\s*,\s*'recordatorio'\s*\)", sql)
    assert re.search(
        r"estado\s+IN\s*\(\s*'en_curso'\s*,\s*'enviado'\s*,\s*'omitido'\s*,\s*'fallido'\s*\)", sql
    )
    for motivo in MOTIVOS:
        assert f"'{motivo}'" in sql, motivo
    # Coherencia: en_curso y enviado sin motivo; omitido y fallido con motivo.
    assert re.search(r"estado IN \('en_curso', 'enviado'\) AND motivo IS NULL", sql)
    assert re.search(r"estado IN \('omitido', 'fallido'\) AND motivo IS NOT NULL", sql)


def test_49_siembra_las_dos_plantillas() -> None:
    sql = _sin_comentarios(_leer(49))
    assert re.search(r"CREATE TABLE IF NOT EXISTS plantillas_whatsapp_globales\b", sql)
    assert "'notificacion_cancelacion'" in sql
    assert "'recordatorio_cita'" in sql
    assert "ON CONFLICT" in sql and "DO NOTHING" in sql
    assert sql.count("'no_solicitada'") >= 2  # default o siembra de ambas
    for estado in ("pendiente_aprobacion", "aprobada", "rechazada", "pausada"):
        assert f"'{estado}'" in sql
    assert re.search(r"actualizada_por\s+UUID\s+REFERENCES gerencia_users\(id\)\s+ON DELETE SET NULL", sql)
    # Plataforma: sin tenant_id y sin tabla ni funciones de credenciales.
    assert "tenant_id" not in sql
    assert "plataforma_credenciales" not in sql
    assert "SECURITY DEFINER" not in sql


def test_50_contexto_agente_eventos() -> None:
    sql = _sin_comentarios(_leer(50))
    assert re.search(r"CREATE TABLE IF NOT EXISTS contexto_agente_eventos\b", sql)
    assert re.search(r"conversation_id\s+UUID\s+NOT NULL\s+REFERENCES conversations\(id\)\s+ON DELETE CASCADE", sql)
    assert re.search(r"aviso_id\s+UUID\s+NOT NULL\s+UNIQUE\s+REFERENCES avisos_cita\(id\)\s+ON DELETE CASCADE", sql)
    assert re.search(r"INDEX IF NOT EXISTS \w+\s+ON contexto_agente_eventos\s*\(\s*conversation_id\s*,\s*creado_en", sql)
    # El backend no escribe en tablas de n8n.
    assert not re.search(r"\b(INSERT INTO|UPDATE|ALTER TABLE)\s+(conversations|users)\b", sql, re.I)


def test_51_tipos_de_alertas_son_los_de_46_mas_uno() -> None:
    previos = _tipos_alertas((DIR_SQL / "46_push_lead_asignado.sql").read_text(encoding="utf-8"))
    nuevos = _tipos_alertas(_leer(51))
    assert nuevos[: len(previos)] == previos
    assert nuevos[len(previos):] == ["aviso_cita_no_enviado"]
    assert re.search(r"DROP CONSTRAINT IF EXISTS alertas_tipo_check", _leer(51))
