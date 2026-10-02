"""
POST /eventos/conversacion-transferida: emite la alerta en vivo al tenant
correcto con el conversation_id para que el portal navegue al chat.
"""

from uuid import uuid4

from routers import eventos
from schemas import ConversacionTransferidaIn


def _parchar(monkeypatch):
    llamadas: list[tuple] = []

    async def falso(tenant_id, tipo, titulo, mensaje, datos=None, **kw):
        llamadas.append((tenant_id, tipo, titulo, mensaje, datos))

    monkeypatch.setattr(eventos, "broadcast_alerta", falso)
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
