"""
Enlace entre un cliente de campo y un lead del embudo (sql/38_clientes_lead.sql).

Mismo patrón que test_clientes_alta.py: se sustituyen las funciones que tocan la
base (`fetch_one`, `fetch_all`, `cargar_cliente`) y se prueba la regla, no la SQL.
`cargar_cliente` se sustituye aparte porque vive en `services.crm` y usa su propio
`fetch_one` ahí — parchar el de `routers.clientes` no lo alcanza.
"""

from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi import HTTPException

from routers import clientes
from services.crm import AccesoCRM
from schemas import ClienteVincularLeadIn

TENANT = uuid4()
CLIENTE = uuid4()
CONTACTO_LIBRE = uuid4()
CONTACTO_YA_VINCULADO = uuid4()
CONTACTO_DE_OTRO_TENANT = uuid4()


def acceso_gerencia() -> AccesoCRM:
    return AccesoCRM(
        tenant_id=TENANT, vendedor_id=None, es_gerencia=True, etiqueta="dueño@ejemplo.com"
    )


def acceso_vendedor() -> AccesoCRM:
    return AccesoCRM(tenant_id=TENANT, vendedor_id=uuid4(), es_gerencia=False, etiqueta="Ana")


def fila_cliente(user_id: UUID | None = None) -> dict:
    return {
        "id": CLIENTE,
        "tenant_id": TENANT,
        "vendedor_id": None,
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
        "user_id": user_id,
        "creado_en": "2026-10-07T12:00:00+00:00",
        "actualizado_en": "2026-10-07T12:00:00+00:00",
    }


class BaseFalsa:
    def __init__(self):
        self.usuarios_validos = {CONTACTO_LIBRE, CONTACTO_YA_VINCULADO}
        # Simula el índice único parcial: este contacto ya es de OTRO cliente.
        self.vinculado_a_otro_cliente = CONTACTO_YA_VINCULADO
        self.cliente_user_id: UUID | None = None

    async def cargar_cliente(self, cliente_id, acceso, conn=None):
        if cliente_id != CLIENTE:
            raise HTTPException(status_code=404, detail="Cliente no encontrado")
        return fila_cliente(self.cliente_user_id)

    async def fetch_one(self, sql: str, *args):
        if "SELECT id FROM users WHERE id" in sql:
            user_id, tenant_id = args
            if user_id in self.usuarios_validos and tenant_id == TENANT:
                return {"id": user_id}
            return None

        if "user_id = NULL" in sql:
            self.cliente_user_id = None
            return {"id": CLIENTE}

        if "UPDATE clientes SET user_id" in sql:
            cliente_id, tenant_id, user_id = args
            if user_id == self.vinculado_a_otro_cliente:
                raise asyncpg.UniqueViolationError("duplicate key")
            self.cliente_user_id = user_id
            return {"id": CLIENTE}

        if "FROM clientes c" in sql:
            return fila_cliente(self.cliente_user_id)

        raise AssertionError(f"query inesperada: {sql}")

    async def fetch_all(self, sql: str, *args):
        return [
            {
                "user_id": CONTACTO_LIBRE,
                "nombre": "Carlos Pérez",
                "handle": "5215512345678",
                "tiene_pipeline": True,
            }
        ]


@pytest.fixture
def base(monkeypatch):
    falsa = BaseFalsa()
    monkeypatch.setattr(clientes, "cargar_cliente", falsa.cargar_cliente)
    monkeypatch.setattr(clientes, "fetch_one", falsa.fetch_one)
    monkeypatch.setattr(clientes, "fetch_all", falsa.fetch_all)
    return falsa


async def test_buscar_devuelve_candidatos(base):
    salida = await clientes.leads_disponibles(buscar="Carlos", acceso=acceso_gerencia())

    assert len(salida) == 1
    assert salida[0].user_id == CONTACTO_LIBRE
    assert salida[0].tiene_pipeline is True


async def test_vincular_un_contacto_libre(base):
    salida = await clientes.vincular_lead(
        CLIENTE, ClienteVincularLeadIn(user_id=CONTACTO_LIBRE), acceso_gerencia()
    )

    assert salida.user_id == CONTACTO_LIBRE


async def test_vincular_contacto_de_otro_tenant_da_404(base):
    with pytest.raises(HTTPException) as error:
        await clientes.vincular_lead(
            CLIENTE,
            ClienteVincularLeadIn(user_id=CONTACTO_DE_OTRO_TENANT),
            acceso_gerencia(),
        )

    assert error.value.status_code == 404


async def test_vincular_contacto_ya_vinculado_a_otro_cliente_da_409(base):
    with pytest.raises(HTTPException) as error:
        await clientes.vincular_lead(
            CLIENTE,
            ClienteVincularLeadIn(user_id=CONTACTO_YA_VINCULADO),
            acceso_gerencia(),
        )

    assert error.value.status_code == 409


async def test_cliente_de_otro_tenant_da_404_antes_de_tocar_el_contacto(base):
    with pytest.raises(HTTPException) as error:
        await clientes.vincular_lead(
            uuid4(), ClienteVincularLeadIn(user_id=CONTACTO_LIBRE), acceso_gerencia()
        )

    assert error.value.status_code == 404


async def test_desvincular_limpia_el_enlace(base):
    base.cliente_user_id = CONTACTO_LIBRE

    salida = await clientes.desvincular_lead(CLIENTE, acceso_gerencia())

    assert salida.user_id is None


async def test_vendedor_no_puede_vincular(base):
    """`exigir_gerencia_crm` rechaza antes de llegar al cuerpo del endpoint."""
    from services.crm import exigir_gerencia_crm

    with pytest.raises(HTTPException) as error:
        await exigir_gerencia_crm(acceso_vendedor())

    assert error.value.status_code == 403
