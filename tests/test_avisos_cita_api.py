"""
Aviso de cancelación del equipo y consulta de registros (spec 004, T6).

Sin BD ni red: el endpoint real corre con `usuario_actual` sustituido y con las
funciones de datos del servicio de calendario y de `avisos_cita` reemplazadas
por fakes en memoria. Lo que se prueba es lo que decide T6: quién dispara el
aviso, qué motivo sale, que se registre una sola vez, que un fallo del aviso no
toque la cancelación y que `GET .../avisos` solo lo lea gerencia.

La unicidad real en Postgres queda cubierta por `test_avisos_sql.py` y por A9.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

import realtime
from config import settings
from deps import UsuarioActual, usuario_actual
from main import app
from routers import calendario as router_calendario
from services import avisos_cita as ac
from services import calendario as svc_calendario
from services import avisos_cita_envio, contexto_agente
from services.avisos_cita_envio import ResultadoEnvioTenant

TENANT = uuid4()
OTRO_TENANT = uuid4()
PROVEEDOR_ID = uuid4()
OTRO_PROVEEDOR_ID = uuid4()
INICIO = datetime.now(timezone.utc) + timedelta(days=2)
RAIZ = Path(__file__).resolve().parent.parent


def _usuario(role: str, *, tenant: UUID = TENANT) -> UsuarioActual:
    return UsuarioActual(id=uuid4(), tenant_id=tenant, email=f"{role}@ejemplo.com", role=role)


def _reserva(
    reserva_id: UUID,
    *,
    estado: str = "confirmada",
    proveedor_id: UUID = PROVEEDOR_ID,
    tenant_id: UUID = TENANT,
) -> svc_calendario.Reserva:
    return svc_calendario.Reserva(
        id=reserva_id, tenant_id=tenant_id, proveedor_id=proveedor_id,
        proveedor_nombre="Juan", proveedor_color="#fff", servicio_id=uuid4(),
        servicio_nombre="Corte", hora_inicio=INICIO, hora_fin=INICIO + timedelta(minutes=30),
        estado=estado, user_id=None, cliente_nombre="Ana", cliente_telefono=None,
        notas=None, precio_cobrado=None, metodo_pago=None, creado_en=INICIO,
    )


class _Mundo:
    """Estado en memoria que sustituye a la BD y a los proveedores externos."""

    def __init__(self) -> None:
        self.reservas: dict[UUID, svc_calendario.Reserva] = {}
        self.datos_aviso: dict[UUID, dict[str, Any]] = {}
        self.conversacion: UUID | None = uuid4()
        self.plantilla_estado: str | None = "APPROVED"  # caché de la plantilla del tenant
        self.habilitado = True  # calendario activo y función encendida
        self.avisos: dict[UUID, dict[str, Any]] = {}
        self.envios: list[tuple[str, str, list[str]]] = []
        self.eventos: list[dict[str, Any]] = []
        self.gerencia: list[tuple[UUID, str]] = []
        self.cancelaciones = 0
        self.fallar_envio = False
        self.fallar_aviso = False
        self.usuario: UsuarioActual = _usuario("owner")

    def nueva_reserva(
        self,
        *,
        telefono: str | None = "55 1234 5678",
        canal: str | None = "whatsapp",
        servicio: str = "Corte",
        proveedor_id: UUID = PROVEEDOR_ID,
        tenant_id: UUID = TENANT,
    ) -> UUID:
        reserva_id = uuid4()
        self.reservas[reserva_id] = _reserva(reserva_id, proveedor_id=proveedor_id, tenant_id=tenant_id)
        self.datos_aviso[reserva_id] = {
            "estado": "cancelada", "hora_inicio": INICIO, "cliente_telefono": telefono,
            "user_id": uuid4(), "servicio_nombre": servicio, "channel_type": canal,
        }
        return reserva_id

    def del_tipo(self, tipo: str = "cancelacion") -> list[dict[str, Any]]:
        return [a for a in self.avisos.values() if a["tipo"] == tipo]


@pytest.fixture
def mundo(monkeypatch: pytest.MonkeyPatch) -> _Mundo:
    m = _Mundo()
    monkeypatch.setattr(settings, "AVISOS_CITA_WHATSAPP_ACTIVOS", True)

    async def _usuario_actual() -> UsuarioActual:
        return m.usuario

    app.dependency_overrides[usuario_actual] = _usuario_actual

    # --- calendario (datos y cancelación) ---
    async def modulo_activo(tenant_id: UUID) -> None:
        return None

    async def reserva_del_tenant(reserva_id: UUID, tenant_id: UUID):
        r = m.reservas.get(reserva_id)
        return r if r is not None and r.tenant_id == tenant_id else None

    async def cancelar(reserva_id, tenant_id, motivo, actor, *, origen, ahora=None):
        m.cancelaciones += 1
        assert origen == "equipo"
        r = m.reservas[reserva_id]
        cancelada = _reserva(reserva_id, estado="cancelada", proveedor_id=r.proveedor_id)
        m.reservas[reserva_id] = cancelada
        return cancelada

    async def sin_cuenta(proveedor_id: UUID):
        return None

    async def proveedor_actual(usuario: UsuarioActual):
        return SimpleNamespace(id=PROVEEDOR_ID)

    async def nada(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(router_calendario, "verificar_calendario_activo", modulo_activo)
    monkeypatch.setattr(svc_calendario, "reserva_del_tenant", reserva_del_tenant)
    monkeypatch.setattr(svc_calendario, "cancelar_reserva", cancelar)
    monkeypatch.setattr(svc_calendario, "cuenta_del_proveedor", sin_cuenta)
    monkeypatch.setattr(router_calendario, "proveedor_actual", proveedor_actual)
    monkeypatch.setattr(router_calendario, "broadcast_alerta", nada)
    monkeypatch.setattr(router_calendario, "avisar_a_proveedor", nada)
    monkeypatch.setattr(router_calendario, "emitir_datos", nada)

    # --- servicio de avisos ---
    async def crear_ejecucion(tipo: str) -> UUID:
        return uuid4()

    async def datos_cancelacion(tenant_id: UUID, reserva_id: UUID):
        return m.datos_aviso.get(reserva_id)

    async def conversacion(tenant_id: UUID, user_id: UUID | None):
        return m.conversacion

    async def estado_aviso(tenant_id: UUID, tipo: str, **kw: Any) -> ac.EstadoAviso:
        nombre = "recordatorio_cita" if tipo == "recordatorio" else "notificacion_cancelacion"
        return ac.EstadoAviso(m.habilitado, nombre, m.plantilla_estado, True)

    async def servicios(tenant_id: UUID):
        return SimpleNamespace(zona_horaria="America/Mexico_City")

    async def registrar_intento(*, tenant_id, reserva_id, tipo, hora_inicio, ejecucion_id,
                                estado="en_curso", motivo=None, sin_conversacion=False):
        if m.fallar_aviso:
            raise RuntimeError("BD caída")
        if any(
            a["reserva_id"] == reserva_id and a["tipo"] == tipo and a["hora_inicio"] == hora_inicio
            for a in m.avisos.values()
        ):
            return None  # ON CONFLICT DO NOTHING
        aviso_id = uuid4()
        m.avisos[aviso_id] = {
            "id": aviso_id, "tenant_id": tenant_id, "reserva_id": reserva_id, "tipo": tipo,
            "hora_inicio": hora_inicio, "estado": estado, "motivo": motivo,
            "sin_conversacion": sin_conversacion, "telefono_destino": None,
            "creado_en": datetime.now(timezone.utc), "cerrado_en": None,
        }
        return aviso_id

    async def cerrar_registro(aviso_id, estado, motivo=None, telefono_destino=None):
        a = m.avisos[aviso_id]
        if a["estado"] != "en_curso":
            return False
        a.update(estado=estado, motivo=motivo, cerrado_en=datetime.now(timezone.utc),
                 telefono_destino=telefono_destino if estado == "enviado" else None)
        return True

    async def enviar_plantilla(tenant_id: UUID, destinatario: str, plantilla: str,
                               parametros: list[dict[str, Any]], **kw: Any) -> ResultadoEnvioTenant:
        if m.fallar_envio:
            raise RuntimeError("proveedor caído")
        valores = [p["text"] for p in parametros[0]["parameters"]]  # solo cuerpo
        m.envios.append((destinatario, plantilla, valores))
        return ResultadoEnvioTenant(True, None, "wamid.1")

    async def registrar_evento(**kw: Any) -> bool:
        if kw["conversation_id"] is None:
            return False
        m.eventos.append(kw)
        return True

    async def avisar_gerencia_cancelacion(tenant_id: UUID, reserva_id: UUID, motivo: str) -> None:
        m.gerencia.append((reserva_id, motivo))

    monkeypatch.setattr(ac, "crear_ejecucion", crear_ejecucion)
    monkeypatch.setattr(ac, "terminar_ejecucion", nada)
    monkeypatch.setattr(ac, "_datos_cancelacion", datos_cancelacion)
    monkeypatch.setattr(ac, "get_tenant_servicios", servicios)
    monkeypatch.setattr(ac, "registrar_intento", registrar_intento)
    monkeypatch.setattr(ac, "cerrar_registro", cerrar_registro)
    monkeypatch.setattr(ac, "avisar_gerencia_cancelacion", avisar_gerencia_cancelacion)
    monkeypatch.setattr(contexto_agente, "conversacion_whatsapp_del_cliente", conversacion)
    monkeypatch.setattr(contexto_agente, "registrar_evento", registrar_evento)
    monkeypatch.setattr(ac, "_estado_aviso", estado_aviso)
    monkeypatch.setattr(avisos_cita_envio, "enviar_plantilla_tenant", enviar_plantilla)

    async def listar(tenant_id: UUID, reserva_id: UUID) -> list[dict]:
        return [dict(a) for a in m.avisos.values()
                if a["tenant_id"] == tenant_id and a["reserva_id"] == reserva_id]

    monkeypatch.setattr(ac, "listar_avisos_de_reserva", listar)
    yield m
    app.dependency_overrides.pop(usuario_actual, None)


async def _cancelar(m: _Mundo, reserva_id: UUID, tenant: UUID = TENANT):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post(f"/api/tenants/{tenant}/calendario/reservas/{reserva_id}/cancelar", json={})


async def _avisos(reserva_id: UUID, tenant: UUID = TENANT):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get(f"/api/tenants/{tenant}/calendario/reservas/{reserva_id}/avisos")


# ------------------------------------------------------------
# Quién dispara el aviso y con qué resultado (A4, A5)
# ------------------------------------------------------------
@pytest.mark.parametrize("rol", ["owner", "superadmin", "proveedor"])
async def test_cancelacion_del_equipo_con_telefono_valido_hace_un_intento(mundo, rol):
    mundo.usuario = _usuario(rol)
    reserva_id = mundo.nueva_reserva()

    r = await _cancelar(mundo, reserva_id)

    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "cancelada"
    assert len(mundo.envios) == 1
    destinatario, plantilla, parametros = mundo.envios[0]
    assert destinatario == "+525512345678"
    assert plantilla == "notificacion_cancelacion"
    assert parametros[0] == "Corte" and len(parametros[1]) == 10
    [aviso] = mundo.del_tipo()
    assert aviso["estado"] == "enviado" and aviso["motivo"] is None
    assert aviso["telefono_destino"] == "+525512345678"
    assert len(mundo.eventos) == 1 and mundo.gerencia == []


async def test_member_no_puede_cancelar_y_no_deja_intento(mundo):
    mundo.usuario = _usuario("member")
    reserva_id = mundo.nueva_reserva()

    r = await _cancelar(mundo, reserva_id)

    assert r.status_code == 403
    assert mundo.cancelaciones == 0 and mundo.avisos == {} and mundo.envios == []


async def test_proveedor_no_dispara_aviso_sobre_la_cita_de_otro(mundo):
    mundo.usuario = _usuario("proveedor")
    reserva_id = mundo.nueva_reserva(proveedor_id=OTRO_PROVEEDOR_ID)

    r = await _cancelar(mundo, reserva_id)

    assert r.status_code == 403
    assert mundo.avisos == {} and mundo.envios == []


async def test_sin_telefono_queda_omitido_sin_llamar_al_proveedor_y_avisa_a_gerencia(mundo):
    reserva_id = mundo.nueva_reserva(telefono=None)

    r = await _cancelar(mundo, reserva_id)

    assert r.status_code == 200
    assert mundo.envios == []
    [aviso] = mundo.del_tipo()
    assert (aviso["estado"], aviso["motivo"]) == ("omitido", "sin_telefono")
    assert mundo.gerencia == [(reserva_id, "sin_telefono")]
    assert mundo.eventos == []


async def test_telefono_invalido_queda_fallido_sin_proveedor(mundo):
    reserva_id = mundo.nueva_reserva(telefono="abc")

    await _cancelar(mundo, reserva_id)

    assert mundo.envios == []
    [aviso] = mundo.del_tipo()
    assert (aviso["estado"], aviso["motivo"]) == ("fallido", "telefono_invalido")
    assert mundo.gerencia == [(reserva_id, "telefono_invalido")]


async def test_widget_queda_omitido_canal_no_soportado_sin_aviso_a_gerencia(mundo):
    reserva_id = mundo.nueva_reserva(canal="widget")

    await _cancelar(mundo, reserva_id)

    assert mundo.envios == []
    [aviso] = mundo.del_tipo()
    assert (aviso["estado"], aviso["motivo"]) == ("omitido", "canal_no_soportado")
    assert mundo.gerencia == []


async def test_instagram_con_telefono_valido_si_manda_plantilla(mundo):
    reserva_id = mundo.nueva_reserva(canal="instagram")
    mundo.conversacion = None  # sin conversación de WhatsApp (RF-15)

    await _cancelar(mundo, reserva_id)

    assert len(mundo.envios) == 1
    [aviso] = mundo.del_tipo()
    assert aviso["estado"] == "enviado" and aviso["sin_conversacion"] is True
    assert mundo.eventos == []


async def test_bandera_apagada_omite_sin_aviso_a_gerencia(mundo, monkeypatch):
    monkeypatch.setattr(settings, "AVISOS_CITA_WHATSAPP_ACTIVOS", False)
    reserva_id = mundo.nueva_reserva()

    await _cancelar(mundo, reserva_id)

    assert mundo.envios == []
    [aviso] = mundo.del_tipo()
    assert aviso["motivo"] == "desactivado_por_configuracion"
    assert mundo.gerencia == []


async def test_plantilla_no_aprobada_omite_y_avisa_a_gerencia(mundo):
    mundo.plantilla_estado = "REJECTED"
    reserva_id = mundo.nueva_reserva()

    await _cancelar(mundo, reserva_id)

    assert mundo.envios == []
    [aviso] = mundo.del_tipo()
    assert aviso["motivo"] == "plantilla_no_aprobada"
    assert mundo.gerencia == [(reserva_id, "plantilla_no_aprobada")]


async def test_funcion_apagada_no_envia_ni_registra_ni_avisa(mundo):
    mundo.habilitado = False  # D7: función apagada, calendario inactivo u oferta pendiente
    reserva_id = mundo.nueva_reserva()

    r = await _cancelar(mundo, reserva_id)

    assert r.status_code == 200 and mundo.reservas[reserva_id].estado == "cancelada"
    assert mundo.envios == [] and mundo.avisos == {} and mundo.gerencia == []


async def test_plantilla_pending_espera_sin_registro_ni_alerta(mundo):
    mundo.plantilla_estado = "PENDING"
    reserva_id = mundo.nueva_reserva()

    await _cancelar(mundo, reserva_id)

    assert mundo.envios == [] and mundo.avisos == {} and mundo.gerencia == []


async def test_servicio_sin_nombre_usa_el_respaldo_en_la_plantilla(mundo):
    reserva_id = mundo.nueva_reserva(servicio="   ")

    await _cancelar(mundo, reserva_id)

    assert mundo.envios[0][2][0] == "servicio agendado"


async def test_cita_ya_empezada_cancelada_por_el_equipo_si_manda_plantilla(mundo):
    reserva_id = mundo.nueva_reserva()
    mundo.datos_aviso[reserva_id]["hora_inicio"] = datetime.now(timezone.utc) - timedelta(minutes=20)

    await _cancelar(mundo, reserva_id)

    assert len(mundo.envios) == 1


# ------------------------------------------------------------
# Fallos: la cancelación nunca cambia (RF-8)
# ------------------------------------------------------------
async def test_fallo_del_proveedor_deja_fallido_y_la_reserva_cancelada(mundo):
    mundo.fallar_envio = True
    reserva_id = mundo.nueva_reserva()

    r = await _cancelar(mundo, reserva_id)

    assert r.status_code == 200 and mundo.reservas[reserva_id].estado == "cancelada"
    [aviso] = mundo.del_tipo()
    assert (aviso["estado"], aviso["motivo"]) == ("fallido", "error_proveedor")
    assert mundo.gerencia == [(reserva_id, "error_proveedor")]
    assert mundo.eventos == []


async def test_fallo_al_registrar_no_cambia_la_cancelacion(mundo):
    mundo.fallar_aviso = True
    reserva_id = mundo.nueva_reserva()

    r = await _cancelar(mundo, reserva_id)

    assert r.status_code == 200 and mundo.reservas[reserva_id].estado == "cancelada"
    assert mundo.envios == []


async def test_dos_ejecuciones_de_la_misma_cancelacion_envian_una_vez(mundo):
    reserva_id = mundo.nueva_reserva()
    await _cancelar(mundo, reserva_id)

    segunda = await ac.procesar_cancelacion(TENANT, reserva_id)

    assert len(mundo.envios) == 1 and len(mundo.del_tipo()) == 1
    assert segunda is not None and segunda.estado is None


async def test_cancelacion_rechazada_no_deja_intento(mundo, monkeypatch):
    async def rechaza(*args: Any, **kwargs: Any):
        raise svc_calendario.CancelacionRechazada("reserva_no_confirmada", "cancelada")

    monkeypatch.setattr(svc_calendario, "cancelar_reserva", rechaza)
    reserva_id = mundo.nueva_reserva()

    r = await _cancelar(mundo, reserva_id)

    assert r.status_code == 409
    assert mundo.avisos == {} and mundo.envios == []


async def test_reserva_no_cancelada_al_releer_no_envia(mundo):
    reserva_id = mundo.nueva_reserva()
    mundo.datos_aviso[reserva_id]["estado"] = "confirmada"

    assert await ac.procesar_cancelacion(TENANT, reserva_id) is None
    assert mundo.avisos == {} and mundo.envios == []


# ------------------------------------------------------------
# Cancelación del agente y reagendación: ningún intento (A4)
# ------------------------------------------------------------
def test_solo_el_endpoint_del_equipo_programa_el_aviso():
    router = (RAIZ / "routers" / "calendario.py").read_text(encoding="utf-8")
    assert router.count("procesar_cancelacion") == 1
    for relativo in ("routers/eventos.py", "services/calendario.py"):
        fuente = (RAIZ / relativo).read_text(encoding="utf-8")
        assert "avisos_cita" not in fuente, relativo


# ------------------------------------------------------------
# Aviso a gerencia: alerta personal a owner y superadmin, no a member
# ------------------------------------------------------------
async def test_aviso_a_gerencia_llega_como_alerta_personal_a_owner_y_superadmin(monkeypatch):
    owner, superadmin, member = uuid4(), uuid4(), uuid4()
    cuentas = [
        {"id": owner, "role": "owner"}, {"id": superadmin, "role": "superadmin"},
        {"id": member, "role": "member"},
    ]
    emitidas: list[UUID | None] = []

    async def fetch_all(sql: str, tenant_id: UUID, roles: list[str]):
        return [{"id": c["id"]} for c in cuentas if c["role"] in roles]

    async def broadcast(tenant_id, tipo, titulo, mensaje, datos=None, *, portal_user_id=None, **kw):
        assert tipo == "aviso_cita_no_enviado"
        emitidas.append(portal_user_id)

    monkeypatch.setattr(realtime, "fetch_all", fetch_all)
    monkeypatch.setattr(realtime, "broadcast_alerta", broadcast)

    await ac.avisar_gerencia_cancelacion(TENANT, uuid4(), "sin_telefono")

    assert sorted(emitidas) == sorted([owner, superadmin])
    assert member not in emitidas and None not in emitidas


# ------------------------------------------------------------
# GET .../avisos (RF-16, A11)
# ------------------------------------------------------------
@pytest.mark.parametrize("rol", ["owner", "superadmin"])
async def test_get_avisos_gerencia_ve_los_registros(mundo, rol):
    reserva_id = mundo.nueva_reserva()
    await _cancelar(mundo, reserva_id)
    mundo.usuario = _usuario(rol)

    r = await _avisos(reserva_id)

    assert r.status_code == 200
    [fila] = r.json()
    assert fila["tipo"] == "cancelacion" and fila["estado"] == "enviado"
    assert fila["reserva_id"] == str(reserva_id)
    assert set(fila) == {
        "id", "reserva_id", "tipo", "hora_inicio", "estado", "motivo",
        "sin_conversacion", "creado_en", "cerrado_en",
    }


@pytest.mark.parametrize("rol", ["member", "vendedor", "proveedor"])
async def test_get_avisos_otros_roles_403(mundo, rol):
    reserva_id = mundo.nueva_reserva()
    mundo.usuario = _usuario(rol)

    r = await _avisos(reserva_id)

    assert r.status_code == 403


async def test_get_avisos_de_reserva_de_otro_negocio_404(mundo):
    ajena = mundo.nueva_reserva(tenant_id=OTRO_TENANT)
    mundo.usuario = _usuario("owner")

    assert (await _avisos(ajena)).status_code == 404  # existe, pero en otro negocio
    assert (await _avisos(uuid4())).status_code == 404  # no existe: misma respuesta
    assert (await _avisos(ajena, tenant=OTRO_TENANT)).status_code == 404  # tenant ajeno en la ruta


async def test_get_avisos_sin_sesion_401(mundo):
    app.dependency_overrides.pop(usuario_actual, None)

    r = await _avisos(uuid4())

    assert r.status_code == 401
