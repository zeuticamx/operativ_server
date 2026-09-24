"""
Tests de integración HTTP/DB del módulo de calendarios.

El cálculo de slots en sí (horarios, excepciones, traslapes) ya está
cubierto puro y sin DB en test_calendario_slots.py. Acá se prueba lo que
solo se puede probar con una base real: el candado de Postgres contra
doble-reserva, la idempotencia de n8n, el gate del módulo (409/402) y la
disciplina 404-por-tenant del router del portal.
"""

from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from services.pipeline import set_tenant_servicios
from session import conexion, execute, fetch_one, fetch_value

RUTA_PROVEEDORES = "/api/tenants/{tenant_id}/calendario/proveedores"
RUTA_SERVICIOS = "/api/tenants/{tenant_id}/calendario/servicios"


async def _encender_calendario(tenant_id) -> None:
    await set_tenant_servicios(tenant_id, None, None, calendario_activo=True)


async def _crear_proveedor(http_client, headers, tenant_id, nombre="Juan") -> dict:
    r = await http_client.post(
        RUTA_PROVEEDORES.format(tenant_id=tenant_id), json={"nombre": nombre}, headers=headers
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _crear_servicio(http_client, headers, tenant_id, duracion=30) -> dict:
    r = await http_client.post(
        RUTA_SERVICIOS.format(tenant_id=tenant_id),
        json={"nombre": "Corte", "duracion_minutos": duracion},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _poner_horario(
    http_client, headers, tenant_id, proveedor_id, dia_semana, hora_inicio="09:00:00", hora_fin="18:00:00"
) -> dict:
    r = await http_client.put(
        f"/api/tenants/{tenant_id}/calendario/proveedores/{proveedor_id}/horarios",
        json={"bloques": [{"dia_semana": dia_semana, "hora_inicio": hora_inicio, "hora_fin": hora_fin}]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _poner_bloques(http_client, headers, tenant_id, proveedor_id, bloques: list[dict]) -> dict:
    r = await http_client.put(
        f"/api/tenants/{tenant_id}/calendario/proveedores/{proveedor_id}/horarios",
        json={"bloques": bloques},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _fecha_futura() -> date:
    """Una fecha bien alejada de 'hoy' para que ahora_utc nunca la excluya."""
    return date.today() + timedelta(days=14)


# America/Mexico_City es UTC-6 fijo desde que México abolió el horario de
# verano en 2022 (mismo criterio que test_calendario_slots.py) — permite
# escribir los horarios de estos tests en hora local, que es como los
# piensa la historia de usuario ("mi jornada es de 10 a 19").
_CDMX = timezone(timedelta(hours=-6))


def _hora_cdmx(fecha: date, hora: int, minuto: int = 0) -> datetime:
    return datetime.combine(fecha, time(hora, minuto), tzinfo=_CDMX).astimezone(timezone.utc)


def _cabeceras_internas(monkeypatch) -> dict:
    from config import settings

    monkeypatch.setattr(settings, "N8N_INTERNAL_TOKEN", "secreto-de-prueba-calendario")
    return {"X-Internal-Token": "secreto-de-prueba-calendario"}


# ------------------------------------------------------------
# Gate del módulo
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_modulo_apagado_bloquea_el_router_del_portal(http_client, tenant_y_usuario, headers_autenticado):
    tenant_id = tenant_y_usuario["tenant_id"]
    r = await http_client.get(RUTA_PROVEEDORES.format(tenant_id=tenant_id), headers=headers_autenticado)
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_modulo_apagado_bloquea_el_endpoint_de_n8n(http_client, tenant_y_usuario, monkeypatch):
    tenant_id = tenant_y_usuario["tenant_id"]
    headers = _cabeceras_internas(monkeypatch)
    r = await http_client.post(
        "/api/eventos/calendario/disponibilidad",
        json={
            "tenant_id": str(tenant_id),
            "servicio_id": str(uuid4()),
            "fecha_desde": str(date.today()),
            "fecha_hasta": str(date.today()),
        },
        headers=headers,
    )
    assert r.status_code == 409


# ------------------------------------------------------------
# Disponibilidad
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_disponibilidad_devuelve_los_slots_del_horario(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=30)

    fecha = _fecha_futura()
    await _poner_horario(http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday())

    headers = _cabeceras_internas(monkeypatch)
    r = await http_client.post(
        "/api/eventos/calendario/disponibilidad",
        json={
            "tenant_id": str(tenant_id),
            "servicio_id": servicio["id"],
            "fecha_desde": str(fecha),
            "fecha_hasta": str(fecha),
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["calendario_activo"] is True
    # 9-18 local en bloques de 30 min = 18 slots.
    assert len(body["slots"]) == 18
    assert all(s["proveedor_id"] == proveedor["id"] for s in body["slots"])


# ------------------------------------------------------------
# Crear reserva (n8n) — idempotencia y traslape
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_crear_reserva_por_n8n_es_idempotente(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=30)

    fecha = _fecha_futura()
    await _poner_horario(http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday())
    hora_inicio = datetime.combine(fecha, time(15, 0), tzinfo=timezone.utc)  # 9:00 CDMX
    clave = f"n8n-exec-{uuid4()}"
    cuerpo = {
        "tenant_id": str(tenant_id),
        "proveedor_id": proveedor["id"],
        "servicio_id": servicio["id"],
        "hora_inicio": hora_inicio.isoformat(),
        "cliente_nombre": "Cliente de prueba",
        "idempotency_key": clave,
    }
    headers = _cabeceras_internas(monkeypatch)

    primera = await http_client.post("/api/eventos/calendario/reservas", json=cuerpo, headers=headers)
    assert primera.status_code == 200, primera.text
    cuerpo_primera = primera.json()
    assert cuerpo_primera["creado"] is True
    assert cuerpo_primera["duplicado"] is False
    reserva_id = cuerpo_primera["reserva"]["id"]

    segunda = await http_client.post("/api/eventos/calendario/reservas", json=cuerpo, headers=headers)
    assert segunda.status_code == 200, segunda.text
    cuerpo_segunda = segunda.json()
    assert cuerpo_segunda["creado"] is True
    assert cuerpo_segunda["duplicado"] is True
    assert cuerpo_segunda["reserva"]["id"] == reserva_id

    total = await fetch_value("SELECT COUNT(*) FROM reservas WHERE idempotency_key = $1", clave)
    assert total == 1


@pytest.mark.asyncio
async def test_crear_reserva_en_horario_ocupado_es_rechazada(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=30)

    fecha = _fecha_futura()
    await _poner_horario(http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday())
    hora_inicio = datetime.combine(fecha, time(15, 0), tzinfo=timezone.utc)
    headers = _cabeceras_internas(monkeypatch)

    primera = await http_client.post(
        "/api/eventos/calendario/reservas",
        json={
            "tenant_id": str(tenant_id),
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": hora_inicio.isoformat(),
            "cliente_nombre": "Cliente uno",
            "idempotency_key": f"n8n-exec-{uuid4()}",
        },
        headers=headers,
    )
    assert primera.json()["creado"] is True

    # Mismo proveedor y horario, otro cliente y otra clave de idempotencia:
    # es un rechazo por traslape, no un duplicado.
    segunda = await http_client.post(
        "/api/eventos/calendario/reservas",
        json={
            "tenant_id": str(tenant_id),
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": hora_inicio.isoformat(),
            "cliente_nombre": "Cliente dos",
            "idempotency_key": f"n8n-exec-{uuid4()}",
        },
        headers=headers,
    )
    assert segunda.status_code == 200
    cuerpo_segunda = segunda.json()
    assert cuerpo_segunda["creado"] is False
    assert cuerpo_segunda["motivo_rechazo"] == "horario_ocupado"


@pytest.mark.asyncio
async def test_cancelar_libera_el_horario_para_volver_a_reservar(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=30)

    fecha = _fecha_futura()
    await _poner_horario(http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday())
    hora_inicio = datetime.combine(fecha, time(15, 0), tzinfo=timezone.utc)
    headers = _cabeceras_internas(monkeypatch)

    primera = await http_client.post(
        "/api/eventos/calendario/reservas",
        json={
            "tenant_id": str(tenant_id),
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": hora_inicio.isoformat(),
            "cliente_nombre": "Cliente uno",
            "idempotency_key": f"n8n-exec-{uuid4()}",
        },
        headers=headers,
    )
    reserva_id = primera.json()["reserva"]["id"]

    cancelada = await http_client.post(
        "/api/eventos/calendario/reservas/cancelar",
        json={"tenant_id": str(tenant_id), "reserva_id": reserva_id, "motivo": "el cliente avisó"},
        headers=headers,
    )
    assert cancelada.status_code == 200, cancelada.text
    assert cancelada.json()["estado"] == "cancelada"

    reintento = await http_client.post(
        "/api/eventos/calendario/reservas",
        json={
            "tenant_id": str(tenant_id),
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": hora_inicio.isoformat(),
            "cliente_nombre": "Cliente dos",
            "idempotency_key": f"n8n-exec-{uuid4()}",
        },
        headers=headers,
    )
    assert reintento.json()["creado"] is True


# ------------------------------------------------------------
# Jornada laboral del barbero (historia de usuario: bloquear reservas fuera
# de horario, respetar pausas, avisar de citas huérfanas)
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_escenario1_solo_hay_slots_dentro_de_la_jornada_configurada(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    """Jornada 10:00-19:00: ningún slot ofrecido cae antes de las 10 ni después de las 19."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=30)
    fecha = _fecha_futura()
    await _poner_horario(
        http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday(),
        hora_inicio="10:00:00", hora_fin="19:00:00",
    )

    headers = _cabeceras_internas(monkeypatch)
    r = await http_client.post(
        "/api/eventos/calendario/disponibilidad",
        json={
            "tenant_id": str(tenant_id),
            "servicio_id": servicio["id"],
            "fecha_desde": str(fecha),
            "fecha_hasta": str(fecha),
        },
        headers=headers,
    )
    slots = r.json()["slots"]
    assert len(slots) == 18  # 9 horas / 30 min

    def _parse(iso: str) -> datetime:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))

    assert _parse(slots[0]["hora_inicio"]) == _hora_cdmx(fecha, 10, 0)
    assert _parse(slots[-1]["hora_inicio"]) == _hora_cdmx(fecha, 18, 30)


@pytest.mark.asyncio
async def test_escenario2_reserva_que_excede_el_fin_de_turno_se_rechaza(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    """Jornada termina a las 19:00; un servicio de 45 min a las 18:30 terminaría a las 19:15."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=45)
    fecha = _fecha_futura()
    await _poner_horario(
        http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday(),
        hora_inicio="10:00:00", hora_fin="19:00:00",
    )
    hora_inicio = _hora_cdmx(fecha, 18, 30)

    # Vía n8n: no truena, responde con el rechazo estructurado.
    headers = _cabeceras_internas(monkeypatch)
    r = await http_client.post(
        "/api/eventos/calendario/reservas",
        json={
            "tenant_id": str(tenant_id),
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": hora_inicio.isoformat(),
            "cliente_nombre": "Cliente tarde",
            "idempotency_key": f"n8n-exec-{uuid4()}",
        },
        headers=headers,
    )
    cuerpo = r.json()
    assert cuerpo["creado"] is False
    assert cuerpo["motivo_rechazo"] == "fuera_de_horario"

    # Vía el portal: 422 con el mensaje exacto de la historia de usuario.
    manual = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas",
        json={
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": hora_inicio.isoformat(),
            "cliente_nombre": "Cliente tarde",
        },
        headers=headers_autenticado,
    )
    assert manual.status_code == 422
    assert manual.json()["detail"] == (
        "El horario seleccionado está fuera de la jornada de atención del barbero"
    )


@pytest.mark.asyncio
async def test_escenario3_solicitud_directa_fuera_de_jornada_se_rechaza(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    """Una llamada directa (simulando acceso a la API sin pasar por el flujo de disponibilidad)."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=30)
    fecha = _fecha_futura()
    await _poner_horario(
        http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday(),
        hora_inicio="10:00:00", hora_fin="19:00:00",
    )

    headers = _cabeceras_internas(monkeypatch)
    r = await http_client.post(
        "/api/eventos/calendario/reservas",
        json={
            "tenant_id": str(tenant_id),
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 8, 0).isoformat(),  # antes de que abra
            "cliente_nombre": "Madrugador",
            "idempotency_key": f"n8n-exec-{uuid4()}",
        },
        headers=headers,
    )
    assert r.status_code == 200  # el rechazo va en el cuerpo, no como error HTTP
    assert r.json() == {"creado": False, "duplicado": False, "reserva": None, "motivo_rechazo": "fuera_de_horario"}


@pytest.mark.asyncio
async def test_escenario4_la_pausa_de_almuerzo_bloquea_reservas(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    """Turno partido 10-14 y 15-19: la pausa de 14 a 15 no es horario laborable."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=30)
    fecha = _fecha_futura()
    await _poner_bloques(
        http_client, headers_autenticado, tenant_id, proveedor["id"],
        [
            {"dia_semana": fecha.weekday(), "hora_inicio": "10:00:00", "hora_fin": "14:00:00"},
            {"dia_semana": fecha.weekday(), "hora_inicio": "15:00:00", "hora_fin": "19:00:00"},
        ],
    )

    headers = _cabeceras_internas(monkeypatch)
    en_la_pausa = await http_client.post(
        "/api/eventos/calendario/reservas",
        json={
            "tenant_id": str(tenant_id),
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 14, 15).isoformat(),
            "cliente_nombre": "Cliente al mediodía",
            "idempotency_key": f"n8n-exec-{uuid4()}",
        },
        headers=headers,
    )
    assert en_la_pausa.json()["motivo_rechazo"] == "fuera_de_horario"

    # Control: la misma duración, ya en el segundo bloque, sí se acepta.
    despues_de_la_pausa = await http_client.post(
        "/api/eventos/calendario/reservas",
        json={
            "tenant_id": str(tenant_id),
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 15, 0).isoformat(),
            "cliente_nombre": "Cliente de la tarde",
            "idempotency_key": f"n8n-exec-{uuid4()}",
        },
        headers=headers,
    )
    assert despues_de_la_pausa.json()["creado"] is True


@pytest.mark.asyncio
async def test_reprogramar_fuera_de_jornada_tambien_se_rechaza(
    http_client, tenant_y_usuario, headers_autenticado
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=30)
    fecha = _fecha_futura()
    await _poner_horario(
        http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday(),
        hora_inicio="10:00:00", hora_fin="19:00:00",
    )

    creada = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas",
        json={
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 10, 0).isoformat(),
            "cliente_nombre": "Cliente",
        },
        headers=headers_autenticado,
    )
    reserva_id = creada.json()["id"]

    r = await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/reservas/{reserva_id}/reprogramar",
        json={"hora_inicio": _hora_cdmx(fecha, 20, 0).isoformat()},
        headers=headers_autenticado,
    )
    assert r.status_code == 422
    assert r.json()["detail"] == (
        "El horario seleccionado está fuera de la jornada de atención del barbero"
    )


@pytest.mark.asyncio
async def test_horario_mas_corto_avisa_de_citas_en_conflicto_sin_cancelarlas(
    http_client, tenant_y_usuario, headers_autenticado
):
    """
    Consideración técnica de la historia de usuario: si el barbero acorta su
    horario, las citas que ya no caben se reportan (`reservas_en_conflicto`)
    pero NO se cancelan solas -- gerencia decide qué hacer con cada una.
    """
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=30)
    fecha = _fecha_futura()
    # Horario amplio primero, para poder reservar a las 20:00.
    await _poner_horario(
        http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday(),
        hora_inicio="08:00:00", hora_fin="21:00:00",
    )
    creada = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas",
        json={
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 20, 0).isoformat(),
            "cliente_nombre": "Cliente nocturno",
        },
        headers=headers_autenticado,
    )
    reserva_id = creada.json()["id"]

    # Ahora se acorta el horario a 08:00-18:00: esa cita de las 20:00 ya no cabe.
    respuesta = await _poner_horario(
        http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday(),
        hora_inicio="08:00:00", hora_fin="18:00:00",
    )
    ids_en_conflicto = [r["id"] for r in respuesta["reservas_en_conflicto"]]
    assert reserva_id in ids_en_conflicto

    # La cita sigue viva y confirmada: acortar el horario no la tocó.
    detalle = await http_client.get(
        f"/api/tenants/{tenant_id}/calendario/reservas",
        params={
            "desde": _hora_cdmx(fecha, 0, 0).isoformat(),
            "hasta": _hora_cdmx(fecha, 23, 59).isoformat(),
        },
        headers=headers_autenticado,
    )
    assert detalle.status_code == 200, detalle.text
    reserva_actual = next(r for r in detalle.json() if r["id"] == reserva_id)
    assert reserva_actual["estado"] == "confirmada"


# ------------------------------------------------------------
# Descansos (pausas dentro de la jornada) — historia de usuario del barbero
# ------------------------------------------------------------
RUTA_DESCANSOS = "/api/tenants/{tenant_id}/calendario/proveedores/{proveedor_id}/descansos"


async def _crear_descanso(http_client, headers, tenant_id, proveedor_id, **kwargs):
    return await http_client.post(
        RUTA_DESCANSOS.format(tenant_id=tenant_id, proveedor_id=proveedor_id),
        json=kwargs,
        headers=headers,
    )


@pytest.mark.asyncio
async def test_descanso_escenario1_se_guarda_y_bloquea_la_franja(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    """Jornada 09-18 + descanso 13-14: la franja del descanso no se ofrece como slot."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=30)
    fecha = _fecha_futura()
    await _poner_horario(
        http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday(),
        hora_inicio="09:00:00", hora_fin="18:00:00",
    )

    r = await _crear_descanso(
        http_client, headers_autenticado, tenant_id, proveedor["id"],
        dia_semana=fecha.weekday(), hora_inicio="13:00:00", hora_fin="14:00:00",
    )
    assert r.status_code == 201, r.text

    leidos = await http_client.get(
        RUTA_DESCANSOS.format(tenant_id=tenant_id, proveedor_id=proveedor["id"]),
        headers=headers_autenticado,
    )
    assert len(leidos.json()) == 1

    headers = _cabeceras_internas(monkeypatch)
    disponibilidad = await http_client.post(
        "/api/eventos/calendario/disponibilidad",
        json={
            "tenant_id": str(tenant_id),
            "servicio_id": servicio["id"],
            "proveedor_id": proveedor["id"],
            "fecha_desde": fecha.isoformat(),
            "fecha_hasta": fecha.isoformat(),
        },
        headers=headers,
    )
    assert disponibilidad.status_code == 200, disponibilidad.text
    horas_locales = {
        datetime.fromisoformat(s["hora_inicio"]).astimezone(_CDMX).time()
        for s in disponibilidad.json()["slots"]
    }
    assert time(13, 0) not in horas_locales
    assert time(13, 30) not in horas_locales
    assert time(12, 30) in horas_locales  # justo antes del descanso, sí libre
    assert time(14, 0) in horas_locales  # justo después, sí libre


@pytest.mark.asyncio
async def test_descanso_fuera_de_jornada_se_rechaza(http_client, tenant_y_usuario, headers_autenticado):
    """Escenario 2: un descanso que empieza antes o termina después de la jornada se rechaza."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    fecha = _fecha_futura()
    await _poner_horario(
        http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday(),
        hora_inicio="09:00:00", hora_fin="18:00:00",
    )

    antes_de_abrir = await _crear_descanso(
        http_client, headers_autenticado, tenant_id, proveedor["id"],
        dia_semana=fecha.weekday(), hora_inicio="08:30:00", hora_fin="09:30:00",
    )
    assert antes_de_abrir.status_code == 422
    assert antes_de_abrir.json()["detail"] == "El descanso debe estar dentro de tu jornada laboral"

    despues_de_cerrar = await _crear_descanso(
        http_client, headers_autenticado, tenant_id, proveedor["id"],
        dia_semana=fecha.weekday(), hora_inicio="17:30:00", hora_fin="18:30:00",
    )
    assert despues_de_cerrar.status_code == 422
    assert despues_de_cerrar.json()["detail"] == "El descanso debe estar dentro de tu jornada laboral"

    sin_guardar = await http_client.get(
        RUTA_DESCANSOS.format(tenant_id=tenant_id, proveedor_id=proveedor["id"]),
        headers=headers_autenticado,
    )
    assert sin_guardar.json() == []


@pytest.mark.asyncio
async def test_descanso_impide_reserva_que_interfiere_por_duracion(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    """Escenario 4: descanso 14:00-15:00, servicio de 45 min a las 13:30 interfiere con el descanso."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=45)
    fecha = _fecha_futura()
    await _poner_horario(
        http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday(),
        hora_inicio="09:00:00", hora_fin="18:00:00",
    )
    r = await _crear_descanso(
        http_client, headers_autenticado, tenant_id, proveedor["id"],
        dia_semana=fecha.weekday(), hora_inicio="14:00:00", hora_fin="15:00:00",
    )
    assert r.status_code == 201, r.text

    headers = _cabeceras_internas(monkeypatch)
    intento = await http_client.post(
        "/api/eventos/calendario/reservas",
        json={
            "tenant_id": str(tenant_id),
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 13, 30).isoformat(),
            "cliente_nombre": "Cliente antes del descanso",
            "idempotency_key": f"n8n-exec-{uuid4()}",
        },
        headers=headers,
    )
    assert intento.json()["motivo_rechazo"] == "fuera_de_horario"


@pytest.mark.asyncio
async def test_descanso_con_citas_confirmadas_se_rechaza(
    http_client, tenant_y_usuario, headers_autenticado
):
    """
    Escenario 5: hay una cita confirmada a las 13:30; fijar un descanso de
    13:00 a 14:00 para esa misma fecha debe rechazarse (no guardarse) hasta
    que gerencia resuelva esa cita.
    """
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=30)
    fecha = _fecha_futura()
    await _poner_horario(
        http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday(),
        hora_inicio="09:00:00", hora_fin="18:00:00",
    )
    creada = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas",
        json={
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 13, 30).isoformat(),
            "cliente_nombre": "Cliente de las 13:30",
        },
        headers=headers_autenticado,
    )
    assert creada.status_code == 200, creada.text

    r = await _crear_descanso(
        http_client, headers_autenticado, tenant_id, proveedor["id"],
        dia_semana=fecha.weekday(), hora_inicio="13:00:00", hora_fin="14:00:00",
    )
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "Tienes citas confirmadas en ese horario. Cancela o reprograma la cita antes de fijar el descanso"
    )

    sin_guardar = await http_client.get(
        RUTA_DESCANSOS.format(tenant_id=tenant_id, proveedor_id=proveedor["id"]),
        headers=headers_autenticado,
    )
    assert sin_guardar.json() == []


@pytest.mark.asyncio
async def test_descanso_puntual_solo_aplica_esa_fecha(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    """Un descanso por `fecha` (no `dia_semana`) no afecta otros días con el mismo horario semanal."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=30)
    fecha = _fecha_futura()
    otra_fecha = fecha + timedelta(days=7)  # mismo día de la semana, sin el descanso puntual
    await _poner_horario(
        http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday(),
        hora_inicio="09:00:00", hora_fin="18:00:00",
    )
    r = await _crear_descanso(
        http_client, headers_autenticado, tenant_id, proveedor["id"],
        fecha=fecha.isoformat(), hora_inicio="13:00:00", hora_fin="14:00:00",
    )
    assert r.status_code == 201, r.text

    headers = _cabeceras_internas(monkeypatch)
    otra = await http_client.post(
        "/api/eventos/calendario/reservas",
        json={
            "tenant_id": str(tenant_id),
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(otra_fecha, 13, 0).isoformat(),
            "cliente_nombre": "Cliente de la otra semana",
            "idempotency_key": f"n8n-exec-{uuid4()}",
        },
        headers=headers,
    )
    assert otra.json()["creado"] is True


@pytest.mark.asyncio
async def test_eliminar_descanso(http_client, tenant_y_usuario, headers_autenticado):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    fecha = _fecha_futura()
    await _poner_horario(
        http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday(),
        hora_inicio="09:00:00", hora_fin="18:00:00",
    )
    creado = await _crear_descanso(
        http_client, headers_autenticado, tenant_id, proveedor["id"],
        dia_semana=fecha.weekday(), hora_inicio="13:00:00", hora_fin="14:00:00",
    )
    descanso_id = creado.json()["id"]

    r = await http_client.delete(
        f"/api/tenants/{tenant_id}/calendario/descansos/{descanso_id}", headers=headers_autenticado
    )
    assert r.status_code == 204

    otra_vez = await http_client.delete(
        f"/api/tenants/{tenant_id}/calendario/descansos/{descanso_id}", headers=headers_autenticado
    )
    assert otra_vez.status_code == 404


# ------------------------------------------------------------
# Bitácora de reservas — historia de usuario del dueño de la estética
# ------------------------------------------------------------
RUTA_AUDITORIA = "/api/tenants/{tenant_id}/calendario/auditoria"


@pytest.mark.asyncio
async def test_escenario1_crear_reserva_deja_un_registro_inmutable(
    http_client, tenant_y_usuario, headers_autenticado
):
    """Fecha/hora, ID de la cita, actor, estado anterior/nuevo y (para 'creada') sin motivo."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    fecha = _fecha_futura()
    await _poner_horario(
        http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday(),
    )
    creada = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas",
        json={
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 10, 0).isoformat(),
            "cliente_nombre": "Cliente bitácora",
        },
        headers=headers_autenticado,
    )
    assert creada.status_code == 200, creada.text
    reserva_id = creada.json()["id"]

    r = await http_client.get(
        RUTA_AUDITORIA.format(tenant_id=tenant_id), headers=headers_autenticado
    )
    assert r.status_code == 200, r.text
    eventos = r.json()
    assert len(eventos) == 1
    evento = eventos[0]
    assert evento["reserva_id"] == reserva_id
    assert evento["evento"] == "creada"
    assert evento["estado_anterior"] is None
    assert evento["estado_nuevo"] == "confirmada"
    assert evento["origen"] == "portal"
    assert evento["actor"] == tenant_y_usuario["email"]
    assert evento["datos_nuevos"]["proveedor_id"] == proveedor["id"]
    assert "creado_en" in evento


@pytest.mark.asyncio
async def test_bitacora_registra_reprogramar_cancelar_y_cambiar_estado(
    http_client, tenant_y_usuario, headers_autenticado
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    fecha = _fecha_futura()
    await _poner_horario(http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday())

    creada = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas",
        json={
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 10, 0).isoformat(),
            "cliente_nombre": "Cliente",
        },
        headers=headers_autenticado,
    )
    reserva_id = creada.json()["id"]

    reprogramada = await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/reservas/{reserva_id}/reprogramar",
        json={"hora_inicio": _hora_cdmx(fecha, 11, 0).isoformat()},
        headers=headers_autenticado,
    )
    assert reprogramada.status_code == 200, reprogramada.text

    estado = await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/reservas/{reserva_id}/estado",
        json={"estado": "no_asistio"},
        headers=headers_autenticado,
    )
    assert estado.status_code == 200, estado.text

    historial = await http_client.get(
        f"/api/tenants/{tenant_id}/calendario/reservas/{reserva_id}/auditoria",
        headers=headers_autenticado,
    )
    assert historial.status_code == 200, historial.text
    eventos = historial.json()
    # Escenario 4: ascendente, de la creación al estado actual.
    assert [e["evento"] for e in eventos] == ["creada", "reprogramada", "no_asistio"]
    assert eventos[0]["creado_en"] <= eventos[1]["creado_en"] <= eventos[2]["creado_en"]

    reprogramada_evento = eventos[1]
    assert reprogramada_evento["estado_anterior"] == reprogramada_evento["estado_nuevo"] == "confirmada"
    assert reprogramada_evento["datos_anteriores"]["hora_inicio"] != reprogramada_evento["datos_nuevos"]["hora_inicio"]

    estado_evento = eventos[2]
    assert estado_evento["estado_anterior"] == "confirmada"
    assert estado_evento["estado_nuevo"] == "no_asistio"

    # Cancelar una reserva distinta también deja su propio registro con el
    # estado anterior correcto.
    otra = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas",
        json={
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 14, 0).isoformat(),
            "cliente_nombre": "Cliente dos",
        },
        headers=headers_autenticado,
    )
    otra_id = otra.json()["id"]
    cancelada = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas/{otra_id}/cancelar",
        json={"motivo": "Se enfermó"},
        headers=headers_autenticado,
    )
    assert cancelada.status_code == 200, cancelada.text

    historial_otra = await http_client.get(
        f"/api/tenants/{tenant_id}/calendario/reservas/{otra_id}/auditoria",
        headers=headers_autenticado,
    )
    eventos_otra = historial_otra.json()
    assert eventos_otra[-1]["evento"] == "cancelada"
    assert eventos_otra[-1]["estado_anterior"] == "confirmada"
    assert eventos_otra[-1]["estado_nuevo"] == "cancelada"
    assert eventos_otra[-1]["motivo"] == "Se enfermó"


@pytest.mark.asyncio
async def test_reasignar_reserva_cambia_de_barbero_y_lo_audita(
    http_client, tenant_y_usuario, headers_autenticado
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    barbero_1 = await _crear_proveedor(http_client, headers_autenticado, tenant_id, nombre="Ana")
    barbero_2 = await _crear_proveedor(http_client, headers_autenticado, tenant_id, nombre="Luis")
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    fecha = _fecha_futura()
    await _poner_horario(http_client, headers_autenticado, tenant_id, barbero_1["id"], fecha.weekday())
    await _poner_horario(http_client, headers_autenticado, tenant_id, barbero_2["id"], fecha.weekday())

    creada = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas",
        json={
            "proveedor_id": barbero_1["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 10, 0).isoformat(),
            "cliente_nombre": "Cliente",
        },
        headers=headers_autenticado,
    )
    reserva_id = creada.json()["id"]

    r = await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/reservas/{reserva_id}/reasignar",
        json={"proveedor_id": barbero_2["id"]},
        headers=headers_autenticado,
    )
    assert r.status_code == 200, r.text
    assert r.json()["proveedor_id"] == barbero_2["id"]
    # La cita conserva su horario original: solo cambió el barbero.
    assert r.json()["hora_inicio"] == creada.json()["hora_inicio"]

    historial = await http_client.get(
        f"/api/tenants/{tenant_id}/calendario/reservas/{reserva_id}/auditoria",
        headers=headers_autenticado,
    )
    eventos = historial.json()
    assert eventos[-1]["evento"] == "cambio_barbero"
    assert eventos[-1]["datos_anteriores"]["proveedor_nombre"] == "Ana"
    assert eventos[-1]["datos_nuevos"]["proveedor_nombre"] == "Luis"


@pytest.mark.asyncio
async def test_reasignar_fuera_de_horario_del_nuevo_barbero_se_rechaza(
    http_client, tenant_y_usuario, headers_autenticado
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    barbero_1 = await _crear_proveedor(http_client, headers_autenticado, tenant_id, nombre="Ana")
    barbero_2 = await _crear_proveedor(http_client, headers_autenticado, tenant_id, nombre="Luis")
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    fecha = _fecha_futura()
    await _poner_horario(
        http_client, headers_autenticado, tenant_id, barbero_1["id"], fecha.weekday(),
        hora_inicio="09:00:00", hora_fin="18:00:00",
    )
    # Luis solo atiende por la mañana: la cita de las 16:00 no le cabe.
    await _poner_horario(
        http_client, headers_autenticado, tenant_id, barbero_2["id"], fecha.weekday(),
        hora_inicio="09:00:00", hora_fin="13:00:00",
    )
    creada = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas",
        json={
            "proveedor_id": barbero_1["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 16, 0).isoformat(),
            "cliente_nombre": "Cliente",
        },
        headers=headers_autenticado,
    )
    reserva_id = creada.json()["id"]

    r = await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/reservas/{reserva_id}/reasignar",
        json={"proveedor_id": barbero_2["id"]},
        headers=headers_autenticado,
    )
    assert r.status_code == 422
    assert r.json()["detail"] == (
        "El horario seleccionado está fuera de la jornada de atención del barbero"
    )

    # El rechazo no debe haber dejado un registro de 'cambio_barbero' a medias.
    historial = await http_client.get(
        f"/api/tenants/{tenant_id}/calendario/reservas/{reserva_id}/auditoria",
        headers=headers_autenticado,
    )
    assert [e["evento"] for e in historial.json()] == ["creada"]


@pytest.mark.asyncio
async def test_escenario3_filtrado_de_la_bitacora(
    http_client, tenant_y_usuario, headers_autenticado
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    ana = await _crear_proveedor(http_client, headers_autenticado, tenant_id, nombre="Ana")
    luis = await _crear_proveedor(http_client, headers_autenticado, tenant_id, nombre="Luis")
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    fecha = _fecha_futura()
    await _poner_horario(http_client, headers_autenticado, tenant_id, ana["id"], fecha.weekday())
    await _poner_horario(http_client, headers_autenticado, tenant_id, luis["id"], fecha.weekday())

    con_ana = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas",
        json={
            "proveedor_id": ana["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 10, 0).isoformat(),
            "cliente_nombre": "María",
        },
        headers=headers_autenticado,
    )
    con_luis = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas",
        json={
            "proveedor_id": luis["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 11, 0).isoformat(),
            "cliente_nombre": "Pedro",
        },
        headers=headers_autenticado,
    )
    assert con_ana.status_code == 200 and con_luis.status_code == 200

    # Filtro por barbero.
    solo_ana = await http_client.get(
        RUTA_AUDITORIA.format(tenant_id=tenant_id),
        params={"proveedor_id": ana["id"]},
        headers=headers_autenticado,
    )
    assert [e["reserva_id"] for e in solo_ana.json()] == [con_ana.json()["id"]]

    # Filtro por cliente (búsqueda parcial, insensible a mayúsculas).
    solo_pedro = await http_client.get(
        RUTA_AUDITORIA.format(tenant_id=tenant_id),
        params={"cliente": "pedro"},
        headers=headers_autenticado,
    )
    assert [e["reserva_id"] for e in solo_pedro.json()] == [con_luis.json()["id"]]

    # Filtro por ID de cita.
    solo_esa = await http_client.get(
        RUTA_AUDITORIA.format(tenant_id=tenant_id),
        params={"reserva_id": con_ana.json()["id"]},
        headers=headers_autenticado,
    )
    assert len(solo_esa.json()) == 1

    # Filtro por estatus resultante: cancelar la de Pedro y buscar 'cancelada'.
    await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas/{con_luis.json()['id']}/cancelar",
        json={},
        headers=headers_autenticado,
    )
    canceladas = await http_client.get(
        RUTA_AUDITORIA.format(tenant_id=tenant_id),
        params={"estado": "cancelada"},
        headers=headers_autenticado,
    )
    assert [e["reserva_id"] for e in canceladas.json()] == [con_luis.json()["id"]]

    # Filtro por rango de fechas: una ventana en el pasado no debe traer nada.
    vacio = await http_client.get(
        RUTA_AUDITORIA.format(tenant_id=tenant_id),
        params={
            "desde": "2000-01-01T00:00:00Z",
            "hasta": "2000-01-02T00:00:00Z",
        },
        headers=headers_autenticado,
    )
    assert vacio.json() == []


@pytest.mark.asyncio
async def test_escenario2_orden_cronologico_mas_reciente_primero(
    http_client, tenant_y_usuario, headers_autenticado
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    fecha = _fecha_futura()
    await _poner_horario(http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday())

    primera = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas",
        json={
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 10, 0).isoformat(),
            "cliente_nombre": "Primero",
        },
        headers=headers_autenticado,
    )
    segunda = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas",
        json={
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 11, 0).isoformat(),
            "cliente_nombre": "Segundo",
        },
        headers=headers_autenticado,
    )
    r = await http_client.get(RUTA_AUDITORIA.format(tenant_id=tenant_id), headers=headers_autenticado)
    ids_en_orden = [e["reserva_id"] for e in r.json()]
    assert ids_en_orden == [segunda.json()["id"], primera.json()["id"]]


@pytest.mark.asyncio
async def test_reserva_creada_por_n8n_audita_con_origen_n8n(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    fecha = _fecha_futura()
    await _poner_horario(http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday())

    headers = _cabeceras_internas(monkeypatch)
    r = await http_client.post(
        "/api/eventos/calendario/reservas",
        json={
            "tenant_id": str(tenant_id),
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 10, 0).isoformat(),
            "cliente_nombre": "Cliente de WhatsApp",
            "idempotency_key": f"n8n-exec-{uuid4()}",
        },
        headers=headers,
    )
    assert r.json()["creado"] is True

    bitacora = await http_client.get(
        RUTA_AUDITORIA.format(tenant_id=tenant_id), headers=headers_autenticado
    )
    evento = bitacora.json()[0]
    assert evento["origen"] == "n8n"
    assert evento["actor"] == "Cliente: Cliente de WhatsApp"


# ------------------------------------------------------------
# Cobro al completar + corte de caja diario
# ------------------------------------------------------------
RUTA_CORTE_DIARIO = "/api/tenants/{tenant_id}/calendario/corte-diario"


async def _crear_reserva_lista(http_client, headers, tenant_id, proveedor_id, servicio_id, hora_inicio, cliente="Cliente"):
    r = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas",
        json={
            "proveedor_id": proveedor_id,
            "servicio_id": servicio_id,
            "hora_inicio": hora_inicio.isoformat(),
            "cliente_nombre": cliente,
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_completar_reserva_requiere_precio_y_metodo_pago(
    http_client, tenant_y_usuario, headers_autenticado
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    fecha = _fecha_futura()
    await _poner_horario(http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday())
    reserva_id = await _crear_reserva_lista(
        http_client, headers_autenticado, tenant_id, proveedor["id"], servicio["id"],
        _hora_cdmx(fecha, 10, 0),
    )

    sin_precio = await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/reservas/{reserva_id}/estado",
        json={"estado": "completada"},
        headers=headers_autenticado,
    )
    assert sin_precio.status_code == 422

    sin_metodo = await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/reservas/{reserva_id}/estado",
        json={"estado": "completada", "precio_cobrado": "350.00"},
        headers=headers_autenticado,
    )
    assert sin_metodo.status_code == 422


@pytest.mark.asyncio
async def test_no_asistio_no_permite_precio_cobrado(
    http_client, tenant_y_usuario, headers_autenticado
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    fecha = _fecha_futura()
    await _poner_horario(http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday())
    reserva_id = await _crear_reserva_lista(
        http_client, headers_autenticado, tenant_id, proveedor["id"], servicio["id"],
        _hora_cdmx(fecha, 10, 0),
    )

    r = await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/reservas/{reserva_id}/estado",
        json={"estado": "no_asistio", "precio_cobrado": "100.00", "metodo_pago": "efectivo"},
        headers=headers_autenticado,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_completar_reserva_guarda_precio_y_metodo_pago(
    http_client, tenant_y_usuario, headers_autenticado
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    fecha = _fecha_futura()
    await _poner_horario(http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday())
    reserva_id = await _crear_reserva_lista(
        http_client, headers_autenticado, tenant_id, proveedor["id"], servicio["id"],
        _hora_cdmx(fecha, 10, 0),
    )

    r = await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/reservas/{reserva_id}/estado",
        json={"estado": "completada", "precio_cobrado": "350.00", "metodo_pago": "tarjeta"},
        headers=headers_autenticado,
    )
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["precio_cobrado"] == "350.00"
    assert cuerpo["metodo_pago"] == "tarjeta"


@pytest.mark.asyncio
async def test_corte_diario_escenario1_resumen_del_dia_con_lista_detallada(
    http_client, tenant_y_usuario, headers_autenticado
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id, nombre="Ana")
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    fecha = _fecha_futura()
    await _poner_horario(http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday())

    r1 = await _crear_reserva_lista(
        http_client, headers_autenticado, tenant_id, proveedor["id"], servicio["id"],
        _hora_cdmx(fecha, 10, 0), cliente="María",
    )
    r2 = await _crear_reserva_lista(
        http_client, headers_autenticado, tenant_id, proveedor["id"], servicio["id"],
        _hora_cdmx(fecha, 11, 0), cliente="Pedro",
    )
    for rid, precio, metodo in [(r1, "300.00", "efectivo"), (r2, "250.50", "tarjeta")]:
        await http_client.patch(
            f"/api/tenants/{tenant_id}/calendario/reservas/{rid}/estado",
            json={"estado": "completada", "precio_cobrado": precio, "metodo_pago": metodo},
            headers=headers_autenticado,
        )

    corte = await http_client.get(
        RUTA_CORTE_DIARIO.format(tenant_id=tenant_id),
        params={"fecha": fecha.isoformat()},
        headers=headers_autenticado,
    )
    assert corte.status_code == 200, corte.text
    cuerpo = corte.json()
    assert cuerpo["fecha"] == fecha.isoformat()
    assert cuerpo["total_servicios"] == 2
    assert cuerpo["total_cobrado"] == "550.50"
    assert len(cuerpo["servicios"]) == 2

    # Escenario 2: cada fila trae hora, cliente, servicio, barbero, método y precio.
    fila = cuerpo["servicios"][0]
    for campo in (
        "hora_inicio", "cliente_nombre", "servicio_nombre", "proveedor_nombre",
        "metodo_pago", "precio_cobrado",
    ):
        assert fila[campo] is not None, campo


@pytest.mark.asyncio
async def test_corte_diario_escenario4_excluye_estados_no_completados(
    http_client, tenant_y_usuario, headers_autenticado
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    fecha = _fecha_futura()
    await _poner_horario(http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday())

    confirmada = await _crear_reserva_lista(
        http_client, headers_autenticado, tenant_id, proveedor["id"], servicio["id"],
        _hora_cdmx(fecha, 9, 0), cliente="Pendiente",
    )
    cancelada = await _crear_reserva_lista(
        http_client, headers_autenticado, tenant_id, proveedor["id"], servicio["id"],
        _hora_cdmx(fecha, 10, 0), cliente="Canceló",
    )
    await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas/{cancelada}/cancelar",
        json={}, headers=headers_autenticado,
    )
    no_asistio = await _crear_reserva_lista(
        http_client, headers_autenticado, tenant_id, proveedor["id"], servicio["id"],
        _hora_cdmx(fecha, 11, 0), cliente="No llegó",
    )
    await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/reservas/{no_asistio}/estado",
        json={"estado": "no_asistio"}, headers=headers_autenticado,
    )
    completada = await _crear_reserva_lista(
        http_client, headers_autenticado, tenant_id, proveedor["id"], servicio["id"],
        _hora_cdmx(fecha, 12, 0), cliente="Sí pagó",
    )
    await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/reservas/{completada}/estado",
        json={"estado": "completada", "precio_cobrado": "400.00", "metodo_pago": "transferencia"},
        headers=headers_autenticado,
    )
    # `confirmada` se deja tal cual: nunca se completó, así que no debe
    # aparecer en el corte aunque siga viva en la agenda.

    corte = await http_client.get(
        RUTA_CORTE_DIARIO.format(tenant_id=tenant_id),
        params={"fecha": fecha.isoformat()},
        headers=headers_autenticado,
    )
    cuerpo = corte.json()
    assert cuerpo["total_servicios"] == 1
    assert cuerpo["total_cobrado"] == "400.00"
    assert [s["id"] for s in cuerpo["servicios"]] == [completada]


@pytest.mark.asyncio
async def test_corte_diario_escenario3_filtra_por_fecha_especifica(
    http_client, tenant_y_usuario, headers_autenticado
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    dia1 = _fecha_futura()
    dia2 = dia1 + timedelta(days=1)
    # Un solo PUT con los dos días: reemplazar_horarios sustituye TODA la
    # semana de una vez, así que dos llamadas seguidas (una por día)
    # borrarían la primera al aplicar la segunda.
    await _poner_bloques(
        http_client, headers_autenticado, tenant_id, proveedor["id"],
        [
            {"dia_semana": dia1.weekday(), "hora_inicio": "09:00:00", "hora_fin": "18:00:00"},
            {"dia_semana": dia2.weekday(), "hora_inicio": "09:00:00", "hora_fin": "18:00:00"},
        ],
    )

    r1 = await _crear_reserva_lista(
        http_client, headers_autenticado, tenant_id, proveedor["id"], servicio["id"],
        _hora_cdmx(dia1, 10, 0), cliente="Día uno",
    )
    r2 = await _crear_reserva_lista(
        http_client, headers_autenticado, tenant_id, proveedor["id"], servicio["id"],
        _hora_cdmx(dia2, 10, 0), cliente="Día dos",
    )
    for rid in (r1, r2):
        await http_client.patch(
            f"/api/tenants/{tenant_id}/calendario/reservas/{rid}/estado",
            json={"estado": "completada", "precio_cobrado": "200.00", "metodo_pago": "efectivo"},
            headers=headers_autenticado,
        )

    corte_dia1 = await http_client.get(
        RUTA_CORTE_DIARIO.format(tenant_id=tenant_id),
        params={"fecha": dia1.isoformat()},
        headers=headers_autenticado,
    )
    assert [s["id"] for s in corte_dia1.json()["servicios"]] == [r1]
    assert corte_dia1.json()["total_cobrado"] == "200.00"


@pytest.mark.asyncio
async def test_corte_diario_escenario5_filtra_por_barbero(
    http_client, tenant_y_usuario, headers_autenticado
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    ana = await _crear_proveedor(http_client, headers_autenticado, tenant_id, nombre="Ana")
    luis = await _crear_proveedor(http_client, headers_autenticado, tenant_id, nombre="Luis")
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    fecha = _fecha_futura()
    await _poner_horario(http_client, headers_autenticado, tenant_id, ana["id"], fecha.weekday())
    await _poner_horario(http_client, headers_autenticado, tenant_id, luis["id"], fecha.weekday())

    con_ana = await _crear_reserva_lista(
        http_client, headers_autenticado, tenant_id, ana["id"], servicio["id"],
        _hora_cdmx(fecha, 10, 0), cliente="De Ana",
    )
    con_luis = await _crear_reserva_lista(
        http_client, headers_autenticado, tenant_id, luis["id"], servicio["id"],
        _hora_cdmx(fecha, 11, 0), cliente="De Luis",
    )
    for rid, precio in [(con_ana, "150.00"), (con_luis, "999.00")]:
        await http_client.patch(
            f"/api/tenants/{tenant_id}/calendario/reservas/{rid}/estado",
            json={"estado": "completada", "precio_cobrado": precio, "metodo_pago": "efectivo"},
            headers=headers_autenticado,
        )

    corte_ana = await http_client.get(
        RUTA_CORTE_DIARIO.format(tenant_id=tenant_id),
        params={"fecha": fecha.isoformat(), "proveedor_id": ana["id"]},
        headers=headers_autenticado,
    )
    cuerpo = corte_ana.json()
    assert cuerpo["total_servicios"] == 1
    assert cuerpo["total_cobrado"] == "150.00"
    assert cuerpo["servicios"][0]["id"] == con_ana


@pytest.mark.asyncio
async def test_corte_diario_escenario6_dia_sin_servicios(
    http_client, tenant_y_usuario, headers_autenticado
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    fecha = _fecha_futura()

    corte = await http_client.get(
        RUTA_CORTE_DIARIO.format(tenant_id=tenant_id),
        params={"fecha": fecha.isoformat()},
        headers=headers_autenticado,
    )
    assert corte.status_code == 200, corte.text
    cuerpo = corte.json()
    assert cuerpo["total_servicios"] == 0
    assert cuerpo["total_cobrado"] == "0.00" or cuerpo["total_cobrado"] == "0"
    assert cuerpo["servicios"] == []


@pytest.mark.asyncio
async def test_corte_diario_sin_fecha_usa_el_dia_de_hoy(
    http_client, tenant_y_usuario, headers_autenticado
):
    """Escenario 1: por defecto, la fecha del día en curso."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    await _crear_proveedor(http_client, headers_autenticado, tenant_id)

    corte = await http_client.get(
        RUTA_CORTE_DIARIO.format(tenant_id=tenant_id), headers=headers_autenticado
    )
    assert corte.status_code == 200, corte.text
    assert corte.json()["fecha"] == date.today().isoformat()


# ------------------------------------------------------------
# 404 en vez de 403 en el router del portal
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_proveedor_de_otro_tenant_da_404(http_client, tenant_y_usuario, headers_autenticado):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)

    otro_tenant_id = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, $2)", otro_tenant_id, "Otro negocio")
    try:
        await _encender_calendario(otro_tenant_id)
        ajeno = await fetch_value(
            """
            INSERT INTO proveedores (tenant_id, nombre) VALUES ($1, 'Ajeno')
            RETURNING id
            """,
            otro_tenant_id,
        )

        r = await http_client.patch(
            f"/api/tenants/{tenant_id}/calendario/proveedores/{ajeno}",
            json={"activo": False},
            headers=headers_autenticado,
        )
        assert r.status_code == 404
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", otro_tenant_id)


# ------------------------------------------------------------
# El candado de Postgres, directo (sin pasar por services.calendario)
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_exclude_de_postgres_rechaza_el_traslape_directo(
    http_client, tenant_y_usuario, headers_autenticado
):
    """
    Prueba el candado en sí, no el manejo en Python: aunque alguien escriba
    directo por SQL (un script, otra app) sin pasar por crear_reserva, dos
    reservas confirmadas del mismo proveedor que se traslapan no pueden
    coexistir.
    """
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=30)

    fecha = _fecha_futura()
    inicio = datetime.combine(fecha, time(15, 0), tzinfo=timezone.utc)
    fin = inicio + timedelta(minutes=30)

    async with conexion() as conn:
        await conn.execute(
            """
            INSERT INTO reservas (tenant_id, proveedor_id, servicio_id, cliente_nombre, hora_inicio, hora_fin)
            VALUES ($1, $2, $3, 'Uno', $4, $5)
            """,
            tenant_id,
            proveedor["id"],
            servicio["id"],
            inicio,
            fin,
        )
        with pytest.raises(asyncpg.exceptions.ExclusionViolationError):
            await conn.execute(
                """
                INSERT INTO reservas (tenant_id, proveedor_id, servicio_id, cliente_nombre, hora_inicio, hora_fin)
                VALUES ($1, $2, $3, 'Dos', $4, $5)
                """,
                tenant_id,
                proveedor["id"],
                servicio["id"],
                inicio + timedelta(minutes=15),
                fin + timedelta(minutes=15),
            )


@pytest.mark.asyncio
async def test_reserva_manual_desde_portal_no_genera_alerta_reserva_creada(
    http_client, tenant_y_usuario, headers_autenticado
):
    """
    Historia de notificaciones en tiempo real, escenario 5: si gerencia
    agenda un walk-in desde el propio portal, no debe avisarse a sí misma
    de una reserva que ella misma acaba de crear. La alerta "reserva_creada"
    es solo para las que llegan solas por el chat (ver el siguiente test).
    """
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    fecha = _fecha_futura()
    await _poner_horario(http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday())

    r = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas",
        json={
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 12, 0).isoformat(),
            "cliente_nombre": "Walk-in mostrador",
        },
        headers=headers_autenticado,
    )
    assert r.status_code == 200, r.text

    total = await fetch_value(
        "SELECT COUNT(*) FROM alertas WHERE tenant_id = $1 AND tipo = 'reserva_creada'",
        tenant_id,
    )
    assert total == 0


@pytest.mark.asyncio
async def test_reserva_creada_por_n8n_genera_alerta_con_detalle(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    """
    Escenarios 1 y 2 de la historia: una reserva que llega por el chat sí
    debe avisar a gerencia, con teléfono, horario y precio en el mensaje
    (además de proveedor_id/hora_inicio en `datos`, que el frontend usa
    para centrar el calendario en la cita -- escenario 3).
    """
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)

    servicio_r = await http_client.post(
        RUTA_SERVICIOS.format(tenant_id=tenant_id),
        json={"nombre": "Corte", "duracion_minutos": 30, "precio": "150.00"},
        headers=headers_autenticado,
    )
    assert servicio_r.status_code == 201, servicio_r.text
    servicio = servicio_r.json()

    fecha = _fecha_futura()
    await _poner_horario(http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday())
    hora_inicio = _hora_cdmx(fecha, 12, 0)

    headers = _cabeceras_internas(monkeypatch)
    r = await http_client.post(
        "/api/eventos/calendario/reservas",
        json={
            "tenant_id": str(tenant_id),
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": hora_inicio.isoformat(),
            "cliente_nombre": "Cliente chat",
            "cliente_telefono": "555-0000",
            "idempotency_key": f"n8n-exec-{uuid4()}",
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["creado"] is True

    alerta = await fetch_one(
        "SELECT titulo, mensaje, datos FROM alertas WHERE tenant_id = $1 AND tipo = 'reserva_creada'",
        tenant_id,
    )
    assert alerta is not None
    assert "Cliente chat" in alerta["mensaje"]
    assert "555-0000" in alerta["mensaje"]
    assert "150.00" in alerta["mensaje"]
    assert alerta["datos"]["proveedor_id"] == proveedor["id"]
    assert alerta["datos"]["reserva_id"] == r.json()["reserva"]["id"]


@pytest.mark.asyncio
async def test_editar_servicio_actualiza_datos_y_registra_auditoria(
    http_client, tenant_y_usuario, headers_autenticado
):
    """
    Historia de edición del catálogo, escenarios 1 y 2: el PATCH refleja los
    nuevos valores de inmediato, y queda una fila en servicio_auditoria con
    el antes/después de cada campo que sí cambió (consideración técnica de
    la historia: quién, cuándo, qué campos).
    """
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=30)

    r = await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/servicios/{servicio['id']}",
        json={"nombre": "Corte premium", "duracion_minutos": 45, "precio": "250.00"},
        headers=headers_autenticado,
    )
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["nombre"] == "Corte premium"
    assert cuerpo["duracion_minutos"] == 45
    assert cuerpo["precio"] == "250.00"

    fila = await fetch_one(
        """
        SELECT datos_anteriores, datos_nuevos, actor_email
        FROM servicio_auditoria WHERE servicio_id = $1
        """,
        servicio["id"],
    )
    assert fila is not None
    assert fila["actor_email"] == tenant_y_usuario["email"]
    assert fila["datos_anteriores"] == {
        "nombre": "Corte",
        "duracion_minutos": 30,
        "precio": None,
    }
    assert fila["datos_nuevos"] == {
        "nombre": "Corte premium",
        "duracion_minutos": 45,
        "precio": "250.00",
    }


@pytest.mark.asyncio
async def test_editar_servicio_sin_cambios_reales_no_registra_auditoria(
    http_client, tenant_y_usuario, headers_autenticado
):
    """Un PATCH que solo toca `activo` (o reenvía el mismo nombre) no debe ensuciar la bitácora."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=30)

    r = await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/servicios/{servicio['id']}",
        json={"nombre": "Corte", "activo": False},
        headers=headers_autenticado,
    )
    assert r.status_code == 200, r.text

    total = await fetch_value(
        "SELECT COUNT(*) FROM servicio_auditoria WHERE servicio_id = $1", servicio["id"]
    )
    assert total == 0


@pytest.mark.asyncio
async def test_editar_servicio_rechaza_precio_negativo_y_duracion_cero(
    http_client, tenant_y_usuario, headers_autenticado
):
    """Escenario 5: precio negativo o duración de 0 minutos no deben poder guardarse."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=30)

    negativo = await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/servicios/{servicio['id']}",
        json={"precio": "-10.00"},
        headers=headers_autenticado,
    )
    assert negativo.status_code == 422

    sin_duracion = await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/servicios/{servicio['id']}",
        json={"duracion_minutos": 0},
        headers=headers_autenticado,
    )
    assert sin_duracion.status_code == 422

    vacio = await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/servicios/{servicio['id']}",
        json={"nombre": ""},
        headers=headers_autenticado,
    )
    assert vacio.status_code == 422

    # Nada de esto tocó el servicio ni generó ruido en la bitácora.
    actual = await fetch_one(
        "SELECT nombre, duracion_minutos, precio FROM servicios WHERE id = $1", servicio["id"]
    )
    assert actual["nombre"] == "Corte"
    assert actual["duracion_minutos"] == 30
    total = await fetch_value(
        "SELECT COUNT(*) FROM servicio_auditoria WHERE servicio_id = $1", servicio["id"]
    )
    assert total == 0


@pytest.mark.asyncio
async def test_editar_servicio_no_afecta_reservas_ya_agendadas(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    """
    Escenarios 3 y 4: cambiar duración/precio del catálogo no debe mover el
    horario de una cita ya agendada (hora_fin quedó fija al crearla) ni su
    precio ya cobrado (precio_cobrado es un snapshot aparte, ver
    19_calendario_cobros.sql) -- solo las citas nuevas ven los valores
    actualizados.
    """
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers_autenticado, tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=30)
    fecha = _fecha_futura()
    await _poner_horario(http_client, headers_autenticado, tenant_id, proveedor["id"], fecha.weekday())

    creada = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas",
        json={
            "proveedor_id": proveedor["id"],
            "servicio_id": servicio["id"],
            "hora_inicio": _hora_cdmx(fecha, 10, 0).isoformat(),
            "cliente_nombre": "Cliente antiguo",
        },
        headers=headers_autenticado,
    )
    assert creada.status_code == 200, creada.text
    reserva = creada.json()
    assert reserva["hora_fin"] == _hora_cdmx(fecha, 10, 30).isoformat().replace("+00:00", "Z")

    # Se completa con el precio vigente ANTES de la edición del catálogo.
    completada = await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/reservas/{reserva['id']}/estado",
        json={"estado": "completada", "precio_cobrado": "200.00", "metodo_pago": "efectivo"},
        headers=headers_autenticado,
    )
    assert completada.status_code == 200, completada.text

    # Ahora se edita el catálogo: la duración sube y el precio también.
    r = await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/servicios/{servicio['id']}",
        json={"duracion_minutos": 45, "precio": "250.00"},
        headers=headers_autenticado,
    )
    assert r.status_code == 200, r.text

    # La reserva ya cerrada no se mueve.
    intacta = await fetch_one(
        "SELECT hora_inicio, hora_fin, precio_cobrado FROM reservas WHERE id = $1", reserva["id"]
    )
    assert intacta["hora_fin"] == _hora_cdmx(fecha, 10, 30)
    assert str(intacta["precio_cobrado"]) == "200.00"
