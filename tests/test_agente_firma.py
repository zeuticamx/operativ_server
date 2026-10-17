"""
Tests de autenticación del webhook del agente (services/agente/firma.py).

Lo que importa si falla:

  1. el handshake de Meta falla cerrado sin META_VERIFY_TOKEN (en n8n había
     un valor de respaldo escrito en el propio workflow — hallazgo 6)
  2. el HMAC de Meta se calcula con el app_secret del remitente y sobre los
     bytes tal cual llegaron
  3. el widget solo responde a los dominios que el tenant registró, y el
     comodín '*' se rechaza salvo que se encienda a propósito (hallazgo 9)
  4. el token interno se compara en tiempo constante y exige X-Tenant-Id

La base de datos se simula: lo que se prueba es la decisión, no el SELECT.
"""

import hashlib
import hmac
import json
from uuid import uuid4

import pytest

from config import settings
from services.agente import firma

TENANT = uuid4()


# ============================================================
# 1. Handshake de Meta (GET)
# ============================================================
def test_handshake_devuelve_el_challenge(monkeypatch):
    monkeypatch.setattr(settings, "META_VERIFY_TOKEN", "secreto-real")
    challenge = firma.resolver_challenge({
        "hub.mode": "subscribe",
        "hub.verify_token": "secreto-real",
        "hub.challenge": "1234",
    })
    assert challenge == "1234"


def test_handshake_sin_variable_configurada_falla_cerrado(monkeypatch):
    """
    El caso que la auditoría marcó: en n8n, sin la variable, se usaba un
    valor escrito en el código y el handshake seguía funcionando.
    """
    monkeypatch.setattr(settings, "META_VERIFY_TOKEN", "")
    with pytest.raises(firma.FirmaInvalida):
        firma.resolver_challenge({
            "hub.mode": "subscribe",
            "hub.verify_token": "operativai_2024_secreto",
            "hub.challenge": "1234",
        })


def test_handshake_con_token_equivocado_se_rechaza(monkeypatch):
    monkeypatch.setattr(settings, "META_VERIFY_TOKEN", "secreto-real")
    with pytest.raises(firma.FirmaInvalida):
        firma.resolver_challenge({
            "hub.mode": "subscribe",
            "hub.verify_token": "otro",
            "hub.challenge": "1234",
        })


def test_handshake_sin_challenge_se_rechaza(monkeypatch):
    monkeypatch.setattr(settings, "META_VERIFY_TOKEN", "secreto-real")
    with pytest.raises(firma.FirmaInvalida):
        firma.resolver_challenge({"hub.mode": "subscribe", "hub.verify_token": "secreto-real"})


# ============================================================
# 2. Token interno (proxy de NeuroAPI)
# ============================================================
def test_token_interno_valido_devuelve_el_tenant(monkeypatch):
    monkeypatch.setattr(settings, "N8N_INTERNAL_TOKEN", "tok")
    assert firma.verificar_token_interno(
        {"x-internal-token": "tok", "x-tenant-id": str(TENANT)}
    ) == TENANT


def test_token_interno_sin_configurar_falla_cerrado(monkeypatch):
    monkeypatch.setattr(settings, "N8N_INTERNAL_TOKEN", "")
    with pytest.raises(firma.FirmaInvalida):
        firma.verificar_token_interno({"x-internal-token": "", "x-tenant-id": str(TENANT)})


def test_token_interno_sin_tenant_se_rechaza(monkeypatch):
    monkeypatch.setattr(settings, "N8N_INTERNAL_TOKEN", "tok")
    with pytest.raises(firma.FirmaInvalida):
        firma.verificar_token_interno({"x-internal-token": "tok"})


def test_token_interno_con_tenant_que_no_es_uuid_se_rechaza(monkeypatch):
    monkeypatch.setattr(settings, "N8N_INTERNAL_TOKEN", "tok")
    with pytest.raises(firma.FirmaInvalida):
        firma.verificar_token_interno({"x-internal-token": "tok", "x-tenant-id": "no-soy-uuid"})


# ============================================================
# 3. HMAC de Meta
# ============================================================
SECRETO_APP = "app-secret-del-tenant"


def cuerpo_whatsapp() -> bytes:
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "PNID-1"}}}]}],
    }).encode()


def firmar(cuerpo: bytes, secreto: str = SECRETO_APP) -> str:
    return "sha256=" + hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()


async def test_firma_meta_valida(monkeypatch):
    monkeypatch.setattr(firma, "fetch_value", _secreto_fijo(SECRETO_APP))
    cuerpo = cuerpo_whatsapp()
    resultado = await firma.verificar_firma_meta(cuerpo, {"x-hub-signature-256": firmar(cuerpo)})
    assert resultado["object"] == "whatsapp_business_account"


async def test_firma_meta_de_otro_secreto_se_rechaza(monkeypatch):
    monkeypatch.setattr(firma, "fetch_value", _secreto_fijo(SECRETO_APP))
    cuerpo = cuerpo_whatsapp()
    with pytest.raises(firma.FirmaInvalida):
        await firma.verificar_firma_meta(
            cuerpo, {"x-hub-signature-256": firmar(cuerpo, "secreto-ajeno")}
        )


async def test_firma_meta_sin_cabecera_se_rechaza(monkeypatch):
    monkeypatch.setattr(firma, "fetch_value", _secreto_fijo(SECRETO_APP))
    with pytest.raises(firma.FirmaInvalida):
        await firma.verificar_firma_meta(cuerpo_whatsapp(), {})


async def test_firma_meta_de_un_remitente_sin_secreto_se_rechaza(monkeypatch):
    """Un número que ya no es nuestro: no hay app_secret con el que validar."""
    monkeypatch.setattr(firma, "fetch_value", _secreto_fijo(None))
    cuerpo = cuerpo_whatsapp()
    with pytest.raises(firma.FirmaInvalida):
        await firma.verificar_firma_meta(cuerpo, {"x-hub-signature-256": firmar(cuerpo)})


async def test_firma_meta_sobre_los_bytes_originales(monkeypatch):
    """
    El HMAC va sobre los bytes tal cual llegaron: reserializar el JSON
    cambia espacios y orden de claves, y la firma deja de coincidir.
    """
    monkeypatch.setattr(firma, "fetch_value", _secreto_fijo(SECRETO_APP))
    cuerpo = cuerpo_whatsapp()
    reserializado = json.dumps(json.loads(cuerpo), indent=2).encode()
    with pytest.raises(firma.FirmaInvalida):
        await firma.verificar_firma_meta(reserializado, {"x-hub-signature-256": firmar(cuerpo)})


def _secreto_fijo(valor):
    async def fake_fetch_value(*_args):
        return valor
    return fake_fetch_value


def test_identifica_remitente_por_canal():
    wa = {"object": "whatsapp_business_account",
          "entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "P1"}}}]}]}
    assert firma.identificar_remitente(wa) == ("whatsapp", "P1")
    assert firma.identificar_remitente({"object": "instagram", "entry": [{"id": "IG"}]}) == (
        "instagram", "IG",
    )
    assert firma.identificar_remitente({"object": "page", "entry": [{"id": "FB"}]}) == (
        "facebook", "FB",
    )
    assert firma.identificar_remitente({"object": "threads", "entry": [{"id": "X"}]}) == (
        None, None,
    )


# ============================================================
# 4. Widget web
# ============================================================
def _canal_widget(dominio: str | None):
    async def fake_fetch_one(*_args):
        if dominio is None:
            return None
        return {"tenant_id": TENANT, "dominio_permitido": dominio}
    return fake_fetch_one


CUERPO_WIDGET = {"object": "web_widget", "widget_key": "wk-1", "contenido": "hola"}


async def test_widget_origen_registrado(monkeypatch):
    monkeypatch.setattr(firma, "fetch_one", _canal_widget("https://cliente.com"))
    r = await firma.verificar_widget(CUERPO_WIDGET, {"origin": "https://cliente.com"})
    assert r.tenant_id == TENANT
    assert r.origen_permitido == "https://cliente.com"


async def test_widget_normaliza_el_dominio_guardado_sin_esquema(monkeypatch):
    """
    "cliente.com" y "https://cliente.com" son el mismo origen. Sin
    normalizar, el tenant recibía 403 teniendo la clave correcta.
    """
    monkeypatch.setattr(firma, "fetch_one", _canal_widget("cliente.com"))
    r = await firma.verificar_widget(CUERPO_WIDGET, {"origin": "https://cliente.com"})
    assert r.tenant_id == TENANT


async def test_widget_acepta_lista_de_dominios(monkeypatch):
    monkeypatch.setattr(firma, "fetch_one", _canal_widget("https://a.com, https://b.com"))
    r = await firma.verificar_widget(CUERPO_WIDGET, {"origin": "https://b.com"})
    assert r.origen_permitido == "https://b.com"


async def test_widget_origen_ajeno_se_rechaza(monkeypatch):
    monkeypatch.setattr(firma, "fetch_one", _canal_widget("https://cliente.com"))
    with pytest.raises(firma.FirmaInvalida):
        await firma.verificar_widget(CUERPO_WIDGET, {"origin": "https://atacante.com"})


async def test_widget_sin_origen_se_rechaza(monkeypatch):
    monkeypatch.setattr(firma, "fetch_one", _canal_widget("https://cliente.com"))
    with pytest.raises(firma.FirmaInvalida):
        await firma.verificar_widget(CUERPO_WIDGET, {})


async def test_widget_key_desconocida_se_rechaza(monkeypatch):
    monkeypatch.setattr(firma, "fetch_one", _canal_widget(None))
    with pytest.raises(firma.FirmaInvalida):
        await firma.verificar_widget(CUERPO_WIDGET, {"origin": "https://cliente.com"})


async def test_widget_sin_widget_key_se_rechaza(monkeypatch):
    monkeypatch.setattr(firma, "fetch_one", _canal_widget("https://cliente.com"))
    with pytest.raises(firma.FirmaInvalida):
        await firma.verificar_widget({"object": "web_widget"}, {"origin": "https://cliente.com"})


async def test_widget_comodin_se_rechaza_por_defecto(monkeypatch):
    """
    Hallazgo 9: `dominio_permitido = '*'` venía de una prueba y dejaba el
    widget abierto a cualquier sitio. Apagado por defecto.
    """
    monkeypatch.setattr(settings, "WIDGET_COMODIN_PERMITIDO", False)
    monkeypatch.setattr(firma, "fetch_one", _canal_widget("*"))
    with pytest.raises(firma.FirmaInvalida):
        await firma.verificar_widget(CUERPO_WIDGET, {"origin": "https://cualquiera.com"})


async def test_widget_comodin_se_acepta_si_se_enciende(monkeypatch):
    monkeypatch.setattr(settings, "WIDGET_COMODIN_PERMITIDO", True)
    monkeypatch.setattr(firma, "fetch_one", _canal_widget("*"))
    r = await firma.verificar_widget(CUERPO_WIDGET, {"origin": "https://cualquiera.com"})
    assert r.tenant_id == TENANT
    assert r.origen_permitido == "https://cualquiera.com"


async def test_widget_sin_dominios_registrados_se_rechaza(monkeypatch):
    monkeypatch.setattr(firma, "fetch_one", _canal_widget(""))
    with pytest.raises(firma.FirmaInvalida):
        await firma.verificar_widget(CUERPO_WIDGET, {"origin": "https://cliente.com"})
