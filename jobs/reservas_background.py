"""
Job de background: las citas confirmadas que ya pasaron se dan por
'no_asistio' solas (services/calendario.marcar_vencidas_no_asistio).

Sin esto, una cita que nadie marcó se quedaba 'confirmada' para siempre:
ensuciaba la agenda del proveedor y el reporte de asistencia. Si en
realidad sí se atendió, se corrige a 'completada' desde el portal (con su
cobro), y la bitácora muestra los dos pasos.

Vive en el scheduler de jobs/alertas_background.py, mismo motivo que el
resto de los jobs: un solo AsyncIOScheduler por proceso.
"""

import logging

from config import settings
from services.calendario import marcar_vencidas_no_asistio

log = logging.getLogger("operativai.jobs.reservas")

JOB_ID = "reservas_vencidas"


async def job_reservas_vencidas() -> None:
    try:
        marcadas = await marcar_vencidas_no_asistio(settings.RESERVAS_VENCIDAS_GRACIA_MINUTOS)
        if marcadas:
            log.info("Citas vencidas marcadas como no_asistio: %s", marcadas)
    except Exception:
        # Un fallo acá no debe tumbar el scheduler (y con él los demás jobs).
        log.exception("Falló la revisión de citas vencidas")
