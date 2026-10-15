"""
Correo espejo de las notificaciones y aviso a gerencia (spec 004, T5).

Sin BD, sin SMTP y sin red: `fetch_all` y el envío de correo se sustituyen por
fakes que recogen las llamadas. Cubre RF-1 a RF-5, CL-8, CL-13, CL-14 y A1, A2,
A3, A13, A22.
"""

import asyncio
import types
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

import realtime
from routers import eventos
from schemas import ConversacionTransferidaIn
from services import correo, correo_espejo
from services.asignacion_conversaciones import ResultadoAuto

TENANT = uuid4()


def _cuenta(role: str, email: str | None = None, activa: bool = True) -> dict:
    uid = uuid4()
    return {"id": uid, "role": role, "email": email if email is not None else f"{role}-{uid}@x.com", "is_active": activa}


class _Entorno:
    """Cuentas del negocio + correos enviados."""

    def __init__(self, cuentas: list[dict]) -> None:
        self.cuentas = cuentas
        self.correos: list[tuple[str, str, str]] = []
        self.falla_smtp = False
        self.consultas: list[tuple] = []

    async def fetch_all(self, sql: str, *args):
        self.consultas.append((sql, args))
        tenant_id = args[0]
        assert tenant_id == TENANT
        if "id = $2" in sql:  # alerta personal
            return [c for c in self.cuentas if c["id"] == args[1] and c["is_active"]]
        roles = set(args[1])
        return [c for c in self.cuentas if c["role"] in roles and c["is_active"]]

    async def enviar(self, destino: str, titulo: str, mensaje: str) -> None:
        if self.falla_smtp:
            raise correo.ErrorEnvioCorreo("SMTP caído")
        self.correos.append((destino, titulo, mensaje))


async def _esperar_tareas() -> None:
    """Espera las tareas del espejo de ESTE test (otros tests pueden dejar tareas de otro loop)."""
    actual = asyncio.get_running_loop()
    propias = [t for t in correo_espejo._tareas if t.get_loop() is actual]
    await asyncio.gather(*propias)


@pytest.fixture
def entorno(monkeypatch):
    def _crear(cuentas: list[dict]) -> _Entorno:
        e = _Entorno(cuentas)
        monkeypatch.setattr(correo_espejo, "fetch_all", e.fetch_all)
        monkeypatch.setattr(correo_espejo, "enviar_notificacion_negocio", e.enviar)
        return e

    return _crear


# ============================================================
# Reglas puras
# ============================================================
@pytest.mark.parametrize("tipo", ["cuota_excedida", "perfil_incompleto", "solicitud_escritura"])
def test_tipos_con_correo_propio_no_se_espejan(tipo):
    assert not correo_espejo.debe_espejarse(tipo)


@pytest.mark.parametrize(
    "tipo",
    [
        "sin_actividad",
        "nuevo_lead",
        "conversacion_transferida",
        "conversacion_asignada",
        "aviso_cita_no_enviado",
        "asignacion_fallida",
    ],
)
def test_el_resto_se_espeja_sin_exclusion_por_tipo(tipo):
    # Si alguien añade una exclusión por volumen, este test rompe a propósito.
    assert correo_espejo.debe_espejarse(tipo)


def test_destinatarios_filtra_inactivas_sin_correo_repetidas_y_excluidas():
    a, b, c, d = _cuenta("owner", "A@x.com"), _cuenta("member", "a@x.com"), _cuenta("member", "   "), _cuenta("owner", "d@x.com", activa=False)
    e = _cuenta("superadmin", "e@x.com")
    f = _cuenta("member", "f@x.com")
    assert correo_espejo.destinatarios([a, b, c, d, e, f], excluir=[f["id"]]) == ["A@x.com", "e@x.com"]


# ============================================================
# Espejo: audiencia, SMTP, escape
# ============================================================
async def test_un_correo_por_destinatario_de_la_sala_incluido_member(entorno):
    owner, sup, member = _cuenta("owner"), _cuenta("superadmin"), _cuenta("member")
    vendedor = _cuenta("vendedor")
    e = entorno([owner, sup, member, vendedor])

    n = await correo_espejo.espejar(TENANT, "nuevo_lead", "Lead nuevo", "Ana escribió")

    assert n == 3
    assert sorted(c[0] for c in e.correos) == sorted([owner["email"], sup["email"], member["email"]])
    assert all(c[1:] == ("Lead nuevo", "Ana escribió") for c in e.correos)


@pytest.mark.parametrize("tipo", ["sin_actividad", "nuevo_lead"])
async def test_sin_actividad_y_nuevo_lead_generan_correo(entorno, tipo):
    e = entorno([_cuenta("owner")])
    assert await correo_espejo.espejar(TENANT, tipo, "t", "m") == 1
    assert len(e.correos) == 1


async def test_alerta_personal_solo_va_a_esa_cuenta(entorno):
    owner, vendedor = _cuenta("owner"), _cuenta("vendedor")
    e = entorno([owner, vendedor])

    await correo_espejo.espejar(TENANT, "conversacion_asignada", "t", "m", portal_user_id=vendedor["id"])

    assert [c[0] for c in e.correos] == [vendedor["email"]]


async def test_cuenta_inactiva_o_sin_correo_no_recibe(entorno):
    e = entorno([_cuenta("owner", activa=False), _cuenta("member", "   "), _cuenta("superadmin", "ok@x.com")])

    await correo_espejo.espejar(TENANT, "nuevo_lead", "t", "m")

    assert [c[0] for c in e.correos] == ["ok@x.com"]


async def test_alerta_personal_de_cuenta_inactiva_no_envia(entorno):
    inactiva = _cuenta("owner", activa=False)
    e = entorno([inactiva])

    assert await correo_espejo.espejar(TENANT, "nuevo_lead", "t", "m", portal_user_id=inactiva["id"]) == 0
    assert e.correos == []


@pytest.mark.parametrize("tipo", ["cuota_excedida", "perfil_incompleto", "solicitud_escritura"])
async def test_tipos_con_correo_propio_no_consultan_ni_envian(entorno, tipo):
    e = entorno([_cuenta("owner")])

    assert await correo_espejo.espejar(TENANT, tipo, "t", "m") == 0
    assert e.correos == [] and e.consultas == []


async def test_smtp_caido_no_lanza_y_se_intenta_a_todos_una_vez(entorno, caplog):
    e = entorno([_cuenta("owner"), _cuenta("member")])
    e.falla_smtp = True

    with caplog.at_level("ERROR", logger="operativai.correo_espejo"):
        n = await correo_espejo.espejar(TENANT, "nuevo_lead", "t", "m")

    assert n == 0
    assert len([r for r in caplog.records if "correo espejo" in r.getMessage()]) == 2


async def test_fallo_de_bd_no_lanza(monkeypatch):
    async def revienta(*_a, **_k):
        raise RuntimeError("BD caída")

    monkeypatch.setattr(correo_espejo, "fetch_all", revienta)
    assert await correo_espejo.espejar(TENANT, "nuevo_lead", "t", "m") == 0


async def test_excluir_correo_a_quita_solo_el_correo(entorno):
    owner, member = _cuenta("owner"), _cuenta("member")
    e = entorno([owner, member])

    await correo_espejo.espejar(TENANT, "conversacion_transferida", "t", "m", excluir_correo_a=[member["id"]])

    assert [c[0] for c in e.correos] == [owner["email"]]


async def test_programar_espejo_corre_en_segundo_plano_y_no_lanza(entorno):
    e = entorno([_cuenta("owner")])

    correo_espejo.programar_espejo(TENANT, "nuevo_lead", "t", "m")
    assert e.correos == []  # no esperó: la tarea aún no corrió
    await _esperar_tareas()
    assert len(e.correos) == 1


# ============================================================
# Cuerpo del correo (RF-2, A13)
# ============================================================
async def test_nombre_con_script_se_escapa_y_el_enlace_esta(monkeypatch):
    enviados: list[dict] = []

    async def falso(destino, asunto, texto, html):
        enviados.append({"destino": destino, "asunto": asunto, "texto": texto, "html": html})

    monkeypatch.setattr(correo, "enviar_correo", falso)
    monkeypatch.setattr(correo.settings, "BASE_URL_FRONTEND", "https://app.operativai.test")

    await correo.enviar_notificacion_negocio("a@x.com", "Lead <script>alert(1)</script>", "Hola <b>Ana</b> & Cía\nlínea 2")

    (c,) = enviados
    assert "<script>" not in c["html"] and "&lt;script&gt;" in c["html"]
    assert "&lt;b&gt;Ana&lt;/b&gt; &amp; Cía" in c["html"]
    assert 'href="https://app.operativai.test"' in c["html"]
    # La versión de texto lleva el mismo título y mensaje, y el enlace.
    assert "Hola <b>Ana</b> & Cía" in c["texto"] and "https://app.operativai.test" in c["texto"]
    assert "\n" not in c["asunto"] and c["asunto"].endswith("- OperativAI")


# ============================================================
# broadcast_alerta: espejo y orden
# ============================================================
class _Alerta:
    def __init__(self, tipo, titulo, mensaje, portal_user_id):
        self.id = uuid4()
        self.tenant_id = TENANT
        self.tipo = tipo
        self.titulo = titulo
        self.mensaje = mensaje
        self.datos = {}
        self.leido = False
        self.creado_en = datetime(2026, 10, 14, tzinfo=timezone.utc)
        self.portal_user_id = portal_user_id


@pytest.fixture
def broadcast(monkeypatch):
    espejos: list[tuple] = []
    emitidos: list[tuple] = []

    async def crear(tenant_id, tipo, titulo, mensaje, datos=None, portal_user_id=None):
        return _Alerta(tipo, titulo, mensaje, portal_user_id)

    async def emit(evento, datos, room=None):
        emitidos.append((evento, room))

    def espejo(*args):
        espejos.append(args)

    monkeypatch.setattr(realtime, "crear_alerta", crear)
    monkeypatch.setattr(realtime, "programar_push_alerta", lambda *a, **k: None)
    monkeypatch.setattr(realtime, "programar_espejo", espejo)
    monkeypatch.setattr(realtime.sio, "emit", emit)
    return types.SimpleNamespace(espejos=espejos, emitidos=emitidos)


async def test_broadcast_programa_el_espejo_con_la_audiencia_y_la_exclusion(broadcast):
    excluido = uuid4()

    await realtime.broadcast_alerta(TENANT, "nuevo_lead", "T", "M", excluir_correo_a=[excluido])

    (args,) = broadcast.espejos
    assert args == (TENANT, "nuevo_lead", "T", "M", None, [excluido])
    assert broadcast.emitidos[0][0] == "nueva_alerta"


async def test_un_fallo_del_espejo_no_cambia_la_alerta(broadcast, monkeypatch):
    # programar_espejo real, con un SMTP caído y BD caída: la alerta igual sale.
    async def revienta(*_a, **_k):
        raise RuntimeError("BD caída")

    monkeypatch.setattr(realtime, "programar_espejo", correo_espejo.programar_espejo)
    monkeypatch.setattr(correo_espejo, "fetch_all", revienta)

    await realtime.broadcast_alerta(TENANT, "nuevo_lead", "T", "M")
    await _esperar_tareas()

    assert [e[0] for e in broadcast.emitidos] == ["nueva_alerta"]


def test_los_eventos_de_interfaz_no_pasan_por_el_espejo():
    # RF-5: el espejo solo cuelga de broadcast_alerta, no de los emit de UI.
    import inspect

    for nombre in ("emit_mensaje", "emit_conversacion_estado", "emitir_datos", "emitir_alertas_leidas", "marcar_leida"):
        assert "espejo" not in inspect.getsource(getattr(realtime, nombre))
    assert "programar_espejo" in inspect.getsource(realtime.broadcast_alerta)


# ============================================================
# Aviso a gerencia (alerta personal por owner/superadmin activo)
# ============================================================
@pytest.fixture
def gerencia(monkeypatch):
    emitidas: list[dict] = []

    async def broadcast_falso(tenant_id, tipo, titulo, mensaje, datos=None, portal_user_id=None, excluir_correo_a=()):
        emitidas.append({"tenant": tenant_id, "tipo": tipo, "datos": datos, "para": portal_user_id})

    monkeypatch.setattr(realtime, "broadcast_alerta", broadcast_falso)

    def preparar(cuentas: list[dict]):
        async def fetch_all(sql, *args):
            assert args[0] == TENANT
            roles = set(args[1])
            return [{"id": c["id"]} for c in cuentas if c["role"] in roles and c["is_active"]]

        monkeypatch.setattr(realtime, "fetch_all", fetch_all)
        return emitidas

    return preparar


async def test_aviso_a_gerencia_llega_a_owner_y_superadmin_activos_y_no_a_member(gerencia):
    owner, sup, member = _cuenta("owner"), _cuenta("superadmin"), _cuenta("member")
    owner_inactivo, vendedor = _cuenta("owner", activa=False), _cuenta("vendedor")
    emitidas = gerencia([owner, sup, member, owner_inactivo, vendedor])

    await realtime.avisar_gerencia(TENANT, "aviso_cita_no_enviado", "T", "M", {"motivo": "sin_telefono"})

    assert {e["para"] for e in emitidas} == {owner["id"], sup["id"]}
    assert all(e["tipo"] == "aviso_cita_no_enviado" and e["datos"] == {"motivo": "sin_telefono"} for e in emitidas)
    assert all(isinstance(e["para"], UUID) for e in emitidas)  # siempre personal, nunca de sala


async def test_aviso_a_gerencia_sin_cuentas_no_emite(gerencia):
    assert gerencia([_cuenta("member")]) == []
    await realtime.avisar_gerencia(TENANT, "aviso_cita_no_enviado", "T", "M")


async def test_aviso_a_gerencia_no_lanza_si_falla_la_bd(monkeypatch):
    async def revienta(*_a, **_k):
        raise RuntimeError("BD caída")

    monkeypatch.setattr(realtime, "fetch_all", revienta)
    await realtime.avisar_gerencia(TENANT, "aviso_cita_no_enviado", "T", "M")


async def test_aviso_a_gerencia_sigue_con_las_demas_si_una_falla(monkeypatch):
    a, b = _cuenta("owner"), _cuenta("superadmin")
    llegaron: list[UUID] = []

    async def fetch_all(sql, *args):
        return [{"id": a["id"]}, {"id": b["id"]}]

    async def broadcast_falso(tenant_id, tipo, titulo, mensaje, datos=None, portal_user_id=None, **kw):
        if portal_user_id == a["id"]:
            raise RuntimeError("falló la primera")
        llegaron.append(portal_user_id)

    monkeypatch.setattr(realtime, "fetch_all", fetch_all)
    monkeypatch.setattr(realtime, "broadcast_alerta", broadcast_falso)

    await realtime.avisar_gerencia(TENANT, "aviso_cita_no_enviado", "T", "M")

    assert llegaron == [b["id"]]


async def test_el_aviso_a_gerencia_tiene_correo_espejo_por_cuenta(entorno, monkeypatch):
    # Cada alerta personal de gerencia se espeja a su dueño, y a nadie más.
    owner, sup, member = _cuenta("owner"), _cuenta("superadmin"), _cuenta("member")
    e = entorno([owner, sup, member])
    async def crear(tenant_id, tipo, titulo, mensaje, datos=None, portal_user_id=None):
        return _Alerta(tipo, titulo, mensaje, portal_user_id)

    async def emit(*_a, **_k):
        return None

    async def fetch_gerencia(sql, *args):
        return [{"id": owner["id"]}, {"id": sup["id"]}]

    monkeypatch.setattr(realtime, "crear_alerta", crear)
    monkeypatch.setattr(realtime, "programar_push_alerta", lambda *a, **k: None)
    monkeypatch.setattr(realtime.sio, "emit", emit)
    monkeypatch.setattr(realtime, "fetch_all", fetch_gerencia)
    monkeypatch.setattr(realtime, "programar_espejo", correo_espejo.programar_espejo)

    await realtime.avisar_gerencia(TENANT, "aviso_cita_no_enviado", "T", "M")
    await _esperar_tareas()

    assert sorted(c[0] for c in e.correos) == sorted([owner["email"], sup["email"]])


# ============================================================
# RF-4 e: transferida + asignada al mismo usuario = un solo correo
# ============================================================
def _parchar_eventos(monkeypatch, resultado):
    llamadas: list[dict] = []

    async def broadcast_falso(tenant_id, tipo, titulo, mensaje, datos=None, **kw):
        llamadas.append({"tipo": tipo, **kw})

    async def nada(*_a, **_k):
        return None

    async def autoasignar(*_a, **_k):
        return resultado

    monkeypatch.setattr(eventos, "broadcast_alerta", broadcast_falso)
    monkeypatch.setattr(eventos, "avisar_a_proveedor", nada)
    monkeypatch.setattr(eventos, "avisar_a_usuario", nada)
    monkeypatch.setattr(eventos, "emit_conversacion_estado", nada)
    monkeypatch.setattr(eventos.asignacion_conv, "autoasignar", autoasignar)
    monkeypatch.setattr(eventos.conversaciones_svc, "cuenta_del_proveedor", nada)
    return llamadas


def _entrada() -> ConversacionTransferidaIn:
    return ConversacionTransferidaIn(tenant_id=uuid4(), conversation_id=uuid4(), canal="whatsapp", area="ventas")


async def test_transferida_excluye_del_correo_al_asignado_que_recibe_asignada(monkeypatch):
    asignado = uuid4()
    llamadas = _parchar_eventos(monkeypatch, ResultadoAuto(asignado, cayo_en_owner=False, fallo=False, motivo="x"))

    await eventos.conversacion_transferida(_entrada())

    (ll,) = llamadas
    assert ll["tipo"] == "conversacion_transferida"
    assert tuple(ll["excluir_correo_a"]) == (asignado,)


async def test_transferida_sin_asignacion_no_excluye_a_nadie(monkeypatch):
    llamadas = _parchar_eventos(monkeypatch, None)

    await eventos.conversacion_transferida(_entrada())

    assert tuple(llamadas[0]["excluir_correo_a"]) == ()


async def test_si_la_asignacion_falla_el_owner_recibe_ambos_correos(monkeypatch):
    # Cae en el owner con `asignacion_fallida` (no `conversacion_asignada`): no hay duplicado que evitar.
    owner = uuid4()
    llamadas = _parchar_eventos(monkeypatch, ResultadoAuto(owner, cayo_en_owner=True, fallo=True, motivo="x"))

    await eventos.conversacion_transferida(_entrada())

    assert tuple(llamadas[0]["excluir_correo_a"]) == ()
