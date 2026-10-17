"""
Cuenta global de WhatsApp (spec 004, T2): parte de envío.

Sin BD ni red: `fetch_one` (get_channel_credentials) y `httpx.AsyncClient` se
sustituyen por fakes. La parte de respuesta fija (T9) se añadirá a este archivo.
"""

from typing import Any
from uuid import uuid4

import pytest

from config import settings
from services import cuenta_global_wa, whatsapp
from services.whatsapp import ProveedorWhatsAppError

TENANT_GLOBAL = "a383f05c-7028-4d5f-8c5d-30232a80faf3"


class _RespuestaFalsa:
    def __init__(self, estado: int = 200, cuerpo: dict[str, Any] | None = None):
        self.status_code = estado
        self._cuerpo = cuerpo if cuerpo is not None else {
            "success": True,
            "data": {"message_id": "wamid.1", "status": "sent"},
        }
        self.text = str(self._cuerpo)

    def json(self) -> dict[str, Any]:
        return self._cuerpo


class _Red:
    """Registra cada POST que saldría a NeuroAPI."""

    def __init__(self) -> None:
        self.llamadas: list[dict[str, Any]] = []
        self.respuesta = _RespuestaFalsa()

    def instalar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        red = self

        class _ClienteFalso:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "_ClienteFalso":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]):
                red.llamadas.append({"url": url, "json": json, "headers": headers})
                return red.respuesta

        monkeypatch.setattr(whatsapp.httpx, "AsyncClient", _ClienteFalso)


class _Credenciales:
    """Fake de `get_channel_credentials`: registra los tenants consultados."""

    def __init__(self, fila: dict[str, Any] | None) -> None:
        self.fila = fila
        self.consultas: list[tuple[str, tuple[Any, ...]]] = []

    def instalar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fetch_one(sql: str, *args: Any) -> dict[str, Any] | None:
            self.consultas.append((sql, args))
            return self.fila

        monkeypatch.setattr(cuenta_global_wa, "fetch_one", _fetch_one)


def _fila(**extra: Any) -> dict[str, Any]:
    base = {
        "phone_number_id": "PNID-GLOBAL",
        "access_token": "clave-del-canal",
        "bsp_provider": "neuroapi",
    }
    base.update(extra)
    return base


@pytest.fixture
def configurado(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AVISOS_CUENTA_GLOBAL_TENANT_ID", TENANT_GLOBAL)
    monkeypatch.setattr(settings, "NEUROAPI_API_KEY", "clave-de-entorno")
    monkeypatch.setattr(settings, "NEUROAPI_API_BASE_URL", "https://neuro.test/api/v1")


async def test_envio_usa_es_mx_y_variables(monkeypatch, configurado):
    red = _Red()
    red.instalar(monkeypatch)
    _Credenciales(_fila()).instalar(monkeypatch)

    resultado = await cuenta_global_wa.enviar_plantilla(
        "+525512345678", "notificacion_cancelacion", ["Corte", "16-10-2026"]
    )

    assert resultado.id_mensaje == "wamid.1"
    assert len(red.llamadas) == 1
    llamada = red.llamadas[0]
    assert llamada["url"] == "https://neuro.test/api/v1/neuroapi/messaging/send"
    cuerpo = llamada["json"]
    assert cuerpo["type"] == "template"
    assert cuerpo["to"] == "+525512345678"
    assert cuerpo["from_phone_number_id"] == "PNID-GLOBAL"
    assert cuerpo["template"]["name"] == "notificacion_cancelacion"
    assert cuerpo["template"]["language"] == {"code": "es_MX"}
    assert cuerpo["template"]["components"] == [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": "Corte"},
                {"type": "text", "text": "16-10-2026"},
            ],
        }
    ]
    assert llamada["headers"]["x-api-key"] == "clave-del-canal"


async def test_envio_solo_consulta_el_tenant_configurado(monkeypatch, configurado):
    _Red().instalar(monkeypatch)
    creds = _Credenciales(_fila())
    creds.instalar(monkeypatch)

    await cuenta_global_wa.enviar_plantilla("+525512345678", "recordatorio_cita", ["A", "B"])

    assert len(creds.consultas) == 1
    sql, args = creds.consultas[0]
    assert "get_channel_credentials" in sql
    assert str(args[0]) == TENANT_GLOBAL
    assert args[1] == "whatsapp"
    # Nunca el tenant de una reserva cualquiera.
    assert str(uuid4()) not in [str(a) for a in args]


async def test_envio_canal_inactivo_falla_antes_de_la_red(monkeypatch, configurado):
    red = _Red()
    red.instalar(monkeypatch)
    _Credenciales(None).instalar(monkeypatch)  # la función solo devuelve activos

    with pytest.raises(ProveedorWhatsAppError):
        await cuenta_global_wa.enviar_plantilla("+525512345678", "recordatorio_cita", ["A"])
    assert red.llamadas == []


async def test_envio_proveedor_distinto_de_neuroapi_falla_antes_de_la_red(monkeypatch, configurado):
    red = _Red()
    red.instalar(monkeypatch)
    _Credenciales(_fila(bsp_provider="meta")).instalar(monkeypatch)

    with pytest.raises(ProveedorWhatsAppError):
        await cuenta_global_wa.enviar_plantilla("+525512345678", "recordatorio_cita", ["A"])
    assert red.llamadas == []


async def test_envio_sin_tenant_configurado_falla_antes_de_la_red(monkeypatch):
    monkeypatch.setattr(settings, "AVISOS_CUENTA_GLOBAL_TENANT_ID", "")
    red = _Red()
    red.instalar(monkeypatch)
    creds = _Credenciales(_fila())
    creds.instalar(monkeypatch)

    with pytest.raises(ProveedorWhatsAppError):
        await cuenta_global_wa.enviar_plantilla("+525512345678", "recordatorio_cita", ["A"])
    assert red.llamadas == []
    assert creds.consultas == []


async def test_envio_tenant_mal_formado_falla_antes_de_la_red(monkeypatch):
    monkeypatch.setattr(settings, "AVISOS_CUENTA_GLOBAL_TENANT_ID", "no-es-uuid")
    red = _Red()
    red.instalar(monkeypatch)
    _Credenciales(_fila()).instalar(monkeypatch)

    with pytest.raises(ProveedorWhatsAppError):
        await cuenta_global_wa.enviar_plantilla("+525512345678", "recordatorio_cita", ["A"])
    assert red.llamadas == []


async def test_envio_fallback_a_neuroapi_api_key(monkeypatch, configurado):
    red = _Red()
    red.instalar(monkeypatch)
    _Credenciales(_fila(access_token="")).instalar(monkeypatch)

    await cuenta_global_wa.enviar_plantilla("+525512345678", "recordatorio_cita", ["A"])

    assert red.llamadas[0]["headers"]["x-api-key"] == "clave-de-entorno"


async def test_envio_sin_ninguna_clave_falla_antes_de_la_red(monkeypatch, configurado):
    monkeypatch.setattr(settings, "NEUROAPI_API_KEY", "")
    red = _Red()
    red.instalar(monkeypatch)
    _Credenciales(_fila(access_token=None)).instalar(monkeypatch)

    with pytest.raises(ProveedorWhatsAppError):
        await cuenta_global_wa.enviar_plantilla("+525512345678", "recordatorio_cita", ["A"])
    assert red.llamadas == []


async def test_envio_sin_phone_number_id_falla_antes_de_la_red(monkeypatch, configurado):
    red = _Red()
    red.instalar(monkeypatch)
    _Credenciales(_fila(phone_number_id=None)).instalar(monkeypatch)

    with pytest.raises(ProveedorWhatsAppError):
        await cuenta_global_wa.enviar_plantilla("+525512345678", "recordatorio_cita", ["A"])
    assert red.llamadas == []


async def test_error_del_proveedor_conserva_el_codigo_http(monkeypatch, configurado):
    red = _Red()
    red.respuesta = _RespuestaFalsa(400, {"success": False, "error": {"code": "BAD_REQUEST"}})
    red.instalar(monkeypatch)
    _Credenciales(_fila()).instalar(monkeypatch)

    with pytest.raises(ProveedorWhatsAppError) as exc:
        await cuenta_global_wa.enviar_plantilla("+525512345678", "recordatorio_cita", ["A"])
    assert exc.value.status_code == 400


def test_el_default_de_idioma_de_whatsapp_no_cambia():
    import inspect

    firma = inspect.signature(whatsapp.NeuroApiProvider.enviar_plantilla)
    assert firma.parameters["idioma"].default == "es"


def test_banderas_por_defecto_apagadas():
    from config import Settings

    assert Settings.AVISOS_CITA_WHATSAPP_ACTIVOS is False
    assert Settings.AVISOS_CUENTA_GLOBAL_RESPUESTA_ACTIVA is False
    assert Settings.AVISOS_CUENTA_GLOBAL_TENANT_ID == ""
    assert Settings.AVISOS_CITA_REVISION_MINUTOS == 5
    assert Settings.AVISOS_EJECUCION_VIVA_MINUTOS == 5


def test_validate_avisa_si_la_bandera_esta_activa_sin_tenant(monkeypatch, caplog):
    monkeypatch.setattr(settings, "AVISOS_CITA_WHATSAPP_ACTIVOS", True)
    monkeypatch.setattr(settings, "AVISOS_CUENTA_GLOBAL_TENANT_ID", "")
    monkeypatch.setattr(settings, "JWT_SECRET", settings.JWT_SECRET or "x")
    monkeypatch.setattr(settings, "META_APP_ID", settings.META_APP_ID or "x")
    monkeypatch.setattr(settings, "META_APP_SECRET", settings.META_APP_SECRET or "x")
    with caplog.at_level("WARNING", logger="operativai.config"):
        settings.validate()
    assert any("AVISOS_CUENTA_GLOBAL_TENANT_ID" in r.getMessage() for r in caplog.records)


# ============================================================
# T9: respuesta fija de la cuenta global (plan 2.13, D13)
# ============================================================
import uuid
from datetime import datetime, timedelta, timezone

from routers import canales_neuroapi_webhook as webhook
from services import entrada_mensajes, respuesta_cuenta_global as rcg

PNID_GLOBAL = "PNID-GLOBAL"
REMITENTE = "5215599999999"  # Meta entrega los móviles de México con el 1
TELEFONO_AVISO = "+525599999999"


def _evento(pnid: str = PNID_GLOBAL, remitente: str | None = REMITENTE) -> dict[str, Any]:
    valor: dict[str, Any] = {"metadata": {"phone_number_id": pnid}}
    if remitente:
        valor["messages"] = [{"from": remitente, "id": "wamid.X", "type": "text",
                              "text": {"body": "hola"}}]
    return {"entry": [{"id": "1", "changes": [{"field": "messages", "value": valor}]}]}


class _Mundo:
    """BD y red falsas del gancho: registra lo que se envía, se escribe y se reenvía."""

    def __init__(self) -> None:
        self.linea_global = PNID_GLOBAL
        self.avisos = [TELEFONO_AVISO]
        self.cliente_del_tenant = False
        self.ultima_respuesta: datetime | None = None
        self.envios: list[tuple[str, str]] = []
        self.escrituras: list[tuple[str, tuple[Any, ...]]] = []
        self.reenvios: list[Any] = []
        self.consultas: list[str] = []
        # Spec 005a: la línea también vale marcada en agentes_canal, sin
        # credencial que coincida.
        self.linea_por_marca = False

    def instalar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mundo = self

        async def _fetch_one(sql: str, *args: Any) -> dict[str, Any] | None:
            mundo.consultas.append(sql)
            if "FROM channel_credentials" in sql:
                if mundo.linea_por_marca:
                    return None
                return {"ok": 1} if args[1] == mundo.linea_global else None
            if "FROM agentes_canal" in sql:
                if mundo.linea_por_marca and args[1] == mundo.linea_global:
                    return {"ok": 1}
                return None
            if "FROM avisos_cita" in sql:
                for v in args[0]:
                    if v in mundo.avisos:
                        return {"telefono_destino": v}
                return None
            if "FROM users" in sql:
                return {"ok": 1} if mundo.cliente_del_tenant else None
            if "FROM cuenta_global_respuestas" in sql:
                if mundo.ultima_respuesta is None:
                    return None
                return {"ultima_respuesta_en": mundo.ultima_respuesta}
            raise AssertionError(f"consulta inesperada: {sql}")

        async def _execute(sql: str, *args: Any) -> None:
            mundo.escrituras.append((sql, args))

        async def _enviar_texto(destino: str, texto: str):
            mundo.envios.append((destino, texto))

        async def _reenviar(cuerpo: bytes, tenant: Any, destino: str) -> None:
            mundo.reenvios.append(tenant)

        async def _tenant_por_numero(pnid: str):
            return uuid.uuid4()

        monkeypatch.setattr(rcg, "fetch_one", _fetch_one)
        monkeypatch.setattr(rcg, "execute", _execute)
        monkeypatch.setattr(cuenta_global_wa, "enviar_texto_global", _enviar_texto)
        monkeypatch.setattr(entrada_mensajes, "reenviar", _reenviar)
        monkeypatch.setattr(entrada_mensajes, "tenant_por_numero", _tenant_por_numero)
        monkeypatch.setattr(entrada_mensajes, "destino_para", lambda t: "https://n8n.test/x")
        monkeypatch.setattr(settings, "AVISOS_CUENTA_GLOBAL_TENANT_ID", TENANT_GLOBAL)


@pytest.fixture
def mundo(monkeypatch: pytest.MonkeyPatch) -> _Mundo:
    m = _Mundo()
    m.instalar(monkeypatch)
    monkeypatch.setattr(settings, "AVISOS_CUENTA_GLOBAL_RESPUESTA_ACTIVA", True)
    return m


async def _recibir(evento: dict[str, Any]) -> None:
    await webhook._reenviar_mensaje(b"{}", evento, "")


def test_texto_fijo_literal():
    assert rcg.TEXTO_RESPUESTA_CUENTA_GLOBAL == (
        "Este mensaje es con fines informativos para mayor informacion "
        "comunicate al numero donde realizaste tu cita"
    )


def test_extraer_remitente_y_variantes():
    assert rcg.extraer_remitente(_evento()) == REMITENTE
    assert rcg.extraer_remitente(_evento(remitente=None)) is None
    assert rcg.extraer_remitente({"data": {"from": "+52 55"}}) == "5255"
    assert rcg.variantes_digitos("5215599999999") == ["5215599999999", "525599999999"]
    assert rcg.variantes_digitos("525599999999") == ["525599999999", "5215599999999"]
    assert rcg.variantes_digitos("14155550100") == ["14155550100"]


async def test_bandera_apagada_todo_se_reenvia(monkeypatch, mundo):
    monkeypatch.setattr(settings, "AVISOS_CUENTA_GLOBAL_RESPUESTA_ACTIVA", False)
    await _recibir(_evento())
    assert len(mundo.reenvios) == 1
    assert mundo.envios == []
    assert mundo.consultas == []


async def test_respuesta_a_aviso_no_se_reenvia_y_recibe_texto_fijo(mundo):
    await _recibir(_evento())
    assert mundo.reenvios == []
    assert mundo.envios == [(TELEFONO_AVISO, rcg.TEXTO_RESPUESTA_CUENTA_GLOBAL)]
    assert len(mundo.escrituras) == 1
    sql, args = mundo.escrituras[0]
    assert "cuenta_global_respuestas" in sql and args == (TELEFONO_AVISO,)
    # Nunca crea evento de contexto.
    assert all("contexto_agente_eventos" not in s for s, _ in mundo.escrituras)


async def test_linea_marcada_en_numeros_tambien_desvia(mundo):
    """Spec 005a RF-16: la marca es_linea_avisos vale sin credencial que coincida."""
    mundo.linea_por_marca = True
    await _recibir(_evento())
    assert mundo.reenvios == []
    assert mundo.envios == [(TELEFONO_AVISO, rcg.TEXTO_RESPUESTA_CUENTA_GLOBAL)]


async def test_segundo_mensaje_dentro_de_24h_no_recibe_nada_ni_se_reenvia(mundo):
    mundo.ultima_respuesta = datetime.now(timezone.utc) - timedelta(hours=2)
    await _recibir(_evento())
    assert mundo.reenvios == []
    assert mundo.envios == []
    assert mundo.escrituras == []


async def test_pasadas_24h_vuelve_a_responder(mundo):
    mundo.ultima_respuesta = datetime.now(timezone.utc) - timedelta(hours=25)
    await _recibir(_evento())
    assert len(mundo.envios) == 1 and mundo.reenvios == []


async def test_remitente_sin_aviso_se_reenvia(mundo):
    mundo.avisos = []
    await _recibir(_evento())
    assert len(mundo.reenvios) == 1 and mundo.envios == []


async def test_cliente_del_tenant_se_reenvia(mundo):
    mundo.cliente_del_tenant = True
    await _recibir(_evento())
    assert len(mundo.reenvios) == 1 and mundo.envios == []


async def test_otro_phone_number_id_sin_cambios(mundo):
    await _recibir(_evento(pnid="OTRA-LINEA"))
    assert len(mundo.reenvios) == 1 and mundo.envios == []
    assert not any("avisos_cita" in c for c in mundo.consultas)


async def test_evento_sin_remitente_se_reenvia(mundo):
    await _recibir(_evento(remitente=None))
    assert len(mundo.reenvios) == 1 and mundo.envios == []


async def test_fallo_del_envio_no_llega_al_agente_ni_se_registra(monkeypatch, mundo):
    async def _falla(destino: str, texto: str):
        raise ProveedorWhatsAppError("caído", status_code=500)

    monkeypatch.setattr(cuenta_global_wa, "enviar_texto_global", _falla)
    await _recibir(_evento())
    assert mundo.reenvios == []
    assert mundo.escrituras == []


async def test_fallo_de_la_comprobacion_sigue_el_flujo_normal(monkeypatch, mundo):
    async def _revienta(sql: str, *args: Any):
        raise RuntimeError("bd caída")

    monkeypatch.setattr(rcg, "fetch_one", _revienta)
    await _recibir(_evento())
    assert len(mundo.reenvios) == 1


async def test_enviar_texto_global_usa_texto_libre(monkeypatch, configurado):
    red = _Red()
    red.instalar(monkeypatch)
    _Credenciales(_fila()).instalar(monkeypatch)
    await cuenta_global_wa.enviar_texto_global("+525512345678", "hola")
    cuerpo = red.llamadas[0]["json"]
    assert cuerpo["type"] == "text" and cuerpo["text"]["body"] == "hola"
    assert cuerpo["from_phone_number_id"] == "PNID-GLOBAL"
