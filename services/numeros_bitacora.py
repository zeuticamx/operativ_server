"""
Bitácora del negocio de los números de WhatsApp (spec 005a, RF-21).

Una fila de `numeros_bitacora` por hecho: vinculación, pausa, principal,
configuración, recorte, herramienta... con fecha, número, motivo y autor
(gerencia del negocio, la plataforma o el sistema). La lista de motivos es la
de la sección "Contrato de códigos" de la spec y está cerrada: agregar uno es
cambiar la spec (y el CHECK de sql/56). La bitácora de la PLATAFORMA (cupo,
precio, altas ajenas...) es `gerencia_auditoria` (services/gerencia.py).

Todas las funciones reciben la conexión de quien las llama, para escribir en la
misma transacción que el cambio que documentan.
"""

import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from uuid import UUID

import asyncpg

# Sección "Contrato de códigos": motivos de bitácora del negocio.
MOTIVOS: frozenset[str] = frozenset(
    {
        "vinculacion",
        "desvinculacion",
        "pausa_manual",
        "reactivacion_manual",
        "pausa_por_cambio_de_plan",
        "pausa_por_fin_de_suscripcion",
        "pausa_por_fin_de_prueba",
        "pausa_por_pago_de_prueba",
        "pausa_por_despliegue",
        "pausa_por_cupo_bajado_por_plataforma",
        "pausa_por_plan_mas_bajo_recalculado",
        "cambio_de_principal",
        "principal_sin_numero_activo",
        "configuracion_incompleta",
        "configuracion_completada",
        "recorte_por_plan",
        "herramienta_retirada_por_plan",
        "herramienta_activada",
        "herramienta_desactivada",
        "prompt_cambiado",
        "agente_encendido",
        "agente_apagado",
        "cambio_de_linea_avisos",
    }
)

TIPOS_AUTOR: tuple[str, ...] = ("gerencia", "plataforma", "sistema")


@dataclass(frozen=True)
class Autor:
    """Quién hizo el cambio: la gerencia del negocio, la plataforma o el sistema."""

    tipo: str
    portal_user_id: Optional[UUID] = None
    email: Optional[str] = None

    def __post_init__(self) -> None:
        if self.tipo not in TIPOS_AUTOR:
            raise ValueError(f"Tipo de autor desconocido: {self.tipo!r}")

    @classmethod
    def gerencia(cls, portal_user_id: UUID, email: Optional[str] = None) -> "Autor":
        return cls("gerencia", portal_user_id, email)

    @classmethod
    def plataforma(cls, email: Optional[str] = None, portal_user_id: Optional[UUID] = None) -> "Autor":
        return cls("plataforma", portal_user_id, email)

    @classmethod
    def sistema(cls) -> "Autor":
        return cls("sistema")


SISTEMA = Autor("sistema")


def ultimos_digitos(identificador: str, cuantos: int = 4) -> str:
    """
    Los últimos caracteres de un identificador de número. Es lo único que se
    deja en las bitácoras: nunca el identificador entero (P9).
    """
    return (identificador or "").strip()[-cuantos:]


async def registrar(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    agente_canal_id: Optional[UUID],
    motivo: str,
    autor: Autor,
    detalle: Optional[Mapping[str, Any]] = None,
) -> None:
    """
    Una entrada de la bitácora del negocio. `agente_canal_id` es None cuando el
    hecho no es de un número (Instagram y Facebook, o el negocio entero).
    Un motivo fuera de la lista cerrada es un error de programación.
    """
    if motivo not in MOTIVOS:
        raise ValueError(f"Motivo de bitácora desconocido: {motivo!r}")
    await conn.execute(
        """
        INSERT INTO numeros_bitacora
            (tenant_id, agente_canal_id, motivo, autor_tipo, autor_portal_user_id,
             autor_email, detalle)
        VALUES ($1, $2, $3, $4, $5, $6, $7::text::jsonb)
        """,
        tenant_id,
        agente_canal_id,
        motivo,
        autor.tipo,
        autor.portal_user_id,
        autor.email,
        # Texto y no dict: vale con o sin el códec jsonb del pool.
        json.dumps(dict(detalle or {}), default=str),
    )


async def listar(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    *,
    limite: int = 100,
    agente_canal_id: Optional[UUID] = None,
) -> list[asyncpg.Record]:
    """Lo más reciente primero. Con `agente_canal_id`, solo los hechos de ese número."""
    return await conn.fetch(
        """
        SELECT id, tenant_id, agente_canal_id, motivo, autor_tipo, autor_portal_user_id,
               autor_email, detalle, creada_en
        FROM numeros_bitacora
        WHERE tenant_id = $1 AND ($3::uuid IS NULL OR agente_canal_id = $3)
        ORDER BY creada_en DESC, id DESC
        LIMIT $2
        """,
        tenant_id,
        limite,
        agente_canal_id,
    )
