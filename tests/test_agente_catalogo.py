"""
Regresión: `prompt.catalogo_herramientas` rompía con KeyError
'parametros_schema' cuando el negocio tenía una herramienta activa y
permitida por plan. `herramientas_plan.catalogo` no devolvía esa columna,
y el error tumbaba la respuesta del agente a todo mensaje de ese negocio.

Sin base de datos: `conexion` y `herramientas_plan.catalogo` se sustituyen.
"""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from services import herramientas_plan
from services.agente import prompt

TENANT = uuid4()


@asynccontextmanager
async def _conexion_falsa():
    yield None


def _fila(**extra):
    base = {
        "tool_key": "consultar_pedido",
        "tool_type": "http_generico",
        "display_name": "Consultar pedido",
        "description": "Consulta el estado de un pedido por número",
        "familia": "http",
        "activa": True,
        "permitida_por_plan": True,
    }
    base.update(extra)
    return base


@pytest.fixture
def con_catalogo(monkeypatch):
    def _montar(filas):
        async def _catalogo(conn, tenant_id, agente_canal_id):
            return filas

        monkeypatch.setattr(prompt, "conexion", _conexion_falsa)
        monkeypatch.setattr(herramientas_plan, "catalogo", _catalogo)

    return _montar


class _ConnFalsa:
    """Solo lo que `herramientas_plan.catalogo` toca: `fetch` de tenant_tools."""

    def __init__(self, filas):
        self._filas = filas

    async def fetch(self, *_args):
        return self._filas


@pytest.mark.asyncio
async def test_catalogo_real_expone_el_schema_al_prompt(monkeypatch):
    """
    Pasa por `herramientas_plan.catalogo` de verdad: el bug era que esa
    función no devolvía `parametros_schema` y el prompt la leía igual.
    """
    schema = {"type": "object", "properties": {"numero": {"type": "string"}}}
    fila_bd = {
        "tool_key": "consultar_pedido",
        "tool_type": "http_generico",
        "display_name": "Consultar pedido",
        "description": "Consulta el estado de un pedido por número",
        "parametros_schema": schema,
    }

    async def _familias(conn, tenant_id):
        return {"http"}

    async def _sociales(conn, tenant_id):
        return ["consultar_pedido"]

    monkeypatch.setattr(herramientas_plan, "familias_aplicables", _familias)
    monkeypatch.setattr(herramientas_plan, "herramientas_sociales", _sociales)
    monkeypatch.setattr(herramientas_plan, "familia_de", lambda tipo, clave: "http")

    @asynccontextmanager
    async def _conexion_real_falsa():
        yield _ConnFalsa([fila_bd])

    monkeypatch.setattr(prompt, "conexion", _conexion_real_falsa)

    texto, filas = await prompt.catalogo_herramientas(TENANT, None)

    assert len(filas) == 1
    assert filas[0]["parametros_schema"] == schema
    assert 'tool_key: "consultar_pedido"' in texto
    assert "numero" in texto


@pytest.mark.asyncio
async def test_herramienta_sin_schema_usa_objeto_vacio(con_catalogo):
    con_catalogo([_fila(parametros_schema=None)])

    texto, _ = await prompt.catalogo_herramientas(TENANT, None)

    assert "Parámetros: {}" in texto


@pytest.mark.asyncio
async def test_sin_herramientas_activas_no_hay_catalogo(con_catalogo):
    con_catalogo([_fila(activa=False)])

    texto, filas = await prompt.catalogo_herramientas(TENANT, None)

    assert filas == []
    assert "No hay herramientas" in texto
