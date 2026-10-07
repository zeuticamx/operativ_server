"""
Agenda de ventas (sql/39_agenda.sql, routers/agenda.py, services/agenda.py).

Dos capas:
  - las reglas puras (estado al reprogramar, mapeo a AgendaItemOut, texto
    de la alerta), sin base
  - los flujos por HTTP contra la base de prueba: arrastrar una tarea o un
    seguimiento a otra fecha, la bitácora que deja, quién puede moverlo y
    a quién se le avisa
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from schemas import AgendaItemOut
from security import crear_access_token, hash_password
from services import agenda
from services.pipeline import set_tenant_servicios
from session import execute, fetch_all, fetch_value

AHORA = datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc)


# ============================================================
# Reglas puras
# ============================================================
@pytest.mark.parametrize(
    "estado, fecha, esperado",
    [
        ("pendiente", AHORA + timedelta(days=1), "pendiente"),
        ("pendiente", AHORA - timedelta(days=1), "vencida"),
        ("vencida", AHORA + timedelta(hours=1), "pendiente"),
        ("vencida", AHORA - timedelta(hours=1), "vencida"),
        # Mover una completada corrige el dato, no la reabre.
        ("completada", AHORA + timedelta(days=1), "completada"),
        ("completada", AHORA - timedelta(days=1), "completada"),
    ],
)
def test_estado_tras_reprogramar(estado, fecha, esperado):
    assert agenda.estado_tras_reprogramar(estado, fecha, AHORA) == esperado


def test_estado_seguimiento_se_deduce_de_la_fecha():
    assert agenda.estado_seguimiento(AHORA + timedelta(minutes=1), AHORA) == "pendiente"
    assert agenda.estado_seguimiento(AHORA - timedelta(minutes=1), AHORA) == "vencida"


def test_fecha_legible_en_la_zona_del_negocio():
    # 18:00 UTC del miércoles 7 = 12:00 en CDMX (UTC-6, sin horario de verano).
    assert agenda.fecha_legible(AHORA, "America/Mexico_City") == "mié 7 oct, 12:00"
    # Cerca de medianoche UTC el día local es otro: por eso no se usa UTC.
    tarde = datetime(2026, 10, 8, 3, 30, tzinfo=timezone.utc)
    assert agenda.fecha_legible(tarde, "America/Mexico_City") == "mié 7 oct, 21:30"


def test_fecha_legible_con_zona_invalida_no_rompe():
    assert agenda.fecha_legible(AHORA, "Marte/Olympus") == "mié 7 oct, 12:00"
    assert agenda.fecha_legible(AHORA, None) == "mié 7 oct, 12:00"


def _fila_seguimiento(**cambios) -> dict:
    fila = {
        "id": uuid4(),
        "user_id": uuid4(),
        "vendedor_id": uuid4(),
        "vendedor_nombre": "Ana",
        "estado": "cotizado",
        "proximo_seguimiento": AHORA + timedelta(days=2),
        "seguimiento_nota": None,
        "cliente_nombre": "Lucía",
        "cliente_handle": "5215550000000",
    }
    fila.update(cambios)
    return fila


def test_item_de_seguimiento_sin_nota_usa_el_nombre_del_lead():
    item = agenda.item_de_seguimiento(_fila_seguimiento(), AHORA)
    assert item.tipo == "seguimiento"
    assert item.titulo == "Seguimiento a Lucía"
    assert item.estado == "pendiente"
    assert item.etapa == "cotizado"


def test_item_de_seguimiento_con_nota_y_sin_nombre():
    fila = _fila_seguimiento(
        seguimiento_nota="Mandar cotización",
        cliente_nombre=None,
        proximo_seguimiento=AHORA - timedelta(hours=1),
    )
    item = agenda.item_de_seguimiento(fila, AHORA)
    assert item.titulo == "Mandar cotización"
    # Sin nombre cae al handle, igual que el resto del módulo.
    assert item.cliente_nombre == "5215550000000"
    assert item.estado == "vencida"


def test_ordenar_por_fecha_y_estable_a_la_misma_hora():
    def item(tipo, horas):
        return AgendaItemOut(
            tipo=tipo,
            id=uuid4(),
            titulo="x",
            descripcion=None,
            fecha=AHORA + timedelta(hours=horas),
            estado="pendiente",
            vendedor_id=None,
            vendedor_nombre=None,
            cliente_id=uuid4(),
            cliente_nombre=None,
        )

    a, b, c = item("tarea", 2), item("seguimiento", 1), item("seguimiento", 2)
    assert [i.id for i in agenda.ordenar([a, b, c])] == [b.id, c.id, a.id]


# ============================================================
# Por HTTP
# ============================================================
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


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _iso(d: datetime) -> str:
    return d.isoformat()


@pytest.fixture
async def negocio(tenant_y_usuario, plan_enterprise):
    """Owner, Ana (vendedora con cuenta), Beto (sin cuenta), un cliente de campo y un lead de cada uno."""
    tid = tenant_y_usuario["tenant_id"]
    await set_tenant_servicios(tid, None, True)

    uid_ana, token_ana = await _cuenta(tid, "vendedor")
    ana = await fetch_value(
        "INSERT INTO vendedores (tenant_id, portal_user_id, nombre) VALUES ($1, $2, 'Ana') RETURNING id",
        tid,
        uid_ana,
    )
    beto = await fetch_value(
        "INSERT INTO vendedores (tenant_id, nombre) VALUES ($1, 'Beto') RETURNING id", tid
    )
    cliente = await fetch_value(
        """
        INSERT INTO clientes (tenant_id, vendedor_id, nombre_negocio, latitud, longitud)
        VALUES ($1, $2, 'Abarrotes La Esquina', 19.43, -99.13)
        RETURNING id
        """,
        tid,
        ana,
    )

    async def lead(vendedor_id, estado="contactado"):
        user_id = await fetch_value(
            "INSERT INTO users (tenant_id, display_name) VALUES ($1, 'Lucía') RETURNING id", tid
        )
        await execute(
            "INSERT INTO client_pipeline (tenant_id, user_id, vendedor_id, estado) VALUES ($1, $2, $3, $4)",
            tid,
            user_id,
            vendedor_id,
            estado,
        )
        return user_id

    _, token_proveedor = await _cuenta(tid, "proveedor")

    return {
        "tid": tid,
        "owner": tenant_y_usuario["token"],
        "ana": ana,
        "uid_ana": uid_ana,
        "token_ana": token_ana,
        "beto": beto,
        "cliente": cliente,
        "lead_ana": await lead(ana),
        "lead_beto": await lead(beto),
        "lead_cerrado": await lead(ana, "ganado"),
        "proveedor": token_proveedor,
    }


async def _tarea(http_client, negocio, fecha: datetime) -> dict:
    r = await http_client.post(
        "/api/tareas",
        json={
            "cliente_id": str(negocio["cliente"]),
            "titulo": "Llevar muestras",
            "fecha_programada": _iso(fecha),
            "vendedor_id": str(negocio["ana"]),
        },
        headers=_h(negocio["owner"]),
    )
    assert r.status_code == 201, r.text
    return r.json()


def _rango(centro: datetime) -> str:
    desde = (centro - timedelta(days=20)).isoformat()
    hasta = (centro + timedelta(days=20)).isoformat()
    return f"desde={desde.replace('+', '%2B')}&hasta={hasta.replace('+', '%2B')}"


async def test_arrastrar_tarea_vencida_al_futuro_la_reabre_y_deja_bitacora(http_client, negocio):
    ahora = datetime.now(timezone.utc)
    tarea = await _tarea(http_client, negocio, ahora - timedelta(days=1))
    await agenda.marcar_tareas_vencidas()

    nueva = (ahora + timedelta(days=3)).replace(microsecond=0)
    r = await http_client.put(
        f"/api/tareas/{tarea['id']}",
        json={"fecha_programada": _iso(nueva)},
        headers=_h(negocio["owner"]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "pendiente"

    hist = await http_client.get(
        f"/api/tareas/{tarea['id']}/reprogramaciones", headers=_h(negocio["owner"])
    )
    assert hist.status_code == 200
    assert len(hist.json()) == 1
    assert hist.json()[0]["vendedor_nombre"] == "Ana"
    assert datetime.fromisoformat(hist.json()[0]["fecha_nueva"]) == nueva


async def test_editar_solo_el_titulo_no_es_reprogramar(http_client, negocio):
    tarea = await _tarea(http_client, negocio, datetime.now(timezone.utc) + timedelta(days=1))
    r = await http_client.put(
        f"/api/tareas/{tarea['id']}",
        json={"titulo": "Llevar muestras nuevas", "fecha_programada": tarea["fecha_programada"]},
        headers=_h(negocio["owner"]),
    )
    assert r.status_code == 200
    filas = await fetch_all(
        "SELECT id FROM agenda_reprogramaciones WHERE tarea_id = $1", UUID(tarea["id"])
    )
    assert filas == []


async def test_job_marca_vencidas_solo_las_pendientes_pasadas(http_client, negocio):
    ahora = datetime.now(timezone.utc)
    pasada = await _tarea(http_client, negocio, ahora - timedelta(hours=2))
    futura = await _tarea(http_client, negocio, ahora + timedelta(hours=2))

    await agenda.marcar_tareas_vencidas()

    estados = {
        str(f["id"]): f["estado"]
        for f in await fetch_all(
            "SELECT id, estado FROM tareas_seguimiento WHERE tenant_id = $1", negocio["tid"]
        )
    }
    assert estados[pasada["id"]] == "vencida"
    assert estados[futura["id"]] == "pendiente"


async def test_gerencia_mueve_la_tarea_de_ana_y_a_ella_le_llega_un_aviso_personal(http_client, negocio):
    tarea = await _tarea(http_client, negocio, datetime.now(timezone.utc) + timedelta(days=1))
    # La alta ya avisó ("tienes un pendiente nuevo"); se cuenta desde acá.
    await execute("DELETE FROM alertas WHERE tenant_id = $1", negocio["tid"])

    await http_client.put(
        f"/api/tareas/{tarea['id']}",
        json={"fecha_programada": _iso(datetime.now(timezone.utc) + timedelta(days=5))},
        headers=_h(negocio["owner"]),
    )

    alertas = await fetch_all(
        "SELECT tipo, portal_user_id, mensaje FROM alertas WHERE tenant_id = $1", negocio["tid"]
    )
    assert len(alertas) == 1
    assert alertas[0]["tipo"] == "agenda_reprogramada"
    assert alertas[0]["portal_user_id"] == negocio["uid_ana"]
    assert "Llevar muestras" in alertas[0]["mensaje"]


async def test_ana_mueve_su_propia_tarea_sin_avisarse_a_si_misma(http_client, negocio):
    tarea = await _tarea(http_client, negocio, datetime.now(timezone.utc) + timedelta(days=1))
    await execute("DELETE FROM alertas WHERE tenant_id = $1", negocio["tid"])

    r = await http_client.put(
        f"/api/tareas/{tarea['id']}",
        json={"fecha_programada": _iso(datetime.now(timezone.utc) + timedelta(days=2))},
        headers=_h(negocio["token_ana"]),
    )
    assert r.status_code == 200, r.text
    assert await fetch_value("SELECT COUNT(*) FROM alertas WHERE tenant_id = $1", negocio["tid"]) == 0


async def test_agendar_mover_y_quitar_un_seguimiento(http_client, negocio):
    owner = _h(negocio["owner"])
    lead = negocio["lead_ana"]
    primera = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
    segunda = primera + timedelta(days=2)

    r = await http_client.put(
        f"/api/agenda/seguimientos/{lead}",
        json={"fecha": _iso(primera), "nota": "Mandar cotización"},
        headers=owner,
    )
    assert r.status_code == 200, r.text
    assert r.json()["titulo"] == "Mandar cotización"
    assert r.json()["tipo"] == "seguimiento"

    # Sin `nota`, la que había se conserva.
    r = await http_client.put(
        f"/api/agenda/seguimientos/{lead}", json={"fecha": _iso(segunda)}, headers=owner
    )
    assert r.json()["titulo"] == "Mandar cotización"

    r = await http_client.put(f"/api/agenda/seguimientos/{lead}", json={"fecha": None}, headers=owner)
    assert r.status_code == 200
    assert r.json() is None
    nota = await fetch_value("SELECT seguimiento_nota FROM client_pipeline WHERE user_id = $1", lead)
    assert nota is None

    hist = await http_client.get(f"/api/agenda/seguimientos/{lead}/reprogramaciones", headers=owner)
    pasos = [(h["fecha_anterior"] is None, h["fecha_nueva"] is None) for h in hist.json()]
    # Más reciente primero: quitar, mover, agendar.
    assert pasos == [(False, True), (False, False), (True, False)]


async def test_agendar_no_toca_actualizado_en_del_embudo(http_client, negocio):
    lead = negocio["lead_ana"]
    antes = await fetch_value("SELECT actualizado_en FROM client_pipeline WHERE user_id = $1", lead)
    await http_client.put(
        f"/api/agenda/seguimientos/{lead}",
        json={"fecha": _iso(datetime.now(timezone.utc) + timedelta(days=1))},
        headers=_h(negocio["owner"]),
    )
    despues = await fetch_value("SELECT actualizado_en FROM client_pipeline WHERE user_id = $1", lead)
    assert antes == despues


async def test_lead_cerrado_no_se_agenda(http_client, negocio):
    r = await http_client.put(
        f"/api/agenda/seguimientos/{negocio['lead_cerrado']}",
        json={"fecha": _iso(datetime.now(timezone.utc) + timedelta(days=1))},
        headers=_h(negocio["owner"]),
    )
    assert r.status_code == 409


async def test_vendedor_solo_agenda_sus_leads(http_client, negocio):
    ana = _h(negocio["token_ana"])
    fecha = {"fecha": _iso(datetime.now(timezone.utc) + timedelta(days=1))}

    suyo = await http_client.put(f"/api/agenda/seguimientos/{negocio['lead_ana']}", json=fecha, headers=ana)
    assert suyo.status_code == 200, suyo.text

    ajeno = await http_client.put(f"/api/agenda/seguimientos/{negocio['lead_beto']}", json=fecha, headers=ana)
    assert ajeno.status_code == 403
    hist = await http_client.get(
        f"/api/agenda/seguimientos/{negocio['lead_beto']}/reprogramaciones", headers=ana
    )
    assert hist.status_code == 403


async def test_lead_de_otro_negocio_es_404(http_client, negocio):
    r = await http_client.put(
        f"/api/agenda/seguimientos/{uuid4()}",
        json={"fecha": None},
        headers=_h(negocio["owner"]),
    )
    assert r.status_code == 404


async def test_listar_junta_tareas_y_seguimientos_y_filtra_por_vendedor(http_client, negocio):
    ahora = datetime.now(timezone.utc)
    owner = _h(negocio["owner"])
    await _tarea(http_client, negocio, ahora + timedelta(days=1))
    for lead in (negocio["lead_ana"], negocio["lead_beto"]):
        await http_client.put(
            f"/api/agenda/seguimientos/{lead}",
            json={"fecha": _iso(ahora + timedelta(days=2))},
            headers=owner,
        )

    r = await http_client.get(f"/api/agenda?{_rango(ahora)}", headers=owner)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["tareas_disponibles"] and cuerpo["seguimientos_disponibles"]
    assert sorted(i["tipo"] for i in cuerpo["items"]) == ["seguimiento", "seguimiento", "tarea"]

    # El vendedor ve lo suyo aunque pida el de Beto.
    r = await http_client.get(
        f"/api/agenda?{_rango(ahora)}&vendedor_id={negocio['beto']}", headers=_h(negocio["token_ana"])
    )
    assert {i["vendedor_nombre"] for i in r.json()["items"]} == {"Ana"}


async def test_listar_sin_embudo_encendido_solo_trae_tareas(http_client, negocio):
    ahora = datetime.now(timezone.utc)
    await http_client.put(
        f"/api/agenda/seguimientos/{negocio['lead_ana']}",
        json={"fecha": _iso(ahora + timedelta(days=1))},
        headers=_h(negocio["owner"]),
    )
    await set_tenant_servicios(negocio["tid"], None, False)

    r = await http_client.get(f"/api/agenda?{_rango(ahora)}", headers=_h(negocio["owner"]))
    assert r.status_code == 200
    assert r.json()["seguimientos_disponibles"] is False
    assert all(i["tipo"] == "tarea" for i in r.json()["items"])


async def test_rango_invalido(http_client, negocio):
    owner = _h(negocio["owner"])
    ahora = datetime.now(timezone.utc)
    al_reves = f"desde={_iso(ahora + timedelta(days=1)).replace('+', '%2B')}&hasta={_iso(ahora).replace('+', '%2B')}"
    assert (await http_client.get(f"/api/agenda?{al_reves}", headers=owner)).status_code == 400

    largo = f"desde={_iso(ahora).replace('+', '%2B')}&hasta={_iso(ahora + timedelta(days=200)).replace('+', '%2B')}"
    assert (await http_client.get(f"/api/agenda?{largo}", headers=owner)).status_code == 400


async def test_proveedor_no_entra_al_crm(http_client, negocio):
    proveedor = _h(negocio["proveedor"])
    ahora = datetime.now(timezone.utc)
    assert (await http_client.get(f"/api/agenda?{_rango(ahora)}", headers=proveedor)).status_code == 403
    # El mismo candado cubre el resto del CRM de campo (services/crm.acceso_crm).
    assert (await http_client.get("/api/tareas", headers=proveedor)).status_code == 403
    assert (await http_client.get("/api/clientes", headers=proveedor)).status_code == 403
