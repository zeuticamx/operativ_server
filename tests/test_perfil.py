"""
Perfil personal (routers/perfil.py): datos, completitud y foto.
"""

from datetime import date, timedelta
from uuid import uuid4

import pytest

from security import crear_access_token, hash_password
from session import execute, fetch_one, fetch_value
from tests.imagenes_prueba import GIF, jpeg, png

COMPLETO = {
    "nombres": "María José",
    "apellido_paterno": "Pérez",
    "apellido_materno": None,
    "fecha_nacimiento": "1990-05-17",
    "genero": "femenino",
    "empresa": None,
}


def _hace_anios(anios: int, dias: int = 0) -> str:
    hoy = date.today()
    return (hoy.replace(year=hoy.year - anios) + timedelta(days=dias)).isoformat()


@pytest.fixture
def h(tenant_y_usuario):
    return {"Authorization": f"Bearer {tenant_y_usuario['token']}"}


async def _put(http_client, h, **cambios):
    return await http_client.put("/api/perfil", json={**COMPLETO, **cambios}, headers=h)


# ============================================================
# Datos
# ============================================================
@pytest.mark.asyncio
async def test_una_cuenta_nueva_arranca_sin_perfil_y_sin_bloquear_nada(http_client, h):
    r = await http_client.get("/api/perfil", headers=h)

    assert r.status_code == 200
    perfil = r.json()
    assert perfil["completo"] is False
    assert perfil["faltantes"] == ["nombres", "apellido_paterno", "fecha_nacimiento", "genero"]
    assert perfil["tiene_foto"] is False
    # La empresa ya se pidió al crear la cuenta (nombre del negocio): viene
    # puesta, no hay que volver a escribirla.
    assert perfil["empresa"] == "Test Tenant"
    # Cuenta recién creada: le quedan los 20 días de recordatorios.
    assert perfil["recordatorio_dias_restantes"] == 20

    yo = (await http_client.get("/api/auth/yo", headers=h)).json()
    assert yo["perfil_completo"] is False


@pytest.mark.asyncio
async def test_con_los_obligatorios_queda_completo_y_los_opcionales_pueden_ir_vacios(
    http_client, h, tenant_y_usuario
):
    r = await _put(http_client, h)

    assert r.status_code == 200
    perfil = r.json()
    assert perfil["completo"] is True
    assert perfil["faltantes"] == []
    assert perfil["apellido_materno"] is None
    # Sin empresa propia, se ve la del negocio.
    assert perfil["empresa"] == "Test Tenant"
    assert perfil["recordatorio_dias_restantes"] is None

    fila = await fetch_one(
        "SELECT perfil_completado_en FROM portal_users WHERE id = $1", tenant_y_usuario["usuario_id"]
    )
    assert fila["perfil_completado_en"] is not None

    yo = (await http_client.get("/api/auth/yo", headers=h)).json()
    assert (yo["perfil_completo"], yo["nombres"], yo["apellido_paterno"]) == (True, "María José", "Pérez")


@pytest.mark.asyncio
async def test_se_puede_guardar_a_medias(http_client, h):
    r = await http_client.put("/api/perfil", json={"nombres": "Ana", "empresa": "Ferretería"}, headers=h)

    assert r.status_code == 200
    assert r.json()["completo"] is False
    assert r.json()["faltantes"] == ["apellido_paterno", "fecha_nacimiento", "genero"]
    assert r.json()["empresa"] == "Ferretería"


@pytest.mark.asyncio
async def test_vaciar_un_obligatorio_lo_vuelve_incompleto_y_la_fecha_de_completado_se_conserva(
    http_client, h, tenant_y_usuario
):
    await _put(http_client, h)
    primera = await fetch_value(
        "SELECT perfil_completado_en FROM portal_users WHERE id = $1", tenant_y_usuario["usuario_id"]
    )

    # Cambiar un opcional no toca la fecha de completado.
    await _put(http_client, h, empresa="Otra")
    assert await fetch_value(
        "SELECT perfil_completado_en FROM portal_users WHERE id = $1", tenant_y_usuario["usuario_id"]
    ) == primera

    r = await _put(http_client, h, genero=None)
    assert r.json()["completo"] is False
    assert await fetch_value(
        "SELECT perfil_completado_en FROM portal_users WHERE id = $1", tenant_y_usuario["usuario_id"]
    ) is None


@pytest.mark.asyncio
async def test_limpia_espacios_y_convierte_vacios_en_null(http_client, h):
    r = await _put(http_client, h, nombres="  Juan   Carlos ", apellido_materno="   ", empresa="  ACME  ")

    perfil = r.json()
    assert perfil["nombres"] == "Juan Carlos"
    assert perfil["apellido_materno"] is None
    assert perfil["empresa"] == "ACME"


@pytest.mark.asyncio
async def test_una_empresa_propia_manda_sobre_la_del_negocio(http_client, h, tenant_y_usuario):
    r = await _put(http_client, h, empresa="Grupo ACME")
    assert r.json()["empresa"] == "Grupo ACME"
    assert await fetch_value(
        "SELECT empresa FROM portal_users WHERE id = $1", tenant_y_usuario["usuario_id"]
    ) == "Grupo ACME"

    # Borrarla no deja el campo vacío: vuelve a verse el nombre del negocio.
    r = await _put(http_client, h, empresa=None)
    assert r.json()["empresa"] == "Test Tenant"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "campo, valor",
    [
        ("nombres", "Ana2"),
        ("nombres", "<script>"),
        ("apellido_paterno", "Pérez_"),
        ("apellido_materno", "@@"),
        ("nombres", "a" * 101),
        ("genero", "otro"),
        ("genero", "Femenino"),
        ("empresa", "x" * 256),
        ("fecha_nacimiento", "no-es-fecha"),
    ],
)
async def test_valores_invalidos_son_422(http_client, h, campo, valor):
    r = await _put(http_client, h, **{campo: valor})
    assert r.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("nombre", ["O'Connor", "Pérez-Ruiz", "Ma. Guadalupe", "Núñez", "Müller"])
async def test_nombres_reales_con_acentos_y_signos_se_aceptan(http_client, h, nombre):
    r = await _put(http_client, h, apellido_paterno=nombre)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_hay_que_tener_18_anios(http_client, h):
    assert (await _put(http_client, h, fecha_nacimiento=_hace_anios(18))).status_code == 200
    # Cumple 18 mañana: todavía no.
    assert (await _put(http_client, h, fecha_nacimiento=_hace_anios(18, dias=1))).status_code == 422
    assert (await _put(http_client, h, fecha_nacimiento=_hace_anios(10))).status_code == 422
    manana = (date.today() + timedelta(days=1)).isoformat()
    assert (await _put(http_client, h, fecha_nacimiento=manana)).status_code == 422
    assert (await _put(http_client, h, fecha_nacimiento=_hace_anios(121))).status_code == 422


@pytest.mark.asyncio
async def test_completar_el_perfil_marca_leido_el_recordatorio_de_la_campana(
    http_client, h, tenant_y_usuario
):
    await execute(
        """
        INSERT INTO alertas (tenant_id, tipo, titulo, mensaje, portal_user_id)
        VALUES ($1, 'perfil_incompleto', 'Completa tu perfil', 'Faltan datos', $2)
        """,
        tenant_y_usuario["tenant_id"],
        tenant_y_usuario["usuario_id"],
    )

    await _put(http_client, h)

    assert await fetch_value(
        "SELECT leido FROM alertas WHERE portal_user_id = $1", tenant_y_usuario["usuario_id"]
    ) is True


@pytest.mark.asyncio
async def test_sin_token_no_hay_perfil(http_client):
    assert (await http_client.get("/api/perfil")).status_code == 401
    assert (await http_client.put("/api/perfil", json=COMPLETO)).status_code == 401


# ============================================================
# Foto
# ============================================================
async def _subir(http_client, h, nombre, contenido, tipo="application/octet-stream"):
    return await http_client.put(
        "/api/perfil/foto", files={"archivo": (nombre, contenido, tipo)}, headers=h
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "nombre, contenido, mime",
    [("yo.png", png(642, 642), "image/png"), ("yo.jpg", jpeg(320, 480), "image/jpeg")],
)
async def test_sube_y_sirve_la_foto(http_client, h, tenant_y_usuario, nombre, contenido, mime):
    r = await _subir(http_client, h, nombre, contenido)

    assert r.status_code == 200
    assert r.json()["tiene_foto"] is True
    assert r.json()["foto_version"]

    foto = await http_client.get("/api/perfil/foto", headers=h)
    assert foto.status_code == 200
    assert foto.headers["content-type"] == mime
    assert foto.content == contenido
    assert "private" in foto.headers["cache-control"]

    fila = await fetch_one(
        "SELECT mime, ancho, alto, bytes FROM portal_user_fotos WHERE portal_user_id = $1",
        tenant_y_usuario["usuario_id"],
    )
    assert fila["mime"] == mime
    assert fila["bytes"] == len(contenido)


@pytest.mark.asyncio
async def test_reemplazar_la_foto_cambia_la_version(http_client, h):
    v1 = (await _subir(http_client, h, "a.png", png(10, 10))).json()["foto_version"]
    v2 = (await _subir(http_client, h, "b.png", png(20, 20))).json()["foto_version"]
    assert v1 != v2


@pytest.mark.asyncio
async def test_la_foto_es_opcional_para_el_perfil_completo(http_client, h):
    assert (await _put(http_client, h)).json()["completo"] is True
    assert (await http_client.get("/api/perfil/foto", headers=h)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "nombre, contenido, esperado",
    [
        ("yo.png", png(643, 100), 422),
        ("yo.jpg", jpeg(100, 900), 422),
        ("yo.gif", GIF, 415),
        ("yo.png", GIF, 415),  # renombrado
        ("yo.png", jpeg(10, 10), 415),  # la extensión no coincide
        ("yo.png", b"", 422),
    ],
)
async def test_fotos_invalidas_se_rechazan_y_no_se_guarda_nada(
    http_client, h, tenant_y_usuario, nombre, contenido, esperado
):
    r = await _subir(http_client, h, nombre, contenido)

    assert r.status_code == esperado
    assert await fetch_value(
        "SELECT COUNT(*) FROM portal_user_fotos WHERE portal_user_id = $1",
        tenant_y_usuario["usuario_id"],
    ) == 0


@pytest.mark.asyncio
async def test_una_foto_de_mas_de_2_mb_es_413(http_client, h, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "PERFIL_FOTO_MAX_BYTES", 1000)
    grande = png(10, 10) + b"\x00" * 1000

    r = await _subir(http_client, h, "yo.png", grande)
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_borrar_la_foto(http_client, h):
    await _subir(http_client, h, "yo.png", png(10, 10))

    r = await http_client.delete("/api/perfil/foto", headers=h)

    assert r.status_code == 204
    assert (await http_client.get("/api/perfil/foto", headers=h)).status_code == 404
    assert (await http_client.get("/api/perfil", headers=h)).json()["tiene_foto"] is False


@pytest.mark.asyncio
async def test_cada_quien_ve_solo_su_foto(http_client, h, tenant_y_usuario):
    """Otro usuario del mismo negocio no recibe mi foto: la ruta es siempre la propia."""
    await _subir(http_client, h, "yo.png", png(10, 10))

    otro_id = uuid4()
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, 'member', true)
        """,
        otro_id,
        tenant_y_usuario["tenant_id"],
        f"otro-{otro_id}@ejemplo.com",
        hash_password("x" * 12),
    )
    h_otro = {
        "Authorization": f"Bearer {crear_access_token(otro_id, tenant_y_usuario['tenant_id'], 'member')}"
    }

    assert (await http_client.get("/api/perfil/foto", headers=h_otro)).status_code == 404
