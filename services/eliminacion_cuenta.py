"""
Borrado de la cuenta por el propio dueño, desde el portal.

Solo el `owner` puede, y lo que se borra es el negocio entero: los datos de
Stripe, los usuarios del equipo, las conversaciones y el CRM cuelgan del
tenant, no de la persona. Por eso reutiliza services/eliminacion_tenant.py
(misma revisión, mismo DELETE en cascada) con `modo="propietario"`.

Qué bloquea:
  - suscripcion_vigente:   algo que todavía puede cobrar (Stripe sin cancelar,
                           Mercado Pago activa). Una suscripción ya cancelada
                           con días pagados NO bloquea: se advierte que esos
                           días se pierden (`pagado_hasta`).
  - adeudo_pendiente:      el Customer de Stripe tiene facturas abiertas.
  - adeudo_no_verificable: hay Customer de Stripe pero Stripe no contestó.
                           Se falla cerrado: un borrado no se deshace.
  - cuenta_propia:         la cuenta es de gerencia de plataforma.

Antes de revisar nada se exige: rol owner, escribir "ELIMINAR" y re-
autenticarse (contraseña, o credencial de Google en cuentas sin contraseña),
con tope de intentos fallidos.

Sesiones: el JWT no se revoca aparte. deps.usuario_actual y /auth/refresh
releen al usuario en cada petición; borrada la fila, la siguiente petición de
cualquier sesión abierta da 401.

Auditoría: queda una entrada en gerencia_auditoria (sin FK, sobrevive al
borrado) con el UUID, el plan y el Customer de Stripe — sin correos ni
nombres: el dueño pidió borrar sus datos.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from deps import UsuarioActual
from schemas import BloqueoEliminacionOut
from security import verify_password
from services import eliminacion_tenant, stripe_portal
# Se reexportan para que el router no tenga que importar los dos módulos.
from services.eliminacion_tenant import (  # noqa: F401
    EliminacionBloqueada,
    ReferenciasPendientes,
    Revision,
)
from services.gerencia import registrar_auditoria
from services.google_login import TokenGoogleInvalido, verificar_credential
from session import conexion, execute, fetch_one, transaccion

CONFIRMACION = "ELIMINAR"
INTENTOS_MAX = 5
BLOQUEO_INTENTOS = timedelta(minutes=15)

MENSAJE_ADEUDO = (
    "Tienes pagos pendientes con Stripe. Revísalos en \"Gestionar / Cancelar "
    "suscripción\" y vuelve a intentarlo cuando estén resueltos."
)
MENSAJE_NO_VERIFICABLE = (
    "No pudimos confirmar con Stripe que no tengas pagos pendientes. "
    "Inténtalo de nuevo en unos minutos."
)


class NoEsPropietario(Exception):
    pass


class ConfirmacionIncorrecta(Exception):
    pass


class CredencialIncorrecta(Exception):
    pass


class DemasiadosIntentos(Exception):
    def __init__(self, minutos: int):
        super().__init__(f"Demasiados intentos; espera {minutos} min")
        self.minutos = minutos


@dataclass
class RevisionCuenta:
    revision: Revision
    verificacion: Literal["password", "google"]

    @property
    def bloqueos(self) -> list[BloqueoEliminacionOut]:
        return self.revision.bloqueos


def _exigir_propietario(usuario: UsuarioActual) -> UUID:
    if usuario.role != "owner" or usuario.tenant_id is None:
        raise NoEsPropietario()
    return usuario.tenant_id


async def bloqueos_stripe(tenant_id: UUID) -> list[BloqueoEliminacionOut]:
    """
    Adeudos del lado de Stripe. Fuera de la transacción del borrado a
    propósito: una llamada HTTP no debe sostener los FOR UPDATE.
    """
    fila = await fetch_one(
        "SELECT stripe_customer_id FROM tenant_subscriptions WHERE tenant_id = $1",
        tenant_id,
    )
    customer_id = fila["stripe_customer_id"] if fila else None
    if not customer_id:
        return []
    try:
        if await stripe_portal.tiene_facturas_abiertas(customer_id):
            return [BloqueoEliminacionOut(codigo="adeudo_pendiente", mensaje=MENSAJE_ADEUDO)]
    except stripe_portal.StripeNoDisponible:
        return [
            BloqueoEliminacionOut(codigo="adeudo_no_verificable", mensaje=MENSAJE_NO_VERIFICABLE)
        ]
    return []


async def _verificacion(usuario_id: UUID) -> Literal["password", "google"]:
    fila = await fetch_one("SELECT password_hash FROM portal_users WHERE id = $1", usuario_id)
    return "password" if fila and fila["password_hash"] else "google"


async def revisar(usuario: UsuarioActual) -> RevisionCuenta:
    """Si se puede borrar y qué se perdería (para la UI, antes del modal)."""
    tenant_id = _exigir_propietario(usuario)
    async with conexion() as conn:
        r = await eliminacion_tenant.revisar(conn, tenant_id, modo="propietario")
    r.bloqueos.extend(await bloqueos_stripe(tenant_id))
    return RevisionCuenta(revision=r, verificacion=await _verificacion(usuario.id))


async def _verificar_identidad(
    usuario_id: UUID, password: str | None, google_credential: str | None
) -> None:
    """
    Contraseña actual, o credencial de Google del MISMO google_id si la
    cuenta no tiene contraseña. Cuenta los fallos y bloquea al llegar al tope.
    """
    fila = await fetch_one(
        """
        SELECT password_hash, google_id, eliminacion_intentos,
               eliminacion_bloqueada_hasta
        FROM portal_users WHERE id = $1
        """,
        usuario_id,
    )
    if fila is None:
        raise CredencialIncorrecta()

    ahora = datetime.now(timezone.utc)
    bloqueada = fila["eliminacion_bloqueada_hasta"]
    if bloqueada is not None and bloqueada > ahora:
        raise DemasiadosIntentos(max(1, int((bloqueada - ahora).total_seconds() // 60) + 1))

    if fila["password_hash"]:
        valida = bool(password) and verify_password(password, fila["password_hash"])
    else:
        valida = False
        if google_credential and fila["google_id"]:
            try:
                payload = await verificar_credential(google_credential)
                valida = payload.get("sub") == fila["google_id"] and bool(
                    payload.get("email_verified")
                )
            except TokenGoogleInvalido:
                valida = False

    if valida:
        if fila["eliminacion_intentos"]:
            await execute(
                """
                UPDATE portal_users
                   SET eliminacion_intentos = 0, eliminacion_bloqueada_hasta = NULL
                 WHERE id = $1
                """,
                usuario_id,
            )
        return

    # Suma atómica: dos intentos simultáneos no se pisan el contador.
    await execute(
        """
        UPDATE portal_users
           SET eliminacion_intentos = eliminacion_intentos + 1,
               eliminacion_bloqueada_hasta = CASE
                   WHEN eliminacion_intentos + 1 >= $2 THEN NOW() + $3::interval
                   ELSE eliminacion_bloqueada_hasta END
         WHERE id = $1
        """,
        usuario_id,
        INTENTOS_MAX,
        BLOQUEO_INTENTOS,
    )
    raise CredencialIncorrecta()


async def eliminar(
    usuario: UsuarioActual,
    *,
    confirmacion: str,
    password: str | None,
    google_credential: str | None,
) -> Revision:
    """
    Borra el negocio del dueño. Orden: rol, confirmación escrita, identidad,
    adeudos en Stripe y, dentro de una transacción con FOR UPDATE, la
    revisión de suscripción + auditoría + DELETE. Si algo bloquea, no se
    borra nada.
    """
    tenant_id = _exigir_propietario(usuario)
    if confirmacion.strip() != CONFIRMACION:
        raise ConfirmacionIncorrecta()

    await _verificar_identidad(usuario.id, password, google_credential)

    bloqueos_externos = await bloqueos_stripe(tenant_id)

    async with transaccion() as conn:
        r = await eliminacion_tenant.revisar(conn, tenant_id, bloquear=True, modo="propietario")
        bloqueos = r.bloqueos + bloqueos_externos
        if bloqueos:
            raise EliminacionBloqueada(bloqueos)

        sub = await conn.fetchrow(
            "SELECT plan, stripe_customer_id FROM tenant_subscriptions WHERE tenant_id = $1",
            tenant_id,
        )
        await registrar_auditoria(
            actor_email="(propietario)",
            actor_portal_user_id=usuario.id,
            accion="eliminar_cuenta_propietario",
            tenant_id=tenant_id,
            detalle={
                "plan": sub["plan"] if sub else None,
                "stripe_customer_id": sub["stripe_customer_id"] if sub else None,
                "pagado_hasta": r.pagado_hasta.isoformat() if r.pagado_hasta else None,
                "usuarios_portal": r.usuarios_portal,
                "conversaciones": r.conversaciones,
                "creditos_perdidos": str(r.creditos_disponibles),
            },
            conn=conn,
        )
        await eliminacion_tenant.borrar(conn, tenant_id)

    return r
