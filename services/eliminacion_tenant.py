"""
Eliminación definitiva de un negocio, desde el panel de gerencia.

Borrar es el último paso de un camino de dos: primero se da de baja con la
función que ya existe (PATCH /gerencia/tenants/{id}/estado → 'baja', que
apaga el agente y exige motivo) y recién entonces se puede eliminar. Acá no
hay ningún atajo para inactivar: si el negocio no está en 'baja', no se
borra.

Qué bloquea (cualquiera alcanza):
  - cuenta_activa:       el estado de plataforma no es 'baja'. 'suspendido'
                         tampoco sirve: suele ser temporal (impago) y se
                         revierte con un clic; un borrado no.
  - suscripcion_vigente: hay algo que todavía cobra o que ya se cobró y no
                         terminó. Ver `_SUSCRIPCION_VIGENTE`.
  - cuenta_propia:       entre los usuarios del negocio hay alguien de
                         gerencia (el propio gerente, típicamente, que usa
                         un tenant interno). Borrarlo le quitaría el acceso.

El dueño también puede borrar su propia cuenta (services/eliminacion_cuenta.py)
con `modo="propietario"`: ahí no se exige 'baja' (el dueño no puede ponerla)
y una suscripción ya cancelada con días pagados no bloquea, solo se advierte
(`Revision.pagado_hasta`). Lo que sí bloquea es lo que todavía puede cobrar
(`_PUEDE_COBRAR`).

Los créditos sin usar NO bloquean: se muestran como advertencia en la
confirmación (decisión de producto).

`DELETE FROM tenants` es un borrado duro: las FKs con ON DELETE CASCADE se
llevan usuarios del portal, conversaciones, CRM, calendario y el historial
de cobros. Lo que sobrevive es la bitácora de gerencia (sin FK a
propósito), donde queda una foto de lo borrado, y lo que Stripe/Mercado
Pago guardan de su lado — que no se toca.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

import asyncpg

from schemas import BloqueoEliminacionOut, CodigoBloqueoEliminacion
from services.gerencia import registrar_auditoria

# Vigente = todavía da acceso o todavía puede cobrar:
#   - 'activa' (pagada, prueba otorgada, o cancelada con días pagados: la
#     cancelación deja 'activa' hasta fecha_renovacion);
#   - un período pagado que no terminó, sea cual sea el estado;
#   - una Subscription de Stripe que Stripe no dio de baja: aunque acá
#     figure 'pausada' por cobros fallidos, Stripe la sigue reintentando y
#     un cobro exitoso llegaría para un negocio que ya no existe;
#   - lo mismo con una suscripción de Mercado Pago no cancelada.
_SUSCRIPCION_VIGENTE = """
    s.estado = 'activa'
    OR s.fecha_renovacion > NOW()
    OR (s.stripe_subscription_id IS NOT NULL AND s.cancelada_en IS NULL)
    OR (s.mp_subscription_id IS NOT NULL AND s.estado <> 'cancelada')
"""

# Lo que todavía puede generar un cobro: una Subscription de Stripe que nadie
# canceló (ni al vencer ni ya dada de baja), o una de Mercado Pago no
# cancelada. Es el criterio del borrado por el propio dueño: lo ya pagado de
# una suscripción cancelada se pierde, pero no se cobra nada más.
_PUEDE_COBRAR = """
    (s.stripe_subscription_id IS NOT NULL
        AND s.cancelada_en IS NULL
        AND NOT COALESCE(s.cancela_al_vencer, false))
    OR (s.mp_subscription_id IS NOT NULL AND s.estado <> 'cancelada')
"""

Modo = Literal["gerencia", "propietario"]

MENSAJES_PROPIETARIO: dict[CodigoBloqueoEliminacion, str] = {
    "suscripcion_vigente": (
        "Tu suscripción sigue activa y se va a renovar. Cancélala desde "
        "\"Gestionar / Cancelar suscripción\" antes de borrar la cuenta."
    ),
    "cuenta_propia": (
        "Esta cuenta tiene nivel gerencia de plataforma y no se puede borrar "
        "desde aquí."
    ),
}

_SUFIJO_SUSCRIPCION = (
    "Hay que esperar a que termine o cancelarla antes de eliminar, "
    "para no dejar cobros sin negocio."
)

MENSAJES: dict[CodigoBloqueoEliminacion, str] = {
    "cuenta_activa": (
        "Primero hay que dar de baja el negocio (estado 'Baja'). "
        "Solo un negocio dado de baja se puede eliminar."
    ),
    "suscripcion_vigente": f"Tiene una suscripción vigente. {_SUFIJO_SUSCRIPCION}",
    "cuenta_propia": (
        "Entre sus usuarios hay alguien con nivel gerencia. "
        "Eliminarlo le quitaría el acceso al panel."
    ),
}


class NegocioNoEncontrado(Exception):
    pass


class EliminacionBloqueada(Exception):
    def __init__(self, bloqueos: list[BloqueoEliminacionOut]):
        super().__init__(bloqueos[0].mensaje)
        self.bloqueos = bloqueos


class ConfirmacionIncorrecta(Exception):
    pass


class ReferenciasPendientes(Exception):
    """Alguna tabla (p. ej. de n8n en producción) apunta al tenant sin cascada."""


@dataclass
class Revision:
    nombre: str
    bloqueos: list[BloqueoEliminacionOut]
    creditos_disponibles: Decimal
    usuarios_portal: int
    conversaciones: int
    transacciones: int
    # Fin del período pagado si todavía no llegó; None si no hay días por
    # perder. En modo propietario es solo una advertencia.
    pagado_hasta: datetime | None = None

    @property
    def eliminable(self) -> bool:
        return not self.bloqueos


def _mensaje_suscripcion(pagada_hasta: datetime | None) -> str:
    if pagada_hasta is not None:
        return (
            f"Tiene una suscripción vigente hasta el {pagada_hasta:%d/%m/%Y}. "
            f"{_SUFIJO_SUSCRIPCION}"
        )
    return MENSAJES["suscripcion_vigente"]


async def revisar(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    *,
    bloquear: bool = False,
    modo: Modo = "gerencia",
) -> Revision:
    """
    Lo que decide si se puede borrar. La usan la ficha (GET) y el borrado
    (POST): una sola regla, no dos que se desincronicen.

    `bloquear=True` toma FOR UPDATE las filas de estado y de suscripción:
    dentro de la transacción del borrado, un webhook de Stripe o un cambio
    de estado no pueden colarse entre la revisión y el DELETE.
    """
    candado = "FOR UPDATE" if bloquear else ""
    tenant = await conn.fetchrow(f"SELECT id, name FROM tenants WHERE id = $1 {candado}", tenant_id)
    if tenant is None:
        raise NegocioNoEncontrado()

    estado = await conn.fetchval(
        f"SELECT estado FROM tenant_estado_plataforma WHERE tenant_id = $1 {candado}",
        tenant_id,
    )
    sub = await conn.fetchrow(
        f"""
        SELECT s.fecha_renovacion,
               s.fecha_renovacion > NOW() AS periodo_en_curso,
               ({_SUSCRIPCION_VIGENTE}) AS vigente,
               ({_PUEDE_COBRAR}) AS puede_cobrar
        FROM tenant_subscriptions s
        WHERE s.tenant_id = $1
        {candado}
        """,
        tenant_id,
    )
    datos = await conn.fetchrow(
        """
        SELECT
            (SELECT COALESCE(SUM(creditos_disponibles), 0)
               FROM tenant_credits WHERE tenant_id = $1)           AS creditos,
            (SELECT COUNT(*) FROM portal_users WHERE tenant_id = $1)  AS usuarios,
            (SELECT COUNT(*) FROM conversations WHERE tenant_id = $1) AS conversaciones,
            (SELECT COUNT(*) FROM tenant_transactions
               WHERE tenant_id = $1)                               AS transacciones,
            EXISTS (
                SELECT 1 FROM portal_users pu
                JOIN gerencia_users gu ON LOWER(gu.email) = LOWER(pu.email)
                WHERE pu.tenant_id = $1
            )                                                      AS tiene_gerencia
        """,
        tenant_id,
    )

    pagado_hasta = (
        sub["fecha_renovacion"] if sub is not None and sub["periodo_en_curso"] else None
    )

    bloqueos: list[BloqueoEliminacionOut] = []
    if modo == "gerencia":
        # Sin fila de estado = 'activo' (mismo COALESCE que acceso_pagos).
        if (estado or "activo") != "baja":
            bloqueos.append(
                BloqueoEliminacionOut(codigo="cuenta_activa", mensaje=MENSAJES["cuenta_activa"])
            )
        if sub is not None and sub["vigente"]:
            bloqueos.append(
                BloqueoEliminacionOut(
                    codigo="suscripcion_vigente",
                    mensaje=_mensaje_suscripcion(pagado_hasta),
                )
            )
        if datos["tiene_gerencia"]:
            bloqueos.append(
                BloqueoEliminacionOut(codigo="cuenta_propia", mensaje=MENSAJES["cuenta_propia"])
            )
    else:
        if sub is not None and sub["puede_cobrar"]:
            bloqueos.append(
                BloqueoEliminacionOut(
                    codigo="suscripcion_vigente",
                    mensaje=MENSAJES_PROPIETARIO["suscripcion_vigente"],
                )
            )
        if datos["tiene_gerencia"]:
            bloqueos.append(
                BloqueoEliminacionOut(
                    codigo="cuenta_propia", mensaje=MENSAJES_PROPIETARIO["cuenta_propia"]
                )
            )

    return Revision(
        nombre=tenant["name"],
        bloqueos=bloqueos,
        creditos_disponibles=datos["creditos"],
        usuarios_portal=datos["usuarios"],
        conversaciones=datos["conversaciones"],
        transacciones=datos["transacciones"],
        pagado_hasta=pagado_hasta,
    )


async def eliminar(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    *,
    confirmacion: str,
    gerente_email: str,
    gerente_id: UUID,
) -> Revision:
    """
    Borra el negocio. `conn` tiene que venir dentro de una transacción: la
    revisión, la bitácora y el DELETE se confirman o se descartan juntos.

    El orden de las validaciones importa: primero los bloqueos (lo que
    explica por qué no se puede) y después la confirmación, para que un
    gerente que escribió bien el nombre de un negocio activo se entere de
    lo que de verdad falta.
    """
    r = await revisar(conn, tenant_id, bloquear=True)
    if r.bloqueos:
        raise EliminacionBloqueada(r.bloqueos)
    if confirmacion.strip() != r.nombre.strip():
        raise ConfirmacionIncorrecta()

    snapshot = await conn.fetchrow(
        """
        SELECT
            (SELECT array_agg(email ORDER BY email) FROM portal_users
              WHERE tenant_id = $1)                                     AS correos,
            (SELECT stripe_customer_id FROM tenant_subscriptions
              WHERE tenant_id = $1)                                     AS stripe_customer_id,
            (SELECT plan FROM tenant_subscriptions WHERE tenant_id = $1) AS plan,
            (SELECT COALESCE(SUM(monto), 0) FROM tenant_transactions
              WHERE tenant_id = $1 AND estado_pago = 'aprobado')        AS total_cobrado,
            (SELECT motivo FROM tenant_estado_plataforma
              WHERE tenant_id = $1)                                     AS motivo_baja
        """,
        tenant_id,
    )

    # La bitácora no tiene FK a tenants: esta entrada sobrevive al DELETE y
    # es lo único que queda para responder "¿qué era este UUID?".
    await registrar_auditoria(
        actor_email=gerente_email,
        actor_portal_user_id=gerente_id,
        accion="eliminar_tenant",
        tenant_id=tenant_id,
        detalle={
            "nombre": r.nombre,
            "correos": list(snapshot["correos"] or []),
            "plan": snapshot["plan"],
            "stripe_customer_id": snapshot["stripe_customer_id"],
            "motivo_baja": snapshot["motivo_baja"],
            "total_cobrado": str(snapshot["total_cobrado"]),
            "transacciones": r.transacciones,
            "conversaciones": r.conversaciones,
            "usuarios_portal": r.usuarios_portal,
            "creditos_perdidos": str(r.creditos_disponibles),
        },
        conn=conn,
    )

    await borrar(conn, tenant_id)
    return r


async def borrar(conn: asyncpg.Connection, tenant_id: UUID) -> None:
    """
    El DELETE en cascada. Savepoint: si una FK sin cascada lo frena, la
    transacción externa entera se descarta igual (el router relanza), pero
    el error llega con un tipo propio en vez de un 500.
    """
    try:
        async with conn.transaction():
            await conn.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
    except asyncpg.ForeignKeyViolationError as e:
        raise ReferenciasPendientes(str(e)) from e
