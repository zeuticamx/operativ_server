"""
Aviso de cancelación con la plantilla del propio negocio (spec 007, T5a).

Sin BD ni red: el estado del aviso (`_estado_aviso`), el envío por tenant y el registro
se sustituyen por fakes en memoria. Se prueba lo que decide T5a: D7 (función apagada o
plantilla PENDING no registran ni alertan), rechazada/ausente -> omitido + alerta,
`sin_numero_emisor`, fallo de envío, `plantilla_no_disponible` marca la plantilla del
tenant, y que la cuenta global ya no interviene en los avisos de cita.
"""

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from config import settings
from services import avisos_cita as ac
from services import avisos_cita_reglas as reglas
from services import contexto_agente, plantillas_tenant
from services.avisos_cita_envio import ResultadoEnvioTenant

TENANT = uuid4()
INICIO = datetime(2026, 10, 20, 21, 0, tzinfo=timezone.utc)
RAIZ = Path(__file__).resolve().parent.parent


class _Mundo:
    def __init__(self) -> None:
        self.reserva_id = uuid4()
        self.telefono: str | None = "55 1234 5678"
        self.servicio = "Corte"
        self.canal: str | None = "whatsapp"
        self.avisos: dict[UUID, dict[str, Any]] = {}
        self.envios: list[tuple[UUID, str, str, list[dict[str, Any]]]] = []
        self.resultado = ResultadoEnvioTenant(True, None, "wamid.1")
        self.alertas: list[tuple[UUID, str]] = []
        self.ejecuciones_creadas = 0
        self.marcadas: list[tuple[UUID, str, str]] = []
        self.eventos: list[dict[str, Any]] = []
        # estado del aviso de cancelación del tenant
        self.habilitado = True
        self.nombre = "notificacion_cancelacion"
        self.plantilla_estado: str | None = "APPROVED"
        self.emisor_ok = True


@pytest.fixture
def mundo(monkeypatch: pytest.MonkeyPatch) -> _Mundo:
    m = _Mundo()
    monkeypatch.setattr(settings, "AVISOS_CITA_WHATSAPP_ACTIVOS", True)

    async def estado_aviso(tenant_id: UUID, tipo: str, **kw: Any) -> ac.EstadoAviso:
        assert tipo == "cancelacion" and tenant_id == TENANT
        return ac.EstadoAviso(m.habilitado, m.nombre, m.plantilla_estado, m.emisor_ok)

    async def crear_ejecucion(tipo: str) -> UUID:
        m.ejecuciones_creadas += 1
        return uuid4()

    async def terminar(ejecucion_id: UUID) -> None:
        return None

    async def datos(tenant_id: UUID, reserva_id: UUID) -> dict[str, Any]:
        return {
            "estado": "cancelada", "hora_inicio": INICIO, "cliente_telefono": m.telefono,
            "user_id": uuid4(), "servicio_nombre": m.servicio, "channel_type": m.canal,
        }

    async def servicios(tenant_id: UUID) -> Any:
        return types.SimpleNamespace(zona_horaria="America/Mexico_City")

    async def registrar_intento(*, tenant_id, reserva_id, tipo, hora_inicio, ejecucion_id,
                                estado="en_curso", motivo=None, sin_conversacion=False):
        aviso_id = uuid4()
        m.avisos[aviso_id] = {"tipo": tipo, "estado": estado, "motivo": motivo}
        return aviso_id

    async def cerrar_registro(aviso_id, estado, motivo=None, telefono_destino=None):
        a = m.avisos[aviso_id]
        if a["estado"] != "en_curso":
            return False
        a.update(estado=estado, motivo=motivo)
        return True

    async def alerta(tenant_id: UUID, reserva_id: UUID, motivo: str) -> None:
        m.alertas.append((reserva_id, motivo))

    async def conversacion(tenant_id: UUID, user_id: Any) -> UUID | None:
        return None

    async def evento(**kw: Any) -> bool:
        m.eventos.append(kw)
        return True

    async def enviar(tenant_id: UUID, telefono: str, nombre: str, parametros: list, **kw: Any):
        m.envios.append((tenant_id, telefono, nombre, parametros))
        return m.resultado

    async def marcar(tenant_id: UUID, nombre: str, estado: str = "DISABLED", **kw: Any):
        m.marcadas.append((tenant_id, nombre, estado))
        return None

    monkeypatch.setattr(ac, "_estado_aviso", estado_aviso)
    monkeypatch.setattr(ac, "crear_ejecucion", crear_ejecucion)
    monkeypatch.setattr(ac, "terminar_ejecucion", terminar)
    monkeypatch.setattr(ac, "_datos_cancelacion", datos)
    monkeypatch.setattr(ac, "get_tenant_servicios", servicios)
    monkeypatch.setattr(ac, "registrar_intento", registrar_intento)
    monkeypatch.setattr(ac, "cerrar_registro", cerrar_registro)
    monkeypatch.setattr(ac, "avisar_gerencia_cancelacion", alerta)
    monkeypatch.setattr(contexto_agente, "conversacion_whatsapp_del_cliente", conversacion)
    monkeypatch.setattr(contexto_agente, "registrar_evento", evento)
    monkeypatch.setattr(ac.avisos_cita_envio, "enviar_plantilla_tenant", enviar)
    monkeypatch.setattr(ac.plantillas_tenant, "marcar_estado_por_error", marcar)
    return m


async def _cancelar(m: _Mundo):
    return await ac.procesar_cancelacion(TENANT, m.reserva_id)


# ------------------------------------------------------------------ envío feliz

async def test_cancelacion_usa_la_plantilla_vigente_del_tenant_y_sus_parametros(mundo):
    mundo.nombre = "notificacion_cancelacion_v2"

    res = await _cancelar(mundo)

    assert res is not None and res.enviado
    [(tenant, telefono, nombre, params)] = mundo.envios
    assert tenant == TENANT and telefono == "+525512345678"
    assert nombre == "notificacion_cancelacion_v2"
    assert params == reglas.parametros_de_envio("cancelacion", "", "Corte", "20-10-2026")
    [aviso] = mundo.avisos.values()
    assert (aviso["estado"], aviso["motivo"]) == ("enviado", None)
    assert mundo.alertas == []


async def test_cancelacion_sin_servicio_usa_el_respaldo(mundo):
    mundo.servicio = "  "
    await _cancelar(mundo)
    assert mundo.envios[0][3][0]["parameters"][0]["text"] == "servicio agendado"


# ------------------------------------------------------------------ D7

async def test_funcion_apagada_no_envia_no_registra_ni_alerta(mundo):
    mundo.habilitado = False

    assert await _cancelar(mundo) is None

    assert mundo.avisos == {} and mundo.envios == [] and mundo.alertas == []


async def test_plantilla_pending_no_envia_no_registra_ni_alerta(mundo):
    mundo.plantilla_estado = "PENDING"

    assert await _cancelar(mundo) is None

    assert mundo.avisos == {} and mundo.envios == [] and mundo.alertas == []
    assert mundo.ejecuciones_creadas == 0  # tampoco deja fila en avisos_cita_ejecuciones


# ------------------------------------------------------------------ rechazada / ausente / emisor / fallo

@pytest.mark.parametrize("estado", ["REJECTED", "PAUSED", "DISABLED", None])
async def test_plantilla_no_aprobada_o_ausente_omite_y_alerta(mundo, estado):
    mundo.plantilla_estado = estado

    await _cancelar(mundo)

    [aviso] = mundo.avisos.values()
    assert (aviso["estado"], aviso["motivo"]) == ("omitido", "plantilla_no_aprobada")
    assert mundo.alertas == [(mundo.reserva_id, "plantilla_no_aprobada")]
    assert mundo.envios == []


async def test_sin_numero_emisor_omite_y_alerta(mundo):
    mundo.emisor_ok = False

    await _cancelar(mundo)

    [aviso] = mundo.avisos.values()
    assert (aviso["estado"], aviso["motivo"]) == ("omitido", "sin_numero_emisor")
    assert mundo.alertas == [(mundo.reserva_id, "sin_numero_emisor")]
    assert mundo.envios == []


async def test_sin_numero_emisor_al_enviar_cierra_omitido_y_alerta(mundo):
    mundo.resultado = ResultadoEnvioTenant(False, "sin_numero_emisor")

    res = await _cancelar(mundo)

    [aviso] = mundo.avisos.values()
    assert (aviso["estado"], aviso["motivo"]) == ("omitido", "sin_numero_emisor")
    assert res is not None and res.estado == "omitido"
    assert mundo.alertas == [(mundo.reserva_id, "sin_numero_emisor")]
    assert mundo.eventos == []


async def test_fallo_de_envio_deja_fallido_y_alerta(mundo):
    mundo.resultado = ResultadoEnvioTenant(False, "error_proveedor", None, 401)

    await _cancelar(mundo)

    [aviso] = mundo.avisos.values()
    assert (aviso["estado"], aviso["motivo"]) == ("fallido", "error_proveedor")
    assert mundo.alertas == [(mundo.reserva_id, "error_proveedor")]
    assert mundo.marcadas == []
    assert mundo.eventos == []


async def test_excepcion_inesperada_al_enviar_deja_fallido(mundo, monkeypatch):
    async def explota(*a: Any, **k: Any):
        raise ValueError("bug")

    monkeypatch.setattr(ac.avisos_cita_envio, "enviar_plantilla_tenant", explota)

    await _cancelar(mundo)

    [aviso] = mundo.avisos.values()
    assert (aviso["estado"], aviso["motivo"]) == ("fallido", "error_proveedor")


# ------------------------------------------------------------------ plantilla_no_disponible marca la del tenant

@pytest.mark.parametrize(
    "codigo, estado", [(132015, "PAUSED"), (132016, "DISABLED")]
)
async def test_plantilla_no_disponible_con_codigo_de_meta_marca_la_plantilla_del_tenant(mundo, codigo, estado):
    mundo.nombre = "notificacion_cancelacion_v3"
    mundo.resultado = ResultadoEnvioTenant(
        False, "plantilla_no_disponible", None, 500, f'{{"error": {{"details": {{"code": {codigo}}}}}}}'
    )

    await _cancelar(mundo)

    assert mundo.marcadas == [(TENANT, "notificacion_cancelacion_v3", estado)]
    [aviso] = mundo.avisos.values()
    assert (aviso["estado"], aviso["motivo"]) == ("fallido", "plantilla_no_disponible")
    assert mundo.alertas == [(mundo.reserva_id, "plantilla_no_disponible")]


@pytest.mark.parametrize("cuerpo", [None, '{"error": {"code": 132001}}', '{"error": {"code": "META_API_ERROR"}}'])
async def test_plantilla_no_disponible_sin_codigo_reconocible_no_toca_la_plantilla(mundo, cuerpo):
    mundo.resultado = ResultadoEnvioTenant(False, "plantilla_no_disponible", None, 500, cuerpo)

    await _cancelar(mundo)

    assert mundo.marcadas == []
    [aviso] = mundo.avisos.values()
    assert aviso["motivo"] == "plantilla_no_disponible"


async def test_un_fallo_al_marcar_la_plantilla_no_impide_cerrar_el_registro(mundo, monkeypatch):
    async def roto(*a: Any, **k: Any):
        raise RuntimeError("BD caída")

    monkeypatch.setattr(ac.plantillas_tenant, "marcar_estado_por_error", roto)
    mundo.resultado = ResultadoEnvioTenant(False, "plantilla_no_disponible", None, 500, '{"code": 132015}')

    await _cancelar(mundo)

    [aviso] = mundo.avisos.values()
    assert (aviso["estado"], aviso["motivo"]) == ("fallido", "plantilla_no_disponible")


def test_estado_por_error_de_envio_es_puro():
    f = plantillas_tenant.estado_por_error_de_envio
    assert f('{"error": {"details": {"code": 132015}}}') == "PAUSED"
    assert f('{"code": 132016}') == "DISABLED"
    assert f('{"code": 132001}') is None
    assert f(None) is None


# ------------------------------------------------------------------ orden RF-7 se conserva

async def test_sin_telefono_sigue_siendo_omitido_con_alerta(mundo):
    mundo.telefono = None
    await _cancelar(mundo)
    [aviso] = mundo.avisos.values()
    assert (aviso["estado"], aviso["motivo"]) == ("omitido", "sin_telefono")
    assert mundo.alertas == [(mundo.reserva_id, "sin_telefono")]


async def test_widget_sigue_siendo_omitido_sin_alerta(mundo):
    mundo.canal = "widget"
    await _cancelar(mundo)
    [aviso] = mundo.avisos.values()
    assert aviso["motivo"] == "canal_no_soportado"
    assert mundo.alertas == [] and mundo.envios == []


async def test_bandera_global_apagada_sigue_omitiendo_sin_alerta(mundo, monkeypatch):
    monkeypatch.setattr(settings, "AVISOS_CITA_WHATSAPP_ACTIVOS", False)
    await _cancelar(mundo)
    [aviso] = mundo.avisos.values()
    assert aviso["motivo"] == "desactivado_por_configuracion"
    assert mundo.alertas == [] and mundo.envios == []


# ------------------------------------------------------------------ mensajes de gerencia

def test_mensajes_de_gerencia_una_variante_por_motivo_sin_cuenta_global():
    for motivo in ("plantilla_no_aprobada", "plantilla_no_disponible", "sin_numero_emisor", "error_proveedor"):
        assert motivo in ac._MENSAJE_GERENCIA
    assert (
        ac._MENSAJE_GERENCIA["plantilla_no_aprobada"]
        == "la plantilla de cancelación de tu negocio no está aprobada."
    )
    assert len({ac._MENSAJE_GERENCIA[m] for m in ac._MENSAJE_GERENCIA}) == len(ac._MENSAJE_GERENCIA)
    for texto in ac._MENSAJE_GERENCIA.values():
        assert "cuenta de avisos" not in texto and "OperativAI" not in texto and "global" not in texto.lower()


async def test_la_alerta_real_lleva_el_texto_del_ejemplo_de_la_spec(monkeypatch):
    emitidas: list[tuple[Any, ...]] = []

    async def avisar(*args: Any) -> None:
        emitidas.append(args)

    falso = types.ModuleType("realtime")
    falso.avisar_gerencia = avisar  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "realtime", falso)

    await ac.avisar_gerencia_cancelacion(TENANT, uuid4(), "plantilla_no_aprobada")

    [(tenant, tipo, titulo, mensaje, datos)] = emitidas
    assert tipo == "aviso_cita_no_enviado"
    assert titulo == "No se pudo avisar al cliente de la cancelación"
    assert mensaje.endswith("la plantilla de cancelación de tu negocio no está aprobada.")
    assert datos["motivo"] == "plantilla_no_aprobada"


# ------------------------------------------------------------------ _estado_aviso (real, con acceso falso)

class _AccesoFalso:
    def __init__(self, *, calendario=True, config=None, plantilla=None, emisor=True):
        self.calendario, self.config, self.fila, self.emisor = calendario, config, plantilla, emisor
        self.consultas: list[str] = []

    async def calendario_activo(self, tenant_id: UUID) -> bool:
        return self.calendario

    async def leer_config(self, tenant_id: UUID):
        return self.config

    async def plantilla(self, tenant_id: UUID, nombre: str):
        self.consultas.append(nombre)
        return self.fila

    async def emisor_ok(self, tenant_id: UUID) -> bool:
        self.consultas.append("emisor")
        return self.emisor


async def test_estado_aviso_lee_nombre_vigente_estado_y_emisor():
    a = _AccesoFalso(
        config={"oferta": "aceptada", "cancelacion_activo": True,
                "nombre_plantilla_cancelacion": "notificacion_cancelacion_v2"},
        plantilla={"estado": "APPROVED"},
    )
    e = await ac._estado_aviso(TENANT, "cancelacion", acceso=a)
    assert e == ac.EstadoAviso(True, "notificacion_cancelacion_v2", "APPROVED", True)
    assert a.consultas[0] == "notificacion_cancelacion_v2"


@pytest.mark.parametrize("calendario, activo", [(False, True), (True, False)])
async def test_estado_aviso_no_habilitado_con_calendario_o_funcion_apagados(calendario, activo):
    a = _AccesoFalso(
        calendario=calendario,
        config={"oferta": "aceptada", "cancelacion_activo": activo},
        plantilla={"estado": "APPROVED"},
    )
    e = await ac._estado_aviso(TENANT, "cancelacion", acceso=a)
    assert e.habilitado is False
    assert "emisor" not in a.consultas  # no se gastan consultas si no aplica


async def test_estado_aviso_sin_fila_de_config_es_oferta_pendiente_no_habilitado():
    e = await ac._estado_aviso(TENANT, "cancelacion", acceso=_AccesoFalso(config=None))
    assert e.habilitado is False


async def test_estado_aviso_plantilla_ausente_es_estado_none():
    a = _AccesoFalso(config={"oferta": "aceptada", "cancelacion_activo": True}, plantilla=None)
    e = await ac._estado_aviso(TENANT, "cancelacion", acceso=a)
    assert e.habilitado and e.plantilla_estado is None and e.nombre_plantilla == "notificacion_cancelacion"


# ------------------------------------------------------------------ la cuenta global ya no interviene

def test_avisos_cita_no_usa_la_cuenta_global_ni_el_bool_legado():
    fuente = (RAIZ / "services" / "avisos_cita.py").read_text(encoding="utf-8")
    codigo = "\n".join(l for l in fuente.splitlines() if not l.strip().startswith("#"))
    for prohibido in ("cuenta_global_wa", "esta_aprobada", "plantilla_aprobada", "aplicar_error_envio"):
        assert prohibido not in codigo, prohibido


def test_reglas_ya_no_acepta_el_parametro_legado_plantilla_aprobada():
    with pytest.raises(TypeError):
        reglas.motivo_cancelacion(  # type: ignore[call-arg]
            telefono=reglas.EstadoTelefono.VALIDO, canal_ok=True, activo=True, plantilla_aprobada=True
        )
    with pytest.raises(TypeError):
        reglas.motivo_recordatorio(  # type: ignore[call-arg]
            telefono=reglas.EstadoTelefono.VALIDO, canal_ok=True, estado_cita="confirmada",
            hora_coincide=True, inicio_llegado=False, activo=True, plantilla_aprobada=True,
        )
