"""
services/conversaciones.tomar_conversacion: un humano toma el control desde el
portal (inversa de volver_a_ia). Sin BD: se sustituye fetch_one.
"""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from services import conversaciones as svc


def _parchar(monkeypatch, *respuestas):
    """fetch_one devuelve `respuestas` en orden y guarda cada llamada."""
    llamadas: list[tuple] = []
    pendientes = list(respuestas)

    async def falso(sql, *args):
        llamadas.append((sql, args))
        return pendientes.pop(0)

    monkeypatch.setattr(svc, "fetch_one", falso)
    return llamadas


async def test_toma_una_conversacion_activa(monkeypatch):
    conv_id, tenant_id = uuid4(), uuid4()
    llamadas = _parchar(
        monkeypatch,
        {"id": conv_id, "status": "active", "channel_type": "whatsapp"},
        {"id": conv_id, "status": "transferred"},
    )

    fila = await svc.tomar_conversacion(tenant_id, conv_id)

    assert fila["status"] == "transferred"
    sql, args = llamadas[1]
    # Condicional: no pisa una transferencia hecha por el agente entre medias.
    assert "status = 'active'" in sql
    # Sin acuse automático: quien toma la conversación escribe él mismo.
    assert "'ack_pendiente', false" in sql
    assert args == (conv_id, tenant_id)


async def test_ya_transferida_da_409(monkeypatch):
    conv_id = uuid4()
    llamadas = _parchar(
        monkeypatch,
        {"id": conv_id, "status": "transferred", "channel_type": "whatsapp"},
    )

    with pytest.raises(HTTPException) as e:
        await svc.tomar_conversacion(uuid4(), conv_id)

    assert e.value.status_code == 409
    assert len(llamadas) == 1  # no llegó a escribir


async def test_carrera_con_el_agente_da_409(monkeypatch):
    """Estaba activa al leer, pero el UPDATE condicional ya no encontró fila."""
    conv_id = uuid4()
    _parchar(
        monkeypatch,
        {"id": conv_id, "status": "active", "channel_type": "whatsapp"},
        None,
    )

    with pytest.raises(HTTPException) as e:
        await svc.tomar_conversacion(uuid4(), conv_id)

    assert e.value.status_code == 409


async def test_conversacion_de_otro_tenant_da_404(monkeypatch):
    _parchar(monkeypatch, None)

    with pytest.raises(HTTPException) as e:
        await svc.tomar_conversacion(uuid4(), uuid4())

    assert e.value.status_code == 404
