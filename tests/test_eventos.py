"""
Lógica condicional del endpoint que llama n8n en cada mensaje entrante.

Se prueba `on_mensaje_entrante` con las funciones de datos sustituidas: lo
que importa acá es a quién llama y con qué, no la SQL. Las dependencias se
reemplazan en el namespace de `eventos` porque es ahí donde se resuelven.
"""

from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest

import eventos
from pipeline import Pipeline, Servicios

TENANT = uuid4()
CLIENTE = uuid4()
VENDEDOR = uuid4()
MENSAJE = {"texto": "hola, quiero precios"}


class Espia:
    """Registra cada llamada para poder afirmar sobre ellas después."""

    def __init__(self, devuelve=None):
        self.devuelve = devuelve
        self.llamadas: list[tuple] = []

    async def __call__(self, *args, **kwargs):
        self.llamadas.append((args, kwargs))
        if callable(self.devuelve):
            return self.devuelve(*args, **kwargs)
        return self.devuelve

    @property
    def veces(self) -> int:
        return len(self.llamadas)


def embudo(vendedor_id=None, recien_creado=True) -> Pipeline:
    return Pipeline(
        id=uuid4(),
        tenant_id=TENANT,
        user_id=CLIENTE,
        vendedor_id=vendedor_id,
        estado="nuevo",
        monto_estimado=None,
        motivo_perdida=None,
        recien_creado=recien_creado,
    )


@pytest.fixture
def escenario(monkeypatch):
    """
    Deja `eventos` con todo sustituido y devuelve los espías para
    configurarlos. Por defecto: módulo activo, agente apagado, un vendedor
    disponible y el cliente todavía sin embudo.
    """

    class Escenario:
        servicios = Espia(
            Servicios(
                tenant_id=TENANT,
                agente_ia_activo=False,
                gestion_vendedores_activo=True,
            )
        )
        get_or_create = Espia(embudo())
        asignar_auto = Espia(VENDEDOR)
        asignar = Espia()
        notificar = Espia(True)
        transacciones = 0

    esc = Escenario()

    @asynccontextmanager
    async def fake_transaccion():
        esc.transacciones += 1
        # Los fakes no tocan la conexión, pero se pasa algo para que el
        # código no distinga este camino del real.
        yield object()

    monkeypatch.setattr(eventos, "get_tenant_servicios", esc.servicios)
    monkeypatch.setattr(eventos, "get_or_create_pipeline", esc.get_or_create)
    monkeypatch.setattr(eventos, "asignar_vendedor_automatico", esc.asignar_auto)
    monkeypatch.setattr(eventos, "asignar_vendedor", esc.asignar)
    monkeypatch.setattr(eventos, "notificar_vendedor_nuevo_lead", esc.notificar)
    monkeypatch.setattr(eventos, "transaccion", fake_transaccion)
    return esc


# ============================================================
# Módulo apagado
# ============================================================
async def test_modulo_apagado_no_toca_el_embudo(escenario):
    """
    Un tenant que solo usa el agente no paga ni una escritura: se responden
    los flags y se sale.
    """
    escenario.servicios.devuelve = Servicios(
        tenant_id=TENANT, agente_ia_activo=True, gestion_vendedores_activo=False
    )

    salida = await eventos.on_mensaje_entrante(TENANT, CLIENTE, MENSAJE)

    assert salida.gestion_vendedores_activo is False
    assert salida.agente_ia_activo is True
    assert escenario.get_or_create.veces == 0
    assert escenario.asignar_auto.veces == 0
    assert escenario.transacciones == 0


async def test_modulo_apagado_igual_reporta_el_flag_del_agente(escenario):
    """n8n usa esta respuesta para decidir si sigue hacia el nodo del agente."""
    escenario.servicios.devuelve = Servicios(
        tenant_id=TENANT, agente_ia_activo=False, gestion_vendedores_activo=False
    )
    salida = await eventos.on_mensaje_entrante(TENANT, CLIENTE, MENSAJE)
    assert salida.agente_ia_activo is False


# ============================================================
# Módulo activo, con vendedores
# ============================================================
async def test_lead_nuevo_queda_asignado(escenario):
    salida = await eventos.on_mensaje_entrante(TENANT, CLIENTE, MENSAJE)

    assert salida.vendedor_id == VENDEDOR
    assert salida.asignado_ahora is True
    assert escenario.asignar.veces == 1


async def test_la_asignacion_se_escribe_dentro_de_la_transaccion(escenario):
    await eventos.on_mensaje_entrante(TENANT, CLIENTE, MENSAJE)
    assert escenario.transacciones == 1


async def test_cliente_que_ya_tiene_vendedor_no_se_reasigna(escenario):
    """
    Un cliente que vuelve a escribir sigue siendo del mismo vendedor: si
    cada mensaje disparara el reparto, el lead cambiaría de dueño solo.
    """
    escenario.get_or_create.devuelve = embudo(vendedor_id=VENDEDOR, recien_creado=False)

    salida = await eventos.on_mensaje_entrante(TENANT, CLIENTE, MENSAJE)

    assert escenario.asignar_auto.veces == 0
    assert escenario.asignar.veces == 0
    assert salida.asignado_ahora is False
    assert salida.vendedor_id == VENDEDOR


# ============================================================
# Módulo activo SIN vendedores activos (caso borde pedido)
# ============================================================
async def test_sin_vendedores_activos_el_lead_se_crea_igual(escenario):
    """
    El embudo se crea, queda con vendedor_id NULL y no se lanza nada: el
    lead aparece en el panel de gerencia como pendiente de asignar en vez
    de perderse.
    """
    escenario.asignar_auto.devuelve = None

    salida = await eventos.on_mensaje_entrante(TENANT, CLIENTE, MENSAJE)

    assert escenario.get_or_create.veces == 1
    assert salida.vendedor_id is None
    assert salida.asignado_ahora is False
    # No se escribe una asignación vacía: la fila ya nace con NULL.
    assert escenario.asignar.veces == 0


async def test_sin_vendedores_activos_no_se_notifica_a_nadie(escenario):
    escenario.asignar_auto.devuelve = None
    await eventos.on_mensaje_entrante(TENANT, CLIENTE, MENSAJE)
    assert escenario.notificar.veces == 0


async def test_estrategia_manual_deja_el_lead_sin_dueno(escenario):
    """
    'manual' devuelve None igual que "no hay vendedores": el embudo se crea
    y una persona decide después.
    """
    escenario.asignar_auto.devuelve = None
    salida = await eventos.on_mensaje_entrante(TENANT, CLIENTE, MENSAJE)
    assert salida.vendedor_id is None
    assert salida.gestion_vendedores_activo is True


# ============================================================
# Cuándo sale el aviso al vendedor
# ============================================================
async def test_con_el_agente_apagado_se_avisa_al_vendedor(escenario):
    """Nadie está atendiendo al cliente: el vendedor tiene que enterarse ya."""
    await eventos.on_mensaje_entrante(TENANT, CLIENTE, MENSAJE)

    assert escenario.notificar.veces == 1
    args, kwargs = escenario.notificar.llamadas[0]
    assert args[0] == VENDEDOR
    assert args[1] == CLIENTE
    assert args[2] == MENSAJE
    assert kwargs["tenant_id"] == TENANT


async def test_con_el_agente_encendido_no_se_avisa(escenario):
    """
    El agente ya está contestando; el vendedor entra después mirando su
    cartera, no con un empujón por cada mensaje.
    """
    escenario.servicios.devuelve = Servicios(
        tenant_id=TENANT, agente_ia_activo=True, gestion_vendedores_activo=True
    )

    salida = await eventos.on_mensaje_entrante(TENANT, CLIENTE, MENSAJE)

    assert salida.asignado_ahora is True
    assert escenario.notificar.veces == 0


async def test_no_se_avisa_por_un_cliente_que_ya_tenia_vendedor(escenario):
    escenario.get_or_create.devuelve = embudo(vendedor_id=VENDEDOR, recien_creado=False)
    await eventos.on_mensaje_entrante(TENANT, CLIENTE, MENSAJE)
    assert escenario.notificar.veces == 0


async def test_un_aviso_que_falla_no_tumba_el_mensaje_entrante(escenario, monkeypatch):
    """
    n8n necesita la respuesta para seguir su workflow. El lead ya quedó
    asignado; que el aviso no salga no puede costar el mensaje del cliente.
    """

    async def explota(*args, **kwargs):
        raise RuntimeError("el sub-workflow de n8n no respondió")

    monkeypatch.setattr(eventos, "notificar_vendedor_nuevo_lead", explota)

    salida = await eventos.on_mensaje_entrante(TENANT, CLIENTE, MENSAJE)

    assert salida.vendedor_id == VENDEDOR
    assert salida.asignado_ahora is True
