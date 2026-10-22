"""Reglas puras de los avisos de cita por tenant (spec 007, T1b). Sin base de datos ni red."""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from services import avisos_cita_plantillas as pl
from services import avisos_cita_reglas as r
from services.avisos_cita_reglas import EstadoTelefono, motivo_cancelacion, motivo_recordatorio

INICIO = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)


def test_migracion_65_ensancha_alertas_tipo_y_los_tipos_caben() -> None:
    # Guard: alertas.tipo era VARCHAR(30) y los tipos largos hacían fallar el INSERT.
    sql = (Path(__file__).resolve().parent.parent / "sql" / "65_avisos_cita_tenant.sql").read_text(encoding="utf-8")
    sin_comentarios = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    assert re.search(r"ALTER TABLE alertas ALTER COLUMN tipo TYPE VARCHAR\(60\)", sin_comentarios, re.I)
    assert sin_comentarios.index("TYPE VARCHAR(60)") < sin_comentarios.index("ADD CONSTRAINT alertas_tipo_check")
    bloque = sin_comentarios.split("ADD CONSTRAINT alertas_tipo_check", 1)[1]
    tipos = re.findall(r"'([a-z_]+)'", bloque)
    assert "avisos_cita_plantilla_inutilizable" in tipos
    assert all(len(t) <= 60 for t in tipos)


# ----------------------------------------------------------- textos y constantes

def test_textos_literales_de_la_spec() -> None:
    assert pl.cuerpo("cancelacion").startswith("Hola! Lamentamos informarte")
    assert pl.cuerpo("cancelacion").endswith("Muchas gracias.")
    assert "{{1}} del dia {{2}}" in pl.cuerpo("cancelacion")
    assert pl.cuerpo("recordatorio").startswith("hola! te escribe el asistente de {{1}}  en relacion")
    assert pl.cuerpo("recordatorio").endswith("Te esperamos!")  # doble espacio tras {{1}} intencional


def test_variables_y_ejemplos_coinciden_con_el_cuerpo() -> None:
    assert pl.variables("cancelacion") == ("servicio", "fecha")
    assert pl.variables("recordatorio") == ("nombre_negocio", "fecha")
    assert pl.ejemplos("cancelacion") == ["Corte de cabello", "20-10-2026"]
    assert pl.ejemplos("recordatorio") == ["Mi Negocio", "20-10-2026"]
    for tipo in pl.TIPOS:
        assert len(pl.ejemplos(tipo)) == len(pl.variables(tipo))
        assert pl.ejemplos(tipo)[1].count("-") == 2  # dd-mm-aaaa


def test_nombres_base() -> None:
    assert pl.nombre_base("cancelacion") == "notificacion_cancelacion"
    assert pl.nombre_base("recordatorio") == "recordatorio_cita"


def test_tipo_desconocido() -> None:
    with pytest.raises(ValueError):
        pl.cuerpo("otro")


# ----------------------------------------------------------------- siguiente_nombre

def test_siguiente_nombre_libre() -> None:
    assert pl.siguiente_nombre("recordatorio_cita", set()) == "recordatorio_cita"
    assert pl.siguiente_nombre("recordatorio_cita", {"otra"}) == "recordatorio_cita"


def test_siguiente_nombre_con_sufijo() -> None:
    assert pl.siguiente_nombre("recordatorio_cita", {"recordatorio_cita"}) == "recordatorio_cita_v2"
    usados = {"x", "x_v2", "x_v3"}
    assert pl.siguiente_nombre("x", usados) == "x_v4"


def test_siguiente_nombre_rellena_huecos() -> None:
    assert pl.siguiente_nombre("x", {"x", "x_v3"}) == "x_v2"


def test_siguiente_nombre_agotado_devuelve_none() -> None:
    usados = {"x"} | {f"x_v{n}" for n in range(2, 10)}
    assert pl.siguiente_nombre("x", usados) is None
    assert pl.siguiente_nombre("x", usados - {"x_v9"}) == "x_v9"


# ---------------------------------------------------------------------- adoptable

def _plantilla(
    *, idioma: str = "es_MX", categoria: str = "UTILITY", estado: str = "APPROVED", variables: int = 2
) -> dict[str, Any]:
    texto = " ".join(f"{{{{{n}}}}}" for n in range(1, variables + 1)) or "sin variables"
    return {
        "idioma": idioma,
        "categoria": categoria,
        "estado": estado,
        "componentes": [{"type": "BODY", "text": texto}],
    }


@pytest.mark.parametrize("tipo", ["cancelacion", "recordatorio"])
def test_adoptable_si_coincide(tipo: str) -> None:
    assert pl.adoptable(_plantilla(), tipo) is True
    assert pl.adoptable(_plantilla(categoria="MARKETING"), tipo) is True


@pytest.mark.parametrize(
    "cambio",
    [
        {"idioma": "en_US"},
        {"categoria": "AUTHENTICATION"},
        {"variables": 1},
        {"variables": 3},
        {"variables": 0},
    ],
)
def test_no_adoptable(cambio: dict[str, Any]) -> None:
    assert pl.adoptable(_plantilla(**cambio), "cancelacion") is False


def test_adoptable_sin_componentes() -> None:
    assert pl.adoptable({"idioma": "es_MX", "categoria": "UTILITY"}, "cancelacion") is False


@pytest.mark.parametrize("estado, esperado", [
    ("APPROVED", True), ("PENDING", True),
    ("REJECTED", False), ("PAUSED", False), ("DISABLED", False),
])
def test_debe_adoptar_segun_estado(estado: str, esperado: bool) -> None:
    assert pl.debe_adoptar(_plantilla(estado=estado), "recordatorio") is esperado


def test_debe_adoptar_no_adoptable_aunque_aprobada() -> None:
    assert pl.debe_adoptar(_plantilla(variables=1), "recordatorio") is False


# --------------------------------------------------- motivo_cancelacion (estado)

def _canc(**cambios: object) -> str | None:
    base: dict[str, object] = dict(
        telefono=EstadoTelefono.VALIDO, canal_ok=True, activo=True,
        plantilla_estado="APPROVED", emisor_ok=True,
    )
    base.update(cambios)
    return motivo_cancelacion(**base)  # type: ignore[arg-type]


def test_cancelacion_aprobada_con_emisor_se_envia() -> None:
    assert _canc() is None


def test_cancelacion_pending_espera_sin_registrar() -> None:
    motivo = _canc(plantilla_estado="PENDING")
    assert motivo == r.ESPERANDO_APROBACION
    assert r.se_registra(motivo) is False
    assert r.alerta_gerencia_cancelacion(motivo) is False


@pytest.mark.parametrize("estado", ["REJECTED", "PAUSED", "DISABLED", None, "NO_CREADA", ""])
def test_cancelacion_plantilla_no_aprobada(estado: str | None) -> None:
    motivo = _canc(plantilla_estado=estado)
    assert motivo == r.PLANTILLA_NO_APROBADA
    assert r.se_registra(motivo) is True
    assert r.alerta_gerencia_cancelacion(motivo) is True


def test_cancelacion_sin_emisor() -> None:
    motivo = _canc(emisor_ok=False)
    assert motivo == r.SIN_NUMERO_EMISOR
    assert r.se_registra(motivo) is True
    assert r.alerta_gerencia_cancelacion(motivo) is True


def test_sin_emisor_tiene_prioridad_sobre_plantilla() -> None:
    assert _canc(emisor_ok=False, plantilla_estado="PENDING") == r.SIN_NUMERO_EMISOR
    assert _canc(emisor_ok=False, plantilla_estado="REJECTED") == r.SIN_NUMERO_EMISOR


def test_cancelacion_orden_previo_se_conserva() -> None:
    peor = dict(
        telefono=EstadoTelefono.VACIO, canal_ok=False, activo=False,
        plantilla_estado="REJECTED", emisor_ok=False,
    )
    assert _canc(**peor) == r.SIN_TELEFONO
    peor["telefono"] = EstadoTelefono.NO_NORMALIZABLE
    assert _canc(**peor) == r.TELEFONO_INVALIDO
    peor["telefono"] = EstadoTelefono.VALIDO
    assert _canc(**peor) == r.CANAL_NO_SOPORTADO
    peor["canal_ok"] = True
    assert _canc(**peor) == r.DESACTIVADO
    peor["activo"] = True
    assert _canc(**peor) == r.SIN_NUMERO_EMISOR
    peor["emisor_ok"] = True
    assert _canc(**peor) == r.PLANTILLA_NO_APROBADA


# -------------------------------------------------- motivo_recordatorio (estado)

def _rec(**cambios: object) -> str | None:
    base: dict[str, object] = dict(
        telefono=EstadoTelefono.VALIDO, canal_ok=True, estado_cita="confirmada",
        hora_coincide=True, inicio_llegado=False, activo=True,
        plantilla_estado="APPROVED", emisor_ok=True,
    )
    base.update(cambios)
    return motivo_recordatorio(**base)  # type: ignore[arg-type]


def test_recordatorio_ok() -> None:
    assert _rec() is None


def test_recordatorio_pending_espera_sin_registrar() -> None:
    motivo = _rec(plantilla_estado="PENDING")
    assert motivo == r.ESPERANDO_APROBACION
    assert r.se_registra(motivo) is False


def test_recordatorio_rechazada_se_registra_sin_alerta() -> None:
    motivo = _rec(plantilla_estado="REJECTED")
    assert motivo == r.PLANTILLA_NO_APROBADA
    assert r.se_registra(motivo) is True
    assert r.alerta_gerencia_cancelacion(motivo) is True  # la regla es por motivo; el llamador solo la usa en cancelación


def test_recordatorio_sin_emisor_antes_que_plantilla() -> None:
    assert _rec(emisor_ok=False, plantilla_estado="PENDING") == r.SIN_NUMERO_EMISOR


def test_recordatorio_orden_rf11_se_conserva() -> None:
    assert _rec(estado_cita="cancelada", hora_coincide=False, activo=False, emisor_ok=False) == r.CITA_NO_CONFIRMADA
    assert _rec(hora_coincide=False, inicio_llegado=True, activo=False) == r.HORA_CAMBIADA
    assert _rec(inicio_llegado=True, activo=False) == r.CITA_YA_INICIADA
    assert _rec(activo=False, emisor_ok=False) == r.DESACTIVADO


def test_estado_en_minusculas_o_con_espacios() -> None:
    assert _canc(plantilla_estado=" approved ") is None


def test_sin_estado_ni_bool_es_plantilla_ausente() -> None:
    assert motivo_cancelacion(
        telefono=EstadoTelefono.VALIDO, canal_ok=True, activo=True
    ) == r.PLANTILLA_NO_APROBADA


# ------------------------------------------------------------ parametros_de_envio

def test_parametros_cancelacion() -> None:
    assert r.parametros_de_envio("cancelacion", "Mi Negocio", "Corte", "20-10-2026") == [
        {"type": "body", "parameters": [
            {"type": "text", "text": "Corte"}, {"type": "text", "text": "20-10-2026"},
        ]}
    ]


def test_parametros_cancelacion_sin_servicio_usa_respaldo() -> None:
    p = r.parametros_de_envio("cancelacion", "Mi Negocio", None, "20-10-2026")
    assert p[0]["parameters"][0]["text"] == r.SERVICIO_RESPALDO
    p = r.parametros_de_envio("cancelacion", "Mi Negocio", "  ", "20-10-2026")
    assert p[0]["parameters"][0]["text"] == r.SERVICIO_RESPALDO


def test_parametros_recordatorio() -> None:
    p = r.parametros_de_envio("recordatorio", " Mi Negocio ", "Corte", "20-10-2026")
    assert [x["text"] for x in p[0]["parameters"]] == ["Mi Negocio", "20-10-2026"]


def test_parametros_recordatorio_sin_negocio_falla() -> None:
    with pytest.raises(ValueError):
        r.parametros_de_envio("recordatorio", "  ", "Corte", "20-10-2026")


def test_parametros_fecha_vacia_falla() -> None:
    with pytest.raises(ValueError):
        r.parametros_de_envio("cancelacion", "N", "Corte", " ")


def test_parametros_tipo_desconocido() -> None:
    with pytest.raises(ValueError):
        r.parametros_de_envio("otro", "N", "Corte", "20-10-2026")


def test_parametros_coinciden_con_variables_existentes() -> None:
    fecha = r.fecha_mostrada(INICIO, "America/Mexico_City")
    p = r.parametros_de_envio("cancelacion", "N", "Corte", fecha)
    assert [x["text"] for x in p[0]["parameters"]] == r.variables_cancelacion("Corte", INICIO, "America/Mexico_City")


# =====================================================================
# T2 · Servicio de config (spec 007): sin red ni base de datos.
# Capa de acceso y proveedor de Meta falsos, inyectados en cada función.
# =====================================================================

import asyncio
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import HTTPException

from services import avisos_cita_config as cfg
from services import neuroapi_plantillas as neuro
from services import plantillas_tenant as cache_tenant
from services import plantillas_reglas as plantillas_reglas
from services.neuroapi_plantillas import PlantillaApiError

T = uuid4()
USUARIO = uuid4()
COMP_CANC = plantillas_reglas.construir_componentes(
    cuerpo=pl.cuerpo("cancelacion"), ejemplos_cuerpo=pl.ejemplos("cancelacion")
)
COMP_RECO = plantillas_reglas.construir_componentes(
    cuerpo=pl.cuerpo("recordatorio"), ejemplos_cuerpo=pl.ejemplos("recordatorio")
)


def _fila(nombre: str, estado: str = "APPROVED", tipo: str = "cancelacion", **extra: Any) -> dict[str, Any]:
    return {
        "nombre": nombre,
        "idioma": "es_MX",
        "categoria": "UTILITY",
        "estado": estado,
        "motivo_rechazo": None,
        "componentes": COMP_CANC if tipo == "cancelacion" else COMP_RECO,
        "meta_template_id": None,
        **extra,
    }


class ServicioFalso:
    """Meta falsa: `crear_o_adoptar` y `obtener`. `fallar_en` = nombres cuya creación lanza."""

    def __init__(self) -> None:
        self.creadas: list[str] = []
        self.duplicados: dict[str, dict[str, Any] | None] = {}  # nombre -> respuesta remota existente
        self.remotas: dict[str, dict[str, Any] | None] = {}  # para obtener()
        self.fallar_en: set[str] = set()
        self.error_red = False
        self.consultas: list[str] = []

    async def crear_o_adoptar(
        self, *, nombre: str, idioma: str, categoria: str, componentes: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], bool]:
        await asyncio.sleep(0)
        if nombre in self.fallar_en:
            raise PlantillaApiError("NeuroAPI respondió 500", status_code=500)
        if nombre in self.duplicados:
            return (self.duplicados[nombre] or {}), False
        self.creadas.append(nombre)
        return {"id": f"meta-{nombre}", "status": "PENDING", "category": categoria}, True

    async def obtener(self, nombre: str) -> dict[str, Any] | None:
        self.consultas.append(nombre)
        if self.error_red:
            raise PlantillaApiError("No se pudo contactar a NeuroAPI: ConnectError")
        return self.remotas.get(nombre)


class AccesoFalso:
    def __init__(self, *, calendario: bool = True, emisor: bool = True) -> None:
        self.calendario = calendario
        self.emisor = emisor
        self.config: dict[str, Any] | None = None
        self.cache: dict[str, dict[str, Any]] = {}
        self.alertas: list[tuple[str, str, str]] = []
        self.servicio = ServicioFalso()
        self.bloqueos = 0
        self._lock = asyncio.Lock()

    async def calendario_activo(self, tenant_id: UUID) -> bool:
        return self.calendario

    async def emisor_ok(self, tenant_id: UUID) -> bool:
        return self.emisor

    async def leer_config(self, tenant_id: UUID) -> dict[str, Any] | None:
        return dict(self.config) if self.config else None

    async def guardar_config(self, tenant_id: UUID, campos: dict[str, Any]) -> None:
        base = self.config or {
            "oferta": "pendiente",
            "recordatorio_activo": False,
            "cancelacion_activo": False,
            "nombre_plantilla_recordatorio": "recordatorio_cita",
            "nombre_plantilla_cancelacion": "notificacion_cancelacion",
        }
        self.config = {**base, **campos}

    async def plantilla(self, tenant_id: UUID, nombre: str) -> dict[str, Any] | None:
        return self.cache.get(nombre)

    async def nombres_plantillas(self, tenant_id: UUID) -> set[str]:
        return set(self.cache)

    async def guardar_plantilla(
        self, tenant_id: UUID, datos: dict[str, Any], creada_por: UUID | None
    ) -> None:
        self.cache[datos["nombre"]] = dict(datos)

    async def borrar_plantilla(self, tenant_id: UUID, nombre: str) -> None:
        self.cache.pop(nombre, None)

    async def servicio_meta(self, tenant_id: UUID) -> ServicioFalso:
        return self.servicio

    @asynccontextmanager
    async def bloqueo(self, tenant_id: UUID):
        async with self._lock:
            self.bloqueos += 1
            yield

    async def apagar_funcion(self, tenant_id: UUID, nombre: str) -> str | None:
        c = self.config
        if not c:
            return None
        for tipo in ("recordatorio", "cancelacion"):
            if c[f"nombre_plantilla_{tipo}"] == nombre and c[f"{tipo}_activo"]:
                c[f"{tipo}_activo"] = False
                return tipo
        return None

    async def alertar(self, tenant_id: UUID, tipo: str, titulo: str, mensaje: str, datos: dict[str, Any]) -> None:
        self.alertas.append((tipo, titulo, mensaje))


def _acceso(**kw: Any) -> AccesoFalso:
    return AccesoFalso(**kw)


# ---------------------------------------------------------- aceptar: caso feliz

async def test_config_aceptar_pendiente_crea_ambas_en_orden_y_activa() -> None:
    a = _acceso()
    r_ = await cfg.aceptar(T, USUARIO, acceso=a)

    assert a.servicio.creadas == ["notificacion_cancelacion", "recordatorio_cita"]  # cancelación primero
    assert a.config is not None
    assert a.config["oferta"] == "aceptada"
    assert a.config["cancelacion_activo"] and a.config["recordatorio_activo"]
    assert a.config["decidido_por"] == USUARIO
    assert set(a.cache) == {"notificacion_cancelacion", "recordatorio_cita"}
    assert a.cache["recordatorio_cita"]["estado"] == "PENDING"  # aun PENDING, activa igualmente
    assert a.cache["recordatorio_cita"]["componentes"] == COMP_RECO
    assert a.bloqueos == 1
    assert r_["oferta"] == "aceptada" and r_["mensaje"]


async def test_config_aceptar_calendario_inactivo_409() -> None:
    a = _acceso(calendario=False)
    with pytest.raises(cfg.AvisosError) as ei:
        await cfg.aceptar(T, USUARIO, acceso=a)
    assert (ei.value.status_code, ei.value.codigo) == (409, "calendario_inactivo")
    assert a.servicio.creadas == [] and a.config is None


async def test_config_aceptar_sin_numero_emisor_409() -> None:
    a = _acceso(emisor=False)
    with pytest.raises(cfg.AvisosError) as ei:
        await cfg.aceptar(T, USUARIO, acceso=a)
    assert (ei.value.status_code, ei.value.codigo) == (409, "sin_numero_emisor")
    assert a.servicio.creadas == [] and a.config is None


async def test_config_error_a_http() -> None:
    e = cfg.AvisosError(409, "sin_numero_emisor", "x")
    h = cfg.a_http(e)
    assert isinstance(h, HTTPException) and h.status_code == 409
    assert h.detail["codigo"] == "sin_numero_emisor"


# ------------------------------------------------------------------ adopción

async def test_config_aceptar_adopta_existente_aprobada() -> None:
    a = _acceso()
    a.cache["notificacion_cancelacion"] = _fila("notificacion_cancelacion", "APPROVED")
    a.cache["recordatorio_cita"] = _fila("recordatorio_cita", "PENDING", "recordatorio")
    await cfg.aceptar(T, USUARIO, acceso=a)
    assert a.servicio.creadas == []  # nada que crear
    assert a.config["nombre_plantilla_cancelacion"] == "notificacion_cancelacion"
    assert a.config["cancelacion_activo"] and a.config["recordatorio_activo"]


async def test_config_aceptar_rechazada_crea_con_sufijo() -> None:
    a = _acceso()
    a.cache["notificacion_cancelacion"] = _fila("notificacion_cancelacion", "REJECTED")
    await cfg.aceptar(T, USUARIO, acceso=a)
    assert "notificacion_cancelacion_v2" in a.servicio.creadas
    assert a.config["nombre_plantilla_cancelacion"] == "notificacion_cancelacion_v2"
    assert a.config["nombre_plantilla_recordatorio"] == "recordatorio_cita"


async def test_config_aceptar_no_adoptable_crea_con_sufijo() -> None:
    a = _acceso()
    a.cache["recordatorio_cita"] = _fila("recordatorio_cita", "APPROVED", "recordatorio", idioma="en_US")
    await cfg.aceptar(T, USUARIO, acceso=a)
    assert a.config["nombre_plantilla_recordatorio"] == "recordatorio_cita_v2"


async def test_config_aceptar_variables_distintas_no_se_adopta() -> None:
    a = _acceso()
    a.cache["notificacion_cancelacion"] = _fila(
        "notificacion_cancelacion", "APPROVED", "recordatorio", componentes=[{"type": "BODY", "text": "hola"}]
    )
    await cfg.aceptar(T, USUARIO, acceso=a)
    assert a.config["nombre_plantilla_cancelacion"] == "notificacion_cancelacion_v2"


async def test_config_aceptar_duplicado_en_meta_se_adopta_y_guarda_en_cache() -> None:
    a = _acceso()
    a.servicio.duplicados["recordatorio_cita"] = {
        "name": "recordatorio_cita", "language": "es_MX", "category": "UTILITY",
        "status": "APPROVED", "components": COMP_RECO, "id": "9",
    }
    await cfg.aceptar(T, USUARIO, acceso=a)
    assert a.config["nombre_plantilla_recordatorio"] == "recordatorio_cita"
    assert a.cache["recordatorio_cita"]["estado"] == "APPROVED"
    assert a.cache["recordatorio_cita"]["meta_template_id"] == "9"
    assert "recordatorio_cita" not in a.servicio.creadas


async def test_config_aceptar_duplicado_rechazado_en_meta_usa_sufijo() -> None:
    a = _acceso()
    a.servicio.duplicados["recordatorio_cita"] = {
        "name": "recordatorio_cita", "language": "es_MX", "category": "UTILITY",
        "status": "REJECTED", "components": COMP_RECO,
    }
    await cfg.aceptar(T, USUARIO, acceso=a)
    assert a.config["nombre_plantilla_recordatorio"] == "recordatorio_cita_v2"
    assert "recordatorio_cita_v2" in a.servicio.creadas


async def test_config_aceptar_duplicado_sin_remota_se_salta_el_nombre() -> None:
    a = _acceso()
    a.servicio.duplicados["recordatorio_cita"] = None  # Meta dice que existe pero no la devuelve
    await cfg.aceptar(T, USUARIO, acceso=a)
    assert a.config["nombre_plantilla_recordatorio"] == "recordatorio_cita_v2"


async def test_config_aceptar_sufijo_agotado_409() -> None:
    a = _acceso()
    base = "notificacion_cancelacion"
    for n in [base] + [f"{base}_v{i}" for i in range(2, 10)]:
        a.cache[n] = _fila(n, "REJECTED")
    with pytest.raises(cfg.AvisosError) as ei:
        await cfg.aceptar(T, USUARIO, acceso=a)
    assert (ei.value.status_code, ei.value.codigo) == (409, "plantilla_existente_no_utilizable")
    assert a.servicio.creadas == []


# ------------------------------------------------ fallo de la 2ª creación (idempotencia)

async def test_config_fallo_segunda_creacion_deja_primera_y_reintento_crea_solo_la_faltante() -> None:
    a = _acceso()
    a.servicio.fallar_en.add("recordatorio_cita")
    with pytest.raises(PlantillaApiError):  # el router lo traduce con a_http (502)
        await cfg.aceptar(T, USUARIO, acceso=a)

    assert a.servicio.creadas == ["notificacion_cancelacion"]
    assert "notificacion_cancelacion" in a.cache  # la 1ª queda en cache
    assert a.config["oferta"] == "aceptada"
    assert a.config["cancelacion_activo"] is True
    assert a.config["recordatorio_activo"] is False  # la fallida, apagada

    a.servicio.fallar_en.clear()
    r_ = await cfg.aceptar(T, USUARIO, acceso=a)  # reintento
    assert a.servicio.creadas == ["notificacion_cancelacion", "recordatorio_cita"]  # sin recrear la 1ª
    assert a.config["recordatorio_activo"] and a.config["cancelacion_activo"]
    assert r_["oferta"] == "aceptada"


async def test_config_fallo_primera_creacion_no_cambia_la_oferta() -> None:
    a = _acceso()
    a.servicio.fallar_en.add("notificacion_cancelacion")
    with pytest.raises(PlantillaApiError):
        await cfg.aceptar(T, USUARIO, acceso=a)
    assert a.servicio.creadas == []
    assert a.config is None  # sigue pendiente (fila ausente): el modal vuelve a ofrecerse


async def test_config_dos_aceptar_concurrentes_crean_una_sola_vez() -> None:
    a = _acceso()
    await asyncio.gather(cfg.aceptar(T, USUARIO, acceso=a), cfg.aceptar(T, USUARIO, acceso=a))
    assert sorted(a.servicio.creadas) == ["notificacion_cancelacion", "recordatorio_cita"]
    assert a.bloqueos == 2


# ------------------------------------------------------- tabla de transiciones

async def test_config_rechazar_pendiente_apaga_alerta_y_confirma() -> None:
    a = _acceso()
    r_ = await cfg.rechazar(T, USUARIO, acceso=a)
    assert a.config["oferta"] == "rechazada"
    assert not a.config["recordatorio_activo"] and not a.config["cancelacion_activo"]
    assert [x[0] for x in a.alertas] == ["avisos_cita_oferta_rechazada"]
    assert a.alertas[0][2] == cfg.MENSAJE_OFERTA_RECHAZADA
    assert r_["mensaje"] == cfg.MENSAJE_OFERTA_RECHAZADA
    assert a.servicio.creadas == []


async def test_config_rechazar_aceptada_solo_apaga_no_borra_plantillas() -> None:
    a = _acceso()
    await cfg.aceptar(T, USUARIO, acceso=a)
    await cfg.rechazar(T, USUARIO, acceso=a)
    assert a.config["oferta"] == "rechazada"
    assert not a.config["recordatorio_activo"] and not a.config["cancelacion_activo"]
    assert len(a.cache) == 2  # no se borran
    assert [x[0] for x in a.alertas] == ["avisos_cita_oferta_rechazada"]


async def test_config_rechazar_rechazada_es_idempotente_sin_alerta_repetida() -> None:
    a = _acceso()
    await cfg.rechazar(T, USUARIO, acceso=a)
    r_ = await cfg.rechazar(T, USUARIO, acceso=a)
    assert len(a.alertas) == 1
    assert r_["oferta"] == "rechazada"


async def test_config_aceptar_desde_rechazada_reabre() -> None:
    a = _acceso()
    await cfg.rechazar(T, USUARIO, acceso=a)
    await cfg.aceptar(T, USUARIO, acceso=a)
    assert a.config["oferta"] == "aceptada"
    assert a.config["cancelacion_activo"] and a.config["recordatorio_activo"]


async def test_config_aceptar_aceptada_es_idempotente() -> None:
    a = _acceso()
    await cfg.aceptar(T, USUARIO, acceso=a)
    await cfg.aceptar(T, USUARIO, acceso=a)
    assert len(a.servicio.creadas) == 2


async def test_config_rechazar_calendario_inactivo_409() -> None:
    a = _acceso(calendario=False)
    with pytest.raises(cfg.AvisosError) as ei:
        await cfg.rechazar(T, USUARIO, acceso=a)
    assert ei.value.codigo == "calendario_inactivo" and a.alertas == []


# ------------------------------------------------------------------- parchear

async def test_config_parchear_activar_crea_plantilla_si_falta() -> None:
    a = _acceso()
    r_ = await cfg.parchear(T, USUARIO, recordatorio_activo=True, acceso=a)
    assert a.servicio.creadas == ["recordatorio_cita"]
    assert a.config["recordatorio_activo"] is True and a.config["cancelacion_activo"] is False
    assert a.config["oferta"] == "aceptada"  # activar equivale a aceptar (también desde pendiente/rechazada)
    assert r_["funciones"][0]["activa"] is True


async def test_config_parchear_activar_desde_rechazada_pasa_a_aceptada() -> None:
    a = _acceso()
    await cfg.rechazar(T, USUARIO, acceso=a)
    await cfg.parchear(T, USUARIO, cancelacion_activo=True, acceso=a)
    assert a.config["oferta"] == "aceptada"


async def test_config_parchear_plantilla_caida_se_recrea_con_sufijo() -> None:
    a = _acceso()
    await cfg.aceptar(T, USUARIO, acceso=a)
    a.cache["recordatorio_cita"]["estado"] = "REJECTED"
    a.config["recordatorio_activo"] = False
    await cfg.parchear(T, USUARIO, recordatorio_activo=True, acceso=a)
    assert a.config["nombre_plantilla_recordatorio"] == "recordatorio_cita_v2"
    assert a.config["recordatorio_activo"] is True


async def test_config_parchear_activar_con_plantilla_existente_no_crea() -> None:
    a = _acceso()
    a.cache["recordatorio_cita"] = _fila("recordatorio_cita", "APPROVED", "recordatorio")
    await cfg.parchear(T, USUARIO, recordatorio_activo=True, acceso=a)
    assert a.servicio.creadas == []


async def test_config_parchear_desactivar_es_solo_flag_y_no_exige_emisor() -> None:
    a = _acceso()
    await cfg.aceptar(T, USUARIO, acceso=a)
    a.emisor = False
    creadas = list(a.servicio.creadas)
    await cfg.parchear(T, USUARIO, cancelacion_activo=False, acceso=a)
    assert a.config["cancelacion_activo"] is False and a.config["recordatorio_activo"] is True
    assert a.servicio.creadas == creadas


async def test_config_parchear_activar_exige_emisor_y_calendario() -> None:
    a = _acceso(emisor=False)
    with pytest.raises(cfg.AvisosError) as ei:
        await cfg.parchear(T, USUARIO, recordatorio_activo=True, acceso=a)
    assert ei.value.codigo == "sin_numero_emisor"
    b = _acceso(calendario=False)
    with pytest.raises(cfg.AvisosError) as ei2:
        await cfg.parchear(T, USUARIO, recordatorio_activo=False, acceso=b)
    assert ei2.value.codigo == "calendario_inactivo"


async def test_config_parchear_vacio_es_error_de_programacion() -> None:
    with pytest.raises(ValueError):
        await cfg.parchear(T, USUARIO, acceso=_acceso())


async def test_config_parchear_fallo_de_meta_deja_la_funcion_apagada() -> None:
    a = _acceso()
    a.servicio.fallar_en.add("recordatorio_cita")
    with pytest.raises(PlantillaApiError):
        await cfg.parchear(T, USUARIO, recordatorio_activo=True, acceso=a)
    assert not a.config or a.config["recordatorio_activo"] is False


# -------------------------------------------------------------------- obtener

async def test_config_obtener_fila_ausente_es_pendiente_con_defaults() -> None:
    a = _acceso()
    v = await cfg.obtener(T, acceso=a)
    assert v["oferta"] == "pendiente" and v["calendario_activo"] is True
    assert v["numero_emisor_ok"] is True and v["mensaje"] is None
    assert [f["tipo"] for f in v["funciones"]] == ["recordatorio", "cancelacion"]
    for f in v["funciones"]:
        assert f["activa"] is False and f["estado_plantilla"] == "NO_CREADA" and f["motivo_rechazo"] is None
    assert v["funciones"][0]["nombre"] == "recordatorio_cita"
    assert v["funciones"][1]["nombre"] == "notificacion_cancelacion"
    assert v["funciones"][0]["texto"] == pl.cuerpo("recordatorio")
    assert v["funciones"][1]["texto"] == pl.cuerpo("cancelacion")


async def test_config_obtener_solo_las_dos_plantillas_propias_con_nombre_vigente() -> None:
    a = _acceso()
    a.cache["otra_plantilla_del_negocio"] = _fila("otra_plantilla_del_negocio")
    a.cache["recordatorio_cita_v2"] = _fila(
        "recordatorio_cita_v2", "REJECTED", "recordatorio", motivo_rechazo="INVALID_FORMAT"
    )
    a.config = {
        "oferta": "aceptada", "recordatorio_activo": False, "cancelacion_activo": True,
        "nombre_plantilla_recordatorio": "recordatorio_cita_v2",
        "nombre_plantilla_cancelacion": "notificacion_cancelacion",
    }
    v = await cfg.obtener(T, acceso=a)
    assert len(v["funciones"]) == 2
    rec, can = v["funciones"]
    assert rec["nombre"] == "recordatorio_cita_v2" and rec["estado_plantilla"] == "REJECTED"
    assert rec["motivo_rechazo"] == "INVALID_FORMAT"
    assert can["activa"] is True and can["estado_plantilla"] == "NO_CREADA"


async def test_config_obtener_calendario_apagado_no_lanza_y_lo_refleja() -> None:
    a = _acceso(calendario=False, emisor=False)
    v = await cfg.obtener(T, acceso=a)
    assert v["calendario_activo"] is False and v["numero_emisor_ok"] is False


# ------------------------------------------------------------- oferta_pendiente

async def test_config_oferta_pendiente() -> None:
    a = _acceso()
    assert await cfg.oferta_pendiente(T, acceso=a) is True  # fila ausente = pendiente
    a.config = {"oferta": "aceptada"}
    assert await cfg.oferta_pendiente(T, acceso=a) is False
    a.config = {"oferta": "rechazada"}
    assert await cfg.oferta_pendiente(T, acceso=a) is False
    a.config = {"oferta": "pendiente"}
    a.calendario = False
    assert await cfg.oferta_pendiente(T, acceso=a) is False


# ----------------------------------------------------------------- sincronizar

def _remota(nombre: str, estado: str, tipo: str = "recordatorio", motivo: str | None = None) -> dict[str, Any]:
    return {
        "name": nombre, "language": "es_MX", "category": "UTILITY", "status": estado,
        "components": COMP_RECO if tipo == "recordatorio" else COMP_CANC,
        "rejected_reason": motivo, "id": "77",
    }


async def _aceptada() -> AccesoFalso:
    a = _acceso()
    await cfg.aceptar(T, USUARIO, acceso=a)
    return a


async def test_config_sincronizar_pending_pasa_a_aprobada_sin_alerta() -> None:
    a = await _aceptada()
    a.servicio.remotas["recordatorio_cita"] = _remota("recordatorio_cita", "APPROVED")
    a.servicio.remotas["notificacion_cancelacion"] = _remota("notificacion_cancelacion", "APPROVED", "cancelacion")
    await cfg.sincronizar(T, acceso=a)
    assert a.cache["recordatorio_cita"]["estado"] == "APPROVED"
    assert a.config["recordatorio_activo"] is True and a.alertas == []


async def test_config_sincronizar_rechazada_apaga_y_alerta_una_sola_vez() -> None:
    a = await _aceptada()
    a.servicio.remotas["recordatorio_cita"] = _remota("recordatorio_cita", "REJECTED", motivo="INVALID_FORMAT")
    a.servicio.remotas["notificacion_cancelacion"] = _remota("notificacion_cancelacion", "PENDING", "cancelacion")
    await cfg.sincronizar(T, acceso=a)
    await cfg.sincronizar(T, acceso=a)  # la caché ya dice REJECTED: se vuelve a consultar, sin 2ª alerta
    assert a.config["recordatorio_activo"] is False and a.config["cancelacion_activo"] is True
    assert a.cache["recordatorio_cita"]["motivo_rechazo"] == "INVALID_FORMAT"
    assert [x[0] for x in a.alertas] == ["avisos_cita_plantilla_inutilizable"]
    assert "recordatorio" in a.alertas[0][2] and "REJECTED" in a.alertas[0][2]


async def test_config_sincronizar_no_consulta_las_aprobadas() -> None:
    a = _acceso()
    a.cache["notificacion_cancelacion"] = _fila("notificacion_cancelacion", "APPROVED")
    a.cache["recordatorio_cita"] = _fila("recordatorio_cita", "APPROVED", "recordatorio")
    a.config = {
        "oferta": "aceptada", "recordatorio_activo": True, "cancelacion_activo": True,
        "nombre_plantilla_recordatorio": "recordatorio_cita",
        "nombre_plantilla_cancelacion": "notificacion_cancelacion",
    }
    await cfg.sincronizar(T, acceso=a)
    assert a.servicio.consultas == []


async def test_config_sincronizar_borrada_en_meta_quita_fila_apaga_y_alerta() -> None:
    a = await _aceptada()
    a.servicio.remotas["recordatorio_cita"] = None  # Meta ya no la tiene
    a.servicio.remotas["notificacion_cancelacion"] = _remota("notificacion_cancelacion", "PENDING", "cancelacion")
    await cfg.sincronizar(T, acceso=a)
    assert "recordatorio_cita" not in a.cache
    assert a.config["recordatorio_activo"] is False
    assert len(a.alertas) == 1 and "ya no está disponible" in a.alertas[0][2]


async def test_config_sincronizar_error_de_red_responde_con_cache() -> None:
    a = await _aceptada()
    a.servicio.error_red = True
    await cfg.sincronizar(T, acceso=a)  # no lanza
    assert set(a.cache) == {"notificacion_cancelacion", "recordatorio_cita"}
    assert a.config["recordatorio_activo"] is True and a.alertas == []


async def test_config_sincronizar_sin_fila_ni_remota_no_hace_nada() -> None:
    a = _acceso()
    await cfg.sincronizar(T, acceso=a)
    assert a.alertas == [] and a.config is None


# ------------------------------------------------ reaccion_estado (único punto)

def test_config_estados_inutilizables() -> None:
    for e in ("REJECTED", "PAUSED", "DISABLED"):
        assert cache_tenant.estado_inutilizable(e)
    for e in ("APPROVED", "PENDING", None, "FLAGGED"):
        assert not cache_tenant.estado_inutilizable(e)


def test_config_mensaje_inutilizable_literal() -> None:
    m = cache_tenant.mensaje_inutilizable("cancelacion", "REJECTED")
    assert m == (
        "La plantilla de cancelación de tu negocio ya no está disponible (REJECTED). "
        "Se desactivó esa notificación. Puedes recrearla desde Calendario."
    )
    assert "recordatorio" in cache_tenant.mensaje_inutilizable("recordatorio", "PAUSED")


async def test_config_reaccion_estado_apaga_y_alerta_una_vez() -> None:
    a = await _aceptada()
    for _ in range(2):
        await cache_tenant.reaccion_estado(
            T, "recordatorio_cita", "PAUSED", apagar=a.apagar_funcion, alertar=a.alertar
        )
    assert a.config["recordatorio_activo"] is False and a.config["cancelacion_activo"] is True
    assert [x[0] for x in a.alertas] == ["avisos_cita_plantilla_inutilizable"]


async def test_config_reaccion_estado_aprobada_o_pendiente_no_hace_nada() -> None:
    a = await _aceptada()
    for e in ("APPROVED", "PENDING"):
        await cache_tenant.reaccion_estado(T, "recordatorio_cita", e, apagar=a.apagar_funcion, alertar=a.alertar)
    assert a.config["recordatorio_activo"] is True and a.alertas == []


async def test_config_reaccion_estado_fila_quitada_apaga_aunque_el_estado_sea_bueno() -> None:
    a = await _aceptada()
    await cache_tenant.reaccion_estado(
        T, "notificacion_cancelacion", "APPROVED", quitada=True, apagar=a.apagar_funcion, alertar=a.alertar
    )
    assert a.config["cancelacion_activo"] is False and len(a.alertas) == 1
    assert "ya no está disponible" in a.alertas[0][2]


async def test_config_reaccion_estado_nombre_ajeno_a_las_funciones_se_ignora() -> None:
    a = await _aceptada()
    await cache_tenant.reaccion_estado(T, "otra", "REJECTED", apagar=a.apagar_funcion, alertar=a.alertar)
    assert a.alertas == [] and a.config["recordatorio_activo"] is True


async def test_config_marcar_estado_por_error() -> None:
    a = await _aceptada()
    marcados: list[tuple[str, str]] = []

    async def actualizar(t: UUID, nombre: str, estado: str) -> bool:
        marcados.append((nombre, estado))
        return True

    await cache_tenant.marcar_estado_por_error(
        T, "notificacion_cancelacion", "DISABLED",
        actualizar=actualizar, apagar=a.apagar_funcion, alertar=a.alertar,
    )
    assert marcados == [("notificacion_cancelacion", "DISABLED")]
    assert a.config["cancelacion_activo"] is False
    assert [x[0] for x in a.alertas] == ["avisos_cita_plantilla_inutilizable"]


async def test_config_marcar_estado_por_error_sin_fila_local_no_alerta() -> None:
    a = await _aceptada()

    async def actualizar(t: UUID, nombre: str, estado: str) -> bool:
        return False

    await cache_tenant.marcar_estado_por_error(
        T, "notificacion_cancelacion", "DISABLED",
        actualizar=actualizar, apagar=a.apagar_funcion, alertar=a.alertar,
    )
    assert a.alertas == [] and a.config["cancelacion_activo"] is True


# ------------------------------------------------ neuroapi_plantillas: duplicado

def test_config_es_duplicado() -> None:
    d = PlantillaApiError("x", status_code=400, cuerpo='{"error":{"error_subcode":2388023,"message":"x"}}')
    assert neuro.es_duplicado(d)
    d2 = PlantillaApiError("x", status_code=400, cuerpo='{"error":{"message":"Template name already exists"}}')
    assert neuro.es_duplicado(d2)
    d3 = PlantillaApiError("x", status_code=409, cuerpo="{}")
    assert neuro.es_duplicado(d3)
    assert not neuro.es_duplicado(PlantillaApiError("x", status_code=400, cuerpo='{"error":{"message":"bad var"}}'))
    assert not neuro.es_duplicado(PlantillaApiError("x", status_code=500, cuerpo="already exists"))
    assert not neuro.es_duplicado(PlantillaApiError("x"))


async def test_config_servicio_crear_o_adoptar(monkeypatch: pytest.MonkeyPatch) -> None:
    s = neuro.NeuroApiTemplateService(api_key="k", base_url="http://x")
    llamadas: list[str] = []

    async def pedir(metodo: str, ruta: str = "", **kw: Any) -> Any:
        llamadas.append(f"{metodo}{ruta}")
        if metodo == "POST":
            raise PlantillaApiError("dup", status_code=400, cuerpo='{"error":{"error_subcode":2388023}}')
        return {"data": [{"name": "n", "status": "APPROVED"}]}

    monkeypatch.setattr(s, "_pedir", pedir)
    resp, creada = await s.crear_o_adoptar(nombre="n", idioma="es_MX", categoria="UTILITY", componentes=[])
    assert creada is False and resp["status"] == "APPROVED"
    assert llamadas == ["POST", "GET/n"]

    async def pedir_otro(metodo: str, ruta: str = "", **kw: Any) -> Any:
        raise PlantillaApiError("boom", status_code=400, cuerpo='{"error":{"message":"bad"}}')

    monkeypatch.setattr(s, "_pedir", pedir_otro)
    with pytest.raises(PlantillaApiError):
        await s.crear_o_adoptar(nombre="n", idioma="es_MX", categoria="UTILITY", componentes=[])


async def test_config_servicio_crear_o_adoptar_creada(monkeypatch: pytest.MonkeyPatch) -> None:
    s = neuro.NeuroApiTemplateService(api_key="k", base_url="http://x")

    async def pedir(metodo: str, ruta: str = "", **kw: Any) -> Any:
        return {"data": {"id": "5", "status": "PENDING"}}

    monkeypatch.setattr(s, "_pedir", pedir)
    resp, creada = await s.crear_o_adoptar(nombre="n", idioma="es_MX", categoria="UTILITY", componentes=[])
    assert creada is True and resp["id"] == "5"


def test_config_componentes_de_las_plantillas_son_validos() -> None:
    assert cfg.componentes("cancelacion") == COMP_CANC
    assert cfg.componentes("recordatorio") == COMP_RECO
    assert plantillas_reglas.numero_de_variables(COMP_CANC) == 2
    assert plantillas_reglas.numero_de_variables(COMP_RECO) == 2


# ============================================================
# T3: router /api/avisos-cita, /auth/yo, rutas sensibles, alerta de rechazo
# ============================================================
from pydantic import ValidationError  # noqa: E402

from schemas import ConfigAvisosOut, ConfigAvisosPatchIn, TipoAlerta  # noqa: E402
from security import crear_access_token, crear_token_impersonacion, hash_password  # noqa: E402
from services.acceso_soporte import ruta_sensible  # noqa: E402
from services.pipeline import set_tenant_servicios  # noqa: E402
from session import execute, fetch_one, fetch_value  # noqa: E402

TEXTO_RECHAZO_SPEC = (
    "Las notificaciones de recordatorio y cancelación de citas quedaron desactivadas. "
    "Puedes activarlas cuando quieras desde Calendario."
)


def test_t3_ruta_avisos_cita_es_sensible_para_ver_como() -> None:
    assert ruta_sensible("/api/avisos-cita")
    assert ruta_sensible("/api/avisos-cita/config")
    assert ruta_sensible("/api/avisos-cita/oferta")
    assert not ruta_sensible("/api/avisos-citas")


def test_t3_patch_vacio_es_invalido_y_con_un_campo_pasa() -> None:
    with pytest.raises(ValidationError):
        ConfigAvisosPatchIn()
    assert ConfigAvisosPatchIn(cancelacion_activo=False).recordatorio_activo is None
    assert ConfigAvisosPatchIn(recordatorio_activo=True).recordatorio_activo is True


def test_t3_mensaje_de_rechazo_coincide_con_la_spec_y_el_tipo_de_alerta_existe() -> None:
    assert cfg.MENSAJE_OFERTA_RECHAZADA == TEXTO_RECHAZO_SPEC
    tipos = {t.value for t in TipoAlerta}
    for tipo in ("avisos_cita_oferta_rechazada", "avisos_cita_plantilla_inutilizable"):
        assert tipo in tipos


def test_t3_vista_cumple_el_contrato_de_salida() -> None:
    v = cfg.vista_de(
        cfg.config_efectiva(None),
        {"recordatorio": None, "cancelacion": _fila("n", "PENDING")},
        calendario_activo=True,
        emisor_ok=False,
    )
    salida = ConfigAvisosOut(**v).model_dump()
    assert [f["tipo"] for f in salida["funciones"]] == ["recordatorio", "cancelacion"]
    assert salida["funciones"][0]["estado_plantilla"] == "NO_CREADA"
    assert salida["funciones"][1]["estado_plantilla"] == "PENDING"
    assert salida["mensaje"] is None


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _cuenta_t3(tenant_id: UUID, rol: str) -> str:
    uid = uuid4()
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, $5, true)
        """,
        uid, tenant_id, f"{rol}-{uid}@ejemplo.com", hash_password("test_password"), rol,
    )
    return crear_access_token(uid, tenant_id, rol)


@pytest.fixture
async def negocio_t3(tenant_y_usuario):
    tid = tenant_y_usuario["tenant_id"]
    await set_tenant_servicios(tid, None, None, True)
    yield {
        "tid": tid,
        "owner": tenant_y_usuario["token"],
        "usuario_id": tenant_y_usuario["usuario_id"],
        "member": await _cuenta_t3(tid, "member"),
        "vendedor": await _cuenta_t3(tid, "vendedor"),
    }
    await execute("DELETE FROM avisos_cita_config WHERE tenant_id = $1", tid)
    await execute("DELETE FROM alertas WHERE tenant_id = $1", tid)


@pytest.mark.asyncio
async def test_t3_get_config_member_y_owner_leen_y_vendedor_403(http_client, negocio_t3, monkeypatch) -> None:
    async def sin_red(tenant_id: UUID, **_: Any) -> None:
        return None

    monkeypatch.setattr(cfg, "sincronizar", sin_red)
    for rol in ("owner", "member"):
        r = await http_client.get("/api/avisos-cita/config", headers=_h(negocio_t3[rol]))
        assert r.status_code == 200, r.text
        cuerpo = r.json()
        assert cuerpo["oferta"] == "pendiente" and cuerpo["calendario_activo"] is True
        assert [f["tipo"] for f in cuerpo["funciones"]] == ["recordatorio", "cancelacion"]
        assert cuerpo["mensaje"] is None
        assert cuerpo["funciones"][0]["texto"] == pl.cuerpo("recordatorio")
        assert cuerpo["funciones"][1]["texto"] == pl.cuerpo("cancelacion")
    r = await http_client.get("/api/avisos-cita/config", headers=_h(negocio_t3["vendedor"]))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_t3_get_config_no_falla_si_la_conciliacion_revienta(http_client, negocio_t3, monkeypatch) -> None:
    async def explota(tenant_id: UUID, **_: Any) -> None:
        raise PlantillaApiError("sin red", status_code=502)

    monkeypatch.setattr(cfg, "sincronizar", explota)
    r = await http_client.get("/api/avisos-cita/config", headers=_h(negocio_t3["owner"]))
    assert r.status_code == 200
    assert r.json()["funciones"][0]["estado_plantilla"] == "NO_CREADA"


@pytest.mark.asyncio
async def test_t3_get_config_llama_sincronizar_antes_de_obtener(http_client, negocio_t3, monkeypatch) -> None:
    orden: list[str] = []
    obtener_real = cfg.obtener

    async def sincronizar_falso(tenant_id: UUID, **_: Any) -> None:
        orden.append("sincronizar")

    async def obtener_espia(tenant_id: UUID, **kw: Any) -> dict[str, Any]:
        orden.append("obtener")
        return await obtener_real(tenant_id, **kw)

    monkeypatch.setattr(cfg, "sincronizar", sincronizar_falso)
    monkeypatch.setattr(cfg, "obtener", obtener_espia)
    r = await http_client.get("/api/avisos-cita/config", headers=_h(negocio_t3["owner"]))
    assert r.status_code == 200 and orden == ["sincronizar", "obtener"]


@pytest.mark.asyncio
async def test_t3_escrituras_solo_gerencia(http_client, negocio_t3) -> None:
    for rol in ("member", "vendedor"):
        h = _h(negocio_t3[rol])
        assert (await http_client.post("/api/avisos-cita/oferta", json={"accion": "rechazar"}, headers=h)).status_code == 403
        assert (await http_client.patch("/api/avisos-cita/config", json={"cancelacion_activo": False}, headers=h)).status_code == 403


@pytest.mark.asyncio
async def test_t3_patch_vacio_422_sin_llamar_al_servicio(http_client, negocio_t3, monkeypatch) -> None:
    async def no_debe_llamarse(*a: Any, **k: Any) -> None:
        raise AssertionError("parchear no debe llamarse con body vacío")

    monkeypatch.setattr(cfg, "parchear", no_debe_llamarse)
    r = await http_client.patch("/api/avisos-cita/config", json={}, headers=_h(negocio_t3["owner"]))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_t3_oferta_accion_invalida_422(http_client, negocio_t3) -> None:
    r = await http_client.post("/api/avisos-cita/oferta", json={"accion": "otra"}, headers=_h(negocio_t3["owner"]))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_t3_rechazar_devuelve_mensaje_deja_alerta_y_es_idempotente(http_client, negocio_t3) -> None:
    h = _h(negocio_t3["owner"])
    r = await http_client.post("/api/avisos-cita/oferta", json={"accion": "rechazar"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["oferta"] == "rechazada"
    assert r.json()["mensaje"] == TEXTO_RECHAZO_SPEC
    assert all(f["activa"] is False for f in r.json()["funciones"])
    n = await fetch_value(
        "SELECT COUNT(*) FROM alertas WHERE tenant_id = $1 AND tipo = 'avisos_cita_oferta_rechazada' AND mensaje = $2",
        negocio_t3["tid"], TEXTO_RECHAZO_SPEC,
    )
    assert n == 1
    r2 = await http_client.post("/api/avisos-cita/oferta", json={"accion": "rechazar"}, headers=h)
    assert r2.status_code == 200 and r2.json()["oferta"] == "rechazada"
    assert await fetch_value(
        "SELECT COUNT(*) FROM alertas WHERE tenant_id = $1 AND tipo = 'avisos_cita_oferta_rechazada'",
        negocio_t3["tid"],
    ) == 1


@pytest.mark.asyncio
async def test_t3_calendario_inactivo_409_en_escrituras_y_get_200(http_client, negocio_t3, monkeypatch) -> None:
    await set_tenant_servicios(negocio_t3["tid"], None, None, False)
    h = _h(negocio_t3["owner"])
    for r in (
        await http_client.post("/api/avisos-cita/oferta", json={"accion": "aceptar"}, headers=h),
        await http_client.post("/api/avisos-cita/oferta", json={"accion": "rechazar"}, headers=h),
        await http_client.patch("/api/avisos-cita/config", json={"cancelacion_activo": True}, headers=h),
    ):
        assert r.status_code == 409 and r.json()["detail"]["codigo"] == "calendario_inactivo"

    async def sin_red(tenant_id: UUID, **_: Any) -> None:
        return None

    monkeypatch.setattr(cfg, "sincronizar", sin_red)
    g = await http_client.get("/api/avisos-cita/config", headers=h)
    assert g.status_code == 200 and g.json()["calendario_activo"] is False


@pytest.mark.asyncio
async def test_t3_aceptar_sin_emisor_409_y_errores_de_neuroapi_son_502(http_client, negocio_t3, monkeypatch) -> None:
    h = _h(negocio_t3["owner"])
    r = await http_client.post("/api/avisos-cita/oferta", json={"accion": "aceptar"}, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["codigo"] == "sin_numero_emisor"

    async def falla(*a: Any, **k: Any) -> None:
        raise PlantillaApiError("Meta caída", status_code=500)

    monkeypatch.setattr(cfg, "aceptar", falla)
    monkeypatch.setattr(cfg, "parchear", falla)
    r = await http_client.post("/api/avisos-cita/oferta", json={"accion": "aceptar"}, headers=h)
    assert r.status_code == 502
    r = await http_client.patch("/api/avisos-cita/config", json={"recordatorio_activo": True}, headers=h)
    assert r.status_code == 502


@pytest.mark.asyncio
async def test_t3_patch_apagar_no_pide_emisor_y_devuelve_config(http_client, negocio_t3) -> None:
    r = await http_client.patch(
        "/api/avisos-cita/config", json={"recordatorio_activo": False}, headers=_h(negocio_t3["owner"])
    )
    assert r.status_code == 200, r.text
    assert r.json()["funciones"][0]["activa"] is False


@pytest.mark.asyncio
async def test_t3_el_tenant_sale_del_token_no_del_cuerpo(http_client, negocio_t3) -> None:
    otro = uuid4()
    r = await http_client.post(
        "/api/avisos-cita/oferta",
        json={"accion": "rechazar", "tenant_id": str(otro)},
        headers=_h(negocio_t3["owner"]),
    )
    assert r.status_code == 200
    assert await fetch_one("SELECT 1 FROM avisos_cita_config WHERE tenant_id = $1", otro) is None
    assert await fetch_one("SELECT 1 FROM avisos_cita_config WHERE tenant_id = $1", negocio_t3["tid"]) is not None


# --- /auth/yo -------------------------------------------------------------
async def _yo(http_client: Any, token: str) -> dict[str, Any]:
    r = await http_client.get("/api/auth/yo", headers=_h(token))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_t3_yo_oferta_pendiente_segun_calendario_y_decision(http_client, tenant_y_usuario) -> None:
    tid, owner = tenant_y_usuario["tenant_id"], tenant_y_usuario["token"]
    try:
        # Sin fila de servicios (calendario no activo): no hay oferta.
        assert (await _yo(http_client, owner))["oferta_avisos_cita_pendiente"] is False
        await set_tenant_servicios(tid, None, None, False)
        assert (await _yo(http_client, owner))["oferta_avisos_cita_pendiente"] is False
        # Calendario activo y sin fila de config = pendiente.
        await set_tenant_servicios(tid, None, None, True)
        assert (await _yo(http_client, owner))["oferta_avisos_cita_pendiente"] is True
        for oferta, esperado in (("aceptada", False), ("rechazada", False), ("pendiente", True)):
            await execute(
                """
                INSERT INTO avisos_cita_config (tenant_id, oferta) VALUES ($1, $2)
                ON CONFLICT (tenant_id) DO UPDATE SET oferta = EXCLUDED.oferta
                """,
                tid, oferta,
            )
            assert (await _yo(http_client, owner))["oferta_avisos_cita_pendiente"] is esperado
    finally:
        await execute("DELETE FROM avisos_cita_config WHERE tenant_id = $1", tid)


@pytest.mark.asyncio
async def test_t3_yo_oferta_no_se_muestra_a_member(http_client, negocio_t3) -> None:
    assert (await _yo(http_client, negocio_t3["owner"]))["oferta_avisos_cita_pendiente"] is True
    assert (await _yo(http_client, negocio_t3["member"]))["oferta_avisos_cita_pendiente"] is False


@pytest.mark.asyncio
async def test_t3_yo_oferta_nunca_en_ver_como_y_ver_como_no_escribe(http_client, negocio_t3) -> None:
    gerente_id = uuid4()
    email = f"gerente-{gerente_id}@operativai.test"
    await execute(
        """
        INSERT INTO portal_users (id, tenant_id, email, password_hash, role, is_active)
        VALUES ($1, $2, $3, $4, 'owner', true)
        """,
        gerente_id, negocio_t3["tid"], email, hash_password("test_password"),
    )
    await execute(
        "INSERT INTO gerencia_users (email, full_name, cargo) VALUES ($1, 'Test', 'Developer')", email
    )
    try:
        token, _ = crear_token_impersonacion(
            negocio_t3["usuario_id"], negocio_t3["tid"], "owner", gerente_id, 15
        )
        assert (await _yo(http_client, token))["oferta_avisos_cita_pendiente"] is False
        r = await http_client.post("/api/avisos-cita/oferta", json={"accion": "rechazar"}, headers=_h(token))
        assert r.status_code == 403
        r = await http_client.patch("/api/avisos-cita/config", json={"cancelacion_activo": False}, headers=_h(token))
        assert r.status_code == 403
    finally:
        await execute("DELETE FROM gerencia_users WHERE LOWER(email) = LOWER($1)", email)
        await execute("DELETE FROM gerencia_auditoria WHERE actor_email = $1", email)


# ======================================================================
# T4 · Emisor y envío por tenant (services/avisos_cita_envio.py)
# ======================================================================
from services import avisos_cita_envio as env  # noqa: E402
from services.whatsapp import ProveedorWhatsAppError, ResultadoEnvio  # noqa: E402


def _canal(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": uuid4(), "phone_number_id": "111", "bsp_provider": "neuroapi", "estado": "activo",
        "es_principal": True, "es_linea_avisos": False,
        "agent_name": "Ana", "system_prompt": "Eres el asistente.",
    }
    base.update(kw)
    return base


def test_emisor_elegir_principal_valido() -> None:
    otro = _canal(es_principal=False, phone_number_id="222")
    p = _canal()
    assert env.elegir_principal([otro, p]) is p
    assert env.elegir_principal([]) is None
    assert env.elegir_principal([otro]) is None


@pytest.mark.parametrize(
    "cambio",
    [
        {"estado": "pausado"},
        {"estado": "desvinculado"},
        {"es_linea_avisos": True},
        {"agent_name": "  "},
        {"system_prompt": ""},
        {"system_prompt": None},
        {"phone_number_id": " "},
    ],
)
def test_emisor_principal_no_valido_da_none(cambio: dict[str, Any]) -> None:
    assert env.elegir_principal([_canal(**cambio)]) is None


def test_emisor_coincide_credencial() -> None:
    p = _canal()
    cred = {"phone_number_id": "111", "bsp_provider": "neuroapi", "access_token": "x"}
    assert env.coincide_credencial(p, cred) is True
    assert env.coincide_credencial(p, {**cred, "phone_number_id": " 111 "}) is True
    assert env.coincide_credencial(p, {**cred, "phone_number_id": "999"}) is False
    assert env.coincide_credencial(p, {**cred, "bsp_provider": "meta"}) is False
    assert env.coincide_credencial(p, {**cred, "phone_number_id": None}) is False
    assert env.coincide_credencial(p, None) is False
    assert env.coincide_credencial(None, cred) is False


def test_emisor_motivo_de_error_estilo_existente() -> None:
    assert env.motivo_de_error(400) == "plantilla_no_disponible"
    assert env.motivo_de_error(500) == "plantilla_no_disponible"
    assert env.motivo_de_error(401) == "error_proveedor"
    assert env.motivo_de_error(None) == "error_proveedor"


class _ProveedorFalso:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.llamadas: list[tuple[Any, ...]] = []

    async def enviar_plantilla_con_componentes(self, destinatario, plantilla, idioma, componentes):
        self.llamadas.append((destinatario, plantilla, idioma, componentes))
        if self.error:
            raise self.error
        return ResultadoEnvio(id_mensaje="wamid.1")


class _AccesoEnvioFalso:
    def __init__(self, filas, credencial, proveedor=None) -> None:
        self.filas, self.cred, self.prov = filas, credencial, proveedor or _ProveedorFalso()
        self.credenciales_usadas: list[dict[str, Any]] = []

    async def filas_principal(self, tenant_id):
        return self.filas

    async def credencial(self, tenant_id):
        return self.cred

    def proveedor(self, credencial):
        self.credenciales_usadas.append(credencial)
        return self.prov


CRED_OK = {"phone_number_id": "111", "bsp_provider": "neuroapi", "access_token": "t"}


async def test_emisor_numero_emisor_y_emisor_ok() -> None:
    a = _AccesoEnvioFalso([_canal()], CRED_OK)
    assert (await env.numero_emisor(uuid4(), acceso=a))["phone_number_id"] == "111"
    assert await env.emisor_ok(uuid4(), acceso=a) is True
    sin = _AccesoEnvioFalso([], CRED_OK)
    assert await env.numero_emisor(uuid4(), acceso=sin) is None
    assert await env.emisor_ok(uuid4(), acceso=sin) is False


async def test_emisor_ok_false_si_la_credencial_es_de_otro_numero_o_no_hay() -> None:
    assert await env.emisor_ok(uuid4(), acceso=_AccesoEnvioFalso([_canal()], {**CRED_OK, "phone_number_id": "9"})) is False
    assert await env.emisor_ok(uuid4(), acceso=_AccesoEnvioFalso([_canal()], None)) is False
    assert await env.emisor_ok(uuid4(), acceso=_AccesoEnvioFalso([_canal(estado="pausado")], CRED_OK)) is False


async def test_emisor_enviar_ok_usa_componentes_es_mx_y_la_credencial_del_principal() -> None:
    a = _AccesoEnvioFalso([_canal()], CRED_OK)
    comps = r.parametros_de_envio("recordatorio", "Mi Negocio", None, "20-10-2026")
    res = await env.enviar_plantilla_tenant(uuid4(), "+5215512345678", "recordatorio_cita_v2", comps, acceso=a)
    assert res.enviado and res.motivo is None and res.id_mensaje == "wamid.1"
    assert a.prov.llamadas == [("+5215512345678", "recordatorio_cita_v2", "es_MX", comps)]
    assert a.credenciales_usadas == [CRED_OK]


async def test_emisor_enviar_sin_emisor_no_llama_al_proveedor() -> None:
    for a in (
        _AccesoEnvioFalso([], CRED_OK),
        _AccesoEnvioFalso([_canal(es_linea_avisos=True, es_principal=False)], CRED_OK),
        _AccesoEnvioFalso([_canal()], {**CRED_OK, "phone_number_id": "otro"}),
    ):
        res = await env.enviar_plantilla_tenant(uuid4(), "+521", "n", [], acceso=a)
        assert (res.enviado, res.motivo) == (False, "sin_numero_emisor")
        assert a.prov.llamadas == [] and a.credenciales_usadas == []


async def test_emisor_enviar_mapea_errores_del_proveedor() -> None:
    casos = ((400, "plantilla_no_disponible"), (500, "plantilla_no_disponible"), (401, "error_proveedor"), (None, "error_proveedor"))
    for status, motivo in casos:
        prov = _ProveedorFalso(ProveedorWhatsAppError("x", status_code=status, cuerpo="cuerpo"))
        res = await env.enviar_plantilla_tenant(uuid4(), "+521", "n", [], acceso=_AccesoEnvioFalso([_canal()], CRED_OK, prov))
        assert (res.enviado, res.motivo, res.status_code, res.cuerpo) == (False, motivo, status, "cuerpo")
    prov = _ProveedorFalso(RuntimeError("boom"))
    res = await env.enviar_plantilla_tenant(uuid4(), "+521", "n", [], acceso=_AccesoEnvioFalso([_canal()], CRED_OK, prov))
    assert (res.enviado, res.motivo) == (False, "error_proveedor")


async def test_emisor_acceso_bd_de_config_delega_en_avisos_cita_envio(monkeypatch) -> None:
    visto: list[UUID] = []

    async def falso(tenant_id: UUID) -> bool:
        visto.append(tenant_id)
        return True

    monkeypatch.setattr(cfg.avisos_cita_envio, "emisor_ok", falso)
    tid = uuid4()
    assert await cfg.AccesoBD().emisor_ok(tid) is True
    assert visto == [tid]


def test_emisor_no_usa_la_cuenta_global() -> None:
    fuente = (Path(__file__).resolve().parent.parent / "services" / "avisos_cita_envio.py").read_text(encoding="utf-8")
    codigo = "\n".join(l for l in fuente.splitlines() if not l.strip().startswith("#"))
    assert "import cuenta_global_wa" not in codigo and "cuenta_global_wa." not in codigo
    assert "AVISOS_CUENTA_GLOBAL" not in codigo


async def test_emisor_sql_de_numero_emisor_contra_la_base(tenant_y_usuario) -> None:
    """Consulta real: principal activo y completo; pausado no cuenta. Limpia al final."""
    tid = tenant_y_usuario["tenant_id"]
    try:
        assert await env.numero_emisor(tid) is None
        await execute(
            """
            INSERT INTO agentes_canal (tenant_id, phone_number_id, estado, es_principal, agent_name, system_prompt)
            VALUES ($1, $2, 'activo', true, 'Ana', 'Prompt')
            """,
            tid, f"t4-{uuid4().hex[:12]}",
        )
        fila = await env.numero_emisor(tid)
        assert fila is not None and fila["estado"] == "activo" and fila["es_principal"] is True
        # Sin credencial del canal no hay coincidencia (D6).
        assert await env.emisor_ok(tid) is False
        await execute("UPDATE agentes_canal SET es_principal = false, estado = 'pausado', pausado_en = NOW() WHERE tenant_id = $1", tid)
        assert await env.numero_emisor(tid) is None
    finally:
        await execute("DELETE FROM agentes_canal WHERE tenant_id = $1", tid)
    assert await fetch_value("SELECT COUNT(*) FROM agentes_canal WHERE tenant_id = $1", tid) == 0


# ------------------------------------------------------------------ T10 (RF-11, D9): aislamiento bidireccional

import logging  # noqa: E402

from config import settings  # noqa: E402

WABA_G = "WABA-GLOBAL"
WABA_N = "WABA-NEGOCIO"
TENANT_N = uuid4()


def _entry(nombre: str, event: str, waba: str, meta_id: str | None = "777") -> dict[str, Any]:
    valor: dict[str, Any] = {
        "event": event,
        "message_template_name": nombre,
        "message_template_language": "es_MX",
    }
    if meta_id is not None:
        valor["message_template_id"] = meta_id
    return {"id": waba, "changes": [{"field": "message_template_status_update", "value": valor}]}


def _sobre(*entradas: dict[str, Any]) -> dict[str, Any]:
    return {"entry": list(entradas)}


class _Negocio:
    """Caché de un negocio con las funciones de aviso en memoria (misma semántica atómica que la BD)."""

    def __init__(self, estado: str = "PENDING", waba: str | None = WABA_N, meta_id: str | None = "777") -> None:
        self.fila: dict[str, Any] = {
            "tenant_id": TENANT_N, "nombre": "recordatorio_cita", "idioma": "es_MX", "estado": estado,
            "motivo_rechazo": None, "meta_template_id": meta_id, "waba_id": waba,
        }
        self.activa = {"recordatorio": True, "cancelacion": True}
        self.alertas: list[str] = []

    def instalar(self, mp: pytest.MonkeyPatch) -> "_Negocio":
        n = self

        async def fetch_all(sql: str, *a: Any) -> list[dict[str, Any]]:
            return [dict(n.fila)] if n.fila["nombre"] == a[0] else []

        async def execute_(sql: str, *a: Any) -> None:
            n.fila["estado"], n.fila["motivo_rechazo"] = a[3], a[4]
            n.fila["meta_template_id"] = n.fila["meta_template_id"] or a[5]
            n.fila["waba_id"] = n.fila["waba_id"] or a[6]

        async def apagar(tenant_id: UUID, nombre: str) -> str | None:
            tipo = "recordatorio" if nombre == "recordatorio_cita" else "cancelacion"
            if n.activa[tipo]:
                n.activa[tipo] = False
                return tipo
            return None

        async def alertar(tenant_id: UUID, tipo: str, titulo: str, mensaje: str, datos: dict[str, Any]) -> None:
            n.alertas.append(tipo)

        mp.setattr(cache_tenant, "fetch_all", fetch_all)
        mp.setattr(cache_tenant, "execute", execute_)
        mp.setattr(cache_tenant, "_apagar_funcion_bd", apagar)
        mp.setattr(cache_tenant, "_alertar_bd", alertar)
        return n


@pytest.fixture
def waba_global(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AVISOS_CUENTA_GLOBAL_WABA_ID", WABA_G, raising=False)


def test_t10_entradas_de_negocio_descarta_las_del_waba_global() -> None:
    g = _entry("recordatorio_cita", "APPROVED", WABA_G)
    n = _entry("recordatorio_cita", "APPROVED", WABA_N)
    assert cache_tenant.entradas_de_negocio(_sobre(g, n), WABA_G) == [n]
    assert cache_tenant.entradas_de_negocio(_sobre(g), WABA_G) == []


@pytest.mark.parametrize("vacio", ["", None, "  "])
def test_t10_entradas_de_negocio_con_waba_global_vacio_no_descarta_nada(vacio: str | None) -> None:
    g = _entry("recordatorio_cita", "APPROVED", WABA_G)
    n = _entry("recordatorio_cita", "APPROVED", WABA_N)
    assert cache_tenant.entradas_de_negocio(_sobre(g, n), vacio) == [g, n]


@pytest.mark.parametrize("basura", [None, [], "x", {}, {"entry": None}])
def test_t10_entradas_de_negocio_tolera_basura(basura: Any) -> None:
    assert cache_tenant.entradas_de_negocio(basura, WABA_G) == []


def test_t10_elegir_candidata_unica_que_contradice_se_descarta() -> None:
    c = {"tenant_id": 1, "meta_template_id": "111", "waba_id": WABA_N}
    assert cache_tenant.elegir_candidata([c], "999", WABA_N) is None  # meta_template_id distinto
    assert cache_tenant.elegir_candidata([c], "111", WABA_G) is c  # meta_id coincide: prioridad existente
    assert cache_tenant.elegir_candidata([c], None, WABA_G) is None  # waba_id distinto
    assert cache_tenant.elegir_candidata([c], "999", None) is None


def test_t10_elegir_candidata_sin_contradiccion_funciona_como_antes() -> None:
    sin_datos = {"tenant_id": 1, "meta_template_id": None, "waba_id": None}
    assert cache_tenant.elegir_candidata([sin_datos], "999", WABA_G) is sin_datos  # NULL no contradice
    parcial = {"tenant_id": 1, "meta_template_id": None, "waba_id": WABA_N}
    assert cache_tenant.elegir_candidata([parcial], "999", WABA_N) is parcial
    assert cache_tenant.elegir_candidata([parcial], None, None) is parcial  # evento sin datos: no contradice


async def test_t10_a_evento_global_approved_no_cambia_la_plantilla_pending_del_negocio(
    monkeypatch: pytest.MonkeyPatch, waba_global: None
) -> None:
    n = _Negocio("PENDING", waba=None, meta_id=None).instalar(monkeypatch)  # aún sin ids aprendidos
    r = await cache_tenant.aplicar_evento_meta(_sobre(_entry("recordatorio_cita", "APPROVED", WABA_G, "42")))
    assert r == []
    assert n.fila["estado"] == "PENDING" and n.fila["waba_id"] is None and n.fila["meta_template_id"] is None


@pytest.mark.parametrize("evento", ["REJECTED", "PAUSED", "DISABLED"])
async def test_t10_b_evento_global_malo_no_apaga_ni_alerta(
    monkeypatch: pytest.MonkeyPatch, waba_global: None, evento: str
) -> None:
    n = _Negocio("APPROVED").instalar(monkeypatch)
    r = await cache_tenant.aplicar_evento_meta(_sobre(_entry("recordatorio_cita", evento, WABA_G, "42")))
    assert r == [] and n.fila["estado"] == "APPROVED"
    assert n.activa == {"recordatorio": True, "cancelacion": True} and n.alertas == []


@pytest.mark.parametrize("evento", ["REJECTED", "PAUSED", "DISABLED"])
async def test_t10_c_evento_del_waba_del_negocio_aplica_apaga_y_alerta_una_vez(
    monkeypatch: pytest.MonkeyPatch, waba_global: None, evento: str
) -> None:
    n = _Negocio("APPROVED").instalar(monkeypatch)
    ev = _sobre(_entry("recordatorio_cita", evento, WABA_N))
    r1 = await cache_tenant.aplicar_evento_meta(ev)
    r2 = await cache_tenant.aplicar_evento_meta(ev)
    assert r1 == [(TENANT_N, "recordatorio_cita", evento)] and r2 == []
    assert n.activa == {"recordatorio": False, "cancelacion": True}
    assert n.alertas == ["avisos_cita_plantilla_inutilizable"]


async def test_t10_contradiccion_de_waba_o_de_id_se_descarta_con_warning(
    monkeypatch: pytest.MonkeyPatch, waba_global: None, caplog: pytest.LogCaptureFixture
) -> None:
    n = _Negocio("PENDING").instalar(monkeypatch)
    with caplog.at_level(logging.WARNING):
        a = await cache_tenant.aplicar_evento_meta(_sobre(_entry("recordatorio_cita", "APPROVED", "WABA-OTRO", None)))
        b = await cache_tenant.aplicar_evento_meta(_sobre(_entry("recordatorio_cita", "APPROVED", WABA_N, "999")))
    assert a == [] and b == [] and n.fila["estado"] == "PENDING"
    assert len([x for x in caplog.records if x.levelno == logging.WARNING]) == 2


async def test_t10_d_waba_global_vacio_descarta_contradictorio_y_aplica_coherente_con_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "AVISOS_CUENTA_GLOBAL_WABA_ID", "", raising=False)
    n = _Negocio("PENDING").instalar(monkeypatch)
    with caplog.at_level(logging.WARNING):
        malo = await cache_tenant.aplicar_evento_meta(_sobre(_entry("recordatorio_cita", "APPROVED", WABA_G, "42")))
        assert malo == [] and n.fila["estado"] == "PENDING"
        caplog.clear()
        bueno = await cache_tenant.aplicar_evento_meta(_sobre(_entry("recordatorio_cita", "APPROVED", WABA_N)))
    assert bueno == [(TENANT_N, "recordatorio_cita", "APPROVED")] and n.fila["estado"] == "APPROVED"
    avisos = [x for x in caplog.records if x.levelno == logging.WARNING]
    assert len(avisos) == 1 and "WABA global" in avisos[0].getMessage()


async def test_t10_con_waba_global_configurado_un_evento_coherente_no_deja_warning(
    monkeypatch: pytest.MonkeyPatch, waba_global: None, caplog: pytest.LogCaptureFixture
) -> None:
    _Negocio("PENDING").instalar(monkeypatch)
    with caplog.at_level(logging.WARNING):
        await cache_tenant.aplicar_evento_meta(_sobre(_entry("recordatorio_cita", "APPROVED", WABA_N)))
    assert [x for x in caplog.records if x.levelno == logging.WARNING] == []


async def test_t10_e_sobre_con_entry_global_y_de_negocio_aplica_solo_el_de_negocio(
    monkeypatch: pytest.MonkeyPatch, waba_global: None
) -> None:
    n = _Negocio("APPROVED").instalar(monkeypatch)
    ev = _sobre(_entry("recordatorio_cita", "APPROVED", WABA_G, "42"), _entry("recordatorio_cita", "REJECTED", WABA_N))
    r = await cache_tenant.aplicar_evento_meta(ev)
    assert r == [(TENANT_N, "recordatorio_cita", "REJECTED")]
    assert n.fila["estado"] == "REJECTED" and n.alertas == ["avisos_cita_plantilla_inutilizable"]
    # Orden inverso: el global (APPROVED) no debe pisar al de negocio (REJECTED).
    n2 = _Negocio("PENDING").instalar(monkeypatch)
    ev2 = _sobre(_entry("recordatorio_cita", "REJECTED", WABA_N), _entry("recordatorio_cita", "APPROVED", WABA_G, "42"))
    await cache_tenant.aplicar_evento_meta(ev2)
    assert n2.fila["estado"] == "REJECTED"


async def test_t10_candidata_sin_ids_aprendidos_se_aplica_con_waba_global_configurado(
    monkeypatch: pytest.MonkeyPatch, waba_global: None
) -> None:
    n = _Negocio("PENDING", waba=None, meta_id=None).instalar(monkeypatch)
    r = await cache_tenant.aplicar_evento_meta(_sobre(_entry("recordatorio_cita", "APPROVED", WABA_N, "777")))
    assert r == [(TENANT_N, "recordatorio_cita", "APPROVED")]
    assert n.fila["waba_id"] == WABA_N and n.fila["meta_template_id"] == "777"


async def test_t10_f_caso_directo_global_solo_aplica_al_handler_global(
    monkeypatch: pytest.MonkeyPatch, waba_global: None
) -> None:
    from services import plantillas_whatsapp as pw

    estados = {n: "no_solicitada" for n in pw.NOMBRES_PLANTILLA}

    async def obtener_estado(nombre: str) -> str:
        return estados[nombre]

    async def cambiar_estado(nombre: str, nuevo: str, **kw: Any) -> str:
        anterior, estados[nombre] = estados[nombre], nuevo
        return anterior

    monkeypatch.setattr(pw, "obtener_estado", obtener_estado)
    monkeypatch.setattr(pw, "cambiar_estado", cambiar_estado)
    n = _Negocio("PENDING").instalar(monkeypatch)
    ev = _sobre(_entry("recordatorio_cita", "APPROVED", WABA_G, "42"))
    assert await pw.aplicar_evento_meta(ev) == [("recordatorio_cita", "aprobada")]
    assert await cache_tenant.aplicar_evento_meta(ev) == []
    assert n.fila["estado"] == "PENDING"
