"""
Tests del coste en dólares del consumo del agente (services/agente/costos.py)
y de que ese coste llegue a `tenant_token_usage`.

Lo que importa si falla:

  1. la entrada NO es un precio único: lo cacheado cuesta ~0,1x y lo escrito
     en caché 1,25x. Cobrar el total a tarifa plena infla la factura de
     cualquier conversación con caché, que son todas
  2. un modelo sin tarifa da None y no 0 — un cero se sumaría en los informes
     como si el mensaje hubiera sido gratis
  3. el id que devuelve la API puede traer fecha (`...-20251001`) y aun así
     tiene que encontrar su tarifa
  4. `_registrar_consumo` llama a `registrar_uso_tokens` con TODOS sus
     argumentos obligatorios. Esto es una regresión con historia: faltaba
     `costo_usd`, la llamada levantaba TypeError, el `except Exception` se lo
     comía y NO se registraba nada —ni tokens ni usage_events— en silencio.
     Los tests del bucle no lo veían porque sustituyen `_registrar_consumo`
     entero, así que nadie ejercitaba la llamada real.
"""

import inspect
from decimal import Decimal
from uuid import uuid4

import pytest

from services import gerencia
from services.agente import costos, loop_agente
from services.agente.modelos import ContextoMensaje

TENANT = uuid4()
CONVERSACION = uuid4()
MENSAJE_BD = uuid4()


# ============================================================
# 1. La tarifa y el cálculo (puro)
# ============================================================
def test_entrada_simple_por_modelo():
    """Mil tokens de entrada, sin caché ni salida, en los tres del catálogo."""
    assert costos.calcular_costo("claude-haiku-4-5", entrada=1000, salida=0) == Decimal(
        "0.001000"
    )
    assert costos.calcular_costo("claude-sonnet-5", entrada=1000, salida=0) == Decimal(
        "0.003000"
    )
    assert costos.calcular_costo("claude-opus-5", entrada=1000, salida=0) == Decimal(
        "0.005000"
    )


def test_la_salida_cuesta_cinco_veces_la_entrada():
    entrada = costos.calcular_costo("claude-haiku-4-5", entrada=1000, salida=0)
    salida = costos.calcular_costo("claude-haiku-4-5", entrada=0, salida=1000)
    assert salida == entrada * 5


def test_lo_cacheado_cuesta_una_decima_parte():
    plena = costos.calcular_costo("claude-haiku-4-5", entrada=1000, salida=0)
    cacheada = costos.calcular_costo(
        "claude-haiku-4-5", entrada=0, salida=0, cache_lectura=1000
    )
    assert cacheada == Decimal("0.000100")
    assert cacheada * 10 == plena


def test_escribir_en_cache_cuesta_un_25_por_ciento_mas():
    plena = costos.calcular_costo("claude-haiku-4-5", entrada=1000, salida=0)
    escritura = costos.calcular_costo(
        "claude-haiku-4-5", entrada=0, salida=0, cache_escritura=1000
    )
    assert escritura == Decimal("0.001250")
    assert escritura == plena * Decimal("1.25")


def test_sumar_la_entrada_antes_de_cobrarla_infla_la_factura():
    """
    El motivo de que `calcular_costo` reciba las tres partes por separado.

    `tenant_token_usage.tokens_entrada` guarda el total sumado porque mide
    VOLUMEN. Cobrar ese total a tarifa plena es otra cosa: acá son 2,5 veces
    más de lo que cuesta en realidad.
    """
    real = costos.calcular_costo(
        "claude-haiku-4-5", entrada=1000, salida=200, cache_lectura=4000
    )
    como_si_todo_fuera_entrada_plena = costos.calcular_costo(
        "claude-haiku-4-5", entrada=5000, salida=200
    )
    assert real == Decimal("0.002400")
    assert como_si_todo_fuera_entrada_plena == Decimal("0.006000")
    assert como_si_todo_fuera_entrada_plena > real * 2


@pytest.mark.parametrize(
    "modelo",
    ["claude-haiku-4-5-20251001", "claude-sonnet-5-20260401", "claude-opus-5-20260101"],
)
def test_un_id_con_fecha_encuentra_su_tarifa(modelo):
    """
    Se registra `respuesta.model` —lo que devolvió la API—, y ahí el id puede
    venir con la fecha del snapshot aunque se haya pedido el alias.
    """
    assert costos.tarifa_de(modelo) is not None
    assert costos.calcular_costo(modelo, entrada=1000, salida=0) > 0


def test_el_prefijo_no_confunde_familias():
    """Opus con fecha cobra como Opus, no como otra familia más corta."""
    assert costos.tarifa_de("claude-opus-4-8-20260210") == costos.tarifa_de(
        "claude-opus-4-8"
    )
    assert costos.tarifa_de("claude-sonnet-4-6-20251120") == costos.tarifa_de(
        "claude-sonnet-4-6"
    )


@pytest.mark.parametrize("modelo", [None, "", "gpt-4o", "claude-inventado-9", "claude"])
def test_un_modelo_sin_tarifa_da_none_y_no_cero(modelo):
    """
    Un 0 diría "este mensaje fue gratis" y se sumaría en los informes como
    tal. None deja la columna en NULL, que es "no se sabe qué costó".
    """
    assert costos.tarifa_de(modelo) is None
    assert costos.calcular_costo(modelo, entrada=1000, salida=500) is None


def test_el_importe_cabe_en_la_columna():
    """`costo_usd` es NUMERIC(12,6): 6 decimales y 6 enteros."""
    costo = costos.calcular_costo(
        "claude-opus-5", entrada=1_000_000, salida=1_000_000, cache_lectura=1_000_000
    )
    assert costo.as_tuple().exponent == -6
    assert costo < Decimal("1000000")


# ============================================================
# 2. Que el coste llegue a la tabla
# ============================================================
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


def respuesta_con_consumo(modelo="claude-sonnet-5", **tokens):
    consumo = loop_agente.Consumo(
        entrada=tokens.get("entrada", 1000),
        salida=tokens.get("salida", 200),
        cache_lectura=tokens.get("cache_lectura", 4000),
        cache_escritura=tokens.get("cache_escritura", 0),
        llamadas=tokens.get("llamadas", 1),
    )
    return loop_agente.Respuesta(texto="ok", consumo=consumo, modelo=modelo)


@pytest.fixture
def registrado(monkeypatch):
    """
    Captura la llamada a `registrar_uso_tokens` VALIDÁNDOLA contra la firma
    real antes de aceptarla.

    Un doble con `**kwargs` se tragaría un argumento obligatorio que falte,
    que es exactamente el bug que hubo. Con `signature().bind()` el doble
    exige el mismo contrato que la función de verdad, y sigue exigiéndolo si
    alguien le añade parámetros mañana.
    """
    firma = inspect.signature(gerencia.registrar_uso_tokens)
    capturado: dict = {}

    async def falsa(*args, **kwargs):
        try:
            firma.bind(*args, **kwargs)  # TypeError si falta un obligatorio
        except TypeError as e:
            # `_registrar_consumo` se traga cualquier excepción a propósito
            # (perder la medición no puede costarle la respuesta al cliente),
            # así que se deja anotada para que el test pueda decir QUÉ pasó en
            # vez de fallar con un KeyError sin contexto.
            capturado["__error__"] = str(e)
            raise
        capturado.update(kwargs)
        capturado["tenant_id"] = args[0] if args else kwargs.get("tenant_id")
        return True

    async def execute(*_args, **_kwargs):
        return "INSERT 0 1"

    monkeypatch.setattr(gerencia, "registrar_uso_tokens", falsa)
    monkeypatch.setattr(loop_agente, "execute", execute)
    return capturado


def exigir_llamada(registrado: dict) -> None:
    """Falla con el motivo, no con un KeyError, si la llamada no se hizo."""
    assert "__error__" not in registrado, (
        "registrar_uso_tokens se llamó incumpliendo su firma: "
        + registrado["__error__"]
    )
    assert registrado, "registrar_uso_tokens no se llamó"


async def test_se_registra_el_costo_en_dolares(contexto, registrado):
    """El caso que faltaba: la llamada real, con todos sus argumentos."""
    await loop_agente._registrar_consumo(contexto, respuesta_con_consumo())

    exigir_llamada(registrado)
    assert registrado["tenant_id"] == TENANT
    # 1000 de entrada + 4000 leídos de caché, 200 de salida, en Sonnet 5.
    assert registrado["costo_usd"] == Decimal("0.007200")
    # El volumen sí va sumado: es lo que mide la columna de tokens.
    assert registrado["tokens_entrada"] == 5000
    assert registrado["tokens_salida"] == 200


async def test_el_volumen_va_sumado_pero_el_precio_no(contexto, registrado):
    """
    Las dos columnas dicen cosas distintas a propósito: `tokens_entrada` es
    volumen procesado y `costo_usd` es dinero. Si el coste se hubiera
    calculado sobre el volumen, saldría 0,021 en vez de 0,0072.
    """
    await loop_agente._registrar_consumo(contexto, respuesta_con_consumo())

    exigir_llamada(registrado)
    volumen = registrado["tokens_entrada"]
    costo_si_fuera_plano = costos.calcular_costo(
        "claude-sonnet-5", entrada=volumen, salida=registrado["tokens_salida"]
    )
    assert registrado["costo_usd"] < costo_si_fuera_plano


async def test_un_modelo_desconocido_registra_tokens_con_costo_nulo(
    contexto, registrado
):
    """Se pierde el precio, no la medición."""
    await loop_agente._registrar_consumo(
        contexto, respuesta_con_consumo(modelo="modelo-que-no-existe")
    )

    exigir_llamada(registrado)
    assert registrado["costo_usd"] is None
    assert registrado["tokens_entrada"] == 5000


async def test_sin_llamadas_no_se_registra_nada(contexto, registrado):
    """Un mensaje que no llegó al modelo no deja fila de consumo."""
    await loop_agente._registrar_consumo(
        contexto, respuesta_con_consumo(llamadas=0)
    )

    assert registrado == {}
