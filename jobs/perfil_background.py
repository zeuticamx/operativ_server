"""
Job de background: recuerda completar el perfil (routers/perfil.py).

Regla, por usuario:
  - Solo mientras el perfil esté incompleto (`perfil_completado_en` NULL).
  - Solo durante los primeros PERFIL_RECORDATORIO_DIAS (20) días naturales
    desde la creación de la cuenta. Se decidió que las cuentas creadas antes
    de esto no entren: la ventana cuenta desde `created_at`, así que una
    cuenta de hace más de 20 días simplemente nunca cumple la condición.
  - Uno cada 24 h: el primero en la primera pasada después del alta, y
    después uno por día. Son 20 en total, con PERFIL_RECORDATORIO_DIAS como
    tope duro además de la ventana.
  - Solo roles del portal (owner, superadmin, member). El vendedor usa su
    propia app y no ve Preferencias.

Cada recordatorio es un correo + una alerta PERSONAL en la campana
(alertas.portal_user_id): solo la ve ese usuario, no sus compañeros. La
alerta anterior de este tipo se borra antes de crear la nueva, para que no
se apilen 20 en la campana.

El envío se RESERVA con un solo UPDATE ... RETURNING antes de mandar nada:
si dos pasadas corren a la vez, la segunda ya no encuentra la fila. Si el
correo falla, el día igual cuenta — la regla es "un recordatorio por día
durante 20 días", no "20 correos entregados".
"""

import logging
from uuid import UUID

from config import settings
from realtime import broadcast_alerta
from routers.perfil import faltantes as campos_faltantes
from services.correo import ErrorEnvioCorreo, enviar_recordatorio_perfil, texto_faltantes
from session import execute, fetch_all

log = logging.getLogger("operativai.jobs.perfil")

JOB_ID = "recordatorio_perfil"

# Roles que usan el portal y ven Preferencias.
ROLES_CON_RECORDATORIO = ["owner", "superadmin", "member"]

# El job corre cada hora, y "hace 24 h exactas" casi nunca coincide con una
# pasada: sin margen, cada recordatorio saldría en la pasada SIGUIENTE a las
# 24 h y el horario se correría ~1 h por día, perdiendo el último de los 20.
# Con 30 min de margen, la pasada de "mañana a la misma hora" ya califica.
_MARGEN_MINUTOS = 30

# `created_at` es TIMESTAMP sin zona (hora local del servidor): se compara
# contra LOCALTIMESTAMP, no contra NOW(), para que los dos lados estén en la
# misma zona. `ultimo_recordatorio_perfil_en` sí es TIMESTAMPTZ.
_RESERVAR = """
    UPDATE portal_users pu
       SET recordatorios_perfil_enviados = pu.recordatorios_perfil_enviados + 1,
           ultimo_recordatorio_perfil_en = NOW()
     WHERE pu.is_active
       AND pu.tenant_id IS NOT NULL
       AND pu.role = ANY($1::text[])
       AND pu.perfil_completado_en IS NULL
       AND pu.created_at > LOCALTIMESTAMP - make_interval(days => $2)
       AND pu.recordatorios_perfil_enviados < $2
       AND (pu.ultimo_recordatorio_perfil_en IS NULL
            OR pu.ultimo_recordatorio_perfil_en
               <= NOW() - make_interval(hours => 24) + make_interval(mins => $3))
       AND ($4::uuid[] IS NULL OR pu.id = ANY($4::uuid[]))
    RETURNING pu.id, pu.tenant_id, pu.email, pu.nombres, pu.apellido_paterno,
              pu.fecha_nacimiento, pu.genero,
              pu.recordatorios_perfil_enviados AS numero,
              GREATEST(1, CEIL(EXTRACT(EPOCH FROM (pu.created_at + make_interval(days => $2))
                                                 - LOCALTIMESTAMP) / 86400))::int
                  AS dias_restantes
"""


async def job_recordatorio_perfil(usuarios: list[UUID] | None = None) -> int:
    """
    Una pasada. Devuelve a cuántos usuarios les tocó recordatorio.

    `usuarios` limita la pasada a esos ids (el scheduler no lo pasa). Existe
    para los tests: la base de desarrollo tiene cuentas reales y una pasada
    completa les consumiría recordatorios.
    """
    reservados = await fetch_all(
        _RESERVAR,
        ROLES_CON_RECORDATORIO,
        settings.PERFIL_RECORDATORIO_DIAS,
        _MARGEN_MINUTOS,
        usuarios,
    )

    for u in reservados:
        faltan = campos_faltantes(u)
        try:
            # Una sola en la campana: la de ayer se reemplaza por la de hoy.
            await execute(
                "DELETE FROM alertas WHERE portal_user_id = $1 AND tipo = 'perfil_incompleto'",
                u["id"],
            )
            await broadcast_alerta(
                u["tenant_id"],
                "perfil_incompleto",
                "Completa tu perfil",
                f"Faltan datos en Preferencias: {texto_faltantes(faltan)}.",
                {"faltantes": faltan, "dias_restantes": u["dias_restantes"]},
                portal_user_id=u["id"],
            )
        except Exception:  # noqa: BLE001 - un usuario no puede frenar al resto
            log.exception("No se pudo crear el recordatorio de perfil en la campana de %s", u["id"])

        try:
            await enviar_recordatorio_perfil(
                u["email"], u["nombres"], faltan, u["dias_restantes"]
            )
        except ErrorEnvioCorreo:
            log.error("No se pudo mandar el recordatorio de perfil a %s", u["email"])

    if reservados:
        log.info("Recordatorios de perfil enviados: %s", len(reservados))
    return len(reservados)
