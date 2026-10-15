"""
Estado global de plantillas (spec 004, T4): solo lógica, sin BD ni red.

La API de gerencia y los eventos de NeuroAPI se prueban en T8; aquí van las
reglas puras, la lectura del estado en cada evaluación y el cambio automático
por código de Meta (plan 2.9), con `session` sustituido por un fake.
"""

from typing import Any

import pytest

from services import plantillas_whatsapp as pw


class _BDPlantillas:
    def __init__(self, estados: dict[str, str]) -> None:
        self.estados = dict(estados)
        self.updates: list[tuple[Any, ...]] = []
        self.auditoria: list[tuple[Any, ...]] = []

    def instalar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fetch_one(sql: str, *args: Any) -> dict[str, Any] | None:
            assert "plantillas_whatsapp_globales" in sql
            estado = self.estados.get(args[0])
            return {"estado": estado} if estado is not None else None

        async def _execute(sql: str, *args: Any) -> str:
            if "UPDATE plantillas_whatsapp_globales" in sql:
                self.updates.append(args)
                self.estados[args[0]] = args[1]
            elif "INSERT INTO gerencia_auditoria" in sql:
                self.auditoria.append(args)
            else:  # pragma: no cover - cualquier otra escritura es un error del test
                raise AssertionError(sql)
            return "OK"

        monkeypatch.setattr(pw, "fetch_one", _fetch_one)
        monkeypatch.setattr(pw, "execute", _execute)


# ------------------------------------------------------------
# Reglas puras
# ------------------------------------------------------------
@pytest.mark.parametrize(
    "estado, esperado",
    [
        ("no_solicitada", True),
        ("rechazada", True),
        ("pausada", True),
        ("pendiente_aprobacion", False),
        ("aprobada", False),
    ],
)
def test_puede_reintentar_solo_si_no_esta_aprobada_ni_pendiente(estado, esperado):
    assert pw.puede_reintentar(estado) is esperado


@pytest.mark.parametrize("estado", ["no_solicitada", "pendiente_aprobacion", "rechazada", "pausada", None])
def test_solo_aprobada_permite_enviar(estado):
    assert pw.plantilla_aprobada(estado) is False
    assert pw.plantilla_aprobada("aprobada") is True


def test_extrae_codigo_de_un_json_anidado():
    cuerpo = (
        '{"success": false, "error": {"code": "META_API_ERROR", "details": '
        '{"error": {"code": 132015, "message": "paused"}}}}'
    )
    assert pw.extraer_codigo_meta(cuerpo) == 132015


def test_extrae_codigo_como_texto_y_de_error_subcode():
    assert pw.extraer_codigo_meta('{"error": {"error_subcode": "132016"}}') == 132016


def test_extrae_codigo_de_un_cuerpo_recortado():
    recortado = '{"success":false,"error":{"code":"META_API_ERROR","details":{"code":132015,"mess'
    assert pw.extraer_codigo_meta(recortado) == 132015


@pytest.mark.parametrize(
    "cuerpo",
    [
        None,
        "",
        "Internal Server Error",
        '{"success": false, "error": {"code": "BAD_REQUEST"}}',
        # Un número suelto en el texto no es un código de Meta.
        '{"message": "fallo 132015 en algun lado"}',
        '{"code": 131000}',
    ],
)
def test_sin_codigo_reconocible_devuelve_none(cuerpo):
    assert pw.extraer_codigo_meta(cuerpo) is None


def test_estado_por_codigo():
    assert pw.estado_por_codigo_meta(132015) == "pausada"
    assert pw.estado_por_codigo_meta(132016) == "rechazada"
    assert pw.estado_por_codigo_meta(132001) is None
    assert pw.estado_por_codigo_meta(None) is None


# ------------------------------------------------------------
# Lectura del estado
# ------------------------------------------------------------
async def test_estado_se_lee_en_cada_evaluacion(monkeypatch):
    bd = _BDPlantillas({"recordatorio_cita": "aprobada"})
    bd.instalar(monkeypatch)

    assert await pw.esta_aprobada("recordatorio_cita") is True
    bd.estados["recordatorio_cita"] = "pausada"
    assert await pw.esta_aprobada("recordatorio_cita") is False


async def test_fila_ausente_cuenta_como_no_solicitada(monkeypatch):
    _BDPlantillas({}).instalar(monkeypatch)
    assert await pw.obtener_estado("recordatorio_cita") == "no_solicitada"
    assert await pw.esta_aprobada("recordatorio_cita") is False


# ------------------------------------------------------------
# Cambio automático por error de envío
# ------------------------------------------------------------
async def test_132015_deja_la_plantilla_pausada_con_nota_y_auditoria(monkeypatch):
    bd = _BDPlantillas({"notificacion_cancelacion": "aprobada"})
    bd.instalar(monkeypatch)

    nuevo = await pw.aplicar_error_envio("notificacion_cancelacion", '{"error": {"code": 132015}}')

    assert nuevo == "pausada"
    assert bd.estados["notificacion_cancelacion"] == "pausada"
    assert bd.updates == [("notificacion_cancelacion", "pausada", "auto: error de envío 132015")]
    assert len(bd.auditoria) == 1
    actor, accion, detalle = bd.auditoria[0]
    assert actor == pw.ACTOR_SISTEMA
    assert accion == pw.ACCION_AUDITORIA
    assert detalle["anterior"] == "aprobada" and detalle["nuevo"] == "pausada"


async def test_132016_deja_la_plantilla_rechazada(monkeypatch):
    bd = _BDPlantillas({"recordatorio_cita": "aprobada"})
    bd.instalar(monkeypatch)

    assert await pw.aplicar_error_envio("recordatorio_cita", '{"code": "132016"}') == "rechazada"
    assert bd.estados["recordatorio_cita"] == "rechazada"


@pytest.mark.parametrize(
    "cuerpo",
    ['{"error": {"code": 132001}}', '{"error": {"code": "META_API_ERROR"}}', "boom", None],
)
async def test_132001_o_un_error_sin_codigo_no_cambia_el_estado(monkeypatch, cuerpo):
    bd = _BDPlantillas({"recordatorio_cita": "aprobada"})
    bd.instalar(monkeypatch)

    assert await pw.aplicar_error_envio("recordatorio_cita", cuerpo) is None
    assert bd.estados["recordatorio_cita"] == "aprobada"
    assert bd.updates == [] and bd.auditoria == []


async def test_si_ya_tiene_ese_estado_no_escribe_otra_vez(monkeypatch):
    bd = _BDPlantillas({"recordatorio_cita": "pausada"})
    bd.instalar(monkeypatch)

    assert await pw.aplicar_error_envio("recordatorio_cita", '{"code": 132015}') is None
    assert bd.updates == [] and bd.auditoria == []
