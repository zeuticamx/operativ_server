"""
Job de background: alerta cuando un lead lleva mucho tiempo sin moverse.

No hay tabla de jobs ni cola aparte: es un único AsyncIOScheduler
(APScheduler) que vive dentro del mismo proceso de uvicorn, arrancado y
parado desde el lifespan de main.py. Alcanza para una sola instancia; si el
backend llega a correr en más de un worker/proceso a la vez, cada uno
dispararía su propia revisión en paralelo — el candado de duplicados por
alerta evita que eso duplique alertas, pero no evita el trabajo repetido de
recorrer la tabla dos veces. Ese es el momento de sacar esto del proceso web
(un cron aparte, o un job de verdad en algo como Celery).
"""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import settings
from deps import ROLES_GERENCIA
from realtime import broadcast_alerta
from services.correo import ErrorEnvioCorreo, enviar_resumen_alertas
from services.pipeline_estados import ESTADOS_CERRADOS
from session import fetch_all, fetch_value

log = logging.getLogger("operativai.jobs.alertas")

JOB_ID = "alertas_sin_actividad"
JOB_ID_RESUMEN = "resumen_diario_alertas"

# Una alerta por lead cada 24h como máximo, sin importar cada cuánto corre
# el job: correrlo seguido sirve para detectar pronto el cruce del umbral de
# `ALERTA_SIN_ACTIVIDAD_DIAS`, no para insistir con el mismo lead cada hora.
_VENTANA_DEDUP_HORAS = 24

# Mismo tratamiento del "null" literal y mismo fallback nombre/handle que
# _NOMBRE_CLIENTE/_HANDLE_CLIENTE en routers/vendedores.py: n8n interpola
# undefined como esa cadena al insertar en `users`. `client_pipeline` no
# guarda el nombre del cliente — sale de `users` igual que en el resto del
# módulo de vendedores.
_SELECT_INACTIVOS = """
    SELECT
        cp.tenant_id,
        cp.user_id,
        NULLIF(NULLIF(TRIM(u.display_name), ''), 'null') AS cliente_nombre,
        COALESCE(
            NULLIF(NULLIF(TRIM(u.whatsapp_id), ''), 'null'),
            NULLIF(NULLIF(TRIM(u.instagram_id), ''), 'null'),
            NULLIF(NULLIF(TRIM(u.facebook_id), ''), 'null')
        ) AS cliente_handle,
        cp.vendedor_id,
        v.nombre AS vendedor_nombre,
        cp.actualizado_en
    FROM client_pipeline cp
    JOIN users u ON u.id = cp.user_id
    LEFT JOIN vendedores v ON v.id = cp.vendedor_id
    WHERE cp.estado <> ALL($1::text[])
      AND cp.actualizado_en < $2
    ORDER BY cp.actualizado_en ASC
"""

_YA_AVISADO = """
    SELECT id FROM alertas
    WHERE tenant_id = $1
      AND tipo = 'sin_actividad'
      AND datos->>'cliente_id' = $2
      AND creado_en > NOW() - make_interval(hours => $3)
    LIMIT 1
"""


async def job_detectar_sin_actividad() -> None:
    """
    Una pasada sobre todos los tenants. La llama el scheduler; no hay
    endpoint HTTP para esto porque no hace falta pedirlo a mano — si algún
    día hace falta un botón de "revisar ahora", es un one-liner en un router
    que llame a esta misma función.
    """
    limite = datetime.now(timezone.utc) - timedelta(days=settings.ALERTA_SIN_ACTIVIDAD_DIAS)

    inactivos = await fetch_all(_SELECT_INACTIVOS, list(ESTADOS_CERRADOS), limite)
    if not inactivos:
        return

    disparadas = 0
    for lead in inactivos:
        ya_avisado = await fetch_value(
            _YA_AVISADO,
            lead["tenant_id"],
            str(lead["user_id"]),
            _VENTANA_DEDUP_HORAS,
        )
        if ya_avisado is not None:
            continue

        nombre = lead["cliente_nombre"] or lead["cliente_handle"] or "Cliente sin nombre"
        # Real, no el fijo `7` del umbral: dos leads que cruzaron el límite
        # en corridas distintas llevan tiempos distintos de verdad.
        dias_inactivo = (datetime.now(timezone.utc) - lead["actualizado_en"]).days

        await broadcast_alerta(
            lead["tenant_id"],
            tipo="sin_actividad",
            titulo="⚠️ Lead sin actividad",
            mensaje=(
                f"{nombre} (a cargo de {lead['vendedor_nombre']}) "
                f"lleva {dias_inactivo} días sin movimiento"
                if lead["vendedor_nombre"]
                else f"{nombre} lleva {dias_inactivo} días sin movimiento y sin vendedor asignado"
            ),
            datos={
                "cliente_id": str(lead["user_id"]),
                "cliente_nombre": nombre,
                "vendedor_id": str(lead["vendedor_id"]) if lead["vendedor_id"] else None,
                "vendedor_nombre": lead["vendedor_nombre"],
                "dias_inactivo": dias_inactivo,
                "ultima_actualizacion": lead["actualizado_en"].isoformat(),
            },
        )
        disparadas += 1

    log.info(
        "Revisión de inactividad: %s leads sin movimiento, %s alertas nuevas",
        len(inactivos),
        disparadas,
    )


_SELECT_NO_LEIDAS = """
    SELECT a.tenant_id, t.name AS tenant_nombre, a.titulo
    FROM alertas a
    JOIN tenants t ON t.id = a.tenant_id
    WHERE a.leido = false
    ORDER BY a.tenant_id, a.creado_en DESC
"""

# Mismo query que _emails_gerencia en routers/alertas.py: se duplica en vez
# de importar un helper privado de otro módulo por cuatro líneas de SQL.
_SELECT_EMAILS_GERENCIA = """
    SELECT email FROM portal_users
    WHERE tenant_id = $1 AND role = ANY($2::text[]) AND is_active = true
"""


async def job_resumen_diario_alertas() -> None:
    """
    Una vez al día (RESUMEN_ALERTAS_HORA_UTC), un correo por gerencia con
    todo lo que ese tenant sigue teniendo sin leer. No hay dedupe entre
    corridas como en job_detectar_sin_actividad: corre una sola vez por día
    a hora fija, no hace falta.
    """
    filas = await fetch_all(_SELECT_NO_LEIDAS)
    if not filas:
        return

    por_tenant: dict[str, dict] = {}
    for fila in filas:
        grupo = por_tenant.setdefault(
            fila["tenant_id"], {"nombre": fila["tenant_nombre"], "alertas": []}
        )
        grupo["alertas"].append({"titulo": fila["titulo"]})

    correos_enviados = 0
    for tenant_id, info in por_tenant.items():
        emails = await fetch_all(_SELECT_EMAILS_GERENCIA, tenant_id, list(ROLES_GERENCIA))
        for fila_email in emails:
            try:
                await enviar_resumen_alertas(
                    fila_email["email"], info["nombre"], info["alertas"]
                )
                correos_enviados += 1
            except ErrorEnvioCorreo:
                log.error(
                    "No se pudo mandar el resumen diario a %s (tenant=%s)",
                    fila_email["email"],
                    tenant_id,
                )

    log.info(
        "Resumen diario de alertas: %s tenants con pendientes, %s correos enviados",
        len(por_tenant),
        correos_enviados,
    )


def iniciar_scheduler() -> AsyncIOScheduler:
    """Arranca el scheduler. Se llama una sola vez, desde el lifespan de main.py."""
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        job_detectar_sin_actividad,
        "interval",
        hours=settings.ALERTA_SIN_ACTIVIDAD_INTERVALO_HORAS,
        id=JOB_ID,
        name="Detectar leads sin actividad",
        # Si una corrida se alarga (tenant con mucho volumen), la próxima
        # espera en vez de apilarse encima.
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_resumen_diario_alertas,
        CronTrigger(hour=settings.RESUMEN_ALERTAS_HORA_UTC, minute=0),
        id=JOB_ID_RESUMEN,
        name="Resumen diario de alertas por correo",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info(
        "Scheduler de alertas iniciado: sin_actividad cada %sh (umbral %s días), "
        "resumen diario a las %s:00 UTC",
        settings.ALERTA_SIN_ACTIVIDAD_INTERVALO_HORAS,
        settings.ALERTA_SIN_ACTIVIDAD_DIAS,
        settings.RESUMEN_ALERTAS_HORA_UTC,
    )
    return scheduler
