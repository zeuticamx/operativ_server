"""
Reporte de incidencias (routers/incidencias.py, gerencia_incidencias.py,
services/incidencias.py).

El correo no sale: se sustituye `enviar_correo` para capturar lo que se
habría mandado. Las imágenes son PNG/JPEG mínimos armados a mano (solo hace
falta que la firma y el encabezado sean válidos).
"""

import json
import struct
from uuid import uuid4

import pytest

from config import settings
from security import crear_access_token
from services import incidencias as svc
from session import execute, fetch_all, fetch_one

URL = "/api/incidencias"
VALIDO = {"resumen": "No carga el calendario", "descripcion": "Al abrir Calendario se queda en blanco."}


def _png(ancho: int = 40, alto: int = 30) -> bytes:
    """PNG con firma e IHDR válidos (lo que valida services/imagen.py)."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", ancho, alto) + b"\x08\x02\x00\x00\x00"
    )


def _jpeg(ancho: int = 40, alto: int = 30) -> bytes:
    sof = b"\xff\xc0" + struct.pack(">H", 11) + b"\x08" + struct.pack(">HH", alto, ancho) + b"\x01\x01\x11\x00"
    return b"\xff\xd8\xff\xe0" + struct.pack(">H", 2) + sof + b"\xff\xd9"


@pytest.fixture
def correos(monkeypatch):
    """Lista de (destino, asunto, texto, html) que se habrían mandado."""
    enviados: list[tuple[str, str, str, str]] = []

    async def falso(destino: str, asunto: str, texto: str, html: str) -> None:
        enviados.append((destino, asunto, texto, html))

    monkeypatch.setattr(svc, "enviar_correo", falso)
    return enviados


@pytest.fixture
async def gerencia(db, tenant_y_usuario):
    """El usuario de prueba, ascendido a gerencia de plataforma."""
    await execute(
        "INSERT INTO gerencia_users (email, full_name, cargo) VALUES ($1, 'Equipo Test', 'Soporte')",
        tenant_y_usuario["email"],
    )
    yield tenant_y_usuario
    await execute("DELETE FROM gerencia_users WHERE email = $1", tenant_y_usuario["email"])


@pytest.fixture(autouse=True)
async def limpiar(db):
    """Los reportes de prueba no se borran solos si el tenant es NULL."""
    yield
    await execute("DELETE FROM reportes_incidencia WHERE email LIKE 'test-%@ejemplo.com'")


async def _enviar(http_client, headers, datos=None, archivos=None):
    return await http_client.post(URL, data=datos if datos is not None else VALIDO, files=archivos, headers=headers)


async def _fila(reporte_id):
    return await fetch_one("SELECT * FROM reportes_incidencia WHERE id = $1", reporte_id)


# ============================================================
# Envío correcto
# ============================================================
async def test_envio_correcto_guarda_el_reporte(http_client, headers_autenticado, tenant_y_usuario, correos):
    r = await _enviar(http_client, headers_autenticado)

    assert r.status_code == 201
    assert set(r.json()) == {"id"}  # solo el acuse
    fila = await _fila(r.json()["id"])
    assert fila["resumen"] == VALIDO["resumen"]
    assert fila["descripcion"] == VALIDO["descripcion"]
    assert fila["estado"] == "abierto"


async def test_quien_reporta_sale_del_token_no_del_formulario(
    http_client, headers_autenticado, tenant_y_usuario, correos
):
    suplantado = {**VALIDO, "portal_user_id": str(uuid4()), "tenant_id": str(uuid4()), "email": "otro@x.com"}

    r = await _enviar(http_client, headers_autenticado, suplantado)

    fila = await _fila(r.json()["id"])
    assert fila["portal_user_id"] == tenant_y_usuario["usuario_id"]
    assert fila["tenant_id"] == tenant_y_usuario["tenant_id"]
    assert fila["email"] == tenant_y_usuario["email"]


async def test_avisa_por_correo_al_equipo_y_escapa_html(
    http_client, headers_autenticado, gerencia, correos
):
    r = await _enviar(
        http_client,
        headers_autenticado,
        {"resumen": "Falla <script>alert(1)</script>", "descripcion": "Texto <b>negrita</b> y más"},
    )
    assert r.status_code == 201

    destinos = [c[0] for c in correos]
    assert gerencia["email"] in destinos
    _, asunto, texto, html = next(c for c in correos if c[0] == gerencia["email"])
    assert asunto.startswith("Reporte: Falla")
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<b>negrita</b>" not in html


async def test_resumen_con_salto_de_linea_no_inyecta_cabeceras(http_client, headers_autenticado, correos):
    r = await _enviar(
        http_client,
        headers_autenticado,
        {"resumen": "Falla\r\nBcc: victima@x.com", "descripcion": VALIDO["descripcion"]},
    )

    assert r.status_code == 201
    fila = await _fila(r.json()["id"])
    assert "\n" not in fila["resumen"] and "\r" not in fila["resumen"]


# ============================================================
# Campos obligatorios
# ============================================================
@pytest.mark.parametrize(
    "datos",
    [
        {"descripcion": VALIDO["descripcion"]},  # sin resumen
        {"resumen": VALIDO["resumen"]},  # sin descripción
        {"resumen": "    ", "descripcion": VALIDO["descripcion"]},  # solo espacios
        {"resumen": "abc", "descripcion": VALIDO["descripcion"]},  # muy corto
        {"resumen": "x" * 121, "descripcion": VALIDO["descripcion"]},  # muy largo
        {"resumen": VALIDO["resumen"], "descripcion": "corto"},
        {"resumen": VALIDO["resumen"], "descripcion": "y" * 4001},
    ],
)
async def test_campos_obligatorios_y_rangos(http_client, headers_autenticado, tenant_y_usuario, datos, correos):
    r = await _enviar(http_client, headers_autenticado, datos)

    assert r.status_code == 422
    # Nada se guardó ni se avisó.
    assert await fetch_all("SELECT 1 FROM reportes_incidencia WHERE portal_user_id = $1", tenant_y_usuario["usuario_id"]) == []
    assert correos == []


async def test_sin_sesion_da_401(http_client):
    r = await http_client.post(URL, data=VALIDO)
    assert r.status_code == 401


# ============================================================
# Captura de metadatos
# ============================================================
async def test_guarda_el_contexto_conocido_y_descarta_el_resto(http_client, headers_autenticado, correos):
    contexto = {
        "ruta": "/conversaciones?canal=whatsapp",
        "navegador": "Chrome 126",
        "sistema_operativo": "Windows 11",
        "idioma": "es-MX",
        "zona_horaria": "America/Mexico_City",
        "pantalla": {"ancho": 1920, "alto": 1080},
        "ventana": {"ancho": 1280, "alto": 720},
        "access_token": "secreto-que-no-debe-guardarse",
        "cualquier_cosa": {"x": 1},
    }

    r = await _enviar(http_client, headers_autenticado, {**VALIDO, "contexto": json.dumps(contexto)})

    guardado = json.loads((await _fila(r.json()["id"]))["contexto"])
    assert guardado["ruta"] == "/conversaciones?canal=whatsapp"
    assert guardado["navegador"] == "Chrome 126"
    assert guardado["sistema_operativo"] == "Windows 11"
    assert guardado["pantalla"] == {"ancho": 1920, "alto": 1080}
    assert guardado["ventana"] == {"ancho": 1280, "alto": 720}
    assert "access_token" not in guardado and "cualquier_cosa" not in guardado


def test_limpiar_contexto_recorta_y_valida_tipos():
    limpio = svc.limpiar_contexto(
        json.dumps(
            {
                "ruta": "/" + "a" * 900,
                "pantalla": {"ancho": True, "alto": 1080},  # bool no es un ancho
                "ventana": {"ancho": -5, "alto": 700},
                "idioma": 123,  # no es texto
            }
        )
    )
    assert len(limpio["ruta"]) == 500
    assert "pantalla" not in limpio and "ventana" not in limpio and "idioma" not in limpio


@pytest.mark.parametrize("bruto", [None, "", "no es json", "[1,2]", "42", '"texto"'])
def test_contexto_ilegible_queda_vacio_sin_romper(bruto):
    assert svc.limpiar_contexto(bruto) == {}


async def test_sin_user_agent_en_el_contexto_usa_el_de_la_cabecera(http_client, headers_autenticado, correos):
    r = await _enviar(http_client, {**headers_autenticado, "User-Agent": "Mozilla/5.0 PruebaAgente"})

    guardado = json.loads((await _fila(r.json()["id"]))["contexto"])
    assert guardado["user_agent"] == "Mozilla/5.0 PruebaAgente"


# ============================================================
# Adjunto
# ============================================================
async def test_adjunto_png_valido_se_guarda(http_client, headers_autenticado, correos):
    r = await _enviar(http_client, headers_autenticado, archivos={"adjunto": ("captura.png", _png(), "image/png")})

    assert r.status_code == 201
    adj = await fetch_one("SELECT mime, ancho, alto, bytes FROM reportes_incidencia_adjuntos WHERE reporte_id = $1", r.json()["id"])
    assert (adj["mime"], adj["ancho"], adj["alto"]) == ("image/png", 40, 30)


async def test_adjunto_jpeg_valido_se_guarda(http_client, headers_autenticado, correos):
    r = await _enviar(http_client, headers_autenticado, archivos={"adjunto": ("foto.jpg", _jpeg(), "image/jpeg")})
    assert r.status_code == 201


async def test_captura_de_pantalla_grande_si_cabe(http_client, headers_autenticado, correos):
    """A diferencia de la foto de perfil (642 px), una captura mide como el monitor."""
    r = await _enviar(http_client, headers_autenticado, archivos={"adjunto": ("c.png", _png(3840, 2160), "image/png")})
    assert r.status_code == 201


@pytest.mark.parametrize(
    ("nombre", "contenido", "esperado"),
    [
        ("informe.pdf", b"%PDF-1.4 ...", 415),  # no es imagen
        ("virus.png", b"MZ\x90\x00 no soy un png", 415),  # extensión miente
        ("captura.png", _jpeg(), 415),  # extensión no coincide con el contenido
        ("vacio.png", b"", 422),
        ("roto.png", b"\x89PNG\r\n\x1a\nbasura", 422),  # encabezado dañado
        ("enorme.png", _png(9000, 9000), 422),  # dimensiones fuera de rango
    ],
)
async def test_adjunto_invalido_se_rechaza(http_client, headers_autenticado, tenant_y_usuario, nombre, contenido, esperado, correos):
    r = await _enviar(http_client, headers_autenticado, archivos={"adjunto": (nombre, contenido, "image/png")})

    assert r.status_code == esperado
    assert await fetch_all("SELECT 1 FROM reportes_incidencia WHERE portal_user_id = $1", tenant_y_usuario["usuario_id"]) == []


async def test_adjunto_de_mas_de_5_mb_da_413(http_client, headers_autenticado, correos):
    grande = _png() + b"\x00" * (settings.REPORTE_ADJUNTO_MAX_BYTES + 1)

    r = await _enviar(http_client, headers_autenticado, archivos={"adjunto": ("grande.png", grande, "image/png")})

    assert r.status_code == 413


# ============================================================
# Límite por hora
# ============================================================
async def test_cuarto_reporte_en_la_hora_da_429(http_client, headers_autenticado, correos):
    assert settings.REPORTES_MAX_POR_HORA == 3
    for _ in range(3):
        assert (await _enviar(http_client, headers_autenticado)).status_code == 201

    r = await _enviar(http_client, headers_autenticado)

    assert r.status_code == 429
    assert 0 < int(r.headers["Retry-After"]) <= 3600


async def test_pasada_la_hora_vuelve_a_dejar_enviar(http_client, headers_autenticado, tenant_y_usuario, correos):
    for _ in range(3):
        await _enviar(http_client, headers_autenticado)
    assert (await _enviar(http_client, headers_autenticado)).status_code == 429

    # Pasó más de una hora desde el primero.
    await execute(
        """
        UPDATE reportes_incidencia SET creado_en = NOW() - INTERVAL '61 minutes'
         WHERE id = (SELECT id FROM reportes_incidencia WHERE portal_user_id = $1
                      ORDER BY creado_en ASC LIMIT 1)
        """,
        tenant_y_usuario["usuario_id"],
    )

    assert (await _enviar(http_client, headers_autenticado)).status_code == 201


async def test_el_limite_es_por_usuario(http_client, headers_autenticado, tenant_y_usuario, correos):
    for _ in range(3):
        await _enviar(http_client, headers_autenticado)

    otro_id = uuid4()
    await execute(
        "INSERT INTO portal_users (id, tenant_id, email, password_hash, role) VALUES ($1, $2, $3, 'x', 'member')",
        otro_id,
        tenant_y_usuario["tenant_id"],
        f"test-{otro_id}@ejemplo.com",
    )
    otro = {"Authorization": f"Bearer {crear_access_token(otro_id, tenant_y_usuario['tenant_id'], 'member')}"}

    assert (await _enviar(http_client, otro)).status_code == 201


# ============================================================
# Gerencia
# ============================================================
async def test_solo_gerencia_ve_la_lista(http_client, headers_autenticado, correos):
    r = await http_client.get("/api/gerencia/incidencias", headers=headers_autenticado)
    assert r.status_code == 403


async def test_gerencia_ve_abre_y_actualiza(http_client, headers_autenticado, gerencia, correos):
    creado = await _enviar(http_client, headers_autenticado, archivos={"adjunto": ("c.png", _png(), "image/png")})
    rid = creado.json()["id"]

    lista = await http_client.get("/api/gerencia/incidencias?estado=abierto", headers=headers_autenticado)
    assert rid in [x["id"] for x in lista.json()]

    detalle = (await http_client.get(f"/api/gerencia/incidencias/{rid}", headers=headers_autenticado)).json()
    assert detalle["tiene_adjunto"] is True and detalle["estado"] == "abierto"
    assert detalle["email"] == gerencia["email"]

    img = await http_client.get(f"/api/gerencia/incidencias/{rid}/adjunto", headers=headers_autenticado)
    assert img.status_code == 200
    assert img.headers["content-type"] == "image/png"
    assert img.headers["x-content-type-options"] == "nosniff"
    assert img.content == _png()

    upd = await http_client.patch(
        f"/api/gerencia/incidencias/{rid}", json={"estado": "resuelto"}, headers=headers_autenticado
    )
    assert upd.status_code == 200
    assert upd.json()["estado"] == "resuelto"
    assert upd.json()["atendido_por"] == gerencia["email"]

    bitacora = await fetch_one(
        "SELECT detalle FROM gerencia_auditoria WHERE accion = 'incidencia_actualizada' AND actor_email = $1 ORDER BY created_at DESC LIMIT 1",
        gerencia["email"],
    )
    assert bitacora is not None
    await execute("DELETE FROM gerencia_auditoria WHERE accion = 'incidencia_actualizada' AND actor_email = $1", gerencia["email"])


async def test_estado_invalido_da_422(http_client, headers_autenticado, gerencia, correos):
    rid = (await _enviar(http_client, headers_autenticado)).json()["id"]

    r = await http_client.patch(f"/api/gerencia/incidencias/{rid}", json={"estado": "borrado"}, headers=headers_autenticado)

    assert r.status_code == 422
