"""
Estructural, sin base de datos: sql/62_refresh_sesiones.sql (spec 006)
trae la tabla y el índice que routers/auth.py da por hechos, y es
idempotente (mismo criterio que tests/test_numeros_sql.py).
"""

import re
from pathlib import Path

DIR_SQL = Path(__file__).resolve().parent.parent / "sql"
ARCHIVO = DIR_SQL / "62_refresh_sesiones.sql"


def _texto() -> str:
    return ARCHIVO.read_text(encoding="utf-8")


def test_existe_un_solo_archivo_62():
    assert len(list(DIR_SQL.glob("62_*.sql"))) == 1


def test_trae_tabla_e_indice():
    sql = _texto()
    assert re.search(r"CREATE TABLE IF NOT EXISTS refresh_sesiones", sql)
    assert re.search(r"CREATE INDEX IF NOT EXISTS ix_refresh_sesiones_user", sql)
    # Claves que la lógica de rotación da por hechas.
    for columna in ("jti", "user_id", "emitido_en", "expira_en", "usado_en"):
        assert columna in sql


def test_es_idempotente():
    sql = _texto()
    for m in re.finditer(r"CREATE\s+(UNIQUE\s+)?(TABLE|INDEX)\s+(?!IF NOT EXISTS)", sql, re.I):
        raise AssertionError(f"CREATE sin IF NOT EXISTS: {m.group(0)}")
