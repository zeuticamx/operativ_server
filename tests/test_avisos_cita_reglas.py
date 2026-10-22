"""Reglas puras de los avisos de cita (spec 004, T3). Sin base de datos ni servidor."""

from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

import pytest

from services.avisos_cita_reglas import (
    EstadoTelefono,
    canal_soportado,
    clasificar_telefono,
    ejecucion_viva,
    en_ventana,
    fecha_mostrada,
    motivo_cancelacion,
    motivo_recordatorio,
    puede_responder,
    variables_cancelacion,
    variables_recordatorio,
)

AHORA = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
ZONA_MX = "America/Mexico_City"  # UTC-6 en octubre de 2026 (sin horario de verano)


# ---------------------------------------------------------------- teléfono

@pytest.mark.parametrize(
    "crudo, esperado",
    [
        ("55 1234 5678", "+525512345678"),
        ("(55) 1234-5678", "+525512345678"),
        ("55.1234.5678", "+525512345678"),
        ("+1 (415) 555-0100", "+14155550100"),
        ("0052 55 1234 5678", "+525512345678"),
        ("+52 55 1234 5678", "+525512345678"),
    ],
)
def test_telefono_valido(crudo: str, esperado: str) -> None:
    assert clasificar_telefono(crudo) == (EstadoTelefono.VALIDO, esperado)


@pytest.mark.parametrize("crudo", ["525512345678", "12345", "abc", "+0123456789", "+123", "55 1234 56 78 9"])
def test_telefono_no_normalizable(crudo: str) -> None:
    assert clasificar_telefono(crudo) == (EstadoTelefono.NO_NORMALIZABLE, None)


@pytest.mark.parametrize("crudo", [None, "", "   ", "\t\n"])
def test_telefono_vacio(crudo: str | None) -> None:
    assert clasificar_telefono(crudo) == (EstadoTelefono.VACIO, None)


# ------------------------------------------------------------------- canal

def test_canal_widget_no_soportado() -> None:
    assert canal_soportado("widget") is False
    assert canal_soportado("web") is False


@pytest.mark.parametrize("canal", ["whatsapp", "instagram", "facebook", None])
def test_canal_soportado(canal: str | None) -> None:
    assert canal_soportado(canal) is True


# ------------------------------------------------- motivos de cancelación

def _canc(**cambios: object) -> str | None:
    base: dict[str, object] = dict(
        telefono=EstadoTelefono.VALIDO, canal_ok=True, activo=True, plantilla_estado="APPROVED"
    )
    base.update(cambios)
    return motivo_cancelacion(**base)  # type: ignore[arg-type]


def test_cancelacion_sin_motivo_si_todo_ok() -> None:
    assert _canc() is None


def test_cancelacion_orden_de_motivos() -> None:
    todo_mal = dict(
        telefono=EstadoTelefono.VACIO, canal_ok=False, activo=False, plantilla_estado="REJECTED"
    )
    assert _canc(**todo_mal) == "sin_telefono"
    todo_mal["telefono"] = EstadoTelefono.NO_NORMALIZABLE
    assert _canc(**todo_mal) == "telefono_invalido"
    todo_mal["telefono"] = EstadoTelefono.VALIDO
    assert _canc(**todo_mal) == "canal_no_soportado"
    todo_mal["canal_ok"] = True
    assert _canc(**todo_mal) == "desactivado_por_configuracion"
    todo_mal["activo"] = True
    assert _canc(**todo_mal) == "plantilla_no_aprobada"


# ------------------------------------------------ motivos de recordatorio

def _rec(**cambios: object) -> str | None:
    base: dict[str, object] = dict(
        telefono=EstadoTelefono.VALIDO,
        canal_ok=True,
        estado_cita="confirmada",
        hora_coincide=True,
        inicio_llegado=False,
        activo=True,
        plantilla_estado="APPROVED",
    )
    base.update(cambios)
    return motivo_recordatorio(**base)  # type: ignore[arg-type]


def test_recordatorio_sin_motivo_si_todo_ok() -> None:
    assert _rec() is None


def test_recordatorio_orden_de_motivos() -> None:
    todo_mal = dict(
        telefono=EstadoTelefono.VACIO,
        canal_ok=False,
        estado_cita="cancelada",
        hora_coincide=False,
        inicio_llegado=True,
        activo=False,
        plantilla_estado="REJECTED",
    )
    esperado = [
        ("telefono", EstadoTelefono.VACIO, "sin_telefono"),
        ("telefono", EstadoTelefono.NO_NORMALIZABLE, "telefono_invalido"),
        ("telefono", EstadoTelefono.VALIDO, "canal_no_soportado"),
        ("canal_ok", True, "cita_no_confirmada"),
        ("estado_cita", "confirmada", "hora_cambiada"),
        ("hora_coincide", True, "cita_ya_iniciada"),
        ("inicio_llegado", False, "desactivado_por_configuracion"),
        ("activo", True, "plantilla_no_aprobada"),
    ]
    for campo, valor, motivo in esperado:
        todo_mal[campo] = valor
        assert _rec(**todo_mal) == motivo


@pytest.mark.parametrize("estado", ["cancelada", "completada", "no_asistio"])
def test_recordatorio_cita_no_confirmada(estado: str) -> None:
    assert _rec(estado_cita=estado) == "cita_no_confirmada"


# ----------------------------------------------------------------- ventana

def test_en_ventana_limites() -> None:
    assert en_ventana(AHORA + timedelta(hours=5), AHORA) is True      # borde superior incluido
    assert en_ventana(AHORA + timedelta(hours=5, seconds=1), AHORA) is False
    assert en_ventana(AHORA + timedelta(seconds=1), AHORA) is True
    assert en_ventana(AHORA, AHORA) is False                           # ya iniciada
    assert en_ventana(AHORA - timedelta(minutes=1), AHORA) is False


def test_en_ventana_ejemplo_de_la_spec() -> None:
    # Cita del 16-10 a las 00:30 hora del negocio (06:30 UTC): se muestra 16-10-2026
    # pero la ventana abre el 15-10 a las 19:30 hora del negocio (01:30 UTC del 16).
    inicio = datetime(2026, 10, 16, 6, 30, tzinfo=timezone.utc)
    assert fecha_mostrada(inicio, ZONA_MX) == "16-10-2026"
    apertura = inicio - timedelta(hours=5)
    local = apertura.astimezone(ZoneInfo(ZONA_MX))
    assert (local.day, local.month, local.hour, local.minute) == (15, 10, 19, 30)
    assert en_ventana(inicio, apertura) is True  # justo en la apertura ya entra
    assert en_ventana(inicio, apertura - timedelta(seconds=1)) is False


def test_fecha_mostrada_usa_la_zona_del_negocio() -> None:
    inicio = datetime(2026, 10, 16, 3, 0, tzinfo=timezone.utc)  # 15-10 21:00 en México
    assert fecha_mostrada(inicio, ZONA_MX) == "15-10-2026"
    assert fecha_mostrada(inicio, "UTC") == "16-10-2026"


def test_instantes_ingenuos_lanzan() -> None:
    with pytest.raises(ValueError):
        en_ventana(datetime(2026, 10, 14, 15, 0), AHORA)
    with pytest.raises(ValueError):
        fecha_mostrada(datetime(2026, 10, 14, 15, 0), ZONA_MX)


def test_zona_invalida_lanza() -> None:
    with pytest.raises(ValueError):
        fecha_mostrada(AHORA, "No/Existe")


# --------------------------------------------------------------- variables

def test_variables_cancelacion() -> None:
    inicio = datetime(2026, 10, 16, 6, 30, tzinfo=timezone.utc)
    assert variables_cancelacion("corte de dama", inicio, ZONA_MX) == ["corte de dama", "16-10-2026"]


@pytest.mark.parametrize("servicio", [None, "", "   "])
def test_variables_cancelacion_respaldo_sin_vacios(servicio: str | None) -> None:
    inicio = datetime(2026, 10, 16, 6, 30, tzinfo=timezone.utc)
    variables = variables_cancelacion(servicio, inicio, ZONA_MX)
    assert variables == ["servicio agendado", "16-10-2026"]
    assert all(v.strip() for v in variables)


def test_variables_recordatorio() -> None:
    inicio = datetime(2026, 10, 15, 20, 0, tzinfo=timezone.utc)
    assert variables_recordatorio("Barberia Rogelio", inicio, ZONA_MX) == ["Barberia Rogelio", "15-10-2026"]


def test_variables_recordatorio_sin_nombre_lanza() -> None:
    with pytest.raises(ValueError):
        variables_recordatorio("  ", AHORA, ZONA_MX)


# --------------------------------------------------------- ejecución viva

def test_ejecucion_viva_a_los_5_minutos() -> None:
    assert ejecucion_viva(AHORA - timedelta(minutes=4, seconds=59), AHORA) is True
    assert ejecucion_viva(AHORA - timedelta(minutes=5), AHORA) is False
    assert ejecucion_viva(AHORA - timedelta(minutes=30), AHORA) is False
    assert ejecucion_viva(None, AHORA) is False


def test_ejecucion_viva_minutos_configurables() -> None:
    assert ejecucion_viva(AHORA - timedelta(minutes=8), AHORA, minutos=10) is True


# --------------------------------------------------------- puede_responder

def test_puede_responder() -> None:
    assert puede_responder(None, AHORA) is True
    assert puede_responder(AHORA - timedelta(hours=23, minutes=59), AHORA) is False
    assert puede_responder(AHORA - timedelta(hours=24), AHORA) is True
    assert puede_responder(AHORA - timedelta(days=3), AHORA) is True
