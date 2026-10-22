"""
Tests del modo "Probar agente" (services/agente_tester.py y el modo
`simulacion` del bucle).

Lo que importa si falla:

  1. probar al agente no deja huella: no lee ni escribe mensajes, no registra
     consumo, no cobra crédito y no transfiere ninguna conversación
  2. las herramientas con efectos se simulan; solo corren de verdad las
     lecturas de una lista cerrada
  3. el cupo diario es atómico y se agota (20 por negocio y día)
  4. la conversación de prueba se valida antes de gastar cupo
  5. "ver como" tiene cerrada la ruta (gasta tokens a nombre del dueño)

No se llama a la API ni a la base.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from services import agente_tester
from services.acceso_soporte import ruta_sensible
from services.agente import escalar, herramientas, loop_agente, prompt
from services.agente.modelos import ContextoMensaje

TENANT = uuid4()


# ============================================================
# Dobles (mismos que tests/test_agente_loop.py)
# ============================================================
def bloque_texto(texto: str):
    return SimpleNamespace(type="text", text=texto)


def bloque_tool(id_: str, nombre: str, entrada: dict):
    return SimpleNamespace(type="tool_use", id=id_, name=nombre, input=entrada)


def respuesta_api(bloques, *, stop_reason="end_turn"):
    return SimpleNamespace(
        content=bloques,
        stop_reason=stop_reason,
        model="claude-sonnet-5",
        stop_details=None,
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


class ClienteFalso:
    def __init__(self, respuestas):
        self._respuestas = list(respuestas)
        self.peticiones: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.peticiones.append({**kwargs, "messages": list(kwargs["messages"])})
        return self._respuestas.pop(0)


CONFIG = {
    "agent_name": "Asistente",
    "system_prompt": "Barbería El Corte. Cortes y barba.",
    "model": "claude-sonnet-5",
    "temperature": 0.7,
    "history_window": 30,
}


@pytest.fixture
def contexto():
    return ContextoMensaje(
        tenant_id=TENANT,
        conversation_id=uuid4(),
        user_id=uuid4(),
        canal="web",
        usuario_id="prueba",
        nombre="Cliente de prueba",
        contenido="¿cuánto cuesta el corte?",
        tipo="text",
        mensaje_id=None,
        db_message_id=uuid4(),
        db_message_created_at=None,
        config=CONFIG,
    )


@pytest.fixture
def huellas(monkeypatch):
    """Todo lo que tocaría Postgres falla el test si se llama en simulación."""
    llamadas: list[str] = []

    async def catalogo(_tenant, _canal=None):
        return "- tool_key: \"crear_reserva\"", [{"tool_key": "crear_reserva"}]

    def prohibido(nombre):
        async def f(*_a, **_k):
            llamadas.append(nombre)
            return None

        return f

    monkeypatch.setattr(prompt, "catalogo_herramientas", catalogo)
    monkeypatch.setattr(prompt, "historial", prohibido("historial"))
    monkeypatch.setattr(prompt, "eventos_de_contexto", prohibido("eventos"))
    monkeypatch.setattr(loop_agente, "_registrar_consumo", prohibido("consumo"))
    monkeypatch.setattr(escalar, "escalar", prohibido("escalar"))
    return llamadas


def usar_cliente(monkeypatch, respuestas) -> ClienteFalso:
    cliente = ClienteFalso(respuestas)
    monkeypatch.setattr(loop_agente, "_cliente", lambda: cliente)
    return cliente


# ============================================================
# 1. Sin huella
# ============================================================
async def test_simulacion_no_toca_mensajes_ni_consumo(monkeypatch, contexto, huellas):
    cliente = usar_cliente(monkeypatch, [respuesta_api([bloque_texto("El corte cuesta 200.")])])

    r = await loop_agente.responder(
        contexto,
        simulacion=True,
        historial_previo=[{"role": "user", "content": "hola"}, {"role": "assistant", "content": "¡Hola!"}],
    )

    assert r.texto == "El corte cuesta 200."
    assert huellas == []
    enviados = cliente.peticiones[0]["messages"]
    assert [m["content"] for m in enviados] == ["hola", "¡Hola!", "¿cuánto cuesta el corte?"]


async def test_escalar_en_simulacion_no_transfiere_pero_corta(monkeypatch, contexto, huellas):
    usar_cliente(
        monkeypatch,
        [
            respuesta_api(
                [bloque_tool("t1", "escalar_humano", {"motivo": "pide humano", "area": "ventas"})],
                stop_reason="tool_use",
            )
        ],
    )

    r = await loop_agente.responder(contexto, simulacion=True, historial_previo=[])

    assert r.transferida is True
    assert "escalar" not in huellas
    assert r.herramientas == [
        {"tool_key": "escalar_humano", "parametros": {"motivo": "pide humano", "area": "ventas"}}
    ]


async def test_herramienta_en_simulacion_no_cobra_y_deja_traza(monkeypatch, contexto, huellas):
    recibidas = []

    async def falsa(**kwargs):
        recibidas.append(kwargs)
        return "[SIMULADO] ok"

    monkeypatch.setattr(herramientas, "ejecutar", falsa)
    usar_cliente(
        monkeypatch,
        [
            respuesta_api(
                [bloque_tool("t1", "ejecutar_herramienta",
                             {"tool_key": "crear_reserva", "parametros": {"hora": "10:00"}})],
                stop_reason="tool_use",
            ),
            respuesta_api([bloque_texto("Listo, quedaste agendado.")]),
        ],
    )

    r = await loop_agente.responder(contexto, simulacion=True, historial_previo=[])

    assert recibidas[0]["cobrar"] is False
    assert r.herramientas == [{"tool_key": "crear_reserva", "parametros": {"hora": "10:00"}}]
    assert r.texto == "Listo, quedaste agendado."


async def test_produccion_sigue_cobrando(monkeypatch, contexto):
    """El modo normal no cambia: `cobrar` es True salvo en simulación."""
    recibidas = []

    async def falsa(**kwargs):
        recibidas.append(kwargs)
        return "ok"

    async def sin_nada(*_a, **_k):
        return []

    async def catalogo(_t, _c=None):
        return "", [{"tool_key": "x"}]

    monkeypatch.setattr(herramientas, "ejecutar", falsa)
    monkeypatch.setattr(prompt, "catalogo_herramientas", catalogo)
    monkeypatch.setattr(prompt, "historial", sin_nada)
    monkeypatch.setattr(prompt, "eventos_de_contexto", sin_nada)
    monkeypatch.setattr(loop_agente, "_registrar_consumo", sin_nada)
    usar_cliente(
        monkeypatch,
        [
            respuesta_api(
                [bloque_tool("t1", "ejecutar_herramienta", {"tool_key": "x", "parametros": {}})],
                stop_reason="tool_use",
            ),
            respuesta_api([bloque_texto("listo")]),
        ],
    )

    await loop_agente.responder(contexto)

    assert recibidas[0]["cobrar"] is True


# ============================================================
# 2. Qué corre de verdad
# ============================================================
@pytest.mark.parametrize(
    "tipo, config, esperado",
    [
        ("sql_lectura", {}, True),
        ("google_sheets", {}, True),
        ("google_docs", {}, True),
        ("http_generico", {}, True),
        ("http_generico", {"method": "get"}, True),
        ("http_generico", {"method": "POST"}, False),
        ("http_generico", {"method": "DELETE"}, False),
        ("http_post_json", {}, False),
        ("tipo_nuevo_desconocido", {}, False),
    ],
)
def test_solo_lecturas_conocidas_corren_de_verdad(tipo, config, esperado):
    assert herramientas.se_ejecuta_en_prueba(tipo, config) is esperado


# ============================================================
# 3. Cupo diario
# ============================================================
async def test_cupo_descuenta_y_devuelve_los_restantes(monkeypatch):
    async def fetch_one(_sql, *_args):
        return {"mensajes": 5}

    monkeypatch.setattr(agente_tester, "fetch_one", fetch_one)
    assert await agente_tester.consumir_cupo(TENANT) == agente_tester.LIMITE_DIARIO - 5


async def test_cupo_agotado_levanta(monkeypatch):
    async def fetch_one(_sql, *_args):
        return None  # el UPSERT no sumó: ya se llegó al tope

    monkeypatch.setattr(agente_tester, "fetch_one", fetch_one)
    with pytest.raises(agente_tester.CupoAgotado):
        await agente_tester.consumir_cupo(TENANT)


def test_el_limite_diario_es_20():
    assert agente_tester.LIMITE_DIARIO == 20


# ============================================================
# 4. Validación de la conversación de prueba
# ============================================================
def turno(role, content):
    return {"role": role, "content": content}


def test_separa_historial_y_mensaje_nuevo():
    historial, mensaje = agente_tester.preparar_turnos(
        [turno("user", "hola"), turno("assistant", "¡hola!"), turno("user", "precio?")], 30
    )
    assert mensaje == "precio?"
    assert [t["content"] for t in historial] == ["hola", "¡hola!"]


def test_el_historial_se_recorta_a_la_ventana_y_empieza_por_el_usuario():
    turnos = [
        turno("user", "a"), turno("assistant", "b"),
        turno("user", "c"), turno("assistant", "d"),
        turno("user", "e"),
    ]
    historial, _ = agente_tester.preparar_turnos(turnos, 3)
    # Los 3 últimos del historial son b, c, d: b es del agente y se descarta.
    assert [t["content"] for t in historial] == ["c", "d"]


@pytest.mark.parametrize(
    "turnos",
    [
        [],
        [turno("user", "hola"), turno("assistant", "respuesta")],
        [turno("user", "   ")],
    ],
)
def test_conversaciones_invalidas_se_rechazan(turnos):
    with pytest.raises(ValueError):
        agente_tester.preparar_turnos(turnos, 30)


def test_conversacion_demasiado_larga_se_rechaza():
    turnos = [turno("user" if i % 2 == 0 else "assistant", "x") for i in range(agente_tester.MAX_TURNOS + 1)]
    with pytest.raises(ValueError):
        agente_tester.preparar_turnos(turnos, 30)


# ============================================================
# `probar`: falla del modelo
# ============================================================
async def test_sin_clave_de_api_es_no_disponible(monkeypatch):
    async def responder(*_a, **_k):
        raise RuntimeError("ANTHROPIC_API_KEY sin configurar")

    monkeypatch.setattr(loop_agente, "responder", responder)
    with pytest.raises(agente_tester.PruebaNoDisponible):
        await agente_tester.probar(
            tenant_id=TENANT,
            config=CONFIG,
            turnos=[turno("user", "hola")],
            agente_canal_id=None,
            restantes_hoy=19,
        )


async def test_fallo_de_la_api_no_se_presenta_como_respuesta(monkeypatch):
    async def responder(*_a, **_k):
        return loop_agente.Respuesta(texto=loop_agente.RESPUESTA_VACIA, fallo=True)

    monkeypatch.setattr(loop_agente, "responder", responder)
    with pytest.raises(agente_tester.PruebaNoDisponible):
        await agente_tester.probar(
            tenant_id=TENANT,
            config=CONFIG,
            turnos=[turno("user", "hola")],
            agente_canal_id=None,
            restantes_hoy=19,
        )


# ============================================================
# 5. "Ver como"
# ============================================================
def test_probar_esta_cerrado_en_ver_como():
    assert ruta_sensible("/api/agente/probar")
    # Editar la config sigue siendo posible con concesión de escritura.
    assert not ruta_sensible("/api/agente")
