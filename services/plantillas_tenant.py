"""
Caché local de las plantillas de WhatsApp de cada negocio (tabla
`plantillas_whatsapp_tenant`, sql/60). La fuente de verdad es Meta: aquí se
guarda lo que NeuroAPI devolvió al sincronizar, al crear, y los cambios de
estado que Meta empuja por `message_template_status_update` (sin polling).

Toda consulta filtra por `tenant_id`: una plantilla de otro negocio no existe
para quien pregunta (la ruta responde 404).
"""

import logging
from typing import Any, Awaitable, Callable
from uuid import UUID

from config import settings
from services import plantillas_reglas as reglas
from services import plantillas_whatsapp as _globales  # solo sus funciones puras de códigos de Meta
from session import execute, fetch_all, fetch_one

log = logging.getLogger("operativai.plantillas_tenant")

# `event` de Meta -> estado de la caché. FLAGGED/PENDING_DELETION y otros no mueven nada.
ESTADO_POR_EVENTO = {
    "APPROVED": "APPROVED",
    "REINSTATED": "APPROVED",
    "REJECTED": "REJECTED",
    "DISABLED": "DISABLED",
    "PAUSED": "PAUSED",
    "PENDING": "PENDING",
}
CAMPO_ESTADO = "message_template_status_update"

# Spec 007: estados de Meta con los que una plantilla ya no sirve para avisar al cliente.
ESTADOS_INUTILIZABLES = ("REJECTED", "PAUSED", "DISABLED")
TIPO_ALERTA_INUTILIZABLE = "avisos_cita_plantilla_inutilizable"
_ETIQUETA_AVISO = {"recordatorio": "recordatorio", "cancelacion": "cancelación"}
ESTADO_QUITADA = "eliminada"


# ------------------------------------------------------------
# Puras
# ------------------------------------------------------------
def normalizar_meta(crudo: dict[str, Any]) -> dict[str, Any] | None:
    """Forma de Meta (name/language/category/status/components/id) -> campos de la caché."""
    nombre = str(crudo.get("name") or "").strip()
    if not nombre:
        return None
    categoria = str(crudo.get("category") or "").upper()
    estado = str(crudo.get("status") or "PENDING").upper()
    lenguaje = crudo.get("language")
    if isinstance(lenguaje, dict):
        lenguaje = lenguaje.get("code")
    motivo = crudo.get("rejected_reason") or crudo.get("reason")
    componentes = crudo.get("components")
    return {
        "nombre": nombre,
        "idioma": str(lenguaje or "es_MX"),
        "categoria": categoria if categoria in reglas.CATEGORIAS else "UTILITY",
        "estado": estado if estado in reglas.ESTADOS else "PENDING",
        "motivo_rechazo": None if not motivo or str(motivo).upper() == "NONE" else str(motivo),
        "componentes": componentes if isinstance(componentes, list) else [],
        "meta_template_id": str(crudo["id"]) if crudo.get("id") else None,
    }


def a_salida(fila: dict[str, Any]) -> dict[str, Any]:
    comps = fila["componentes"]
    return {
        "nombre": fila["nombre"],
        "idioma": fila["idioma"],
        "categoria": fila["categoria"],
        "estado": fila["estado"],
        "motivo_rechazo": fila["motivo_rechazo"],
        "componentes": comps,
        "variables_cuerpo": reglas.numero_de_variables(comps),
        "variable_encabezado": bool(reglas.variables_de(reglas.texto_de(comps, "HEADER"))),
        "actualizada_en": fila["actualizada_en"],
        "sincronizada_en": fila["sincronizada_en"],
    }


def contradice_evento(candidata: dict[str, Any], meta_id: str | None, waba_id: str | None) -> bool:
    """
    Pura (spec 007, RF-11). True si la candidata ya tiene guardado un `meta_template_id` o un
    `waba_id` y el evento trae otro distinto. Un dato ausente (NULL, aún no aprendido) o sin
    contraparte en el evento nunca contradice.
    """
    for guardado, del_evento in ((candidata.get("meta_template_id"), meta_id), (candidata.get("waba_id"), waba_id)):
        if guardado and del_evento and str(guardado) != str(del_evento):
            return True
    return False


def elegir_candidata(
    candidatas: list[dict[str, Any]], meta_id: str | None, waba_id: str | None
) -> dict[str, Any] | None:
    """
    Dueña de un evento de Meta, que solo trae WABA, nombre, idioma e id de
    plantilla. Prioridad: mismo `meta_template_id`; luego mismo `waba_id`; si
    ninguno decide, solo vale cuando hay UNA candidata y sus `waba_id` /
    `meta_template_id` guardados no contradicen al evento (RF-11: un evento de otra
    cuenta con el mismo nombre no es de ese negocio). Con varios negocios
    homónimos y sin dato que desempate, no se adivina (se devuelve None).
    """
    if meta_id:
        por_id = [c for c in candidatas if c.get("meta_template_id") == meta_id]
        if len(por_id) == 1:
            return por_id[0]
    if waba_id:
        por_waba = [
            c for c in candidatas if c.get("waba_id") == waba_id and not contradice_evento(c, meta_id, waba_id)
        ]
        if len(por_waba) == 1:
            return por_waba[0]
    if len(candidatas) == 1 and not contradice_evento(candidatas[0], meta_id, waba_id):
        return candidatas[0]
    return None


def entradas_de_negocio(evento: Any, waba_global: str | None) -> list[Any]:
    """
    Pura (spec 007, RF-11). Las `entry` del sobre que puede aplicar el handler de negocio:
    descarta las de la cuenta global (`evento_es_de_cuenta_global` por entry, así un sobre con
    varios WABA aplica a cada handler solo lo suyo). Con `waba_global` vacío no hay forma de
    identificarlas y no se descarta ninguna.
    """
    entradas = evento.get("entry") if isinstance(evento, dict) else None
    if not isinstance(entradas, list):
        return []
    return [e for e in entradas if not _globales.evento_es_de_cuenta_global({"entry": [e]}, waba_global, ())]


def cambios_de_estado(evento: Any) -> list[dict[str, Any]]:
    """De un sobre de Meta saca cada `message_template_status_update` con estado conocido."""
    if not isinstance(evento, dict) or not isinstance(evento.get("entry"), list):
        return []
    salida: list[dict[str, Any]] = []
    for entrada in evento["entry"]:
        if not isinstance(entrada, dict) or not isinstance(entrada.get("changes"), list):
            continue
        for cambio in entrada["changes"]:
            if not isinstance(cambio, dict) or cambio.get("field") != CAMPO_ESTADO:
                continue
            v = cambio.get("value")
            if not isinstance(v, dict):
                continue
            nuevo = ESTADO_POR_EVENTO.get(str(v.get("event") or "").strip().upper())
            nombre = str(v.get("message_template_name") or "").strip()
            if nuevo is None or not nombre:
                continue
            motivo = v.get("reason")
            salida.append(
                {
                    "nombre": nombre,
                    "idioma": str(v.get("message_template_language") or "") or None,
                    "meta_id": str(v["message_template_id"]) if v.get("message_template_id") else None,
                    "waba_id": str(entrada["id"]) if entrada.get("id") else None,
                    "estado": nuevo,
                    "motivo": None if not motivo or str(motivo).upper() == "NONE" else str(motivo),
                }
            )
    return salida


def estado_inutilizable(estado: str | None) -> bool:
    return str(estado or "").upper() in ESTADOS_INUTILIZABLES


def mensaje_inutilizable(tipo: str, estado: str) -> str:
    """Texto fijo de la alerta `avisos_cita_plantilla_inutilizable` (core-spec 007)."""
    etiqueta = _ETIQUETA_AVISO.get(tipo, tipo)
    return (
        f"La plantilla de {etiqueta} de tu negocio ya no está disponible ({estado}). "
        "Se desactivó esa notificación. Puedes recrearla desde Calendario."
    )


def estado_por_error_de_envio(cuerpo: str | None) -> str | None:
    """
    Estado de caché que implica el error de un envío de plantilla, leído del código de Meta
    en el cuerpo: 132015 -> PAUSED, 132016 -> DISABLED. Sin código reconocible (un 500
    genérico, 132001 o nada) -> None: no se toca la plantilla por un fallo que puede ser
    pasajero (mismo criterio que el estado global, plan 2.9 de spec 004).
    """
    codigo = _globales.extraer_codigo_meta(cuerpo)
    if codigo == _globales.CODIGO_META_PAUSADA:
        return "PAUSED"
    if codigo == _globales.CODIGO_META_DESHABILITADA:
        return "DISABLED"
    return None


FuncionApagar = Callable[[UUID, str], Awaitable[str | None]]
FuncionAlertar = Callable[[UUID, str, str, str, dict[str, Any]], Awaitable[None]]
FuncionActualizarEstado = Callable[[UUID, str, str], Awaitable[bool]]


async def reaccion_estado(
    tenant_id: UUID,
    nombre: str,
    estado: str | None,
    *,
    quitada: bool = False,
    apagar: FuncionApagar | None = None,
    alertar: FuncionAlertar | None = None,
) -> str | None:
    """
    ÚNICO punto por el que el webhook de Meta, la sincronización y el DELETE avisan de
    que una plantilla cambió (spec 007, RF-6). Si el estado es REJECTED/PAUSED/DISABLED,
    o la fila se quitó (`quitada`), y esa plantilla es la vigente de una función de
    aviso de cita ACTIVA: apaga la función y alerta a gerencia. `apagar` es atómico
    (solo devuelve el tipo si la función estaba activa), así que la alerta sale UNA
    sola vez aunque lleguen varios eventos. Función ya apagada u otra plantilla
    -> no hace nada. Devuelve el tipo apagado o None. Nunca lanza por la alerta.
    """
    if not quitada and not estado_inutilizable(estado):
        return None
    tipo = await (apagar or _apagar_funcion_bd)(tenant_id, nombre)
    if tipo is None:
        return None
    etiqueta_estado = ESTADO_QUITADA if quitada else str(estado).upper()
    try:
        await (alertar or _alertar_bd)(
            tenant_id,
            TIPO_ALERTA_INUTILIZABLE,
            "Una notificación de citas se desactivó",
            mensaje_inutilizable(tipo, etiqueta_estado),
            {"tipo": tipo, "plantilla": nombre, "estado": etiqueta_estado},
        )
    except Exception:
        log.exception("No se pudo alertar la plantilla inutilizable %r del negocio %s", nombre, tenant_id)
    return tipo


async def marcar_estado_por_error(
    tenant_id: UUID,
    nombre: str,
    estado: str = "DISABLED",
    *,
    actualizar: FuncionActualizarEstado | None = None,
    apagar: FuncionApagar | None = None,
    alertar: FuncionAlertar | None = None,
) -> str | None:
    """
    El envío devolvió `plantilla_no_disponible`: Meta ya no acepta esa plantilla aunque
    la caché diga APPROVED. Marca la fila local (si existe) y aplica `reaccion_estado`.
    Sin fila local no hace nada (no es plantilla de este negocio).
    """
    if not await (actualizar or _actualizar_estado_bd)(tenant_id, nombre, estado):
        return None
    return await reaccion_estado(tenant_id, nombre, estado, apagar=apagar, alertar=alertar)


# ------------------------------------------------------------
# BD
# ------------------------------------------------------------
async def _actualizar_estado_bd(tenant_id: UUID, nombre: str, estado: str) -> bool:
    fila = await fetch_one(
        """
        UPDATE plantillas_whatsapp_tenant
        SET estado = $3, actualizada_en = NOW()
        WHERE tenant_id = $1 AND nombre = $2
        RETURNING nombre
        """,
        tenant_id,
        nombre,
        estado,
    )
    return fila is not None


async def _apagar_funcion_bd(tenant_id: UUID, nombre: str) -> str | None:
    """
    Apaga la función de aviso de cita cuya plantilla vigente es `nombre`, SOLO si estaba
    activa (una sola sentencia con bloqueo de fila: dos llamadas simultáneas no apagan
    dos veces). Devuelve 'recordatorio' | 'cancelacion', o None si no había nada que apagar.
    """
    fila = await fetch_one(
        """
        WITH antes AS (
            SELECT tenant_id,
                   (recordatorio_activo AND nombre_plantilla_recordatorio = $2) AS rec,
                   (cancelacion_activo  AND nombre_plantilla_cancelacion  = $2) AS can
            FROM avisos_cita_config
            WHERE tenant_id = $1
            FOR UPDATE
        )
        UPDATE avisos_cita_config c
        SET recordatorio_activo = c.recordatorio_activo AND NOT antes.rec,
            cancelacion_activo  = c.cancelacion_activo  AND NOT antes.can,
            actualizada_en = NOW()
        FROM antes
        WHERE c.tenant_id = antes.tenant_id AND (antes.rec OR antes.can)
        RETURNING antes.rec AS rec, antes.can AS can
        """,
        tenant_id,
        nombre,
    )
    if fila is None:
        return None
    return "recordatorio" if fila["rec"] else "cancelacion"


async def _alertar_bd(
    tenant_id: UUID, tipo: str, titulo: str, mensaje: str, datos: dict[str, Any]
) -> None:
    # Import perezoso: realtime arrastra Socket.IO y esta capa no lo necesita para el resto.
    from realtime import avisar_gerencia

    await avisar_gerencia(tenant_id, tipo, titulo, mensaje, datos)


async def listar(
    tenant_id: UUID, *, categoria: str | None = None, estado: str | None = None
) -> list[dict[str, Any]]:
    filas = await fetch_all(
        """
        SELECT * FROM plantillas_whatsapp_tenant
        WHERE tenant_id = $1
          AND ($2::text IS NULL OR categoria = $2)
          AND ($3::text IS NULL OR estado = $3)
        ORDER BY actualizada_en DESC, nombre
        """,
        tenant_id,
        categoria,
        estado,
    )
    return [dict(f) for f in filas]


async def obtener(tenant_id: UUID, nombre: str) -> dict[str, Any] | None:
    fila = await fetch_one(
        """
        SELECT * FROM plantillas_whatsapp_tenant
        WHERE tenant_id = $1 AND nombre = $2
        ORDER BY actualizada_en DESC LIMIT 1
        """,
        tenant_id,
        nombre,
    )
    return dict(fila) if fila else None


async def guardar(
    tenant_id: UUID, datos: dict[str, Any], *, creada_por: UUID | None = None, sincronizada: bool
) -> None:
    """Inserta o actualiza por (tenant, nombre, idioma)."""
    await execute(
        """
        INSERT INTO plantillas_whatsapp_tenant
            (tenant_id, nombre, idioma, categoria, estado, motivo_rechazo, componentes,
             meta_template_id, creada_por, sincronizada_en)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,
                CASE WHEN $10 THEN NOW() END)
        ON CONFLICT (tenant_id, nombre, idioma) DO UPDATE SET
            categoria = EXCLUDED.categoria,
            estado = EXCLUDED.estado,
            motivo_rechazo = EXCLUDED.motivo_rechazo,
            componentes = CASE WHEN jsonb_array_length(EXCLUDED.componentes) > 0
                               THEN EXCLUDED.componentes
                               ELSE plantillas_whatsapp_tenant.componentes END,
            meta_template_id = COALESCE(EXCLUDED.meta_template_id,
                                        plantillas_whatsapp_tenant.meta_template_id),
            actualizada_en = NOW(),
            sincronizada_en = COALESCE(EXCLUDED.sincronizada_en,
                                       plantillas_whatsapp_tenant.sincronizada_en)
        """,
        tenant_id,
        datos["nombre"],
        datos["idioma"],
        datos["categoria"],
        datos["estado"],
        datos.get("motivo_rechazo"),
        # list, no str: el códec jsonb del pool ya serializa.
        datos["componentes"],
        datos.get("meta_template_id"),
        creada_por,
        sincronizada,
    )


async def sincronizar(
    tenant_id: UUID,
    remotas: list[dict[str, Any]],
    *,
    lista_completa: bool,
    apagar: FuncionApagar | None = None,
    alertar: FuncionAlertar | None = None,
) -> int:
    """
    Vuelca el catálogo de Meta en la caché. Con `lista_completa` (NeuroAPI devolvió
    menos del límite pedido, o sea, todo) borra las filas locales que Meta ya no tiene
    y, por cada una quitada, aplica `reaccion_estado(quitada=True)` (spec 007, RF-6).
    Devuelve cuántas plantillas quedaron sincronizadas.
    """
    normalizadas = [n for n in (normalizar_meta(r) for r in remotas) if n]
    for n in normalizadas:
        await guardar(tenant_id, n, sincronizada=True)
    if lista_completa:
        claves = [f"{n['nombre']}|{n['idioma']}" for n in normalizadas]
        quitadas = await fetch_all(
            """
            DELETE FROM plantillas_whatsapp_tenant
            WHERE tenant_id = $1 AND NOT ((nombre || '|' || idioma) = ANY($2::text[]))
            RETURNING nombre
            """,
            tenant_id,
            claves,
        )
        for nombre in {f["nombre"] for f in quitadas}:
            await reaccion_estado(tenant_id, nombre, None, quitada=True, apagar=apagar, alertar=alertar)
    return len(normalizadas)


async def borrar_local(tenant_id: UUID, nombre: str) -> None:
    await execute(
        "DELETE FROM plantillas_whatsapp_tenant WHERE tenant_id = $1 AND nombre = $2",
        tenant_id,
        nombre,
    )


async def quitar(
    tenant_id: UUID,
    nombre: str,
    *,
    apagar: FuncionApagar | None = None,
    alertar: FuncionAlertar | None = None,
) -> str | None:
    """Borra la fila de la caché y avisa a `reaccion_estado` (DELETE de la API). Devuelve el tipo apagado."""
    await borrar_local(tenant_id, nombre)
    return await reaccion_estado(tenant_id, nombre, None, quitada=True, apagar=apagar, alertar=alertar)


async def aplicar_evento_meta(
    evento: Any,
    *,
    apagar: FuncionApagar | None = None,
    alertar: FuncionAlertar | None = None,
    waba_global: str | None = None,
) -> list[tuple[UUID, str, str]]:
    """
    Aplica los `message_template_status_update` a la caché de los negocios y llama a
    `reaccion_estado` (REJECTED/PAUSED/DISABLED con función activa -> función off + alerta
    una sola vez; el resto no hace nada). El negocio dueño se identifica por nombre +
    idioma y se desempata con `meta_template_id` / WABA (`elegir_candidata`).
    Devuelve `(tenant_id, nombre, estado_nuevo)` de lo que cambió. No crea filas:
    una plantilla que ningún negocio tiene en la caché (p. ej. las de la cuenta
    global) se ignora. Sin dueño único, se registra y se descarta.

    Aislamiento bidireccional (RF-11/D9): las `entry` del WABA global
    (`AVISOS_CUENTA_GLOBAL_WABA_ID`, o `waba_global` si se pasa) se ignoran por completo, y un
    evento que contradiga el `waba_id`/`meta_template_id` guardados se descarta con `warning`.
    Con el WABA global vacío no se descarta nada por «ser global» y cada evento aplicado deja
    un `warning` (no se puede distinguir un evento global de uno del negocio).
    """
    waba = waba_global if waba_global is not None else settings.AVISOS_CUENTA_GLOBAL_WABA_ID
    sin_waba_global = not str(waba or "").strip()
    propias = entradas_de_negocio(evento, waba)
    if len(propias) < len(evento["entry"] if isinstance(evento, dict) and isinstance(evento.get("entry"), list) else []):
        log.info("Evento de estado de plantilla de la cuenta global: ignorado por el handler de negocio")
    aplicados: list[tuple[UUID, str, str]] = []
    for c in cambios_de_estado({"entry": propias}):
        filas = await fetch_all(
            """
            SELECT tenant_id, nombre, idioma, estado, meta_template_id, waba_id
            FROM plantillas_whatsapp_tenant
            WHERE nombre = $1 AND ($2::text IS NULL OR idioma = $2)
            """,
            c["nombre"],
            c["idioma"],
        )
        if not filas:
            continue
        duenio = elegir_candidata([dict(f) for f in filas], c["meta_id"], c["waba_id"])
        if duenio is None:
            if len(filas) == 1 and contradice_evento(dict(filas[0]), c["meta_id"], c["waba_id"]):
                log.warning(
                    "Evento de plantilla %r descartado: su WABA/id contradice los de la plantilla del negocio",
                    c["nombre"],
                )
            else:
                log.warning(
                    "Evento de plantilla %r sin dueño único (%d candidatas): ignorado",
                    c["nombre"],
                    len(filas),
                )
            continue
        if sin_waba_global:
            log.warning(
                "Evento de plantilla %r aplicado con el WABA global vacío (AVISOS_CUENTA_GLOBAL_WABA_ID): no se puede "
                "descartar un evento de la cuenta global (solo rige la regla de contradicción)",
                c["nombre"],
            )
        if duenio["estado"] == c["estado"]:
            # Sin cambio de caché; igual se reacciona (idempotente y atómico: la alerta no se
            # duplica) por si un evento anterior actualizó la fila pero no llegó a apagar.
            await reaccion_estado(
                duenio["tenant_id"], duenio["nombre"], c["estado"], apagar=apagar, alertar=alertar
            )
            continue
        await execute(
            """
            UPDATE plantillas_whatsapp_tenant
            SET estado = $4, motivo_rechazo = $5, actualizada_en = NOW(),
                meta_template_id = COALESCE(meta_template_id, $6),
                waba_id = COALESCE(waba_id, $7)
            WHERE tenant_id = $1 AND nombre = $2 AND idioma = $3
            """,
            duenio["tenant_id"],
            duenio["nombre"],
            duenio["idioma"],
            c["estado"],
            c["motivo"],
            c["meta_id"],
            c["waba_id"],
        )
        aplicados.append((duenio["tenant_id"], duenio["nombre"], c["estado"]))
        await reaccion_estado(
            duenio["tenant_id"], duenio["nombre"], c["estado"], apagar=apagar, alertar=alertar
        )
    return aplicados
