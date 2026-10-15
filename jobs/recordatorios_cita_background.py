"""
Job de background: recordatorio de cita por WhatsApp 5 h antes (spec 004, T7).

Cada AVISOS_CITA_REVISION_MINUTOS (5) minutos revisa las citas confirmadas que
entran en la ventana y manda la plantilla `recordatorio_cita` desde la cuenta
global (services/avisos_cita.procesar_recordatorios). Que no se duplique entre
procesos lo garantiza la unicidad (reserva, tipo, hora_inicio) en BD, no el
scheduler.

Vive en el scheduler de jobs/alertas_background.py, mismo motivo que el resto
de los jobs: un solo AsyncIOScheduler por proceso.
"""

import logging

from services import avisos_cita

log = logging.getLogger("operativai.jobs.recordatorios_cita")

JOB_ID = "recordatorios_cita"


async def job_recordatorios_cita() -> None:
    try:
        resumen = await avisos_cita.procesar_recordatorios()
        if resumen["revisadas"]:
            log.info("Recordatorios de cita: %s", resumen)
    except Exception:
        # Un fallo acá no debe tumbar el scheduler (y con él los demás jobs).
        log.exception("Falló la pasada de recordatorios de cita")
