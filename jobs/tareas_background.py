"""
Job de background: las tareas de seguimiento pendientes cuya fecha ya pasó
se marcan 'vencidas' solas (services/agenda.marcar_tareas_vencidas).

El estado existía desde 07_crm_campo.sql pero nadie lo escribía, así que
la agenda y la app de vendedores no distinguían "pendiente para el jueves"
de "pendiente desde hace un mes". Reprogramarla a una fecha futura la
devuelve a 'pendiente' (routers/tareas.actualizar).

Vive en el scheduler de jobs/alertas_background.py, mismo motivo que el
resto de los jobs: un solo AsyncIOScheduler por proceso.
"""

import logging

from services.agenda import marcar_tareas_vencidas

log = logging.getLogger("operativai.jobs.tareas")

JOB_ID = "tareas_vencidas"


async def job_tareas_vencidas() -> None:
    try:
        marcadas = await marcar_tareas_vencidas()
        if marcadas:
            log.info("Tareas de seguimiento marcadas como vencidas: %s", marcadas)
    except Exception:
        # Un fallo acá no debe tumbar el scheduler (y con él los demás jobs).
        log.exception("Falló la revisión de tareas vencidas")
