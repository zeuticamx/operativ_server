"""
Recordatorios de perfil incompleto (jobs/perfil_background.py) y las
alertas personales que los llevan a la campana.

Regla: mientras el perfil esté incompleto, un recordatorio (correo + alerta
personal) cada 24 h durante los primeros 20 días naturales desde la
creación de la cuenta. Nada después, nada si está completo, nada para
roles que no usan el portal.

La pasada se limita siempre a los usuarios del test (`usuarios=[...]`): la
base de desarrollo tiene cuentas reales que una pasada completa tocaría.
"""

import asyncio
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest

import jobs.perfil_background as perfil_background
import realtime
from jobs.perfil_background import job_recordatorio_perfil
from security import crear_access_token, hash_password
from services.correo import ErrorEnvioCorreo
from session import execute, fetch_all, fetch_one, fetch_value


# ============================================================
# Fixtures y helpers
# ============================================================
@pytest.fixture
def correos(monkeypatch):
    enviados: list[dict] = []

    async def enviar(destino, nombre, faltantes, dias_restantes):
        enviados.append(
            {"destino": destino, "nombre": nombre, "faltantes": faltantes, "dias": dias_restantes}
        )

    monkeypatch.setattr(perfil_background, "enviar_recordatorio_perfil", enviar)
    return enviados


@pytest.fixture
def emitidos(monkeypatch):
    """Lo que salió por WebSocket, con su room."""
    salida: list[dict] = []

    async def emit(evento, datos, room=None):
        salida.append({"evento": evento, "datos": datos, "room": room})

    monkeypatch.setattr(realtime.sio, "emit", emit)
    return salida


@pytest.fixture
async def tenant(db):
    tid = uuid4()
    await execute("INSERT INTO tenants (id, name) VALUES ($1, 'Negocio test')", tid)
    yield tid
    await execute("DELETE FROM tenants WHERE id = $1", tid)


async def _usuario(tenant_id, *, role="owner", creado_hace="0 hours", activo=True, **perfil):
    uid = uuid4()
    await execute(
        f"""
        INSERT INTO portal_users
            (id, tenant_id, email, password_hash, role, is_active, created_at,
             nombres, apellido_paterno, fecha_nacimiento, genero, perfil_completado_en)
        VALUES ($1, $2, $3, $4, $5, $6, LOCALTIMESTAMP - INTERVAL '{creado_hace}',
                $7, $8, $9, $10, $11)
        """,
        uid,
        tenant_id,
        f"perfil-{uid}@ejemplo.com",
        hash_password("x" * 12),
        role,
        activo,
        perfil.get("nombres"),
        perfil.get("apellido_paterno"),
        perfil.get("fecha_nacimiento"),
        perfil.get("genero"),
        perfil.get("perfil_completado_en"),
    )
    return uid


async def _pasar_horas(uid, horas: int) -> None:
    """Corre el reloj del usuario hacia atrás: como si hubieran pasado `horas`."""
    await execute(
        f"""
        UPDATE portal_users
           SET created_at = created_at - INTERVAL '{horas} hours',
               ultimo_recordatorio_perfil_en = ultimo_recordatorio_perfil_en - INTERVAL '{horas} hours'
         WHERE id = $1
        """,
        uid,
    )


async def _enviados(uid) -> int:
    return await fetch_value(
        "SELECT recordatorios_perfil_enviados FROM portal_users WHERE id = $1", uid
    )


# ============================================================
# Cuándo toca
# ============================================================
@pytest.mark.asyncio
async def test_una_cuenta_nueva_incompleta_recibe_correo_y_alerta_personal(
    tenant, correos, emitidos
):
    uid = await _usuario(tenant, nombres="Ana")

    assert await job_recordatorio_perfil([uid]) == 1

    assert len(correos) == 1
    assert correos[0]["nombre"] == "Ana"
    assert correos[0]["faltantes"] == ["apellido_paterno", "fecha_nacimiento", "genero"]
    assert correos[0]["dias"] == 20

    alerta = await fetch_one(
        "SELECT tipo, portal_user_id, leido, datos FROM alertas WHERE portal_user_id = $1", uid
    )
    assert alerta["tipo"] == "perfil_incompleto"
    assert alerta["leido"] is False
    assert alerta["datos"]["dias_restantes"] == 20

    # Por WebSocket solo a la room del usuario, nunca a la del negocio.
    nuevas = [e for e in emitidos if e["evento"] == "nueva_alerta"]
    assert [e["room"] for e in nuevas] == [f"usuario_{uid}"]


@pytest.mark.asyncio
async def test_no_repite_antes_de_24_horas(tenant, correos, emitidos):
    uid = await _usuario(tenant)

    await job_recordatorio_perfil([uid])
    assert await job_recordatorio_perfil([uid]) == 0

    await _pasar_horas(uid, 23)
    assert await job_recordatorio_perfil([uid]) == 0
    assert len(correos) == 1


@pytest.mark.asyncio
async def test_a_las_24_horas_manda_el_siguiente_y_la_campana_no_acumula(
    tenant, correos, emitidos
):
    uid = await _usuario(tenant)
    await job_recordatorio_perfil([uid])

    await _pasar_horas(uid, 24)
    assert await job_recordatorio_perfil([uid]) == 1

    assert await _enviados(uid) == 2
    assert len(correos) == 2
    # La de ayer se reemplaza: una sola en la campana.
    assert await fetch_value(
        "SELECT COUNT(*) FROM alertas WHERE portal_user_id = $1", uid
    ) == 1


@pytest.mark.asyncio
async def test_el_margen_evita_que_el_horario_se_corra_una_hora_por_dia(tenant, correos, emitidos):
    """
    El job corre cada hora: la pasada de "mañana a la misma hora" puede caer
    segundos antes de las 24 h exactas. Con 30 min de margen igual califica.
    """
    uid = await _usuario(tenant)
    await job_recordatorio_perfil([uid])
    await execute(
        """
        UPDATE portal_users
           SET ultimo_recordatorio_perfil_en = NOW() - INTERVAL '23 hours 45 minutes'
         WHERE id = $1
        """,
        uid,
    )

    assert await job_recordatorio_perfil([uid]) == 1


@pytest.mark.asyncio
async def test_veinte_dias_son_veinte_recordatorios_y_despues_nada(tenant, correos, emitidos):
    """Simula 25 días de pasadas: uno por día durante los primeros 20, luego se corta."""
    uid = await _usuario(tenant)

    por_dia = []
    for _ in range(25):
        por_dia.append(await job_recordatorio_perfil([uid]))
        await _pasar_horas(uid, 24)

    assert por_dia == [1] * 20 + [0] * 5
    assert await _enviados(uid) == 20
    # El último correo avisa que es el último.
    assert correos[-1]["dias"] == 1


@pytest.mark.asyncio
async def test_una_cuenta_de_mas_de_20_dias_no_recibe_nada(tenant, correos, emitidos):
    """Las cuentas viejas no entran: la ventana cuenta desde la creación."""
    uid = await _usuario(tenant, creado_hace="20 days 1 minute")

    assert await job_recordatorio_perfil([uid]) == 0
    assert correos == []


@pytest.mark.asyncio
async def test_el_ultimo_dia_de_la_ventana_todavia_recibe(tenant, correos, emitidos):
    uid = await _usuario(tenant, creado_hace="19 days 23 hours")

    assert await job_recordatorio_perfil([uid]) == 1
    assert correos[0]["dias"] == 1


@pytest.mark.asyncio
async def test_con_el_perfil_completo_no_hay_recordatorios(tenant, correos, emitidos):
    uid = await _usuario(
        tenant,
        nombres="Ana",
        apellido_paterno="Pérez",
        fecha_nacimiento=date(1990, 1, 1),
        genero="femenino",
        perfil_completado_en=datetime.now(timezone.utc),
    )

    assert await job_recordatorio_perfil([uid]) == 0


@pytest.mark.asyncio
async def test_completar_el_perfil_corta_los_recordatorios(
    http_client, tenant, correos, emitidos
):
    uid = await _usuario(tenant)
    await job_recordatorio_perfil([uid])

    token = crear_access_token(uid, tenant, "owner")
    r = await http_client.put(
        "/api/perfil",
        json={
            "nombres": "Ana",
            "apellido_paterno": "Pérez",
            "fecha_nacimiento": "1990-01-01",
            "genero": "prefiero_no_decirlo",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.json()["completo"] is True

    await _pasar_horas(uid, 24)
    assert await job_recordatorio_perfil([uid]) == 0
    assert len(correos) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role, activo, recibe",
    [
        ("owner", True, True),
        ("superadmin", True, True),
        ("member", True, True),
        ("vendedor", True, False),
        ("owner", False, False),
    ],
)
async def test_solo_usuarios_activos_del_portal(tenant, correos, emitidos, role, activo, recibe):
    uid = await _usuario(tenant, role=role, activo=activo)

    assert await job_recordatorio_perfil([uid]) == (1 if recibe else 0)


@pytest.mark.asyncio
async def test_el_tope_de_20_se_respeta_aunque_siga_en_la_ventana(tenant, correos, emitidos):
    uid = await _usuario(tenant)
    await execute(
        "UPDATE portal_users SET recordatorios_perfil_enviados = 20 WHERE id = $1", uid
    )

    assert await job_recordatorio_perfil([uid]) == 0


@pytest.mark.asyncio
async def test_dos_pasadas_a_la_vez_mandan_uno_solo(tenant, correos, emitidos):
    uid = await _usuario(tenant)

    resultados = await asyncio.gather(
        job_recordatorio_perfil([uid]), job_recordatorio_perfil([uid])
    )

    assert sorted(resultados) == [0, 1]
    assert len(correos) == 1
    assert await _enviados(uid) == 1


@pytest.mark.asyncio
async def test_si_el_correo_falla_el_dia_igual_cuenta_y_la_alerta_se_crea(
    tenant, emitidos, monkeypatch
):
    async def falla(*_a, **_k):
        raise ErrorEnvioCorreo("smtp caído")

    monkeypatch.setattr(perfil_background, "enviar_recordatorio_perfil", falla)
    uid = await _usuario(tenant)

    assert await job_recordatorio_perfil([uid]) == 1
    assert await _enviados(uid) == 1
    assert await fetch_value("SELECT COUNT(*) FROM alertas WHERE portal_user_id = $1", uid) == 1


# ============================================================
# Privacidad de la alerta personal
# ============================================================
@pytest.mark.asyncio
async def test_la_alerta_personal_no_la_ven_ni_la_marcan_los_companeros(
    http_client, tenant, correos, emitidos
):
    duena = await _usuario(tenant, role="member")
    companero = await _usuario(tenant, role="owner")
    await job_recordatorio_perfil([duena])
    alerta_id = await fetch_value("SELECT id FROM alertas WHERE portal_user_id = $1", duena)

    h_companero = {"Authorization": f"Bearer {crear_access_token(companero, tenant, 'owner')}"}
    h_duena = {"Authorization": f"Bearer {crear_access_token(duena, tenant, 'member')}"}

    # El compañero (owner, con acceso al historial del negocio) no la ve.
    lista = (await http_client.get(f"/api/tenants/{tenant}/alertas", headers=h_companero)).json()
    assert str(alerta_id) not in [a["id"] for a in lista]
    stats = (
        await http_client.get(f"/api/tenants/{tenant}/alertas/estadisticas", headers=h_companero)
    ).json()
    assert "perfil_incompleto" not in stats["por_tipo"]

    # Ni la puede marcar leída: 404, como si no existiera.
    r = await http_client.patch(f"/api/alertas/{alerta_id}/marcar-leida", headers=h_companero)
    assert r.status_code == 404

    # Su dueña sí, aunque sea 'member' (que no marca alertas del negocio).
    r = await http_client.patch(f"/api/alertas/{alerta_id}/marcar-leida", headers=h_duena)
    assert r.status_code == 200
    assert await fetch_value("SELECT leido FROM alertas WHERE id = $1", alerta_id) is True


@pytest.mark.asyncio
async def test_un_member_sigue_sin_poder_marcar_alertas_del_negocio(http_client, tenant):
    """El cambio de marcar-leida no le abre a 'member' las alertas del negocio."""
    member = await _usuario(tenant, role="member")
    alerta_id = await fetch_value(
        """
        INSERT INTO alertas (tenant_id, tipo, titulo, mensaje)
        VALUES ($1, 'nuevo_lead', 'Lead', 'Nuevo') RETURNING id
        """,
        tenant,
    )
    h = {"Authorization": f"Bearer {crear_access_token(member, tenant, 'member')}"}

    r = await http_client.patch(f"/api/alertas/{alerta_id}/marcar-leida", headers=h)

    assert r.status_code == 404
    assert await fetch_value("SELECT leido FROM alertas WHERE id = $1", alerta_id) is False


@pytest.mark.asyncio
async def test_el_resumen_diario_a_gerencia_no_incluye_las_personales(tenant, correos, emitidos):
    from jobs.alertas_background import _SELECT_NO_LEIDAS

    uid = await _usuario(tenant)
    await job_recordatorio_perfil([uid])

    filas = await fetch_all(_SELECT_NO_LEIDAS)
    assert tenant not in [f["tenant_id"] for f in filas]
