"""
POST /eventos/conversacion-transferida: emite la alerta en vivo al tenant
correcto con el conversation_id para que el portal navegue al chat.
"""

from uuid import uuid4

from routers import eventos
from schemas import ConversacionTransferidaIn
from services.asignacion_conversaciones import ResultadoAuto

# La última función de asignación falsa, para mirar con qué área se la llamó.
llamadas_auto: list = []


def _parchar(monkeypatch, proveedor=None, personales=None, resultado=None):
    """
    Reemplaza la alerta del negocio y la copia personal al proveedor (y el
    lookup de quién es el proveedor del chat, que iría a la BD), y la
    asignación automática: por defecto no asigna a nadie (`resultado=None`).
    """
    llamadas: list[tuple] = []

    async def falso(tenant_id, tipo, titulo, mensaje, datos=None, **kw):
        llamadas.append((tenant_id, tipo, titulo, mensaje, datos))

    async def falso_personal(tenant_id, portal_user_id, tipo, titulo, mensaje, datos=None):
        if portal_user_id is not None and personales is not None:
            personales.append((tenant_id, portal_user_id, tipo, datos))

    async def cuenta(_conversation_id):
        return proveedor

    async def autoasignar(tenant_id, conversation_id, area):
        autoasignar.llamadas.append(area)
        return resultado

    async def estado_falso(*_a, **_k):
        return None

    autoasignar.llamadas = []
    llamadas_auto.append(autoasignar)
    monkeypatch.setattr(eventos, "broadcast_alerta", falso)
    monkeypatch.setattr(eventos, "avisar_a_proveedor", falso_personal)
    monkeypatch.setattr(eventos, "avisar_a_usuario", falso_personal)
    monkeypatch.setattr(eventos, "emit_conversacion_estado", estado_falso)
    monkeypatch.setattr(eventos.asignacion_conv, "autoasignar", autoasignar)
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


async def test_pasa_el_area_que_sugiere_el_agente(monkeypatch):
    _parchar(monkeypatch)

    await eventos.conversacion_transferida(
        ConversacionTransferidaIn(
            tenant_id=uuid4(), conversation_id=uuid4(), canal="whatsapp", area="agenda"
        )
    )
    await eventos.conversacion_transferida(
        ConversacionTransferidaIn(tenant_id=uuid4(), conversation_id=uuid4(), canal="whatsapp")
    )

    # Sin área el agente no sugirió nada: se trata como 'otro' (al dueño).
    assert llamadas_auto[-1].llamadas == ["agenda", "otro"]


async def test_asignada_avisa_solo_al_asignado_y_no_al_proveedor_por_cita(monkeypatch):
    asignado, proveedor = uuid4(), uuid4()
    personales: list[tuple] = []
    resultado = ResultadoAuto(asignado, cayo_en_owner=False, fallo=False, motivo="Vendedor del lead del cliente")
    _parchar(monkeypatch, proveedor=proveedor, personales=personales, resultado=resultado)

    r = await eventos.conversacion_transferida(
        ConversacionTransferidaIn(tenant_id=uuid4(), conversation_id=uuid4(), canal="whatsapp", area="ventas")
    )

    assert r.asignado_a == asignado and r.cayo_en_owner is False
    # La asignación explícita manda: el proveedor de la cita no recibe copia.
    (_tid, uid, tipo, _datos), = personales
    assert (uid, tipo) == (asignado, "conversacion_asignada")


async def test_si_no_hay_responsable_cae_en_el_owner_con_alerta_de_fallo(monkeypatch):
    owner = uuid4()
    personales: list[tuple] = []
    resultado = ResultadoAuto(owner, cayo_en_owner=True, fallo=True, motivo="no hay un vendedor con cuenta activa")
    _parchar(monkeypatch, personales=personales, resultado=resultado)

    r = await eventos.conversacion_transferida(
        ConversacionTransferidaIn(tenant_id=uuid4(), conversation_id=uuid4(), canal="whatsapp", area="ventas")
    )

    assert r.asignado_a == owner and r.cayo_en_owner is True
    (_tid, uid, tipo, datos), = personales
    assert (uid, tipo) == (owner, "asignacion_fallida")
    assert "no hay un vendedor" in datos["motivo_asignacion"]


async def test_si_la_asignacion_revienta_el_aviso_no_se_cae(monkeypatch):
    personales: list[tuple] = []
    llamadas = _parchar(monkeypatch, proveedor=uuid4(), personales=personales)

    async def revienta(*_a, **_k):
        raise RuntimeError("BD caída")

    monkeypatch.setattr(eventos.asignacion_conv, "autoasignar", revienta)

    r = await eventos.conversacion_transferida(
        ConversacionTransferidaIn(tenant_id=uuid4(), conversation_id=uuid4(), canal="whatsapp")
    )

    # El negocio igual fue alertado, y el proveedor por cita recibe su copia de siempre.
    assert r.registrado is True and r.asignado_a is None
    assert len(llamadas) == 1 and len(personales) == 1
