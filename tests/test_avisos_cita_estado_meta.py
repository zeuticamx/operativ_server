"""
Spec 007, T6: estado de las plantillas de Meta.

- El handler GLOBAL (`plantillas_whatsapp.aplicar_evento_meta`) ignora los eventos que no son
  de la cuenta global (hallazgo 1 del core-spec).
- Los eventos de plantillas de un negocio, el DELETE y la sincronización que quitan la fila
  pasan por `plantillas_tenant.reaccion_estado` (función off + alerta una sola vez).
Sin BD ni red: todo con dobles en memoria.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from config import settings
from deps import UsuarioActual, usuario_actual
from main import app
from services import plantillas_tenant as cache
from services import plantillas_whatsapp as pw

T = uuid4()
OTRO = uuid4()
WABA_GLOBAL = "WABA-GLOBAL"
WABA_TENANT = "WABA-TENANT"


def _evento(nombre: str, event: str, *, waba: str = WABA_TENANT, meta_id: str | None = "777") -> dict[str, Any]:
    valor: dict[str, Any] = {
        "event": event,
        "message_template_name": nombre,
        "message_template_language": "es_MX",
    }
    if meta_id is not None:
        valor["message_template_id"] = meta_id
    return {"entry": [{"id": waba, "changes": [{"field": "message_template_status_update", "value": valor}]}]}


class _Funciones:
    """`apagar` / `alertar` en memoria con la misma semántica atómica que `_apagar_funcion_bd`."""

    def __init__(self, recordatorio: bool = True, cancelacion: bool = True) -> None:
        self.config = {
            "recordatorio_activo": recordatorio,
            "cancelacion_activo": cancelacion,
            "nombre_plantilla_recordatorio": "recordatorio_cita",
            "nombre_plantilla_cancelacion": "notificacion_cancelacion",
        }
        self.alertas: list[tuple[str, str, str]] = []

    async def apagar(self, tenant_id: UUID, nombre: str) -> str | None:
        for tipo in ("recordatorio", "cancelacion"):
            if self.config[f"nombre_plantilla_{tipo}"] == nombre and self.config[f"{tipo}_activo"]:
                self.config[f"{tipo}_activo"] = False
                return tipo
        return None

    async def alertar(self, tenant_id: UUID, tipo: str, titulo: str, mensaje: str, datos: dict[str, Any]) -> None:
        self.alertas.append((tipo, titulo, mensaje))

    def instalar(self, mp: pytest.MonkeyPatch) -> None:
        mp.setattr(cache, "_apagar_funcion_bd", self.apagar)
        mp.setattr(cache, "_alertar_bd", self.alertar)


# ------------------------------------------------------------------ handler global (hallazgo 1)


def test_evento_es_de_cuenta_global_por_waba() -> None:
    ev = _evento("recordatorio_cita", "REJECTED", waba=WABA_GLOBAL)
    assert pw.evento_es_de_cuenta_global(ev, WABA_GLOBAL, frozenset()) is True
    assert pw.evento_es_de_cuenta_global(_evento("recordatorio_cita", "REJECTED"), WABA_GLOBAL, frozenset()) is False


def test_evento_es_de_cuenta_global_por_meta_id() -> None:
    ev = _evento("recordatorio_cita", "REJECTED", waba=WABA_TENANT, meta_id="42")
    assert pw.evento_es_de_cuenta_global(ev, None, {"42"}) is True
    assert pw.evento_es_de_cuenta_global(ev, WABA_GLOBAL, {"99"}) is False


def test_evento_es_de_cuenta_global_sin_datos_de_la_cuenta_es_falso() -> None:
    # Sin WABA ni ids configurados no se puede afirmar que sea global: se ignora (seguro).
    assert pw.evento_es_de_cuenta_global(_evento("recordatorio_cita", "APPROVED"), None, frozenset()) is False
    assert pw.evento_es_de_cuenta_global(_evento("recordatorio_cita", "APPROVED"), "", frozenset()) is False


@pytest.mark.parametrize("basura", [None, [], "x", {}, {"entry": None}, {"entry": [1, {"changes": "x"}]}])
def test_evento_es_de_cuenta_global_tolera_basura(basura: Any) -> None:
    assert pw.evento_es_de_cuenta_global(basura, WABA_GLOBAL, {"1"}) is False


@pytest.fixture
def global_(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    estado = {"estados": {n: "no_solicitada" for n in pw.NOMBRES_PLANTILLA}, "cambios": []}

    async def obtener_estado(nombre: str) -> str:
        return estado["estados"][nombre]

    async def cambiar_estado(nombre: str, nuevo: str, **kw: Any) -> str:
        anterior = estado["estados"][nombre]
        estado["estados"][nombre] = nuevo
        estado["cambios"].append((nombre, nuevo))
        return anterior

    monkeypatch.setattr(settings, "AVISOS_CUENTA_GLOBAL_WABA_ID", WABA_GLOBAL, raising=False)
    monkeypatch.setattr(pw, "obtener_estado", obtener_estado)
    monkeypatch.setattr(pw, "cambiar_estado", cambiar_estado)
    return estado


async def test_evento_de_tenant_con_mismo_nombre_no_cambia_estado_global(global_: dict[str, Any]) -> None:
    for nombre in pw.NOMBRES_PLANTILLA:
        r = await pw.aplicar_evento_meta(_evento(nombre, "REJECTED", waba=WABA_TENANT))
        assert r == []
    assert global_["cambios"] == []
    assert set(global_["estados"].values()) == {"no_solicitada"}


async def test_evento_de_la_cuenta_global_si_cambia_su_estado(global_: dict[str, Any]) -> None:
    r = await pw.aplicar_evento_meta(_evento("recordatorio_cita", "APPROVED", waba=WABA_GLOBAL))
    assert r == [("recordatorio_cita", "aprobada")]
    assert global_["estados"]["recordatorio_cita"] == "aprobada"


async def test_sobre_con_dos_wabas_solo_aplica_el_global(global_: dict[str, Any]) -> None:
    ev = _evento("recordatorio_cita", "PAUSED", waba=WABA_TENANT)
    ev["entry"].append(_evento("notificacion_cancelacion", "APPROVED", waba=WABA_GLOBAL)["entry"][0])
    await pw.aplicar_evento_meta(ev)
    assert global_["cambios"] == [("notificacion_cancelacion", "aprobada")]


async def test_sin_waba_global_configurado_no_toca_el_estado_global(
    global_: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "AVISOS_CUENTA_GLOBAL_WABA_ID", "", raising=False)
    assert await pw.aplicar_evento_meta(_evento("recordatorio_cita", "APPROVED", waba=WABA_GLOBAL)) == []
    assert global_["cambios"] == []


# ------------------------------------------------------------------ eventos de plantillas del negocio


class _CacheEventos:
    def __init__(self, estado: str = "PENDING", nombre: str = "recordatorio_cita") -> None:
        self.filas: list[dict[str, Any]] = [
            {"tenant_id": T, "nombre": nombre, "idioma": "es_MX", "estado": estado,
             "meta_template_id": "777", "waba_id": WABA_TENANT}
        ]

    def instalar(self, mp: pytest.MonkeyPatch) -> None:
        c = self

        async def fetch_all(sql: str, *a: Any) -> list[dict[str, Any]]:
            return [dict(f) for f in c.filas if f["nombre"] == a[0]]

        async def execute(sql: str, *a: Any) -> None:
            for f in c.filas:
                if (f["tenant_id"], f["nombre"], f["idioma"]) == a[:3]:
                    f["estado"] = a[3]

        mp.setattr(cache, "fetch_all", fetch_all)
        mp.setattr(cache, "execute", execute)


async def test_rejected_desactiva_y_alerta_una_sola_vez(monkeypatch: pytest.MonkeyPatch) -> None:
    f = _Funciones()
    _CacheEventos("PENDING").instalar(monkeypatch)
    ev = _evento("recordatorio_cita", "REJECTED")
    r1 = await cache.aplicar_evento_meta(ev, apagar=f.apagar, alertar=f.alertar)
    r2 = await cache.aplicar_evento_meta(ev, apagar=f.apagar, alertar=f.alertar)  # evento duplicado
    assert r1 == [(T, "recordatorio_cita", "REJECTED")] and r2 == []
    assert f.config["recordatorio_activo"] is False and f.config["cancelacion_activo"] is True
    assert [a[0] for a in f.alertas] == ["avisos_cita_plantilla_inutilizable"]
    assert "recordatorio" in f.alertas[0][2] and "REJECTED" in f.alertas[0][2]


@pytest.mark.parametrize("evento, estado", [("PAUSED", "PAUSED"), ("DISABLED", "DISABLED")])
async def test_paused_y_disabled_tambien_apagan(monkeypatch: pytest.MonkeyPatch, evento: str, estado: str) -> None:
    f = _Funciones()
    _CacheEventos("APPROVED", "notificacion_cancelacion").instalar(monkeypatch)
    await cache.aplicar_evento_meta(_evento("notificacion_cancelacion", evento), apagar=f.apagar, alertar=f.alertar)
    assert f.config["cancelacion_activo"] is False and f.config["recordatorio_activo"] is True
    assert estado in f.alertas[0][2] and "cancelación" in f.alertas[0][2]


async def test_evento_con_la_funcion_ya_apagada_no_alerta(monkeypatch: pytest.MonkeyPatch) -> None:
    f = _Funciones(recordatorio=False)
    _CacheEventos("APPROVED").instalar(monkeypatch)
    await cache.aplicar_evento_meta(_evento("recordatorio_cita", "REJECTED"), apagar=f.apagar, alertar=f.alertar)
    assert f.alertas == []


@pytest.mark.parametrize("benigno", ["APPROVED", "PENDING", "REINSTATED"])
async def test_evento_de_estado_benigno_no_apaga_nada(monkeypatch: pytest.MonkeyPatch, benigno: str) -> None:
    f = _Funciones()
    _CacheEventos("REJECTED").instalar(monkeypatch)
    await cache.aplicar_evento_meta(_evento("recordatorio_cita", benigno), apagar=f.apagar, alertar=f.alertar)
    assert f.config["recordatorio_activo"] is True and f.config["cancelacion_activo"] is True
    assert f.alertas == []


async def test_plantilla_que_no_es_la_vigente_de_una_funcion_no_apaga(monkeypatch: pytest.MonkeyPatch) -> None:
    f = _Funciones()
    _CacheEventos("APPROVED", "promo").instalar(monkeypatch)
    await cache.aplicar_evento_meta(_evento("promo", "REJECTED"), apagar=f.apagar, alertar=f.alertar)
    assert f.alertas == [] and f.config["recordatorio_activo"] is True


async def test_estado_ya_guardado_en_cache_igual_reacciona_sin_duplicar(monkeypatch: pytest.MonkeyPatch) -> None:
    # La caché ya decía REJECTED pero la función seguía activa (p. ej. se cayó entre ambos pasos).
    f = _Funciones()
    _CacheEventos("REJECTED").instalar(monkeypatch)
    ev = _evento("recordatorio_cita", "REJECTED")
    await cache.aplicar_evento_meta(ev, apagar=f.apagar, alertar=f.alertar)
    await cache.aplicar_evento_meta(ev, apagar=f.apagar, alertar=f.alertar)
    assert f.config["recordatorio_activo"] is False and len(f.alertas) == 1


# ------------------------------------------------------------------ DELETE y sincronización quitan la fila


async def test_quitar_borra_la_fila_apaga_y_alerta(monkeypatch: pytest.MonkeyPatch) -> None:
    f = _Funciones()
    borradas: list[str] = []

    async def borrar_local(tenant_id: UUID, nombre: str) -> None:
        borradas.append(nombre)

    monkeypatch.setattr(cache, "borrar_local", borrar_local)
    await cache.quitar(T, "recordatorio_cita", apagar=f.apagar, alertar=f.alertar)
    assert borradas == ["recordatorio_cita"]
    assert f.config["recordatorio_activo"] is False
    assert len(f.alertas) == 1 and "ya no está disponible" in f.alertas[0][2] and "eliminada" in f.alertas[0][2]


async def test_quitar_con_la_funcion_apagada_no_alerta(monkeypatch: pytest.MonkeyPatch) -> None:
    f = _Funciones(cancelacion=False)

    async def borrar_local(tenant_id: UUID, nombre: str) -> None:
        return None

    monkeypatch.setattr(cache, "borrar_local", borrar_local)
    await cache.quitar(T, "notificacion_cancelacion", apagar=f.apagar, alertar=f.alertar)
    assert f.alertas == []


async def test_sincronizar_completo_que_quita_filas_apaga_y_alerta(monkeypatch: pytest.MonkeyPatch) -> None:
    f = _Funciones()

    async def guardar(*a: Any, **k: Any) -> None:
        return None

    async def fetch_all(sql: str, *a: Any) -> list[dict[str, Any]]:
        assert "DELETE" in sql and "RETURNING" in sql
        return [{"nombre": "recordatorio_cita"}]

    monkeypatch.setattr(cache, "guardar", guardar)
    monkeypatch.setattr(cache, "fetch_all", fetch_all)
    n = await cache.sincronizar(T, [], lista_completa=True, apagar=f.apagar, alertar=f.alertar)
    assert n == 0
    assert f.config["recordatorio_activo"] is False and f.config["cancelacion_activo"] is True
    assert len(f.alertas) == 1 and "ya no está disponible" in f.alertas[0][2]


async def test_sincronizar_parcial_no_borra_ni_reacciona(monkeypatch: pytest.MonkeyPatch) -> None:
    f = _Funciones()

    async def guardar(*a: Any, **k: Any) -> None:
        return None

    async def fetch_all(sql: str, *a: Any) -> list[dict[str, Any]]:
        raise AssertionError("no debe borrar con lista parcial")

    monkeypatch.setattr(cache, "guardar", guardar)
    monkeypatch.setattr(cache, "fetch_all", fetch_all)
    await cache.sincronizar(T, [], lista_completa=False, apagar=f.apagar, alertar=f.alertar)
    assert f.alertas == []


# ------------------------------------------------------------------ por la API (cableado del router)


class _Servicio:
    remotas: list[dict[str, Any]] = []

    @classmethod
    async def desde_tenant(cls, tenant_id: UUID) -> "_Servicio":
        return cls()

    async def eliminar(self, nombre: str) -> None:
        return None

    async def listar(self, **kw: Any) -> list[dict[str, Any]]:
        return self.remotas


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    f = _Funciones()
    f.instalar(monkeypatch)
    filas: dict[str, dict[str, Any]] = {
        "recordatorio_cita": {"nombre": "recordatorio_cita"},
        "notificacion_cancelacion": {"nombre": "notificacion_cancelacion"},
    }

    async def obtener(tenant_id: UUID, nombre: str):
        return filas.get(nombre)

    async def borrar_local(tenant_id: UUID, nombre: str) -> None:
        filas.pop(nombre, None)

    async def listar(tenant_id: UUID, **kw: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(cache, "obtener", obtener)
    monkeypatch.setattr(cache, "borrar_local", borrar_local)
    monkeypatch.setattr(cache, "listar", listar)
    monkeypatch.setattr("routers.plantillas_whatsapp_tenant.NeuroApiTemplateService", _Servicio)

    # _servicio() mira bsp_provider antes de elegir entre NeuroAPI y Meta: sin
    # BD real en estos tests, se fija en 'neuroapi' (ver test_plantillas_tenant.py).
    async def _bsp_provider(*args: Any, **kwargs: Any) -> str:
        return "neuroapi"

    monkeypatch.setattr("routers.plantillas_whatsapp_tenant.fetch_value", _bsp_provider)

    async def _u() -> UsuarioActual:
        return UsuarioActual(id=uuid4(), tenant_id=T, email="o@n.com", role="owner")

    app.dependency_overrides[usuario_actual] = _u
    yield f, filas
    app.dependency_overrides.pop(usuario_actual, None)


async def test_delete_de_la_plantilla_vigente_apaga_la_funcion_y_alerta(api) -> None:
    f, filas = api
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.delete("/api/plantillas-whatsapp/recordatorio_cita?confirmar=true")
    assert r.status_code == 204
    assert "recordatorio_cita" not in filas
    assert f.config["recordatorio_activo"] is False and f.config["cancelacion_activo"] is True
    assert [a[0] for a in f.alertas] == ["avisos_cita_plantilla_inutilizable"]


async def test_delete_de_una_plantilla_cualquiera_no_alerta(api) -> None:
    f, filas = api
    filas["promo"] = {"nombre": "promo"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.delete("/api/plantillas-whatsapp/promo?confirmar=true")
    assert r.status_code == 204 and f.alertas == []


async def test_get_con_sincronizar_llega_a_cache_sincronizar_con_lista_completa(api, monkeypatch: pytest.MonkeyPatch) -> None:
    visto: dict[str, Any] = {}

    async def sincronizar(tenant_id: UUID, remotas: list, *, lista_completa: bool, **kw: Any) -> int:
        visto["lista_completa"] = lista_completa
        return 0

    monkeypatch.setattr(cache, "sincronizar", sincronizar)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/plantillas-whatsapp?sincronizar=true")
    assert r.status_code == 200 and visto == {"lista_completa": True}
