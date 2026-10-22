"""
Vista de gerencia a las conversaciones de un número y su revocación por el dueño.

Sin BD ni red: `usuario_actual` sustituido y las funciones de datos de
services/vista_gerencia (y la auditoría) reemplazadas por fakes en memoria.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from deps import UsuarioActual, usuario_actual
from main import app
from routers import gerencia_operacion, vista_gerencia as router_vista
from services import vista_gerencia as svc
from services.acceso_soporte import ruta_sensible

TENANT = uuid4()
NUMERO = {"agente_canal_id": uuid4(), "tenant_id": TENANT, "tenant_nombre": "Mi negocio"}


def _usuario(role="owner", *, gerencia=False, imp=False) -> UsuarioActual:
    return UsuarioActual(
        id=uuid4(),
        tenant_id=TENANT,
        email=f"{role}@ejemplo.com",
        role=role,
        es_gerencia_plataforma=gerencia,
        impersonado_por="soporte@ejemplo.com" if imp else None,
        impersonado_por_id=uuid4() if imp else None,
    )


@pytest.fixture
def mundo(monkeypatch):
    m = SimpleNamespace(numero=NUMERO, revocada=False, escrituras=[], auditoria=[])

    async def resolver():
        return m.numero

    async def revocada(tenant_id):
        return m.revocada

    async def fijar(tenant_id, valor, portal_user_id):
        m.revocada = valor
        m.escrituras.append(valor)

    async def auditar(**kw):
        m.auditoria.append(kw["accion"])

    monkeypatch.setattr(svc, "resolver_numero", resolver)
    monkeypatch.setattr(svc, "vista_revocada", revocada)
    monkeypatch.setattr(svc, "fijar_revocada", fijar)
    monkeypatch.setattr(router_vista, "registrar_auditoria", auditar)
    yield m
    app.dependency_overrides.pop(usuario_actual, None)


async def _llamar(usuario, metodo, ruta):
    app.dependency_overrides[usuario_actual] = lambda: usuario
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.request(metodo, ruta)


def test_ruta_sensible_cierra_la_decision_a_ver_como():
    assert ruta_sensible("/api/vista-gerencia/revocar")


async def test_numero_solo_para_gerencia(mundo):
    r = await _llamar(_usuario("owner"), "GET", "/api/gerencia/numero-conversaciones")
    assert r.status_code == 403


async def test_numero_resuelto(mundo):
    r = await _llamar(_usuario(gerencia=True), "GET", "/api/gerencia/numero-conversaciones")
    assert r.status_code == 200
    assert r.json()["tenant_id"] == str(TENANT)


async def test_numero_no_configurado_es_404(mundo):
    mundo.numero = None
    r = await _llamar(_usuario(gerencia=True), "GET", "/api/gerencia/numero-conversaciones")
    assert r.status_code == 404


async def test_revocada_cierra_numero_y_rutas_por_tenant(mundo):
    mundo.revocada = True
    g = _usuario(gerencia=True)
    r = await _llamar(g, "GET", "/api/gerencia/numero-conversaciones")
    assert r.status_code == 403
    assert r.json()["detail"]["codigo"] == "vista_revocada_por_dueno"
    r = await _llamar(g, "GET", f"/api/gerencia/tenants/{TENANT}/conversaciones")
    assert r.status_code == 403
    r = await _llamar(g, "POST", f"/api/gerencia/tenants/{TENANT}/conversaciones/{uuid4()}/tomar")
    assert r.status_code == 403


async def test_dueno_revoca_y_restablece_con_auditoria(mundo):
    d = _usuario("owner")
    r = await _llamar(d, "POST", "/api/vista-gerencia/revocar")
    assert r.json() == {"aplica": True, "revocada": True}
    r = await _llamar(d, "POST", "/api/vista-gerencia/restablecer")
    assert r.json() == {"aplica": True, "revocada": False}
    assert mundo.auditoria == ["vista_gerencia_revocada", "vista_gerencia_restablecida"]


async def test_negocio_ajeno_no_aplica(mundo):
    mundo.numero = {**NUMERO, "tenant_id": uuid4()}
    r = await _llamar(_usuario("owner"), "GET", "/api/vista-gerencia")
    assert r.json() == {"aplica": False, "revocada": False}
    r = await _llamar(_usuario("owner"), "POST", "/api/vista-gerencia/revocar")
    assert r.status_code == 404
    assert mundo.escrituras == []


@pytest.mark.parametrize("role", ["member", "vendedor", "proveedor"])
async def test_solo_owner_o_superadmin_deciden(mundo, role):
    r = await _llamar(_usuario(role), "POST", "/api/vista-gerencia/revocar")
    assert r.status_code == 403
    assert mundo.escrituras == []


async def test_ver_como_no_puede_decidir(mundo):
    r = await _llamar(_usuario("owner", imp=True), "POST", "/api/vista-gerencia/revocar")
    assert r.status_code == 403
    assert mundo.escrituras == []
