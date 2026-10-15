"""
Correo espejo de las notificaciones (spec 004, RF-1 a RF-5).

Cada alerta que sale por WebSocket (`realtime.broadcast_alerta`) sale también
por correo a quien la recibe. La audiencia se calcula en BD por rol, como
`_emails_gerencia` en routers/alertas.py, nunca mirando quién tiene el
portal abierto:

  alerta del negocio  -> cuentas activas de ROLES_NEGOCIO del tenant
  alerta personal     -> esa cuenta (si está activa y es del tenant)

Best-effort: un único intento por destinatario, sin reintentos. Nada de lo que
pasa aquí puede lanzar al llamador ni retrasar la alerta: `programar_espejo`
lanza una tarea en segundo plano (mismo patrón que services/push.py) y cada
envío va en su propio try/except, solo con log (RF-3).
"""

import asyncio
import logging
from typing import Any, Iterable, Optional
from uuid import UUID

from deps import ROLES_NEGOCIO
from services.correo import enviar_notificacion_negocio
from session import fetch_all

log = logging.getLogger("operativai.correo_espejo")

# RF-4 a, b, c: hechos que ya tienen su propio correo. Todo lo demás se espeja,
# incluidos `sin_actividad` y `nuevo_lead` (decisión del usuario: sin exclusión
# por tipo; el volumen no es motivo para excluir).
TIPOS_SIN_ESPEJO = frozenset({"cuota_excedida", "perfil_incompleto", "solicitud_escritura"})

# Referencias fuertes para que el GC no recoja las tareas a medias.
_tareas: set["asyncio.Task[None]"] = set()


def debe_espejarse(tipo: str) -> bool:
    return tipo not in TIPOS_SIN_ESPEJO


def destinatarios(cuentas: Iterable[Any], excluir: Iterable[UUID] = ()) -> list[str]:
    """
    Correos de las cuentas activas con correo, sin repetir (ni por mayúsculas)
    y sin las cuentas de `excluir` (RF-4 e). Conserva el orden de entrada.
    """
    excluidos = {str(u) for u in excluir}
    vistos: set[str] = set()
    salida: list[str] = []
    for cuenta in cuentas:
        if not cuenta["is_active"] or str(cuenta["id"]) in excluidos:
            continue
        correo = (cuenta["email"] or "").strip()
        if not correo or correo.lower() in vistos:
            continue
        vistos.add(correo.lower())
        salida.append(correo)
    return salida


async def cuentas_de_la_alerta(tenant_id: UUID, portal_user_id: Optional[UUID]) -> list[Any]:
    """Cuentas candidatas a recibir la alerta, leídas de BD (A1, CL-13)."""
    if portal_user_id is not None:
        return await fetch_all(
            """
            SELECT id, email, is_active FROM portal_users
            WHERE tenant_id = $1 AND id = $2 AND is_active = true
            """,
            tenant_id,
            portal_user_id,
        )
    return await fetch_all(
        """
        SELECT id, email, is_active FROM portal_users
        WHERE tenant_id = $1 AND role = ANY($2::text[]) AND is_active = true
        """,
        tenant_id,
        list(ROLES_NEGOCIO),
    )


async def espejar(
    tenant_id: UUID,
    tipo: str,
    titulo: str,
    mensaje: str,
    portal_user_id: Optional[UUID] = None,
    excluir_correo_a: Iterable[UUID] = (),
) -> int:
    """
    Manda el correo de la alerta a cada destinatario. Devuelve cuántos salieron.
    Nunca lanza.
    """
    try:
        if not debe_espejarse(tipo):
            return 0
        cuentas = await cuentas_de_la_alerta(tenant_id, portal_user_id)
        enviados = 0
        for destino in destinatarios(cuentas, excluir_correo_a):
            try:
                await enviar_notificacion_negocio(destino, titulo, mensaje)
                enviados += 1
            except Exception:
                # RF-3 / CL-8: solo log, un único intento, la alerta ya salió.
                log.exception(
                    "No se pudo mandar el correo espejo de la alerta %s a %s (tenant=%s)",
                    tipo,
                    destino,
                    tenant_id,
                )
        return enviados
    except Exception:
        log.exception("Falló el correo espejo de la alerta %s (tenant=%s)", tipo, tenant_id)
        return 0


def programar_espejo(
    tenant_id: UUID,
    tipo: str,
    titulo: str,
    mensaje: str,
    portal_user_id: Optional[UUID] = None,
    excluir_correo_a: Iterable[UUID] = (),
) -> None:
    """
    Como `espejar`, pero en segundo plano: la petición que creó la alerta no
    espera a la BD ni al SMTP. No lanza.
    """
    if not debe_espejarse(tipo):
        return
    try:
        tarea = asyncio.create_task(
            espejar(tenant_id, tipo, titulo, mensaje, portal_user_id, tuple(excluir_correo_a))
        )
    except Exception:
        log.exception("No se pudo programar el correo espejo de la alerta %s", tipo)
        return
    _tareas.add(tarea)
    tarea.add_done_callback(_tareas.discard)
