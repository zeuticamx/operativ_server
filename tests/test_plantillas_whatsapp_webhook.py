"""
Eventos de estado de plantilla en el webhook de NeuroAPI (spec 004, T8).

Sin BD ni red: el estado vive en un almacén en memoria y el reenvío a n8n es un
registro de llamadas. La firma HMAC es la de siempre; el evento nuevo no la
debilita (firma inválida sigue dando 401).
"""

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from config import settings
from main import app
from services import entrada_mensajes
from services import plantillas_whatsapp as pw

RUTA = "/api/canales/whatsapp/neuroapi/webhook"
SECRETO = "secreto-de-prueba-plantillas"


def _firmar(cuerpo: bytes) -> str:
    return "sha256=" + hmac.new(SECRETO.encode(), cuerpo, hashlib.sha256).hexdigest()


def _estado(nombre: str, evento: str, *, campo: str = "message_template_status_update") -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "waba-1", "changes": [{
            "field": campo,
            "value": {"event": evento, "message_template_name": nombre,
                      "message_template_id": 123, "message_template_language": "es_MX"},
        }]}],
    }


class _Mundo:
    def __init__(self) -> None:
        self.estados = {n: "no_solicitada" for n in pw.NOMBRES_PLANTILLA}
        self.cambios: list[dict[str, Any]] = []
        self.auditoria: list[dict[str, Any]] = []
        self.reenvios: list[bytes] = []
        self.falla_bd = False

    async def post(self, cuerpo: dict[str, Any] | bytes, *, firma: str | None = "valida"):
        crudo = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo).encode()
        cabeceras = {"content-type": "application/json"}
        if firma is not None:
            cabeceras["x-hub-signature-256"] = _firmar(crudo) if firma == "valida" else firma
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return await c.post(RUTA, content=crudo, headers=cabeceras)


@pytest.fixture
def mundo(monkeypatch: pytest.MonkeyPatch) -> _Mundo:
    m = _Mundo()
    monkeypatch.setattr(settings, "NEUROAPI_CONNECT_WEBHOOK_SECRET", SECRETO)
    # T6: el handler global solo aplica eventos del WABA de la cuenta global (los de _estado()).
    monkeypatch.setattr(settings, "AVISOS_CUENTA_GLOBAL_WABA_ID", "waba-1")

    class _Conexion:
        async def fetchrow(self, sql: str, nombre: str):
            return {"estado": m.estados[nombre]} if nombre in m.estados else None

        async def execute(self, sql: str, nombre: str, estado: str, nota: str | None, por: Any):
            if m.falla_bd:
                raise RuntimeError("BD caída")
            m.estados[nombre] = estado
            m.cambios.append({"nombre": nombre, "estado": estado, "nota": nota, "por": por})

    @asynccontextmanager
    async def transaccion():
        yield _Conexion()

    async def obtener_estado(nombre: str) -> str:
        return m.estados[nombre]

    async def registrar_auditoria(**kw: Any) -> None:
        m.auditoria.append(kw)

    async def tenant_por_numero(phone_number_id: str):
        return uuid4()

    async def reenviar(cuerpo_crudo: bytes, tenant_id: Any, destino: str, **_: Any) -> None:
        m.reenvios.append(cuerpo_crudo)

    monkeypatch.setattr(pw, "transaccion", transaccion)
    monkeypatch.setattr(pw, "obtener_estado", obtener_estado)
    monkeypatch.setattr(pw, "registrar_auditoria", registrar_auditoria)
    monkeypatch.setattr(entrada_mensajes, "tenant_por_numero", tenant_por_numero)
    monkeypatch.setattr(entrada_mensajes, "reenviar", reenviar)
    monkeypatch.setattr(entrada_mensajes, "destino_para", lambda tenant_id: "http://n8n.invalid/entrada")
    return m


# ------------------------------------------------------------
# Traducción de estados
# ------------------------------------------------------------
@pytest.mark.parametrize(
    "evento, esperado",
    [
        ("APPROVED", "aprobada"),
        ("REJECTED", "rechazada"),
        ("DISABLED", "rechazada"),
        ("PAUSED", "pausada"),
        ("PENDING", "pendiente_aprobacion"),
    ],
)
async def test_cada_estado_de_meta_cambia_la_plantilla_esperada(mundo, evento, esperado):
    r = await mundo.post(_estado("recordatorio_cita", evento))
    assert r.status_code == 200 and r.json() == {"recibido": True}
    assert mundo.estados["recordatorio_cita"] == esperado
    assert mundo.estados["notificacion_cancelacion"] == "no_solicitada"
    assert mundo.cambios == [{"nombre": "recordatorio_cita", "estado": esperado,
                              "nota": "webhook NeuroAPI", "por": None}]
    [aud] = mundo.auditoria
    assert aud["accion"] == "plantilla_whatsapp_estado" and aud["tenant_id"] is None
    assert aud["actor_email"] == "sistema:neuroapi_webhook" and aud["actor_portal_user_id"] is None
    assert aud["detalle"] == {"plantilla": "recordatorio_cita", "anterior": "no_solicitada",
                              "nuevo": esperado, "origen": "evento_meta"}
    assert mundo.reenvios == []


async def test_el_evento_es_insensible_a_mayusculas(mundo):
    await mundo.post(_estado("notificacion_cancelacion", "approved"))
    assert mundo.estados["notificacion_cancelacion"] == "aprobada"


async def test_plantilla_ajena_no_cambia_nada_ni_se_reenvia(mundo):
    r = await mundo.post(_estado("promo_navidad", "APPROVED"))
    assert r.status_code == 200
    assert mundo.cambios == [] and mundo.auditoria == [] and mundo.reenvios == []


async def test_evento_desconocido_no_cambia_nada(mundo):
    r = await mundo.post(_estado("recordatorio_cita", "PENDING_DELETION"))
    assert r.status_code == 200 and mundo.cambios == [] and mundo.reenvios == []


async def test_mismo_estado_no_escribe_ni_audita(mundo):
    mundo.estados["recordatorio_cita"] = "aprobada"
    await mundo.post(_estado("recordatorio_cita", "APPROVED"))
    assert mundo.cambios == [] and mundo.auditoria == []


async def test_account_update_solo_se_registra_y_no_se_reenvia(mundo):
    cuerpo = {"entry": [{"changes": [{"field": "account_update",
                                      "value": {"event": "ACCOUNT_VIOLATION"}}]}]}
    r = await mundo.post(cuerpo)
    assert r.status_code == 200
    assert mundo.cambios == [] and mundo.auditoria == [] and mundo.reenvios == []
    assert set(mundo.estados.values()) == {"no_solicitada"}


async def test_dos_plantillas_en_un_mismo_sobre_se_aplican_las_dos(mundo):
    cuerpo = _estado("recordatorio_cita", "APPROVED")
    cuerpo["entry"][0]["changes"].append(
        _estado("notificacion_cancelacion", "PAUSED")["entry"][0]["changes"][0]
    )
    await mundo.post(cuerpo)
    assert mundo.estados == {"recordatorio_cita": "aprobada", "notificacion_cancelacion": "pausada"}


async def test_un_fallo_de_bd_no_rompe_la_respuesta(mundo):
    mundo.falla_bd = True
    r = await mundo.post(_estado("recordatorio_cita", "APPROVED"))
    assert r.status_code == 200 and mundo.reenvios == []


# ------------------------------------------------------------
# Lo que no cambia
# ------------------------------------------------------------
async def test_firma_invalida_sigue_dando_401_y_no_cambia_nada(mundo):
    r = await mundo.post(_estado("recordatorio_cita", "APPROVED"), firma="sha256=falsa")
    assert r.status_code == 401
    assert (await mundo.post(_estado("recordatorio_cita", "APPROVED"), firma=None)).status_code == 401
    assert mundo.cambios == []


async def test_sin_secreto_configurado_no_atiende(mundo, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "NEUROAPI_CONNECT_WEBHOOK_SECRET", "")
    r = await mundo.post(_estado("recordatorio_cita", "APPROVED"))
    assert r.status_code == 503 and mundo.cambios == []


async def test_un_mensaje_normal_sigue_yendo_a_n8n(mundo):
    mensaje = {"entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "111222333"},
        "messages": [{"from": "5215512345678", "text": {"body": "hola"}}],
    }}]}]}
    r = await mundo.post(mensaje)
    assert r.status_code == 200
    assert len(mundo.reenvios) == 1 and mundo.cambios == []


async def test_sobre_mezclado_con_mensajes_no_se_intercepta(mundo):
    mezclado = _estado("recordatorio_cita", "APPROVED")
    mezclado["entry"][0]["changes"].append({"field": "messages", "value": {
        "metadata": {"phone_number_id": "111222333"}, "messages": [{"from": "1", "text": {"body": "x"}}],
    }})
    await mundo.post(mezclado)
    assert len(mundo.reenvios) == 1 and mundo.cambios == []


def test_account_update_de_alta_de_linea_no_se_intercepta():
    alta = {"entry": [{"changes": [{"field": "account_update", "value": {
        "event": "whatsapp.connected", "session_id": "sess_1", "phone_number_id": "1"}}]}]}
    assert pw.es_evento_de_plantilla(alta) is False


def test_es_evento_de_plantilla_tolera_basura():
    for basura in (None, [], "x", {}, {"entry": None}, {"entry": [1, {"changes": "x"}]}):
        assert pw.es_evento_de_plantilla(basura) is False
