"""
Alertas persistentes al cambiar de manos una conversación (tomar, soltar,
reasignar, devolver a la IA). Sin BD: se reemplazan las dos vías de realtime.
"""

from uuid import uuid4

from services import avisos_conversacion as avisos


def _parchar(monkeypatch):
    negocio: list[tuple] = []
    personales: list[tuple] = []

    async def al_negocio(tenant_id, tipo, titulo, mensaje, datos=None, **kw):
        negocio.append((tenant_id, tipo, mensaje, datos))

    async def a_usuario(tenant_id, portal_user_id, tipo, titulo, mensaje, datos=None):
        personales.append((tenant_id, portal_user_id, tipo, datos))

    monkeypatch.setattr(avisos, "broadcast_alerta", al_negocio)
    monkeypatch.setattr(avisos, "avisar_a_usuario", a_usuario)
    return negocio, personales


async def test_tomada_alerta_al_negocio_con_la_conversacion(monkeypatch):
    negocio, personales = _parchar(monkeypatch)
    tenant, conv = uuid4(), uuid4()

    await avisos.tomada(tenant, conv, "ana@negocio.com")

    (tid, tipo, mensaje, datos), = negocio
    assert (tid, tipo) == (tenant, "conversacion_tomada")
    assert datos == {"conversation_id": str(conv)}
    assert "ana@negocio.com" in mensaje
    assert personales == []


async def test_tomada_por_soporte_no_expone_el_correo_del_gerente(monkeypatch):
    negocio, _ = _parchar(monkeypatch)

    await avisos.tomada(uuid4(), uuid4(), "gerente@operativai.com", desde_soporte=True)

    assert "gerente@operativai.com" not in negocio[0][2]
    assert "soporte" in negocio[0][2]


async def test_liberada_avisa_al_asignado_anterior(monkeypatch):
    negocio, personales = _parchar(monkeypatch)
    tenant, conv, anterior, owner = uuid4(), uuid4(), uuid4(), uuid4()

    await avisos.liberada(tenant, conv, anterior, owner, "owner@negocio.com")

    (tid, uid, tipo, datos), = personales
    assert (tid, uid, tipo) == (tenant, anterior, "conversacion_liberada")
    assert datos["conversation_id"] == str(conv)
    assert negocio == []


async def test_liberada_no_avisa_si_la_suelta_el_mismo_o_no_habia_dueno(monkeypatch):
    _, personales = _parchar(monkeypatch)
    yo = uuid4()

    await avisos.liberada(uuid4(), uuid4(), yo, yo, "yo@negocio.com")
    await avisos.liberada(uuid4(), uuid4(), None, yo, "yo@negocio.com")

    assert personales == []


async def test_reasignada_cambia_el_texto_del_aviso(monkeypatch, ):
    capturado: list[str] = []

    async def a_usuario(tenant_id, portal_user_id, tipo, titulo, mensaje, datos=None):
        capturado.append(mensaje)

    monkeypatch.setattr(avisos, "avisar_a_usuario", a_usuario)

    await avisos.liberada(uuid4(), uuid4(), uuid4(), uuid4(), "owner", reasignada=True)

    assert "reasignó" in capturado[0]


async def test_devuelta_a_ia_avisa_al_negocio_y_al_anterior(monkeypatch):
    negocio, personales = _parchar(monkeypatch)
    tenant, conv, anterior, actor = uuid4(), uuid4(), uuid4(), uuid4()

    await avisos.devuelta_a_ia(tenant, conv, anterior, actor, "owner@negocio.com")

    assert [n[1] for n in negocio] == ["conversacion_devuelta_ia"]
    assert [(p[1], p[2]) for p in personales] == [(anterior, "conversacion_devuelta_ia")]


async def test_devuelta_a_ia_por_el_propio_asignado_no_le_manda_copia(monkeypatch):
    negocio, personales = _parchar(monkeypatch)
    yo = uuid4()

    await avisos.devuelta_a_ia(uuid4(), uuid4(), yo, yo, "yo@negocio.com")

    assert len(negocio) == 1 and personales == []


async def test_un_fallo_al_avisar_no_rompe_la_operacion(monkeypatch):
    async def revienta(*_a, **_k):
        raise RuntimeError("BD caída")

    monkeypatch.setattr(avisos, "broadcast_alerta", revienta)
    monkeypatch.setattr(avisos, "avisar_a_usuario", revienta)

    await avisos.tomada(uuid4(), uuid4(), "ana")
    await avisos.liberada(uuid4(), uuid4(), uuid4(), uuid4(), "owner")
    await avisos.devuelta_a_ia(uuid4(), uuid4(), uuid4(), uuid4(), "owner")
