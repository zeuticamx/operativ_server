"""
Asignación de conversaciones (sql/41_conversaciones_asignacion.sql,
services/asignacion_conversaciones.py).

    owner / superadmin  ven todo, asignan, reasignan y quitan
    quien la toma       se la queda; solo el owner se la quita
    vendedor/proveedor  ven lo asignado a ellos (+ lo de su ficha mientras
                        nadie lo tenga)
    agente de n8n       sugiere un `area`; el backend decide, y si no hay a
                        quién, cae en el owner con una nota

Las decisiones del agente (`elegir_destino`) son puras y se prueban sin BD;
el resto, por HTTP contra la base local.
"""

from uuid import uuid4

import pytest

from routers import eventos
from schemas import MensajeConversacionAsignadaIn
from security import crear_access_token, hash_password
from services import asignacion_conversaciones as asig
from services import conversaciones as conversaciones_svc
from services.asignacion_conversaciones import elegir_destino
from services.pipeline import set_tenant_servicios
from session import execute, fetch_one, fetch_value

pytestmark = pytest.mark.usefixtures("plan_enterprise")


# ============================================================
# Decisión del agente (pura)
# ============================================================
A, B, C = uuid4(), uuid4(), uuid4()


def test_agenda_va_al_proveedor_de_la_cita():
    r = elegir_destino("agenda", proveedor=A, vendedor_lead=B, vendedor_reparto=C)
    assert (r.portal_user_id, r.fallo) == (A, False)


def test_agenda_sin_proveedor_es_un_fallo():
    r = elegir_destino("agenda", proveedor=None, vendedor_lead=B, vendedor_reparto=C)
    assert r.portal_user_id is None and r.fallo is True


def test_ventas_prefiere_el_vendedor_del_lead_al_del_reparto():
    assert elegir_destino("ventas", None, B, C).portal_user_id == B
    assert elegir_destino("ventas", None, None, C).portal_user_id == C


def test_ventas_sin_nadie_es_un_fallo():
    r = elegir_destino("ventas", proveedor=A, vendedor_lead=None, vendedor_reparto=None)
    # Tener un proveedor no sirve para 'ventas': cada área mira lo suyo.
    assert r.portal_user_id is None and r.fallo is True


@pytest.mark.parametrize("area", ["otro", "inventada", ""])
def test_otro_o_desconocido_no_asigna_a_nadie_y_no_es_fallo(area):
    r = elegir_destino(area, A, B, C)
    assert r.portal_user_id is None and r.fallo is False


# ============================================================
# Helpers
# ============================================================
def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _cuenta(tenant_id, role: str, activo=True):
    uid = uuid4()
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        uid,
        tenant_id,
        f"{role}-{uid}@ejemplo.com",
        hash_password("test_password"),
        role,
        activo,
    )
    return uid, crear_access_token(uid, tenant_id, role)


async def _vendedor(tenant_id, nombre, portal_user_id=None, activo=True):
    return await fetch_value(
        "INSERT INTO vendedores (tenant_id, portal_user_id, nombre, activo) VALUES ($1, $2, $3, $4) RETURNING id",
        tenant_id,
        portal_user_id,
        nombre,
        activo,
    )


async def _conversacion(tenant_id, estado="transferred", vendedor_id=None):
    """(user_id, conversation_id); con `vendedor_id`, el cliente es lead de ese vendedor."""
    user_id = await fetch_value(
        "INSERT INTO users (tenant_id, display_name) VALUES ($1, 'Cliente') RETURNING id", tenant_id
    )
    if vendedor_id is not None:
        await execute(
            "INSERT INTO client_pipeline (tenant_id, user_id, vendedor_id) VALUES ($1, $2, $3)",
            tenant_id,
            user_id,
            vendedor_id,
        )
    conv_id = await fetch_value(
        "INSERT INTO conversations (tenant_id, user_id, channel_type, status) "
        "VALUES ($1, $2, 'whatsapp', $3) RETURNING id",
        tenant_id,
        user_id,
        estado,
    )
    return user_id, conv_id


async def _quien_la_tiene(conv_id):
    return await fetch_value("SELECT asignado_a FROM conversations WHERE id = $1", conv_id)


@pytest.fixture(autouse=True)
async def columna_metadata(db):
    """
    `conversations.metadata` es de n8n y la base local de pruebas no la trae
    (00_base_local.sql); tomar / asignar / volver-a-ia la escriben. Se agrega
    igual que existe en la base real. IF NOT EXISTS: no pisa nada.
    """
    await execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb")


@pytest.fixture
async def equipo(tenant_y_usuario):
    """Owner + 2 members + 2 vendedores y 1 proveedor con cuenta, módulo de vendedores encendido."""
    tid = tenant_y_usuario["tenant_id"]
    await set_tenant_servicios(tid, None, True)

    m1, t_m1 = await _cuenta(tid, "member")
    m2, t_m2 = await _cuenta(tid, "member")
    u_v1, t_v1 = await _cuenta(tid, "vendedor")
    u_v2, t_v2 = await _cuenta(tid, "vendedor")
    v1 = await _vendedor(tid, "Ana", u_v1)
    v2 = await _vendedor(tid, "Beto", u_v2)
    u_p, t_p = await _cuenta(tid, "proveedor")
    prov = await fetch_value(
        "INSERT INTO proveedores (tenant_id, nombre, portal_user_id) VALUES ($1, 'Juan', $2) RETURNING id",
        tid,
        u_p,
    )
    return {
        "tid": tid,
        "owner": _h(tenant_y_usuario["token"]),
        "owner_id": tenant_y_usuario["usuario_id"],
        "m1": m1, "m1_h": _h(t_m1),
        "m2": m2, "m2_h": _h(t_m2),
        "u_v1": u_v1, "v1": v1, "v1_h": _h(t_v1),
        "u_v2": u_v2, "v2": v2, "v2_h": _h(t_v2),
        "u_p": u_p, "prov": prov, "p_h": _h(t_p),
    }


# ============================================================
# El owner asigna; todos los demás ven lo que les toca
# ============================================================
async def test_owner_asigna_y_el_vendedor_la_ve(http_client, equipo):
    _, conv = await _conversacion(equipo["tid"], estado="active")

    r = await http_client.put(
        f"/api/conversaciones/{conv}/asignacion",
        json={"asignado_a": str(equipo["u_v1"]), "nota": "Cliente de mayoreo"},
        headers=equipo["owner"],
    )
    assert r.status_code == 200, r.text
    # Asignar apaga la IA.
    assert r.json()["status"] == "transferred"
    assert await _quien_la_tiene(conv) == equipo["u_v1"]

    r = await http_client.get("/api/conversaciones", headers=equipo["v1_h"])
    assert [c["id"] for c in r.json()] == [str(conv)]
    assert r.json()[0]["asignado_nombre"] == f"vendedor-{equipo['u_v1']}@ejemplo.com"
    # (El detalle con mensajes lee columnas de la migración 22 que la base local
    # de pruebas no trae: acá se prueba la guarda, con es_visible.)
    assert await conversaciones_svc.es_visible(
        equipo["tid"], conv, conversaciones_svc.Alcance(equipo["u_v1"], vendedor_id=equipo["v1"])
    )

    # El otro vendedor no la ve: ni en la lista ni abriéndola.
    assert (await http_client.get("/api/conversaciones", headers=equipo["v2_h"])).json() == []
    assert (await http_client.get(f"/api/conversaciones/{conv}", headers=equipo["v2_h"])).status_code == 403

    # El owner ve todo, incluido quién la tiene.
    r = await http_client.get("/api/conversaciones", headers=equipo["owner"])
    fila, = [c for c in r.json() if c["id"] == str(conv)]
    assert fila["asignado_a"] == str(equipo["u_v1"]) and fila["asignado_origen"] == "owner"

    historial = await fetch_one(
        "SELECT origen, a_portal_user_id FROM conversacion_asignaciones WHERE conversation_id = $1", conv
    )
    assert (historial["origen"], historial["a_portal_user_id"]) == ("owner", equipo["u_v1"])


async def test_solo_la_gerencia_asigna(http_client, equipo):
    _, conv = await _conversacion(equipo["tid"])
    cuerpo = {"asignado_a": str(equipo["u_v1"])}

    for h in (equipo["m1_h"], equipo["v1_h"], equipo["p_h"]):
        r = await http_client.put(f"/api/conversaciones/{conv}/asignacion", json=cuerpo, headers=h)
        assert r.status_code == 403, r.text
        assert (await http_client.get("/api/conversaciones/asignables", headers=h)).status_code == 403
    assert await _quien_la_tiene(conv) is None

    r = await http_client.get("/api/conversaciones/asignables", headers=equipo["owner"])
    assert r.status_code == 200
    assert {a["role"] for a in r.json()} == {"owner", "member", "vendedor", "proveedor"}


async def test_no_se_asigna_a_quien_no_puede_recibirla(http_client, equipo, tenant_y_usuario):
    tid = equipo["tid"]
    _, conv = await _conversacion(tid)
    url = f"/api/conversaciones/{conv}/asignacion"

    # De otro negocio: 404, no se confirma que existe.
    otro_tenant = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro')", otro_tenant)
    try:
        ajeno, _ = await _cuenta(otro_tenant, "member")
        r = await http_client.put(url, json={"asignado_a": str(ajeno)}, headers=equipo["owner"])
        assert r.status_code == 404
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", otro_tenant)

    # Cuenta inactiva: para quien asigna es como si no existiera.
    inactiva, _ = await _cuenta(tid, "member", activo=False)
    assert (await http_client.put(url, json={"asignado_a": str(inactiva)}, headers=equipo["owner"])).status_code == 404

    # Vendedor con cuenta pero ficha desactivada: 422.
    await execute("UPDATE vendedores SET activo = false WHERE id = $1", equipo["v2"])
    r = await http_client.put(url, json={"asignado_a": str(equipo["u_v2"])}, headers=equipo["owner"])
    assert r.status_code == 422, r.text
    assert await _quien_la_tiene(conv) is None


# ============================================================
# Tomar: se la queda, y solo el owner se la quita
# ============================================================
async def test_tomar_se_la_queda_y_otro_no_puede_quitarsela(http_client, equipo):
    _, conv = await _conversacion(equipo["tid"], estado="active")

    r = await http_client.post(f"/api/conversaciones/{conv}/tomar", headers=equipo["m1_h"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "transferred" and r.json()["asignado_a"] == str(equipo["m1"])

    # Tomarla otra vez es idempotente para el mismo.
    assert (await http_client.post(f"/api/conversaciones/{conv}/tomar", headers=equipo["m1_h"])).status_code == 200

    # Otro member (ve todo) no se la puede quedar, ni siquiera el owner por esta vía.
    for h in (equipo["m2_h"], equipo["owner"]):
        r = await http_client.post(f"/api/conversaciones/{conv}/tomar", headers=h)
        assert r.status_code == 409, r.text
    assert await _quien_la_tiene(conv) == equipo["m1"]

    # Solo el owner la reasigna.
    r = await http_client.put(
        f"/api/conversaciones/{conv}/asignacion", json={"asignado_a": str(equipo["m2"])}, headers=equipo["owner"]
    )
    assert r.status_code == 200
    assert await _quien_la_tiene(conv) == equipo["m2"]


async def test_tomar_una_ya_transferida_por_el_agente_sin_asignar(http_client, equipo):
    _, conv = await _conversacion(equipo["tid"], estado="transferred")
    r = await http_client.post(f"/api/conversaciones/{conv}/tomar", headers=equipo["m1_h"])
    assert r.status_code == 200, r.text
    assert await _quien_la_tiene(conv) == equipo["m1"]


async def test_solo_el_asignado_o_el_owner_contestan_y_devuelven_a_la_ia(http_client, equipo):
    _, conv = await _conversacion(equipo["tid"])
    await http_client.post(f"/api/conversaciones/{conv}/tomar", headers=equipo["m1_h"])

    # El otro member la ve pero no puede contestar ni devolverla a la IA.
    otro = equipo["m2_h"]
    r = await http_client.post(f"/api/conversaciones/{conv}/mensajes", json={"texto": "hola"}, headers=otro)
    assert r.status_code == 403 and "la tiene" in r.json()["detail"]
    assert (await http_client.post(f"/api/conversaciones/{conv}/volver-a-ia", headers=otro)).status_code == 403
    assert await _quien_la_tiene(conv) == equipo["m1"]

    # El asignado la devuelve a la IA: termina la asignación y queda en el historial.
    r = await http_client.post(f"/api/conversaciones/{conv}/volver-a-ia", headers=equipo["m1_h"])
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert await _quien_la_tiene(conv) is None
    assert await fetch_value(
        "SELECT COUNT(*) FROM conversacion_asignaciones WHERE conversation_id = $1 AND a_portal_user_id IS NULL",
        conv,
    ) == 1


async def test_soltar(http_client, equipo):
    _, conv = await _conversacion(equipo["tid"])
    await http_client.post(f"/api/conversaciones/{conv}/tomar", headers=equipo["m1_h"])
    url = f"/api/conversaciones/{conv}/asignacion"

    # Otro no puede soltar la de alguien más; el owner sí; y el propio asignado la suya.
    assert (await http_client.delete(url, headers=equipo["m2_h"])).status_code == 403
    r = await http_client.delete(url, headers=equipo["owner"])
    assert r.status_code == 200 and r.json()["status"] == "transferred"
    assert await _quien_la_tiene(conv) is None

    await http_client.post(f"/api/conversaciones/{conv}/tomar", headers=equipo["m1_h"])
    assert (await http_client.delete(url, headers=equipo["m1_h"])).status_code == 200
    # Sin asignar no hay nada que soltar.
    assert (await http_client.delete(url, headers=equipo["m1_h"])).status_code == 409


async def test_conversacion_de_otro_negocio_es_404(http_client, equipo):
    otro_tenant = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro')", otro_tenant)
    try:
        _, ajena = await _conversacion(otro_tenant)
        for metodo, ruta in (("post", "tomar"), ("delete", "asignacion")):
            r = await getattr(http_client, metodo)(f"/api/conversaciones/{ajena}/{ruta}", headers=equipo["owner"])
            assert r.status_code == 404, (ruta, r.text)
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", otro_tenant)


# ============================================================
# Qué ve un rol acotado mientras nadie la tiene
# ============================================================
async def test_el_vendedor_ve_los_leads_suyos_sin_asignar_y_los_pierde_al_asignarse_a_otro(http_client, equipo):
    _, suya = await _conversacion(equipo["tid"], vendedor_id=equipo["v1"])
    _, de_beto = await _conversacion(equipo["tid"], vendedor_id=equipo["v2"])
    _, sin_lead = await _conversacion(equipo["tid"])

    r = await http_client.get("/api/conversaciones", headers=equipo["v1_h"])
    assert {c["id"] for c in r.json()} == {str(suya)}
    assert (await http_client.get(f"/api/conversaciones/{sin_lead}", headers=equipo["v1_h"])).status_code == 403
    assert (await http_client.get(f"/api/conversaciones/{uuid4()}", headers=equipo["v1_h"])).status_code == 404

    # Puede tomar la de su lead; no la ajena.
    assert (await http_client.post(f"/api/conversaciones/{suya}/tomar", headers=equipo["v1_h"])).status_code == 200
    assert (await http_client.post(f"/api/conversaciones/{de_beto}/tomar", headers=equipo["v1_h"])).status_code == 403

    # Si el owner se la da a otro, deja de ser suya aunque el lead siga siéndolo.
    await http_client.put(
        f"/api/conversaciones/{suya}/asignacion", json={"asignado_a": str(equipo["m1"])}, headers=equipo["owner"]
    )
    assert (await http_client.get("/api/conversaciones", headers=equipo["v1_h"])).json() == []


async def test_filtros_de_la_lista(http_client, equipo):
    _, a = await _conversacion(equipo["tid"])
    _, b = await _conversacion(equipo["tid"])
    await http_client.post(f"/api/conversaciones/{a}/tomar", headers=equipo["m1_h"])

    async def ids(filtro, headers):
        r = await http_client.get(f"/api/conversaciones?asignada={filtro}", headers=headers)
        assert r.status_code == 200, r.text
        return {c["id"] for c in r.json()}

    assert await ids("mias", equipo["m1_h"]) == {str(a)}
    assert await ids("mias", equipo["m2_h"]) == set()
    assert await ids("sin_asignar", equipo["owner"]) == {str(b)}
    assert await ids(str(equipo["m1"]), equipo["owner"]) == {str(a)}
    assert (await http_client.get("/api/conversaciones?asignada=cualquier-cosa", headers=equipo["owner"])).status_code == 422


# ============================================================
# Asignación del agente de n8n
# ============================================================
async def test_autoasigna_al_vendedor_del_lead(equipo):
    _, conv = await _conversacion(equipo["tid"], vendedor_id=equipo["v1"])

    r = await asig.autoasignar(equipo["tid"], conv, "ventas")

    assert r is not None and r.portal_user_id == equipo["u_v1"] and not r.cayo_en_owner
    assert await _quien_la_tiene(conv) == equipo["u_v1"]
    assert await fetch_value("SELECT asignado_origen FROM conversations WHERE id = $1", conv) == "agente"
    # Reintento de n8n (o un humano que se adelantó): no pisa nada.
    assert await asig.autoasignar(equipo["tid"], conv, "ventas") is None


async def test_ventas_sin_lead_usa_el_reparto(equipo):
    _, conv = await _conversacion(equipo["tid"])
    r = await asig.autoasignar(equipo["tid"], conv, "ventas")
    assert r is not None and r.portal_user_id in (equipo["u_v1"], equipo["u_v2"])


async def test_sin_responsable_cae_en_el_owner_con_nota(equipo):
    # Área 'agenda' sin cita con un proveedor: el área pedía alguien concreto y no hay.
    _, conv = await _conversacion(equipo["tid"])
    r = await asig.autoasignar(equipo["tid"], conv, "agenda")
    assert r is not None and r.cayo_en_owner and r.fallo
    assert await _quien_la_tiene(conv) == equipo["owner_id"]
    nota = await fetch_value("SELECT asignacion_nota FROM conversations WHERE id = $1", conv)
    assert "No se pudo asignar automáticamente" in nota

    # 'otro' o un valor inventado: al owner, pero no es un fallo.
    _, conv2 = await _conversacion(equipo["tid"])
    r = await asig.autoasignar(equipo["tid"], conv2, "lo-que-sea")
    assert r is not None and r.cayo_en_owner and not r.fallo
    assert await _quien_la_tiene(conv2) == equipo["owner_id"]


async def test_ventas_sin_modulo_de_vendedores_cae_en_el_owner(equipo):
    await set_tenant_servicios(equipo["tid"], None, False)
    _, conv = await _conversacion(equipo["tid"], vendedor_id=equipo["v1"])
    r = await asig.autoasignar(equipo["tid"], conv, "ventas")
    assert r is not None and r.cayo_en_owner and r.fallo


async def test_no_autoasigna_una_conversacion_que_no_esta_transferida(equipo):
    _, conv = await _conversacion(equipo["tid"], estado="active")
    assert await asig.autoasignar(equipo["tid"], conv, "ventas") is None
    assert await asig.autoasignar(equipo["tid"], uuid4(), "ventas") is None


# ============================================================
# Quien se queda sin acceso no se lleva las conversaciones
# ============================================================
async def test_desactivar_a_un_miembro_devuelve_sus_conversaciones_al_owner(http_client, equipo):
    _, conv = await _conversacion(equipo["tid"])
    await http_client.post(f"/api/conversaciones/{conv}/tomar", headers=equipo["m1_h"])

    r = await http_client.patch(
        f"/api/equipo/usuarios/{equipo['m1']}", json={"activo": False}, headers=equipo["owner"]
    )
    assert r.status_code == 200, r.text
    assert await _quien_la_tiene(conv) == equipo["owner_id"]
    assert "ya no puede atenderla" in await fetch_value(
        "SELECT asignacion_nota FROM conversations WHERE id = $1", conv
    )


async def test_desactivar_la_ficha_del_vendedor_tambien(http_client, equipo):
    _, conv = await _conversacion(equipo["tid"], vendedor_id=equipo["v1"])
    await http_client.post(f"/api/conversaciones/{conv}/tomar", headers=equipo["v1_h"])

    r = await http_client.patch(
        f"/api/vendedores/{equipo['v1']}", json={"activo": False}, headers=equipo["owner"]
    )
    assert r.status_code == 200, r.text
    assert await _quien_la_tiene(conv) == equipo["owner_id"]


# ============================================================
# Aviso por cada mensaje del cliente (n8n)
# ============================================================
async def test_avisa_al_asignado_un_mensaje_por_conversacion_hasta_que_lo_lea(equipo):
    _, conv = await _conversacion(equipo["tid"])
    datos = MensajeConversacionAsignadaIn(
        tenant_id=equipo["tid"], conversation_id=conv, cliente_nombre="Ana", texto="¿sigue ahí?"
    )

    # Sin asignado no hay a quién avisar, y no es un error.
    assert (await eventos.mensaje_conversacion_asignada(datos)).avisado is False

    await asig.asignar(equipo["tid"], conv, equipo["m1"], equipo["owner_id"])
    assert (await eventos.mensaje_conversacion_asignada(datos)).avisado is True
    # Cinco mensajes seguidos no llenan de alertas: queda un aviso sin leer.
    assert (await eventos.mensaje_conversacion_asignada(datos)).avisado is False

    await execute(
        "UPDATE alertas SET leido = true WHERE portal_user_id = $1 AND tipo = 'mensaje_conversacion_asignada'",
        equipo["m1"],
    )
    assert (await eventos.mensaje_conversacion_asignada(datos)).avisado is True
    # Y la alerta es personal: solo la ve quien la tiene.
    assert await fetch_value(
        "SELECT COUNT(*) FROM alertas WHERE tenant_id = $1 AND tipo = 'mensaje_conversacion_asignada' "
        "AND portal_user_id IS DISTINCT FROM $2",
        equipo["tid"],
        equipo["m1"],
    ) == 0
