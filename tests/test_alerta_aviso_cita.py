"""
Alerta `aviso_cita_no_enviado` (spec 004) y limpieza de tareas en segundo plano.

El tipo debe estar en `schemas.TipoAlerta`: `crear_alerta` valida la fila con
`AlertaOut`, y sin él el aviso a gerencia se perdía en silencio. Sin BD real:
`fetch_one`/`fetch_all` se simulan y `broadcast_alerta`/`avisar_gerencia` son
los reales.
"""

from datetime import datetime, timezone
from uuid import uuid4

import realtime
from routers import alertas as alertas_router
from schemas import AlertaOut, TipoAlerta
from services import correo_espejo, push


def _instalar_bd_falsa(monkeypatch, tenant_id, cuenta_id):
    insertadas: list[dict] = []

    async def fetch_one(sql, *args):
        fila = {
            "id": uuid4(),
            "tenant_id": args[0],
            "tipo": args[1],
            "titulo": args[2],
            "mensaje": args[3],
            "datos": args[4],
            "leido": False,
            "creado_en": datetime.now(timezone.utc),
            "portal_user_id": args[5],
        }
        insertadas.append(fila)
        return fila

    async def fetch_all_alertas(sql, *args):
        return []

    async def fetch_all_realtime(sql, *args):
        return [{"id": cuenta_id}]

    async def espejar_nulo(*args, **kwargs):
        return 0

    monkeypatch.setattr(alertas_router, "fetch_one", fetch_one)
    monkeypatch.setattr(alertas_router, "fetch_all", fetch_all_alertas)
    monkeypatch.setattr(realtime, "fetch_all", fetch_all_realtime)
    monkeypatch.setattr(correo_espejo, "espejar", espejar_nulo)
    return insertadas


def test_tipo_alerta_incluye_aviso_cita_no_enviado():
    assert TipoAlerta("aviso_cita_no_enviado") is TipoAlerta.aviso_cita_no_enviado


async def test_crear_alerta_aviso_cita_no_enviado_valida(monkeypatch):
    tenant_id, cuenta_id = uuid4(), uuid4()
    _instalar_bd_falsa(monkeypatch, tenant_id, cuenta_id)

    alerta = await alertas_router.crear_alerta(
        tenant_id, "aviso_cita_no_enviado", "t", "m", {"reserva_id": "x"}, cuenta_id
    )

    assert isinstance(alerta, AlertaOut)
    assert alerta.tipo == TipoAlerta.aviso_cita_no_enviado


async def test_avisar_gerencia_emite_el_aviso_real(monkeypatch):
    tenant_id, cuenta_id = uuid4(), uuid4()
    insertadas = _instalar_bd_falsa(monkeypatch, tenant_id, cuenta_id)
    emitidos: list[tuple] = []

    async def emit(evento, datos, room=None, **kw):
        emitidos.append((evento, datos, room))

    monkeypatch.setattr(realtime.sio, "emit", emit)

    await realtime.avisar_gerencia(tenant_id, "aviso_cita_no_enviado", "t", "m", {})

    assert len(insertadas) == 1
    assert len(emitidos) == 1
    assert emitidos[0][1]["tipo"] == "aviso_cita_no_enviado"


async def test_alerta_real_de_calendario_no_deja_push_colgado(monkeypatch):
    """Con el autouse de conftest (PUSH_ACTIVO=False), push._tareas queda vacío."""
    assert push.settings.PUSH_ACTIVO is False
    tenant_id = uuid4()
    _instalar_bd_falsa(monkeypatch, tenant_id, uuid4())

    async def emit(*a, **kw):
        return None

    monkeypatch.setattr(realtime.sio, "emit", emit)

    await realtime.broadcast_alerta(tenant_id, "reserva_cancelada", "t", "m", {})

    assert not push._tareas
