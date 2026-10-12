"""
Tests con DB del historial de citas de un cliente (spec 002).

Convención de nombres (tasks.md): la consulta de datos usa el prefijo
`test_servicio_`; los del endpoint del portal `test_portal_` y los del agente
`test_agente_` (tareas 3 y 4). Ningún otro nombre de este archivo contiene esos
prefijos.

La regla pura (elegibilidad, orden, tope) ya está cubierta sin DB en
test_historial_citas.py; aquí se prueba lo que solo se ve con una base real:
la deducción del cliente desde la conversación, el aislamiento por negocio y
canal, el filtro de proveedor y la zona vigente.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

import schemas
from services import calendario as cal
from security import crear_access_token, hash_password
from services.pipeline import set_tenant_servicios
from session import execute, fetch_one, fetch_value

pytestmark = pytest.mark.usefixtures("plan_enterprise")

AHORA = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)

# Campos que NUNCA pueden salir de la consulta (ni al portal ni al agente).
PROHIBIDOS_SIEMPRE = {
    "id", "reserva_id", "precio", "precio_cobrado", "metodo_pago", "pago",
    "motivo_cancelacion", "motivo", "cliente_telefono", "cliente_nombre", "tenant_id",
}


# ------------------------------------------------------------
# Ayudas de datos
# ------------------------------------------------------------
async def _proveedor(tenant_id, nombre="Juan") -> UUID:
    return await fetch_value(
        "INSERT INTO proveedores (tenant_id, nombre) VALUES ($1, $2) RETURNING id", tenant_id, nombre
    )


async def _servicio(tenant_id, nombre="Corte") -> UUID:
    return await fetch_value(
        "INSERT INTO servicios (tenant_id, nombre, duracion_minutos) VALUES ($1, $2, 30) RETURNING id",
        tenant_id,
        nombre,
    )


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


async def _cita(
    tenant_id,
    proveedor_id,
    servicio_id,
    inicio: datetime,
    *,
    user_id=None,
    estado="completada",
    telefono=None,
    notas=None,
    motivo=None,
    cancelada_por=None,
    tardia=False,
    creado_en=None,
) -> UUID:
    return await fetch_value(
        """
        INSERT INTO reservas (tenant_id, proveedor_id, servicio_id, user_id, cliente_nombre,
                              cliente_telefono, hora_inicio, hora_fin, estado, notas,
                              motivo_cancelacion, cancelada_por, cancelacion_tardia,
                              precio_cobrado, metodo_pago, creado_en)
        VALUES ($1, $2, $3, $4, 'Cliente', $5, $6, $7, $8, $9, $10, $11, $12,
                99.50, 'efectivo', COALESCE($13, now()))
        RETURNING id
        """,
        tenant_id,
        proveedor_id,
        servicio_id,
        user_id,
        telefono,
        inicio,
        inicio + timedelta(minutes=30),
        estado,
        notas,
        motivo,
        cancelada_por,
        tardia,
        creado_en,
    )


async def _doce_citas(tenant_id, proveedor_id, servicio_id, user_id, n=12) -> None:
    """n citas completadas, una por día hacia atrás desde AHORA (día 1 = la más reciente)."""
    for i in range(1, n + 1):
        await _cita(tenant_id, proveedor_id, servicio_id, AHORA - timedelta(days=i), user_id=user_id)


async def _consultar(tenant_id, conv_id, **kw):
    return await cal.historial_citas_de_conversacion(tenant_id, conv_id, ahora=AHORA, **kw)


# ------------------------------------------------------------
# Tope y hay_mas
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_servicio_doce_citas_devuelve_diez_con_hay_mas(tenant_y_usuario):
    t = tenant_y_usuario["tenant_id"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _doce_citas(t, prov, serv, user_id, 12)

    r = await _consultar(t, conv)

    assert len(r["citas"]) == 10
    assert r["hay_mas"] is True
    # Descendente: la más reciente (hace 1 día) primero, la décima (hace 10 días) última.
    assert r["citas"][0]["fecha_hora_inicio"].startswith("2026-01-09T06:00:00-06:00")
    assert r["citas"][-1]["fecha_hora_inicio"].startswith("2025-12-31T06:00:00-06:00")


@pytest.mark.asyncio
async def test_servicio_diez_citas_exactas_sin_hay_mas(tenant_y_usuario):
    t = tenant_y_usuario["tenant_id"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _doce_citas(t, prov, serv, user_id, 10)

    r = await _consultar(t, conv)

    assert len(r["citas"]) == 10 and r["hay_mas"] is False


@pytest.mark.asyncio
async def test_servicio_confirmada_futura_no_aparece(tenant_y_usuario):
    t = tenant_y_usuario["tenant_id"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _cita(t, prov, serv, AHORA + timedelta(days=2), user_id=user_id, estado="confirmada")
    await _cita(t, prov, serv, AHORA + timedelta(days=3), user_id=user_id, estado="cancelada",
                cancelada_por="cliente")

    r = await _consultar(t, conv)

    # La confirmada futura no es elegible; la cancelada futura sí (R2).
    assert [c["estado"] for c in r["citas"]] == ["cancelada"]


# ------------------------------------------------------------
# Vacío, cliente, negocio y canal
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_servicio_sin_citas_devuelve_lista_vacia(tenant_y_usuario):
    t = tenant_y_usuario["tenant_id"]
    _, conv = await _cliente_y_conversacion(t)

    assert await _consultar(t, conv) == {"citas": [], "hay_mas": False}


@pytest.mark.asyncio
async def test_servicio_walk_in_con_el_mismo_telefono_queda_excluido(tenant_y_usuario):
    t = tenant_y_usuario["tenant_id"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    # Walk-in (sin user_id) con teléfono: no se vincula por teléfono (R5).
    await _cita(t, prov, serv, AHORA - timedelta(days=1), user_id=None, telefono="+5215551234567")
    await _cita(t, prov, serv, AHORA - timedelta(days=2), user_id=user_id, telefono="+5215551234567")

    r = await _consultar(t, conv)

    assert len(r["citas"]) == 1
    assert r["citas"][0]["fecha_hora_inicio"].startswith("2026-01-08")


@pytest.mark.asyncio
async def test_servicio_no_mezcla_otros_clientes_del_mismo_negocio(tenant_y_usuario):
    t = tenant_y_usuario["tenant_id"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_a, conv_a = await _cliente_y_conversacion(t)
    user_b, _ = await _cliente_y_conversacion(t)
    await _cita(t, prov, serv, AHORA - timedelta(days=1), user_id=user_a)
    await _cita(t, prov, serv, AHORA - timedelta(days=2), user_id=user_b)
    await _cita(t, prov, serv, AHORA - timedelta(days=3), user_id=user_b)

    assert len((await _consultar(t, conv_a))["citas"]) == 1


@pytest.mark.asyncio
async def test_servicio_conversacion_de_otro_negocio_devuelve_none(tenant_y_usuario):
    t = tenant_y_usuario["tenant_id"]
    otro = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro')", otro)
    try:
        _, conv_ajena = await _cliente_y_conversacion(otro)
        assert await _consultar(t, conv_ajena) is None
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", otro)


@pytest.mark.asyncio
async def test_servicio_conversacion_inexistente_devuelve_none(tenant_y_usuario):
    assert await _consultar(tenant_y_usuario["tenant_id"], uuid4()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("canal", ["web", "widget", "chat_web", "WhatsApp", ""])
async def test_servicio_canal_fuera_de_la_lista_blanca_devuelve_none(tenant_y_usuario, canal):
    t = tenant_y_usuario["tenant_id"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t, canal=canal)
    await _cita(t, prov, serv, AHORA - timedelta(days=1), user_id=user_id)

    assert await _consultar(t, conv) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("canal", ["whatsapp", "instagram", "facebook"])
async def test_servicio_canales_admitidos_devuelven_historial(tenant_y_usuario, canal):
    t = tenant_y_usuario["tenant_id"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t, canal=canal)
    await _cita(t, prov, serv, AHORA - timedelta(days=1), user_id=user_id)

    assert len((await _consultar(t, conv))["citas"]) == 1


# ------------------------------------------------------------
# Filtro de proveedor (R10)
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_servicio_proveedor_ve_3_de_11_sin_hay_mas(tenant_y_usuario):
    t = tenant_y_usuario["tenant_id"]
    p, q, serv = await _proveedor(t, "P"), await _proveedor(t, "Q"), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    for i in range(1, 12):
        dueno = p if i in (2, 5, 9) else q
        await _cita(t, dueno, serv, AHORA - timedelta(days=i), user_id=user_id)

    r = await _consultar(t, conv, proveedor_id=p)

    assert len(r["citas"]) == 3
    assert r["hay_mas"] is False
    assert {c["proveedor_nombre"] for c in r["citas"]} == {"P"}
    # Sin filtro, el negocio ve 10 con hay_mas (11 elegibles).
    completo = await _consultar(t, conv)
    assert len(completo["citas"]) == 10 and completo["hay_mas"] is True


@pytest.mark.asyncio
async def test_servicio_proveedor_con_mas_de_diez_propias_ve_diez_con_hay_mas(tenant_y_usuario):
    t = tenant_y_usuario["tenant_id"]
    p, q, serv = await _proveedor(t, "P"), await _proveedor(t, "Q"), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _doce_citas(t, p, serv, user_id, 12)
    for i in range(13, 16):
        await _cita(t, q, serv, AHORA - timedelta(days=i), user_id=user_id)

    r = await _consultar(t, conv, proveedor_id=p)

    assert len(r["citas"]) == 10 and r["hay_mas"] is True
    assert {c["proveedor_nombre"] for c in r["citas"]} == {"P"}


# ------------------------------------------------------------
# Campos de la respuesta y esquemas
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_servicio_la_respuesta_no_trae_precio_pago_motivo_ni_identificador(tenant_y_usuario):
    t = tenant_y_usuario["tenant_id"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _cita(t, prov, serv, AHORA - timedelta(days=1), user_id=user_id, estado="cancelada",
                cancelada_por="cliente", tardia=True, motivo="Se me murió el perro",
                telefono="+5215550000000", notas="Alergia al látex")

    r = await _consultar(t, conv)
    cita = r["citas"][0]

    assert set(cita) == {
        "fecha_hora_inicio", "hora_fin", "servicio_nombre", "proveedor_nombre", "estado",
        "notas", "cancelada_por", "cancelacion_tardia",
    }
    assert not (PROHIBIDOS_SIEMPRE & set(cita))
    assert "Se me murió el perro" not in repr(r)
    assert "99.5" not in repr(r) and "efectivo" not in repr(r)
    assert "5550000000" not in repr(r)


@pytest.mark.asyncio
async def test_servicio_notas_se_conservan_y_no_se_confunden_con_el_motivo(tenant_y_usuario):
    t = tenant_y_usuario["tenant_id"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _cita(t, prov, serv, AHORA - timedelta(days=1), user_id=user_id, estado="cancelada",
                cancelada_por="equipo", tardia=False, notas="Trae a su hijo", motivo="Motivo secreto")
    await _cita(t, prov, serv, AHORA - timedelta(days=2), user_id=user_id, notas=None)

    citas = (await _consultar(t, conv))["citas"]

    assert citas[0]["notas"] == "Trae a su hijo"
    assert citas[0]["cancelada_por"] == "equipo" and citas[0]["cancelacion_tardia"] is False
    assert "Motivo secreto" not in repr(citas)
    assert citas[1]["notas"] is None and citas[1]["cancelada_por"] is None


def test_servicio_esquemas_declaran_solo_los_campos_permitidos():
    agente = set(schemas.CitaHistorialAgenteOut.model_fields)
    portal = set(schemas.CitaHistorialPortalOut.model_fields)

    assert agente == {"fecha_hora_inicio", "servicio_nombre", "proveedor_nombre", "estado"}
    assert portal == agente | {"hora_fin", "notas", "cancelada_por", "cancelacion_tardia"}
    for campos in (agente, portal):
        assert not (PROHIBIDOS_SIEMPRE & campos)
    assert set(schemas.HistorialCitasAgenteOut.model_fields) == {"citas", "hay_mas"}
    assert set(schemas.HistorialCitasPortalOut.model_fields) == {"citas", "hay_mas"}


@pytest.mark.asyncio
async def test_servicio_el_esquema_del_agente_descarta_notas_y_quien_cancelo(tenant_y_usuario):
    t = tenant_y_usuario["tenant_id"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _cita(t, prov, serv, AHORA - timedelta(days=1), user_id=user_id, estado="cancelada",
                cancelada_por="cliente", tardia=True, notas="Nota privada")

    r = await _consultar(t, conv)
    para_agente = schemas.HistorialCitasAgenteOut.model_validate(r).model_dump(mode="json")
    para_portal = schemas.HistorialCitasPortalOut.model_validate(r).model_dump(mode="json")

    assert set(para_agente["citas"][0]) == {
        "fecha_hora_inicio", "servicio_nombre", "proveedor_nombre", "estado"
    }
    assert "Nota privada" not in repr(para_agente)
    assert para_portal["citas"][0]["notas"] == "Nota privada"
    assert para_portal["citas"][0]["cancelada_por"] == "cliente"
    assert para_portal["citas"][0]["cancelacion_tardia"] is True


# ------------------------------------------------------------
# Zona vigente del negocio (R12)
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_servicio_hora_local_con_la_zona_vigente_y_su_cambio(tenant_y_usuario):
    t = tenant_y_usuario["tenant_id"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _cita(t, prov, serv, datetime(2026, 1, 9, 20, 0, tzinfo=timezone.utc), user_id=user_id)

    await set_tenant_servicios(t, None, None, zona_horaria="America/Mexico_City")
    antes = (await _consultar(t, conv))["citas"][0]
    assert antes["fecha_hora_inicio"] == "2026-01-09T14:00:00-06:00"
    assert antes["hora_fin"] == "2026-01-09T14:30:00-06:00"

    # Cambio de zona: el mismo instante se lee ahora con la zona nueva, sin migrar nada.
    await set_tenant_servicios(t, None, None, zona_horaria="America/Bogota")
    despues = (await _consultar(t, conv))["citas"][0]
    assert despues["fecha_hora_inicio"] == "2026-01-09T15:00:00-05:00"
    assert despues["hora_fin"] == "2026-01-09T15:30:00-05:00"


@pytest.mark.asyncio
async def test_servicio_sin_fila_de_servicios_usa_la_zona_por_defecto(tenant_y_usuario):
    t = tenant_y_usuario["tenant_id"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await execute("DELETE FROM tenant_servicios WHERE tenant_id = $1", t)
    await _cita(t, prov, serv, datetime(2026, 1, 9, 20, 0, tzinfo=timezone.utc), user_id=user_id)

    assert (await _consultar(t, conv))["citas"][0]["fecha_hora_inicio"] == "2026-01-09T14:00:00-06:00"


# ------------------------------------------------------------
# Solo lectura (R7)
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_servicio_consultar_no_modifica_reservas_ni_auditoria(tenant_y_usuario):
    t = tenant_y_usuario["tenant_id"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    rid = await _cita(t, prov, serv, AHORA - timedelta(days=1), user_id=user_id)

    async def estado():
        fila = await fetch_one("SELECT * FROM reservas WHERE id = $1", rid)
        n = await fetch_value("SELECT count(*) FROM reserva_auditoria WHERE reserva_id = $1", rid)
        return dict(fila), n

    antes = await estado()
    primera = await _consultar(t, conv)
    segunda = await _consultar(t, conv)

    assert await estado() == antes
    assert primera == segunda


# ============================================================
# Endpoint del portal: GET /api/conversaciones/{id}/historial-citas
# ============================================================
RUTA_PORTAL = "/api/conversaciones/{}/historial-citas"
TEXTO_NO_ENCONTRADA = "Conversación no encontrada"
TEXTO_PROVEEDOR_DESACTIVADO = "Tu cuenta de proveedor está desactivada"
TEXTO_PROVEEDOR_SIN_FICHA = "Tu cuenta tiene el rol de proveedor pero no está ligada a una ficha de proveedor"


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _cuenta(tenant_id, role: str) -> tuple[UUID, dict]:
    """Cuenta del portal con ese rol; devuelve (portal_user_id, headers)."""
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
    return uid, _h(crear_access_token(uid, tenant_id, role))


async def _proveedor_con_cuenta(tenant_id, nombre="P", activo=True) -> tuple[UUID, UUID, dict]:
    """(proveedor_id, portal_user_id, headers) de un proveedor con ficha."""
    uid, headers = await _cuenta(tenant_id, "proveedor")
    pid = await fetch_value(
        "INSERT INTO proveedores (tenant_id, nombre, portal_user_id, activo) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        tenant_id,
        nombre,
        uid,
        activo,
    )
    return pid, uid, headers


async def _asignar(conv_id, portal_user_id) -> None:
    await execute("UPDATE conversations SET asignado_a = $2 WHERE id = $1", conv_id, portal_user_id)


async def _plan_temporal(*, agente: bool, calendario: bool) -> str:
    nombre = f"temp-{uuid4().hex[:10]}"
    await execute(
        """
        INSERT INTO planes
            (nombre, precio_monthly, agente_ia_activo, gestion_vendedores_activo,
             herramientas_activo, crm_campo_activo, calendario_activo, orden)
        VALUES ($1, 49, $2, false, false, false, $3, 99)
        """,
        nombre,
        agente,
        calendario,
    )
    return nombre


async def _contratar(tenant_id, plan: str, estado: str = "activa") -> None:
    await execute("DELETE FROM tenant_subscriptions WHERE tenant_id = $1", tenant_id)
    await execute(
        "INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly) VALUES ($1, $2, $3, 49)",
        tenant_id,
        plan,
        estado,
    )


async def _suspender(tenant_id) -> None:
    await execute(
        """
        INSERT INTO tenant_estado_plataforma (tenant_id, estado, motivo)
        VALUES ($1, 'suspendido', 'test')
        ON CONFLICT (tenant_id) DO UPDATE SET estado = EXCLUDED.estado
        """,
        tenant_id,
    )


@pytest.fixture
async def negocio(tenant_y_usuario):
    """Negocio A: calendario encendido, plan enterprise vigente (fixture del módulo)."""
    t = tenant_y_usuario["tenant_id"]
    await set_tenant_servicios(t, None, None, calendario_activo=True)
    return {"tid": t, "owner": _h(tenant_y_usuario["token"])}


@pytest.fixture
async def planes_temporales(db):
    """Limpia suscripciones y planes a medida que creen los tests (FK)."""
    creados: list[str] = []
    yield creados
    for nombre in creados:
        await execute("DELETE FROM tenant_subscriptions WHERE plan = $1", nombre)
        await execute("DELETE FROM planes WHERE nombre = $1", nombre)


async def _get(http_client, conv_id, headers):
    return await http_client.get(RUTA_PORTAL.format(conv_id), headers=headers)


# ------------------------------------------------------------
# Camino feliz por rol y contenido
# ------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("rol", ["owner", "member"])
async def test_portal_owner_y_member_ven_las_citas_del_cliente(http_client, negocio, rol):
    t = negocio["tid"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _doce_citas(t, prov, serv, user_id, 12)
    headers = negocio["owner"] if rol == "owner" else (await _cuenta(t, "member"))[1]

    r = await _get(http_client, conv, headers)

    assert r.status_code == 200
    cuerpo = r.json()
    assert len(cuerpo["citas"]) == 10 and cuerpo["hay_mas"] is True


@pytest.mark.asyncio
async def test_portal_sin_citas_responde_200_vacio(http_client, negocio):
    _, conv = await _cliente_y_conversacion(negocio["tid"])

    r = await _get(http_client, conv, negocio["owner"])

    assert r.status_code == 200
    assert r.json() == {"citas": [], "hay_mas": False}


@pytest.mark.asyncio
async def test_portal_privacidad_de_la_carga_y_notas_sin_motivo(http_client, negocio):
    t = negocio["tid"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _cita(t, prov, serv, AHORA - timedelta(days=1), user_id=user_id, estado="cancelada",
                cancelada_por="cliente", tardia=True, motivo="no puedo ir",
                telefono="+5215550000000", notas="Alergia al latex")

    r = await _get(http_client, conv, negocio["owner"])

    assert r.status_code == 200
    cita = r.json()["citas"][0]
    assert set(cita) == {
        "fecha_hora_inicio", "hora_fin", "servicio_nombre", "proveedor_nombre", "estado",
        "notas", "cancelada_por", "cancelacion_tardia",
    }
    assert not (PROHIBIDOS_SIEMPRE & set(cita))
    assert cita["notas"] == "Alergia al latex"
    assert cita["cancelada_por"] == "cliente" and cita["cancelacion_tardia"] is True
    for prohibido in ("no puedo ir", "99.5", "efectivo", "5550000000"):
        assert prohibido not in r.text


# ------------------------------------------------------------
# 404 uniforme, 422, 401, 403
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_portal_owner_de_otro_negocio_recibe_404(http_client, negocio):
    t = negocio["tid"]
    _, conv = await _cliente_y_conversacion(t)
    otro = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro')", otro)
    try:
        _, headers_otro = await _cuenta(otro, "owner")
        await set_tenant_servicios(otro, None, None, calendario_activo=True)
        await execute(
            "INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly) "
            "VALUES ($1, 'enterprise', 'activa', 299.99)",
            otro,
        )

        r = await _get(http_client, conv, headers_otro)

        assert r.status_code == 404
        assert r.json()["detail"] == TEXTO_NO_ENCONTRADA
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", otro)


@pytest.mark.asyncio
async def test_portal_conversacion_inexistente_recibe_el_mismo_404(http_client, negocio):
    r = await _get(http_client, uuid4(), negocio["owner"])

    assert r.status_code == 404
    assert r.json()["detail"] == TEXTO_NO_ENCONTRADA


@pytest.mark.asyncio
async def test_portal_conversacion_del_widget_recibe_404(http_client, negocio):
    t = negocio["tid"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t, canal="web")
    await _cita(t, prov, serv, AHORA - timedelta(days=1), user_id=user_id)

    r = await _get(http_client, conv, negocio["owner"])

    assert r.status_code == 404
    assert r.json()["detail"] == TEXTO_NO_ENCONTRADA


@pytest.mark.asyncio
async def test_portal_conversacion_del_widget_tampoco_la_ve_un_proveedor_asignado(http_client, negocio):
    t = negocio["tid"]
    _, uid, headers = await _proveedor_con_cuenta(t)
    _, conv = await _cliente_y_conversacion(t, canal="web")
    await _asignar(conv, uid)

    r = await _get(http_client, conv, headers)

    assert r.status_code == 404
    assert r.json()["detail"] == TEXTO_NO_ENCONTRADA


@pytest.mark.asyncio
async def test_portal_id_mal_formado_en_la_ruta_da_422(http_client, negocio):
    r = await http_client.get("/api/conversaciones/no-es-uuid/historial-citas", headers=negocio["owner"])

    assert r.status_code == 422


@pytest.mark.asyncio
async def test_portal_sin_token_da_401(http_client, negocio):
    _, conv = await _cliente_y_conversacion(negocio["tid"])

    r = await http_client.get(RUTA_PORTAL.format(conv))

    assert r.status_code == 401


@pytest.mark.asyncio
async def test_portal_vendedor_recibe_403(http_client, negocio):
    t = negocio["tid"]
    uid, headers = await _cuenta(t, "vendedor")
    await execute(
        "INSERT INTO vendedores (tenant_id, portal_user_id, nombre, activo) VALUES ($1, $2, 'Ana', true)",
        t,
        uid,
    )
    _, conv = await _cliente_y_conversacion(t)

    r = await _get(http_client, conv, headers)

    assert r.status_code == 403
    assert r.json()["detail"] == "Esta sección no está disponible para tu rol"


# ------------------------------------------------------------
# Calendario y plan (R9)
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_portal_calendario_apagado_da_409_con_codigo(http_client, negocio):
    await set_tenant_servicios(negocio["tid"], None, None, calendario_activo=False)
    _, conv = await _cliente_y_conversacion(negocio["tid"])

    r = await _get(http_client, conv, negocio["owner"])

    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "calendario_inactivo"


@pytest.mark.asyncio
async def test_portal_plan_sin_calendario_da_402_plan_insuficiente(http_client, negocio):
    await _contratar(negocio["tid"], "starter")
    _, conv = await _cliente_y_conversacion(negocio["tid"])

    r = await _get(http_client, conv, negocio["owner"])

    assert r.status_code == 402
    detalle = r.json()["detail"]
    assert detalle["codigo"] == "plan_insuficiente" and detalle["herramienta"] == "calendario"


@pytest.mark.asyncio
async def test_portal_plan_sin_conversaciones_da_402_plan_insuficiente(
    http_client, negocio, planes_temporales
):
    plan = await _plan_temporal(agente=False, calendario=True)
    planes_temporales.append(plan)
    await _contratar(negocio["tid"], plan)
    _, conv = await _cliente_y_conversacion(negocio["tid"])

    r = await _get(http_client, conv, negocio["owner"])

    assert r.status_code == 402
    detalle = r.json()["detail"]
    assert detalle["codigo"] == "plan_insuficiente" and detalle["herramienta"] == "agente"


@pytest.mark.asyncio
async def test_portal_cuenta_suspendida_da_402(http_client, negocio):
    await _suspender(negocio["tid"])
    _, conv = await _cliente_y_conversacion(negocio["tid"])

    r = await _get(http_client, conv, negocio["owner"])

    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "cuenta_suspendida"


@pytest.mark.asyncio
@pytest.mark.parametrize("estado", ["pausada", "cancelada"])
async def test_portal_plan_vencido_da_402_plan_requerido_por_el_calendario(http_client, negocio, estado):
    """Las conversaciones se leerían con el plan vencido; el calendario no tiene esa excepción."""
    await _contratar(negocio["tid"], "enterprise", estado)
    _, conv = await _cliente_y_conversacion(negocio["tid"])

    r = await _get(http_client, conv, negocio["owner"])

    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "plan_requerido"


@pytest.mark.asyncio
async def test_portal_sin_plan_da_402_plan_requerido(http_client, negocio):
    await execute("DELETE FROM tenant_subscriptions WHERE tenant_id = $1", negocio["tid"])
    _, conv = await _cliente_y_conversacion(negocio["tid"])

    r = await _get(http_client, conv, negocio["owner"])

    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "plan_requerido"


# ------------------------------------------------------------
# Orden de verificación (criterio 12)
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_portal_orden_calendario_apagado_sin_acceso_a_conversaciones_da_402_no_409(
    http_client, negocio, planes_temporales
):
    plan = await _plan_temporal(agente=False, calendario=False)
    planes_temporales.append(plan)
    await _contratar(negocio["tid"], plan)
    await set_tenant_servicios(negocio["tid"], None, None, calendario_activo=False)
    _, conv = await _cliente_y_conversacion(negocio["tid"])

    r = await _get(http_client, conv, negocio["owner"])

    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "plan_insuficiente"


@pytest.mark.asyncio
async def test_portal_orden_proveedor_desactivado_con_cuenta_suspendida_da_402_no_403(http_client, negocio):
    t = negocio["tid"]
    _, _, headers = await _proveedor_con_cuenta(t, activo=False)
    await _suspender(t)
    _, conv = await _cliente_y_conversacion(t)

    r = await _get(http_client, conv, headers)

    assert r.status_code == 402
    assert r.json()["detail"]["codigo"] == "cuenta_suspendida"


@pytest.mark.asyncio
async def test_portal_orden_proveedor_desactivado_con_plan_en_regla_da_403(http_client, negocio):
    t = negocio["tid"]
    _, _, headers = await _proveedor_con_cuenta(t, activo=False)
    _, conv = await _cliente_y_conversacion(t)

    r = await _get(http_client, conv, headers)

    assert r.status_code == 403
    assert r.json()["detail"] == TEXTO_PROVEEDOR_DESACTIVADO


@pytest.mark.asyncio
async def test_portal_proveedor_desactivado_antes_que_calendario_apagado(http_client, negocio):
    """La ficha (403) se comprueba antes que el módulo (409), como pide el orden de la spec."""
    t = negocio["tid"]
    _, _, headers = await _proveedor_con_cuenta(t, activo=False)
    await set_tenant_servicios(t, None, None, calendario_activo=False)
    _, conv = await _cliente_y_conversacion(t)

    r = await _get(http_client, conv, headers)

    assert r.status_code == 403


@pytest.mark.asyncio
async def test_portal_proveedor_sin_ficha_da_403_con_su_texto(http_client, negocio):
    t = negocio["tid"]
    _, headers = await _cuenta(t, "proveedor")  # rol proveedor sin fila en `proveedores`
    _, conv = await _cliente_y_conversacion(t)

    r = await _get(http_client, conv, headers)

    assert r.status_code == 403
    assert r.json()["detail"] == TEXTO_PROVEEDOR_SIN_FICHA


# ------------------------------------------------------------
# Proveedor: solo lo suyo (R10)
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_portal_proveedor_ve_3_de_11_sin_hay_mas_ni_totales(http_client, negocio):
    t = negocio["tid"]
    p, uid, headers = await _proveedor_con_cuenta(t, "P")
    q, serv = await _proveedor(t, "Q"), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    for i in range(1, 12):
        await _cita(t, p if i in (2, 5, 9) else q, serv, AHORA - timedelta(days=i), user_id=user_id)
    await _asignar(conv, uid)

    r = await _get(http_client, conv, headers)

    assert r.status_code == 200
    cuerpo = r.json()
    assert len(cuerpo["citas"]) == 3 and cuerpo["hay_mas"] is False
    assert {c["proveedor_nombre"] for c in cuerpo["citas"]} == {"P"}
    assert set(cuerpo) == {"citas", "hay_mas"}  # sin total de otras citas


@pytest.mark.asyncio
async def test_portal_proveedor_con_mas_de_diez_propias_ve_diez_con_hay_mas(http_client, negocio):
    t = negocio["tid"]
    p, uid, headers = await _proveedor_con_cuenta(t, "P")
    q, serv = await _proveedor(t, "Q"), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _doce_citas(t, p, serv, user_id, 12)
    for i in range(13, 16):
        await _cita(t, q, serv, AHORA - timedelta(days=i), user_id=user_id)
    await _asignar(conv, uid)

    r = await _get(http_client, conv, headers)

    assert r.status_code == 200
    assert len(r.json()["citas"]) == 10 and r.json()["hay_mas"] is True
    assert {c["proveedor_nombre"] for c in r.json()["citas"]} == {"P"}


@pytest.mark.asyncio
async def test_portal_proveedor_con_conversacion_visible_y_sin_citas_propias_recibe_vacio(http_client, negocio):
    t = negocio["tid"]
    _, uid, headers = await _proveedor_con_cuenta(t, "P")
    q, serv = await _proveedor(t, "Q"), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _cita(t, q, serv, AHORA - timedelta(days=1), user_id=user_id)
    await _asignar(conv, uid)

    r = await _get(http_client, conv, headers)

    assert r.status_code == 200
    assert r.json() == {"citas": [], "hay_mas": False}


@pytest.mark.asyncio
async def test_portal_proveedor_sin_acceso_a_la_conversacion_recibe_404_no_403(http_client, negocio):
    t = negocio["tid"]
    _, _, headers = await _proveedor_con_cuenta(t, "P")
    q, serv = await _proveedor(t, "Q"), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _cita(t, q, serv, AHORA - timedelta(days=1), user_id=user_id)  # el cliente es de Q

    r = await _get(http_client, conv, headers)

    assert r.status_code == 404
    assert r.json()["detail"] == TEXTO_NO_ENCONTRADA


@pytest.mark.asyncio
async def test_portal_proveedor_no_ve_la_conversacion_asignada_a_otra_persona(http_client, negocio):
    t = negocio["tid"]
    p, _, headers = await _proveedor_con_cuenta(t, "P")
    serv = await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _cita(t, p, serv, AHORA - timedelta(days=1), user_id=user_id)  # sería suya por ficha...
    await _asignar(conv, (await _cuenta(t, "member"))[0])  # ...pero la asignación manda

    r = await _get(http_client, conv, headers)

    assert r.status_code == 404


@pytest.mark.asyncio
async def test_portal_reasignacion_de_la_cita_mas_reciente_cambia_quien_ve_la_conversacion(http_client, negocio):
    t = negocio["tid"]
    p, _, h_p = await _proveedor_con_cuenta(t, "P")
    q, _, h_q = await _proveedor_con_cuenta(t, "Q")
    serv = await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    cita_q = await _cita(t, q, serv, AHORA - timedelta(days=1), user_id=user_id,
                         creado_en=AHORA - timedelta(days=5))
    assert (await _get(http_client, conv, h_q)).status_code == 200
    assert (await _get(http_client, conv, h_p)).status_code == 404

    await execute("UPDATE reservas SET proveedor_id = $2 WHERE id = $1", cita_q, p)

    r_q = await _get(http_client, conv, h_q)
    assert r_q.status_code == 404
    assert r_q.json()["detail"] == TEXTO_NO_ENCONTRADA
    r_p = await _get(http_client, conv, h_p)
    assert r_p.status_code == 200
    assert len(r_p.json()["citas"]) == 1


@pytest.mark.asyncio
async def test_portal_reasignar_una_cita_que_no_es_la_mas_reciente_deja_a_q_con_200_sin_ella(http_client, negocio):
    t = negocio["tid"]
    p, _, h_p = await _proveedor_con_cuenta(t, "P")
    q, _, h_q = await _proveedor_con_cuenta(t, "Q")
    serv = await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    vieja = await _cita(t, q, serv, AHORA - timedelta(days=3), user_id=user_id,
                        creado_en=AHORA - timedelta(days=9))
    await _cita(t, q, serv, AHORA - timedelta(days=1), user_id=user_id,
                creado_en=AHORA - timedelta(days=5))  # la más reciente por creación: sigue siendo de Q

    await execute("UPDATE reservas SET proveedor_id = $2 WHERE id = $1", vieja, p)

    r_q = await _get(http_client, conv, h_q)
    assert r_q.status_code == 200
    assert len(r_q.json()["citas"]) == 1
    assert {c["proveedor_nombre"] for c in r_q.json()["citas"]} == {"Q"}
    assert (await _get(http_client, conv, h_p)).status_code == 404  # P no ve la conversación (no es la reciente)


# ------------------------------------------------------------
# Lista blanca de permitir_a_proveedor y solo lectura
# ------------------------------------------------------------
def test_portal_la_lista_blanca_de_proveedor_incluye_el_nombre_real_de_la_funcion():
    from main import app
    from routers import conversaciones_historial as modulo

    # Registrado en la app con el prefijo /api, solo GET.
    ruta_api = app.openapi()["paths"]["/api/conversaciones/{conversacion_id}/historial-citas"]
    assert set(ruta_api) == {"get"}
    rutas = [
        r for r in modulo.router.routes
        if getattr(r, "path", "") == "/conversaciones/{conversacion_id}/historial-citas"
    ]
    assert len(rutas) == 1 and rutas[0].methods == {"GET"}
    nombre = rutas[0].endpoint.__name__
    assert nombre == "historial_citas" and modulo.historial_citas is rutas[0].endpoint

    # La dependencia `permitir_a_proveedor` del router lleva la lista blanca en su cierre.
    listas = [
        celda.cell_contents
        for dep in modulo.router.dependencies
        for celda in (getattr(dep.dependency, "__closure__", None) or ())
        if isinstance(celda.cell_contents, frozenset)
    ]
    assert any(nombre in lista for lista in listas)


@pytest.mark.asyncio
async def test_portal_consultar_no_modifica_reservas_ni_auditoria(http_client, negocio):
    t = negocio["tid"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    rid = await _cita(t, prov, serv, AHORA - timedelta(days=1), user_id=user_id)

    async def estado():
        fila = await fetch_one("SELECT * FROM reservas WHERE id = $1", rid)
        n = await fetch_value("SELECT count(*) FROM reserva_auditoria WHERE reserva_id = $1", rid)
        return dict(fila), n

    antes = await estado()
    r1 = await _get(http_client, conv, negocio["owner"])
    r2 = await _get(http_client, conv, negocio["owner"])

    assert r1.status_code == r2.status_code == 200 and r1.json() == r2.json()
    assert await estado() == antes


@pytest.mark.asyncio
async def test_portal_solo_acepta_get(http_client, negocio):
    _, conv = await _cliente_y_conversacion(negocio["tid"])

    for metodo in ("post", "put", "patch", "delete"):
        r = await getattr(http_client, metodo)(RUTA_PORTAL.format(conv), headers=negocio["owner"])
        assert r.status_code == 405, metodo


# ------------------------------------------------------------
# "Ver como" sin concesión
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_portal_ver_como_sin_concesion_puede_consultar(http_client, negocio):
    t = negocio["tid"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _cita(t, prov, serv, AHORA - timedelta(days=1), user_id=user_id)

    tid_gerente = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Interno')", tid_gerente)
    email_g = None
    try:
        uid_g, headers_g = await _cuenta(tid_gerente, "owner")
        email_g = await fetch_value("SELECT email FROM portal_users WHERE id = $1", uid_g)
        await execute(
            "INSERT INTO gerencia_users (email, full_name, cargo) VALUES ($1, 'Test', 'Developer')", email_g
        )
        r_imp = await http_client.post(
            f"/api/gerencia/tenants/{t}/impersonar",
            json={"motivo": "soporte: revisar historial"},
            headers=headers_g,
        )
        assert r_imp.status_code == 200
        soporte = _h(r_imp.json()["access_token"])

        r = await _get(http_client, conv, soporte)

        assert r.status_code == 200
        assert len(r.json()["citas"]) == 1
    finally:
        if email_g is not None:
            await execute("DELETE FROM gerencia_users WHERE LOWER(email) = LOWER($1)", email_g)
        await execute("DELETE FROM gerencia_auditoria WHERE tenant_id = $1 OR tenant_id = $2", t, tid_gerente)
        await execute("DELETE FROM tenants WHERE id = $1", tid_gerente)


# ============================================================
# Endpoint del agente: POST /api/eventos/calendario/historial-cliente
# ============================================================
RUTA_AGENTE = "/api/eventos/calendario/historial-cliente"
TOKEN_AGENTE = "secreto-de-prueba-historial-agente"


@pytest.fixture
def interno(monkeypatch) -> dict:
    """Cabecera de llamada interna de n8n (sin JWT)."""
    from config import settings

    monkeypatch.setattr(settings, "N8N_INTERNAL_TOKEN", TOKEN_AGENTE)
    return {"X-Internal-Token": TOKEN_AGENTE}


async def _post_agente(http_client, interno, tenant_id, conversation_id, **extra):
    return await http_client.post(
        RUTA_AGENTE,
        json={"tenant_id": str(tenant_id), "conversation_id": conversation_id, **extra},
        headers=interno,
    )


async def _negocio_ajeno() -> UUID:
    """Negocio B con calendario encendido y plan vigente (el llamante lo borra)."""
    otro = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro')", otro)
    await set_tenant_servicios(otro, None, None, calendario_activo=True)
    await execute(
        "INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly) "
        "VALUES ($1, 'enterprise', 'activa', 299.99)",
        otro,
    )
    return otro


@pytest.mark.asyncio
async def test_agente_devuelve_el_historial_sin_campos_prohibidos(http_client, negocio, interno):
    t = negocio["tid"]
    prov, serv = await _proveedor(t, "Juan"), await _servicio(t, "Corte")
    user_id, conv = await _cliente_y_conversacion(t)
    await _cita(t, prov, serv, AHORA - timedelta(days=1), user_id=user_id, estado="cancelada",
                cancelada_por="cliente", tardia=True, motivo="no puedo ir",
                telefono="+5215550000000", notas="Alergia al latex")

    r = await _post_agente(http_client, interno, t, str(conv))

    assert r.status_code == 200
    cuerpo = r.json()
    assert set(cuerpo) == {"citas", "hay_mas"} and cuerpo["hay_mas"] is False
    assert len(cuerpo["citas"]) == 1
    cita = cuerpo["citas"][0]
    assert set(cita) == {"fecha_hora_inicio", "servicio_nombre", "proveedor_nombre", "estado"}
    assert cita["servicio_nombre"] == "Corte" and cita["proveedor_nombre"] == "Juan"
    assert cita["estado"] == "cancelada"
    for prohibido in ("Alergia", "no puedo ir", "99.5", "efectivo", "5550000000", "tardia", "cancelada_por"):
        assert prohibido not in r.text


@pytest.mark.asyncio
async def test_agente_doce_citas_devuelve_diez_con_hay_mas_en_orden_descendente(http_client, negocio, interno):
    t = negocio["tid"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _doce_citas(t, prov, serv, user_id, 12)

    r = await _post_agente(http_client, interno, t, str(conv))

    assert r.status_code == 200
    cuerpo = r.json()
    assert len(cuerpo["citas"]) == 10 and cuerpo["hay_mas"] is True
    inicios = [c["fecha_hora_inicio"] for c in cuerpo["citas"]]
    assert inicios == sorted(inicios, reverse=True)


@pytest.mark.asyncio
async def test_agente_sin_citas_responde_200_vacio(http_client, negocio, interno):
    _, conv = await _cliente_y_conversacion(negocio["tid"])

    r = await _post_agente(http_client, interno, negocio["tid"], str(conv))

    assert r.status_code == 200 and r.json() == {"citas": [], "hay_mas": False}


# ------------------------------------------------------------
# 404 uniforme (R6, criterio 5)
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_agente_todos_los_casos_de_conversacion_invalida_dan_el_mismo_404(http_client, negocio, interno):
    t = negocio["tid"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_web, conv_web = await _cliente_y_conversacion(t, canal="web")
    await _cita(t, prov, serv, AHORA - timedelta(days=1), user_id=user_web)
    otro = await _negocio_ajeno()
    try:
        _, conv_ajena = await _cliente_y_conversacion(otro)
        casos = {
            "ajena": str(conv_ajena),
            "inexistente": str(uuid4()),
            "nulo": None,
            "vacio": "",
            "abc": "abc",
            "numero": 12345,
            "booleano": True,
            "objeto": {"id": str(conv_ajena)},
            "lista": [str(conv_ajena)],
            "widget": str(conv_web),
        }
        cuerpos = {}
        for nombre, valor in casos.items():
            r = await _post_agente(http_client, interno, t, valor)
            assert r.status_code == 404, nombre
            cuerpos[nombre] = r.text
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", otro)

    assert len(set(cuerpos.values())) == 1
    assert TEXTO_NO_ENCONTRADA in next(iter(cuerpos.values()))


@pytest.mark.asyncio
async def test_agente_tenant_de_b_con_conversacion_de_a_da_404(http_client, negocio, interno):
    t = negocio["tid"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _cita(t, prov, serv, AHORA - timedelta(days=1), user_id=user_id)
    otro = await _negocio_ajeno()
    try:
        r = await _post_agente(http_client, interno, otro, str(conv))
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", otro)

    assert r.status_code == 404
    assert r.json()["detail"] == TEXTO_NO_ENCONTRADA


@pytest.mark.asyncio
async def test_agente_sin_el_campo_conversation_id_da_422(http_client, negocio, interno):
    r = await http_client.post(RUTA_AGENTE, json={"tenant_id": str(negocio["tid"])}, headers=interno)

    assert r.status_code == 422


@pytest.mark.asyncio
async def test_agente_sin_tenant_id_da_422(http_client, negocio, interno):
    _, conv = await _cliente_y_conversacion(negocio["tid"])

    r = await http_client.post(RUTA_AGENTE, json={"conversation_id": str(conv)}, headers=interno)

    assert r.status_code == 422


# ------------------------------------------------------------
# Autenticación (R8, criterio 7) y orden de las comprobaciones (R9)
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_agente_sin_autenticacion_da_401(http_client, negocio, interno):
    _, conv = await _cliente_y_conversacion(negocio["tid"])
    cuerpo = {"tenant_id": str(negocio["tid"]), "conversation_id": str(conv)}

    sin = await http_client.post(RUTA_AGENTE, json=cuerpo)
    mala = await http_client.post(RUTA_AGENTE, json=cuerpo, headers={"X-Internal-Token": "otro"})
    solo_jwt = await http_client.post(RUTA_AGENTE, json=cuerpo, headers=negocio["owner"])

    assert sin.status_code == mala.status_code == solo_jwt.status_code == 401


@pytest.mark.asyncio
async def test_agente_el_401_va_antes_que_el_422(http_client, negocio, interno):
    r = await http_client.post(RUTA_AGENTE, json={})

    assert r.status_code == 401


@pytest.mark.asyncio
async def test_agente_el_422_va_antes_que_el_modulo_apagado(http_client, negocio, interno):
    await set_tenant_servicios(negocio["tid"], None, None, calendario_activo=False)

    r = await http_client.post(RUTA_AGENTE, json={"tenant_id": str(negocio["tid"])}, headers=interno)

    assert r.status_code == 422


@pytest.mark.asyncio
async def test_agente_calendario_apagado_da_409_con_codigo(http_client, negocio, interno):
    t = negocio["tid"]
    await set_tenant_servicios(t, None, None, calendario_activo=False)
    _, conv = await _cliente_y_conversacion(t)

    r = await _post_agente(http_client, interno, t, str(conv))

    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "calendario_inactivo"


@pytest.mark.asyncio
async def test_agente_calendario_apagado_da_409_con_conversacion_ajena(http_client, negocio, interno):
    t = negocio["tid"]
    await set_tenant_servicios(t, None, None, calendario_activo=False)
    otro = await _negocio_ajeno()
    try:
        _, conv_ajena = await _cliente_y_conversacion(otro)
        r = await _post_agente(http_client, interno, t, str(conv_ajena))
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", otro)

    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "calendario_inactivo"


@pytest.mark.asyncio
@pytest.mark.parametrize("valor", ["inexistente", None, "", "abc", 7, {"a": 1}])
async def test_agente_calendario_apagado_da_409_con_identificador_invalido(http_client, negocio, interno, valor):
    t = negocio["tid"]
    await set_tenant_servicios(t, None, None, calendario_activo=False)
    if valor == "inexistente":
        valor = str(uuid4())

    r = await _post_agente(http_client, interno, t, valor)

    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "calendario_inactivo"


@pytest.mark.asyncio
async def test_agente_sin_pagos_da_402_de_texto(http_client, negocio, interno):
    t = negocio["tid"]
    _, conv = await _cliente_y_conversacion(t)
    await execute("UPDATE tenant_subscriptions SET estado = 'pausada' WHERE tenant_id = $1", t)
    await execute("DELETE FROM tenant_credits WHERE tenant_id = $1", t)

    r = await _post_agente(http_client, interno, t, str(conv))

    assert r.status_code == 402
    assert isinstance(r.json()["detail"], str)


# ------------------------------------------------------------
# Cliente deducido solo de la conversación (R23, R24; criterios 26, 27)
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_agente_ignora_campos_extra_que_intentan_indicar_otro_cliente(http_client, negocio, interno):
    t = negocio["tid"]
    prov, serv = await _proveedor(t), await _servicio(t, "Corte de U")
    serv_w = await _servicio(t, "Tinte de W")
    user_u, conv_u = await _cliente_y_conversacion(t)
    user_w, _ = await _cliente_y_conversacion(t)
    await _cita(t, prov, serv, AHORA - timedelta(days=1), user_id=user_u)
    rid_w = await _cita(t, prov, serv_w, AHORA - timedelta(days=2), user_id=user_w)
    base = await _post_agente(http_client, interno, t, str(conv_u))

    r = await _post_agente(
        http_client, interno, t, str(conv_u),
        user_id=str(user_w), reserva_id=str(rid_w), cliente_id=str(uuid4()),
        otro_tenant_id=str(uuid4()), cliente_telefono="+5215559999999", proveedor_id=str(prov),
    )

    assert r.status_code == 200 and r.json() == base.json()
    assert [c["servicio_nombre"] for c in r.json()["citas"]] == ["Corte de U"]


@pytest.mark.asyncio
async def test_agente_ninguna_cita_de_otro_cliente_del_mismo_negocio(http_client, negocio, interno):
    t = negocio["tid"]
    prov, serv = await _proveedor(t), await _servicio(t, "Corte")
    serv_w = await _servicio(t, "Servicio de W")
    user_u, conv_u = await _cliente_y_conversacion(t)
    user_w, _ = await _cliente_y_conversacion(t)
    await _cita(t, prov, serv, AHORA - timedelta(days=3), user_id=user_u)
    await _cita(t, prov, serv_w, AHORA - timedelta(days=1), user_id=user_w, telefono="+5215551111111")

    r = await _post_agente(http_client, interno, t, str(conv_u))

    assert r.status_code == 200
    assert [c["servicio_nombre"] for c in r.json()["citas"]] == ["Corte"]
    assert "Servicio de W" not in r.text and "5551111111" not in r.text


@pytest.mark.asyncio
async def test_agente_acepta_el_uuid_en_mayusculas(http_client, negocio, interno):
    t = negocio["tid"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _cita(t, prov, serv, AHORA - timedelta(days=1), user_id=user_id)

    r = await _post_agente(http_client, interno, t, str(conv).upper())

    assert r.status_code == 200 and len(r.json()["citas"]) == 1


# ------------------------------------------------------------
# Hora local (R12, criterio 19)
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_agente_hora_con_el_desfase_local_y_su_cambio_de_zona(http_client, negocio, interno):
    t = negocio["tid"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    await _cita(t, prov, serv, datetime(2026, 1, 9, 20, 0, tzinfo=timezone.utc), user_id=user_id)

    await set_tenant_servicios(t, None, None, zona_horaria="America/Mexico_City")
    r1 = await _post_agente(http_client, interno, t, str(conv))
    await set_tenant_servicios(t, None, None, zona_horaria="America/Bogota")
    r2 = await _post_agente(http_client, interno, t, str(conv))

    assert r1.json()["citas"][0]["fecha_hora_inicio"] == "2026-01-09T14:00:00-06:00"
    assert r2.json()["citas"][0]["fecha_hora_inicio"] == "2026-01-09T15:00:00-05:00"


# ------------------------------------------------------------
# Solo lectura y repetible (R7, R22; criterio 25)
# ------------------------------------------------------------
@pytest.mark.asyncio
async def test_agente_llamar_dos_veces_no_cambia_reserva_ni_auditoria_y_repite_la_respuesta(
    http_client, negocio, interno
):
    t = negocio["tid"]
    prov, serv = await _proveedor(t), await _servicio(t)
    user_id, conv = await _cliente_y_conversacion(t)
    rid = await _cita(t, prov, serv, AHORA - timedelta(days=1), user_id=user_id)

    async def estado():
        fila = dict(await fetch_one("SELECT * FROM reservas WHERE id = $1", rid))
        n = await fetch_value("SELECT count(*) FROM reserva_auditoria WHERE reserva_id = $1", rid)
        return fila, fila["actualizado_en"], n

    antes = await estado()
    r1 = await _post_agente(http_client, interno, t, str(conv))
    r2 = await _post_agente(http_client, interno, t, str(conv))
    despues = await estado()

    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json() and len(r1.json()["citas"]) == 1
    assert despues[0] == antes[0]
    assert despues[1] == antes[1]
    assert despues[2] == antes[2]
