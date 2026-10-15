"""Tests de la spec 003: limpiar el contexto del agente en una conversación.

Convención de nombres: test_puro_ (sin BD), test_guarda_, test_servicio_,
test_portal_ y test_detalle_ (las tareas siguientes los añaden aquí).
"""
import os
import re
import subprocess
import sys
from pathlib import Path

RAIZ_BACKEND = Path(__file__).resolve().parent.parent
SQL_47 = RAIZ_BACKEND / "sql" / "47_conversacion_contexto_cortes.sql"


def _leer_flag(valor: str | None) -> str:
    """Lee el flag en un subproceso limpio, para que config se importe de cero."""
    entorno = {k: v for k, v in os.environ.items() if k != "CONTEXTO_CONVERSACION_N8N_PUBLICADO"}
    entorno["PYTHONIOENCODING"] = "utf-8"
    if valor is not None:
        entorno["CONTEXTO_CONVERSACION_N8N_PUBLICADO"] = valor
    resultado = subprocess.run(
        [sys.executable, "-c", "import config; print(config.settings.CONTEXTO_CONVERSACION_N8N_PUBLICADO)"],
        cwd=RAIZ_BACKEND,
        env=entorno,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return resultado.stdout.strip()


def test_puro_flag_por_defecto_apagado() -> None:
    assert _leer_flag(None) == "False"
    for valor in ("true", "1", "sí"):
        assert _leer_flag(valor) == "True", valor


def test_puro_migracion_idempotente() -> None:
    sql = SQL_47.read_text(encoding="utf-8")
    plano = re.sub(r"\s+", " ", re.sub(r"--[^\n]*", "", sql))
    assert "CREATE TABLE IF NOT EXISTS conversacion_contexto_cortes" in plano
    assert re.search(
        r"CREATE INDEX IF NOT EXISTS \w+ ON conversacion_contexto_cortes \(conversation_id, creado_en DESC\)",
        plano,
    )
    assert re.search(r"creado_en TIMESTAMPTZ NOT NULL DEFAULT now\(\)", plano, re.IGNORECASE)
    assert re.search(r"tenant_id UUID NOT NULL REFERENCES tenants\(id\) ON DELETE CASCADE", plano)
    assert re.search(r"conversation_id UUID NOT NULL REFERENCES conversations\(id\) ON DELETE CASCADE", plano)
    assert re.search(r"realizado_por UUID REFERENCES portal_users\(id\) ON DELETE SET NULL", plano)
    assert not re.search(r"email|correo", plano, re.IGNORECASE)


# --- T2: guarda owner_sin_impersonar ---------------------------------------
import inspect
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from deps import UsuarioActual, owner_sin_impersonar, usuario_actual


def _usuario(role: str, **extra) -> UsuarioActual:
    return UsuarioActual(id=uuid4(), tenant_id=uuid4(), email="x@ejemplo.test", role=role, **extra)


def _cliente(usuario: UsuarioActual) -> TestClient:
    app = FastAPI()

    @app.post("/probar")
    async def probar(u: UsuarioActual = Depends(owner_sin_impersonar)) -> dict:
        return {"id": str(u.id), "role": u.role}

    app.dependency_overrides[usuario_actual] = lambda: usuario
    return TestClient(app)


def test_guarda_owner_propio_pasa() -> None:
    usuario = _usuario("owner")
    r = _cliente(usuario).post("/probar")
    assert r.status_code == 200
    assert r.json() == {"id": str(usuario.id), "role": "owner"}


@pytest.mark.parametrize("rol", ["member", "vendedor", "proveedor", "superadmin"])
def test_guarda_otros_roles_403(rol: str) -> None:
    assert _cliente(_usuario(rol)).post("/probar").status_code == 403


def test_guarda_owner_impersonado_403() -> None:
    usuario = _usuario(
        "owner",
        impersonado_por="soporte@ejemplo.test",
        impersonado_por_id=uuid4(),
    )
    assert _cliente(usuario).post("/probar").status_code == 403


def test_guarda_sin_parametros_de_ruta() -> None:
    firma = inspect.signature(owner_sin_impersonar)
    assert list(firma.parameters) == ["usuario"]


# --- T3: lógica pura y consultas de la tabla de cortes ---------------------
import itertools

from services.contexto_conversacion import (
    conversacion_de_cliente,
    puede_limpiar_contexto,
    registrar_corte,
    ultimo_corte,
)
from session import execute, fetch_all, fetch_one, fetch_value


def test_puro_puede_limpiar() -> None:
    canales = ["whatsapp", "instagram", "facebook", "web", None, "", "WhatsApp"]
    for propio, flag, plan, canal in itertools.product([True, False], [True, False], [True, False], canales):
        esperado = propio and flag and plan and canal in ("whatsapp", "instagram", "facebook")
        assert puede_limpiar_contexto(propio, flag, canal, plan) is esperado, (propio, flag, plan, canal)


async def _conversacion(tenant_id, canal: str = "whatsapp", con_mensaje: bool = False):
    user_id = await fetch_value(
        "INSERT INTO users (tenant_id, display_name) VALUES ($1, 'Cliente') RETURNING id", tenant_id
    )
    conv_id = await fetch_value(
        "INSERT INTO conversations (tenant_id, user_id, channel_type) VALUES ($1, $2, $3) RETURNING id",
        tenant_id,
        user_id,
        canal,
    )
    if con_mensaje:
        await execute(
            "INSERT INTO messages (tenant_id, conversation_id, role, content) VALUES ($1, $2, 'user', 'hola')",
            tenant_id,
            conv_id,
        )
    return conv_id


async def test_servicio_registrar_y_ultimo(tenant_y_usuario) -> None:
    tid, uid = tenant_y_usuario["tenant_id"], tenant_y_usuario["usuario_id"]
    conv = await _conversacion(tid)  # sin mensajes: admite corte (CL3)
    assert await ultimo_corte(tid, conv) is None

    primero = await registrar_corte(tid, conv, uid)
    assert primero["tenant_id"] == tid and primero["conversation_id"] == conv and primero["realizado_por"] == uid
    segundos = await fetch_value("SELECT abs(extract(epoch FROM now() - $1::timestamptz))", primero["creado_en"])
    assert segundos < 60

    segundo = await registrar_corte(tid, conv, uid)
    filas = await fetch_all("SELECT id FROM conversacion_contexto_cortes WHERE conversation_id = $1", conv)
    assert len(filas) == 2
    assert (await ultimo_corte(tid, conv))["id"] == segundo["id"]
    # otro negocio no ve el corte
    assert await ultimo_corte(uuid4(), conv) is None


async def test_servicio_conversacion_de_cliente(tenant_y_usuario) -> None:
    tid = tenant_y_usuario["tenant_id"]
    for canal in ("whatsapp", "instagram", "facebook"):
        conv = await _conversacion(tid, canal)
        fila = await conversacion_de_cliente(tid, conv)
        assert fila is not None and fila["id"] == conv
    assert await conversacion_de_cliente(tid, await _conversacion(tid, "web")) is None
    assert await conversacion_de_cliente(uuid4(), conv) is None  # otro tenant
    assert await conversacion_de_cliente(tid, uuid4()) is None  # inexistente


async def test_servicio_registrar_no_toca_otras_tablas(tenant_y_usuario) -> None:
    tid, uid = tenant_y_usuario["tenant_id"], tenant_y_usuario["usuario_id"]
    conv = await _conversacion(tid, con_mensaje=True)

    async def foto():
        return (
            [tuple(f) for f in await fetch_all(
                "SELECT id, role, content, created_at FROM messages WHERE conversation_id = $1 ORDER BY id", conv)],
            tuple(await fetch_one(
                "SELECT status, asignado_a, asignado_en FROM conversations WHERE id = $1", conv)),
            await fetch_value("SELECT count(*) FROM alertas WHERE tenant_id = $1", tid),
            await fetch_value("SELECT count(*) FROM conversacion_asignaciones WHERE conversation_id = $1", conv),
        )

    antes = await foto()
    await registrar_corte(tid, conv, uid)
    assert await foto() == antes


# --- T4: endpoint POST /api/conversaciones/{id}/limpiar-contexto -----------
from config import settings
from security import crear_access_token, hash_password

RUTA_LIMPIAR = "/api/conversaciones/{}/limpiar-contexto"
TEXTO_409 = "Limpiar el contexto del agente todavía no está disponible."
ID_MALFORMADO = "no-es-uuid"


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _cuenta(tenant_id, rol: str):
    """Cuenta del portal con ese rol; devuelve (portal_user_id, headers)."""
    uid = uuid4()
    await execute(
        "INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active) "
        "VALUES ($1, $2, $3, $4, $5, true)",
        uid,
        tenant_id,
        f"{rol}-{uid}@ejemplo.test",
        hash_password("test_password"),
        rol,
    )
    return uid, _h(crear_access_token(uid, tenant_id, rol))


async def _contratar(tenant_id, plan: str, estado: str) -> None:
    await execute("DELETE FROM tenant_subscriptions WHERE tenant_id = $1", tenant_id)
    await execute(
        "INSERT INTO tenant_subscriptions (tenant_id, plan, estado, precio_monthly) VALUES ($1, $2, $3, 49)",
        tenant_id,
        plan,
        estado,
    )


async def _suspender(tenant_id) -> None:
    await execute(
        "INSERT INTO tenant_estado_plataforma (tenant_id, estado, motivo) VALUES ($1, 'suspendido', 'test') "
        "ON CONFLICT (tenant_id) DO UPDATE SET estado = EXCLUDED.estado",
        tenant_id,
    )


@pytest.fixture
async def negocio(tenant_y_usuario, plan_enterprise):
    return {
        "tid": tenant_y_usuario["tenant_id"],
        "uid": tenant_y_usuario["usuario_id"],
        "owner": _h(tenant_y_usuario["token"]),
    }


@pytest.fixture
def flag_encendido(monkeypatch):
    monkeypatch.setattr(settings, "CONTEXTO_CONVERSACION_N8N_PUBLICADO", True)


@pytest.fixture
def flag_apagado(monkeypatch):
    monkeypatch.setattr(settings, "CONTEXTO_CONVERSACION_N8N_PUBLICADO", False)


async def _limpiar(http_client, conv, headers):
    return await http_client.post(RUTA_LIMPIAR.format(conv), headers=headers)


async def _cortes(conv) -> int:
    return await fetch_value("SELECT count(*) FROM conversacion_contexto_cortes WHERE conversation_id = $1", conv)


async def _cuenta_vendedor(tid):
    uid, headers = await _cuenta(tid, "vendedor")
    await execute(
        "INSERT INTO vendedores (tenant_id, portal_user_id, nombre, activo) VALUES ($1, $2, 'Ana', true)", tid, uid
    )
    return uid, headers


async def _cuenta_proveedor(tid):
    uid, headers = await _cuenta(tid, "proveedor")
    await execute("INSERT INTO proveedores (tenant_id, nombre, portal_user_id) VALUES ($1, 'P', $2)", tid, uid)
    return uid, headers


@pytest.mark.parametrize("rol", ["member", "vendedor", "proveedor", "superadmin"])
@pytest.mark.parametrize("flag_on", [True, False])
async def test_portal_otros_roles_403_con_id_valido_y_malformado(http_client, negocio, monkeypatch, rol, flag_on):
    monkeypatch.setattr(settings, "CONTEXTO_CONVERSACION_N8N_PUBLICADO", flag_on)
    tid = negocio["tid"]
    if rol == "vendedor":
        uid, headers = await _cuenta_vendedor(tid)
    elif rol == "proveedor":
        uid, headers = await _cuenta_proveedor(tid)
    else:
        uid, headers = await _cuenta(tid, rol)
    conv = await _conversacion(tid)
    await execute("UPDATE conversations SET asignado_a = $2 WHERE id = $1", conv, uid)  # suya, si aplica

    for ident in (conv, uuid4(), ID_MALFORMADO):
        assert (await _limpiar(http_client, ident, headers)).status_code == 403, ident
    assert await _cortes(conv) == 0


@pytest.mark.parametrize("rol", ["member", "superadmin"])
async def test_portal_member_y_superadmin_con_plan_pausada_dan_403_no_402(http_client, negocio, flag_encendido, rol):
    tid = negocio["tid"]
    await _contratar(tid, "enterprise", "pausada")
    _, headers = await _cuenta(tid, rol)
    conv = await _conversacion(tid)

    assert (await _limpiar(http_client, conv, headers)).status_code == 403


@pytest.mark.parametrize("flag_on", [True, False])
async def test_portal_otro_negocio_y_canal_web_dan_404(http_client, negocio, monkeypatch, flag_on):
    monkeypatch.setattr(settings, "CONTEXTO_CONVERSACION_N8N_PUBLICADO", flag_on)
    tid = negocio["tid"]
    web = await _conversacion(tid, "web")
    otro = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Otro')", otro)
    try:
        ajena = await _conversacion(otro)
        for conv in (web, ajena, uuid4()):
            r = await _limpiar(http_client, conv, negocio["owner"])
            assert r.status_code == 404, conv
            assert r.json()["detail"] == "Conversación no encontrada"
        assert await _cortes(web) == 0 and await _cortes(ajena) == 0
    finally:
        await execute("DELETE FROM tenants WHERE id = $1", otro)


async def test_portal_flag_apagado_da_409_con_texto_y_no_escribe(http_client, negocio, flag_apagado):
    conv = await _conversacion(negocio["tid"])

    for ident in (conv, ID_MALFORMADO):
        r = await _limpiar(http_client, ident, negocio["owner"])
        assert r.status_code == 409
        assert r.json()["detail"] == TEXTO_409
    assert await _cortes(conv) == 0


@pytest.mark.parametrize("caso", ["pausada", "suspendida"])
async def test_portal_plan_sin_agente_da_402_igual_que_volver_a_ia(http_client, negocio, flag_encendido, caso):
    tid = negocio["tid"]
    if caso == "pausada":
        await _contratar(tid, "enterprise", "pausada")
    else:
        await _suspender(tid)
    conv = await _conversacion(tid)

    referencia = await http_client.post(f"/api/conversaciones/{conv}/volver-a-ia", headers=negocio["owner"])
    assert referencia.status_code == 402
    for ident in (conv, ID_MALFORMADO):
        r = await _limpiar(http_client, ident, negocio["owner"])
        assert r.status_code == 402, ident
        assert r.json()["detail"] == referencia.json()["detail"]
    assert await _cortes(conv) == 0


async def test_portal_id_malformado_con_plan_vigente_da_422(http_client, negocio, flag_encendido):
    assert (await _limpiar(http_client, ID_MALFORMADO, negocio["owner"])).status_code == 422


async def test_portal_conversacion_inexistente_con_plan_pausada_da_404(http_client, negocio, flag_encendido):
    await _contratar(negocio["tid"], "enterprise", "pausada")

    assert (await _limpiar(http_client, uuid4(), negocio["owner"])).status_code == 404


async def test_portal_exito_registra_corte_sin_tocar_nada_mas(http_client, negocio, flag_encendido) -> None:
    tid = negocio["tid"]
    conv = await _conversacion(tid, "whatsapp", con_mensaje=True)
    miembro, _ = await _cuenta(tid, "member")
    await execute("UPDATE conversations SET asignado_a = $2, asignado_en = now() WHERE id = $1", conv, miembro)

    async def foto():
        return (
            [tuple(f) for f in await fetch_all(
                "SELECT id, role, content, created_at FROM messages WHERE conversation_id = $1 ORDER BY id", conv)],
            tuple(await fetch_one("SELECT status, asignado_a, asignado_en FROM conversations WHERE id = $1", conv)),
            await fetch_value("SELECT count(*) FROM alertas WHERE tenant_id = $1", tid),
        )

    antes = await foto()
    r = await _limpiar(http_client, conv, negocio["owner"])

    assert r.status_code == 201
    cuerpo = r.json()
    assert cuerpo["conversacion_id"] == str(conv) and cuerpo["contexto_limpiado_en"]
    fila = await fetch_one(
        "SELECT tenant_id, realizado_por FROM conversacion_contexto_cortes WHERE conversation_id = $1", conv
    )
    assert fila["realizado_por"] == negocio["uid"] and fila["tenant_id"] == tid
    assert await _cortes(conv) == 1
    assert await foto() == antes


async def test_portal_conversacion_transferida_sigue_transferida(http_client, negocio, flag_encendido) -> None:
    conv = await _conversacion(negocio["tid"])
    await execute("UPDATE conversations SET status = 'transferred' WHERE id = $1", conv)

    assert (await _limpiar(http_client, conv, negocio["owner"])).status_code == 201

    assert await fetch_value("SELECT status FROM conversations WHERE id = $1", conv) == "transferred"
    assert await _cortes(conv) == 1


async def test_portal_sin_token_da_401(http_client, negocio, flag_encendido) -> None:
    conv = await _conversacion(negocio["tid"])

    assert (await http_client.post(RUTA_LIMPIAR.format(conv))).status_code == 401


@pytest.fixture
def correos_falsos(monkeypatch):
    async def falso(destino, gerente, negocio, motivo, enlace, minutos):
        return None

    monkeypatch.setattr("services.acceso_soporte.enviar_solicitud_acceso_soporte", falso)


@pytest.mark.parametrize("con_concesion", [True, False])
async def test_portal_ver_como_da_403_y_audita_solo_con_concesion(
    http_client, negocio, flag_encendido, correos_falsos, con_concesion
) -> None:
    tid = negocio["tid"]
    conv = await _conversacion(tid)
    tid_g = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Interno')", tid_g)
    uid_g, headers_g = await _cuenta(tid_g, "owner")
    email_g = await fetch_value("SELECT email FROM portal_users WHERE id = $1", uid_g)
    await execute("INSERT INTO gerencia_users (email, full_name, cargo) VALUES ($1, 'Test', 'Developer')", email_g)
    try:
        r = await http_client.post(
            f"/api/gerencia/tenants/{tid}/impersonar", json={"motivo": "soporte: probar"}, headers=headers_g
        )
        assert r.status_code == 200
        soporte = _h(r.json()["access_token"])
        if con_concesion:
            sol = await http_client.post(
                "/api/acceso-soporte/solicitar", json={"motivo": "Corregir el horario"}, headers=soporte
            )
            assert sol.status_code == 201
            ap = await http_client.post(
                f"/api/acceso-soporte/{sol.json()['id']}/aprobar", json={"duracion_min": 15}, headers=negocio["owner"]
            )
            assert ap.status_code == 200

        r = await _limpiar(http_client, conv, soporte)

        assert r.status_code == 403
        assert await _cortes(conv) == 0
        filas = await fetch_value(
            "SELECT count(*) FROM gerencia_auditoria WHERE tenant_id = $1 AND accion = 'impersonacion_escritura'",
            tid,
        )
        assert filas == (1 if con_concesion else 0)
    finally:
        await execute("DELETE FROM gerencia_auditoria WHERE tenant_id = $1 OR tenant_id = $2", tid, tid_g)
        await execute("DELETE FROM gerencia_users WHERE LOWER(email) = LOWER($1)", email_g)
        await execute("DELETE FROM tenants WHERE id = $1", tid_g)


def test_portal_router_contexto_sin_dependencias_de_router_y_guarda_primera() -> None:
    from main import app
    from routers import conversaciones as modulo

    assert list(modulo.router_contexto.dependencies) == []
    rutas = [r for r in modulo.router_contexto.routes if getattr(r, "path", "").endswith("/limpiar-contexto")]
    assert len(rutas) == 1 and rutas[0].methods == {"POST"} and rutas[0].status_code == 201
    assert rutas[0].dependant.dependencies[0].call is owner_sin_impersonar
    assert "/api/conversaciones/{conversacion_id}/limpiar-contexto" in app.openapi()["paths"]


# --- T5: campos nuevos en GET /api/conversaciones/{id} ----------------------
async def _detalle(http_client, conv, headers):
    return await http_client.get(f"/api/conversaciones/{conv}", headers=headers)


async def test_detalle_owner_propio_puede_limpiar(http_client, negocio, flag_encendido) -> None:
    conv = await _conversacion(negocio["tid"], "whatsapp")

    r = await _detalle(http_client, conv, negocio["owner"])

    assert r.status_code == 200
    assert r.json()["puede_limpiar_contexto"] is True


async def test_detalle_flag_apagado_no_puede(http_client, negocio, flag_apagado) -> None:
    conv = await _conversacion(negocio["tid"], "whatsapp")

    r = await _detalle(http_client, conv, negocio["owner"])

    assert r.status_code == 200
    assert r.json()["puede_limpiar_contexto"] is False


@pytest.mark.parametrize("rol", ["member", "superadmin"])
async def test_detalle_otros_roles_no_pueden(http_client, negocio, flag_encendido, rol) -> None:
    conv = await _conversacion(negocio["tid"], "whatsapp")
    _, headers = await _cuenta(negocio["tid"], rol)

    r = await _detalle(http_client, conv, headers)

    assert r.status_code == 200
    assert r.json()["puede_limpiar_contexto"] is False


async def test_detalle_owner_impersonado_no_puede(http_client, negocio, flag_encendido, correos_falsos) -> None:
    tid = negocio["tid"]
    conv = await _conversacion(tid, "whatsapp")
    tid_g = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Interno')", tid_g)
    uid_g, headers_g = await _cuenta(tid_g, "owner")
    email_g = await fetch_value("SELECT email FROM portal_users WHERE id = $1", uid_g)
    await execute("INSERT INTO gerencia_users (email, full_name, cargo) VALUES ($1, 'Test', 'Developer')", email_g)
    try:
        r = await http_client.post(
            f"/api/gerencia/tenants/{tid}/impersonar", json={"motivo": "soporte: probar"}, headers=headers_g
        )
        assert r.status_code == 200

        r = await _detalle(http_client, conv, _h(r.json()["access_token"]))

        assert r.status_code == 200
        assert r.json()["puede_limpiar_contexto"] is False
    finally:
        await execute("DELETE FROM gerencia_auditoria WHERE tenant_id = $1 OR tenant_id = $2", tid, tid_g)
        await execute("DELETE FROM gerencia_users WHERE LOWER(email) = LOWER($1)", email_g)
        await execute("DELETE FROM tenants WHERE id = $1", tid_g)


async def test_detalle_plan_pausada_responde_200_sin_poder_limpiar(http_client, negocio, flag_encendido) -> None:
    tid = negocio["tid"]
    await _contratar(tid, "enterprise", "pausada")
    conv = await _conversacion(tid, "whatsapp", con_mensaje=True)

    r = await _detalle(http_client, conv, negocio["owner"])

    assert r.status_code == 200
    assert r.json()["puede_limpiar_contexto"] is False
    assert len(r.json()["mensajes"]) == 1


async def test_detalle_canal_web_no_puede(http_client, negocio, flag_encendido) -> None:
    conv = await _conversacion(negocio["tid"], "web")

    r = await _detalle(http_client, conv, negocio["owner"])

    assert r.status_code == 200
    assert r.json()["puede_limpiar_contexto"] is False


async def test_detalle_sin_cortes_es_null_y_con_dos_cortes_el_mas_reciente(
    http_client, negocio, flag_encendido
) -> None:
    tid, uid = negocio["tid"], negocio["uid"]
    conv = await _conversacion(tid, "whatsapp")
    assert (await _detalle(http_client, conv, negocio["owner"])).json()["contexto_limpiado_en"] is None

    await registrar_corte(tid, conv, uid)
    segundo = await registrar_corte(tid, conv, uid)

    cuerpo = (await _detalle(http_client, conv, negocio["owner"])).json()
    from datetime import datetime

    asignado = datetime.fromisoformat(cuerpo["contexto_limpiado_en"].replace("Z", "+00:00"))
    assert asignado.tzinfo is not None
    assert asignado == segundo["creado_en"]


async def test_detalle_sigue_incluyendo_los_mensajes(http_client, negocio, flag_encendido) -> None:
    tid, uid = negocio["tid"], negocio["uid"]
    conv = await _conversacion(tid, "whatsapp", con_mensaje=True)
    await registrar_corte(tid, conv, uid)

    r = await _detalle(http_client, conv, negocio["owner"])

    assert r.status_code == 200
    mensajes = r.json()["mensajes"]
    assert [m["content"] for m in mensajes] == ["hola"]
