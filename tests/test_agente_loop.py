"""
Tests del bucle de tool-calling (services/agente/loop_agente.py).

Es el reemplazo del nodo "Agent" de n8n, y lo que importa si falla:

  1. una respuesta sin herramientas sale tal cual, en una sola llamada
  2. una llamada a herramienta se ejecuta y su resultado vuelve al modelo
  3. varias herramientas en una misma respuesta se ejecutan y sus resultados
     viajan en UN solo mensaje (repartirlos enseña al modelo a dejar de
     pedirlas en paralelo)
  4. el modelo no puede elegir tenant ni conversación: los pone el servidor
  5. un rechazo del modelo no se lee como si fuera contenido
  6. el bucle corta y no se queda girando si el modelo nunca termina
  7. el consumo de tokens se suma entre vueltas (esto es lo que sustituye al
     workflow `medir-uso-tokens`)
  8. el breakpoint de caché del historial se mueve con cada vuelta en vez de
     acumularse, y no se marca nada si no hay una segunda vuelta segura

No se llama a la API ni a la base: el cliente de Anthropic se sustituye por
un doble que devuelve respuestas pactadas.
"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import anthropic
import pytest

from services.agente import escalar, herramientas, loop_agente, prompt
from services.agente.modelos import ContextoMensaje

TENANT = uuid4()
CONVERSACION = uuid4()
MENSAJE_BD = uuid4()


# ============================================================
# Dobles
# ============================================================
def bloque_texto(texto: str):
    return SimpleNamespace(type="text", text=texto)


def bloque_tool(id_: str, nombre: str, entrada: dict):
    return SimpleNamespace(type="tool_use", id=id_, name=nombre, input=entrada)


def respuesta_api(
    bloques, *, stop_reason="end_turn", entrada=10, salida=5, categoria=None
):
    return SimpleNamespace(
        content=bloques,
        stop_reason=stop_reason,
        model="claude-sonnet-5",
        stop_details=SimpleNamespace(category=categoria) if categoria else None,
        usage=SimpleNamespace(
            input_tokens=entrada,
            output_tokens=salida,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


class ClienteFalso:
    """
    Devuelve las respuestas pactadas y guarda cada petición recibida.

    `peticiones` guarda los kwargs tal cual, así que su lista `messages` es la
    MISMA que muta el bucle: leer `peticiones[0]["messages"]` después de dos
    vueltas devuelve el estado final, no el de la primera petición. Para casi
    todo da igual (los mensajes solo crecen por el final), pero no para el
    breakpoint de caché, que se mueve de un turno a otro.
    `instantaneas` es la copia profunda por petición, para poder afirmar sobre
    lo que viajó en cada vuelta.
    """

    def __init__(self, respuestas):
        self._respuestas = list(respuestas)
        self.peticiones: list[dict] = []
        self.instantaneas: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.peticiones.append(kwargs)
        self.instantaneas.append(deepcopy(kwargs))
        if not self._respuestas:
            raise AssertionError("El bucle pidió más vueltas de las pactadas")
        return self._respuestas.pop(0)


@pytest.fixture
def contexto():
    return ContextoMensaje(
        tenant_id=TENANT,
        conversation_id=CONVERSACION,
        user_id=uuid4(),
        canal="whatsapp",
        usuario_id="5215599999999",
        nombre="Ana",
        contenido="¿tienen cita mañana?",
        tipo="text",
        mensaje_id="wamid.A",
        db_message_id=MENSAJE_BD,
        db_message_created_at=None,
        ids_agrupados=[MENSAJE_BD],
    )


@pytest.fixture(autouse=True)
def sin_base(monkeypatch):
    """Corta todo lo que tocaría Postgres; cada test repone lo que necesite."""

    async def config(_tenant):
        return {
            "agent_name": "Asistente",
            "system_prompt": "Barbería El Corte. Cortes y barba.",
            "model": "claude-sonnet-5",
            "temperature": None,
            "history_window": 30,
        }

    async def config_numero(_agente_canal_id):
        return await config(None)

    async def config_social(_tenant):
        return await config(None)

    async def catalogo(_tenant, _agente_canal_id=None):
        return "- tool_key: \"consultar_servicios\"", [
            {"tool_key": "consultar_servicios"}
        ]

    async def historial(_ctx, _ventana):
        return []

    async def eventos(_conv):
        return []

    async def registrar(_ctx, _resp):
        return None

    monkeypatch.setattr(prompt, "config_tenant", config)
    monkeypatch.setattr(prompt, "config_numero", config_numero)
    monkeypatch.setattr(prompt, "config_social", config_social)
    monkeypatch.setattr(prompt, "catalogo_herramientas", catalogo)
    monkeypatch.setattr(prompt, "historial", historial)
    monkeypatch.setattr(prompt, "eventos_de_contexto", eventos)
    monkeypatch.setattr(loop_agente, "_registrar_consumo", registrar)


def usar_cliente(monkeypatch, respuestas) -> ClienteFalso:
    cliente = ClienteFalso(respuestas)
    monkeypatch.setattr(loop_agente, "_cliente", lambda: cliente)
    return cliente


# ============================================================
# 1. Respuesta directa
# ============================================================
async def test_respuesta_sin_herramientas(monkeypatch, contexto):
    cliente = usar_cliente(
        monkeypatch, [respuesta_api([bloque_texto("Sí, mañana a las 10.")])]
    )

    r = await loop_agente.responder(contexto)

    assert r.texto == "Sí, mañana a las 10."
    assert r.transferida is False
    assert len(cliente.peticiones) == 1


async def test_respuesta_vacia_cae_a_un_texto_util(monkeypatch, contexto):
    """Un turno sin texto dejaría al cliente sin nada que leer."""
    usar_cliente(monkeypatch, [respuesta_api([])])
    r = await loop_agente.responder(contexto)
    assert r.texto == loop_agente.RESPUESTA_VACIA


# ============================================================
# 2 y 3. Herramientas
# ============================================================
async def test_llamada_a_herramienta_vuelve_al_modelo(monkeypatch, contexto):
    ejecutadas = []

    async def falsa(**kwargs):
        ejecutadas.append(kwargs)
        return "Corte 200, barba 150."

    monkeypatch.setattr(herramientas, "ejecutar", falsa)
    cliente = usar_cliente(
        monkeypatch,
        [
            respuesta_api(
                [bloque_tool("toolu_1", "ejecutar_herramienta",
                             {"tool_key": "consultar_servicios", "parametros": {}})],
                stop_reason="tool_use",
            ),
            respuesta_api([bloque_texto("El corte cuesta 200.")]),
        ],
    )

    r = await loop_agente.responder(contexto)

    assert r.texto == "El corte cuesta 200."
    assert len(ejecutadas) == 1
    assert ejecutadas[0]["tool_key"] == "consultar_servicios"

    # La segunda petición lleva el resultado de la herramienta.
    segunda = cliente.peticiones[1]["messages"]
    resultado = segunda[-1]["content"][0]
    assert resultado["type"] == "tool_result"
    assert resultado["tool_use_id"] == "toolu_1"
    assert resultado["content"] == "Corte 200, barba 150."


async def test_varias_herramientas_en_un_solo_mensaje(monkeypatch, contexto):
    """
    Los resultados de una respuesta con varias herramientas van juntos.
    Repartirlos en mensajes distintos le enseña al modelo a dejar de pedirlas
    en paralelo.
    """
    async def falsa(**kwargs):
        return f"ok {kwargs['tool_key']}"

    monkeypatch.setattr(herramientas, "ejecutar", falsa)
    cliente = usar_cliente(
        monkeypatch,
        [
            respuesta_api(
                [
                    bloque_tool("toolu_1", "ejecutar_herramienta", {"tool_key": "a"}),
                    bloque_tool("toolu_2", "ejecutar_herramienta", {"tool_key": "b"}),
                ],
                stop_reason="tool_use",
            ),
            respuesta_api([bloque_texto("Listo.")]),
        ],
    )

    await loop_agente.responder(contexto)

    ultimo = cliente.peticiones[1]["messages"][-1]
    assert ultimo["role"] == "user"
    assert len(ultimo["content"]) == 2
    assert {b["tool_use_id"] for b in ultimo["content"]} == {"toolu_1", "toolu_2"}


async def test_una_herramienta_que_falla_igual_devuelve_resultado(monkeypatch, contexto):
    """Omitir un tool_result rompe el turno: el error también se contesta."""
    async def falsa(**kwargs):
        return herramientas.ERROR_TECNICO

    monkeypatch.setattr(herramientas, "ejecutar", falsa)
    cliente = usar_cliente(
        monkeypatch,
        [
            respuesta_api(
                [bloque_tool("toolu_1", "ejecutar_herramienta", {"tool_key": "a"})],
                stop_reason="tool_use",
            ),
            respuesta_api([bloque_texto("Disculpa, no pude consultarlo.")]),
        ],
    )

    await loop_agente.responder(contexto)

    resultado = cliente.peticiones[1]["messages"][-1]["content"][0]
    assert resultado["tool_use_id"] == "toolu_1"
    assert resultado["content"] == herramientas.ERROR_TECNICO


async def test_herramienta_inventada_no_rompe_el_turno(monkeypatch, contexto):
    cliente = usar_cliente(
        monkeypatch,
        [
            respuesta_api(
                [bloque_tool("toolu_1", "no_existe", {})], stop_reason="tool_use"
            ),
            respuesta_api([bloque_texto("Perdona, me equivoqué.")]),
        ],
    )

    r = await loop_agente.responder(contexto)

    assert r.texto == "Perdona, me equivoqué."
    assert cliente.peticiones[1]["messages"][-1]["content"][0]["tool_use_id"] == "toolu_1"


# ============================================================
# 4. El modelo no elige el tenant ni la conversación
# ============================================================
async def test_el_modelo_no_puede_elegir_tenant_ni_conversacion(monkeypatch, contexto):
    """
    Aunque el modelo mande tenant_id y conversation_id en los parámetros, lo
    que llega a la herramienta es lo del contexto. Si pudiera elegirlos,
    pediría datos de la conversación de otro cliente.
    """
    recibido = {}

    async def falsa(**kwargs):
        recibido.update(kwargs)
        return "ok"

    monkeypatch.setattr(herramientas, "ejecutar", falsa)
    usar_cliente(
        monkeypatch,
        [
            respuesta_api(
                [
                    bloque_tool("toolu_1", "ejecutar_herramienta", {
                        "tool_key": "a",
                        "parametros": {
                            "tenant_id": str(uuid4()),
                            "conversation_id": str(uuid4()),
                        },
                    })
                ],
                stop_reason="tool_use",
            ),
            respuesta_api([bloque_texto("Listo.")]),
        ],
    )

    await loop_agente.responder(contexto)

    assert recibido["tenant_id"] == TENANT
    assert recibido["conversation_id"] == CONVERSACION


async def test_el_esquema_de_escalar_no_expone_la_conversacion():
    """Mismo criterio, visto desde el esquema que recibe el modelo."""
    propiedades = escalar.definicion()["input_schema"]["properties"]
    assert set(propiedades) == {"motivo", "area"}


# ============================================================
# 5. Escalamiento
# ============================================================
async def test_al_escalar_el_bot_calla(monkeypatch, contexto):
    async def falso_escalar(**kwargs):
        return escalar.OK

    monkeypatch.setattr(escalar, "escalar", falso_escalar)
    usar_cliente(
        monkeypatch,
        [
            respuesta_api(
                [bloque_tool("toolu_1", "escalar_humano",
                             {"motivo": "queja", "area": "otro"})],
                stop_reason="tool_use",
            )
        ],
    )

    r = await loop_agente.responder(contexto)

    # Corta sin pedir otra vuelta: el acuse es texto fijo, no del modelo.
    assert r.transferida is True


# ============================================================
# 6. Rechazo del modelo
# ============================================================
async def test_un_rechazo_no_se_lee_como_contenido(monkeypatch, contexto):
    """
    Un rechazo llega como 200 con `stop_reason: refusal` y sin contenido
    utilizable. Hay que mirar stop_reason antes de leer content.
    """
    usar_cliente(
        monkeypatch,
        [respuesta_api([], stop_reason="refusal", categoria="cyber")],
    )

    r = await loop_agente.responder(contexto)

    assert r.texto == loop_agente.RESPUESTA_RECHAZO
    assert "persona" in r.texto


# ============================================================
# 7. El bucle corta
# ============================================================
async def test_el_bucle_no_gira_para_siempre(monkeypatch, contexto):
    """Un modelo que nunca deja de pedir herramientas tiene que topar."""
    async def falsa(**kwargs):
        return "ok"

    monkeypatch.setattr(herramientas, "ejecutar", falsa)
    cliente = usar_cliente(
        monkeypatch,
        [
            respuesta_api(
                [bloque_tool(f"toolu_{i}", "ejecutar_herramienta", {"tool_key": "a"})],
                stop_reason="tool_use",
            )
            for i in range(loop_agente.MAX_VUELTAS)
        ],
    )

    await loop_agente.responder(contexto)

    assert len(cliente.peticiones) == loop_agente.MAX_VUELTAS


# ============================================================
# 8. Consumo de tokens — lo que sustituye a medir-uso-tokens
# ============================================================
async def test_el_consumo_se_suma_entre_vueltas(monkeypatch, contexto):
    """
    Una respuesta con herramientas son varias llamadas al modelo, y todas
    gastan. En n8n esto obligaba a un workflow aparte que sondeaba la API de
    ejecuciones cada 2 minutos; acá viene en cada respuesta.
    """
    async def falsa(**kwargs):
        return "ok"

    monkeypatch.setattr(herramientas, "ejecutar", falsa)
    usar_cliente(
        monkeypatch,
        [
            respuesta_api(
                [bloque_tool("toolu_1", "ejecutar_herramienta", {"tool_key": "a"})],
                stop_reason="tool_use",
                entrada=100, salida=20,
            ),
            respuesta_api([bloque_texto("Listo.")], entrada=150, salida=30),
        ],
    )

    r = await loop_agente.responder(contexto)

    assert r.consumo.llamadas == 2
    assert r.consumo.entrada == 250
    assert r.consumo.salida == 50


# ============================================================
# 9. Forma de la petición
# ============================================================
async def test_el_historial_va_en_messages_y_no_en_el_system(monkeypatch, contexto):
    """
    En n8n el historial se concatenaba dentro del system prompt, lo que hacía
    que el prefijo cambiara en cada mensaje y el caché nunca acertara.
    """
    async def historial(_ctx, _ventana):
        return [
            {"role": "user", "content": "hola"},
            {"role": "assistant", "content": "¡Hola! ¿En qué te ayudo?"},
        ]

    monkeypatch.setattr(prompt, "historial", historial)
    cliente = usar_cliente(monkeypatch, [respuesta_api([bloque_texto("Dime.")])])

    await loop_agente.responder(contexto)

    peticion = cliente.peticiones[0]
    assert [m["role"] for m in peticion["messages"]] == ["user", "assistant", "user"]
    assert peticion["messages"][-1]["content"] == contexto.contenido

    system_completo = " ".join(b["text"] for b in peticion["system"])
    assert "¡Hola! ¿En qué te ayudo?" not in system_completo


async def test_el_bloque_estable_se_marca_para_cache_y_la_fecha_queda_fuera(
    monkeypatch, contexto
):
    """
    El corte va al final del bloque estable. La fecha y hora tienen que
    quedar DESPUÉS: dentro del bloque cacheado invalidarían el prefijo en
    cada mensaje.
    """
    cliente = usar_cliente(monkeypatch, [respuesta_api([bloque_texto("ok")])])

    await loop_agente.responder(contexto)

    system = cliente.peticiones[0]["system"]
    assert system[0].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in system[1]
    assert "Fecha y hora" in system[1]["text"]
    assert "Fecha y hora" not in system[0]["text"]


async def test_las_reglas_de_plataforma_van_antes_del_prompt_del_tenant(
    monkeypatch, contexto
):
    """El dueño del negocio no puede sobrescribirlas (hallazgo 10)."""
    cliente = usar_cliente(monkeypatch, [respuesta_api([bloque_texto("ok")])])

    await loop_agente.responder(contexto)

    estable = cliente.peticiones[0]["system"][0]["text"]
    assert estable.index("asistente automatizado") < estable.index("Barbería El Corte")
    assert "No pidas ni registres datos sensibles" in estable


async def test_sonnet_no_recibe_temperature(monkeypatch, contexto):
    """
    `tenant_agent_config.temperature` existe y n8n se la pasaba al modelo,
    pero Sonnet 5 y Opus 5 la rechazan con 400. Solo viaja donde es válida.
    """
    async def config(_tenant):
        return {
            "agent_name": "Asistente",
            "system_prompt": "x",
            "model": "claude-sonnet-5",
            "temperature": 0.7,
            "history_window": 30,
        }

    monkeypatch.setattr(prompt, "config_social", config)
    cliente = usar_cliente(monkeypatch, [respuesta_api([bloque_texto("ok")])])

    await loop_agente.responder(contexto)

    assert "temperature" not in cliente.peticiones[0]


# ============================================================
# 10. Errores de la API
# ============================================================
async def test_una_caida_de_red_no_tumba_la_peticion(monkeypatch, contexto):
    class ClienteCaido:
        def __init__(self):
            self.messages = SimpleNamespace(create=self._create)

        async def _create(self, **kwargs):
            raise anthropic.APIConnectionError(request=None)

    monkeypatch.setattr(loop_agente, "_cliente", ClienteCaido)

    r = await loop_agente.responder(contexto)

    assert r.texto == loop_agente.RESPUESTA_VACIA


async def test_clave_rechazada_avisa_en_el_log(monkeypatch, contexto, caplog):
    """Un 401 de Anthropic casi siempre es la clave: el log debe decirlo."""
    respuesta_rechazada = SimpleNamespace(status_code=401, headers={}, request=None)

    class ClaveRechazada:
        def __init__(self):
            self.messages = SimpleNamespace(create=self._create)

        async def _create(self, **kwargs):
            raise anthropic.AuthenticationError(
                message="invalid x-api-key", response=respuesta_rechazada, body=None
            )

    monkeypatch.setattr(loop_agente, "_cliente", ClaveRechazada)

    with caplog.at_level("ERROR", logger="operativai.agente.loop"):
        r = await loop_agente.responder(contexto)

    assert r.texto == loop_agente.RESPUESTA_VACIA
    assert "ANTHROPIC_API_KEY" in caplog.text


# ============================================================
# 8. Caché del historial
# ============================================================
def _marcas(mensajes: list[dict]) -> list[tuple[int, int]]:
    """Posiciones (mensaje, bloque) con `cache_control` en una petición."""
    marcas = []
    for i, mensaje in enumerate(mensajes):
        contenido = mensaje.get("content")
        if not isinstance(contenido, list):
            continue
        for j, bloque in enumerate(contenido):
            if isinstance(bloque, dict) and "cache_control" in bloque:
                marcas.append((i, j))
    return marcas


async def test_sin_herramientas_no_se_marca_el_historial(monkeypatch, contexto):
    """
    Una conversación que se resuelve en una sola llamada no marca nada en los
    mensajes: no habrá segunda petición que lea esa entrada, así que escribirla
    solo pagaría el recargo de escritura. El bloque estable de `system` sí se
    marca, porque ese se reaprovecha entre conversaciones del mismo negocio.
    """
    cliente = usar_cliente(monkeypatch, [respuesta_api([bloque_texto("ok")])])

    await loop_agente.responder(contexto)

    assert _marcas(cliente.instantaneas[0]["messages"]) == []
    assert cliente.instantaneas[0]["system"][0].get("cache_control")


async def test_tras_una_vuelta_se_marca_el_ultimo_turno(monkeypatch, contexto):
    """Con una segunda vuelta asegurada, el prefijo acumulado sí se cachea."""
    async def falsa(**_kwargs):
        return "Corte 200."

    monkeypatch.setattr(herramientas, "ejecutar", falsa)
    cliente = usar_cliente(
        monkeypatch,
        [
            respuesta_api(
                [bloque_tool("toolu_1", "ejecutar_herramienta", {"tool_key": "a"})],
                stop_reason="tool_use",
            ),
            respuesta_api([bloque_texto("El corte cuesta 200.")]),
        ],
    )

    await loop_agente.responder(contexto)

    # La primera petición no lleva marca; la segunda, en el último bloque del
    # último turno (el mensaje con los resultados de herramienta).
    assert _marcas(cliente.instantaneas[0]["messages"]) == []

    mensajes = cliente.instantaneas[1]["messages"]
    assert _marcas(mensajes) == [(len(mensajes) - 1, len(mensajes[-1]["content"]) - 1)]
    assert mensajes[-1]["content"][-1]["type"] == "tool_result"


async def test_el_breakpoint_se_mueve_y_no_se_acumula(monkeypatch, contexto):
    """
    Con dos vueltas de herramientas, la marca viaja al turno nuevo y se quita
    del anterior. Acumularlas daría un 400: la petición admite 4 breakpoints
    y `system` ya gasta uno.
    """
    async def falsa(**_kwargs):
        return "dato"

    monkeypatch.setattr(herramientas, "ejecutar", falsa)
    cliente = usar_cliente(
        monkeypatch,
        [
            respuesta_api(
                [bloque_tool("toolu_1", "ejecutar_herramienta", {"tool_key": "a"})],
                stop_reason="tool_use",
            ),
            respuesta_api(
                [bloque_tool("toolu_2", "ejecutar_herramienta", {"tool_key": "b"})],
                stop_reason="tool_use",
            ),
            respuesta_api([bloque_texto("listo")]),
        ],
    )

    await loop_agente.responder(contexto)

    assert len(cliente.instantaneas) == 3
    for i, peticion in enumerate(cliente.instantaneas):
        marcas_mensajes = _marcas(peticion["messages"])
        marcas_system = [b for b in peticion["system"] if "cache_control" in b]
        assert len(marcas_mensajes) <= 1, f"petición {i}: marcas acumuladas"
        # El tope de la API es 4 en total, contando las de system.
        assert len(marcas_mensajes) + len(marcas_system) <= 4

    # Y en las dos últimas la marca está en el turno recién añadido.
    for peticion in cliente.instantaneas[1:]:
        mensajes = peticion["messages"]
        assert _marcas(mensajes) == [
            (len(mensajes) - 1, len(mensajes[-1]["content"]) - 1)
        ]
