"""
Asignación de conversaciones a un miembro del equipo.

Una conversación transferida a un humano tiene como máximo UN responsable
(`conversations.asignado_a` → `portal_users.id`). Tres maneras de llegar ahí:

  - 'owner'    el dueño/administrador la asigna o reasigna (`asignar`).
  - 'usuario'  alguien la toma él mismo (`tomar`). Solo el owner se la quita.
  - 'agente'   el agente de n8n al escalar (`autoasignar`). El LLM solo
               sugiere un `area`; QUIÉN la recibe lo decide este módulo.

El módulo está partido en dos capas, como `asignacion.py`:

  - `elegir_destino` es pura: recibe los candidatos ya resueltos y devuelve
    a quién le toca. Se prueba sin base de datos.
  - El resto hace el I/O y delega la decisión en ella.

Todo cambio queda en `conversacion_asignaciones` (historial).
"""

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

import asyncpg
from fastapi import HTTPException, status

from deps import ROL_PROVEEDOR, ROL_VENDEDOR, ROLES_ASIGNABLES
from services.asignacion import asignar_vendedor_automatico
from services.conversaciones import _PROVEEDOR_DEL_CLIENTE, _ASIGNADO_NOMBRE
from services.pipeline import get_tenant_servicios
from session import fetch_all, fetch_one, transaccion

# Lo que el agente de n8n puede sugerir al escalar. 'otro' = nada que ligue la
# conversación a un vendedor o proveedor: va al dueño.
AREAS = ("ventas", "agenda", "otro")

ESTADOS_ASIGNABLES = ("active", "transferred")


# ============================================================
# Decisión (puro, sin I/O)
# ============================================================
@dataclass(frozen=True)
class Resolucion:
    """A quién le toca según el área, o por qué no se pudo."""

    portal_user_id: Optional[UUID]
    motivo: str
    # True = el área pedía un responsable concreto y no se encontró. 'otro' no
    # es un fallo: es "sin responsable específico" y también va al dueño.
    fallo: bool = False


def elegir_destino(
    area: str,
    proveedor: Optional[UUID],
    vendedor_lead: Optional[UUID],
    vendedor_reparto: Optional[UUID],
) -> Resolucion:
    """
    `proveedor`: cuenta del proveedor de la cita más reciente del cliente.
    `vendedor_lead`: cuenta del vendedor dueño de su lead.
    `vendedor_reparto`: cuenta del vendedor que toca por la estrategia de reparto.
    Todos ya validados (ficha y cuenta activas) o None.

    El vendedor del lead gana al del reparto: un cliente que vuelve a escribir
    sigue siendo del mismo vendedor (mismo criterio que on_mensaje_entrante).
    """
    if area == "agenda":
        if proveedor is not None:
            return Resolucion(proveedor, "Proveedor de la cita más reciente del cliente")
        return Resolucion(
            None, "el cliente no tiene una cita con un proveedor con cuenta activa", fallo=True
        )

    if area == "ventas":
        if vendedor_lead is not None:
            return Resolucion(vendedor_lead, "Vendedor del lead del cliente")
        if vendedor_reparto is not None:
            return Resolucion(vendedor_reparto, "Vendedor por reparto automático")
        return Resolucion(
            None, "no hay un vendedor con cuenta activa para este cliente", fallo=True
        )

    return Resolucion(None, "El agente la derivó al dueño (sin área de ventas ni de agenda)")


# ============================================================
# I/O: candidatos
# ============================================================
async def _proveedor_del_cliente(conn: asyncpg.Connection, conversacion_id: UUID) -> Optional[UUID]:
    fila = await conn.fetchrow(
        f"""
        SELECT pu.id
        FROM conversations c
        JOIN proveedores p ON p.id = {_PROVEEDOR_DEL_CLIENTE}
        JOIN portal_users pu ON pu.id = p.portal_user_id
        WHERE c.id = $1 AND p.activo AND pu.is_active
        """,
        conversacion_id,
    )
    return fila["id"] if fila else None


async def _vendedor_del_lead(conn: asyncpg.Connection, conversacion_id: UUID) -> Optional[UUID]:
    fila = await conn.fetchrow(
        """
        SELECT pu.id
        FROM conversations c
        JOIN client_pipeline p ON p.tenant_id = c.tenant_id AND p.user_id = c.user_id
        JOIN vendedores v ON v.id = p.vendedor_id
        JOIN portal_users pu ON pu.id = v.portal_user_id
        WHERE c.id = $1 AND v.activo AND pu.is_active
        """,
        conversacion_id,
    )
    return fila["id"] if fila else None


async def _vendedor_por_reparto(conn: asyncpg.Connection, tenant_id: UUID) -> Optional[UUID]:
    elegido = await asignar_vendedor_automatico(tenant_id, conn=conn)
    if elegido is None:
        return None
    fila = await conn.fetchrow(
        """
        SELECT pu.id
        FROM vendedores v
        JOIN portal_users pu ON pu.id = v.portal_user_id
        WHERE v.id = $1 AND v.activo AND pu.is_active
        """,
        elegido,
    )
    return fila["id"] if fila else None


async def _owner_de_respaldo(conn: asyncpg.Connection, tenant_id: UUID) -> Optional[UUID]:
    """El dueño activo del negocio; si no hay (no debería), un superadmin."""
    fila = await conn.fetchrow(
        """
        SELECT id FROM portal_users
        WHERE tenant_id = $1 AND is_active AND role IN ('owner', 'superadmin')
        ORDER BY (role = 'owner') DESC, created_at
        LIMIT 1
        """,
        tenant_id,
    )
    return fila["id"] if fila else None


# ============================================================
# I/O: escritura
# ============================================================
async def asignables(tenant_id: UUID):
    """Cuentas a las que se les puede asignar (para el selector del portal)."""
    return await fetch_all(
        """
        SELECT pu.id, pu.role,
               COALESCE(NULLIF(TRIM(CONCAT_WS(' ', pu.nombres, pu.apellido_paterno)), ''),
                        pu.full_name, pu.email) AS nombre,
               pu.email
        FROM portal_users pu
        WHERE pu.tenant_id = $1 AND pu.is_active AND pu.role = ANY($2::text[])
          AND (pu.role <> 'vendedor' OR EXISTS (
                SELECT 1 FROM vendedores v WHERE v.portal_user_id = pu.id AND v.activo))
          AND (pu.role <> 'proveedor' OR EXISTS (
                SELECT 1 FROM proveedores p WHERE p.portal_user_id = pu.id AND p.activo))
        ORDER BY CASE pu.role WHEN 'owner' THEN 0 WHEN 'superadmin' THEN 1
                              WHEN 'member' THEN 2 WHEN 'vendedor' THEN 3 ELSE 4 END,
                 nombre
        """,
        tenant_id,
        sorted(ROLES_ASIGNABLES),
    )


async def _validar_destino(conn: asyncpg.Connection, tenant_id: UUID, destino_id: UUID) -> None:
    """
    404 si la cuenta no existe en este negocio (o está inactiva: para quien
    asigna es lo mismo que no existir); 422 si existe pero no puede recibir
    conversaciones (rol fuera de ROLES_ASIGNABLES, o ficha desactivada).
    """
    fila = await conn.fetchrow(
        "SELECT role FROM portal_users WHERE id = $1 AND tenant_id = $2 AND is_active",
        destino_id,
        tenant_id,
    )
    if fila is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")

    rol = fila["role"]
    if rol not in ROLES_ASIGNABLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A ese usuario no se le pueden asignar conversaciones",
        )

    tabla = {ROL_VENDEDOR: "vendedores", ROL_PROVEEDOR: "proveedores"}.get(rol)
    if tabla is not None:
        ficha = await conn.fetchrow(
            f"SELECT 1 FROM {tabla} WHERE portal_user_id = $1 AND tenant_id = $2 AND activo",
            destino_id,
            tenant_id,
        )
        if ficha is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Su ficha está desactivada o no existe: no puede recibir conversaciones",
            )


async def _bloquear(conn: asyncpg.Connection, tenant_id: UUID, conversacion_id: UUID):
    """
    La conversación con su fila bloqueada hasta el fin de la transacción: es
    lo que hace que dos personas que la toman a la vez no se pisen (la
    segunda espera y ve que ya tiene dueño). El tenant va en el WHERE: una
    conversación de otro negocio es 404.
    """
    fila = await conn.fetchrow(
        """
        SELECT c.id, c.status, c.asignado_a, {nombre} AS asignado_nombre
        FROM conversations c
        LEFT JOIN portal_users pa ON pa.id = c.asignado_a
        WHERE c.id = $1 AND c.tenant_id = $2
        FOR UPDATE OF c
        """.format(nombre=_ASIGNADO_NOMBRE),
        conversacion_id,
        tenant_id,
    )
    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversación no encontrada"
        )
    if fila["status"] not in ESTADOS_ASIGNABLES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="La conversación ya está cerrada"
        )
    return fila


async def _aplicar(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    conversacion_id: UUID,
    previo: Optional[UUID],
    destino_id: Optional[UUID],
    *,
    origen: str,
    actor_id: Optional[UUID],
    nota: Optional[str],
    motivo_escalamiento: str,
):
    """
    Escribe la asignación (o la quita, con destino None) y la deja en el
    historial. Asignar a alguien apaga la IA (status='transferred'): si la IA
    siguiera contestando, se pisaría con quien atiende. Si la conversación
    estaba activa se marca igual que `escalar_humano` y sin acuse
    (`ack_pendiente=false`: quien la toma va a escribir él mismo); si ya
    estaba transferida el metadata no se toca, para no apagar el acuse que el
    agente dejó pendiente.
    """
    if destino_id is None:
        fila = await conn.fetchrow(
            """
            UPDATE conversations
            SET asignado_a = NULL, asignado_en = NULL, asignado_origen = NULL,
                asignacion_nota = $3
            WHERE id = $1 AND tenant_id = $2
            RETURNING id, status, asignado_a
            """,
            conversacion_id,
            tenant_id,
            nota,
        )
    else:
        fila = await conn.fetchrow(
            """
            UPDATE conversations
            SET status = 'transferred',
                metadata = CASE WHEN status = 'active'
                    THEN COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                        'motivo_escalamiento', $6::text,
                        'escalado_en', NOW()::text,
                        'ack_pendiente', false)
                    ELSE metadata END,
                asignado_a = $3, asignado_en = NOW(), asignado_origen = $4,
                asignacion_nota = $5
            WHERE id = $1 AND tenant_id = $2
            RETURNING id, status, asignado_a
            """,
            conversacion_id,
            tenant_id,
            destino_id,
            origen,
            nota,
            motivo_escalamiento,
        )

    await conn.execute(
        """
        INSERT INTO conversacion_asignaciones
            (tenant_id, conversation_id, de_portal_user_id, a_portal_user_id,
             origen, actor_portal_user_id, nota)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        tenant_id,
        conversacion_id,
        previo,
        destino_id,
        origen,
        actor_id,
        nota,
    )
    return fila


async def asignar(
    tenant_id: UUID,
    conversacion_id: UUID,
    destino_id: UUID,
    actor_id: UUID,
    nota: Optional[str] = None,
):
    """El owner asigna o reasigna. Devuelve (fila, asignado_anterior)."""
    async with transaccion() as conn:
        await _validar_destino(conn, tenant_id, destino_id)
        actual = await _bloquear(conn, tenant_id, conversacion_id)
        fila = await _aplicar(
            conn, tenant_id, conversacion_id, actual["asignado_a"], destino_id,
            origen="owner", actor_id=actor_id, nota=nota,
            motivo_escalamiento="asignada_desde_portal",
        )
    return fila, actual["asignado_a"]


async def tomar(tenant_id: UUID, conversacion_id: UUID, usuario_id: UUID):
    """
    Alguien toma la conversación y pasa a ser suya. Vale si está sin
    asignar (activa o ya transferida por el agente); si ya es tuya devuelve
    el estado actual (idempotente); si la tiene otro, 409 con su nombre —
    solo el owner se la puede quitar (`asignar` / `liberar`).
    """
    async with transaccion() as conn:
        actual = await _bloquear(conn, tenant_id, conversacion_id)
        if actual["asignado_a"] == usuario_id:
            return {"id": actual["id"], "status": actual["status"], "asignado_a": usuario_id}
        if actual["asignado_a"] is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"La conversación ya la tiene {actual['asignado_nombre']}",
            )
        return await _aplicar(
            conn, tenant_id, conversacion_id, None, usuario_id,
            origen="usuario", actor_id=usuario_id, nota=None,
            motivo_escalamiento="tomada_desde_portal",
        )


async def liberar(
    tenant_id: UUID,
    conversacion_id: UUID,
    actor_id: UUID,
    *,
    es_gerencia: bool,
):
    """
    Deja la conversación sin asignar (sigue transferida: nadie de la IA la
    retoma sola). Devuelve (fila, asignado_anterior). Quien no es gerencia
    solo puede soltar la suya.
    """
    async with transaccion() as conn:
        actual = await _bloquear(conn, tenant_id, conversacion_id)
        previo = actual["asignado_a"]
        if previo is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="La conversación no está asignada"
            )
        if previo != actor_id and not es_gerencia:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"La conversación la tiene {actual['asignado_nombre']}: solo el dueño se la puede quitar",
            )
        fila = await _aplicar(
            conn, tenant_id, conversacion_id, previo, None,
            origen="owner" if es_gerencia else "usuario", actor_id=actor_id, nota=None,
            motivo_escalamiento="",
        )
    return fila, previo


@dataclass(frozen=True)
class ResultadoAuto:
    """Lo que dejó la asignación automática, para avisar a quien corresponda."""

    portal_user_id: UUID
    # Cayó en el dueño porque no se halló responsable (o el área era 'otro').
    cayo_en_owner: bool
    # El área pedía alguien concreto y no se encontró (alerta de fallo).
    fallo: bool
    motivo: str


async def autoasignar(tenant_id: UUID, conversacion_id: UUID, area: str) -> Optional[ResultadoAuto]:
    """
    Asignación al escalar el agente. None si no corresponde asignar: la
    conversación no existe, no está transferida, o YA tiene dueño (un humano
    se la adelantó, o n8n reintentó la llamada). Nunca lanza por "no hay a
    quién": cae en el dueño con una nota para que la reasigne.

    Que `area` traiga cualquier otro valor se trata como 'otro': el LLM
    sugiere, no manda, y un valor inventado no puede tumbar el escalamiento.
    """
    area = area if area in AREAS else "otro"
    servicios = await get_tenant_servicios(tenant_id)

    async with transaccion() as conn:
        actual = await conn.fetchrow(
            "SELECT status, asignado_a FROM conversations WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
            conversacion_id,
            tenant_id,
        )
        if actual is None or actual["status"] != "transferred" or actual["asignado_a"] is not None:
            return None

        proveedor = (
            await _proveedor_del_cliente(conn, conversacion_id)
            if area == "agenda" and servicios.calendario_activo
            else None
        )
        vendedor_lead = vendedor_reparto = None
        if area == "ventas" and servicios.gestion_vendedores_activo:
            vendedor_lead = await _vendedor_del_lead(conn, conversacion_id)
            if vendedor_lead is None:
                vendedor_reparto = await _vendedor_por_reparto(conn, tenant_id)

        res = elegir_destino(area, proveedor, vendedor_lead, vendedor_reparto)

        destino = res.portal_user_id
        nota: Optional[str] = None
        cayo_en_owner = destino is None
        if destino is None:
            destino = await _owner_de_respaldo(conn, tenant_id)
            if destino is None:
                return None
            nota = (
                f"No se pudo asignar automáticamente: {res.motivo}. Reasígnala a quien corresponda."
                if res.fallo
                else res.motivo
            )

        await _aplicar(
            conn, tenant_id, conversacion_id, None, destino,
            origen="agente", actor_id=None, nota=nota or res.motivo,
            motivo_escalamiento="asignada_por_agente",
        )

    return ResultadoAuto(
        portal_user_id=destino, cayo_en_owner=cayo_en_owner, fallo=res.fallo, motivo=res.motivo
    )


async def asignado_de(conversacion_id: UUID):
    """(tenant_id, asignado_a) de la conversación, o None si no existe o nadie la tiene."""
    fila = await fetch_one(
        "SELECT tenant_id, asignado_a FROM conversations WHERE id = $1 AND asignado_a IS NOT NULL",
        conversacion_id,
    )
    return fila


async def hay_aviso_sin_leer(portal_user_id: UUID, conversacion_id: UUID) -> bool:
    """
    ¿Ya tiene una alerta de "mensaje nuevo" sin leer de esta conversación?
    Un aviso por conversación hasta que lo lea: si no, un cliente que escribe
    cinco veces seguidas llena de alertas al asignado.
    """
    fila = await fetch_one(
        """
        SELECT 1 FROM alertas
        WHERE portal_user_id = $1 AND tipo = 'mensaje_conversacion_asignada'
          AND NOT leido AND datos->>'conversation_id' = $2
        LIMIT 1
        """,
        portal_user_id,
        str(conversacion_id),
    )
    return fila is not None


async def reasignar_de_cuenta(
    tenant_id: UUID, portal_user_id: UUID, quien: str
) -> int:
    """
    Lo que tenía asignado `portal_user_id` pasa al dueño, con una nota, porque
    esa cuenta ya no puede atenderlo (la desactivaron, o a su ficha de
    vendedor / proveedor). Sin esto la conversación quedaría a nombre de
    alguien que no puede contestar, y como solo el owner se la quita a un
    asignado, nadie más la podría tomar. Devuelve cuántas movió.

    Solo las transferidas: una devuelta a la IA ya no tiene asignado. Si no
    hay dueño activo (no debería) quedan sin asignar. Al dueño le llega una
    alerta personal para que las reparta.
    """
    nota = f"{quien} ya no puede atenderla. Reasígnala a quien corresponda."
    async with transaccion() as conn:
        owner = await _owner_de_respaldo(conn, tenant_id)
        movidas = await conn.fetch(
            """
            UPDATE conversations
            SET asignado_a = $3, asignado_en = CASE WHEN $3::uuid IS NULL THEN NULL ELSE NOW() END,
                asignado_origen = CASE WHEN $3::uuid IS NULL THEN NULL ELSE 'sistema' END,
                asignacion_nota = $4
            WHERE tenant_id = $1 AND asignado_a = $2 AND status = 'transferred'
            RETURNING id
            """,
            tenant_id,
            portal_user_id,
            owner,
            nota,
        )
        for fila in movidas:
            await conn.execute(
                """
                INSERT INTO conversacion_asignaciones
                    (tenant_id, conversation_id, de_portal_user_id, a_portal_user_id,
                     origen, actor_portal_user_id, nota)
                VALUES ($1, $2, $5, $3, 'sistema', NULL, $4)
                """,
                tenant_id,
                fila["id"],
                owner,
                nota,
                portal_user_id,
            )

    if movidas and owner is not None:
        # Import perezoso: realtime importa deps, que importa services.
        from realtime import broadcast_alerta, emitir_datos

        await broadcast_alerta(
            tenant_id,
            "asignacion_fallida",
            "Conversaciones sin responsable",
            f"{quien} ya no puede atenderlas: {len(movidas)} conversación(es) quedaron a tu nombre. Reasígnalas.",
            {"conversaciones": len(movidas)},
            portal_user_id=owner,
        )
        await emitir_datos(tenant_id, "conversaciones", [owner, portal_user_id])
    return len(movidas)
