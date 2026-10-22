"""
Configuración de los avisos de cita por negocio (spec 007): oferta (aceptar/rechazar),
interruptores de recordatorio y cancelación, y las plantillas de WhatsApp propias de cada
función (creadas/adoptadas en el WABA del negocio vía NeuroAPI).

Sin lógica HTTP salvo `a_http` (como `neuroapi_plantillas`). Todo el acceso a base de datos,
a Meta, a la alerta y a la comprobación del número emisor pasa por un objeto `acceso`
inyectable (`AccesoBD` en producción); los tests usan uno falso, sin red ni DB.

`AccesoBD.emisor_ok` delega en `services/avisos_cita_envio.emisor_ok` (D6: principal activo
coincidente con la credencial del canal); sin él, aceptar/activar responden 409 `sin_numero_emisor`.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol
from uuid import UUID

from fastapi import HTTPException

from services import avisos_cita_envio
from services import avisos_cita_plantillas as pl
from services import plantillas_reglas as reglas
from services import plantillas_tenant as cache
from services.neuroapi_plantillas import NeuroApiTemplateService, PlantillaApiError
from session import fetch_one, transaccion

log = logging.getLogger("operativai.avisos_cita_config")

OFERTA_PENDIENTE = "pendiente"
OFERTA_ACEPTADA = "aceptada"
OFERTA_RECHAZADA = "rechazada"

# Orden de creación (core-spec): cancelación primero.
TIPOS_CREACION = (pl.TIPO_CANCELACION, pl.TIPO_RECORDATORIO)
# Orden de las funciones en la respuesta de `obtener`.
TIPOS_VISTA = (pl.TIPO_RECORDATORIO, pl.TIPO_CANCELACION)

ALERTA_OFERTA_RECHAZADA = "avisos_cita_oferta_rechazada"
MENSAJE_OFERTA_RECHAZADA = (
    "Las notificaciones de recordatorio y cancelación de citas quedaron desactivadas. "
    "Puedes activarlas cuando quieras desde Calendario."
)
MENSAJE_OFERTA_ACEPTADA = (
    "Listo: creamos las plantillas de recordatorio y cancelación de citas. "
    "Se enviarán en cuanto Meta las apruebe."
)

COLUMNAS_CONFIG = (
    "oferta",
    "recordatorio_activo",
    "cancelacion_activo",
    "nombre_plantilla_recordatorio",
    "nombre_plantilla_cancelacion",
    "decidido_en",
    "decidido_por",
)


class AvisosError(Exception):
    """Error de negocio con código estable para el cliente (409 casi siempre)."""

    def __init__(self, status_code: int, codigo: str, mensaje: str) -> None:
        super().__init__(mensaje)
        self.status_code = status_code
        self.codigo = codigo
        self.mensaje = mensaje


def a_http(e: AvisosError) -> HTTPException:
    return HTTPException(
        status_code=e.status_code, detail={"codigo": e.codigo, "mensaje": e.mensaje}
    )


def _calendario_inactivo() -> AvisosError:
    return AvisosError(
        409, "calendario_inactivo", "El módulo de calendario no está activo en tu negocio"
    )


def _sin_numero_emisor() -> AvisosError:
    return AvisosError(
        409,
        "sin_numero_emisor",
        "Necesitas un número principal de WhatsApp activo y con la configuración completa "
        "para enviar estos avisos",
    )


def _no_utilizable() -> AvisosError:
    return AvisosError(
        409,
        "plantilla_existente_no_utilizable",
        "Ya existen plantillas con esos nombres y ninguna se puede usar; "
        "elimina alguna desde Plantillas de WhatsApp",
    )


# ------------------------------------------------------------
# Puras
# ------------------------------------------------------------
def columna_activo(tipo: str) -> str:
    return f"{_tipo_valido(tipo)}_activo"


def columna_nombre(tipo: str) -> str:
    return f"nombre_plantilla_{_tipo_valido(tipo)}"


def _tipo_valido(tipo: str) -> str:
    if tipo not in pl.TIPOS:
        raise ValueError(f"tipo de aviso desconocido: {tipo!r}")
    return tipo


def config_efectiva(fila: dict[str, Any] | None) -> dict[str, Any]:
    """Fila ausente = oferta pendiente, ambas funciones apagadas y nombres base."""
    base: dict[str, Any] = {
        "oferta": OFERTA_PENDIENTE,
        "recordatorio_activo": False,
        "cancelacion_activo": False,
        "nombre_plantilla_recordatorio": pl.nombre_base(pl.TIPO_RECORDATORIO),
        "nombre_plantilla_cancelacion": pl.nombre_base(pl.TIPO_CANCELACION),
    }
    if fila:
        for k in base:
            if fila.get(k) is not None:
                base[k] = fila[k]
    base["recordatorio_activo"] = bool(base["recordatorio_activo"])
    base["cancelacion_activo"] = bool(base["cancelacion_activo"])
    return base


def componentes(tipo: str) -> list[dict[str, Any]]:
    """`components` de Meta para el aviso (solo cuerpo, con los ejemplos de la spec)."""
    return reglas.construir_componentes(cuerpo=pl.cuerpo(tipo), ejemplos_cuerpo=pl.ejemplos(tipo))


def datos_de_creada(nombre: str, comps: list[dict[str, Any]], respuesta: dict[str, Any]) -> dict[str, Any]:
    """Fila de caché de una plantilla recién creada (Meta contesta PENDING al crear)."""
    guardada = cache.normalizar_meta({"name": nombre, "language": pl.IDIOMA, **respuesta}) or {}
    return {
        **guardada,
        "nombre": nombre,
        "idioma": pl.IDIOMA,
        "categoria": respuesta.get("category") or pl.CATEGORIA,
        "estado": guardada.get("estado", "PENDING"),
        "componentes": comps,
    }


def vista_de(
    config: dict[str, Any],
    filas: dict[str, dict[str, Any] | None],
    *,
    calendario_activo: bool,
    emisor_ok: bool,
    mensaje: str | None = None,
) -> dict[str, Any]:
    """Arma el `ConfigAvisos` del contrato. `filas` = caché de cada tipo (None = no creada)."""
    funciones: list[dict[str, Any]] = []
    for tipo in TIPOS_VISTA:
        fila = filas.get(tipo)
        funciones.append(
            {
                "tipo": tipo,
                "nombre": config[columna_nombre(tipo)],
                "texto": pl.cuerpo(tipo),
                "activa": bool(config[columna_activo(tipo)]),
                "estado_plantilla": str(fila["estado"]) if fila else "NO_CREADA",
                "motivo_rechazo": (fila.get("motivo_rechazo") if fila else None),
            }
        )
    return {
        "oferta": config["oferta"],
        "calendario_activo": calendario_activo,
        "numero_emisor_ok": emisor_ok,
        "funciones": funciones,
        "mensaje": mensaje,
    }


# ------------------------------------------------------------
# Capa de acceso (inyectable)
# ------------------------------------------------------------
class Acceso(Protocol):
    async def calendario_activo(self, tenant_id: UUID) -> bool: ...
    async def emisor_ok(self, tenant_id: UUID) -> bool: ...
    async def leer_config(self, tenant_id: UUID) -> dict[str, Any] | None: ...
    async def guardar_config(self, tenant_id: UUID, campos: dict[str, Any]) -> None: ...
    async def plantilla(self, tenant_id: UUID, nombre: str) -> dict[str, Any] | None: ...
    async def nombres_plantillas(self, tenant_id: UUID) -> set[str]: ...
    async def guardar_plantilla(
        self, tenant_id: UUID, datos: dict[str, Any], creada_por: UUID | None
    ) -> None: ...
    async def borrar_plantilla(self, tenant_id: UUID, nombre: str) -> None: ...
    async def servicio_meta(self, tenant_id: UUID) -> Any: ...
    def bloqueo(self, tenant_id: UUID) -> Any: ...
    async def apagar_funcion(self, tenant_id: UUID, nombre: str) -> str | None: ...
    async def alertar(
        self, tenant_id: UUID, tipo: str, titulo: str, mensaje: str, datos: dict[str, Any]
    ) -> None: ...


class AccesoBD:
    """Implementación real: asyncpg sin ORM, NeuroAPI y alertas del portal."""

    async def calendario_activo(self, tenant_id: UUID) -> bool:
        fila = await fetch_one(
            "SELECT calendario_activo FROM tenant_servicios WHERE tenant_id = $1", tenant_id
        )
        return bool(fila and fila["calendario_activo"])

    async def emisor_ok(self, tenant_id: UUID) -> bool:
        return await avisos_cita_envio.emisor_ok(tenant_id)  # D6

    async def leer_config(self, tenant_id: UUID) -> dict[str, Any] | None:
        fila = await fetch_one("SELECT * FROM avisos_cita_config WHERE tenant_id = $1", tenant_id)
        return dict(fila) if fila else None

    async def guardar_config(self, tenant_id: UUID, campos: dict[str, Any]) -> None:
        columnas = [c for c in campos if c in COLUMNAS_CONFIG]
        if len(columnas) != len(campos):
            raise ValueError("columna desconocida en avisos_cita_config")
        lista = ", ".join(columnas)
        marcas = ", ".join(f"${i}" for i in range(2, len(columnas) + 2))
        asignar = ", ".join(f"{c} = EXCLUDED.{c}" for c in columnas)
        await fetch_one(
            f"""
            INSERT INTO avisos_cita_config (tenant_id, {lista})
            VALUES ($1, {marcas})
            ON CONFLICT (tenant_id) DO UPDATE SET {asignar}, actualizada_en = NOW()
            RETURNING tenant_id
            """,
            tenant_id,
            *[campos[c] for c in columnas],
        )

    async def plantilla(self, tenant_id: UUID, nombre: str) -> dict[str, Any] | None:
        return await cache.obtener(tenant_id, nombre)

    async def nombres_plantillas(self, tenant_id: UUID) -> set[str]:
        return {f["nombre"] for f in await cache.listar(tenant_id)}

    async def guardar_plantilla(
        self, tenant_id: UUID, datos: dict[str, Any], creada_por: UUID | None
    ) -> None:
        # `creada_por` None = viene de Meta (adoptada/sincronizada): queda como sincronizada.
        await cache.guardar(tenant_id, datos, creada_por=creada_por, sincronizada=creada_por is None)

    async def borrar_plantilla(self, tenant_id: UUID, nombre: str) -> None:
        await cache.borrar_local(tenant_id, nombre)

    async def servicio_meta(self, tenant_id: UUID) -> NeuroApiTemplateService:
        return await NeuroApiTemplateService.desde_tenant(tenant_id)

    @asynccontextmanager
    async def bloqueo(self, tenant_id: UUID) -> AsyncIterator[None]:
        """Un solo aceptar/activar a la vez por negocio (dos clics, dos pestañas)."""
        async with transaccion() as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))",
                f"avisos_cita_config:{tenant_id}",
            )
            yield

    async def apagar_funcion(self, tenant_id: UUID, nombre: str) -> str | None:
        return await cache._apagar_funcion_bd(tenant_id, nombre)

    async def alertar(
        self, tenant_id: UUID, tipo: str, titulo: str, mensaje: str, datos: dict[str, Any]
    ) -> None:
        await cache._alertar_bd(tenant_id, tipo, titulo, mensaje, datos)


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


async def _exigir_calendario(a: Acceso, tenant_id: UUID) -> None:
    if not await a.calendario_activo(tenant_id):
        raise _calendario_inactivo()


async def _exigir_emisor(a: Acceso, tenant_id: UUID) -> None:
    if not await a.emisor_ok(tenant_id):
        raise _sin_numero_emisor()


# ------------------------------------------------------------
# Crear / adoptar plantillas
# ------------------------------------------------------------
async def _asegurar_plantilla(
    a: Acceso,
    tenant_id: UUID,
    tipo: str,
    nombre_vigente: str,
    servicio: Callable[[], Awaitable[Any]],
    usuario_id: UUID | None,
) -> str:
    """
    Deja lista (creada o adoptada) la plantilla del aviso y devuelve su nombre.
    Regla de adopción del core-spec: APPROVED/PENDING y adoptable -> se usa; REJECTED/
    PAUSED/DISABLED o no adoptable -> se crea con el siguiente nombre con sufijo. Si Meta
    contesta duplicado se consulta y se guarda en caché (adoptar). 409 si se agotan los
    sufijos. Cada creada queda en caché al instante (no espera a la otra).
    """
    base = pl.nombre_base(tipo)
    comps = componentes(tipo)
    usados = set(await a.nombres_plantillas(tenant_id))
    nombre = nombre_vigente
    fila = await a.plantilla(tenant_id, nombre)
    while True:
        if fila is None:
            srv = await servicio()
            respuesta, creada = await srv.crear_o_adoptar(
                nombre=nombre, idioma=pl.IDIOMA, categoria=pl.CATEGORIA, componentes=comps
            )
            if creada:
                await a.guardar_plantilla(
                    tenant_id, datos_de_creada(nombre, comps, respuesta), usuario_id
                )
                return nombre
            normalizada = (
                cache.normalizar_meta({"name": nombre, **respuesta}) if respuesta else None
            )
            if normalizada is not None:
                normalizada["nombre"] = nombre
                await a.guardar_plantilla(tenant_id, normalizada, None)
                fila = normalizada
        if fila is not None and pl.debe_adoptar(fila, tipo):
            return nombre
        usados.add(nombre)
        siguiente = pl.siguiente_nombre(base, usados)
        if siguiente is None:
            raise _no_utilizable()
        nombre = siguiente
        fila = await a.plantilla(tenant_id, nombre)


async def _asegurar_funciones(
    a: Acceso, tenant_id: UUID, usuario_id: UUID, tipos: tuple[str, ...] | list[str]
) -> None:
    """
    Crea/adopta la plantilla de cada tipo en orden y activa su función, persistiendo tras
    CADA una. Si una falla: tras haber logrado alguna, la oferta queda 'aceptada' con la
    fallida apagada; si falla la primera no se toca la oferta. Siempre re-lanza el error.
    """
    config = config_efectiva(await a.leer_config(tenant_id))
    perezoso: dict[str, Any] = {}

    async def servicio() -> Any:
        if "s" not in perezoso:
            perezoso["s"] = await a.servicio_meta(tenant_id)
        return perezoso["s"]

    hubo_exito = False
    for tipo in tipos:
        try:
            nombre = await _asegurar_plantilla(
                a, tenant_id, tipo, config[columna_nombre(tipo)], servicio, usuario_id
            )
        except (PlantillaApiError, AvisosError):
            if hubo_exito or await a.leer_config(tenant_id) is not None:
                await a.guardar_config(tenant_id, {columna_activo(tipo): False})
            raise
        hubo_exito = True
        await a.guardar_config(
            tenant_id,
            {
                columna_nombre(tipo): nombre,
                columna_activo(tipo): True,
                "oferta": OFERTA_ACEPTADA,
                "decidido_en": _ahora(),
                "decidido_por": usuario_id,
            },
        )


# ------------------------------------------------------------
# API del servicio
# ------------------------------------------------------------
async def obtener(
    tenant_id: UUID, *, acceso: Acceso | None = None, mensaje: str | None = None
) -> dict[str, Any]:
    """`ConfigAvisos` desde la caché local (sin red). El router sincroniza antes si quiere."""
    a = acceso or AccesoBD()
    config = config_efectiva(await a.leer_config(tenant_id))
    filas = {t: await a.plantilla(tenant_id, config[columna_nombre(t)]) for t in TIPOS_VISTA}
    return vista_de(
        config,
        filas,
        calendario_activo=await a.calendario_activo(tenant_id),
        emisor_ok=await a.emisor_ok(tenant_id),
        mensaje=mensaje,
    )


async def oferta_pendiente(tenant_id: UUID, *, acceso: Acceso | None = None) -> bool:
    """RF-1: calendario activo y oferta 'pendiente' (fila ausente cuenta como pendiente)."""
    a = acceso or AccesoBD()
    if not await a.calendario_activo(tenant_id):
        return False
    fila = await a.leer_config(tenant_id)
    return config_efectiva(fila)["oferta"] == OFERTA_PENDIENTE


async def oferta_pendiente_yo(tenant_id: UUID) -> bool:
    """
    Lo mismo que `oferta_pendiente` para GET /auth/yo, con UN solo JOIN (se consulta en cada
    carga del portal): calendario activo y oferta 'pendiente' (sin fila = pendiente).
    """
    fila = await fetch_one(
        """
        SELECT COALESCE(c.oferta, 'pendiente') = 'pendiente' AS pendiente
        FROM tenant_servicios s
        LEFT JOIN avisos_cita_config c ON c.tenant_id = s.tenant_id
        WHERE s.tenant_id = $1 AND s.calendario_activo
        """,
        tenant_id,
    )
    return bool(fila and fila["pendiente"])


async def aceptar(
    tenant_id: UUID, usuario_id: UUID, *, acceso: Acceso | None = None
) -> dict[str, Any]:
    """
    POST /oferta {aceptar}: pendiente|rechazada -> crea/adopta y activa; aceptada ->
    idempotente (reintenta solo las faltantes). Exige calendario activo y emisor (D6).
    """
    a = acceso or AccesoBD()
    await _exigir_calendario(a, tenant_id)
    await _exigir_emisor(a, tenant_id)
    async with a.bloqueo(tenant_id):
        await _asegurar_funciones(a, tenant_id, usuario_id, TIPOS_CREACION)
    return await obtener(tenant_id, acceso=a, mensaje=MENSAJE_OFERTA_ACEPTADA)


async def rechazar(
    tenant_id: UUID, usuario_id: UUID, *, acceso: Acceso | None = None
) -> dict[str, Any]:
    """
    POST /oferta {rechazar}: apaga ambas funciones -> 'rechazada' (NO borra plantillas) y
    deja una alerta personal. Ya rechazada: idempotente, sin alerta repetida.
    """
    a = acceso or AccesoBD()
    await _exigir_calendario(a, tenant_id)
    async with a.bloqueo(tenant_id):
        actual = config_efectiva(await a.leer_config(tenant_id))["oferta"]
        if actual != OFERTA_RECHAZADA:
            await a.guardar_config(
                tenant_id,
                {
                    "oferta": OFERTA_RECHAZADA,
                    "recordatorio_activo": False,
                    "cancelacion_activo": False,
                    "decidido_en": _ahora(),
                    "decidido_por": usuario_id,
                },
            )
            try:
                await a.alertar(
                    tenant_id,
                    ALERTA_OFERTA_RECHAZADA,
                    "Notificaciones de citas desactivadas",
                    MENSAJE_OFERTA_RECHAZADA,
                    {"oferta": OFERTA_RECHAZADA},
                )
            except Exception:
                log.exception("No se pudo emitir la alerta de oferta rechazada (%s)", tenant_id)
    return await obtener(tenant_id, acceso=a, mensaje=MENSAJE_OFERTA_RECHAZADA)


async def parchear(
    tenant_id: UUID,
    usuario_id: UUID,
    *,
    recordatorio_activo: bool | None = None,
    cancelacion_activo: bool | None = None,
    acceso: Acceso | None = None,
) -> dict[str, Any]:
    """
    PATCH /config. Activar exige calendario y emisor (D6) y deja lista la plantilla (la
    crea con sufijo si falta o cayó); si la oferta no estaba 'aceptada' pasa a serlo.
    Desactivar solo baja el flag. Ningún campo -> ValueError (el router responde 422).
    """
    pedidos = {
        pl.TIPO_RECORDATORIO: recordatorio_activo,
        pl.TIPO_CANCELACION: cancelacion_activo,
    }
    if all(v is None for v in pedidos.values()):
        raise ValueError("parchear necesita al menos un campo")
    a = acceso or AccesoBD()
    await _exigir_calendario(a, tenant_id)
    activar = [t for t in TIPOS_CREACION if pedidos[t] is True]
    desactivar = [t for t in TIPOS_CREACION if pedidos[t] is False]
    if activar:
        await _exigir_emisor(a, tenant_id)
    async with a.bloqueo(tenant_id):
        if desactivar:
            await a.guardar_config(tenant_id, {columna_activo(t): False for t in desactivar})
        if activar:
            await _asegurar_funciones(a, tenant_id, usuario_id, activar)
    return await obtener(tenant_id, acceso=a)


async def _consultar(servicio: Any, nombre: str) -> dict[str, Any] | None:
    try:
        return await servicio.obtener(nombre)
    except PlantillaApiError as e:
        if e.status_code == 404:
            return None
        raise


async def sincronizar(tenant_id: UUID, *, acceso: Acceso | None = None) -> None:
    """
    Conciliación antes de GET /config: por cada función cuya plantilla está PENDING,
    REJECTED o ausente en caché, pregunta a NeuroAPI, actualiza la caché y aplica la misma
    `reaccion_estado` que el webhook. Si Meta ya no la tiene, quita la fila local. Un fallo
    de red o de credenciales no lanza: se responde con lo que hay en caché.
    """
    a = acceso or AccesoBD()
    config = config_efectiva(await a.leer_config(tenant_id))
    servicio: Any = None
    try:
        for tipo in TIPOS_VISTA:
            nombre = config[columna_nombre(tipo)]
            fila = await a.plantilla(tenant_id, nombre)
            if fila is not None and str(fila["estado"]).upper() not in ("PENDING", "REJECTED"):
                continue
            if fila is None and not config[columna_activo(tipo)]:
                continue  # nunca creada y apagada: nada que conciliar (evita una consulta a Meta por GET)
            if servicio is None:
                servicio = await a.servicio_meta(tenant_id)
            remota = await _consultar(servicio, nombre)
            if remota is None:
                if fila is not None:
                    await a.borrar_plantilla(tenant_id, nombre)
                await cache.reaccion_estado(
                    tenant_id, nombre, None, quitada=True,
                    apagar=a.apagar_funcion, alertar=a.alertar,
                )
                continue
            normalizada = cache.normalizar_meta({"name": nombre, **remota})
            if normalizada is None:
                continue
            normalizada["nombre"] = nombre
            await a.guardar_plantilla(tenant_id, normalizada, None)
            await cache.reaccion_estado(
                tenant_id, nombre, normalizada["estado"],
                apagar=a.apagar_funcion, alertar=a.alertar,
            )
    except PlantillaApiError:
        log.warning("No se pudo sincronizar las plantillas de avisos del negocio %s", tenant_id)
