"""
POST /eventos/conversacion-transferida: emite la alerta en vivo al tenant
correcto con el conversation_id para que el portal navegue al chat.
"""

from uuid import uuid4

from routers import eventos
from schemas import ConversacionTransferidaIn


def _parchar(monkeypatch, proveedor=None, personales=None):
    """
    Reemplaza la alerta del negocio y la copia personal al proveedor (y el
    lookup de quién es el proveedor del chat, que iría a la BD).
    """
    llamadas: list[tuple] = []

    async def falso(tenant_id, tipo, titulo, mensaje, datos=None, **kw):
        llamadas.append((tenant_id, tipo, titulo, mensaje, datos))

    async def falso_personal(tenant_id, portal_user_id, tipo, titulo, mensaje, datos=None):
        if portal_user_id is not None and personales is not None:
            personales.append((tenant_id, portal_user_id, tipo, datos))

    async def cuenta(_conversation_id):
        return proveedor

    monkeypatch.setattr(eventos, "broadcast_alerta", falso)
    monkeypatch.setattr(eventos, "avisar_a_proveedor", falso_personal)
    monkeypatch.setattr(eventos.conversaciones_svc, "cuenta_del_proveedor", cuenta)
    return llamadas


async def test_emite_alerta_al_tenant_con_la_conversacion(monkeypatch):
    llamadas = _parchar(monkeypatch)
    tenant_id, conv_id = uuid4(), uuid4()

    r = await eventos.conversacion_transferida(
        ConversacionTransferidaIn(
            tenant_id=tenant_id,
            conversation_id=conv_id,
            canal="whatsapp",
            cliente_nombre="Ana",
            motivo="quiere un descuento",
        )
    )

    assert r.registrado is True
    (tid, tipo, _titulo, mensaje, datos), = llamadas
    assert tid == tenant_id
    assert tipo == "conversacion_transferida"
    assert datos == {"conversation_id": str(conv_id), "canal": "whatsapp"}
    assert "Ana" in mensaje and "quiere un descuento" in mensaje


async def test_sin_nombre_identifica_al_cliente_por_telefono(monkeypatch):
    llamadas = _parchar(monkeypatch)

    await eventos.conversacion_transferida(
        ConversacionTransferidaIn(
            tenant_id=uuid4(),
            conversation_id=uuid4(),
            canal="whatsapp",
            cliente_telefono="+5215512345678",
        )
    )

    assert "+5215512345678" in llamadas[0][3]


async def test_tambien_avisa_al_proveedor_dueno_del_chat(monkeypatch):
    proveedor = uuid4()
    personales: list[tuple] = []
    llamadas = _parchar(monkeypatch, proveedor=proveedor, personales=personales)
    tenant_id, conv_id = uuid4(), uuid4()

    await eventos.conversacion_transferida(
        ConversacionTransferidaIn(tenant_id=tenant_id, conversation_id=conv_id, canal="whatsapp")
    )

    # La del negocio sigue saliendo igual, y además una copia personal.
    assert len(llamadas) == 1
    (tid, uid, tipo, datos), = personales
    assert (tid, uid, tipo) == (tenant_id, proveedor, "conversacion_transferida")
    assert datos["conversation_id"] == str(conv_id)
