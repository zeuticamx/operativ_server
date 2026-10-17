"""
Tests de la entrega por canal (services/agente/envio.py).

Es el último tramo del flujo y el que falla en silencio: si algo aquí se
rompe, el agente generó una buena respuesta, la guardó en el historial, y el
cliente no recibió nada.

Lo que importa si falla:

  1. el texto largo se parte y se manda entero, no truncado
  2. cada canal va a su URL con su forma de cuerpo
  3. el token viaja SOLO en la cabecera, nunca en el cuerpo (hallazgo 2)
  4. en Instagram se usa el page_id de las credenciales, no el del webhook
  5. fuera de la ventana de 24 h se manda la plantilla, no texto libre
  6. a quien pidió la baja no se le manda la plantilla (hallazgo 1)
  7. un fallo del canal se diagnostica y queda registrado, sin levantar

Ni red ni base: se simulan httpx y las consultas.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.agente import bajas, envio
from services.agente.modelos import Destino

TENANT = uuid4()
CONVERSACION = uuid4()


# ============================================================
# Dobles
# ============================================================
class RespuestaFalsa:
    def __init__(self, status_code=200, text='{"messages":[{"id":"wamid.X"}]}'):
        self.status_code = status_code
        self.text = text


class ClienteHttpFalso:
    """
    Guarda cada POST y responde lo pactado.

    La cola de respuestas se comparte entre instancias: `envio` abre un
    `AsyncClient` nuevo por petición, así que una cola por instancia le daría
    a cada envío la primera respuesta de la lista.
    """

    enviados: list[dict] = []

    def __init__(self, cola: list, error=None):
        self._cola = cola
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, url, json=None, headers=None):
        ClienteHttpFalso.enviados.append({"url": url, "json": json, "headers": headers})
        if self._error:
            raise self._error
        return self._cola.pop(0) if self._cola else RespuestaFalsa()


@pytest.fixture(autouse=True)
def limpiar():
    ClienteHttpFalso.enviados = []
    yield
    ClienteHttpFalso.enviados = []


@pytest.fixture
def registrados():
    """Lo que se escribiría en delivery_errors."""
    return []


@pytest.fixture(autouse=True)
def sin_base(monkeypatch, registrados):
    """Credenciales de WhatsApp por Graph, ventana abierta, sin escrituras."""
    async def fetch_one(query, *args):
        if "get_channel_credentials" in query:
            return {
                "channel_type": args[1],
                "phone_number_id": "PNID-CRED",
                "page_id": "PAGE-CRED",
                "ig_user_id": "IG-CRED",
                "access_token": "SECRETO-DEL-TENANT",
                "bsp_provider": "meta",
            }
        if "tenant_templates" in query:
            return {"template_name": "reapertura", "language_code": "es_MX"}
        return None

    async def fetch_value(query, *args):
        return True  # ventana de 24 h abierta

    async def execute(query, *args):
        if "delivery_errors" in query:
            registrados.append(args)
        return "OK"

    monkeypatch.setattr(envio, "fetch_one", fetch_one)
    monkeypatch.setattr(envio, "fetch_value", fetch_value)
    monkeypatch.setattr(envio, "execute", execute)


@pytest.fixture(autouse=True)
def sin_bajas(monkeypatch):
    """
    Por defecto nadie pidió la baja.

    `bajas` tiene sus propios `fetch_value`/`execute`, así que no basta con
    los dobles de `envio`: sin este fixture la comprobación de la plantilla
    iría a la base de verdad.
    """
    async def fetch_value(query, *args):
        return False

    monkeypatch.setattr(bajas, "fetch_value", fetch_value)


def de_baja(monkeypatch, fallando=False):
    """Pone al cliente de la lista de bajas, o rompe la consulta."""
    async def fetch_value(query, *args):
        if fallando:
            raise RuntimeError("la base no responde")
        return True

    monkeypatch.setattr(bajas, "fetch_value", fetch_value)


def usar_http(monkeypatch, respuestas=None, error=None):
    cola = list(respuestas or [])
    monkeypatch.setattr(
        envio.httpx, "AsyncClient", lambda **kw: ClienteHttpFalso(cola, error)
    )


def destino(canal="whatsapp", **kwargs) -> Destino:
    base = dict(
        tenant_id=TENANT,
        canal=canal,
        usuario_id="5215599999999",
        nombre="Ana",
        conversation_id=CONVERSACION,
    )
    base.update(kwargs)
    return Destino(**base)


# ============================================================
# 1. Partir el texto
# ============================================================
def test_texto_corto_no_se_parte():
    assert envio.partir("hola", 4000) == ["hola"]


def test_parte_por_parrafo_cuando_puede():
    """
    Se prefiere cortar en un final de párrafo... siempre que caiga en la
    segunda mitad del trozo. Un corte muy temprano desaprovecharía el
    mensaje y generaría más fragmentos de los necesarios, así que por debajo
    de la mitad se ignora y se busca un corte más tardío.
    """
    texto = "Primer párrafo con cuerpo suficiente.\n\n" + "x" * 40
    partes = envio.partir(texto, 60)
    assert partes[0] == "Primer párrafo con cuerpo suficiente."


def test_un_corte_demasiado_temprano_se_descarta():
    """
    El párrafo acaba en el carácter 6 de un trozo de 40: cortar ahí daría un
    fragmento casi vacío, así que se busca más adelante.
    """
    partes = envio.partir("Hola.\n\n" + "x" * 60, 40)
    assert partes[0] != "Hola."
    assert len(partes[0]) <= 40


def test_ninguna_parte_excede_el_limite():
    texto = "palabra " * 500
    for parte in envio.partir(texto, 100):
        assert len(parte) <= 100


def test_no_se_pierde_texto_al_partir():
    """Lo peor que puede pasar: el cliente recibe media respuesta."""
    texto = "Tenemos corte, barba y tinte. " * 50
    partes = envio.partir(texto, 200)
    assert "".join(p.replace(" ", "") for p in partes) == texto.replace(" ", "")


def test_una_palabra_mas_larga_que_el_limite_se_corta_en_seco():
    """Mejor cortar una palabra que mandar algo que el canal rechaza."""
    for parte in envio.partir("x" * 300, 100):
        assert len(parte) <= 100


# ============================================================
# 2 y 3. Forma de la petición y el token
# ============================================================
async def test_whatsapp_por_graph(monkeypatch):
    usar_http(monkeypatch)

    r = await envio.entregar(destino(), "Hola Ana")

    assert r.entregado and r.partes == 1
    envio_hecho = ClienteHttpFalso.enviados[0]
    assert "/PNID-CRED/messages" in envio_hecho["url"]
    assert envio_hecho["json"]["messaging_product"] == "whatsapp"
    assert envio_hecho["json"]["text"]["body"] == "Hola Ana"


async def test_el_token_va_en_la_cabecera_y_nunca_en_el_cuerpo(monkeypatch):
    """
    Hallazgo 2 de la auditoría: en n8n el access_token viajaba dentro del
    item y quedaba en el historial de ejecuciones.
    """
    usar_http(monkeypatch)

    await envio.entregar(destino(), "Hola")

    envio_hecho = ClienteHttpFalso.enviados[0]
    assert envio_hecho["headers"]["Authorization"] == "Bearer SECRETO-DEL-TENANT"
    assert "SECRETO-DEL-TENANT" not in str(envio_hecho["json"])


async def test_whatsapp_por_neuroapi_usa_x_api_key(monkeypatch):
    async def fetch_one(query, *args):
        if "get_channel_credentials" in query:
            return {
                "channel_type": "whatsapp",
                "phone_number_id": "PNID-CRED",
                "page_id": None,
                "ig_user_id": None,
                "access_token": "CLAVE-NEUROAPI",
                "bsp_provider": "neuroapi",
            }
        return None

    monkeypatch.setattr(envio, "fetch_one", fetch_one)
    usar_http(monkeypatch)

    await envio.entregar(destino(), "Hola")

    envio_hecho = ClienteHttpFalso.enviados[0]
    assert "/neuroapi/messaging/send" in envio_hecho["url"]
    assert envio_hecho["headers"]["x-api-key"] == "CLAVE-NEUROAPI"
    assert envio_hecho["json"]["from_phone_number_id"] == "PNID-CRED"
    # NeuroAPI no quiere el sobre de Meta.
    assert "messaging_product" not in envio_hecho["json"]
    assert "CLAVE-NEUROAPI" not in str(envio_hecho["json"])


async def test_facebook(monkeypatch):
    usar_http(monkeypatch)

    await envio.entregar(destino(canal="facebook", page_id="PAGE-WEBHOOK"), "Hola")

    envio_hecho = ClienteHttpFalso.enviados[0]
    assert "/PAGE-WEBHOOK/messages" in envio_hecho["url"]
    assert envio_hecho["json"]["recipient"]["id"] == "5215599999999"


async def test_instagram_usa_el_page_id_de_las_credenciales(monkeypatch):
    """
    En Instagram el `entry.id` del webhook es el id de la CUENTA, y el envío
    va contra la página vinculada. Confundirlos hacía fallar la respuesta.
    """
    usar_http(monkeypatch)

    await envio.entregar(
        destino(canal="instagram", page_id="ESTO-ES-LA-CUENTA-IG"), "Hola"
    )

    envio_hecho = ClienteHttpFalso.enviados[0]
    assert "/PAGE-CRED/messages" in envio_hecho["url"]
    assert "ESTO-ES-LA-CUENTA-IG" not in envio_hecho["url"]


async def test_el_texto_largo_se_manda_en_varias_peticiones(monkeypatch):
    usar_http(monkeypatch, respuestas=[RespuestaFalsa() for _ in range(10)])

    r = await envio.entregar(destino(canal="instagram"), "palabra " * 400)

    assert r.entregado
    assert r.partes == len(ClienteHttpFalso.enviados) > 1


async def test_el_widget_no_pasa_por_aqui():
    """Su respuesta viaja en el cuerpo de la petición HTTP del navegador."""
    with pytest.raises(ValueError):
        await envio.entregar(destino(canal="web"), "Hola")


# ============================================================
# 4. Ventana de 24 h
# ============================================================
async def test_ventana_cerrada_manda_plantilla(monkeypatch):
    async def cerrada(query, *args):
        return False

    monkeypatch.setattr(envio, "fetch_value", cerrada)
    usar_http(monkeypatch)

    r = await envio.entregar(destino(), "Este texto NO debe salir")

    assert r.entregado and r.con_plantilla
    envio_hecho = ClienteHttpFalso.enviados[0]
    assert envio_hecho["json"]["type"] == "template"
    assert envio_hecho["json"]["template"]["name"] == "reapertura"
    assert envio_hecho["json"]["template"]["components"][0]["parameters"][0]["text"] == "Ana"
    # Fuera de la ventana Meta no admite texto libre.
    assert "Este texto NO debe salir" not in str(envio_hecho["json"])


async def test_sin_plantilla_configurada_no_se_inventa_nada(monkeypatch, registrados):
    async def cerrada(query, *args):
        return False

    async def sin_plantilla(query, *args):
        if "get_channel_credentials" in query:
            return {
                "channel_type": "whatsapp", "phone_number_id": "PNID-CRED",
                "page_id": None, "ig_user_id": None,
                "access_token": "T", "bsp_provider": "meta",
            }
        return None

    monkeypatch.setattr(envio, "fetch_value", cerrada)
    monkeypatch.setattr(envio, "fetch_one", sin_plantilla)
    usar_http(monkeypatch)

    r = await envio.entregar(destino(), "Hola")

    assert not r.entregado
    assert ClienteHttpFalso.enviados == []
    assert registrados


async def test_sin_conversacion_no_se_comprueba_la_ventana(monkeypatch):
    """
    El aviso de plan agotado sale antes de crear la conversación. Es
    respuesta inmediata a un mensaje del cliente, así que la ventana está
    abierta por definición.
    """
    async def explota(query, *args):
        raise AssertionError("no debería consultarse la ventana")

    monkeypatch.setattr(envio, "fetch_value", explota)
    usar_http(monkeypatch)

    r = await envio.entregar(destino(conversation_id=None), "En breve te atenderemos.")

    assert r.entregado


# ============================================================
# 5. Bajas: el único envío que puede empezar una conversación
# ============================================================
async def test_no_se_manda_plantilla_a_quien_pidio_la_baja(monkeypatch, registrados):
    """
    Hallazgo 1. La plantilla de reapertura es el único mensaje que sale sin
    que el cliente haya escrito, así que es el único que la baja bloquea.
    """
    async def cerrada(query, *args):
        return False

    monkeypatch.setattr(envio, "fetch_value", cerrada)
    de_baja(monkeypatch)
    usar_http(monkeypatch)

    r = await envio.entregar(destino(), "Hola")

    assert not r.entregado
    assert not r.con_plantilla
    # Nada salió al canal.
    assert ClienteHttpFalso.enviados == []
    # Pero el negocio puede ver por qué.
    assert registrados


async def test_si_no_se_puede_consultar_la_baja_la_plantilla_no_sale(
    monkeypatch, registrados
):
    """Falla cerrado: ante la duda no se escribe a quien quizá pidió la baja."""
    async def cerrada(query, *args):
        return False

    monkeypatch.setattr(envio, "fetch_value", cerrada)
    de_baja(monkeypatch, fallando=True)
    usar_http(monkeypatch)

    r = await envio.entregar(destino(), "Hola")

    assert not r.entregado
    assert ClienteHttpFalso.enviados == []


async def test_la_baja_no_bloquea_la_respuesta_dentro_de_la_ventana(monkeypatch):
    """
    Estar de baja NO silencia al agente: si el cliente escribe, se le
    contesta. Al escribir él se abre la ventana y la conversación la inicia
    él, que es lo que la baja no prohíbe.
    """
    de_baja(monkeypatch)
    usar_http(monkeypatch)

    r = await envio.entregar(destino(), "Claro que sí, te agendo el corte.")

    assert r.entregado and r.partes == 1
    assert ClienteHttpFalso.enviados[0]["json"]["type"] == "text"


# ============================================================
# 6. Fallos
# ============================================================
@pytest.mark.parametrize(
    "codigo, esperado",
    [
        (190, "Token inválido o expirado"),
        (131047, "Ventana de 24 h cerrada"),
        (613, "Rate limit"),
    ],
)
def test_diagnostico_de_errores_de_meta(codigo, esperado):
    _, diagnostico = envio._diagnosticar(f'{{"error":{{"code":{codigo}}}}}')
    assert esperado in diagnostico


def test_diagnostico_con_comillas_escapadas():
    """Según por dónde venga, el error llega con las comillas escapadas."""
    codigo, _ = envio._diagnosticar('{\\"error\\":{\\"code\\":190}}')
    assert codigo == 190


def test_diagnostico_de_un_codigo_desconocido_usa_el_mensaje():
    codigo, diagnostico = envio._diagnosticar(
        '{"error":{"code":999,"message":"Algo raro pasó"}}'
    )
    assert codigo == 999
    assert diagnostico == "Algo raro pasó"


async def test_un_rechazo_del_canal_se_registra_y_no_levanta(monkeypatch, registrados):
    usar_http(monkeypatch, respuestas=[RespuestaFalsa(400, '{"error":{"code":190}}')])

    r = await envio.entregar(destino(), "Hola")

    assert not r.entregado
    assert "Token inválido" in r.motivo
    assert registrados


async def test_un_fallo_de_red_se_registra_y_no_levanta(monkeypatch, registrados):
    usar_http(monkeypatch, error=envio.httpx.ConnectError("sin ruta"))

    r = await envio.entregar(destino(), "Hola")

    assert not r.entregado
    assert registrados


async def test_sin_credenciales_no_se_intenta_enviar(monkeypatch, registrados):
    async def sin_cred(query, *args):
        return None

    monkeypatch.setattr(envio, "fetch_one", sin_cred)
    usar_http(monkeypatch)

    r = await envio.entregar(destino(), "Hola")

    assert not r.entregado
    assert ClienteHttpFalso.enviados == []
    assert registrados


async def test_el_131047_en_pleno_envio_cae_a_la_plantilla(monkeypatch):
    """
    La ventana puede cerrarse entre la comprobación y el envío, o el canal
    llevar su propio reloj.
    """
    usar_http(monkeypatch, respuestas=[
        RespuestaFalsa(400, '{"error":{"code":131047}}'),
        RespuestaFalsa(),
    ])

    r = await envio.entregar(destino(), "Hola")

    assert r.entregado and r.con_plantilla
    assert ClienteHttpFalso.enviados[-1]["json"]["type"] == "template"


async def test_no_se_registra_el_texto_del_mensaje(monkeypatch, registrados):
    """El registro de fallos no guarda PII (hallazgo 3)."""
    usar_http(monkeypatch, respuestas=[RespuestaFalsa(400, '{"error":{"code":190}}')])

    await envio.entregar(destino(), "Mi número de tarjeta es 4532 1234 5678 9012")

    assert registrados
    assert "4532" not in str(registrados)
