"""
Job de plataforma: detecta consumo de IA anómalo y avisa al equipo de
OperativAI (gerencia_users), no al negocio.

Corre dentro del mismo AsyncIOScheduler que las alertas de leads (ver
jobs/alertas_background.iniciar_scheduler); valen las mismas advertencias
sobre correr el backend con más de un proceso. Acá además el candado de
duplicados es el índice único de gerencia_alertas, así que dos procesos
corriendo a la vez no mandan dos correos por el mismo incidente.
"""

import logging

from services.gerencia_salud import avisar_alertas_por_correo, registrar_alertas_consumo

log = logging.getLogger("operativai.jobs.gerencia")

JOB_ID = "consumo_anomalo"


async def job_consumo_anomalo() -> None:
    nuevas = await registrar_alertas_consumo()
    if not nuevas:
        return

    enviados = await avisar_alertas_por_correo(nuevas)
    log.warning(
        "Consumo anómalo: %s alerta(s) nueva(s), %s correo(s) al equipo de plataforma",
        len(nuevas),
        enviados,
    )
