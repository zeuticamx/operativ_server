"""
Reparto automático de leads entre vendedores.

La estrategia SIEMPRE se lee de `tenant_vendedor_config`; ningún endpoint
decide por su cuenta a quién le toca.

El módulo está partido en dos capas a propósito:

  - `elegir_por_carga` / `elegir_round_robin` son puras: reciben la lista de
    candidatos ya cargada y devuelven un id. Se prueban sin base de datos.
  - `asignar_vendedor_automatico` hace el I/O y delega la decisión en ellas.

El desempate se resuelve en Python y no con un ORDER BY: son unos pocos
vendedores por tenant, y a cambio la regla de reparto —que es la parte que
se discute y se cambia— queda cubierta por tests.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence
from uuid import UUID

import asyncpg

from services.pipeline_estados import ESTADOS_CERRADOS
from session import transaccion

ESTRATEGIAS = ("carga", "round_robin", "manual")
ESTRATEGIA_POR_DEFECTO = "carga"

# Un vendedor al que nunca se le asignó nada tiene que ganarle a todos en el
# desempate por antigüedad, así que se lo trata como si su última asignación
# fuera infinitamente vieja.
_NUNCA = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class VendedorCandidato:
    id: UUID
    creado_en: datetime
    # Leads en estados no cerrados: los que todavía le ocupan tiempo.
    carga: int
    # Cuándo se movió por última vez algo de su cartera. None = nunca.
    ultima_asignacion: Optional[datetime]


@dataclass(frozen=True)
class ConfigAsignacion:
    estrategia: str
    ultimo_vendedor_asignado_id: Optional[UUID]


# ============================================================
# Decisión (puro, sin I/O)
# ============================================================
def elegir_por_carga(candidatos: Sequence[VendedorCandidato]) -> Optional[UUID]:
    """
    El que tenga menos leads abiertos. Empate: el que lleva más tiempo sin
    recibir nada. Empate otra vez: el vendedor más antiguo.

    El `str(id)` final no es una regla de negocio, es para que dos vendedores
    idénticos en todo lo demás se resuelvan siempre igual en vez de depender
    del orden en que los devolvió Postgres.
    """
    if not candidatos:
        return None

    mejor = min(
        candidatos,
        key=lambda c: (
            c.carga,
            c.ultima_asignacion or _NUNCA,
            c.creado_en,
            str(c.id),
        ),
    )
    return mejor.id


def elegir_round_robin(
    candidatos: Sequence[VendedorCandidato],
    ultimo_asignado_id: Optional[UUID],
) -> Optional[UUID]:
    """
    El siguiente de la rueda, ordenada por antigüedad.

    Si el último asignado ya no está en la lista —lo desactivaron, lo
    borraron, o se lo excluyó de esta ronda— la rueda arranca desde el
    principio en vez de quedarse trabada.
    """
    if not candidatos:
        return None

    orden = sorted(candidatos, key=lambda c: (c.creado_en, str(c.id)))
    ids = [c.id for c in orden]

    if ultimo_asignado_id in ids:
        siguiente = (ids.index(ultimo_asignado_id) + 1) % len(ids)
        return ids[siguiente]

    return ids[0]


# ============================================================
# I/O
# ============================================================
async def cargar_candidatos(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    excluir_id: Optional[UUID] = None,
) -> list[VendedorCandidato]:
    filas = await conn.fetch(
        """
        SELECT
            v.id,
            v.creado_en,
            (SELECT COUNT(*)
               FROM client_pipeline p
              WHERE p.vendedor_id = v.id
                AND p.estado <> ALL($2::text[])) AS carga,
            (SELECT MAX(p.actualizado_en)
               FROM client_pipeline p
              WHERE p.vendedor_id = v.id) AS ultima_asignacion
        FROM vendedores v
        WHERE v.tenant_id = $1
          AND v.activo
          AND ($3::uuid IS NULL OR v.id <> $3)
        ORDER BY v.creado_en, v.id
        """,
        tenant_id,
        list(ESTADOS_CERRADOS),
        excluir_id,
    )

    return [
        VendedorCandidato(
            id=f["id"],
            creado_en=f["creado_en"],
            carga=f["carga"],
            ultima_asignacion=f["ultima_asignacion"],
        )
        for f in filas
    ]


async def leer_config(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    para_actualizar: bool = False,
) -> ConfigAsignacion:
    """
    Config de reparto del tenant, creándola con los valores por defecto si
    todavía no existe (un tenant que activa el módulo no tiene por qué haber
    pasado antes por la pantalla de configuración).

    `para_actualizar` toma el lock de la fila: lo necesita el round-robin,
    donde leer el puntero y moverlo tienen que ser un solo paso. Sin él, dos
    mensajes que entran a la vez leen el mismo puntero y le caen al mismo
    vendedor.
    """
    await conn.execute(
        """
        INSERT INTO tenant_vendedor_config (tenant_id)
        VALUES ($1)
        ON CONFLICT (tenant_id) DO NOTHING
        """,
        tenant_id,
    )

    fila = await conn.fetchrow(
        f"""
        SELECT estrategia_asignacion, ultimo_vendedor_asignado_id
        FROM tenant_vendedor_config
        WHERE tenant_id = $1
        {"FOR UPDATE" if para_actualizar else ""}
        """,
        tenant_id,
    )

    return ConfigAsignacion(
        estrategia=fila["estrategia_asignacion"],
        ultimo_vendedor_asignado_id=fila["ultimo_vendedor_asignado_id"],
    )


async def asignar_vendedor_automatico(
    tenant_id: UUID,
    *,
    conn: Optional[asyncpg.Connection] = None,
    excluir_id: Optional[UUID] = None,
) -> Optional[UUID]:
    """
    A quién le toca el próximo lead de este tenant, o None.

    Devuelve None —nunca lanza— en los tres casos en que simplemente no hay
    a quién asignarle: estrategia 'manual', tenant sin vendedores activos, o
    el único vendedor activo es el que se pidió excluir.

    Solo elige: no toca `client_pipeline`. Escribir la asignación es trabajo
    de quien llama, que es el que sabe en qué lead va.

    `excluir_id` lo usa la reasignación de pendientes, para no devolverle al
    mismo vendedor los leads que se le están quitando.
    """
    if conn is not None:
        return await _asignar(conn, tenant_id, excluir_id)

    # El round-robin lee y mueve el puntero, así que hasta el camino de "solo
    # elegir" necesita transacción propia.
    async with transaccion() as propia:
        return await _asignar(propia, tenant_id, excluir_id)


async def _asignar(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    excluir_id: Optional[UUID],
) -> Optional[UUID]:
    config = await leer_config(conn, tenant_id, para_actualizar=True)

    if config.estrategia == "manual":
        return None

    candidatos = await cargar_candidatos(conn, tenant_id, excluir_id)
    if not candidatos:
        return None

    if config.estrategia == "round_robin":
        elegido = elegir_round_robin(candidatos, config.ultimo_vendedor_asignado_id)
        if elegido is not None:
            await conn.execute(
                """
                UPDATE tenant_vendedor_config
                SET ultimo_vendedor_asignado_id = $2
                WHERE tenant_id = $1
                """,
                tenant_id,
                elegido,
            )
        return elegido

    # 'carga' es también el camino por defecto: si algún día aparece una
    # estrategia nueva en la BD y todavía no está acá, repartir por carga es
    # un comportamiento razonable, no un 500.
    return elegir_por_carga(candidatos)
