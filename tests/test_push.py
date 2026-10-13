"""
Push a la app móvil (services/push.py), registro de dispositivos
(routers/dispositivos.py), alerta 'lead_asignado' y GET /api/alertas/mias.

Los tests de la primera sección no tocan la base. Los que necesitan
sql/46_push_lead_asignado.sql se saltan solos si la migración no está
aplicada en la base de pruebas.
"""

from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from routers.alertas import crear_alerta
from schemas import DispositivoPushIn
from security import crear_access_token, hash_password
from services import push
from session import execute, fetch_all, fetch_value

TOKEN = "ExponentPushToken[abc123xyz]"


# ============================================================
# Unitarios: sin base de datos
# ============================================================


def test_armar_mensajes_lleva_alerta_y_canal():
    alerta_id = uuid4()
    mensajes = push.armar_mensajes(
        [TOKEN], alerta_id, "lead_asignado", "Título", "Cuerpo", {"cliente_id": "u1"}
    )
    assert mensajes == [
        {
            "to": TOKEN,
            "title": "Título",
            "body": "Cuerpo",
            "data": {
                "alerta_id": str(alerta_id),
                "tipo": "lead_asignado",
                "datos": {"cliente_id": "u1"},
            },
            "sound": "default",
            "priority": "high",
            "channelId": push.CANAL_ANDROID,
        }
    ]


def test_armar_mensajes_descarta_datos_que_no_caben():
    mensajes = push.armar_mensajes([TOKEN], uuid4(), "cierre", "t", "m", {"x": "a" * 5000})
    assert "datos" not in mensajes[0]["data"]


def test_tokens_invalidos_solo_device_not_registered():
    lote = [{"to": "A"}, {"to": "B"}, {"to": "C"}]
    tickets = [
        {"status": "ok", "id": "1"},
        {"status": "error", "details": {"error": "DeviceNotRegistered"}},
        {"status": "error", "details": {"error": "MessageRateExceeded"}},
    ]
    assert push.tokens_invalidos(lote, tickets) == ["B"]


async def test_enviar_borra_tokens_invalidos_y_cuenta_aceptados(monkeypatch):
    enviados: list = []

    def responder(request: httpx.Request) -> httpx.Response:
        enviados.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"status": "ok", "id": "1"},
                    {"status": "error", "details": {"error": "DeviceNotRegistered"}},
                ]
            },
        )

    cliente_real = httpx.AsyncClient
    monkeypatch.setattr(
        push.httpx,
        "AsyncClient",
        lambda **kw: cliente_real(transport=httpx.MockTransport(responder), **kw),
    )
    borrados: list = []

    async def falso_execute(sql, *args):
        borrados.append(args)

    monkeypatch.setattr(push, "execute", falso_execute)

    aceptados = await push.enviar([{"to": "A"}, {"to": "B"}])

    assert aceptados == 1
    assert len(enviados) == 1
    assert str(enviados[0].url) == push.EXPO_PUSH_URL
    assert borrados == [(["B"],)]


async def test_enviar_parte_en_lotes_de_100(monkeypatch):
    tamanos: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        import json

        lote = json.loads(request.content)
        tamanos.append(len(lote))
        return httpx.Response(200, json={"data": [{"status": "ok"} for _ in lote]})

    cliente_real = httpx.AsyncClient
    monkeypatch.setattr(
        push.httpx,
        "AsyncClient",
        lambda **kw: cliente_real(transport=httpx.MockTransport(responder), **kw),
    )

    aceptados = await push.enviar([{"to": f"T{i}"} for i in range(250)])

    assert tamanos == [100, 100, 50]
    assert aceptados == 250


async def test_enviar_no_levanta_si_expo_falla(monkeypatch):
    cliente_real = httpx.AsyncClient
    monkeypatch.setattr(
        push.httpx,
        "AsyncClient",
        lambda **kw: cliente_real(
            transport=httpx.MockTransport(lambda r: httpx.Response(503)), **kw
        ),
    )
    assert await push.enviar([{"to": "A"}]) == 0


def test_programar_push_respeta_push_activo(monkeypatch):
    monkeypatch.setattr(push.settings, "PUSH_ACTIVO", False)
    # Sin loop corriendo: si intentara crear la tarea, esto levantaría.
    push.programar_push_alerta(uuid4(), None, uuid4(), "cierre", "t", "m")
    assert not push._tareas


@pytest.mark.parametrize(
    "token",
    ["ExponentPushToken[abc123xyz]", "ExpoPushToken[abc123xyz]", "  ExponentPushToken[abc123xyz] "],
)
def test_dispositivo_acepta_tokens_expo(token):
    assert DispositivoPushIn(token=token, plataforma="ios").token == token.strip()


@pytest.mark.parametrize("token", ["fcm-token-cualquiera-123", "ExponentPushToken[abc123xyz"])
def test_dispositivo_rechaza_tokens_ajenos(token):
    with pytest.raises(ValidationError):
        DispositivoPushIn(token=token, plataforma="android")


# ============================================================
# Con base de datos
# ============================================================


@pytest.fixture
async def migracion_46(db):
    """Salta el test si sql/46_push_lead_asignado.sql no está aplicada."""
    existe = await fetch_value("SELECT to_regclass('public.dispositivos_push') IS NOT NULL")
    if not existe:
        pytest.skip("Falta aplicar sql/46_push_lead_asignado.sql en la base de pruebas")


@pytest.fixture
def sin_push(monkeypatch):
    """Que broadcast_alerta no programe envíos reales a Expo durante el test."""
    monkeypatch.setattr(push.settings, "PUSH_ACTIVO", False)


async def _usuario(tenant_id, role: str) -> dict:
    uid = uuid4()
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, $5, true)
        """,
        uid,
        tenant_id,
        f"test-{uid}@ejemplo.com",
        hash_password("x"),
        role,
    )
    return {"id": uid, "token": crear_access_token(uid, tenant_id, role)}


async def test_mis_alertas_owner_ve_negocio_y_suyas(
    http_client, tenant_y_usuario, clean_alertas, sin_push
):
    tid = tenant_y_usuario["tenant_id"]
    otro = await _usuario(tid, "member")
    await crear_alerta(tid, "nuevo_lead", "Del negocio", "m")
    await crear_alerta(tid, "perfil_incompleto", "Mía", "m", portal_user_id=tenant_y_usuario["usuario_id"])
    await crear_alerta(tid, "perfil_incompleto", "Del otro", "m", portal_user_id=otro["id"])

    r = await http_client.get(
        "/api/alertas/mias",
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )

    assert r.status_code == 200
    assert sorted(a["titulo"] for a in r.json()) == ["Del negocio", "Mía"]


async def test_mis_alertas_vendedor_solo_personales(
    http_client, tenant_y_usuario, clean_alertas, sin_push
):
    tid = tenant_y_usuario["tenant_id"]
    vendedor = await _usuario(tid, "vendedor")
    await crear_alerta(tid, "nuevo_lead", "Del negocio", "m")
    await crear_alerta(tid, "agenda_reprogramada", "Suya", "m", portal_user_id=vendedor["id"])

    r = await http_client.get(
        "/api/alertas/mias?leido=false",
        headers={"Authorization": f"Bearer {vendedor['token']}"},
    )

    assert r.status_code == 200
    assert [a["titulo"] for a in r.json()] == ["Suya"]


async def test_registrar_dispositivo_es_upsert_por_token(
    http_client, tenant_y_usuario, migracion_46
):
    tid = tenant_y_usuario["tenant_id"]
    otro = await _usuario(tid, "vendedor")
    cuerpo = {"token": TOKEN, "plataforma": "android"}

    r1 = await http_client.put(
        "/api/dispositivos/push",
        json=cuerpo,
        headers={"Authorization": f"Bearer {tenant_y_usuario['token']}"},
    )
    # Otra persona inicia sesión en el mismo teléfono: el token cambia de dueño.
    r2 = await http_client.put(
        "/api/dispositivos/push", json=cuerpo, headers={"Authorization": f"Bearer {otro['token']}"}
    )

    assert r1.status_code == r2.status_code == 204
    filas = await fetch_all("SELECT portal_user_id FROM dispositivos_push WHERE token = $1", TOKEN)
    assert [f["portal_user_id"] for f in filas] == [otro["id"]]


async def test_baja_solo_borra_el_token_propio(http_client, tenant_y_usuario, migracion_46):
    tid = tenant_y_usuario["tenant_id"]
    ajeno = await _usuario(tid, "vendedor")
    headers = {"Authorization": f"Bearer {tenant_y_usuario['token']}"}
    await http_client.put(
        "/api/dispositivos/push", json={"token": TOKEN, "plataforma": "ios"}, headers=headers
    )

    r_ajeno = await http_client.delete(
        "/api/dispositivos/push",
        params={"token": TOKEN},
        headers={"Authorization": f"Bearer {ajeno['token']}"},
    )
    assert r_ajeno.status_code == 204
    assert await fetch_value("SELECT COUNT(*) FROM dispositivos_push WHERE token = $1", TOKEN) == 1

    r = await http_client.delete("/api/dispositivos/push", params={"token": TOKEN}, headers=headers)
    assert r.status_code == 204
    assert await fetch_value("SELECT COUNT(*) FROM dispositivos_push WHERE token = $1", TOKEN) == 0


async def test_tokens_destino_sigue_las_rooms(tenant_y_usuario, migracion_46):
    """Alerta del negocio -> owner/member; vendedor solo con alerta personal."""
    tid = tenant_y_usuario["tenant_id"]
    vendedor = await _usuario(tid, "vendedor")
    for uid, token in [
        (tenant_y_usuario["usuario_id"], "ExponentPushToken[owner]"),
        (vendedor["id"], "ExponentPushToken[vendedor]"),
    ]:
        await execute(
            "INSERT INTO dispositivos_push (token, tenant_id, portal_user_id, plataforma) VALUES ($1, $2, $3, 'ios')",
            token,
            tid,
            uid,
        )

    assert await push.tokens_destino(tid, None) == ["ExponentPushToken[owner]"]
    assert await push.tokens_destino(tid, vendedor["id"]) == ["ExponentPushToken[vendedor]"]


async def test_avisar_leads_asignados_crea_alerta_personal(
    tenant_y_usuario, clean_alertas, migracion_46, sin_push
):
    from services.notificaciones import avisar_leads_asignados

    tid = tenant_y_usuario["tenant_id"]
    cuenta = await _usuario(tid, "vendedor")
    vendedor_id = await fetch_value(
        "INSERT INTO vendedores (tenant_id, portal_user_id, nombre) VALUES ($1, $2, 'Ana') RETURNING id",
        tid,
        cuenta["id"],
    )
    lead = await fetch_value(
        "INSERT INTO users (tenant_id, display_name) VALUES ($1, 'Lucía') RETURNING id", tid
    )

    await avisar_leads_asignados(tid, vendedor_id, [lead])
    # Asignárselo a sí mismo no avisa.
    await avisar_leads_asignados(tid, vendedor_id, [lead], actor_portal_user_id=cuenta["id"])

    alertas = await fetch_all(
        "SELECT tipo, mensaje, portal_user_id, datos FROM alertas WHERE tenant_id = $1", tid
    )
    assert len(alertas) == 1
    assert alertas[0]["tipo"] == "lead_asignado"
    assert alertas[0]["portal_user_id"] == cuenta["id"]
    assert alertas[0]["datos"]["cliente_id"] == str(lead)
    assert "Lucía" in alertas[0]["mensaje"]


async def test_avisar_leads_asignados_agrupa_varios(
    tenant_y_usuario, clean_alertas, migracion_46, sin_push
):
    from services.notificaciones import avisar_leads_asignados

    tid = tenant_y_usuario["tenant_id"]
    cuenta = await _usuario(tid, "vendedor")
    vendedor_id = await fetch_value(
        "INSERT INTO vendedores (tenant_id, portal_user_id, nombre) VALUES ($1, $2, 'Ana') RETURNING id",
        tid,
        cuenta["id"],
    )
    leads = [
        await fetch_value("INSERT INTO users (tenant_id) VALUES ($1) RETURNING id", tid)
        for _ in range(3)
    ]

    await avisar_leads_asignados(tid, vendedor_id, leads)

    alertas = await fetch_all("SELECT mensaje, datos FROM alertas WHERE tenant_id = $1", tid)
    assert len(alertas) == 1
    assert alertas[0]["datos"]["total"] == 3
