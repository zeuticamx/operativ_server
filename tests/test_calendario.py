"""
Tests de integración HTTP/DB del módulo de calendarios.

El cálculo de slots en sí (horarios, excepciones, traslapes) ya está
cubierto puro y sin DB en test_calendario_slots.py. Acá se prueba lo que
solo se puede probar con una base real: el candado de Postgres contra
doble-reserva, la idempotencia de n8n, el gate del módulo (409/402) y la
disciplina 404-por-tenant del router del portal.
"""

import json
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest

from services.pipeline import set_tenant_servicios
from session import conexion, execute, fetch_all, fetch_one, fetch_value

# Prueban la lógica del módulo, no el cobro: el tenant de prueba tiene plan
# vigente con todo incluido (ver el fixture en conftest.py).
pytestmark = pytest.mark.usefixtures("plan_enterprise")

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


@pytest.mark.asyncio
async def test_disponibilidad_rechaza_rango_en_el_pasado(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id, duracion=30)

    headers = _cabeceras_internas(monkeypatch)
    ayer = date.today() - timedelta(days=2)
    r = await http_client.post(
        "/api/eventos/calendario/disponibilidad",
        json={
            "tenant_id": str(tenant_id),
            "servicio_id": servicio["id"],
            "fecha_desde": str(ayer),
            "fecha_hasta": str(ayer),
        },
        headers=headers,
    )
    assert r.status_code == 422
    assert "Hoy es" in r.json()["detail"]


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
        "El horario seleccionado está fuera de la jornada de atención del proveedor"
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
        "El horario seleccionado está fuera de la jornada de atención del proveedor"
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
        "El horario seleccionado está fuera de la jornada de atención del proveedor"
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

# ------------------------------------------------------------
# Regla de cancelación por servicio (specs/001-regla-cancelacion-citas, T3)
# ------------------------------------------------------------
CAMPO_ANTELACION = "antelacion_cancelacion_horas"


async def _patch_servicio(http_client, headers, tenant_id, servicio_id, cuerpo):
    return await http_client.patch(
        f"/api/tenants/{tenant_id}/calendario/servicios/{servicio_id}", json=cuerpo, headers=headers
    )


async def _antelacion_en_bd(servicio_id):
    return await fetch_value(
        "SELECT antelacion_cancelacion_horas FROM servicios WHERE id = $1", servicio_id
    )


async def _cuenta_de_rol(tenant_id, rol: str) -> dict:
    """Un portal_user del rol dado en `tenant_id`, con su token."""
    from security import crear_access_token, hash_password

    uid = uuid4()
    email = f"{rol}-{uid}@ejemplo.com"
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, $5, true)
        """,
        uid,
        tenant_id,
        email,
        hash_password("test_password"),
        rol,
    )
    return {
        "id": uid,
        "email": email,
        "headers": {"Authorization": f"Bearer {crear_access_token(uid, tenant_id, rol)}"},
    }


@pytest.fixture
async def otro_tenant(db):
    """Un segundo negocio con su dueño; se borra al terminar (CASCADE)."""
    tenant_id = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro negocio')", tenant_id)
    dueno = await _cuenta_de_rol(tenant_id, "owner")
    yield {"tenant_id": tenant_id, "dueno": dueno}
    await execute("DELETE FROM tenants WHERE id = $1", tenant_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("valor", [0, 24, 720, None])
async def test_antelacion_acepta_0_720_y_null(http_client, tenant_y_usuario, headers_autenticado, valor):
    """R1, R4: enteros 0-720 o null (sin regla) se guardan y se devuelven."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    if valor is None:
        # Parte de una regla para que el borrado sea observable.
        previo = await _patch_servicio(
            http_client, headers_autenticado, tenant_id, servicio["id"], {CAMPO_ANTELACION: 5}
        )
        assert previo.status_code == 200, previo.text

    r = await _patch_servicio(
        http_client, headers_autenticado, tenant_id, servicio["id"], {CAMPO_ANTELACION: valor}
    )

    assert r.status_code == 200, r.text
    assert r.json()[CAMPO_ANTELACION] == valor
    assert await _antelacion_en_bd(servicio["id"]) == valor


@pytest.mark.asyncio
@pytest.mark.parametrize("invalido", [721, -1, 1.5, 24.0, "24", True, False, [24], {"h": 24}])
async def test_antelacion_rechaza_valores_que_no_son_entero_estricto_0_720(
    http_client, tenant_y_usuario, headers_autenticado, invalido
):
    """R3: 422 y el valor guardado no cambia."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    previo = await _patch_servicio(
        http_client, headers_autenticado, tenant_id, servicio["id"], {CAMPO_ANTELACION: 12}
    )
    assert previo.status_code == 200, previo.text

    r = await _patch_servicio(
        http_client, headers_autenticado, tenant_id, servicio["id"], {CAMPO_ANTELACION: invalido}
    )

    assert r.status_code == 422, r.text
    assert await _antelacion_en_bd(servicio["id"]) == 12


@pytest.mark.asyncio
async def test_antelacion_omitida_no_cambia_y_null_explicito_borra(
    http_client, tenant_y_usuario, headers_autenticado
):
    """R4: omitir el campo conserva la regla; solo `null` explícito la borra."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    previo = await _patch_servicio(
        http_client, headers_autenticado, tenant_id, servicio["id"], {CAMPO_ANTELACION: 48}
    )
    assert previo.status_code == 200, previo.text

    # Otros campos sin mencionar la antelación: sigue en 48.
    r = await _patch_servicio(
        http_client, headers_autenticado, tenant_id, servicio["id"], {"nombre": "Corte nuevo", "activo": False}
    )
    assert r.status_code == 200, r.text
    assert r.json()[CAMPO_ANTELACION] == 48
    assert await _antelacion_en_bd(servicio["id"]) == 48

    # Cuerpo vacío o con un null en otro campo: sigue siendo 422 (nada que actualizar).
    vacio = await _patch_servicio(http_client, headers_autenticado, tenant_id, servicio["id"], {})
    assert vacio.status_code == 422
    nulo_ajeno = await _patch_servicio(
        http_client, headers_autenticado, tenant_id, servicio["id"], {"nombre": None}
    )
    assert nulo_ajeno.status_code == 422
    assert await _antelacion_en_bd(servicio["id"]) == 48

    # null explícito: borra, y es un cuerpo válido por sí solo.
    borrada = await _patch_servicio(
        http_client, headers_autenticado, tenant_id, servicio["id"], {CAMPO_ANTELACION: None}
    )
    assert borrada.status_code == 200, borrada.text
    assert borrada.json()[CAMPO_ANTELACION] is None
    assert await _antelacion_en_bd(servicio["id"]) is None
    # y lo demás quedó como estaba
    assert borrada.json()["nombre"] == "Corte nuevo"
    assert borrada.json()["activo"] is False


@pytest.mark.asyncio
async def test_servicios_nuevos_y_existentes_nacen_sin_regla(
    http_client, tenant_y_usuario, headers_autenticado
):
    """R8: sin indicar nada el servicio nace en null; el alta también admite valor."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    nuevo = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    assert nuevo[CAMPO_ANTELACION] is None

    # Servicio "existente": insertado sin tocar la columna (como antes de la migración).
    existente = await fetch_value(
        "INSERT INTO servicios (tenant_id, nombre, duracion_minutos) VALUES ($1, 'Previo', 30) RETURNING id",
        tenant_id,
    )
    r = await http_client.get(RUTA_SERVICIOS.format(tenant_id=tenant_id), headers=headers_autenticado)
    assert r.status_code == 200, r.text
    por_id = {s["id"]: s for s in r.json()}
    assert por_id[nuevo["id"]][CAMPO_ANTELACION] is None
    assert por_id[str(existente)][CAMPO_ANTELACION] is None

    con_regla = await http_client.post(
        RUTA_SERVICIOS.format(tenant_id=tenant_id),
        json={"nombre": "Con regla", "duracion_minutos": 30, CAMPO_ANTELACION: 24},
        headers=headers_autenticado,
    )
    assert con_regla.status_code == 201, con_regla.text
    assert con_regla.json()[CAMPO_ANTELACION] == 24
    invalida = await http_client.post(
        RUTA_SERVICIOS.format(tenant_id=tenant_id),
        json={"nombre": "Mala", "duracion_minutos": 30, CAMPO_ANTELACION: 721},
        headers=headers_autenticado,
    )
    assert invalida.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("rol", ["member", "vendedor", "proveedor"])
async def test_antelacion_solo_la_cambia_la_gerencia(
    http_client, tenant_y_usuario, headers_autenticado, rol
):
    """R2: member, vendedor y proveedor reciben 403 y nada cambia."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    cuenta = await _cuenta_de_rol(tenant_id, rol)

    r = await _patch_servicio(
        http_client, cuenta["headers"], tenant_id, servicio["id"], {CAMPO_ANTELACION: 10}
    )

    assert r.status_code == 403, r.text
    assert await _antelacion_en_bd(servicio["id"]) is None


@pytest.mark.asyncio
async def test_antelacion_servicio_de_otro_negocio_da_404_y_member_ajeno_403(
    http_client, tenant_y_usuario, headers_autenticado, otro_tenant
):
    """R5: owner ajeno -> 404 (no confirma que exista); member ajeno -> 403 (el rol va primero)."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)

    ajeno = await _patch_servicio(
        http_client, otro_tenant["dueno"]["headers"], tenant_id, servicio["id"], {CAMPO_ANTELACION: 10}
    )
    assert ajeno.status_code == 404, ajeno.text

    member_ajeno = await _cuenta_de_rol(otro_tenant["tenant_id"], "member")
    r = await _patch_servicio(
        http_client, member_ajeno["headers"], tenant_id, servicio["id"], {CAMPO_ANTELACION: 10}
    )
    assert r.status_code == 403, r.text

    # El dueño legítimo con un servicio_id inexistente: también 404.
    inexistente = await _patch_servicio(
        http_client, headers_autenticado, tenant_id, uuid4(), {CAMPO_ANTELACION: 10}
    )
    assert inexistente.status_code == 404
    assert await _antelacion_en_bd(servicio["id"]) is None


@pytest.mark.asyncio
async def test_antelacion_sin_token_da_401(http_client, tenant_y_usuario):
    r = await _patch_servicio(http_client, {}, tenant_y_usuario["tenant_id"], uuid4(), {CAMPO_ANTELACION: 10})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_antelacion_con_calendario_apagado_da_409(http_client, tenant_y_usuario, headers_autenticado):
    """R7: módulo apagado -> 409, tanto al editar como al dar de alta."""
    tenant_id = tenant_y_usuario["tenant_id"]
    r = await _patch_servicio(
        http_client, headers_autenticado, tenant_id, uuid4(), {CAMPO_ANTELACION: 10}
    )
    assert r.status_code == 409, r.text
    alta = await http_client.post(
        RUTA_SERVICIOS.format(tenant_id=tenant_id),
        json={"nombre": "Corte", "duracion_minutos": 30, CAMPO_ANTELACION: 10},
        headers=headers_autenticado,
    )
    assert alta.status_code == 409, alta.text


@pytest.mark.asyncio
async def test_antelacion_sin_plan_que_incluya_calendario_da_402(
    http_client, tenant_y_usuario, headers_autenticado
):
    """R7: con el módulo encendido pero fuera del plan -> 402 y no se guarda."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    await execute("UPDATE tenant_subscriptions SET plan = 'starter' WHERE tenant_id = $1", tenant_id)

    r = await _patch_servicio(
        http_client, headers_autenticado, tenant_id, servicio["id"], {CAMPO_ANTELACION: 10}
    )

    assert r.status_code == 402, r.text
    assert await _antelacion_en_bd(servicio["id"]) is None


@pytest.mark.asyncio
async def test_antelacion_ver_como_sin_concesion_da_403(
    http_client, tenant_y_usuario, headers_autenticado, otro_tenant
):
    """R11: una sesión "ver como" es de solo lectura salvo concesión del dueño."""
    from security import crear_token_impersonacion

    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    servicio = await _crear_servicio(http_client, headers_autenticado, tenant_id)
    # Quien mira es un gerente de la plataforma (vive en otro tenant).
    gerente = otro_tenant["dueno"]
    await execute(
        "INSERT INTO gerencia_users (email, full_name, cargo) VALUES ($1, 'Test', 'Developer')",
        gerente["email"],
    )
    try:
        token, _ = crear_token_impersonacion(
            tenant_y_usuario["usuario_id"], tenant_id, "owner", gerente["id"], 15
        )
        r = await _patch_servicio(
            http_client,
            {"Authorization": f"Bearer {token}"},
            tenant_id,
            servicio["id"],
            {CAMPO_ANTELACION: 10},
        )
        assert r.status_code == 403, r.text
        assert await _antelacion_en_bd(servicio["id"]) is None
    finally:
        await execute("DELETE FROM gerencia_users WHERE LOWER(email) = LOWER($1)", gerente["email"])


# ------------------------------------------------------------
# Cancelación con origen, marca tardía y 409 (specs/001-regla-cancelacion-citas, T4)
# ------------------------------------------------------------
AHORA = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
RUTA_CANCELAR_AGENTE = "/api/eventos/calendario/reservas/cancelar"
CAMPOS_PREVIOS_RESERVA = (
    "id", "tenant_id", "proveedor_id", "proveedor_nombre", "proveedor_color", "servicio_id",
    "servicio_nombre", "hora_inicio", "hora_fin", "estado", "user_id", "cliente_nombre",
    "cliente_telefono", "notas", "precio_cobrado", "metodo_pago", "creado_en",
)
CAMPOS_NUEVOS_RESERVA = ("cancelacion_tardia", "cancelada_por", "antelacion_aplicada_horas")


@pytest.fixture
def reloj_fijo(monkeypatch):
    """Fija el reloj que routers/eventos.py le inyecta a cancelar_reserva (AHORA)."""

    class _Reloj(datetime):
        @classmethod
        def now(cls, tz=None):
            return AHORA

    monkeypatch.setattr("routers.eventos.datetime", _Reloj)


@pytest.fixture
def alertas(monkeypatch):
    """Captura las alertas que emite el endpoint del agente (general y al proveedor)."""
    capturadas: dict[str, list] = {"broadcast": [], "proveedor": []}

    async def _broadcast(tenant_id, *args, **kwargs):
        capturadas["broadcast"].append(args)

    async def _proveedor(tenant_id, portal_user_id, *args, **kwargs):
        capturadas["proveedor"].append(args)

    monkeypatch.setattr("routers.eventos.broadcast_alerta", _broadcast)
    monkeypatch.setattr("routers.eventos.avisar_a_proveedor", _proveedor)
    return capturadas


async def _escenario_cancelacion(http_client, headers, tenant_id, antelacion=24):
    await _encender_calendario(tenant_id)
    proveedor = await _crear_proveedor(http_client, headers, tenant_id)
    servicio = await _crear_servicio(http_client, headers, tenant_id)
    if antelacion is not None:
        r = await _patch_servicio(http_client, headers, tenant_id, servicio["id"], {CAMPO_ANTELACION: antelacion})
        assert r.status_code == 200, r.text
    return proveedor, servicio


async def _reserva_directa(tenant_id, proveedor_id, servicio_id, inicio, estado="confirmada", minutos=30):
    """Inserta una reserva sin pasar por la API (para fijar horas del pasado/relativas a AHORA)."""
    return await fetch_value(
        """
        INSERT INTO reservas (tenant_id, proveedor_id, servicio_id, hora_inicio, hora_fin, estado, cliente_nombre)
        VALUES ($1, $2, $3, $4, $5, $6, 'Cliente') RETURNING id
        """,
        tenant_id,
        UUID(str(proveedor_id)),
        UUID(str(servicio_id)),
        inicio,
        inicio + timedelta(minutes=minutos),
        estado,
    )


async def _cancelar_agente(http_client, monkeypatch, tenant_id, reserva_id, **extra):
    return await http_client.post(
        RUTA_CANCELAR_AGENTE,
        json={"tenant_id": str(tenant_id), "reserva_id": str(reserva_id), **extra},
        headers=_cabeceras_internas(monkeypatch),
    )


async def _auditoria_cancelada(reserva_id) -> list:
    return await fetch_all(
        "SELECT * FROM reserva_auditoria WHERE reserva_id = $1 AND evento = 'cancelada'", reserva_id
    )


@pytest.mark.asyncio
async def test_cancelar_agente_tardia_marca_audita_y_alerta(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch, reloj_fijo, alertas
):
    """Escenario 1 (R14, R21, R22, R23): N=24, cita a 3 h."""
    tenant_id = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario_cancelacion(http_client, headers_autenticado, tenant_id, 24)
    reserva_id = await _reserva_directa(tenant_id, proveedor["id"], servicio["id"], AHORA + timedelta(hours=3))

    r = await _cancelar_agente(http_client, monkeypatch, tenant_id, reserva_id)

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["estado"] == "cancelada"
    assert cuerpo["cancelada_por"] == "cliente"
    assert cuerpo["cancelacion_tardia"] is True
    assert cuerpo["antelacion_aplicada_horas"] == 24
    fila = await fetch_one(
        "SELECT cancelada_por, cancelacion_tardia, antelacion_aplicada_horas, cancelada_en FROM reservas WHERE id = $1",
        reserva_id,
    )
    assert (fila["cancelada_por"], fila["cancelacion_tardia"], fila["antelacion_aplicada_horas"]) == ("cliente", True, 24)
    assert fila["cancelada_en"] == AHORA

    auditoria = await _auditoria_cancelada(reserva_id)
    assert len(auditoria) == 1
    datos = auditoria[0]["datos_nuevos"]
    datos = datos if isinstance(datos, dict) else json.loads(datos)
    assert datos["cancelada_por"] == "cliente"
    assert datos["antelacion_aplicada_horas"] == 24
    assert datos["segundos_restantes"] == 10800

    sufijo = "(cancelación tardía: faltaban 3 h 0 min, límite 24 h)"
    assert len(alertas["broadcast"]) == 1 and len(alertas["proveedor"]) == 1
    for enviada in (alertas["broadcast"][0], alertas["proveedor"][0]):
        assert enviada[1] == "Reserva cancelada"
        assert enviada[2].endswith(sufijo)


@pytest.mark.asyncio
async def test_cancelar_agente_no_tardia_y_limite_exacto(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch, reloj_fijo, alertas
):
    """Escenario 2 (R15): 30 h y 24 h exactas no son tardías; la alerta no lleva sufijo."""
    tenant_id = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario_cancelacion(http_client, headers_autenticado, tenant_id, 24)
    lejana = await _reserva_directa(tenant_id, proveedor["id"], servicio["id"], AHORA + timedelta(hours=30))
    exacta = await _reserva_directa(tenant_id, proveedor["id"], servicio["id"], AHORA + timedelta(hours=24))
    justo = await _reserva_directa(
        tenant_id, proveedor["id"], servicio["id"], AHORA + timedelta(hours=24) - timedelta(seconds=1),
        minutos=1 / 60,
    )

    for reserva_id, tardia in ((lejana, False), (exacta, False), (justo, True)):
        r = await _cancelar_agente(http_client, monkeypatch, tenant_id, reserva_id)
        assert r.status_code == 200, r.text
        assert r.json()["cancelacion_tardia"] is tardia
        assert r.json()["antelacion_aplicada_horas"] == 24
    mensajes = [a[2] for a in alertas["broadcast"]]
    assert "tardía" not in mensajes[0] and "tardía" not in mensajes[1]
    assert mensajes[2].endswith("(cancelación tardía: faltaban 23 h 59 min, límite 24 h)")


@pytest.mark.asyncio
async def test_cancelar_agente_sin_regla_o_antelacion_cero(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch, reloj_fijo, alertas
):
    """Escenario 4 (R16): sin regla -> antelacion null; con N=0 nunca es tardía."""
    tenant_id = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario_cancelacion(http_client, headers_autenticado, tenant_id, None)
    reserva_id = await _reserva_directa(tenant_id, proveedor["id"], servicio["id"], AHORA + timedelta(minutes=5))

    r = await _cancelar_agente(http_client, monkeypatch, tenant_id, reserva_id)
    assert r.status_code == 200, r.text
    assert r.json()["cancelada_por"] == "cliente"
    assert r.json()["cancelacion_tardia"] is False
    assert r.json()["antelacion_aplicada_horas"] is None

    await _patch_servicio(http_client, headers_autenticado, tenant_id, servicio["id"], {CAMPO_ANTELACION: 0})
    otra = await _reserva_directa(tenant_id, proveedor["id"], servicio["id"], AHORA + timedelta(seconds=1))
    r0 = await _cancelar_agente(http_client, monkeypatch, tenant_id, otra)
    assert r0.status_code == 200, r0.text
    assert r0.json()["cancelacion_tardia"] is False
    assert r0.json()["antelacion_aplicada_horas"] == 0


@pytest.mark.asyncio
async def test_cancelar_desde_el_portal_es_equipo_y_nunca_tardia(
    http_client, tenant_y_usuario, headers_autenticado
):
    """Escenario 3 (R17): el equipo no queda sujeto a la regla, ni a cita_ya_iniciada."""
    tenant_id = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario_cancelacion(http_client, headers_autenticado, tenant_id, 24)
    # Futura cercana (tardía si fuera cliente) y ya iniciada (R13 no aplica al equipo).
    ahora = datetime.now(timezone.utc)
    cercana = await _reserva_directa(tenant_id, proveedor["id"], servicio["id"], ahora + timedelta(hours=3))
    iniciada = await _reserva_directa(tenant_id, proveedor["id"], servicio["id"], ahora - timedelta(minutes=10))

    for reserva_id in (cercana, iniciada):
        r = await http_client.post(
            f"/api/tenants/{tenant_id}/calendario/reservas/{reserva_id}/cancelar",
            json={"motivo": "lo pidió por teléfono"},
            headers=headers_autenticado,
        )
        assert r.status_code == 200, r.text
        cuerpo = r.json()
        assert cuerpo["cancelada_por"] == "equipo"
        assert cuerpo["cancelacion_tardia"] is False
        assert cuerpo["antelacion_aplicada_horas"] is None
        auditoria = await _auditoria_cancelada(reserva_id)
        assert len(auditoria) == 1


@pytest.mark.asyncio
async def test_reintento_de_cancelar_da_409_sin_segunda_auditoria_ni_cambio_de_marca(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch, reloj_fijo, alertas
):
    """Escenario 5 (R12): el reintento de n8n recibe 409 con estado 'cancelada'; nada cambia."""
    tenant_id = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario_cancelacion(http_client, headers_autenticado, tenant_id, 24)
    reserva_id = await _reserva_directa(tenant_id, proveedor["id"], servicio["id"], AHORA + timedelta(hours=3))
    primera = await _cancelar_agente(http_client, monkeypatch, tenant_id, reserva_id)
    assert primera.status_code == 200, primera.text
    antes = await fetch_one(
        "SELECT cancelada_por, cancelacion_tardia, antelacion_aplicada_horas, cancelada_en, motivo_cancelacion "
        "FROM reservas WHERE id = $1",
        reserva_id,
    )

    reintento = await _cancelar_agente(http_client, monkeypatch, tenant_id, reserva_id, motivo="otra vez")
    assert reintento.status_code == 409, reintento.text
    detalle = reintento.json()["detail"]
    assert detalle["codigo"] == "reserva_no_confirmada"
    assert detalle["estado"] == "cancelada"
    assert detalle["mensaje"]

    despues = await fetch_one(
        "SELECT cancelada_por, cancelacion_tardia, antelacion_aplicada_horas, cancelada_en, motivo_cancelacion "
        "FROM reservas WHERE id = $1",
        reserva_id,
    )
    assert dict(despues) == dict(antes)
    assert len(await _auditoria_cancelada(reserva_id)) == 1
    assert len(alertas["broadcast"]) == 1  # sin segunda alerta

    # Desde el portal también: 409 con el mismo objeto.
    portal = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas/{reserva_id}/cancelar",
        json={},
        headers=headers_autenticado,
    )
    assert portal.status_code == 409, portal.text
    assert portal.json()["detail"]["codigo"] == "reserva_no_confirmada"
    assert portal.json()["detail"]["estado"] == "cancelada"
    assert len(await _auditoria_cancelada(reserva_id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("estado", ["completada", "no_asistio"])
async def test_cancelar_reserva_cerrada_da_409_aunque_ya_haya_empezado(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch, reloj_fijo, alertas, estado
):
    """Escenario 5 (R12): el estado manda sobre la hora de inicio."""
    tenant_id = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario_cancelacion(http_client, headers_autenticado, tenant_id, 24)
    reserva_id = await _reserva_directa(
        tenant_id, proveedor["id"], servicio["id"], AHORA - timedelta(hours=2), estado=estado
    )

    r = await _cancelar_agente(http_client, monkeypatch, tenant_id, reserva_id)

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["codigo"] == "reserva_no_confirmada"
    assert r.json()["detail"]["estado"] == estado
    assert await _auditoria_cancelada(reserva_id) == []
    assert await fetch_value("SELECT estado FROM reservas WHERE id = $1", reserva_id) == estado

    portal = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas/{reserva_id}/cancelar",
        json={},
        headers=headers_autenticado,
    )
    assert portal.status_code == 409, portal.text
    assert portal.json()["detail"]["estado"] == estado


@pytest.mark.asyncio
async def test_dos_cancelaciones_simultaneas_una_exito_y_una_409(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    """Escenario 5 (R19): FOR UPDATE serializa; una sola auditoría."""
    import asyncio

    tenant_id = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario_cancelacion(http_client, headers_autenticado, tenant_id, 24)
    reserva_id = await _reserva_directa(
        tenant_id, proveedor["id"], servicio["id"], datetime.now(timezone.utc) + timedelta(days=3)
    )

    respuestas = await asyncio.gather(
        _cancelar_agente(http_client, monkeypatch, tenant_id, reserva_id),
        _cancelar_agente(http_client, monkeypatch, tenant_id, reserva_id),
    )

    assert sorted(r.status_code for r in respuestas) == [200, 409]
    perdedora = next(r for r in respuestas if r.status_code == 409)
    assert perdedora.json()["detail"]["codigo"] == "reserva_no_confirmada"
    assert len(await _auditoria_cancelada(reserva_id)) == 1


@pytest.mark.asyncio
async def test_cita_iniciada_cliente_409_hasta_que_el_job_la_pasa_a_no_asistio(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch, reloj_fijo, alertas
):
    """Escenarios 6 y casos límite (R13): cita_ya_iniciada -> reserva_no_confirmada tras el job."""
    from services.calendario import marcar_vencidas_no_asistio

    tenant_id = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario_cancelacion(http_client, headers_autenticado, tenant_id, 24)
    iniciada = await _reserva_directa(
        tenant_id, proveedor["id"], servicio["id"], AHORA - timedelta(minutes=1), minutos=0.5
    )
    igual = await _reserva_directa(tenant_id, proveedor["id"], servicio["id"], AHORA)

    for reserva_id in (iniciada, igual):
        r = await _cancelar_agente(http_client, monkeypatch, tenant_id, reserva_id)
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == {
            "codigo": "cita_ya_iniciada",
            "mensaje": r.json()["detail"]["mensaje"],
            "estado": "confirmada",
        }
        assert await _auditoria_cancelada(reserva_id) == []
        assert await fetch_value("SELECT cancelada_por FROM reservas WHERE id = $1", reserva_id) is None

    # El equipo sí puede cancelar la iniciada desde el portal mientras siga confirmada.
    equipo = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas/{iniciada}/cancelar", json={}, headers=headers_autenticado
    )
    assert equipo.status_code == 200, equipo.text
    assert equipo.json()["cancelada_por"] == "equipo"

    # La tarea de citas vencidas (hora real: 2026-01-10 ya pasó) la pasa a no_asistio.
    assert await marcar_vencidas_no_asistio(0) >= 1
    despues = await _cancelar_agente(http_client, monkeypatch, tenant_id, igual)
    assert despues.status_code == 409, despues.text
    assert despues.json()["detail"]["codigo"] == "reserva_no_confirmada"
    assert despues.json()["detail"]["estado"] == "no_asistio"


@pytest.mark.asyncio
async def test_cancelar_reserva_ajena_o_inexistente_da_404(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch, otro_tenant
):
    tenant_id = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario_cancelacion(http_client, headers_autenticado, tenant_id, 24)
    reserva_id = await _reserva_directa(
        tenant_id, proveedor["id"], servicio["id"], datetime.now(timezone.utc) + timedelta(days=3)
    )
    await _encender_calendario(otro_tenant["tenant_id"])

    ajena = await _cancelar_agente(http_client, monkeypatch, otro_tenant["tenant_id"], reserva_id)
    inexistente = await _cancelar_agente(http_client, monkeypatch, tenant_id, uuid4())

    assert ajena.status_code == 404, ajena.text
    assert inexistente.status_code == 404, inexistente.text
    assert await fetch_value("SELECT estado FROM reservas WHERE id = $1", reserva_id) == "confirmada"


@pytest.mark.asyncio
async def test_cancelar_agente_calendario_apagado_409_con_codigo_y_portal_conserva_su_forma(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch
):
    """Escenario 12 (R24): 409 objeto en el agente; el 409 del portal sigue siendo texto."""
    tenant_id = tenant_y_usuario["tenant_id"]

    r = await _cancelar_agente(http_client, monkeypatch, tenant_id, uuid4())
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["codigo"] == "calendario_inactivo"
    assert r.json()["detail"]["mensaje"]
    assert "estado" not in r.json()["detail"]

    portal = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas/{uuid4()}/cancelar", json={}, headers=headers_autenticado
    )
    assert portal.status_code == 409, portal.text
    assert isinstance(portal.json()["detail"], str)


@pytest.mark.asyncio
async def test_cancelar_agente_sin_pagos_da_402(http_client, tenant_y_usuario, monkeypatch):
    """Escenario 12 (R24): calendario encendido pero sin suscripción ni créditos -> 402."""
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    await execute("UPDATE tenant_subscriptions SET estado = 'pausada' WHERE tenant_id = $1", tenant_id)
    await execute("DELETE FROM tenant_credits WHERE tenant_id = $1", tenant_id)

    r = await _cancelar_agente(http_client, monkeypatch, tenant_id, uuid4())

    assert r.status_code == 402, r.text


@pytest.mark.asyncio
async def test_cancelar_conserva_campos_previos_e_ignora_origen_y_marca_del_cuerpo(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch, reloj_fijo, alertas
):
    """Escenario 21 (R18): la respuesta solo gana campos; lo enviado como origen/marca se ignora."""
    tenant_id = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario_cancelacion(http_client, headers_autenticado, tenant_id, 24)
    reserva_id = await _reserva_directa(tenant_id, proveedor["id"], servicio["id"], AHORA + timedelta(hours=3))

    r = await _cancelar_agente(
        http_client,
        monkeypatch,
        tenant_id,
        reserva_id,
        cancelada_por="equipo",
        cancelacion_tardia=False,
        antelacion_aplicada_horas=0,
        origen="equipo",
    )

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    for campo in CAMPOS_PREVIOS_RESERVA + CAMPOS_NUEVOS_RESERVA:
        assert campo in cuerpo, campo
    assert cuerpo["cancelada_por"] == "cliente"
    assert cuerpo["cancelacion_tardia"] is True
    assert cuerpo["antelacion_aplicada_horas"] == 24

    # Lo mismo en el portal: el cuerpo no puede declarar ser "cliente" ni tardía.
    otra = await _reserva_directa(
        tenant_id, proveedor["id"], servicio["id"], datetime.now(timezone.utc) + timedelta(hours=3)
    )
    portal = await http_client.post(
        f"/api/tenants/{tenant_id}/calendario/reservas/{otra}/cancelar",
        json={"cancelada_por": "cliente", "cancelacion_tardia": True},
        headers=headers_autenticado,
    )
    assert portal.status_code == 200, portal.text
    for campo in CAMPOS_PREVIOS_RESERVA + CAMPOS_NUEVOS_RESERVA:
        assert campo in portal.json(), campo
    assert portal.json()["cancelada_por"] == "equipo"
    assert portal.json()["cancelacion_tardia"] is False


@pytest.mark.asyncio
async def test_cambiar_la_regla_despues_no_reescribe_la_marca_guardada(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch, reloj_fijo, alertas
):
    """R9: la marca queda congelada al cancelar."""
    tenant_id = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario_cancelacion(http_client, headers_autenticado, tenant_id, 24)
    reserva_id = await _reserva_directa(tenant_id, proveedor["id"], servicio["id"], AHORA + timedelta(hours=3))
    await _cancelar_agente(http_client, monkeypatch, tenant_id, reserva_id)

    await _patch_servicio(http_client, headers_autenticado, tenant_id, servicio["id"], {CAMPO_ANTELACION: None})

    fila = await fetch_one(
        "SELECT cancelacion_tardia, antelacion_aplicada_horas FROM reservas WHERE id = $1", reserva_id
    )
    assert (fila["cancelacion_tardia"], fila["antelacion_aplicada_horas"]) == (True, 24)


@pytest.mark.asyncio
async def test_listado_del_portal_incluye_los_campos_de_cancelacion(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch, reloj_fijo, alertas
):
    """R25/R26 (datos): el listado trae los tres campos, también en no canceladas."""
    tenant_id = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario_cancelacion(http_client, headers_autenticado, tenant_id, 24)
    cancelada = await _reserva_directa(tenant_id, proveedor["id"], servicio["id"], AHORA + timedelta(hours=3))
    viva = await _reserva_directa(tenant_id, proveedor["id"], servicio["id"], AHORA + timedelta(hours=6))
    await _cancelar_agente(http_client, monkeypatch, tenant_id, cancelada)

    r = await http_client.get(
        f"/api/tenants/{tenant_id}/calendario/reservas",
        params={"desde": (AHORA - timedelta(days=1)).isoformat(), "hasta": (AHORA + timedelta(days=1)).isoformat()},
        headers=headers_autenticado,
    )

    assert r.status_code == 200, r.text
    por_id = {x["id"]: x for x in r.json()}
    assert por_id[str(cancelada)]["cancelacion_tardia"] is True
    assert por_id[str(cancelada)]["cancelada_por"] == "cliente"
    assert por_id[str(viva)]["cancelacion_tardia"] is False
    assert por_id[str(viva)]["cancelada_por"] is None
    assert por_id[str(viva)]["antelacion_aplicada_horas"] is None


# ------------------------------------------------------------
# Consulta previa del agente (specs/001-regla-cancelacion-citas, T5, escenario 11)
# ------------------------------------------------------------
RUTA_CONSULTAR_AGENTE = "/api/eventos/calendario/reservas/consultar-cancelacion"


async def _consultar_agente(http_client, monkeypatch, tenant_id, reserva_id):
    return await http_client.post(
        RUTA_CONSULTAR_AGENTE,
        json={"tenant_id": str(tenant_id), "reserva_id": str(reserva_id)},
        headers=_cabeceras_internas(monkeypatch),
    )


async def _estado_reserva_y_auditoria(reserva_id):
    fila = await fetch_one("SELECT * FROM reservas WHERE id = $1", reserva_id)
    n = await fetch_value("SELECT COUNT(*) FROM reserva_auditoria WHERE reserva_id = $1", reserva_id)
    return dict(fila), n


@pytest.mark.asyncio
async def test_consulta_previa_tardia_y_no_tardia_sin_modificar_nada(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch, reloj_fijo
):
    """Escenario 11 (R27, R28): N=24, a 3 h seria tardia; a 30 h no. La reserva y la auditoria no cambian."""
    tenant_id = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario_cancelacion(http_client, headers_autenticado, tenant_id, 24)
    cerca = await _reserva_directa(tenant_id, proveedor["id"], servicio["id"], AHORA + timedelta(hours=3))
    lejos = await _reserva_directa(tenant_id, proveedor["id"], servicio["id"], AHORA + timedelta(hours=30))
    antes_cerca = await _estado_reserva_y_auditoria(cerca)

    r = await _consultar_agente(http_client, monkeypatch, tenant_id, cerca)

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["reserva_id"] == str(cerca)
    assert cuerpo["estado"] == "confirmada"
    assert cuerpo["servicio_id"] == str(servicio["id"])
    assert cuerpo["servicio_nombre"] == servicio["nombre"]
    assert cuerpo["antelacion_minima_horas"] == 24
    assert cuerpo["segundos_restantes"] == 10800
    assert cuerpo["puede_cancelar"] is True
    assert cuerpo["motivo_no_cancelable"] is None
    assert cuerpo["cancelacion_seria_tardia"] is True
    assert cuerpo["hora_inicio"]
    assert await _estado_reserva_y_auditoria(cerca) == antes_cerca

    r2 = await _consultar_agente(http_client, monkeypatch, tenant_id, lejos)
    assert r2.status_code == 200, r2.text
    assert r2.json()["cancelacion_seria_tardia"] is False
    assert r2.json()["segundos_restantes"] == 30 * 3600


@pytest.mark.asyncio
async def test_consulta_previa_coincide_con_la_cancelacion_real(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch, reloj_fijo, alertas
):
    """R28: la marca anunciada es la que recibe la cancelacion hecha en el mismo instante (incluye el limite exacto)."""
    tenant_id = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario_cancelacion(http_client, headers_autenticado, tenant_id, 24)
    for delta in (timedelta(hours=24), timedelta(hours=24) - timedelta(seconds=1), timedelta(hours=5)):
        reserva_id = await _reserva_directa(tenant_id, proveedor["id"], servicio["id"], AHORA + delta)
        consulta = await _consultar_agente(http_client, monkeypatch, tenant_id, reserva_id)
        real = await _cancelar_agente(http_client, monkeypatch, tenant_id, reserva_id)
        assert consulta.json()["cancelacion_seria_tardia"] is real.json()["cancelacion_tardia"], delta


@pytest.mark.asyncio
async def test_consulta_previa_sin_regla(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch, reloj_fijo
):
    tenant_id = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario_cancelacion(http_client, headers_autenticado, tenant_id, None)
    reserva_id = await _reserva_directa(tenant_id, proveedor["id"], servicio["id"], AHORA + timedelta(hours=1))

    r = await _consultar_agente(http_client, monkeypatch, tenant_id, reserva_id)

    assert r.status_code == 200, r.text
    assert r.json()["antelacion_minima_horas"] is None
    assert r.json()["cancelacion_seria_tardia"] is False
    assert r.json()["puede_cancelar"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "estado, desfase, motivo",
    [
        ("cancelada", timedelta(hours=3), "reserva_no_confirmada"),
        ("completada", timedelta(hours=-2), "reserva_no_confirmada"),  # ambas condiciones: gana el estado
        ("no_asistio", timedelta(hours=-2), "reserva_no_confirmada"),
        ("confirmada", timedelta(hours=-2), "cita_ya_iniciada"),
        ("confirmada", timedelta(0), "cita_ya_iniciada"),  # hora actual == inicio
    ],
)
async def test_consulta_previa_motivo_no_cancelable_con_precedencia_estado_sobre_inicio(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch, reloj_fijo, estado, desfase, motivo
):
    tenant_id = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario_cancelacion(http_client, headers_autenticado, tenant_id, 24)
    reserva_id = await _reserva_directa(
        tenant_id, proveedor["id"], servicio["id"], AHORA + desfase, estado=estado
    )
    antes = await _estado_reserva_y_auditoria(reserva_id)

    r = await _consultar_agente(http_client, monkeypatch, tenant_id, reserva_id)

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["estado"] == estado
    assert cuerpo["puede_cancelar"] is False
    assert cuerpo["motivo_no_cancelable"] == motivo
    assert cuerpo["cancelacion_seria_tardia"] is False
    assert cuerpo["segundos_restantes"] == int(desfase.total_seconds())
    assert await _estado_reserva_y_auditoria(reserva_id) == antes


@pytest.mark.asyncio
async def test_consulta_previa_reserva_ajena_o_inexistente_da_404(
    http_client, tenant_y_usuario, headers_autenticado, monkeypatch, otro_tenant
):
    tenant_id = tenant_y_usuario["tenant_id"]
    proveedor, servicio = await _escenario_cancelacion(http_client, headers_autenticado, tenant_id, 24)
    reserva_id = await _reserva_directa(
        tenant_id, proveedor["id"], servicio["id"], datetime.now(timezone.utc) + timedelta(days=3)
    )
    await _encender_calendario(otro_tenant["tenant_id"])

    ajena = await _consultar_agente(http_client, monkeypatch, otro_tenant["tenant_id"], reserva_id)
    inexistente = await _consultar_agente(http_client, monkeypatch, tenant_id, uuid4())

    assert ajena.status_code == 404, ajena.text
    assert inexistente.status_code == 404, inexistente.text
    assert ajena.json() == inexistente.json()


@pytest.mark.asyncio
async def test_consulta_previa_calendario_apagado_409_con_codigo(
    http_client, tenant_y_usuario, monkeypatch
):
    r = await _consultar_agente(http_client, monkeypatch, tenant_y_usuario["tenant_id"], uuid4())

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["codigo"] == "calendario_inactivo"
    assert "estado" not in r.json()["detail"]


@pytest.mark.asyncio
async def test_consulta_previa_sin_pagos_da_402(http_client, tenant_y_usuario, monkeypatch):
    tenant_id = tenant_y_usuario["tenant_id"]
    await _encender_calendario(tenant_id)
    await execute("UPDATE tenant_subscriptions SET estado = 'pausada' WHERE tenant_id = $1", tenant_id)
    await execute("DELETE FROM tenant_credits WHERE tenant_id = $1", tenant_id)

    r = await _consultar_agente(http_client, monkeypatch, tenant_id, uuid4())

    assert r.status_code == 402, r.text


@pytest.mark.asyncio
async def test_consulta_previa_exige_el_token_interno(http_client, tenant_y_usuario, monkeypatch):
    _cabeceras_internas(monkeypatch)
    r = await http_client.post(
        RUTA_CONSULTAR_AGENTE,
        json={"tenant_id": str(tenant_y_usuario["tenant_id"]), "reserva_id": str(uuid4())},
    )
    assert r.status_code in (401, 403), r.text
