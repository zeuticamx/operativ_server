"""
Alta de clientes: quién puede y a quién le queda asignado el cliente nuevo.

Se sustituye `fetch_one` (la única pieza que toca la base) igual que en
test_visitas.py: lo que se prueba es la regla de asignación, no la SQL.
"""

from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from routers import clientes
from services.crm import AccesoCRM
from schemas import ClienteCrearIn

TENANT = uuid4()
VENDEDOR = uuid4()
OTRO_VENDEDOR = uuid4()
CLIENTE_NUEVO = uuid4()


def acceso_vendedor() -> AccesoCRM:
    return AccesoCRM(
        tenant_id=TENANT, vendedor_id=VENDEDOR, es_gerencia=False, etiqueta="Ana"
    )


def acceso_gerencia() -> AccesoCRM:
    return AccesoCRM(
        tenant_id=TENANT, vendedor_id=None, es_gerencia=True, etiqueta="dueño@ejemplo.com"
    )


def datos(**extra) -> ClienteCrearIn:
    base = {
        "nombre_negocio": "Abarrotes La Esquina",
        "latitud": 19.432608,
        "longitud": -99.133209,
    }
    return ClienteCrearIn(**{**base, **extra})


class BaseFalsa:
    """Captura el INSERT y devuelve la fila que leería el SELECT posterior."""

    def __init__(self):
        self.insertado: dict | None = None
        self.vendedores_validados: list[UUID] = []

    async def fetch_one(self, sql: str, *args):
        if "INSERT INTO clientes" in sql:
            self.insertado = {"tenant_id": args[0], "vendedor_id": args[1], "nombre": args[2]}
            return {"id": CLIENTE_NUEVO}

        # El SELECT_CLIENTE de vuelta: solo hace falta que ClienteOut valide.
        return {
            "id": CLIENTE_NUEVO,
            "tenant_id": TENANT,
            "vendedor_id": self.insertado["vendedor_id"] if self.insertado else None,
            "vendedor_nombre": None,
            "nombre_negocio": "Abarrotes La Esquina",
            "contacto_nombre": None,
            "telefono": None,
            "direccion": None,
            "latitud": 19.432608,
            "longitud": -99.133209,
            "radio_tolerancia_metros": 120,
            "estado": "prospecto",
            "prioridad": "media",
            "notas": None,
            "creado_en": "2026-10-06T12:00:00+00:00",
            "actualizado_en": "2026-10-06T12:00:00+00:00",
        }

    async def vendedor_del_tenant(self, vendedor_id, tenant_id, conn=None):
        self.vendedores_validados.append(vendedor_id)
        if vendedor_id != OTRO_VENDEDOR:
            raise HTTPException(status_code=404, detail="Vendedor no encontrado")
        return {"id": vendedor_id, "nombre": "Beto", "activo": True}


@pytest.fixture
def base(monkeypatch):
    falsa = BaseFalsa()
    monkeypatch.setattr(clientes, "fetch_one", falsa.fetch_one)
    monkeypatch.setattr(clientes, "vendedor_del_tenant", falsa.vendedor_del_tenant)
    return falsa


async def test_vendedor_puede_crear_y_el_cliente_le_queda_asignado(base):
    salida = await clientes.crear(datos(), acceso_vendedor())

    assert base.insertado["vendedor_id"] == VENDEDOR
    assert salida.vendedor_id == VENDEDOR


async def test_vendedor_no_puede_asignar_el_cliente_a_un_companero(base):
    """El `vendedor_id` del body se ignora, no se valida ni se respeta."""
    await clientes.crear(datos(vendedor_id=OTRO_VENDEDOR), acceso_vendedor())

    assert base.insertado["vendedor_id"] == VENDEDOR
    assert base.vendedores_validados == []


async def test_gerencia_asigna_a_quien_quiera_de_su_equipo(base):
    await clientes.crear(datos(vendedor_id=OTRO_VENDEDOR), acceso_gerencia())

    assert base.insertado["vendedor_id"] == OTRO_VENDEDOR
    assert base.vendedores_validados == [OTRO_VENDEDOR]


async def test_gerencia_puede_dejarlo_sin_vendedor(base):
    await clientes.crear(datos(), acceso_gerencia())

    assert base.insertado["vendedor_id"] is None
    assert base.vendedores_validados == []


async def test_gerencia_con_vendedor_de_otro_tenant_da_404(base):
    with pytest.raises(HTTPException) as error:
        await clientes.crear(datos(vendedor_id=uuid4()), acceso_gerencia())

    assert error.value.status_code == 404
