"""
Registro, envío con registro, huérfanos y eventos de contexto (spec 004, T4).

Sin BD ni red. `_BD` es un almacén en memoria que reconoce las consultas de
`services/avisos_cita`, `contexto_agente` y `plantillas_whatsapp` por su texto y
simula `ON CONFLICT DO NOTHING` de forma atómica (sin `await` entre comprobar la
clave e insertar), igual que lo garantiza el UNIQUE de la BD. La unicidad real
en Postgres queda cubierta por `test_avisos_sql.py` y por la prueba manual (T10).
"""

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from config import settings
from services import avisos_cita as ac
from services import contexto_agente, plantillas_whatsapp
from services.whatsapp import ProveedorWhatsAppError

TENANT = uuid4()
AHORA = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
INICIO = AHORA + timedelta(hours=4)


class _BD:
    """Almacén en memoria de las tablas que tocan los servicios de avisos."""

    def __init__(self) -> None:
        self.ejecuciones: dict[UUID, dict[str, Any]] = {}
        self.avisos: dict[UUID, dict[str, Any]] = {}
        self.eventos: list[dict[str, Any]] = []
        self.plantillas: dict[str, str] = {
            "notificacion_cancelacion": "aprobada",
            "recordatorio_cita": "aprobada",
        }
        self.auditoria: list[tuple[Any, ...]] = []
        self.conversaciones: list[dict[str, Any]] = []
        self.consultas: list[str] = []
        self.fallar_evento = False

    # --- helpers de prueba ---
    def nueva_ejecucion(self, *, latido: datetime, terminada: datetime | None = None) -> UUID:
        ejecucion_id = uuid4()
        self.ejecuciones[ejecucion_id] = {"latido_en": latido, "terminada_en": terminada}
        return ejecucion_id

    def aviso_en_curso(self, *, ejecucion_id: UUID | None, tipo: str = "cancelacion") -> UUID:
        aviso_id = uuid4()
        self.avisos[aviso_id] = {
            "id": aviso_id, "tenant_id": TENANT, "reserva_id": uuid4(), "tipo": tipo,
            "hora_inicio": INICIO, "estado": "en_curso", "motivo": None,
            "sin_conversacion": False, "telefono_destino": None,
            "ejecucion_id": ejecucion_id, "cerrado_en": None,
        }
        return aviso_id

    def _clave_existe(self, reserva_id: UUID, tipo: str, hora_inicio: datetime) -> bool:
        return any(
            a["reserva_id"] == reserva_id and a["tipo"] == tipo and a["hora_inicio"] == hora_inicio
            for a in self.avisos.values()
        )

    # --- sustitutos de session ---
    async def fetch_value(self, sql: str, *args: Any) -> Any:
        self.consultas.append(sql)
        await asyncio.sleep(0)  # cede el turno: permite pasadas solapadas
        if "INSERT INTO avisos_cita_ejecuciones" in sql:
            ejecucion_id = uuid4()
            self.ejecuciones[ejecucion_id] = {
                "latido_en": datetime.now(timezone.utc), "terminada_en": None,
            }
            return ejecucion_id
        if "INSERT INTO avisos_cita" in sql:
            tenant_id, reserva_id, tipo, hora_inicio, estado, motivo, sin_conv, ejec = args
            if self._clave_existe(reserva_id, tipo, hora_inicio):
                return None  # ON CONFLICT DO NOTHING
            aviso_id = uuid4()
            self.avisos[aviso_id] = {
                "id": aviso_id, "tenant_id": tenant_id, "reserva_id": reserva_id,
                "tipo": tipo, "hora_inicio": hora_inicio, "estado": estado,
                "motivo": motivo, "sin_conversacion": sin_conv, "telefono_destino": None,
                "ejecucion_id": ejec,
                "cerrado_en": None if estado == "en_curso" else datetime.now(timezone.utc),
            }
            return aviso_id
        if "UPDATE avisos_cita" in sql and "RETURNING id" in sql:
            aviso_id, estado, motivo, telefono = args
            aviso = self.avisos.get(aviso_id)
            if aviso is None or aviso["estado"] != "en_curso":
                return None
            aviso.update(
                estado=estado, motivo=motivo, telefono_destino=telefono,
                cerrado_en=datetime.now(timezone.utc),
            )
            return aviso_id
        raise AssertionError(f"fetch_value inesperado: {sql}")

    async def fetch_all(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.consultas.append(sql)
        assert "FROM avisos_cita a" in sql
        filas = []
        for a in self.avisos.values():
            if a["estado"] != "en_curso":
                continue
            e = self.ejecuciones.get(a["ejecucion_id"]) if a["ejecucion_id"] else None
            filas.append({
                "id": a["id"], "tenant_id": a["tenant_id"], "reserva_id": a["reserva_id"],
                "tipo": a["tipo"],
                "latido_en": e["latido_en"] if e else None,
                "terminada_en": e["terminada_en"] if e else None,
            })
        return filas

    async def fetch_one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.consultas.append(sql)
        if "FROM plantillas_whatsapp_globales" in sql:
            estado = self.plantillas.get(args[0])
            return {"estado": estado} if estado else None
        if "FROM conversations" in sql:
            tenant_id, user_id = args
            for c in self.conversaciones:
                if c["tenant_id"] == tenant_id and c["user_id"] == user_id:
                    return {"id": c["id"]}
            return None
        raise AssertionError(f"fetch_one inesperado: {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        self.consultas.append(sql)
        if "INSERT INTO contexto_agente_eventos" in sql:
            if self.fallar_evento:
                raise RuntimeError("BD caída")
            tenant_id, conversation_id, reserva_id, aviso_id, tipo, hora_cita, servicio = args
            if not any(e["aviso_id"] == aviso_id for e in self.eventos):
                self.eventos.append({
                    "tenant_id": tenant_id, "conversation_id": conversation_id,
                    "reserva_id": reserva_id, "aviso_id": aviso_id, "tipo": tipo,
                    "hora_cita": hora_cita, "servicio": servicio,
                })
        elif "UPDATE plantillas_whatsapp_globales" in sql:
            self.plantillas[args[0]] = args[1]
        elif "INSERT INTO gerencia_auditoria" in sql:
            self.auditoria.append(args)
        elif "UPDATE avisos_cita_ejecuciones" in sql:
            self.ejecuciones[args[0]]["latido_en"] = datetime.now(timezone.utc)
            if "terminada_en" in sql:
                self.ejecuciones[args[0]]["terminada_en"] = datetime.now(timezone.utc)
        else:
            raise AssertionError(f"execute inesperado: {sql}")
        return "OK"


class _Proveedor:
    """Fake de `cuenta_global_wa.enviar_plantilla`: cuenta llamadas y puede fallar."""

    def __init__(self, bd: _BD) -> None:
        self.bd = bd
        self.llamadas: list[tuple[str, str, list[str]]] = []
        self.registros_al_llamar: list[list[str]] = []
        self.errores: list[Exception | None] = []  # uno por llamada; vacío = éxito
        self.durante: Any = None

    async def __call__(self, destinatario: str, plantilla: str, parametros: list[str]):
        self.llamadas.append((destinatario, plantilla, parametros))
        # D1: en este punto el registro tiene que existir y estar en_curso.
        self.registros_al_llamar.append([a["estado"] for a in self.bd.avisos.values()])
        await asyncio.sleep(0)
        if self.durante is not None:
            await self.durante()
        error = self.errores.pop(0) if self.errores else None
        if error is not None:
            raise error
        return types.SimpleNamespace(id_mensaje="wamid.1")


@pytest.fixture
def bd(monkeypatch: pytest.MonkeyPatch) -> _BD:
    almacen = _BD()
    for modulo in (ac, contexto_agente, plantillas_whatsapp):
        for nombre in ("fetch_value", "fetch_all", "fetch_one", "execute"):
            if hasattr(modulo, nombre):
                monkeypatch.setattr(modulo, nombre, getattr(almacen, nombre))
    monkeypatch.setattr(settings, "AVISOS_EJECUCION_VIVA_MINUTOS", 5)
    return almacen


@pytest.fixture
def proveedor(bd: _BD, monkeypatch: pytest.MonkeyPatch) -> _Proveedor:
    falso = _Proveedor(bd)
    monkeypatch.setattr(ac.cuenta_global_wa, "enviar_plantilla", falso)
    return falso


@pytest.fixture
def gerencia(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Sustituye `realtime.avisar_gerencia` (llega en T5) y recoge sus llamadas."""
    llamadas: list[tuple[Any, ...]] = []

    async def _avisar(*args: Any) -> None:
        llamadas.append(args)

    falso = types.ModuleType("realtime")
    falso.avisar_gerencia = _avisar  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "realtime", falso)
    return llamadas


def _envio(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        tenant_id=TENANT, reserva_id=uuid4(), tipo="cancelacion", hora_inicio=INICIO,
        ejecucion_id=None, destinatario_e164="+525512345678",
        parametros=["Corte", "15-10-2026"], conversation_id=uuid4(), servicio="Corte",
    )
    base.update(extra)
    return base


# ------------------------------------------------------------
# Puerta de unicidad (CL-5, RF-10)
# ------------------------------------------------------------
async def test_la_misma_clave_solo_se_registra_una_vez(bd):
    reserva = uuid4()
    datos = dict(tenant_id=TENANT, reserva_id=reserva, tipo="recordatorio",
                 hora_inicio=INICIO, ejecucion_id=None)

    primero = await ac.registrar_intento(**datos)
    segundo = await ac.registrar_intento(**datos)
    otra_hora = await ac.registrar_intento(**{**datos, "hora_inicio": INICIO + timedelta(hours=1)})
    otro_tipo = await ac.registrar_intento(**{**datos, "tipo": "cancelacion"})

    assert primero is not None and segundo is None
    assert otra_hora is not None and otro_tipo is not None
    assert len(bd.avisos) == 3


async def test_dos_envios_simultaneos_de_la_misma_clave_llaman_al_proveedor_una_vez(bd, proveedor):
    datos = _envio(tipo="recordatorio")

    resultados = await asyncio.gather(
        ac.enviar_con_registro(**datos), ac.enviar_con_registro(**datos)
    )

    assert len(proveedor.llamadas) == 1
    assert sorted(r.estado or "" for r in resultados) == ["", "enviado"]
    assert len(bd.avisos) == 1  # el perdedor no crea registro nuevo
    assert next(iter(bd.avisos.values()))["estado"] == "enviado"


# ------------------------------------------------------------
# Orden D1 y resultado enviado
# ------------------------------------------------------------
async def test_el_registro_en_curso_existe_antes_de_llamar_al_proveedor(bd, proveedor):
    await ac.enviar_con_registro(**_envio())

    assert proveedor.registros_al_llamar == [["en_curso"]]


async def test_enviado_guarda_telefono_cierre_y_crea_un_evento(bd, proveedor):
    datos = _envio()
    resultado = await ac.enviar_con_registro(**datos)

    aviso = bd.avisos[resultado.aviso_id]
    assert resultado.enviado and resultado.evento_creado
    assert aviso["estado"] == "enviado" and aviso["motivo"] is None
    assert aviso["telefono_destino"] == "+525512345678"
    assert aviso["cerrado_en"] is not None
    assert aviso["sin_conversacion"] is False
    assert proveedor.llamadas == [("+525512345678", "notificacion_cancelacion", ["Corte", "15-10-2026"])]
    assert len(bd.eventos) == 1
    evento = bd.eventos[0]
    assert evento["tipo"] == "cita_cancelada_por_negocio"
    assert evento["aviso_id"] == resultado.aviso_id
    assert evento["conversation_id"] == datos["conversation_id"]
    assert evento["hora_cita"] == INICIO and evento["servicio"] == "Corte"


async def test_el_recordatorio_usa_su_plantilla_y_su_evento(bd, proveedor):
    await ac.enviar_con_registro(**_envio(tipo="recordatorio", parametros=["Mi negocio", "15-10-2026"]))

    assert proveedor.llamadas[0][1] == "recordatorio_cita"
    assert bd.eventos[0]["tipo"] == "recordatorio_cita_enviado"


async def test_sin_conversacion_se_envia_igual_sin_evento_y_con_la_marca(bd, proveedor):
    resultado = await ac.enviar_con_registro(**_envio(conversation_id=None))

    aviso = bd.avisos[resultado.aviso_id]
    assert resultado.enviado and not resultado.evento_creado
    assert aviso["estado"] == "enviado" and aviso["sin_conversacion"] is True
    assert len(proveedor.llamadas) == 1
    assert bd.eventos == []


async def test_el_evento_se_crea_aunque_el_agente_este_apagado(bd, proveedor):
    """CL-12: nada consulta `tenant_agent_config` ni el estado del agente."""
    await ac.enviar_con_registro(**_envio())

    assert len(bd.eventos) == 1
    assert not any("tenant_agent_config" in c or "agente_ia_activo" in c for c in bd.consultas)


async def test_los_eventos_quedan_en_el_orden_en_que_se_cerraron_los_envios(bd, proveedor):
    reserva, conversacion = uuid4(), uuid4()
    await ac.enviar_con_registro(**_envio(
        reserva_id=reserva, conversation_id=conversacion, tipo="recordatorio"))
    await ac.enviar_con_registro(**_envio(
        reserva_id=reserva, conversation_id=conversacion, tipo="cancelacion"))

    assert [e["tipo"] for e in bd.eventos] == ["recordatorio_cita_enviado", "cita_cancelada_por_negocio"]


async def test_un_fallo_al_crear_el_evento_no_cambia_el_aviso_enviado(bd, proveedor):
    bd.fallar_evento = True

    resultado = await ac.enviar_con_registro(**_envio())

    assert resultado.enviado and not resultado.evento_creado
    assert bd.avisos[resultado.aviso_id]["estado"] == "enviado"


# ------------------------------------------------------------
# Fallos del proveedor (CL-9, CL-10, plan 2.9)
# ------------------------------------------------------------
async def test_error_generico_deja_fallido_sin_evento_y_avisa_en_cancelacion(bd, proveedor, gerencia):
    proveedor.errores = [ProveedorWhatsAppError("sin red", status_code=None)]

    resultado = await ac.enviar_con_registro(**_envio())

    aviso = bd.avisos[resultado.aviso_id]
    assert (aviso["estado"], aviso["motivo"]) == ("fallido", "error_proveedor")
    assert bd.eventos == []
    assert len(gerencia) == 1
    tenant_id, tipo, _titulo, _mensaje, datos = gerencia[0]
    assert tenant_id == TENANT and tipo == "aviso_cita_no_enviado"
    assert datos["motivo"] == "error_proveedor"


async def test_recordatorio_fallido_no_avisa_a_gerencia_ni_crea_evento(bd, proveedor, gerencia):
    proveedor.errores = [ProveedorWhatsAppError("boom", status_code=500)]

    resultado = await ac.enviar_con_registro(**_envio(tipo="recordatorio"))

    assert bd.avisos[resultado.aviso_id]["motivo"] == "plantilla_no_disponible"
    assert gerencia == [] and bd.eventos == []


async def test_un_error_inesperado_tambien_deja_fallido(bd, proveedor, gerencia):
    proveedor.errores = [ValueError("bug")]

    resultado = await ac.enviar_con_registro(**_envio())

    assert bd.avisos[resultado.aviso_id]["motivo"] == "error_proveedor"
    assert bd.eventos == []


@pytest.mark.parametrize("status", [400, 500])
async def test_400_y_500_dejan_plantilla_no_disponible_sin_frenar_los_demas(bd, proveedor, gerencia, status):
    proveedor.errores = [ProveedorWhatsAppError("x", status_code=status, cuerpo='{"error": {}}'), None]

    primero = await ac.enviar_con_registro(**_envio())
    segundo = await ac.enviar_con_registro(**_envio())

    assert bd.avisos[primero.aviso_id]["motivo"] == "plantilla_no_disponible"
    assert bd.avisos[primero.aviso_id]["estado"] == "fallido"
    assert segundo.enviado  # el siguiente envío de la pasada no se ve afectado
    assert bd.plantillas["notificacion_cancelacion"] == "aprobada"
    assert len(gerencia) == 1


async def test_132015_deja_la_plantilla_pausada(bd, proveedor, gerencia):
    proveedor.errores = [ProveedorWhatsAppError(
        "x", status_code=500, cuerpo='{"error": {"details": {"code": 132015}}}')]

    resultado = await ac.enviar_con_registro(**_envio())

    assert bd.avisos[resultado.aviso_id]["motivo"] == "plantilla_no_disponible"
    assert bd.plantillas["notificacion_cancelacion"] == "pausada"
    assert bd.plantillas["recordatorio_cita"] == "aprobada"  # la otra no se toca
    assert len(bd.auditoria) == 1


async def test_132016_deja_la_plantilla_rechazada(bd, proveedor, gerencia):
    proveedor.errores = [ProveedorWhatsAppError(
        "x", status_code=400, cuerpo='{"code": 132016}')]

    await ac.enviar_con_registro(**_envio(tipo="recordatorio"))

    assert bd.plantillas["recordatorio_cita"] == "rechazada"


@pytest.mark.parametrize(
    "cuerpo", ['{"error": {"code": "META_API_ERROR"}}', '{"error": {"code": 132001}}', None]
)
async def test_un_500_sin_codigo_o_con_132001_no_cambia_el_estado(bd, proveedor, gerencia, cuerpo):
    proveedor.errores = [ProveedorWhatsAppError("x", status_code=500, cuerpo=cuerpo)]

    resultado = await ac.enviar_con_registro(**_envio())

    assert bd.avisos[resultado.aviso_id]["motivo"] == "plantilla_no_disponible"
    assert bd.plantillas["notificacion_cancelacion"] == "aprobada"
    assert bd.auditoria == []


async def test_un_fallo_al_cambiar_el_estado_no_impide_cerrar_el_registro(bd, proveedor, gerencia, monkeypatch):
    async def _roto(*args: Any, **kwargs: Any):
        raise RuntimeError("BD caída")

    monkeypatch.setattr(ac.plantillas_whatsapp, "aplicar_error_envio", _roto)
    proveedor.errores = [ProveedorWhatsAppError("x", status_code=500, cuerpo='{"code": 132015}')]

    resultado = await ac.enviar_con_registro(**_envio())

    assert bd.avisos[resultado.aviso_id]["motivo"] == "plantilla_no_disponible"


# ------------------------------------------------------------
# Registros sin envío (omitido / fallido) y aviso a gerencia
# ------------------------------------------------------------
@pytest.mark.parametrize(
    "motivo, estado",
    [
        ("sin_telefono", "omitido"),
        ("telefono_invalido", "fallido"),
        ("canal_no_soportado", "omitido"),
        ("desactivado_por_configuracion", "omitido"),
        ("plantilla_no_aprobada", "omitido"),
        ("cita_no_confirmada", "omitido"),
    ],
)
async def test_omitido_y_fallido_sin_envio_no_llaman_al_proveedor_ni_crean_evento(
    bd, proveedor, gerencia, motivo, estado
):
    resultado = await ac.registrar_sin_envio(
        tenant_id=TENANT, reserva_id=uuid4(), tipo="recordatorio", hora_inicio=INICIO,
        ejecucion_id=None, motivo=motivo,
    )

    aviso = bd.avisos[resultado.aviso_id]
    assert (aviso["estado"], aviso["motivo"]) == (estado, motivo)
    assert aviso["cerrado_en"] is not None
    assert proveedor.llamadas == [] and bd.eventos == [] and gerencia == []


@pytest.mark.parametrize(
    "motivo, avisa",
    [
        ("sin_telefono", True),
        ("telefono_invalido", True),
        ("plantilla_no_aprobada", True),
        ("canal_no_soportado", False),
        ("desactivado_por_configuracion", False),
    ],
)
async def test_cancelacion_sin_envio_avisa_a_gerencia_solo_con_los_motivos_de_la_spec(
    bd, proveedor, gerencia, motivo, avisa
):
    await ac.registrar_sin_envio(
        tenant_id=TENANT, reserva_id=uuid4(), tipo="cancelacion", hora_inicio=INICIO,
        ejecucion_id=None, motivo=motivo,
    )

    assert (len(gerencia) == 1) is avisa
    assert proveedor.llamadas == []


async def test_registrar_sin_envio_respeta_la_unicidad(bd, proveedor, gerencia):
    datos = dict(tenant_id=TENANT, reserva_id=uuid4(), tipo="cancelacion", hora_inicio=INICIO,
                 ejecucion_id=None, motivo="sin_telefono")

    await ac.registrar_sin_envio(**datos)
    repetido = await ac.registrar_sin_envio(**datos)

    assert repetido.estado is None
    assert len(bd.avisos) == 1 and len(gerencia) == 1


async def test_un_fallo_al_avisar_a_gerencia_no_lanza(bd, proveedor, monkeypatch):
    async def _roto(*args: Any) -> None:
        raise RuntimeError("socket caído")

    falso = types.ModuleType("realtime")
    falso.avisar_gerencia = _roto  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "realtime", falso)

    resultado = await ac.registrar_sin_envio(
        tenant_id=TENANT, reserva_id=uuid4(), tipo="cancelacion", hora_inicio=INICIO,
        ejecucion_id=None, motivo="sin_telefono",
    )

    assert bd.avisos[resultado.aviso_id]["estado"] == "omitido"


def test_estado_para_motivo_y_motivos_que_avisan():
    assert ac.estado_para_motivo("telefono_invalido") == "fallido"
    assert ac.estado_para_motivo("hora_cambiada") == "omitido"
    for motivo in ("error_proveedor", "plantilla_no_disponible", "resultado_desconocido"):
        assert ac.debe_avisar_gerencia("cancelacion", motivo)
        assert not ac.debe_avisar_gerencia("recordatorio", motivo)
    assert not ac.debe_avisar_gerencia("cancelacion", None)


# ------------------------------------------------------------
# Revalidación del recordatorio (CL-6)
# ------------------------------------------------------------
async def test_hora_cambiada_solo_cierra_un_registro_ya_insertado(bd, proveedor):
    aviso_id = await ac.registrar_intento(
        tenant_id=TENANT, reserva_id=uuid4(), tipo="recordatorio", hora_inicio=INICIO,
        ejecucion_id=None)

    assert await ac.cerrar_sin_envio(aviso_id, "hora_cambiada") is True
    aviso = bd.avisos[aviso_id]
    assert (aviso["estado"], aviso["motivo"]) == ("omitido", "hora_cambiada")
    assert proveedor.llamadas == [] and bd.eventos == []
    # Sin registro previo no hay nada que cerrar y no se crea ninguno.
    assert await ac.cerrar_sin_envio(uuid4(), "hora_cambiada") is False
    assert len(bd.avisos) == 1


# ------------------------------------------------------------
# Huérfanos (CL-4, A18)
# ------------------------------------------------------------
async def test_en_curso_de_ejecucion_muerta_pasa_a_fallido_resultado_desconocido(bd, proveedor, gerencia):
    muerta = bd.nueva_ejecucion(latido=AHORA - timedelta(minutes=6))
    aviso_id = bd.aviso_en_curso(ejecucion_id=muerta, tipo="recordatorio")

    cerrados = await ac.recuperar_huerfanos(AHORA)

    assert cerrados == [aviso_id]
    aviso = bd.avisos[aviso_id]
    assert (aviso["estado"], aviso["motivo"]) == ("fallido", "resultado_desconocido")
    assert aviso["cerrado_en"] is not None
    assert proveedor.llamadas == [] and bd.eventos == []
    assert gerencia == []  # un recordatorio no avisa


async def test_huerfano_de_cancelacion_avisa_a_gerencia(bd, proveedor, gerencia):
    muerta = bd.nueva_ejecucion(latido=AHORA - timedelta(minutes=10))
    bd.aviso_en_curso(ejecucion_id=muerta, tipo="cancelacion")

    await ac.recuperar_huerfanos(AHORA)

    assert len(gerencia) == 1
    assert gerencia[0][4]["motivo"] == "resultado_desconocido"


async def test_en_curso_de_ejecucion_viva_no_se_toca(bd, proveedor, gerencia):
    viva = bd.nueva_ejecucion(latido=AHORA - timedelta(minutes=4, seconds=59))
    aviso_id = bd.aviso_en_curso(ejecucion_id=viva)

    assert await ac.recuperar_huerfanos(AHORA) == []
    assert bd.avisos[aviso_id]["estado"] == "en_curso"
    assert gerencia == []


async def test_ejecucion_con_cinco_minutos_exactos_ya_esta_muerta(bd, proveedor, gerencia):
    limite = bd.nueva_ejecucion(latido=AHORA - timedelta(minutes=5))
    aviso_id = bd.aviso_en_curso(ejecucion_id=limite)

    assert await ac.recuperar_huerfanos(AHORA) == [aviso_id]


async def test_ejecucion_terminada_o_sin_ejecucion_tambien_deja_huerfano(bd, proveedor, gerencia):
    terminada = bd.nueva_ejecucion(latido=AHORA, terminada=AHORA)
    uno = bd.aviso_en_curso(ejecucion_id=terminada, tipo="recordatorio")
    dos = bd.aviso_en_curso(ejecucion_id=None, tipo="recordatorio")

    assert sorted(await ac.recuperar_huerfanos(AHORA)) == sorted([uno, dos])


async def test_los_registros_cerrados_no_se_vuelven_a_tocar(bd, proveedor, gerencia):
    muerta = bd.nueva_ejecucion(latido=AHORA - timedelta(hours=1))
    aviso_id = bd.aviso_en_curso(ejecucion_id=muerta, tipo="recordatorio")
    bd.avisos[aviso_id].update(estado="enviado")

    assert await ac.recuperar_huerfanos(AHORA) == []
    assert bd.avisos[aviso_id]["estado"] == "enviado"


async def test_si_la_recuperacion_se_adelanta_el_envio_tardio_no_pisa_ni_crea_evento(bd, proveedor, gerencia):
    """El proveedor tarda; entretanto la ejecución se da por muerta y el registro se cierra."""
    ejecucion = bd.nueva_ejecucion(latido=datetime.now(timezone.utc))

    async def _mientras_envia() -> None:
        bd.ejecuciones[ejecucion]["latido_en"] = datetime.now(timezone.utc) - timedelta(hours=1)
        await ac.recuperar_huerfanos(datetime.now(timezone.utc))

    proveedor.durante = _mientras_envia

    resultado = await ac.enviar_con_registro(**_envio(ejecucion_id=ejecucion))

    aviso = bd.avisos[next(iter(bd.avisos))]
    assert (aviso["estado"], aviso["motivo"]) == ("fallido", "resultado_desconocido")
    assert resultado.estado is None and not resultado.enviado
    assert bd.eventos == []


async def test_un_proceso_que_muere_dejando_en_curso_no_reenvia_en_la_siguiente_pasada(bd, proveedor):
    """CL-4: el en_curso huérfano pasa a fallido y la clave sigue ocupada."""
    muerta = bd.nueva_ejecucion(latido=AHORA - timedelta(minutes=30))
    reserva = uuid4()
    aviso_id = await ac.registrar_intento(
        tenant_id=TENANT, reserva_id=reserva, tipo="recordatorio", hora_inicio=INICIO,
        ejecucion_id=muerta)
    assert aviso_id is not None

    await ac.recuperar_huerfanos(AHORA)
    reintento = await ac.enviar_con_registro(**_envio(reserva_id=reserva, tipo="recordatorio"))

    assert reintento.estado is None
    assert proveedor.llamadas == []


# ------------------------------------------------------------
# Ejecuciones
# ------------------------------------------------------------
async def test_ciclo_de_vida_de_una_ejecucion(bd):
    ejecucion_id = await ac.crear_ejecucion("recordatorio")
    assert bd.ejecuciones[ejecucion_id]["terminada_en"] is None

    await ac.latir_ejecucion(ejecucion_id)
    await ac.terminar_ejecucion(ejecucion_id)

    assert bd.ejecuciones[ejecucion_id]["terminada_en"] is not None


# ------------------------------------------------------------
# Contexto del agente
# ------------------------------------------------------------
def test_etiquetas_del_evento():
    assert contexto_agente.evento_para_aviso("cancelacion") == "cita_cancelada_por_negocio"
    assert contexto_agente.evento_para_aviso("recordatorio") == "recordatorio_cita_enviado"
    with pytest.raises(ValueError):
        contexto_agente.evento_para_aviso("otro")


async def test_registrar_evento_es_idempotente_por_aviso(bd):
    datos = dict(tenant_id=TENANT, conversation_id=uuid4(), reserva_id=uuid4(),
                 aviso_id=uuid4(), tipo_aviso="cancelacion", hora_cita=INICIO, servicio=None)

    assert await contexto_agente.registrar_evento(**datos) is True
    assert await contexto_agente.registrar_evento(**datos) is True
    assert len(bd.eventos) == 1


async def test_registrar_evento_sin_conversacion_no_escribe(bd):
    creado = await contexto_agente.registrar_evento(
        tenant_id=TENANT, conversation_id=None, reserva_id=uuid4(), aviso_id=uuid4(),
        tipo_aviso="recordatorio", hora_cita=INICIO, servicio="Corte")

    assert creado is False and bd.eventos == []


async def test_conversacion_de_whatsapp_del_cliente(bd):
    usuario, conversacion = uuid4(), uuid4()
    bd.conversaciones.append({"tenant_id": TENANT, "user_id": usuario, "id": conversacion})

    assert await contexto_agente.conversacion_whatsapp_del_cliente(TENANT, usuario) == conversacion
    assert await contexto_agente.conversacion_whatsapp_del_cliente(TENANT, uuid4()) is None
    assert await contexto_agente.conversacion_whatsapp_del_cliente(TENANT, None) is None
    sql = next(c for c in bd.consultas if "FROM conversations" in c)
    assert "channel_type = 'whatsapp'" in sql and "ORDER BY last_message_at DESC" in sql


# ------------------------------------------------------------
# El error de NeuroAPI conserva el cuerpo (de ahí sale el código de Meta)
# ------------------------------------------------------------
async def test_el_error_de_neuroapi_lleva_el_cuerpo_de_la_respuesta(monkeypatch):
    from services import whatsapp

    cuerpo = '{"success": false, "error": {"code": "META_API_ERROR", "details": {"code": 132015}}}'

    class _Respuesta:
        status_code = 500
        text = cuerpo

    class _Cliente:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_Cliente":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]):
            return _Respuesta()

    monkeypatch.setattr(whatsapp.httpx, "AsyncClient", _Cliente)
    proveedor = whatsapp.NeuroApiProvider(
        api_key="k", webhook_secret="", base_url="https://neuro.test/api/v1", phone_number_id="P"
    )

    with pytest.raises(ProveedorWhatsAppError) as info:
        await proveedor.enviar_plantilla("+525512345678", "recordatorio_cita", "es_MX", ["A", "B"])

    assert info.value.status_code == 500
    assert plantillas_whatsapp.extraer_codigo_meta(info.value.cuerpo) == 132015
