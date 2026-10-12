"""
Re-agendar desde el agente: cancelar la cita actual y crear la nueva en una sola
transacción (services/calendario.py::reagendar_reserva) y la herramienta de
solo lectura que lista las citas próximas del cliente.

Con base real: lo que se prueba es la atomicidad (si el horario nuevo no cabe,
la cita anterior no se toca), la marca de cancelación tardía, la idempotencia
y que una cita ajena nunca se pueda mover.
"""

from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from services import calendario as cal
from services.calendario import CancelacionRechazada, actor_desde_chat
from services.pipeline import set_tenant_servicios
from session import execute, fetch_all, fetch_one, fetch_value

pytestmark = pytest.mark.usefixtures("plan_enterprise")

RUTA_REAGENDAR = "/api/eventos/calendario/reservas/reagendar"
RUTA_PROXIMAS = "/api/eventos/calendario/citas-proximas"

# America/Mexico_City es UTC-6 fijo (mismo criterio que test_calendario.py).
_CDMX = timezone(timedelta(hours=-6))


def _fecha() -> date:
    return date.today() + timedelta(days=14)


def _hora(hora: int, minuto: int = 0, dias: int = 0) -> datetime:
    return datetime.combine(_fecha() + timedelta(days=dias), time(hora, minuto), tzinfo=_CDMX).astimezone(
        timezone.utc
    )


def _internas(monkeypatch) -> dict:
    from config import settings

    monkeypatch.setattr(settings, "N8N_INTERNAL_TOKEN", "secreto-de-prueba-reagendar")
    return {"X-Internal-Token": "secreto-de-prueba-reagendar"}


async def _escenario(http_client, headers, tenant_id, antelacion=None):
    """Calendario encendido, un proveedor con jornada 09-18 y un servicio de 30 min."""
    await set_tenant_servicios(tenant_id, None, None, calendario_activo=True)
    r = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/proveedores", json={"nombre": "Juan"}, headers=headers
    )
    assert r.status_code == 201, r.text
    proveedor = r.json()
    r = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/servicios",
        json={"nombre": "Corte", "duracion_minutos": 30},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    servicio = r.json()
    r = await http_client.put(
        f"/api/tenants/{tenant_id}/calendario/proveedores/{proveedor['id']}/horarios",
        json={"bloques": [{"dia_semana": d, "hora_inicio": "09:00:00", "hora_fin": "18:00:00"} for d in range(7)]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    if antelacion is not None:
        await execute(
            "UPDATE servicios SET antelacion_cancelacion_horas = $1 WHERE id = $2",
            antelacion,
            UUID(servicio["id"]),
        )
    return UUID(proveedor["id"]), UUID(servicio["id"])


async def _cliente_y_conversacion(tenant_id, canal="whatsapp") -> tuple[UUID, UUID]:
    user_id = await fetch_value(
        "INSERT INTO users (tenant_id, display_name) VALUES ($1, 'Cliente') RETURNING id", tenant_id
    )
    conv_id = await fetch_value(
        "INSERT INTO conversations (tenant_id, user_id, channel_type) VALUES ($1, $2, $3) RETURNING id",
        tenant_id,
        user_id,
        canal,
    )
    return user_id, conv_id


async def _reserva(tenant_id, proveedor_id, servicio_id, inicio, *, user_id=None, estado="confirmada") -> UUID:
    return await fetch_value(
        """
        INSERT INTO reservas (tenant_id, proveedor_id, servicio_id, user_id, cliente_nombre,
                              hora_inicio, hora_fin, estado)
        VALUES ($1, $2, $3, $4, 'Cliente', $5, $6, $7) RETURNING id
        """,
        tenant_id,
        proveedor_id,
        servicio_id,
        user_id,
        inicio,
        inicio + timedelta(minutes=30),
        estado,
    )


async def _estado(reserva_id) -> dict:
    return dict(
        await fetch_one(
            "SELECT estado, hora_inicio, cancelada_por, cancelacion_tardia, user_id, servicio_id, proveedor_id "
            "FROM reservas WHERE id = $1",
            reserva_id,
        )
    )


async def _reagendar(tenant_id, conv_id, reserva_id, nueva, **kw):
    return await cal.reagendar_reserva(
        tenant_id, conv_id, reserva_id, nueva, actor=actor_desde_chat(None, None), **kw
    )


# ------------------------------------------------------------
# Servicio: reagendar_reserva
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_reagenda_cancela_la_anterior_y_crea_la_nueva_con_los_mismos_datos(
    http_client, tenant_y_usuario, headers_autenticado
):
    tid = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario(http_client, headers_autenticado, tid)
    user_id, conv = await _cliente_y_conversacion(tid)
    vieja = await _reserva(tid, proveedor, servicio, _hora(10), user_id=user_id)

    nueva, anterior, motivo = await _reagendar(tid, conv, vieja, _hora(12))

    assert motivo == "reagendada"
    assert anterior.id == vieja and anterior.estado == "cancelada"
    assert nueva.id != vieja and nueva.estado == "confirmada"
    assert nueva.hora_inicio == _hora(12)
    assert (nueva.proveedor_id, nueva.servicio_id, nueva.user_id) == (proveedor, servicio, user_id)
    fila_vieja = await _estado(vieja)
    assert fila_vieja["estado"] == "cancelada" and fila_vieja["cancelada_por"] == "cliente"
    assert fila_vieja["cancelacion_tardia"] is False

    # Una auditoría por cada reserva, enlazadas entre sí.
    auditoria = await fetch_all(
        "SELECT reserva_id, evento, datos_nuevos FROM reserva_auditoria WHERE reserva_id = ANY($1::uuid[])",
        [vieja, nueva.id],
    )
    assert {(a["reserva_id"], a["evento"]) for a in auditoria} >= {(vieja, "cancelada"), (nueva.id, "creada")}


@pytest.mark.asyncio
async def test_fuera_de_la_antelacion_se_acepta_pero_queda_como_cancelacion_tardia(
    http_client, tenant_y_usuario, headers_autenticado
):
    tid = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario(http_client, headers_autenticado, tid, antelacion=24)
    user_id, conv = await _cliente_y_conversacion(tid)
    inicio = _hora(10)
    vieja = await _reserva(tid, proveedor, servicio, inicio, user_id=user_id)

    # Faltan 2 horas para la cita y el servicio pide 24.
    nueva, anterior, motivo = await _reagendar(tid, conv, vieja, _hora(12), ahora=inicio - timedelta(hours=2))

    assert motivo == "reagendada" and nueva is not None
    assert anterior.cancelacion_tardia is True and anterior.antelacion_aplicada_horas == 24
    assert (await _estado(vieja))["cancelacion_tardia"] is True


@pytest.mark.asyncio
async def test_si_el_horario_nuevo_esta_ocupado_la_cita_anterior_no_se_toca(
    http_client, tenant_y_usuario, headers_autenticado
):
    tid = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario(http_client, headers_autenticado, tid)
    user_id, conv = await _cliente_y_conversacion(tid)
    vieja = await _reserva(tid, proveedor, servicio, _hora(10), user_id=user_id)
    await _reserva(tid, proveedor, servicio, _hora(12))  # otro cliente ya tiene las 12:00

    nueva, anterior, motivo = await _reagendar(tid, conv, vieja, _hora(12))

    assert (nueva, anterior, motivo) == (None, None, "horario_ocupado")
    fila = await _estado(vieja)
    assert fila["estado"] == "confirmada" and fila["cancelada_por"] is None
    assert await fetch_value("SELECT COUNT(*) FROM reserva_auditoria WHERE reserva_id = $1", vieja) == 0


@pytest.mark.asyncio
async def test_fuera_de_la_jornada_no_cancela_nada(http_client, tenant_y_usuario, headers_autenticado):
    tid = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario(http_client, headers_autenticado, tid)
    user_id, conv = await _cliente_y_conversacion(tid)
    vieja = await _reserva(tid, proveedor, servicio, _hora(10), user_id=user_id)

    nueva, anterior, motivo = await _reagendar(tid, conv, vieja, _hora(22))

    assert (nueva, anterior, motivo) == (None, None, "fuera_de_horario")
    assert (await _estado(vieja))["estado"] == "confirmada"


@pytest.mark.asyncio
async def test_puede_moverse_a_un_horario_que_solapa_con_la_propia_cita(
    http_client, tenant_y_usuario, headers_autenticado
):
    tid = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario(http_client, headers_autenticado, tid)
    user_id, conv = await _cliente_y_conversacion(tid)
    vieja = await _reserva(tid, proveedor, servicio, _hora(10), user_id=user_id)

    nueva, _anterior, motivo = await _reagendar(tid, conv, vieja, _hora(10, 15))

    assert motivo == "reagendada" and nueva.hora_inicio == _hora(10, 15)


@pytest.mark.asyncio
async def test_reintento_con_la_misma_clave_no_cancela_ni_crea_otra_vez(
    http_client, tenant_y_usuario, headers_autenticado
):
    tid = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario(http_client, headers_autenticado, tid)
    user_id, conv = await _cliente_y_conversacion(tid)
    vieja = await _reserva(tid, proveedor, servicio, _hora(10), user_id=user_id)
    clave = f"reagendar_reserva:{uuid4().hex[:16]}"

    primera, _a, m1 = await _reagendar(tid, conv, vieja, _hora(12), idempotency_key=clave)
    segunda, anterior, m2 = await _reagendar(tid, conv, vieja, _hora(12), idempotency_key=clave)

    assert (m1, m2) == ("reagendada", "duplicado")
    assert segunda.id == primera.id and anterior is None
    assert await fetch_value(
        "SELECT COUNT(*) FROM reservas WHERE tenant_id = $1 AND estado = 'confirmada'", tid
    ) == 1


@pytest.mark.asyncio
async def test_no_se_puede_mover_la_cita_de_otro_cliente(http_client, tenant_y_usuario, headers_autenticado):
    tid = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario(http_client, headers_autenticado, tid)
    _yo, mi_conv = await _cliente_y_conversacion(tid)
    otro, _otra_conv = await _cliente_y_conversacion(tid)
    ajena = await _reserva(tid, proveedor, servicio, _hora(10), user_id=otro)

    resultado = await _reagendar(tid, mi_conv, ajena, _hora(12))

    assert resultado == (None, None, "no_encontrada")
    assert (await _estado(ajena))["estado"] == "confirmada"


@pytest.mark.asyncio
async def test_conversacion_de_widget_o_ajena_no_puede_reagendar(
    http_client, tenant_y_usuario, headers_autenticado
):
    tid = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario(http_client, headers_autenticado, tid)
    user_id, conv_widget = await _cliente_y_conversacion(tid, canal="web")
    reserva = await _reserva(tid, proveedor, servicio, _hora(10), user_id=user_id)

    assert await _reagendar(tid, conv_widget, reserva, _hora(12)) == (None, None, "no_encontrada")
    assert await _reagendar(tid, uuid4(), reserva, _hora(12)) == (None, None, "no_encontrada")
    assert (await _estado(reserva))["estado"] == "confirmada"


@pytest.mark.asyncio
async def test_cita_no_confirmada_o_ya_iniciada_se_rechaza_como_en_cancelar(
    http_client, tenant_y_usuario, headers_autenticado
):
    tid = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario(http_client, headers_autenticado, tid)
    user_id, conv = await _cliente_y_conversacion(tid)
    inicio = _hora(10)
    en_curso = await _reserva(tid, proveedor, servicio, inicio, user_id=user_id)
    cancelada = await _reserva(tid, proveedor, servicio, _hora(14), user_id=user_id, estado="cancelada")

    with pytest.raises(CancelacionRechazada) as iniciada:
        await _reagendar(tid, conv, en_curso, _hora(12), ahora=inicio + timedelta(minutes=5))
    with pytest.raises(CancelacionRechazada) as cerrada:
        await _reagendar(tid, conv, cancelada, _hora(12))

    assert iniciada.value.codigo == "cita_ya_iniciada"
    assert cerrada.value.codigo == "reserva_no_confirmada"
    assert (await _estado(en_curso))["estado"] == "confirmada"


# ------------------------------------------------------------
# Servicio: citas_proximas_de_conversacion
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_citas_proximas_solo_futuras_confirmadas_y_del_cliente(
    http_client, tenant_y_usuario, headers_autenticado
):
    tid = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario(http_client, headers_autenticado, tid, antelacion=24)
    user_id, conv = await _cliente_y_conversacion(tid)
    otro, _c = await _cliente_y_conversacion(tid)
    ahora = _hora(8)
    lejana = await _reserva(tid, proveedor, servicio, _hora(11, dias=1), user_id=user_id)
    cercana = await _reserva(tid, proveedor, servicio, _hora(10), user_id=user_id)
    await _reserva(tid, proveedor, servicio, _hora(13), user_id=user_id, estado="cancelada")
    await _reserva(tid, proveedor, servicio, _hora(15), user_id=user_id, estado="completada")
    await _reserva(tid, proveedor, servicio, _hora(16), user_id=otro)
    await _reserva(tid, proveedor, servicio, ahora - timedelta(days=2), user_id=user_id)  # ya pasó

    r = await cal.citas_proximas_de_conversacion(tid, conv, ahora=ahora)

    assert [c["reserva_id"] for c in r["citas"]] == [cercana, lejana]  # la más cercana primero
    assert r["hay_mas"] is False
    primera = r["citas"][0]
    assert primera["servicio_id"] == servicio and primera["proveedor_id"] == proveedor
    assert primera["antelacion_minima_horas"] == 24
    assert primera["cancelacion_seria_tardia"] is True  # faltan 2 h y el servicio pide 24
    assert r["citas"][1]["cancelacion_seria_tardia"] is False  # falta más de un día


@pytest.mark.asyncio
async def test_citas_proximas_topa_a_diez_con_hay_mas(http_client, tenant_y_usuario, headers_autenticado):
    tid = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario(http_client, headers_autenticado, tid)
    user_id, conv = await _cliente_y_conversacion(tid)
    for i in range(12):
        await _reserva(tid, proveedor, servicio, _hora(9, dias=i + 1), user_id=user_id)

    r = await cal.citas_proximas_de_conversacion(tid, conv, ahora=_hora(8))

    assert len(r["citas"]) == 10 and r["hay_mas"] is True


@pytest.mark.asyncio
async def test_citas_proximas_de_conversacion_ajena_o_de_widget_es_none(
    http_client, tenant_y_usuario, headers_autenticado
):
    tid = tenant_y_usuario["tenant_id"]
    await _escenario(http_client, headers_autenticado, tid)
    _u, conv_widget = await _cliente_y_conversacion(tid, canal="web")

    assert await cal.citas_proximas_de_conversacion(tid, conv_widget, ahora=_hora(8)) is None
    assert await cal.citas_proximas_de_conversacion(tid, uuid4(), ahora=_hora(8)) is None


# ------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_endpoint_reagendar_de_punta_a_punta(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    tid = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario(http_client, headers_autenticado, tid)
    user_id, conv = await _cliente_y_conversacion(tid)
    vieja = await _reserva(tid, proveedor, servicio, _hora(10), user_id=user_id)

    r = await http_client.post(
        RUTA_REAGENDAR,
        json={
            "tenant_id": str(tid),
            "conversation_id": str(conv),
            "reserva_id": str(vieja),
            "hora_inicio": _hora(12).isoformat(),
            "idempotency_key": f"reagendar_reserva:{uuid4().hex[:16]}",
        },
        headers=_internas(monkeypatch),
    )

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["reagendada"] is True and cuerpo["duplicado"] is False
    assert cuerpo["reserva_anterior_id"] == str(vieja)
    assert cuerpo["reserva"]["id"] != str(vieja)
    assert (await _estado(vieja))["estado"] == "cancelada"


@pytest.mark.asyncio
async def test_endpoint_reagendar_ocupado_responde_200_con_motivo_y_no_cancela(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    tid = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario(http_client, headers_autenticado, tid)
    user_id, conv = await _cliente_y_conversacion(tid)
    vieja = await _reserva(tid, proveedor, servicio, _hora(10), user_id=user_id)
    await _reserva(tid, proveedor, servicio, _hora(12))

    r = await http_client.post(
        RUTA_REAGENDAR,
        json={"tenant_id": str(tid), "conversation_id": str(conv), "reserva_id": str(vieja),
              "hora_inicio": _hora(12).isoformat()},
        headers=_internas(monkeypatch),
    )

    assert r.status_code == 200, r.text
    assert r.json()["reagendada"] is False and r.json()["motivo_rechazo"] == "horario_ocupado"
    assert (await _estado(vieja))["estado"] == "confirmada"


@pytest.mark.asyncio
async def test_endpoint_reagendar_409_si_ya_empezo_y_404_si_es_ajena_o_el_id_es_invalido(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    tid = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario(http_client, headers_autenticado, tid)
    user_id, conv = await _cliente_y_conversacion(tid)
    otro, _c = await _cliente_y_conversacion(tid)
    iniciada = await _reserva(tid, proveedor, servicio, datetime.now(timezone.utc) - timedelta(minutes=10), user_id=user_id)
    ajena = await _reserva(tid, proveedor, servicio, _hora(10), user_id=otro)
    cabeceras = _internas(monkeypatch)

    def _cuerpo(reserva, conversation_id):
        return {"tenant_id": str(tid), "conversation_id": conversation_id, "reserva_id": str(reserva),
                "hora_inicio": _hora(12).isoformat()}

    r = await http_client.post(RUTA_REAGENDAR, json=_cuerpo(iniciada, str(conv)), headers=cabeceras)
    assert r.status_code == 409 and r.json()["detail"]["codigo"] == "cita_ya_iniciada"

    for conversation_id in (str(conv), "no-es-un-uuid", None, 123):
        r = await http_client.post(RUTA_REAGENDAR, json=_cuerpo(ajena, conversation_id), headers=cabeceras)
        assert r.status_code == 404, (conversation_id, r.text)
    assert (await _estado(ajena))["estado"] == "confirmada"


@pytest.mark.asyncio
async def test_endpoint_reagendar_exige_token_interno(http_client, tenant_y_usuario):
    r = await http_client.post(
        RUTA_REAGENDAR,
        json={"tenant_id": str(tenant_y_usuario["tenant_id"]), "conversation_id": str(uuid4()),
              "reserva_id": str(uuid4()), "hora_inicio": _hora(12).isoformat()},
    )
    assert r.status_code in (401, 403, 503)


@pytest.mark.asyncio
async def test_endpoint_citas_proximas_devuelve_reserva_id_y_404_uniforme(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    tid = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario(http_client, headers_autenticado, tid)
    user_id, conv = await _cliente_y_conversacion(tid)
    reserva = await _reserva(tid, proveedor, servicio, _hora(10), user_id=user_id)
    cabeceras = _internas(monkeypatch)

    r = await http_client.post(
        RUTA_PROXIMAS, json={"tenant_id": str(tid), "conversation_id": str(conv)}, headers=cabeceras
    )
    assert r.status_code == 200, r.text
    assert [c["reserva_id"] for c in r.json()["citas"]] == [str(reserva)]

    for conversation_id in (str(uuid4()), "basura", None):
        r = await http_client.post(
            RUTA_PROXIMAS, json={"tenant_id": str(tid), "conversation_id": conversation_id}, headers=cabeceras
        )
        assert r.status_code == 404, (conversation_id, r.text)
