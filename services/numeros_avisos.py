"""
Avisos a la gerencia del negocio por cambios en sus números (spec 005a, RF-20).

Un aviso AGRUPADO por acción (D29): lista los números afectados con su estado,
el motivo y qué puede hacer la gerencia. Sale a cada cuenta activa owner o
superadmin del negocio como alerta PERSONAL del portal; el correo espejo
(services/correo_espejo.py) lo manda `realtime.broadcast_alerta` por su cuenta.

Tipos de alerta (sql/60 y schemas.TipoAlerta):
    numeros_pausados                 pausa por cualquiera de los casos de RF-11
    numeros_recortados               recorte de un límite de plan (RF-10)
    numero_configuracion_incompleta  número (o Instagram/Facebook) incompleto (RF-8, RF-17, RF-22)

NO hay aviso por la pausa manual ni por una desvinculación pedida por la
gerencia (RF-20): esos servicios no llaman a este módulo.

PUNTOS DE EXTENSIÓN para la reevaluación (T8): `avisar_pausa`, `avisar_recorte` y
`avisar_incompleto` están listos y no los llama nadie más que T3 (el
incompleto). El recorte de límites ocurre en un trigger de la base antes de que
el servicio lo vea, así que T8 tiene que comparar la configuración antes y
después de recortar para saber qué números avisar.

Best-effort: un fallo al avisar solo se registra; la acción que lo causó ya se
completó. Llamar después de confirmar la transacción.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID

import asyncpg

from services.numeros_bitacora import ultimos_digitos

log = logging.getLogger("operativai.numeros_avisos")

TIPO_PAUSADOS = "numeros_pausados"
TIPO_RECORTADOS = "numeros_recortados"
TIPO_INCOMPLETO = "numero_configuracion_incompleta"
TIPOS_ALERTA = (TIPO_PAUSADOS, TIPO_RECORTADOS, TIPO_INCOMPLETO)

# Quién recibe el aviso: la gerencia del negocio (D9). Un test comprueba que es
# deps.ROLES_GERENCIA.
ROLES_AVISO: tuple[str, ...] = ("owner", "superadmin")

# Los motivos de pausa de la bitácora (RF-11, casos 1 a 7) y cómo se leen.
MOTIVOS_PAUSA: dict[str, str] = {
    "pausa_por_cambio_de_plan": "cambio de plan",
    "pausa_por_fin_de_suscripcion": "fin de la suscripción",
    "pausa_por_fin_de_prueba": "fin de la prueba",
    "pausa_por_pago_de_prueba": "paso de la prueba a un plan de pago",
    "pausa_por_despliegue": "actualización de las reglas de la plataforma",
    "pausa_por_cupo_bajado_por_plataforma": "cambio del cupo de tu plan por la plataforma",
    "pausa_por_plan_mas_bajo_recalculado": "cambio en el plan que rige a los negocios sin suscripción vigente",
}

# Por qué un número queda sin configuración completa (RF-8, RF-17, RF-22).
MOTIVOS_INCOMPLETO: dict[str, str] = {
    "edicion": "se guardó sin nombre de agente o sin instrucciones (system prompt)",
    "reactivacion": "se reactivó sin nombre de agente o sin instrucciones (system prompt)",
    "plan": "un cambio de plan lo mantiene sin nombre de agente o sin instrucciones (system prompt)",
    "despliegue": "no tiene nombre de agente o instrucciones (system prompt) y deja de responder",
}

ACCION_PAUSA = (
    "Los números pausados conservan su configuración, herramientas e historial. "
    "Puedes reactivarlos desde Canales si tu plan tiene plazas libres, o mejorar tu plan."
)
ACCION_RECORTE = (
    "Revisa la configuración de esos números en Agente: el plan limita el modelo y el historial. "
    "Mejora tu plan si necesitas más."
)
ACCION_INCOMPLETO = (
    "Completa el nombre del agente y las instrucciones (system prompt) en Agente. "
    "Mientras tanto no responde y no puedes contestar a mano desde el portal."
)

_MAX_MENSAJE = 1000


@dataclass(frozen=True)
class NumeroAfectado:
    """Un número (o Instagram y Facebook) al que alcanza el aviso."""

    etiqueta: str
    estado: str
    id: Optional[UUID] = None


@dataclass(frozen=True)
class Aviso:
    tipo: str
    titulo: str
    mensaje: str
    datos: dict[str, Any] = field(default_factory=dict)


def etiqueta_numero(telefono_visible: Optional[str], phone_number_id: Optional[str]) -> str:
    """Cómo se nombra un número en un aviso: su teléfono visible o sus 4 últimos caracteres."""
    visible = (telefono_visible or "").strip()
    if visible:
        return visible
    return f"…{ultimos_digitos(phone_number_id or '')}"


def afectado_desde_fila(fila: Mapping[str, Any]) -> NumeroAfectado:
    """Una fila de `agentes_canal` (id, telefono_visible, phone_number_id, estado) como afectado."""
    return NumeroAfectado(
        etiqueta=etiqueta_numero(fila.get("telefono_visible"), fila.get("phone_number_id")),
        estado=str(fila["estado"]),
        id=fila.get("id"),
    )


def _lista(numeros: Sequence[NumeroAfectado]) -> str:
    return ", ".join(f"{n.etiqueta} ({n.estado})" for n in numeros)


def _datos(negocio: str, tenant_id: Optional[UUID], motivo: str, numeros: Sequence[NumeroAfectado], accion: str) -> dict[str, Any]:
    return {
        "negocio": negocio,
        "tenant_id": str(tenant_id) if tenant_id else None,
        "motivo": motivo,
        "numeros": [
            {"id": str(n.id) if n.id else None, "etiqueta": n.etiqueta, "estado": n.estado} for n in numeros
        ],
        "accion": accion,
    }


def _armar(tipo: str, titulo: str, negocio: str, cuerpo: str, accion: str, datos: dict[str, Any]) -> Aviso:
    mensaje = f"{negocio}: {cuerpo} {accion}"
    return Aviso(tipo=tipo, titulo=titulo, mensaje=mensaje[:_MAX_MENSAJE], datos=datos)


def construir_aviso_pausa(
    negocio: str,
    numeros: Sequence[NumeroAfectado],
    motivo: str,
    *,
    tenant_id: Optional[UUID] = None,
) -> Aviso:
    """RF-20: pausa de números por alguno de los casos de RF-11. `motivo` es el de la bitácora."""
    if motivo not in MOTIVOS_PAUSA:
        raise ValueError(f"Motivo de pausa desconocido: {motivo!r}")
    cuerpo = f"se pausaron {len(numeros)} número(s) de WhatsApp por {MOTIVOS_PAUSA[motivo]}: {_lista(numeros)}."
    return _armar(
        TIPO_PAUSADOS,
        "Se pausaron números de WhatsApp",
        negocio,
        cuerpo,
        ACCION_PAUSA,
        _datos(negocio, tenant_id, motivo, numeros, ACCION_PAUSA),
    )


def construir_aviso_recorte(
    negocio: str,
    numeros: Sequence[NumeroAfectado],
    *,
    tenant_id: Optional[UUID] = None,
) -> Aviso:
    """RF-20, RF-10: el plan recortó el modelo o el historial de estos números (pausados incluidos)."""
    cuerpo = f"el plan recortó la configuración de {len(numeros)} número(s): {_lista(numeros)}."
    return _armar(
        TIPO_RECORTADOS,
        "Se recortó la configuración de tus números",
        negocio,
        cuerpo,
        ACCION_RECORTE,
        _datos(negocio, tenant_id, "recorte_por_plan", numeros, ACCION_RECORTE),
    )


def construir_aviso_incompleto(
    negocio: str,
    numeros: Sequence[NumeroAfectado],
    motivo: str,
    *,
    tenant_id: Optional[UUID] = None,
) -> Aviso:
    """RF-20, RF-8, RF-17: números (o Instagram y Facebook) con configuración incompleta."""
    if motivo not in MOTIVOS_INCOMPLETO:
        raise ValueError(f"Motivo de configuración incompleta desconocido: {motivo!r}")
    cuerpo = f"{_lista(numeros)}: {MOTIVOS_INCOMPLETO[motivo]}."
    return _armar(
        TIPO_INCOMPLETO,
        "Configuración incompleta: el agente no responde",
        negocio,
        cuerpo,
        ACCION_INCOMPLETO,
        _datos(negocio, tenant_id, "configuracion_incompleta", numeros, ACCION_INCOMPLETO),
    )


# ------------------------------------------------------------
# Envío
# ------------------------------------------------------------
async def _emitir(tenant_id: UUID, aviso: Aviso, portal_user_id: UUID) -> None:
    """Alerta personal + push + correo espejo. Import perezoso: realtime importa deps."""
    from realtime import broadcast_alerta

    await broadcast_alerta(
        tenant_id,
        aviso.tipo,
        aviso.titulo,
        aviso.mensaje,
        aviso.datos,
        portal_user_id=portal_user_id,
    )


async def nombre_negocio(conn: asyncpg.Connection, tenant_id: UUID) -> str:
    nombre = await conn.fetchval("SELECT name FROM tenants WHERE id = $1", tenant_id)
    return str(nombre) if nombre else "Tu negocio"


async def avisar_gerencia(conn: asyncpg.Connection, tenant_id: UUID, aviso: Aviso) -> int:
    """
    Una alerta personal por cada cuenta activa owner o superadmin del negocio
    (un member no la recibe). Devuelve a cuántas salió. Nunca lanza.
    """
    try:
        cuentas = await conn.fetch(
            """
            SELECT id FROM portal_users
            WHERE tenant_id = $1 AND role = ANY($2::text[]) AND is_active = true
            ORDER BY created_at, id
            """,
            tenant_id,
            list(ROLES_AVISO),
        )
    except Exception:
        log.exception("No se pudo leer la gerencia del negocio %s para el aviso %s", tenant_id, aviso.tipo)
        return 0
    enviados = 0
    for cuenta in cuentas:
        try:
            await _emitir(tenant_id, aviso, cuenta["id"])
            enviados += 1
        except Exception:
            log.exception("No se pudo emitir el aviso %s a la cuenta %s (tenant=%s)", aviso.tipo, cuenta["id"], tenant_id)
    return enviados


async def avisar_pausa(
    conn: asyncpg.Connection, tenant_id: UUID, filas: Sequence[Mapping[str, Any]], motivo: str
) -> int:
    if not filas:
        return 0
    aviso = construir_aviso_pausa(
        await nombre_negocio(conn, tenant_id), [afectado_desde_fila(f) for f in filas], motivo, tenant_id=tenant_id
    )
    return await avisar_gerencia(conn, tenant_id, aviso)


async def avisar_recorte(conn: asyncpg.Connection, tenant_id: UUID, filas: Sequence[Mapping[str, Any]]) -> int:
    if not filas:
        return 0
    aviso = construir_aviso_recorte(
        await nombre_negocio(conn, tenant_id), [afectado_desde_fila(f) for f in filas], tenant_id=tenant_id
    )
    return await avisar_gerencia(conn, tenant_id, aviso)


async def avisar_incompleto(
    conn: asyncpg.Connection, tenant_id: UUID, filas: Sequence[Mapping[str, Any]], motivo: str
) -> int:
    if not filas:
        return 0
    aviso = construir_aviso_incompleto(
        await nombre_negocio(conn, tenant_id), [afectado_desde_fila(f) for f in filas], motivo, tenant_id=tenant_id
    )
    return await avisar_gerencia(conn, tenant_id, aviso)
