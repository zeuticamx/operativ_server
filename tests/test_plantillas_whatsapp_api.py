"""
API de gerencia de plataforma para el estado de las plantillas (spec 004, T8).

Sin BD ni red: `transaccion`, `fetch_all` y `registrar_auditoria` del servicio se
sustituyen por un almacén en memoria; `usuario_actual` se sustituye por el
usuario del caso. Cubre A23 (quién ve y cambia), RF-18 (reintento) y A25 (tras
pasar a `pausada`, un recordatorio nuevo de cualquier negocio queda omitido).
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from config import settings
from deps import UsuarioActual, usuario_actual
from main import app
from services import avisos_cita as ac
from services import contexto_agente, cuenta_global_wa
from services import plantillas_whatsapp as pw

RUTA = "/api/gerencia/plantillas-whatsapp"
TENANT = uuid4()


class _Conexion:
    def __init__(self, bd: "_BD") -> None:
        self.bd = bd

    async def fetchrow(self, sql: str, nombre: str) -> dict[str, Any] | None:
        assert "plantillas_whatsapp_globales" in sql and "FOR UPDATE" in sql
        fila = self.bd.filas.get(nombre)
        return {"estado": fila["estado"]} if fila else None

    async def execute(self, sql: str, nombre: str, estado: str, nota: str | None, por: UUID | None):
        assert "UPDATE plantillas_whatsapp_globales" in sql
        self.bd.filas[nombre].update(
            estado=estado, nota=nota, actualizada_por=por, actualizada_en=datetime.now(timezone.utc)
        )


class _BD:
    def __init__(self) -> None:
        ahora = datetime.now(timezone.utc)
        self.filas: dict[str, dict[str, Any]] = {
            n: {"nombre": n, "estado": "no_solicitada", "actualizada_en": ahora,
                "actualizada_por": None, "actualizada_por_email": None, "nota": None}
            for n in pw.NOMBRES_PLANTILLA
        }
        self.auditoria: list[dict[str, Any]] = []
        self.transacciones = 0
        self.usuario: UsuarioActual = _usuario("gerencia")

    def instalar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bd = self

        @asynccontextmanager
        async def transaccion():
            bd.transacciones += 1
            yield _Conexion(bd)

        async def fetch_all(sql: str) -> list[dict[str, Any]]:
            return [dict(f) for f in sorted(bd.filas.values(), key=lambda f: f["nombre"])]

        async def registrar_auditoria(**kw: Any) -> None:
            assert kw["conn"] is not None  # misma transacción que el cambio
            bd.auditoria.append(kw)

        monkeypatch.setattr(pw, "transaccion", transaccion)
        monkeypatch.setattr(pw, "fetch_all", fetch_all)
        monkeypatch.setattr(pw, "registrar_auditoria", registrar_auditoria)


def _usuario(tipo: str) -> UsuarioActual:
    if tipo == "gerencia":
        return UsuarioActual(id=uuid4(), tenant_id=TENANT, email="g@operativai.com",
                             role="owner", es_gerencia_plataforma=True)
    if tipo == "ver_como":  # deps fuerza es_gerencia_plataforma=False al impersonar
        return UsuarioActual(id=uuid4(), tenant_id=TENANT, email="g@operativai.com", role="owner",
                             es_gerencia_plataforma=False, impersonado_por="g@operativai.com",
                             impersonado_por_id=uuid4())
    return UsuarioActual(id=uuid4(), tenant_id=TENANT, email=f"{tipo}@negocio.com", role=tipo)


@pytest.fixture
def bd(monkeypatch: pytest.MonkeyPatch):
    b = _BD()
    b.instalar(monkeypatch)

    async def _usuario_actual() -> UsuarioActual:
        return b.usuario

    app.dependency_overrides[usuario_actual] = _usuario_actual
    yield b
    app.dependency_overrides.pop(usuario_actual, None)


async def _llamar(metodo: str, ruta: str, **kw: Any):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(metodo, ruta, **kw)


# ------------------------------------------------------------
# A23: quién puede
# ------------------------------------------------------------
@pytest.mark.parametrize("tipo", ["owner", "superadmin", "member", "vendedor", "proveedor", "ver_como"])
async def test_solo_gerencia_de_plataforma_lee_y_cambia(bd, tipo):
    bd.usuario = _usuario(tipo)
    nombre = pw.PLANTILLA_RECORDATORIO
    assert (await _llamar("GET", RUTA)).status_code == 403
    assert (await _llamar("PATCH", f"{RUTA}/{nombre}", json={"estado": "aprobada"})).status_code == 403
    assert (await _llamar("POST", f"{RUTA}/{nombre}/reintentar")).status_code == 403
    assert bd.filas[nombre]["estado"] == "no_solicitada" and bd.auditoria == []


async def test_gerencia_lista_las_dos_plantillas(bd):
    r = await _llamar("GET", RUTA)
    assert r.status_code == 200
    datos = r.json()
    assert [d["nombre"] for d in datos] == ["notificacion_cancelacion", "recordatorio_cita"]
    assert all(d["estado"] == "no_solicitada" and d["puede_reintentar"] is True for d in datos)


# ------------------------------------------------------------
# PATCH manual
# ------------------------------------------------------------
async def test_patch_cambia_el_estado_y_deja_auditoria(bd):
    nombre = pw.PLANTILLA_CANCELACION
    r = await _llamar("PATCH", f"{RUTA}/{nombre}", json={"estado": "aprobada", "nota": "aprobada en Meta"})
    assert r.status_code == 200
    assert r.json()["estado"] == "aprobada" and r.json()["puede_reintentar"] is False
    fila = bd.filas[nombre]
    assert fila["estado"] == "aprobada" and fila["nota"] == "aprobada en Meta"
    assert fila["actualizada_por"] == bd.usuario.id
    [aud] = bd.auditoria
    assert aud["accion"] == "plantilla_whatsapp_estado" and aud["actor_email"] == "g@operativai.com"
    assert aud["actor_portal_user_id"] == bd.usuario.id and aud["tenant_id"] is None
    assert aud["detalle"] == {"plantilla": nombre, "anterior": "no_solicitada",
                              "nuevo": "aprobada", "origen": "manual"}
    # La otra plantilla no se toca: cada nombre tiene su estado.
    assert bd.filas[pw.PLANTILLA_RECORDATORIO]["estado"] == "no_solicitada"


async def test_patch_rechaza_estado_invalido_y_nombre_desconocido(bd):
    assert (await _llamar("PATCH", f"{RUTA}/recordatorio_cita", json={"estado": "borrada"})).status_code == 422
    r = await _llamar("PATCH", f"{RUTA}/otra_plantilla", json={"estado": "aprobada"})
    assert r.status_code == 404
    assert bd.auditoria == []


# ------------------------------------------------------------
# POST reintentar (RF-18)
# ------------------------------------------------------------
@pytest.mark.parametrize("estado", ["no_solicitada", "rechazada", "pausada"])
async def test_reintentar_pasa_a_pendiente_si_no_esta_aprobada_ni_pendiente(bd, estado):
    nombre = pw.PLANTILLA_RECORDATORIO
    bd.filas[nombre]["estado"] = estado
    r = await _llamar("POST", f"{RUTA}/{nombre}/reintentar")
    assert r.status_code == 200 and r.json()["estado"] == "pendiente_aprobacion"
    assert bd.auditoria[0]["detalle"]["anterior"] == estado
    assert bd.auditoria[0]["detalle"]["origen"] == "reintento"


@pytest.mark.parametrize("estado", ["aprobada", "pendiente_aprobacion"])
async def test_reintentar_con_aprobada_o_pendiente_da_409_sin_cambios(bd, estado):
    nombre = pw.PLANTILLA_RECORDATORIO
    bd.filas[nombre]["estado"] = estado
    r = await _llamar("POST", f"{RUTA}/{nombre}/reintentar")
    assert r.status_code == 409
    assert bd.filas[nombre]["estado"] == estado and bd.auditoria == []


async def test_reintentar_nombre_desconocido_da_404(bd):
    assert (await _llamar("POST", f"{RUTA}/otra/reintentar")).status_code == 404


# ------------------------------------------------------------
# A25: pausar la plantilla frena los recordatorios de cualquier negocio
# ------------------------------------------------------------
async def test_tras_pausar_un_recordatorio_nuevo_queda_omitido(bd, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "AVISOS_CITA_WHATSAPP_ACTIVOS", True)
    avisos: dict[UUID, dict[str, Any]] = {}
    envios: list[Any] = []
    reserva = {
        "reserva_id": uuid4(), "tenant_id": uuid4(),  # un negocio cualquiera
        "hora_inicio": datetime.now(timezone.utc) + timedelta(hours=3), "estado": "confirmada",
        "cliente_telefono": "55 1234 5678", "user_id": uuid4(), "servicio_nombre": "Corte",
        "nombre_negocio": "Barbería Sol", "channel_type": "whatsapp",
    }

    async def sin_ejecuciones(*a: Any, **k: Any):
        return uuid4() if a or k else None

    async def nada(*a: Any, **k: Any) -> list[UUID]:
        return []

    async def candidatos(ahora: datetime):
        hechos = {(a["reserva_id"], a["hora_inicio"]) for a in avisos.values()}
        return [dict(reserva)] if (reserva["reserva_id"], reserva["hora_inicio"]) not in hechos else []

    async def registrar_intento(*, tenant_id, reserva_id, tipo, hora_inicio, ejecucion_id,
                                estado="en_curso", motivo=None, sin_conversacion=False):
        aviso_id = uuid4()
        avisos[aviso_id] = {"reserva_id": reserva_id, "hora_inicio": hora_inicio,
                            "estado": estado, "motivo": motivo}
        return aviso_id

    async def cerrar_registro(aviso_id, estado, motivo=None, telefono_destino=None):
        avisos[aviso_id].update(estado=estado, motivo=motivo)
        return True

    async def datos(tenant_id: UUID, reserva_id: UUID):
        return dict(reserva)

    async def leer_estado(nombre: str) -> str:
        return bd.filas[nombre]["estado"]

    async def enviar(*a: Any) -> None:
        envios.append(a)

    async def servicios(tenant_id: UUID):
        return SimpleNamespace(zona_horaria="America/Mexico_City")

    async def conversacion(tenant_id: UUID, user_id: UUID | None):
        return None

    async def terminar(ejecucion_id: UUID) -> None:
        return None

    for nombre_fn, fn in [
        ("crear_ejecucion", sin_ejecuciones), ("terminar_ejecucion", terminar),
        ("latir_ejecucion", terminar), ("recuperar_huerfanos", nada),
        ("_candidatos_recordatorio", candidatos), ("_datos_reserva_recordatorio", datos),
        ("registrar_intento", registrar_intento), ("cerrar_registro", cerrar_registro),
        ("get_tenant_servicios", servicios),
    ]:
        monkeypatch.setattr(ac, nombre_fn, fn)
    monkeypatch.setattr(contexto_agente, "conversacion_whatsapp_del_cliente", conversacion)
    monkeypatch.setattr(pw, "obtener_estado", leer_estado)
    monkeypatch.setattr(cuenta_global_wa, "enviar_plantilla", enviar)

    r = await _llamar("PATCH", f"{RUTA}/recordatorio_cita", json={"estado": "pausada"})
    assert r.status_code == 200

    await ac.procesar_recordatorios()

    [aviso] = avisos.values()
    assert (aviso["estado"], aviso["motivo"]) == ("omitido", "plantilla_no_aprobada")
    assert envios == []
