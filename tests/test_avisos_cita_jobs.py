"""
Recordatorio automático de citas cada 5 minutos (spec 004, T7).

Sin BD ni red: las funciones de datos de `avisos_cita` se sustituyen por un
almacén en memoria que simula `ON CONFLICT DO NOTHING`, y el proveedor de
WhatsApp por un contador de llamadas. Lo que se prueba es lo que decide T7: qué
citas entran, qué motivo sale en cada caso, que dos pasadas solapadas envíen una
sola vez y que el job quede registrado con sus garantías.

La unicidad real en Postgres queda cubierta por `test_avisos_sql.py` y por A9.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from config import settings
from jobs import alertas_background, recordatorios_cita_background as job
from services import avisos_cita as ac
from services import avisos_cita_envio, contexto_agente
from services.avisos_cita_envio import ResultadoEnvioTenant

TENANT = uuid4()


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


class _Mundo:
    """Estado en memoria que sustituye a la BD y a los proveedores externos."""

    def __init__(self) -> None:
        self.reservas: dict[UUID, dict[str, Any]] = {}
        self.avisos: dict[UUID, dict[str, Any]] = {}
        self.envios: list[tuple[str, str, list[str]]] = []
        self.eventos: list[dict[str, Any]] = []
        self.gerencia: list[tuple[UUID, str]] = []
        self.conversacion: UUID | None = uuid4()
        self.plantilla_estado: str | None = "APPROVED"  # caché de la plantilla del tenant
        self.habilitado = True  # calendario activo y función encendida
        self.emisor_ok = True  # número principal activo coincidente (D6)
        self.fallar_envio = False
        self.fallar_candidatos_de: set[UUID] = set()
        self.ejecuciones: list[UUID] = []
        self.terminadas: list[UUID] = []
        self.huerfanos_llamados = 0

    def reserva(
        self,
        *,
        horas: float = 3,
        telefono: str | None = "55 1234 5678",
        canal: str | None = "whatsapp",
        estado: str = "confirmada",
        negocio: str = "Barbería Sol",
    ) -> UUID:
        reserva_id = uuid4()
        self.reservas[reserva_id] = {
            "reserva_id": reserva_id, "tenant_id": TENANT,
            "hora_inicio": _ahora() + timedelta(hours=horas), "estado": estado,
            "cliente_telefono": telefono, "user_id": uuid4(), "servicio_nombre": "Corte",
            "nombre_negocio": negocio, "channel_type": canal,
        }
        return reserva_id

    def del_tipo(self, tipo: str = "recordatorio") -> list[dict[str, Any]]:
        return [a for a in self.avisos.values() if a["tipo"] == tipo]

    def de(self, reserva_id: UUID) -> dict[str, Any]:
        [a] = [a for a in self.avisos.values() if a["reserva_id"] == reserva_id]
        return a


@pytest.fixture
def mundo(monkeypatch: pytest.MonkeyPatch) -> _Mundo:
    m = _Mundo()
    monkeypatch.setattr(settings, "AVISOS_CITA_WHATSAPP_ACTIVOS", True)

    async def crear_ejecucion(tipo: str) -> UUID:
        ejecucion = uuid4()
        m.ejecuciones.append(ejecucion)
        return ejecucion

    async def terminar_ejecucion(ejecucion_id: UUID) -> None:
        m.terminadas.append(ejecucion_id)

    async def latir(ejecucion_id: UUID) -> None:
        return None

    async def recuperar(ahora=None) -> list[UUID]:
        m.huerfanos_llamados += 1
        return []

    async def candidatos(ahora: datetime) -> list[dict[str, Any]]:
        await asyncio.sleep(0)  # deja que otra pasada lea lo mismo (solapamiento)
        con_aviso = {
            (a["reserva_id"], a["tipo"], a["hora_inicio"]) for a in m.avisos.values()
        }
        if not settings.AVISOS_CITA_WHATSAPP_ACTIVOS or not m.habilitado:
            return []  # el filtro SQL real (D7) deja fuera negocios sin función activa
        return [
            dict(r) for r in m.reservas.values()
            if r["estado"] == "confirmada"
            and ahora < r["hora_inicio"] <= ahora + timedelta(hours=5)
            and (r["reserva_id"], "recordatorio", r["hora_inicio"]) not in con_aviso
        ]

    async def datos_reserva(tenant_id: UUID, reserva_id: UUID):
        r = m.reservas.get(reserva_id)
        return dict(r) if r is not None else None

    async def registrar_intento(*, tenant_id, reserva_id, tipo, hora_inicio, ejecucion_id,
                                estado="en_curso", motivo=None, sin_conversacion=False):
        if reserva_id in m.fallar_candidatos_de:
            raise RuntimeError("BD caída para esta cita")
        await asyncio.sleep(0)
        if any(
            a["reserva_id"] == reserva_id and a["tipo"] == tipo and a["hora_inicio"] == hora_inicio
            for a in m.avisos.values()
        ):
            return None  # ON CONFLICT DO NOTHING
        aviso_id = uuid4()
        m.avisos[aviso_id] = {
            "id": aviso_id, "tenant_id": tenant_id, "reserva_id": reserva_id, "tipo": tipo,
            "hora_inicio": hora_inicio, "estado": estado, "motivo": motivo,
            "sin_conversacion": sin_conversacion, "telefono_destino": None,
        }
        return aviso_id

    async def cerrar_registro(aviso_id, estado, motivo=None, telefono_destino=None):
        a = m.avisos[aviso_id]
        if a["estado"] != "en_curso":
            return False
        a.update(estado=estado, motivo=motivo,
                 telefono_destino=telefono_destino if estado == "enviado" else None)
        return True

    async def enviar_plantilla(tenant_id: UUID, destinatario: str, plantilla: str,
                               parametros: list[dict[str, Any]], **kw: Any) -> ResultadoEnvioTenant:
        if m.fallar_envio:
            raise RuntimeError("proveedor caído")
        valores = [p["text"] for p in parametros[0]["parameters"]]  # solo cuerpo
        m.envios.append((destinatario, plantilla, valores))
        return ResultadoEnvioTenant(True, None, "wamid.1")

    async def registrar_evento(**kw: Any) -> bool:
        if kw["conversation_id"] is None:
            return False
        m.eventos.append(kw)
        return True

    async def avisar_gerencia_cancelacion(tenant_id: UUID, reserva_id: UUID, motivo: str) -> None:
        m.gerencia.append((reserva_id, motivo))

    async def conversacion(tenant_id: UUID, user_id: UUID | None):
        return m.conversacion

    async def estado_aviso(tenant_id: UUID, tipo: str, **kw: Any) -> ac.EstadoAviso:
        nombre = "recordatorio_cita" if tipo == "recordatorio" else "notificacion_cancelacion"
        return ac.EstadoAviso(m.habilitado, nombre, m.plantilla_estado, m.emisor_ok)

    async def servicios(tenant_id: UUID):
        return SimpleNamespace(zona_horaria="America/Mexico_City")

    monkeypatch.setattr(ac, "crear_ejecucion", crear_ejecucion)
    monkeypatch.setattr(ac, "terminar_ejecucion", terminar_ejecucion)
    monkeypatch.setattr(ac, "latir_ejecucion", latir)
    monkeypatch.setattr(ac, "recuperar_huerfanos", recuperar)
    monkeypatch.setattr(ac, "_candidatos_recordatorio", candidatos)
    monkeypatch.setattr(ac, "_datos_reserva_recordatorio", datos_reserva)
    monkeypatch.setattr(ac, "registrar_intento", registrar_intento)
    monkeypatch.setattr(ac, "cerrar_registro", cerrar_registro)
    monkeypatch.setattr(ac, "avisar_gerencia_cancelacion", avisar_gerencia_cancelacion)
    monkeypatch.setattr(ac, "get_tenant_servicios", servicios)
    monkeypatch.setattr(contexto_agente, "conversacion_whatsapp_del_cliente", conversacion)
    monkeypatch.setattr(contexto_agente, "registrar_evento", registrar_evento)
    monkeypatch.setattr(ac, "_estado_aviso", estado_aviso)
    monkeypatch.setattr(avisos_cita_envio, "enviar_plantilla_tenant", enviar_plantilla)
    return m


# ------------------------------------------------------------
# El job queda registrado (A15)
# ------------------------------------------------------------
def test_el_job_queda_registrado_con_sus_garantias(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(alertas_background.AsyncIOScheduler, "start", lambda self: None)
    scheduler = alertas_background.iniciar_scheduler()
    trabajo = scheduler.get_job(job.JOB_ID)
    assert trabajo is not None
    assert trabajo.func is job.job_recordatorios_cita
    assert trabajo.trigger.interval == timedelta(minutes=settings.AVISOS_CITA_REVISION_MINUTOS)
    assert settings.AVISOS_CITA_REVISION_MINUTOS == 5
    assert trabajo.max_instances == 1
    assert trabajo.coalesce is True


async def test_el_job_no_lanza_aunque_la_pasada_falle(monkeypatch: pytest.MonkeyPatch):
    async def explota(*a: Any, **k: Any):
        raise RuntimeError("todo roto")

    monkeypatch.setattr(ac, "procesar_recordatorios", explota)
    await job.job_recordatorios_cita()  # no debe propagar


# ------------------------------------------------------------
# Envío (A7, A8, A15)
# ------------------------------------------------------------
async def test_cita_en_ventana_con_telefono_valido_recibe_recordatorio(mundo):
    reserva_id = mundo.reserva()

    await ac.procesar_recordatorios()

    assert len(mundo.envios) == 1
    destinatario, plantilla, parametros = mundo.envios[0]
    assert destinatario == "+525512345678"
    assert plantilla == "recordatorio_cita"
    assert parametros[0] == "Barbería Sol" and len(parametros[1]) == 10
    aviso = mundo.de(reserva_id)
    assert aviso["estado"] == "enviado" and aviso["motivo"] is None
    assert aviso["telefono_destino"] == "+525512345678"
    assert len(mundo.eventos) == 1
    assert mundo.terminadas == mundo.ejecuciones and mundo.huerfanos_llamados == 1


async def test_cita_fuera_de_la_ventana_no_se_toca(mundo):
    mundo.reserva(horas=6)
    mundo.reserva(horas=-1)
    await ac.procesar_recordatorios()
    assert mundo.avisos == {} and mundo.envios == []


async def test_cita_creada_con_dos_horas_de_antelacion_recibe_recordatorio_en_la_siguiente_pasada(mundo):
    reserva_id = mundo.reserva(horas=2)
    await ac.procesar_recordatorios()
    assert mundo.de(reserva_id)["estado"] == "enviado"
    assert len(mundo.envios) == 1


async def test_la_misma_cita_no_se_recuerda_dos_veces_en_pasadas_sucesivas(mundo):
    mundo.reserva()
    await ac.procesar_recordatorios()
    await ac.procesar_recordatorios()
    assert len(mundo.envios) == 1 and len(mundo.del_tipo()) == 1


async def test_dos_pasadas_solapadas_envian_una_sola_vez(mundo):
    mundo.reserva()
    await asyncio.gather(ac.procesar_recordatorios(), ac.procesar_recordatorios())
    assert len(mundo.envios) == 1
    assert len(mundo.del_tipo()) == 1 and len(mundo.eventos) == 1


async def test_cita_sin_telefono_queda_omitida_sin_enviar_ni_avisar(mundo):
    reserva_id = mundo.reserva(telefono=None)
    await ac.procesar_recordatorios()
    aviso = mundo.de(reserva_id)
    assert (aviso["estado"], aviso["motivo"]) == ("omitido", "sin_telefono")
    assert mundo.envios == [] and mundo.gerencia == [] and mundo.eventos == []


async def test_telefono_invalido_queda_fallido_sin_enviar_ni_avisar(mundo):
    reserva_id = mundo.reserva(telefono="12345")
    await ac.procesar_recordatorios()
    aviso = mundo.de(reserva_id)
    assert (aviso["estado"], aviso["motivo"]) == ("fallido", "telefono_invalido")
    assert mundo.envios == [] and mundo.gerencia == []


async def test_instagram_con_telefono_valido_si_envia(mundo):
    reserva_id = mundo.reserva(canal="instagram")
    await ac.procesar_recordatorios()
    assert mundo.de(reserva_id)["estado"] == "enviado"
    assert len(mundo.envios) == 1


async def test_widget_queda_omitido_por_canal(mundo):
    reserva_id = mundo.reserva(canal="widget")
    await ac.procesar_recordatorios()
    aviso = mundo.de(reserva_id)
    assert (aviso["estado"], aviso["motivo"]) == ("omitido", "canal_no_soportado")
    assert mundo.envios == []


async def test_cliente_sin_conversacion_recibe_aviso_sin_evento(mundo):
    mundo.conversacion = None
    reserva_id = mundo.reserva(canal=None)
    await ac.procesar_recordatorios()
    aviso = mundo.de(reserva_id)
    assert aviso["estado"] == "enviado" and aviso["sin_conversacion"] is True
    assert mundo.eventos == []


# ------------------------------------------------------------
# Bandera y plantilla (A17, RF-17, RF-19)
# ------------------------------------------------------------
async def test_bandera_apagada_no_hay_candidatos_ni_registro_y_al_activarla_la_cita_en_ventana_si_recibe(
    mundo, monkeypatch: pytest.MonkeyPatch
):
    # D7: sin AVISOS_CITA_WHATSAPP_ACTIVOS ni se consulta ni se registra `omitido`.
    monkeypatch.setattr(settings, "AVISOS_CITA_WHATSAPP_ACTIVOS", False)
    reserva_id = mundo.reserva()
    await ac.procesar_recordatorios()
    assert mundo.avisos == {} and mundo.envios == []

    monkeypatch.setattr(settings, "AVISOS_CITA_WHATSAPP_ACTIVOS", True)
    await ac.procesar_recordatorios()
    assert mundo.de(reserva_id)["estado"] == "enviado" and len(mundo.envios) == 1  # RF-10


async def test_candidatos_reales_con_bandera_apagada_no_consultan_la_bd(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "AVISOS_CITA_WHATSAPP_ACTIVOS", False)

    async def no_debe_llamarse(*a: Any, **k: Any):
        raise AssertionError("no debe consultar la BD")

    monkeypatch.setattr(ac, "fetch_all", no_debe_llamarse)
    assert await ac._candidatos_recordatorio(_ahora()) == []


async def test_consulta_de_candidatos_filtra_en_sql_por_config_y_calendario(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "AVISOS_CITA_WHATSAPP_ACTIVOS", True)
    capturado: dict[str, Any] = {}

    async def falso(sql: str, *args: Any):
        capturado["sql"] = sql
        return []

    monkeypatch.setattr(ac, "fetch_all", falso)
    await ac._candidatos_recordatorio(_ahora())
    sql = " ".join(capturado["sql"].split())
    assert "JOIN avisos_cita_config cfg ON cfg.tenant_id = r.tenant_id AND cfg.recordatorio_activo" in sql
    assert "JOIN tenant_servicios ts ON ts.tenant_id = r.tenant_id AND ts.calendario_activo" in sql


async def test_plantilla_no_aprobada_es_final_sin_espera_ni_aviso(mundo):
    mundo.plantilla_estado = "REJECTED"
    reserva_id = mundo.reserva()
    await ac.procesar_recordatorios()
    aviso = mundo.de(reserva_id)
    assert (aviso["estado"], aviso["motivo"]) == ("omitido", "plantilla_no_aprobada")
    assert mundo.envios == [] and mundo.gerencia == []

    mundo.plantilla_estado = "APPROVED"
    await ac.procesar_recordatorios()
    assert mundo.envios == []  # sin reintento al aprobarse


async def test_recordatorio_sin_numero_emisor_queda_omitido_sin_alerta(mundo):
    mundo.emisor_ok = False
    reserva_id = mundo.reserva()
    await ac.procesar_recordatorios()
    aviso = mundo.de(reserva_id)
    assert (aviso["estado"], aviso["motivo"]) == ("omitido", "sin_numero_emisor")
    assert mundo.envios == [] and mundo.gerencia == []


async def test_plantilla_ausente_en_recordatorio_queda_omitida_sin_alerta(mundo):
    mundo.plantilla_estado = None
    reserva_id = mundo.reserva()
    await ac.procesar_recordatorios()
    aviso = mundo.de(reserva_id)
    assert (aviso["estado"], aviso["motivo"]) == ("omitido", "plantilla_no_aprobada")
    assert mundo.gerencia == []


async def test_funcion_apagada_no_envia_ni_registra(mundo):
    mundo.habilitado = False
    mundo.reserva()
    await ac.procesar_recordatorios()
    assert mundo.avisos == {} and mundo.envios == []

    assert mundo.ejecuciones  # la pasada sí corre; solo no hay candidatos
    mundo.habilitado = True  # al activarla, la cita que sigue en ventana sí lo recibe (RF-10)
    await ac.procesar_recordatorios()
    assert len(mundo.envios) == 1


async def test_plantilla_pending_espera_sin_registro_y_se_envia_al_aprobarse(mundo):
    mundo.plantilla_estado = "PENDING"
    mundo.reserva()
    await ac.procesar_recordatorios()
    assert mundo.avisos == {} and mundo.envios == [] and mundo.gerencia == []

    mundo.plantilla_estado = "APPROVED"
    await ac.procesar_recordatorios()
    assert len(mundo.envios) == 1


# ------------------------------------------------------------
# Revalidación (RF-11) y reagenda (CL-6)
# ------------------------------------------------------------
async def test_cita_cancelada_antes_de_enviar_queda_omitida_sin_enviar(
    mundo, monkeypatch: pytest.MonkeyPatch
):
    reserva_id = mundo.reserva()
    original = ac.registrar_intento

    async def registrar_y_cancelar(**kw: Any):
        aviso_id = await original(**kw)
        mundo.reservas[reserva_id]["estado"] = "cancelada"  # cancelan tras reservar el hueco
        return aviso_id

    monkeypatch.setattr(ac, "registrar_intento", registrar_y_cancelar)
    await ac.procesar_recordatorios()
    aviso = mundo.de(reserva_id)
    assert (aviso["estado"], aviso["motivo"]) == ("omitido", "cita_no_confirmada")
    assert mundo.envios == []


async def test_cita_que_ya_inicio_antes_de_enviar_queda_omitida(
    mundo, monkeypatch: pytest.MonkeyPatch
):
    reserva_id = mundo.reserva(horas=1)
    inicio = mundo.reservas[reserva_id]["hora_inicio"]
    llamadas = {"n": 0}

    def reloj() -> datetime:
        llamadas["n"] += 1
        # La primera lectura es la de la selección; la revalidación ya ve la cita iniciada.
        return _ahora() if llamadas["n"] == 1 else inicio + timedelta(minutes=1)

    monkeypatch.setattr(ac, "_ahora", reloj)
    await ac.procesar_recordatorios()
    aviso = mundo.de(reserva_id)
    assert (aviso["estado"], aviso["motivo"]) == ("omitido", "cita_ya_iniciada")
    assert mundo.envios == []


async def test_reagenda_con_hora_anterior_fuera_de_ventana_no_deja_registro(mundo):
    anterior = mundo.reserva(horas=10)
    await ac.procesar_recordatorios()
    assert mundo.avisos == {}

    # Reagendar cancela la reserva y crea otra con id nuevo.
    mundo.reservas[anterior]["estado"] = "cancelada"
    nueva = mundo.reserva(horas=3)
    await ac.procesar_recordatorios()

    assert [a["reserva_id"] for a in mundo.avisos.values()] == [nueva]
    assert mundo.de(nueva)["estado"] == "enviado" and len(mundo.envios) == 1


async def test_hora_cambiada_con_registro_ya_insertado_queda_omitida_sin_enviar(
    mundo, monkeypatch: pytest.MonkeyPatch
):
    reserva_id = mundo.reserva(horas=3)
    original = ac.registrar_intento

    async def registrar_y_mover(**kw: Any):
        aviso_id = await original(**kw)
        mundo.reservas[reserva_id]["hora_inicio"] += timedelta(hours=1)
        return aviso_id

    monkeypatch.setattr(ac, "registrar_intento", registrar_y_mover)
    await ac.procesar_recordatorios()
    aviso = mundo.de(reserva_id)
    assert (aviso["estado"], aviso["motivo"]) == ("omitido", "hora_cambiada")
    assert mundo.envios == []


# ------------------------------------------------------------
# Fallos (A18)
# ------------------------------------------------------------
async def test_recordatorio_fallido_no_genera_aviso_ni_se_reintenta(mundo):
    mundo.fallar_envio = True
    reserva_id = mundo.reserva()
    await ac.procesar_recordatorios()
    aviso = mundo.de(reserva_id)
    assert (aviso["estado"], aviso["motivo"]) == ("fallido", "error_proveedor")
    assert mundo.gerencia == [] and mundo.eventos == []

    mundo.fallar_envio = False
    await ac.procesar_recordatorios()
    assert mundo.envios == []


async def test_un_fallo_en_una_cita_no_frena_el_resto_de_la_pasada(mundo):
    mala = mundo.reserva(horas=1)
    buena = mundo.reserva(horas=2)
    mundo.fallar_candidatos_de.add(mala)

    await ac.procesar_recordatorios()

    assert mundo.de(buena)["estado"] == "enviado"
    assert len(mundo.envios) == 1
    assert mundo.terminadas == mundo.ejecuciones


async def test_fallo_al_armar_el_mensaje_cierra_el_registro_como_fallido(mundo):
    reserva_id = mundo.reserva(negocio="  ")
    await ac.procesar_recordatorios()
    aviso = mundo.de(reserva_id)
    assert (aviso["estado"], aviso["motivo"]) == ("fallido", "error_proveedor")
    assert mundo.envios == []


# ------------------------------------------------------------
# Recordar y luego cancelar: dos eventos en orden (A19)
# ------------------------------------------------------------
async def test_cancelar_tras_recordar_genera_los_dos_eventos_en_orden(
    mundo, monkeypatch: pytest.MonkeyPatch
):
    reserva_id = mundo.reserva()
    await ac.procesar_recordatorios()
    assert [e["tipo_aviso"] for e in mundo.eventos] == ["recordatorio"]

    mundo.reservas[reserva_id]["estado"] = "cancelada"

    async def datos_cancelacion(tenant_id: UUID, rid: UUID):
        return dict(mundo.reservas[rid])

    monkeypatch.setattr(ac, "_datos_cancelacion", datos_cancelacion)
    await ac.procesar_cancelacion(TENANT, reserva_id)

    assert [e["tipo_aviso"] for e in mundo.eventos] == ["recordatorio", "cancelacion"]
    assert [e[1] for e in mundo.envios] == ["recordatorio_cita", "notificacion_cancelacion"]
