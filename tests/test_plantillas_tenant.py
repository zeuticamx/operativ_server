"""
Plantillas de WhatsApp de cada negocio: reglas puras, cliente de NeuroAPI,
API (roles, aislamiento entre negocios, confirmación del borrado) y estado por
evento de Meta. Sin BD ni red: la caché y `desde_tenant` se sustituyen por
dobles en memoria y el HTTP de NeuroAPI por `httpx.MockTransport`.
"""

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from deps import UsuarioActual, usuario_actual
from main import app
from services import neuroapi_plantillas as np
from services import plantillas_reglas as reglas
from services import plantillas_tenant as cache

RUTA = "/api/plantillas-whatsapp"
TENANT = uuid4()
OTRO = uuid4()


# ------------------------------------------------------------
# Reglas puras
# ------------------------------------------------------------
@pytest.mark.parametrize("nombre", ["promo_verano", "a1", "x" * 512])
def test_nombre_valido(nombre):
    assert reglas.validar_nombre(nombre) == nombre


@pytest.mark.parametrize("nombre", ["", "Promo", "con espacio", "tilde_á", "x" * 513, "a-b"])
def test_nombre_invalido(nombre):
    with pytest.raises(reglas.PlantillaInvalida):
        reglas.validar_nombre(nombre)


def test_variables_en_orden_y_sin_repetidos():
    assert reglas.variables_de("{{2}} y {{1}} y {{2}}") == [2, 1]
    assert reglas.variables_de("sin variables") == []


def test_cuerpo_con_variables_arma_ejemplos():
    comps = reglas.construir_componentes(
        cuerpo="Hola {{1}}, tu cita es el {{2}}.", ejemplos_cuerpo=["Ana", "lunes"], pie="Gracias"
    )
    assert comps[0] == {
        "type": "BODY",
        "text": "Hola {{1}}, tu cita es el {{2}}.",
        "example": {"body_text": [["Ana", "lunes"]]},
    }
    assert comps[1] == {"type": "FOOTER", "text": "Gracias"}


@pytest.mark.parametrize(
    "cuerpo, ejemplos",
    [
        ("Hola {{1}} y {{3}}.", ["a", "b"]),  # salto
        ("{{1}} te saluda", ["a"]),  # empieza con variable
        ("Te saluda {{1}}", ["a"]),  # termina con variable
        ("Hola {{1}}, ok", []),  # sin ejemplo
        ("Hola {{1}}, ok", ["  "]),  # ejemplo vacío
        ("", []),  # cuerpo vacío
        ("x" * 1025, []),  # largo
    ],
)
def test_cuerpo_invalido(cuerpo, ejemplos):
    with pytest.raises(reglas.PlantillaInvalida):
        reglas.construir_componentes(cuerpo=cuerpo, ejemplos_cuerpo=ejemplos)


def test_pie_sin_variables_y_boton_url_https():
    with pytest.raises(reglas.PlantillaInvalida):
        reglas.construir_componentes(cuerpo="Hola", ejemplos_cuerpo=[], pie="Pie {{1}}")
    with pytest.raises(reglas.PlantillaInvalida):
        reglas.construir_componentes(
            cuerpo="Hola", ejemplos_cuerpo=[], botones=[{"tipo": "URL", "texto": "Ver", "url": "http://x.com"}]
        )


def test_parametros_de_envio_exige_todos_los_valores():
    comps = reglas.construir_componentes(cuerpo="Hola {{1}}, ok", ejemplos_cuerpo=["Ana"])
    assert reglas.parametros_de_envio(comps, ["Luis"]) == [
        {"type": "body", "parameters": [{"type": "text", "text": "Luis"}]}
    ]
    with pytest.raises(reglas.PlantillaInvalida):
        reglas.parametros_de_envio(comps, [])


# ------------------------------------------------------------
# Cliente de NeuroAPI
# ------------------------------------------------------------
def _servicio(monkeypatch, handler) -> np.NeuroApiTemplateService:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        np.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    return np.NeuroApiTemplateService(api_key="clave-secreta", base_url="https://api.test/api/v1")


async def test_listar_manda_filtros_y_clave(monkeypatch):
    visto: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        visto.update(url=str(req.url), clave=req.headers["x-api-key"])
        return httpx.Response(200, json={"success": True, "data": [{"name": "a"}, {"name": "b"}]})

    s = _servicio(monkeypatch, handler)
    r = await s.listar(categoria="UTILITY", estado="APPROVED", limite=50)
    assert [x["name"] for x in r] == ["a", "b"]
    assert visto["clave"] == "clave-secreta"
    assert visto["url"].startswith("https://api.test/api/v1/neuroapi/messaging/templates?")
    assert "category=UTILITY" in visto["url"] and "status=APPROVED" in visto["url"]


async def test_crear_envia_el_cuerpo_de_meta(monkeypatch):
    cuerpo: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        cuerpo.update(json.loads(req.content))
        return httpx.Response(200, json={"data": {"id": "123", "status": "PENDING"}})

    s = _servicio(monkeypatch, handler)
    comps = [{"type": "BODY", "text": "Hola"}]
    r = await s.crear(nombre="hola", idioma="es_MX", categoria="UTILITY", componentes=comps)
    assert r["id"] == "123"
    assert cuerpo == {"name": "hola", "language": "es_MX", "category": "UTILITY", "components": comps}


@pytest.mark.parametrize(
    "codigo, esperado",
    [(400, 422), (403, 502), (404, 404), (429, 429), (500, 502)],
)
async def test_errores_http_se_traducen(monkeypatch, codigo, esperado):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(codigo, json={"error": {"message": "variables mal formadas"}})

    s = _servicio(monkeypatch, handler)
    with pytest.raises(np.PlantillaApiError) as e:
        await s.eliminar("x")
    http = np.a_http(e.value)
    assert http.status_code == esperado
    if codigo == 400:
        assert http.detail == "variables mal formadas"
    if codigo == 403:
        assert http.detail["codigo"] == "scope_whatsapp_cloud_api_faltante"


async def test_fallo_de_red_es_502(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("caído")

    s = _servicio(monkeypatch, handler)
    with pytest.raises(np.PlantillaApiError) as e:
        await s.obtener("x")
    assert np.a_http(e.value).status_code == 502


# ------------------------------------------------------------
# Eventos de Meta
# ------------------------------------------------------------
def _evento(nombre="promo", event="APPROVED", **v):
    return {
        "entry": [
            {
                "id": "WABA1",
                "changes": [
                    {
                        "field": "message_template_status_update",
                        "value": {
                            "event": event,
                            "message_template_name": nombre,
                            "message_template_language": "es_MX",
                            "message_template_id": "555",
                            **v,
                        },
                    }
                ],
            }
        ]
    }


def test_cambios_de_estado_traduce_y_descarta_lo_desconocido():
    c = cache.cambios_de_estado(_evento(event="REJECTED", reason="INVALID_FORMAT"))
    assert c == [
        {"nombre": "promo", "idioma": "es_MX", "meta_id": "555", "waba_id": "WABA1",
         "estado": "REJECTED", "motivo": "INVALID_FORMAT"}
    ]
    assert cache.cambios_de_estado(_evento(event="FLAGGED")) == []
    assert cache.cambios_de_estado({"entry": "x"}) == []


def test_elegir_candidata_no_adivina_entre_homonimos():
    a = {"tenant_id": 1, "meta_template_id": None, "waba_id": None}
    b = {"tenant_id": 2, "meta_template_id": None, "waba_id": None}
    assert cache.elegir_candidata([a], None, None) is a
    assert cache.elegir_candidata([a, b], None, None) is None
    assert cache.elegir_candidata([a, {**b, "meta_template_id": "555"}], "555", None)["tenant_id"] == 2
    assert cache.elegir_candidata([a, {**b, "waba_id": "W"}], None, "W")["tenant_id"] == 2


async def test_aplicar_evento_actualiza_solo_a_su_dueno(monkeypatch):
    filas = [{"tenant_id": TENANT, "nombre": "promo", "idioma": "es_MX", "estado": "PENDING",
              "meta_template_id": None, "waba_id": None}]
    updates: list[tuple] = []

    async def fetch_all(sql, *a):
        return filas

    async def execute(sql, *a):
        updates.append(a)

    monkeypatch.setattr(cache, "fetch_all", fetch_all)
    monkeypatch.setattr(cache, "execute", execute)

    r = await cache.aplicar_evento_meta(_evento(event="APPROVED"))
    assert r == [(TENANT, "promo", "APPROVED")]
    assert updates[0][:5] == (TENANT, "promo", "es_MX", "APPROVED", None)

    # Mismo estado: idempotente, no escribe.
    filas[0]["estado"] = "APPROVED"
    updates.clear()
    assert await cache.aplicar_evento_meta(_evento(event="APPROVED")) == []
    assert updates == []

    # Plantilla que ningún negocio tiene (p. ej. las globales): se ignora.
    filas.clear()
    assert await cache.aplicar_evento_meta(_evento(nombre="recordatorio_cita")) == []


# ------------------------------------------------------------
# API
# ------------------------------------------------------------
class _Cache:
    def __init__(self) -> None:
        self.filas: dict[tuple[UUID, str], dict[str, Any]] = {}

    def instalar(self, mp: pytest.MonkeyPatch) -> None:
        c = self

        async def obtener(tenant_id, nombre):
            return c.filas.get((tenant_id, nombre))

        async def listar(tenant_id, *, categoria=None, estado=None):
            return [
                f for (t, _), f in c.filas.items()
                if t == tenant_id
                and (categoria is None or f["categoria"] == categoria)
                and (estado is None or f["estado"] == estado)
            ]

        async def guardar(tenant_id, datos, *, creada_por=None, sincronizada):
            from datetime import datetime, timezone

            c.filas[(tenant_id, datos["nombre"])] = {
                **datos, "motivo_rechazo": datos.get("motivo_rechazo"),
                "actualizada_en": datetime.now(timezone.utc), "sincronizada_en": None,
            }

        async def borrar_local(tenant_id, nombre):
            c.filas.pop((tenant_id, nombre), None)

        mp.setattr(cache, "obtener", obtener)
        mp.setattr(cache, "listar", listar)
        mp.setattr(cache, "guardar", guardar)
        mp.setattr(cache, "borrar_local", borrar_local)


class _Servicio:
    llamadas: list[tuple] = []
    fallo: np.PlantillaApiError | None = None

    @classmethod
    async def desde_tenant(cls, tenant_id):
        return cls()

    async def crear(self, **kw):
        if self.fallo:
            raise self.fallo
        self.llamadas.append(("crear", kw["nombre"]))
        return {"id": "9", "status": "PENDING", "category": kw["categoria"]}

    async def eliminar(self, nombre):
        self.llamadas.append(("eliminar", nombre))


def _usuario(role: str, tenant: UUID = TENANT) -> UsuarioActual:
    return UsuarioActual(id=uuid4(), tenant_id=tenant, email=f"{role}@n.com", role=role)


@pytest.fixture
def api(monkeypatch):
    c = _Cache()
    c.instalar(monkeypatch)
    _Servicio.llamadas = []
    _Servicio.fallo = None
    monkeypatch.setattr("routers.plantillas_whatsapp_tenant.NeuroApiTemplateService", _Servicio)
    actual = {"u": _usuario("owner")}

    async def _u() -> UsuarioActual:
        return actual["u"]

    app.dependency_overrides[usuario_actual] = _u
    c.actual = actual
    yield c
    app.dependency_overrides.pop(usuario_actual, None)


async def _llamar(metodo: str, ruta: str, **kw: Any):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(metodo, ruta, **kw)


NUEVA = {"nombre": "promo", "categoria": "UTILITY", "cuerpo": "Hola {{1}}, ok", "ejemplos_cuerpo": ["Ana"]}


@pytest.mark.parametrize("rol", ["vendedor", "proveedor"])
async def test_vendedor_y_proveedor_no_entran(api, rol):
    api.actual["u"] = _usuario(rol)
    assert (await _llamar("GET", RUTA)).status_code == 403
    assert (await _llamar("POST", RUTA, json=NUEVA)).status_code == 403


async def test_member_lee_pero_no_crea_ni_borra(api):
    api.actual["u"] = _usuario("member")
    assert (await _llamar("GET", RUTA)).status_code == 200
    assert (await _llamar("POST", RUTA, json=NUEVA)).status_code == 403
    assert (await _llamar("DELETE", f"{RUTA}/promo?confirmar=true")).status_code == 403


async def test_crear_guarda_pendiente_y_lista(api):
    r = await _llamar("POST", RUTA, json=NUEVA)
    assert r.status_code == 201, r.text
    assert r.json()["estado"] == "PENDING" and r.json()["variables_cuerpo"] == 1
    assert _Servicio.llamadas == [("crear", "promo")]
    lista = (await _llamar("GET", f"{RUTA}?estado=PENDING")).json()
    assert [p["nombre"] for p in lista] == ["promo"]
    assert (await _llamar("GET", f"{RUTA}?estado=APPROVED")).json() == []


async def test_crear_valida_antes_de_llamar_a_neuroapi(api):
    r = await _llamar("POST", RUTA, json={**NUEVA, "nombre": "Mal Nombre"})
    assert r.status_code == 422 and _Servicio.llamadas == []
    r = await _llamar("POST", RUTA, json={**NUEVA, "cuerpo": "Hola {{2}}, ok"})
    assert r.status_code == 422 and _Servicio.llamadas == []


async def test_crear_duplicada_es_409(api):
    assert (await _llamar("POST", RUTA, json=NUEVA)).status_code == 201
    assert (await _llamar("POST", RUTA, json=NUEVA)).status_code == 409


async def test_rechazo_de_meta_llega_como_422(api):
    _Servicio.fallo = np.PlantillaApiError("x", status_code=400, detalle_meta="variable mal formada")
    r = await _llamar("POST", RUTA, json=NUEVA)
    assert r.status_code == 422 and r.json()["detail"] == "variable mal formada"
    assert api.filas == {}


async def test_plantilla_de_otro_negocio_es_404(api):
    await _llamar("POST", RUTA, json=NUEVA)
    api.actual["u"] = _usuario("owner", OTRO)
    assert (await _llamar("GET", f"{RUTA}/promo")).status_code == 404
    assert (await _llamar("DELETE", f"{RUTA}/promo?confirmar=true")).status_code == 404
    assert (await _llamar("GET", RUTA)).json() == []


async def test_borrar_exige_confirmacion(api):
    await _llamar("POST", RUTA, json=NUEVA)
    r = await _llamar("DELETE", f"{RUTA}/promo")
    assert r.status_code == 400 and r.json()["detail"]["codigo"] == "confirmacion_requerida"
    assert ("eliminar", "promo") not in _Servicio.llamadas and (TENANT, "promo") in api.filas
    assert (await _llamar("DELETE", f"{RUTA}/promo?confirmar=true")).status_code == 204
    assert ("eliminar", "promo") in _Servicio.llamadas and (TENANT, "promo") not in api.filas


async def test_prueba_solo_de_plantillas_aprobadas(api):
    await _llamar("POST", RUTA, json=NUEVA)
    r = await _llamar("POST", f"{RUTA}/promo/prueba", json={"telefono": "593999999999", "valores_cuerpo": ["Ana"]})
    assert r.status_code == 409
