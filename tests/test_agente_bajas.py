"""
Tests de la lista de bajas (services/agente/bajas.py) y de su etapa en
services/agente/ingreso.py.

Cierra el hallazgo 1 de la auditoría. Lo que importa si falla:

  1. "cancelar cita" NO es una baja. Es el falso positivo que hay que evitar
     a toda costa: este agente atiende citas, y confundir una cancelación de
     reserva con una baja deja al negocio sin poder volver a escribirle a ese
     cliente y encima sin atender la cancelación.
  2. las fórmulas de verdad ("BAJA", "STOP", "dar de baja"…) sí lo son, con
     mayúsculas, tildes, signos o emoji de por medio
  3. la baja se apunta ANTES de revisar el plan: no es un servicio del plan,
     es una obligación con el cliente
  4. un ALTA de quien no estaba de baja es un mensaje normal, no un acuse

La comprobación en el envío está en tests/test_agente_envio.py (sección 5).

Ni red ni base: se simulan las consultas.
"""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services import numeros_whatsapp
from services.agente import bajas, ingreso
from services.agente import prompt as prompt_mod
from services.agente.modelos import MensajeNormalizado, PeticionVerificada

TENANT = uuid4()
USUARIO = uuid4()
CONVERSACION = uuid4()
MENSAJE = uuid4()


# ============================================================
# 1. Reconocer la petición (puro, sin base)
# ============================================================
@pytest.mark.parametrize(
    "texto",
    [
        "BAJA",
        "baja",
        "Baja.",
        "  baja  ",
        "STOP",
        "stop!",
        "stop 🙏",
        "parar",
        "dar de baja",
        "Dar De Baja",
        "darme de baja",
        "quiero darme de baja",
        "me doy de baja",
        "no molestar",
        "no mas mensajes",
        "no más mensajes",
        "no quiero recibir más mensajes",
        "No me escriban más",
        "cancelar suscripcion",
        "cancelar suscripción",
        "unsubscribe",
    ],
)
def test_esto_es_una_baja(texto):
    assert bajas.palabra_de_baja(texto) is not None


@pytest.mark.parametrize(
    "texto",
    [
        # EL caso. Un agente de citas recibe esto todos los días.
        "cancelar",
        "cancelar cita",
        "Cancelar mi cita",
        "quiero cancelar mi cita de mañana",
        "cancelar reserva",
        "cancelar la reserva del viernes",
        # Respuestas normales de conversación.
        "no",
        "ya no",
        "no gracias",
        "hola",
        # La palabra aparece dentro de la frase, pero el mensaje no es una
        # petición de baja: el cotejo es por mensaje completo a propósito.
        "si no me atienden me doy de baja luego",
        "me quiero dar de baja del gimnasio, no de aquí",
        # Vacíos.
        "",
        "   ",
        None,
        "🙏",
    ],
)
def test_esto_no_es_una_baja(texto):
    assert bajas.palabra_de_baja(texto) is None


def test_la_palabra_que_se_guarda_viene_normalizada():
    """
    Es lo que va a `agente_bajas.palabra`: sale de la lista cerrada del
    código, no del mensaje del cliente, así que no es contenido de la
    conversación (mismo criterio que delivery_errors, hallazgo 3).
    """
    assert bajas.palabra_de_baja("  No Quiero Recibir Más Mensajes!! ") == (
        "no quiero recibir mas mensajes"
    )


@pytest.mark.parametrize("texto", ["ALTA", "alta", "start", "Suscribirme", "dar de alta"])
def test_esto_es_un_alta(texto):
    assert bajas.palabra_de_alta(texto) is not None


@pytest.mark.parametrize("texto", ["alta cocina", "altas temperaturas", "hola", ""])
def test_esto_no_es_un_alta(texto):
    assert bajas.palabra_de_alta(texto) is None


def test_baja_y_alta_no_se_solapan():
    """Ninguna palabra puede significar las dos cosas."""
    for palabra in ("baja", "stop", "dar de baja", "unsubscribe"):
        assert bajas.palabra_de_baja(palabra)
        assert bajas.palabra_de_alta(palabra) is None
    for palabra in ("alta", "start", "dar de alta"):
        assert bajas.palabra_de_alta(palabra)
        assert bajas.palabra_de_baja(palabra) is None


# ============================================================
# 2. La consulta que hace el envío
# ============================================================
async def test_esta_de_baja_consulta_los_tres_campos(monkeypatch):
    vistos = {}

    async def fetch_value(query, *args):
        vistos["query"] = query
        vistos["args"] = args
        return True

    monkeypatch.setattr(bajas, "fetch_value", fetch_value)

    assert await bajas.esta_de_baja(TENANT, "whatsapp", "5215599999999") is True
    assert "agente_bajas" in vistos["query"]
    # Por negocio, por canal y por cliente: una baja en un negocio no puede
    # callar al agente de otro.
    assert vistos["args"] == (TENANT, "whatsapp", "5215599999999")


async def test_si_la_consulta_falla_se_asume_baja(monkeypatch):
    """
    Falla cerrado. Una plantilla que no sale es una conversación que no se
    reabre; una que sale a quien pidió la baja es un incumplimiento de Meta.
    """
    async def explota(query, *args):
        raise RuntimeError("la base no responde")

    monkeypatch.setattr(bajas, "fetch_value", explota)

    assert await bajas.esta_de_baja(TENANT, "whatsapp", "521") is True


async def test_registrar_es_idempotente(monkeypatch):
    """Repetir BAJA refresca la fila, no deja dos."""
    vistos = {}

    async def execute(query, *args):
        vistos["query"] = query
        vistos["args"] = args
        return "OK"

    monkeypatch.setattr(bajas, "execute", execute)

    await bajas.registrar(TENANT, "whatsapp", "521", "baja")

    assert "ON CONFLICT (tenant_id, channel_type, user_id) DO UPDATE" in vistos["query"]
    assert vistos["args"] == (TENANT, "whatsapp", "521", "baja")


async def test_quitar_dice_si_habia_algo_que_quitar(monkeypatch):
    async def habia(query, *args):
        return 1

    async def no_habia(query, *args):
        return None

    monkeypatch.setattr(bajas, "fetch_value", habia)
    assert await bajas.quitar(TENANT, "whatsapp", "521") is True

    monkeypatch.setattr(bajas, "fetch_value", no_habia)
    assert await bajas.quitar(TENANT, "whatsapp", "521") is False


# ============================================================
# 3. La etapa en el ingreso
# ============================================================
@pytest.fixture
def escrito():
    """Las escrituras que haría `ingreso` (mensajes, conversación)."""
    return []


@pytest.fixture
def registradas():
    """Las bajas que se apuntarían, y las que se levantarían."""
    return {"altas": [], "bajas": []}


@pytest.fixture
def ingreso_sin_base(monkeypatch, escrito, registradas):
    """
    Todo resuelto menos la etapa de bajas: tenant, usuario, conversación y
    mensaje nuevo (no duplicado).
    """
    async def _resolver_tenant(mensaje, peticion):
        return TENANT

    async def _upsert_usuario(tenant_id, mensaje):
        return USUARIO

    async def _conversacion(tenant_id, user_id, canal):
        return {"id": CONVERSACION, "status": "active", "metadata": {}}

    async def _guardar_mensaje(tenant_id, conversation_id, mensaje):
        return {"id": MENSAJE, "created_at": datetime(2026, 10, 16, 12, 0)}

    async def execute(query, *args):
        escrito.append((query, args))
        return "OK"

    async def _resolver_entrada_falso(phone_number_id, *, ahora=None):
        from services.numeros_whatsapp import ResultadoEntrada
        return ResultadoEntrada(numero={"id": uuid4()}, motivo=None)

    async def registrar(tenant_id, canal, usuario_id, palabra=None):
        registradas["bajas"].append((tenant_id, canal, usuario_id, palabra))

    async def _config_numero(agente_canal_id):
        return {"agent_name": "A", "system_prompt": "P", "model": "claude-haiku-4-5-20251001", "temperature": None, "history_window": 30}

    async def _config_social(tenant_id):
        return {"agent_name": "A", "system_prompt": "P", "model": "claude-haiku-4-5-20251001", "temperature": None, "history_window": 30}

    monkeypatch.setattr(ingreso, "_resolver_tenant", _resolver_tenant)
    monkeypatch.setattr(ingreso, "_upsert_usuario", _upsert_usuario)
    monkeypatch.setattr(ingreso, "_conversacion", _conversacion)
    monkeypatch.setattr(ingreso, "_guardar_mensaje", _guardar_mensaje)
    monkeypatch.setattr(ingreso, "execute", execute)
    monkeypatch.setattr(ingreso, "_esperar_rafaga", lambda ctx: True)
    monkeypatch.setattr(numeros_whatsapp, "resolver_entrada", _resolver_entrada_falso)
    monkeypatch.setattr(prompt_mod, "config_numero", _config_numero)
    monkeypatch.setattr(prompt_mod, "config_social", _config_social)
    monkeypatch.setattr(bajas, "registrar", registrar)


def _mensaje(contenido: str) -> MensajeNormalizado:
    return MensajeNormalizado(
        procesar=True,
        canal="whatsapp",
        usuario_id="5215599999999",
        nombre="Ana",
        mensaje_id="wamid.ABC",
        contenido=contenido,
        phone_number_id="PNID",
    )


def _usar_mensaje(monkeypatch, contenido: str) -> None:
    monkeypatch.setattr(
        ingreso.normalizar_mod, "normalizar", lambda cuerpo, tenant_id=None: _mensaje(contenido)
    )


def _peticion() -> PeticionVerificada:
    return PeticionVerificada(cuerpo={}, origen="meta")


@pytest.fixture(autouse=True)
def plan_nunca_consultado(monkeypatch):
    """
    El acceso al plan no debe consultarse en el camino de una baja: una baja
    se apunta aunque el negocio tenga el agente apagado o el plan agotado.
    Si alguien mueve la etapa después del gate, estos tests lo cazan.
    """
    import routers.eventos as eventos

    async def explota(*args, **kwargs):
        raise AssertionError(
            "la baja debe tramitarse antes de revisar el plan del negocio"
        )

    monkeypatch.setattr(eventos, "on_mensaje_entrante", explota)


async def test_una_baja_se_apunta_y_se_acusa(monkeypatch, ingreso_sin_base, registradas, escrito):
    _usar_mensaje(monkeypatch, "BAJA")

    r = await ingreso.procesar(_peticion())

    assert registradas["bajas"] == [(TENANT, "whatsapp", "5215599999999", "baja")]
    assert r.respuesta_inmediata == bajas.ACUSE_BAJA
    # No sigue al agente: no se gasta un crédito en contestar una baja.
    assert not r.sigue_al_agente
    # El acuse queda en el historial, para que el negocio vea la baja.
    assert any("INSERT INTO messages" in q for q, _ in escrito)
    # Y el destino lleva la conversación, para poder entregarlo.
    assert r.destino is not None and r.destino.conversation_id == CONVERSACION


async def test_el_acuse_de_baja_dice_como_volver():
    """Sin camino de vuelta, la baja es una puerta que solo cierra."""
    assert "ALTA" in bajas.ACUSE_BAJA


async def test_una_baja_apaga_esperando_reapertura(monkeypatch, ingreso_sin_base, escrito):
    """
    Si la conversación estaba esperando reapertura y llega una baja, hay que
    apagar la bandera: dejarla puesta haría que el envío intentara una
    plantilla que ahora tiene prohibida.
    """
    _usar_mensaje(monkeypatch, "stop")

    await ingreso.procesar(_peticion())

    assert any("esperando_reapertura = false" in q for q, _ in escrito)


async def test_un_reintento_del_proveedor_no_acusa_dos_veces(
    monkeypatch, ingreso_sin_base, registradas
):
    async def duplicado(tenant_id, conversation_id, mensaje):
        return None

    monkeypatch.setattr(ingreso, "_guardar_mensaje", duplicado)
    _usar_mensaje(monkeypatch, "BAJA")

    r = await ingreso.procesar(_peticion())

    assert r.respuesta_inmediata is None
    assert not r.sigue_al_agente
    # La baja se apunta igual: es idempotente y es lo que importa que quede.
    assert registradas["bajas"]


async def test_un_alta_de_quien_estaba_de_baja_se_acusa(
    monkeypatch, ingreso_sin_base
):
    async def quitar(tenant_id, canal, usuario_id):
        return True

    monkeypatch.setattr(bajas, "quitar", quitar)
    _usar_mensaje(monkeypatch, "ALTA")

    r = await ingreso.procesar(_peticion())

    assert r.respuesta_inmediata == bajas.ACUSE_ALTA
    assert not r.sigue_al_agente


async def test_un_alta_de_quien_no_estaba_de_baja_es_un_mensaje_normal(
    monkeypatch, ingreso_sin_base
):
    """
    Si no había baja que levantar, "alta" es una palabra cualquiera y la
    contesta el agente. Si no, se comería el turno con un acuse absurdo.

    Que llegue al gate del plan (y explote el fixture) ES el resultado
    esperado: significa que el mensaje siguió su camino normal.
    """
    async def quitar(tenant_id, canal, usuario_id):
        return False

    monkeypatch.setattr(bajas, "quitar", quitar)
    _usar_mensaje(monkeypatch, "alta")

    with pytest.raises(AssertionError, match="antes de revisar el plan"):
        await ingreso.procesar(_peticion())


async def test_cancelar_una_cita_sigue_al_agente(monkeypatch, ingreso_sin_base, registradas):
    """
    El falso positivo que importa. Una cancelación de cita no es una baja: la
    atiende el agente, y el cliente sigue pudiendo recibir mensajes.
    """
    _usar_mensaje(monkeypatch, "quiero cancelar mi cita de mañana")

    with pytest.raises(AssertionError, match="antes de revisar el plan"):
        await ingreso.procesar(_peticion())

    assert registradas["bajas"] == []
