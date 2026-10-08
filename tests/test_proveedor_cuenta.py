"""
Proveedor del calendario con cuenta propia (sql/37_proveedor_cuenta.sql).

Qué puede: ver y cancelar sus citas y marcarlas; leer su horario; poner y
quitar sus descansos y días libres; ver y atender las conversaciones de los
clientes cuya cita MÁS RECIENTE es con él. Todo lo demás del calendario y
de conversaciones le da 403. Más el cupo de proveedores del plan y el job
que da por 'no_asistio' las citas vencidas.
"""

import itertools
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from security import crear_access_token, hash_password
from services import calendario as calendario_svc
from services.pipeline import set_tenant_servicios
from session import execute, fetch_one, fetch_value

pytestmark = pytest.mark.usefixtures("plan_enterprise")


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _cuenta(tenant_id, role: str):
    uid = uuid4()
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, $5, true)
        """,
        uid,
        tenant_id,
        f"{role}-{uid}@ejemplo.com",
        hash_password("test_password"),
        role,
    )
    return uid, crear_access_token(uid, tenant_id, role)


async def _proveedor(tenant_id, nombre, portal_user_id=None):
    return await fetch_value(
        "INSERT INTO proveedores (tenant_id, nombre, portal_user_id) VALUES ($1, $2, $3) RETURNING id",
        tenant_id,
        nombre,
        portal_user_id,
    )


async def _cliente_con_chat(tenant_id, nombre):
    """(user_id, conversation_id) de un cliente que escribió por WhatsApp."""
    user_id = await fetch_value(
        "INSERT INTO users (tenant_id, display_name) VALUES ($1, $2) RETURNING id", tenant_id, nombre
    )
    conv_id = await fetch_value(
        "INSERT INTO conversations (tenant_id, user_id, channel_type) VALUES ($1, $2, 'whatsapp') RETURNING id",
        tenant_id,
        user_id,
    )
    return user_id, conv_id


# Una hora distinta por cita: dos del mismo proveedor que se traslapen las
# rechaza el EXCLUDE de reservas (16_calendarios.sql).
_horas = itertools.count(1)


async def _reserva(tenant_id, proveedor_id, servicio_id, user_id=None, inicio=None, estado="confirmada", creada=None):
    """Directo por SQL: lo que se prueba es quién la ve, no la validación del alta."""
    inicio = inicio or datetime.now(timezone.utc) + timedelta(days=3, hours=next(_horas))
    return await fetch_value(
        """
        INSERT INTO reservas
            (tenant_id, proveedor_id, servicio_id, user_id, cliente_nombre,
             hora_inicio, hora_fin, estado, creado_en)
        VALUES ($1, $2, $3, $4, 'Cliente', $5::timestamptz, $5::timestamptz + INTERVAL '30 minutes', $6,
                COALESCE($7::timestamptz, NOW()))
        RETURNING id
        """,
        tenant_id,
        proveedor_id,
        servicio_id,
        user_id,
        inicio,
        estado,
        creada,
    )


@pytest.fixture
async def agenda(tenant_y_usuario, http_client):
    """Dueño, un member, Juan (con cuenta) y Pedro (sin cuenta), con horario todos los días."""
    tid = tenant_y_usuario["tenant_id"]
    await set_tenant_servicios(tid, None, None, calendario_activo=True)

    uid_juan, token_juan = await _cuenta(tid, "proveedor")
    juan = await _proveedor(tid, "Juan", uid_juan)
    pedro = await _proveedor(tid, "Pedro")
    _, token_member = await _cuenta(tid, "member")
    servicio = await fetch_value(
        "INSERT INTO servicios (tenant_id, nombre, duracion_minutos) VALUES ($1, 'Corte', 30) RETURNING id",
        tid,
    )

    owner = _h(tenant_y_usuario["token"])
    bloques = [{"dia_semana": d, "hora_inicio": "00:00:00", "hora_fin": "23:59:00"} for d in range(7)]
    for p in (juan, pedro):
        r = await http_client.put(
            f"/api/tenants/{tid}/calendario/proveedores/{p}/horarios", json={"bloques": bloques}, headers=owner
        )
        assert r.status_code == 200, r.text

    return {
        "tid": tid,
        "owner": owner,
        "juan_h": _h(token_juan),
        "juan_uid": uid_juan,
        "member_h": _h(token_member),
        "juan": juan,
        "pedro": pedro,
        "servicio": servicio,
        "base": f"/api/tenants/{tid}/calendario",
    }


# ============================================================
# Invitación
# ============================================================
@pytest.mark.asyncio
async def test_invitar_proveedor_liga_su_ficha(http_client, agenda):
    email = f"barbero-{uuid4()}@ejemplo.com"
    r = await http_client.post(
        "/api/equipo/invitaciones",
        json={"email": email, "role": "proveedor", "proveedor_id": str(agenda["pedro"])},
        headers=agenda["owner"],
    )
    assert r.status_code == 201, r.text
    assert r.json()["proveedor_nombre"] == "Pedro"
    token = r.json()["enlace"].split("#t=", 1)[1]

    r = await http_client.post(
        "/api/auth/invitacion/aceptar",
        json={"token": token, "password": "clave-segura-1", "acepta_terminos": True},
    )
    assert r.status_code == 201, r.text
    sesion = _h(r.json()["access_token"])

    r = await http_client.get(f"{agenda['base']}/proveedores", headers=sesion)
    assert r.status_code == 200
    assert [p["id"] for p in r.json()] == [str(agenda["pedro"])]


@pytest.mark.asyncio
async def test_invitacion_de_proveedor_exige_su_ficha(http_client, agenda):
    r = await http_client.post(
        "/api/equipo/invitaciones",
        json={"email": f"x-{uuid4()}@ejemplo.com", "role": "proveedor"},
        headers=agenda["owner"],
    )
    assert r.status_code == 422
    r = await http_client.post(
        "/api/equipo/invitaciones",
        json={"email": f"x-{uuid4()}@ejemplo.com", "role": "proveedor", "proveedor_id": str(agenda["juan"])},
        headers=agenda["owner"],
    )
    assert r.status_code == 409  # Juan ya tiene acceso


# ============================================================
# Lo que no le toca
# ============================================================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metodo,ruta",
    [
        ("get", "{base}/corte-diario"),
        ("get", "{base}/auditoria"),
        ("get", "{base}/proveedores/cupo"),
        ("post", "{base}/servicios"),
        ("post", "{base}/proveedores"),
        ("put", "{base}/proveedores/{juan}/horarios"),
        ("get", "{base}/proveedores/{pedro}/horarios"),
        ("get", "{base}/proveedores/{pedro}/descansos"),
        ("post", "{base}/reservas"),
        ("get", "/api/conversaciones/metricas"),
        ("get", "/api/agente"),
        ("get", "/api/tenants/{tid}/vendedores"),
    ],
)
async def test_proveedor_no_entra_a_lo_ajeno(http_client, agenda, metodo, ruta):
    url = ruta.format(base=agenda["base"], juan=agenda["juan"], pedro=agenda["pedro"], tid=agenda["tid"])
    kwargs = {"json": {}} if metodo in ("post", "put") else {}
    r = await getattr(http_client, metodo)(url, headers=agenda["juan_h"], **kwargs)
    assert r.status_code == 403, (url, r.status_code, r.text)


@pytest.mark.asyncio
async def test_proveedor_desactivado_pierde_acceso(http_client, agenda):
    await execute("UPDATE proveedores SET activo = false WHERE id = $1", agenda["juan"])
    r = await http_client.get(f"{agenda['base']}/proveedores", headers=agenda["juan_h"])
    assert r.status_code == 403


# ============================================================
# Sus citas
# ============================================================
@pytest.mark.asyncio
async def test_solo_ve_sus_citas_aunque_pida_las_de_otro(http_client, agenda):
    suya = await _reserva(agenda["tid"], agenda["juan"], agenda["servicio"])
    await _reserva(agenda["tid"], agenda["pedro"], agenda["servicio"])
    desde = datetime.now(timezone.utc).isoformat()
    hasta = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

    r = await http_client.get(
        f"{agenda['base']}/reservas",
        params={"desde": desde, "hasta": hasta, "proveedor_id": str(agenda["pedro"])},
        headers=agenda["juan_h"],
    )
    assert r.status_code == 200
    assert [x["id"] for x in r.json()] == [str(suya)]


@pytest.mark.asyncio
async def test_cancela_y_marca_las_suyas_no_las_ajenas(http_client, agenda):
    suya = await _reserva(agenda["tid"], agenda["juan"], agenda["servicio"])
    otra = await _reserva(agenda["tid"], agenda["pedro"], agenda["servicio"])
    otra_mas = await _reserva(agenda["tid"], agenda["juan"], agenda["servicio"])
    h = agenda["juan_h"]

    r = await http_client.post(f"{agenda['base']}/reservas/{suya}/cancelar", json={}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "cancelada"
    r = await http_client.post(f"{agenda['base']}/reservas/{otra}/cancelar", json={}, headers=h)
    assert r.status_code == 403

    r = await http_client.patch(
        f"{agenda['base']}/reservas/{otra_mas}/estado",
        json={"estado": "completada", "precio_cobrado": "150", "metodo_pago": "efectivo"},
        headers=h,
    )
    assert r.status_code == 200, r.text

    # Ni reprogramar ni cambiar de barbero: eso es del dueño.
    r = await http_client.patch(
        f"{agenda['base']}/reservas/{otra_mas}/reasignar",
        json={"proveedor_id": str(agenda["pedro"])},
        headers=h,
    )
    assert r.status_code == 403

    actor = await fetch_value(
        "SELECT actor FROM reserva_auditoria WHERE reserva_id = $1 AND evento = 'cancelada'", suya
    )
    assert actor.startswith("proveedor-")  # el correo de Juan, no el del dueño


# ============================================================
# Su disponibilidad
# ============================================================
@pytest.mark.asyncio
async def test_maneja_sus_descansos_y_dias_libres(http_client, agenda):
    h, base, juan, pedro = agenda["juan_h"], agenda["base"], agenda["juan"], agenda["pedro"]

    r = await http_client.post(
        f"{base}/proveedores/{juan}/descansos",
        json={"dia_semana": 0, "hora_inicio": "14:00:00", "hora_fin": "15:00:00", "etiqueta": "comida"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    descanso = r.json()["id"]
    assert (await http_client.delete(f"{base}/descansos/{descanso}", headers=h)).status_code == 204

    r = await http_client.post(
        f"{base}/proveedores/{pedro}/descansos",
        json={"dia_semana": 0, "hora_inicio": "14:00:00", "hora_fin": "15:00:00"},
        headers=h,
    )
    assert r.status_code == 403

    dia = (datetime.now().date() + timedelta(days=60)).isoformat()
    r = await http_client.post(f"{base}/proveedores/{juan}/excepciones", json={"fecha": dia}, headers=h)
    assert r.status_code == 200, r.text

    # Un horario especial cambia su jornada: eso lo fija el dueño.
    r = await http_client.post(
        f"{base}/proveedores/{juan}/excepciones",
        json={"fecha": dia, "disponible": True, "hora_inicio": "10:00:00", "hora_fin": "12:00:00"},
        headers=h,
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_member_sigue_sin_tocar_la_disponibilidad(http_client, agenda):
    r = await http_client.post(
        f"{agenda['base']}/proveedores/{agenda['juan']}/descansos",
        json={"dia_semana": 0, "hora_inicio": "14:00:00", "hora_fin": "15:00:00"},
        headers=agenda["member_h"],
    )
    assert r.status_code == 403


# ============================================================
# Conversaciones de sus clientes
# ============================================================
@pytest.mark.asyncio
async def test_ve_las_conversaciones_de_sus_clientes(http_client, agenda, monkeypatch):
    tid, juan, pedro, servicio = agenda["tid"], agenda["juan"], agenda["pedro"], agenda["servicio"]
    ahora = datetime.now(timezone.utc)

    ana, conv_ana = await _cliente_con_chat(tid, "Ana")  # cita con Juan
    beto, conv_beto = await _cliente_con_chat(tid, "Beto")  # cita con Pedro
    caro, conv_caro = await _cliente_con_chat(tid, "Caro")  # antes Pedro, ahora Juan
    dani, conv_dani = await _cliente_con_chat(tid, "Dani")  # antes Juan, ahora Pedro
    _, conv_eva = await _cliente_con_chat(tid, "Eva")  # nunca reservó

    await _reserva(tid, juan, servicio, ana, estado="cancelada")  # cancelada también cuenta
    await _reserva(tid, pedro, servicio, beto)
    await _reserva(tid, pedro, servicio, caro, creada=ahora - timedelta(days=5))
    await _reserva(tid, juan, servicio, caro, creada=ahora - timedelta(days=1))
    await _reserva(tid, juan, servicio, dani, creada=ahora - timedelta(days=5))
    await _reserva(tid, pedro, servicio, dani, creada=ahora - timedelta(days=1))

    h = agenda["juan_h"]
    r = await http_client.get("/api/conversaciones", headers=h)
    assert r.status_code == 200, r.text
    assert {c["id"] for c in r.json()} == {str(conv_ana), str(conv_caro)}

    # Las ajenas se frenan antes de tocar nada; la suya pasa (ver "tomar" abajo).
    for ajena in (conv_beto, conv_dani, conv_eva):
        assert (await http_client.get(f"/api/conversaciones/{ajena}", headers=h)).status_code == 403
    assert (await http_client.get(f"/api/conversaciones/{uuid4()}", headers=h)).status_code == 404

    # Puede tomar el chat de su cliente, no el ajeno, ni editar el contacto.
    # La escritura en sí se reemplaza: toca columnas de n8n (conversations.metadata)
    # que la base local de pruebas no tiene; lo que se prueba acá es la guarda.
    from routers import conversaciones as router_conv

    async def tomar_falso(tenant_id, conversacion_id, usuario_id):
        return {"id": conversacion_id, "status": "transferred", "asignado_a": usuario_id}

    async def emit_falso(*_a, **_k):
        return None

    monkeypatch.setattr(router_conv.asig, "tomar", tomar_falso)
    monkeypatch.setattr(router_conv, "emit_conversacion_estado", emit_falso)
    r = await http_client.post(f"/api/conversaciones/{conv_ana}/tomar", headers=h)
    assert r.status_code == 200, r.text
    assert (await http_client.post(f"/api/conversaciones/{conv_beto}/tomar", headers=h)).status_code == 403
    assert (await http_client.post(f"/api/conversaciones/{conv_ana}/contacto", headers=h)).status_code == 403

    # El dueño las sigue viendo todas.
    r = await http_client.get("/api/conversaciones", headers=agenda["owner"])
    assert {str(conv_ana), str(conv_beto), str(conv_eva)} <= {c["id"] for c in r.json()}


# ============================================================
# Cupo de proveedores del plan
# ============================================================
@pytest.fixture
async def tope_dos(agenda):
    """Enterprise con tope de 2 proveedores (Juan y Pedro ya los ocupan)."""
    previo = await fetch_value("SELECT max_proveedores FROM planes WHERE nombre = 'enterprise'")
    await execute("UPDATE planes SET max_proveedores = 2 WHERE nombre = 'enterprise'")
    yield agenda
    await execute("UPDATE planes SET max_proveedores = $1 WHERE nombre = 'enterprise'", previo)


@pytest.mark.asyncio
async def test_alta_de_proveedor_respeta_el_cupo(http_client, tope_dos):
    base, owner = tope_dos["base"], tope_dos["owner"]
    r = await http_client.post(f"{base}/proveedores", json={"nombre": "Luis"}, headers=owner)
    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "cupo_proveedores"

    r = await http_client.get(f"{base}/proveedores/cupo", headers=owner)
    assert r.json() == {"plan": "enterprise", "maximo": 2, "activos": 2}

    # Desactivar libera el lugar.
    r = await http_client.patch(f"{base}/proveedores/{tope_dos['pedro']}", json={"activo": False}, headers=owner)
    assert r.status_code == 200
    r = await http_client.post(f"{base}/proveedores", json={"nombre": "Luis"}, headers=owner)
    assert r.status_code == 201, r.text
    # Y Pedro ya no puede volver mientras Luis ocupe su lugar.
    r = await http_client.patch(f"{base}/proveedores/{tope_dos['pedro']}", json={"activo": True}, headers=owner)
    assert r.status_code == 402


@pytest.mark.asyncio
async def test_plan_sin_tope_de_proveedores(http_client, agenda):
    previo = await fetch_value("SELECT max_proveedores FROM planes WHERE nombre = 'enterprise'")
    assert previo is None  # recién agregado: nadie lo definió todavía
    r = await http_client.post(f"{agenda['base']}/proveedores", json={"nombre": "Luis"}, headers=agenda["owner"])
    assert r.status_code == 201


# ============================================================
# Citas vencidas -> no_asistio
# ============================================================
@pytest.mark.asyncio
async def test_cita_vencida_se_da_por_no_asistio(http_client, agenda):
    pasada = await _reserva(
        agenda["tid"], agenda["juan"], agenda["servicio"], inicio=datetime.now(timezone.utc) - timedelta(hours=3)
    )
    futura = await _reserva(agenda["tid"], agenda["juan"], agenda["servicio"])
    ya_completada = await _reserva(
        agenda["tid"],
        agenda["juan"],
        agenda["servicio"],
        inicio=datetime.now(timezone.utc) - timedelta(hours=6),
        estado="completada",
    )

    assert await calendario_svc.marcar_vencidas_no_asistio() >= 1

    estados = {
        r: await fetch_value("SELECT estado FROM reservas WHERE id = $1", r)
        for r in (pasada, futura, ya_completada)
    }
    assert estados == {pasada: "no_asistio", futura: "confirmada", ya_completada: "completada"}

    bitacora = await fetch_one(
        "SELECT origen, estado_anterior, estado_nuevo FROM reserva_auditoria WHERE reserva_id = $1", pasada
    )
    assert dict(bitacora) == {"origen": "sistema", "estado_anterior": "confirmada", "estado_nuevo": "no_asistio"}

    # Si en realidad sí vino, se corrige a 'completada' con su cobro.
    r = await http_client.patch(
        f"{agenda['base']}/reservas/{pasada}/estado",
        json={"estado": "completada", "precio_cobrado": "150", "metodo_pago": "tarjeta"},
        headers=agenda["juan_h"],
    )
    assert r.status_code == 200, r.text

    # Una segunda pasada no vuelve a tocarla ni a auditarla.
    await calendario_svc.marcar_vencidas_no_asistio()
    assert await fetch_value(
        "SELECT COUNT(*) FROM reserva_auditoria WHERE reserva_id = $1 AND origen = 'sistema'", pasada
    ) == 1


@pytest.mark.asyncio
async def test_la_gracia_deja_en_paz_a_la_cita_recien_terminada(agenda):
    recien = await _reserva(
        agenda["tid"], agenda["juan"], agenda["servicio"], inicio=datetime.now(timezone.utc) - timedelta(minutes=40)
    )
    await calendario_svc.marcar_vencidas_no_asistio(gracia_minutos=60)
    assert await fetch_value("SELECT estado FROM reservas WHERE id = $1", recien) == "confirmada"
