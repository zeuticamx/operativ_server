"""
Estado global de las plantillas de WhatsApp de los avisos de cita (spec 004,
RF-18 y RF-19).

Hay UNA plantilla de cada tipo para todos los negocios (tabla
`plantillas_whatsapp_globales`, sin `tenant_id`). Aquí vive la lógica del
estado: la lectura que se hace en CADA evaluación (nunca cacheada, RF-19), las
reglas puras y el cambio automático por un error de envío con código de Meta
reconocible (plan 2.9). La API de gerencia y los eventos de NeuroAPI (T8)
usan `cambiar_estado`, que escribe el estado y su fila de auditoría juntos.
"""

import json
import logging
import re
from typing import Any
from uuid import UUID

from services.gerencia import registrar_auditoria
from session import execute, fetch_all, fetch_one, transaccion

log = logging.getLogger("operativai.plantillas_whatsapp")

PLANTILLA_CANCELACION = "notificacion_cancelacion"
PLANTILLA_RECORDATORIO = "recordatorio_cita"
NOMBRES_PLANTILLA = (PLANTILLA_CANCELACION, PLANTILLA_RECORDATORIO)

NO_SOLICITADA = "no_solicitada"
PENDIENTE_APROBACION = "pendiente_aprobacion"
APROBADA = "aprobada"
RECHAZADA = "rechazada"
PAUSADA = "pausada"
ESTADOS_PLANTILLA = (NO_SOLICITADA, PENDIENTE_APROBACION, APROBADA, RECHAZADA, PAUSADA)

# Códigos de error de Meta (plan 2.9). Solo los dos primeros mueven el estado.
CODIGO_META_PAUSADA = 132015
CODIGO_META_DESHABILITADA = 132016
CODIGO_META_INEXISTENTE = 132001
_ESTADO_POR_CODIGO = {
    CODIGO_META_PAUSADA: PAUSADA,
    CODIGO_META_DESHABILITADA: RECHAZADA,
}
_CODIGOS_RECONOCIDOS = frozenset(
    {CODIGO_META_PAUSADA, CODIGO_META_DESHABILITADA, CODIGO_META_INEXISTENTE}
)
_CLAVES_CODIGO = frozenset({"code", "error_code", "error_subcode"})
_PATRON_CODIGO = re.compile(r'"(?:code|error_code|error_subcode)"\s*:\s*"?(\d{6})')

ACTOR_SISTEMA = "sistema:avisos_cita"
ACCION_AUDITORIA = "plantilla_whatsapp_estado"
ACTOR_EVENTO_META = "sistema:neuroapi_webhook"
NOTA_EVENTO_META = "webhook NeuroAPI"

# Eventos de Meta que reenvía NeuroAPI en `entry[].changes[]` (T8).
CAMPO_ESTADO_PLANTILLA = "message_template_status_update"
CAMPO_CUENTA = "account_update"
_CAMPOS_DE_PLANTILLA = frozenset({CAMPO_ESTADO_PLANTILLA, CAMPO_CUENTA})
# DISABLED -> rechazada: una plantilla deshabilitada no se recupera sin crear otra.
ESTADO_POR_EVENTO_META = {
    "APPROVED": APROBADA,
    "REJECTED": RECHAZADA,
    "DISABLED": RECHAZADA,
    "PAUSED": PAUSADA,
    "PENDING": PENDIENTE_APROBACION,
}


class PlantillaInexistente(Exception):
    """El nombre no es una de las dos plantillas globales."""


class CambioNoPermitido(Exception):
    """El estado actual no admite el cambio pedido (reintento sobre aprobada o pendiente)."""

    def __init__(self, estado_actual: str) -> None:
        super().__init__(estado_actual)
        self.estado_actual = estado_actual


# ------------------------------------------------------------
# Reglas puras
# ------------------------------------------------------------
def plantilla_aprobada(estado: str | None) -> bool:
    """RF-19: solo `aprobada` permite enviar."""
    return estado == APROBADA


def puede_reintentar(estado: str | None) -> bool:
    """RF-18: el reintento solo se ofrece si no está `aprobada` ni `pendiente_aprobacion`."""
    return estado not in (APROBADA, PENDIENTE_APROBACION)


def _codigo_conocido(valor: Any) -> int | None:
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        numero = valor
    elif isinstance(valor, str) and valor.strip().isdigit():
        numero = int(valor.strip())
    else:
        return None
    return numero if numero in _CODIGOS_RECONOCIDOS else None


def _buscar_codigo(nodo: Any) -> int | None:
    if isinstance(nodo, dict):
        for clave, valor in nodo.items():
            if clave in _CLAVES_CODIGO:
                codigo = _codigo_conocido(valor)
                if codigo is not None:
                    return codigo
        for valor in nodo.values():
            codigo = _buscar_codigo(valor)
            if codigo is not None:
                return codigo
    elif isinstance(nodo, list):
        for valor in nodo:
            codigo = _buscar_codigo(valor)
            if codigo is not None:
                return codigo
    return None


def extraer_codigo_meta(cuerpo: str | None) -> int | None:
    """
    Código de Meta (132001, 132015 o 132016) dentro del cuerpo de un error de
    NeuroAPI, o None si no hay uno reconocible. Mira solo claves de código
    (`code`, `error_code`, `error_subcode`), no cualquier número del texto.
    """
    if not cuerpo:
        return None
    try:
        return _buscar_codigo(json.loads(cuerpo))
    except ValueError:
        pass
    # Cuerpo recortado o no JSON: mismo criterio sobre el texto.
    for coincidencia in _PATRON_CODIGO.finditer(cuerpo):
        codigo = _codigo_conocido(coincidencia.group(1))
        if codigo is not None:
            return codigo
    return None


def estado_por_codigo_meta(codigo: int | None) -> str | None:
    """132015 -> pausada, 132016 -> rechazada, el resto (incluido 132001 o None) -> None."""
    if codigo is None:
        return None
    return _ESTADO_POR_CODIGO.get(codigo)


# ------------------------------------------------------------
# Estado en BD
# ------------------------------------------------------------
async def obtener_estado(nombre: str) -> str:
    """
    Estado actual de la plantilla, leído de la BD en este momento. Una fila
    ausente cuenta como `no_solicitada` (nunca como aprobada).
    """
    fila = await fetch_one(
        "SELECT estado FROM plantillas_whatsapp_globales WHERE nombre = $1", nombre
    )
    return fila["estado"] if fila is not None else NO_SOLICITADA


async def esta_aprobada(nombre: str) -> bool:
    return plantilla_aprobada(await obtener_estado(nombre))


async def aplicar_error_envio(nombre: str, cuerpo_error: str | None) -> str | None:
    """
    Plan 2.9: tras un 400/500 de NeuroAPI, mueve el estado global SOLO si el
    cuerpo trae un código de Meta reconocible (132015 -> pausada, 132016 ->
    rechazada). Con 132001 o sin código no toca nada. Devuelve el estado nuevo
    o None si no cambió. Deja nota `auto: error de envío <código>`,
    `actualizada_por` NULL y una fila en `gerencia_auditoria`.
    """
    codigo = extraer_codigo_meta(cuerpo_error)
    nuevo = estado_por_codigo_meta(codigo)
    if nuevo is None:
        return None
    anterior = await obtener_estado(nombre)
    if anterior == nuevo:
        return None
    await execute(
        """
        UPDATE plantillas_whatsapp_globales
        SET estado = $2, nota = $3, actualizada_en = NOW(), actualizada_por = NULL
        WHERE nombre = $1
        """,
        nombre,
        nuevo,
        f"auto: error de envío {codigo}",
    )
    await execute(
        """
        INSERT INTO gerencia_auditoria (actor_email, accion, detalle)
        VALUES ($1, $2, $3::jsonb)
        """,
        ACTOR_SISTEMA,
        ACCION_AUDITORIA,
        # dict, no str: el códec jsonb del pool ya serializa.
        {
            "plantilla": nombre,
            "anterior": anterior,
            "nuevo": nuevo,
            "origen": "error_envio",
            "codigo_meta": codigo,
        },
    )
    log.warning("Plantilla %s: %s -> %s por error de envío %s", nombre, anterior, nuevo, codigo)
    return nuevo


# ------------------------------------------------------------
# Cambio de estado con auditoría (T8)
# ------------------------------------------------------------
async def listar_plantillas() -> list[dict[str, Any]]:
    """Las dos plantillas con su estado, fecha y la cuenta de gerencia que la cambió."""
    filas = await fetch_all(
        """
        SELECT p.nombre, p.estado, p.actualizada_en, p.actualizada_por,
               g.email AS actualizada_por_email, p.nota
        FROM plantillas_whatsapp_globales p
        LEFT JOIN gerencia_users g ON g.id = p.actualizada_por
        ORDER BY p.nombre
        """
    )
    resultado = []
    for fila in filas:
        datos = dict(fila)
        datos["puede_reintentar"] = puede_reintentar(datos["estado"])
        resultado.append(datos)
    return resultado


async def cambiar_estado(
    nombre: str,
    nuevo: str,
    *,
    nota: str | None,
    actor_email: str,
    actor_id: UUID | None,
    origen: str,
    solo_si_reintentable: bool = False,
) -> str:
    """
    Fija el estado de una plantilla y deja la fila en `gerencia_auditoria` en la
    misma transacción. Devuelve el estado anterior. `actor_id` None = lo hizo el
    sistema (`actualizada_por` NULL). Con `solo_si_reintentable`, levanta
    `CambioNoPermitido` si el estado actual es `aprobada` o `pendiente_aprobacion`
    (lectura y cambio van bajo el mismo bloqueo de fila).
    """
    if nombre not in NOMBRES_PLANTILLA:
        raise PlantillaInexistente(nombre)
    async with transaccion() as conn:
        fila = await conn.fetchrow(
            "SELECT estado FROM plantillas_whatsapp_globales WHERE nombre = $1 FOR UPDATE",
            nombre,
        )
        if fila is None:
            raise PlantillaInexistente(nombre)
        anterior = fila["estado"]
        if solo_si_reintentable and not puede_reintentar(anterior):
            raise CambioNoPermitido(anterior)
        await conn.execute(
            """
            UPDATE plantillas_whatsapp_globales
            SET estado = $2, nota = $3, actualizada_en = NOW(), actualizada_por = $4
            WHERE nombre = $1
            """,
            nombre,
            nuevo,
            nota,
            actor_id,
        )
        await registrar_auditoria(
            actor_email=actor_email,
            actor_portal_user_id=actor_id,
            accion=ACCION_AUDITORIA,
            tenant_id=None,
            detalle={"plantilla": nombre, "anterior": anterior, "nuevo": nuevo, "origen": origen},
            conn=conn,
        )
    return anterior


# ------------------------------------------------------------
# Eventos de Meta reenviados por NeuroAPI (T8)
# ------------------------------------------------------------
def _cambios(evento: Any) -> list[dict[str, Any]]:
    if not isinstance(evento, dict):
        return []
    entradas = evento.get("entry")
    if not isinstance(entradas, list):
        return []
    cambios: list[dict[str, Any]] = []
    for entrada in entradas:
        if not isinstance(entrada, dict) or not isinstance(entrada.get("changes"), list):
            continue
        cambios.extend(c for c in entrada["changes"] if isinstance(c, dict))
    return cambios


def es_evento_de_plantilla(evento: Any) -> bool:
    """
    True si el sobre de Meta trae `message_template_status_update` o
    `account_update` y NINGÚN otro campo: un sobre mezclado con mensajes
    entrantes sigue el flujo normal para no perder el mensaje.
    """
    cambios = _cambios(evento)
    return bool(cambios) and all(_es_cambio_de_plantilla(c) for c in cambios)


def _es_cambio_de_plantilla(cambio: dict[str, Any]) -> bool:
    campo = cambio.get("field")
    if campo not in _CAMPOS_DE_PLANTILLA:
        return False
    # Un `account_update` con `session_id` es el aviso de alta de una línea
    # (sobre de Meta de `whatsapp.connected`): sigue su flujo de siempre.
    if campo == CAMPO_CUENTA:
        valor = cambio.get("value")
        if isinstance(valor, dict):
            datos = valor.get("data")
            if valor.get("session_id") or (isinstance(datos, dict) and datos.get("session_id")):
                return False
    return True


def traducir_evento_meta(evento: Any) -> list[tuple[str, str]]:
    """
    Pura. De cada `message_template_status_update` válido saca
    `(nombre, estado_nuevo)`. Ignora (con log) valores de `event` desconocidos y
    nombres que no son una de las dos plantillas; `account_update` no produce nada.
    """
    resultado: list[tuple[str, str]] = []
    for cambio in _cambios(evento):
        campo = cambio.get("field")
        valor = cambio.get("value")
        if campo == CAMPO_CUENTA:
            log.info("Evento account_update de Meta recibido (solo registro)")
            continue
        if campo != CAMPO_ESTADO_PLANTILLA or not isinstance(valor, dict):
            continue
        nombre = str(valor.get("message_template_name") or "").strip()
        crudo = str(valor.get("event") or "").strip().upper()
        if nombre not in NOMBRES_PLANTILLA:
            log.info("Evento de plantilla ajena %r ignorado", nombre)
            continue
        nuevo = ESTADO_POR_EVENTO_META.get(crudo)
        if nuevo is None:
            log.warning("Evento de plantilla %s con valor desconocido %r ignorado", nombre, crudo)
            continue
        resultado.append((nombre, nuevo))
    return resultado


async def aplicar_evento_meta(evento: Any) -> list[tuple[str, str]]:
    """
    Aplica los cambios de estado de un evento de Meta. Devuelve los
    `(nombre, estado_nuevo)` que de verdad cambiaron. `actualizada_por` queda
    NULL, nota `webhook NeuroAPI` y fila de auditoría con actor de sistema.
    """
    aplicados: list[tuple[str, str]] = []
    for nombre, nuevo in traducir_evento_meta(evento):
        if await obtener_estado(nombre) == nuevo:
            continue
        anterior = await cambiar_estado(
            nombre,
            nuevo,
            nota=NOTA_EVENTO_META,
            actor_email=ACTOR_EVENTO_META,
            actor_id=None,
            origen="evento_meta",
        )
        log.warning("Plantilla %s: %s -> %s por evento de Meta", nombre, anterior, nuevo)
        aplicados.append((nombre, nuevo))
    return aplicados
