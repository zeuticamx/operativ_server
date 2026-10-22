"""
Vista de gerencia a las conversaciones de un número de WhatsApp (sql/67).

Todo `gerencia_users` abre las conversaciones del número configurado en
`GERENCIA_NUMERO_PHONE_NUMBER_ID`; el dueño de ese negocio puede revocar la vista.
Sin HTTP: los routers traducen los resultados a respuestas.
"""

from uuid import UUID

from config import settings
from session import execute, fetch_one, fetch_value


async def resolver_numero():
    """
    Fila (agente_canal_id, tenant_id, tenant_nombre) del número configurado, o None si la
    función está apagada, el número no existe o ya está desvinculado.
    """
    pnid = settings.GERENCIA_NUMERO_PHONE_NUMBER_ID
    if not pnid:
        return None
    return await fetch_one(
        """
        SELECT ac.id AS agente_canal_id, ac.tenant_id, t.name AS tenant_nombre
        FROM agentes_canal ac
        JOIN tenants t ON t.id = ac.tenant_id
        WHERE ac.phone_number_id = $1 AND ac.estado IN ('activo', 'pausado')
        LIMIT 1
        """,
        pnid,
    )


async def vista_revocada(tenant_id: UUID) -> bool:
    return bool(
        await fetch_value(
            "SELECT revocada FROM vista_gerencia_conversaciones WHERE tenant_id = $1",
            tenant_id,
        )
    )


async def aplica_a(tenant_id: UUID) -> bool:
    """True si `tenant_id` es el negocio dueño del número configurado."""
    fila = await resolver_numero()
    return fila is not None and fila["tenant_id"] == tenant_id


async def fijar_revocada(tenant_id: UUID, revocada: bool, portal_user_id: UUID) -> None:
    await execute(
        """
        INSERT INTO vista_gerencia_conversaciones (tenant_id, revocada, decidida_por)
        VALUES ($1, $2, $3)
        ON CONFLICT (tenant_id) DO UPDATE
           SET revocada = EXCLUDED.revocada,
               decidida_por = EXCLUDED.decidida_por,
               decidida_en = NOW()
        """,
        tenant_id,
        revocada,
        portal_user_id,
    )
