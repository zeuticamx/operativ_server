"""
Regla de cancelación por servicio (specs/001-regla-cancelacion-citas, T3), parte SIN DB.

Valida el contrato de `antelacion_cancelacion_horas` en los schemas (entero JSON
estricto 0-720 o null; null explícito vs omitido) y que `actualizar_servicio`
arme el UPDATE solo con los campos presentes. Las pruebas HTTP contra una base
real (roles, 404, 409, 402, "ver como") están en test_calendario.py.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from routers import calendario as router
from schemas import ServicioActualizarIn, ServicioCrearIn, ServicioOut
from services.calendario import Servicio

CAMPO = "antelacion_cancelacion_horas"


@pytest.mark.parametrize("valor", [0, 1, 24, 720])
def test_acepta_enteros_validos(valor):
    assert ServicioActualizarIn.model_validate({CAMPO: valor}).antelacion_cancelacion_horas == valor
    assert ServicioActualizarIn.model_validate_json(f'{{"{CAMPO}": {valor}}}').antelacion_cancelacion_horas == valor
    assert ServicioCrearIn(nombre="Corte", duracion_minutos=30, **{CAMPO: valor}).antelacion_cancelacion_horas == valor


@pytest.mark.parametrize("json_valor", ["721", "-1", "1.5", "24.0", '"24"', "true", "false", "[24]", "{}"])
def test_rechaza_lo_que_no_es_entero_json_estricto_0_720(json_valor):
    with pytest.raises(ValidationError):
        ServicioActualizarIn.model_validate_json(f'{{"{CAMPO}": {json_valor}}}')
    with pytest.raises(ValidationError):
        ServicioCrearIn.model_validate_json(f'{{"nombre": "Corte", "duracion_minutos": 30, "{CAMPO}": {json_valor}}}')


@pytest.mark.parametrize("valor", [721, -1, 1.5, 24.0, "24", True, False])
def test_rechaza_tambien_valores_python(valor):
    """FastAPI valida el cuerpo ya parseado (Python), no el texto JSON."""
    with pytest.raises(ValidationError):
        ServicioActualizarIn.model_validate({CAMPO: valor})


def test_null_explicito_es_un_cambio_valido_y_omitir_no():
    explicito = ServicioActualizarIn.model_validate_json(f'{{"{CAMPO}": null}}')
    assert CAMPO in explicito.model_fields_set
    assert explicito.antelacion_cancelacion_horas is None

    with pytest.raises(ValidationError):
        ServicioActualizarIn.model_validate_json("{}")
    with pytest.raises(ValidationError):
        ServicioActualizarIn.model_validate_json('{"nombre": null}')

    omitido = ServicioActualizarIn.model_validate({"activo": False})
    assert CAMPO not in omitido.model_fields_set


def test_los_otros_campos_siguen_siendo_cambios_validos():
    assert ServicioActualizarIn.model_validate({"activo": False}).activo is False
    assert ServicioActualizarIn.model_validate({"nombre": "Corte"}).nombre == "Corte"


def test_alta_sin_regla_nace_en_null_y_servicio_out_la_expone():
    assert ServicioCrearIn(nombre="Corte", duracion_minutos=30).antelacion_cancelacion_horas is None
    salida = ServicioOut(
        id=uuid4(), tenant_id=uuid4(), nombre="Corte", duracion_minutos=30, precio=None,
        activo=True, creado_en=datetime.now(timezone.utc),
    )
    assert salida.antelacion_cancelacion_horas is None
    assert ServicioOut.model_validate({**salida.model_dump(), CAMPO: 24}).antelacion_cancelacion_horas == 24


def test_servicio_dataclass_lee_la_columna_nueva():
    fila = dict(
        id=uuid4(), tenant_id=uuid4(), nombre="Corte", duracion_minutos=30, precio=None,
        activo=True, creado_en=datetime.now(timezone.utc),
    )
    assert Servicio.desde_fila(fila).antelacion_cancelacion_horas is None
    assert Servicio.desde_fila({**fila, CAMPO: 48}).antelacion_cancelacion_horas == 48


# ------------------------------------------------------------
# actualizar_servicio: el UPDATE solo toca la antelación si vino en el cuerpo
# ------------------------------------------------------------
@pytest.fixture
def capturas(monkeypatch):
    llamadas: list[tuple] = []
    servicio = Servicio(
        id=uuid4(), tenant_id=uuid4(), nombre="Corte", duracion_minutos=30, precio=None,
        activo=True, creado_en=datetime.now(timezone.utc), antelacion_cancelacion_horas=48,
    )

    async def falso_fetch_one(sql, *args):
        llamadas.append((sql, args))
        return {
            "id": servicio.id, "tenant_id": servicio.tenant_id, "nombre": "Corte",
            "duracion_minutos": 30, "precio": None, "activo": True,
            "creado_en": servicio.creado_en, CAMPO: 48,
        }

    async def sin_op(*_a, **_k):
        return None

    async def exigir(*_a, **_k):
        return servicio

    monkeypatch.setattr(router, "fetch_one", falso_fetch_one)
    monkeypatch.setattr(router, "verificar_calendario_activo", sin_op)
    monkeypatch.setattr(router, "verificar_acceso_tenant", lambda *_a, **_k: None)
    monkeypatch.setattr(router, "_exigir_servicio", exigir)
    monkeypatch.setattr(router.calendario, "registrar_edicion_servicio", sin_op)
    return SimpleNamespace(llamadas=llamadas, servicio=servicio)


async def _llamar(capturas, cuerpo: dict):
    usuario = SimpleNamespace(email="a@b.c", id=uuid4())
    await router.actualizar_servicio(
        capturas.servicio.id,
        ServicioActualizarIn.model_validate(cuerpo),
        capturas.servicio.tenant_id,
        usuario,
    )
    return capturas.llamadas[-1]


@pytest.mark.asyncio
async def test_update_omitiendo_la_antelacion_no_la_menciona(capturas):
    sql, args = await _llamar(capturas, {"nombre": "Otro"})
    assert "antelacion_cancelacion_horas =" not in sql
    assert len(args) == 6


@pytest.mark.asyncio
async def test_update_con_null_explicito_la_asigna_a_null(capturas):
    sql, args = await _llamar(capturas, {CAMPO: None})
    assert "antelacion_cancelacion_horas = $7" in sql
    assert args[6] is None


@pytest.mark.asyncio
async def test_update_con_valor_lo_asigna(capturas):
    sql, args = await _llamar(capturas, {CAMPO: 0, "activo": False})
    assert "antelacion_cancelacion_horas = $7" in sql
    assert args[6] == 0
    assert args[5] is False
