"""
Jobs de background de los números de WhatsApp (spec 005a, T8).

Tres trabajos, registrados en el scheduler de jobs/alertas_background.py (mismo
motivo que el resto: un solo AsyncIOScheduler por proceso):

  - cada minuto (`job_numeros_reevaluacion`): drena la cola de reevaluación del
    cupo que dejan los triggers de sql/59 y reevalúa las suscripciones y pruebas
    que el reloj dejó sin vigencia. La PRIMERA pasada lanza además el despliegue de
    una sola vez (RF-22, P19);
  - cada hora (`job_limpiar_intentos`): borra los intentos de alta ajena de más de
    25 horas (RF-2, P9);
  - una vez al día (`job_retencion_ocultas`): elimina las conversaciones de
    números desvinculados hace más de 12 meses (RF-14, P10).

El scheduler es en proceso: con varios procesos pueden solaparse. Por eso todo es
idempotente y cada negocio se reevalúa bajo `pg_advisory_xact_lock`.

Ningún trabajo propaga un error: uno que falle no debe tumbar al scheduler ni a
los demás jobs.
"""

import logging

from services import numeros_reevaluacion
from session import conexion

log = logging.getLogger("operativai.jobs.numeros")

JOB_ID_REEVALUACION = "numeros_reevaluacion"
JOB_ID_INTENTOS = "numeros_limpiar_intentos"
JOB_ID_RETENCION = "numeros_retencion_ocultas"

# True cuando el despliegue ya se ejecutó (o ya estaba hecho) en este proceso: así las
# pasadas siguientes no vuelven a preguntar a la base por la marca.
_despliegue_hecho = False


async def job_numeros_reevaluacion() -> None:
    global _despliegue_hecho
    try:
        async with conexion() as conn:
            if not _despliegue_hecho:
                try:
                    resultado = await numeros_reevaluacion.ejecutar_despliegue_una_vez(conn)
                    # None = ya estaba hecho (o lo está haciendo otra instancia).
                    if resultado is None or resultado.errores == 0:
                        _despliegue_hecho = True
                    if resultado is not None:
                        log.info(
                            "Despliegue 005a: %s negocios reevaluados, %s números pausados, "
                            "%s negocios avisados de configuración incompleta, %s errores",
                            resultado.negocios_reevaluados,
                            resultado.numeros_pausados,
                            resultado.negocios_con_aviso_incompleto,
                            resultado.errores,
                        )
                except Exception:
                    log.exception("Falló el despliegue de los números de WhatsApp; se reintenta en la siguiente pasada")

            atendidos = await numeros_reevaluacion.drenar_cola(conn)
            vencidos = await numeros_reevaluacion.reevaluar_vencidos(conn)
            if atendidos or vencidos:
                log.info("Reevaluación de números: %s por la cola, %s por vencimiento", atendidos, vencidos)
    except Exception:
        log.exception("Falló la reevaluación de los números de WhatsApp")


async def job_limpiar_intentos() -> None:
    try:
        async with conexion() as conn:
            borrados = await numeros_reevaluacion.limpiar_intentos(conn)
            if borrados:
                log.info("Intentos de alta ajena borrados: %s", borrados)
    except Exception:
        log.exception("Falló la limpieza de intentos de alta ajena")


async def job_retencion_ocultas() -> None:
    try:
        async with conexion() as conn:
            conversaciones, numeros = await numeros_reevaluacion.aplicar_retencion(conn)
            if conversaciones or numeros:
                log.info(
                    "Retención de 12 meses: %s conversaciones y %s números desvinculados eliminados",
                    conversaciones,
                    numeros,
                )
    except Exception:
        log.exception("Falló la retención de conversaciones ocultas")
