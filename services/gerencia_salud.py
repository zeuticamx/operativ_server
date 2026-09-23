"""
Salud operativa de la plataforma: qué está roto, o a punto de romperse, en
cada negocio.

Cada detector es una consulta independiente que devuelve filas con la
misma forma (Problema). La pantalla las agrupa por `tipo`; sumar un
detector nuevo es escribir una función más y agregarla a DETECTORES, sin
tocar el router ni el frontend.

Todos miran el estado de AHORA, no una ventana elegida por el usuario: es
la pantalla que se abre en medio de un incidente, y ahí la pregunta es
"qué pasa ya", no "qué pasó el mes pasado".
"""

import html
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable
from uuid import UUID

from config import settings
from services.correo import ErrorEnvioCorreo, enviar_correo
from services.gerencia import SQL_AGENTE_OPERANDO
from session import fetch_all

log = logging.getLogger("operativai.gerencia.salud")

# Umbrales. Constantes y no settings: son criterio de "qué cuenta como
# problema", no algo que cambie entre entornos.
DIAS_AVISO_TOKEN_META = 7
HORAS_CANAL_SILENCIOSO = 48
# Un checkout que sigue en 'pendiente' después de esto es un webhook que no
# llegó (o llegó y falló), no un cliente que todavía está pagando.
HORAS_PAGO_PENDIENTE = 1
DIAS_MAX_PAGO_PENDIENTE = 7
# El job de pagos corre cada SUSCRIPCION_REVISION_INTERVALO_HORAS. Una
# suscripción vencida que sigue 'activa' más allá de dos ciclos quiere
# decir que el job no está corriendo.
HORAS_TOLERANCIA_JOB_PAGOS = 2

TIPO_ALERTA_CONSUMO = "consumo_anomalo"

ORDEN_SEVERIDAD = {"alta": 0, "media": 1, "baja": 2}


@dataclass
class Problema:
    tipo: str
    severidad: str
    tenant_id: UUID | None
    tenant_nombre: str | None
    detalle: str
    fecha: datetime | None
    datos: dict[str, Any] = field(default_factory=dict)


# ============================================================
# Detectores
# ============================================================
async def tokens_meta_por_vencer() -> list[Problema]:
    """
    El token de larga duración de Meta vence a los ~60 días. Si vence, el
    negocio deja de recibir y mandar mensajes sin ningún error visible de
    su lado: se entera cuando un cliente se queja de que nadie contesta.
    """
    filas = await fetch_all(
        """
        SELECT mc.tenant_id, t.name, mc.token_expires_at,
               mc.token_expires_at < NOW() AS vencido
        FROM meta_connections mc
        JOIN tenants t ON t.id = mc.tenant_id
        LEFT JOIN tenant_estado_plataforma ep ON ep.tenant_id = t.id
        WHERE mc.token_expires_at IS NOT NULL
          AND mc.token_expires_at < NOW() + make_interval(days => $1)
          AND COALESCE(ep.estado, 'activo') <> 'baja'
        ORDER BY mc.token_expires_at
        """,
        DIAS_AVISO_TOKEN_META,
    )
    return [
        Problema(
            tipo="token_meta",
            severidad="alta" if f["vencido"] else "media",
            tenant_id=f["tenant_id"],
            tenant_nombre=f["name"],
            detalle=(
                "El token de Meta ya venció: Facebook e Instagram no reciben mensajes"
                if f["vencido"]
                else "El token de Meta vence pronto: hay que reconectar la cuenta"
            ),
            fecha=f["token_expires_at"],
        )
        for f in filas
    ]


async def canales_silenciosos() -> list[Problema]:
    """
    Negocios que venían recibiendo mensajes y de golpe dejaron de hacerlo.

    Solo los que tuvieron actividad en los últimos 30 días: un negocio que
    nunca recibió nada no es un canal caído, es un negocio que todavía no
    arrancó (y ese caso ya lo cuenta el resumen como "pagan y no usan").
    """
    filas = await fetch_all(
        """
        SELECT t.id AS tenant_id, t.name, MAX(m.created_at)::timestamptz AS ultimo
        FROM tenants t
        JOIN messages m
          ON m.tenant_id = t.id
         AND m.created_at >= NOW() - INTERVAL '30 days'
        LEFT JOIN tenant_estado_plataforma ep ON ep.tenant_id = t.id
        WHERE COALESCE(ep.estado, 'activo') IN ('activo', 'prueba')
          AND EXISTS (
              SELECT 1 FROM channel_credentials cc
              WHERE cc.tenant_id = t.id AND cc.is_active
          )
        GROUP BY t.id, t.name
        HAVING MAX(m.created_at) < NOW() - make_interval(hours => $1)
        ORDER BY ultimo
        """,
        HORAS_CANAL_SILENCIOSO,
    )
    return [
        Problema(
            tipo="canal_silencioso",
            severidad="media",
            tenant_id=f["tenant_id"],
            tenant_nombre=f["name"],
            detalle=f"Tiene canales activos y no entra un mensaje hace más de {HORAS_CANAL_SILENCIOSO} h",
            fecha=f["ultimo"],
        )
        for f in filas
    ]


async def agentes_bloqueados_por_pago() -> list[Problema]:
    """
    El dueño tiene el agente encendido, el negocio no está suspendido, y
    aun así el agente no contesta porque se quedó sin plan y sin créditos.
    Sus clientes están escribiendo al vacío: es el caso que más urge
    avisarle al negocio, y el que el negocio menos probablemente sepa.
    """
    filas = await fetch_all(
        f"""
        SELECT g.tenant_id, g.nombre, g.estado_suscripcion, g.fecha_renovacion
        FROM v_gerencia_tenants g
        LEFT JOIN tenant_credits cr ON cr.tenant_id = g.tenant_id
        WHERE g.agente_ia_activo
          AND g.estado IN ('activo', 'prueba')
          AND NOT ({SQL_AGENTE_OPERANDO})
        ORDER BY g.nombre
        """
    )
    return [
        Problema(
            tipo="agente_bloqueado",
            severidad="alta",
            tenant_id=f["tenant_id"],
            tenant_nombre=f["nombre"],
            detalle=(
                f"Agente encendido pero bloqueado: suscripción "
                f"{f['estado_suscripcion'] or 'inexistente'} y sin créditos"
            ),
            fecha=f["fecha_renovacion"],
        )
        for f in filas
    ]


async def pagos_pendientes_trabados() -> list[Problema]:
    filas = await fetch_all(
        """
        SELECT tt.tenant_id, t.name, tt.id, tt.monto, tt.tipo, tt.created_at
        FROM tenant_transactions tt
        JOIN tenants t ON t.id = tt.tenant_id
        WHERE tt.estado_pago = 'pendiente'
          AND tt.created_at < NOW() - make_interval(hours => $1)
          AND tt.created_at > NOW() - make_interval(days => $2)
        ORDER BY tt.created_at
        """,
        HORAS_PAGO_PENDIENTE,
        DIAS_MAX_PAGO_PENDIENTE,
    )
    return [
        Problema(
            tipo="pago_pendiente",
            severidad="media",
            tenant_id=f["tenant_id"],
            tenant_nombre=f["name"],
            detalle=(
                f"Cobro de {f['monto']} ({f['tipo']}) sigue pendiente: "
                "revisar si llegó el webhook de la pasarela"
            ),
            fecha=f["created_at"],
            datos={"transaccion_id": str(f["id"])},
        )
        for f in filas
    ]


async def cobros_fallidos() -> list[Problema]:
    filas = await fetch_all(
        """
        SELECT s.tenant_id, t.name, s.plan, s.estado, s.intentos_fallidos,
               s.fecha_proximo_intento
        FROM tenant_subscriptions s
        JOIN tenants t ON t.id = s.tenant_id
        WHERE s.intentos_fallidos > 0
          AND s.estado <> 'cancelada'
        ORDER BY s.intentos_fallidos DESC
        """
    )
    return [
        Problema(
            tipo="cobro_fallido",
            severidad="alta" if f["estado"] == "pausada" else "media",
            tenant_id=f["tenant_id"],
            tenant_nombre=f["name"],
            detalle=(
                f"Plan {f['plan']}: {f['intentos_fallidos']} intento(s) de cobro "
                f"fallido(s), suscripción {f['estado']}"
            ),
            fecha=f["fecha_proximo_intento"],
        )
        for f in filas
    ]


async def suscripciones_sin_pausar() -> list[Problema]:
    """
    Vencidas hace rato pero todavía 'activa'. No es un problema del
    negocio sino de la plataforma: jobs/pagos_background.py no está
    corriendo, y mientras tanto nadie se bloquea por impago.
    """
    filas = await fetch_all(
        """
        SELECT s.tenant_id, t.name, s.fecha_renovacion
        FROM tenant_subscriptions s
        JOIN tenants t ON t.id = s.tenant_id
        WHERE s.estado = 'activa'
          AND s.fecha_renovacion < NOW() - make_interval(hours => $1)
        ORDER BY s.fecha_renovacion
        """,
        HORAS_TOLERANCIA_JOB_PAGOS,
    )
    return [
        Problema(
            tipo="suscripcion_sin_pausar",
            severidad="alta",
            tenant_id=f["tenant_id"],
            tenant_nombre=f["name"],
            detalle="Suscripción vencida que sigue activa: el job de pagos no la pausó",
            fecha=f["fecha_renovacion"],
        )
        for f in filas
    ]


async def consumo_anomalo() -> list[Problema]:
    filas = await detectar_consumo_anomalo()
    return [
        Problema(
            tipo=TIPO_ALERTA_CONSUMO,
            severidad="alta",
            tenant_id=f["tenant_id"],
            tenant_nombre=f["nombre"],
            detalle=_texto_anomalia(f),
            fecha=None,
            datos=_datos_anomalia(f),
        )
        for f in filas
    ]


DETECTORES: list[Callable[[], Awaitable[list[Problema]]]] = [
    agentes_bloqueados_por_pago,
    consumo_anomalo,
    tokens_meta_por_vencer,
    suscripciones_sin_pausar,
    cobros_fallidos,
    pagos_pendientes_trabados,
    canales_silenciosos,
]


async def problemas_de_salud() -> list[Problema]:
    """
    Corre todos los detectores. Uno que falla no tumba la pantalla: se
    loguea y se reporta como un problema más, porque en medio de un
    incidente es preferible ver seis de siete secciones que un error 500.
    """
    todos: list[Problema] = []
    for detector in DETECTORES:
        try:
            todos.extend(await detector())
        except Exception:
            log.exception("Falló el detector de salud %s", detector.__name__)
            todos.append(
                Problema(
                    tipo="detector_fallido",
                    severidad="media",
                    tenant_id=None,
                    tenant_nombre=None,
                    detalle=f"No se pudo revisar '{detector.__name__}' (ver log del backend)",
                    fecha=None,
                )
            )

    todos.sort(key=lambda p: (ORDEN_SEVERIDAD[p.severidad], p.tipo, p.tenant_nombre or ""))
    return todos


# ============================================================
# Consumo anómalo
# ============================================================
async def detectar_consumo_anomalo() -> list[Any]:
    """
    Negocios cuyo consumo de las últimas 24 h supera FACTOR veces su
    promedio diario de los 7 días anteriores.

    El promedio tiene un piso (CONSUMO_ANOMALO_PISO_TOKENS). Sin él, un
    negocio nuevo — promedio cero — dispararía la alerta con su primera
    respuesta, y uno chico con 200 tokens diarios la dispararía con mil.
    Lo que se quiere cazar es un loop de n8n que quema dinero, no el
    crecimiento normal de un cliente pequeño.

    Se compara contra "hace 24 h" y no contra "hoy" en calendario: el
    corte a medianoche haría que a las 00:05 todo negocio pareciera haber
    consumido casi nada.
    """
    return await fetch_all(
        """
        WITH ultimas AS (
            SELECT tenant_id, SUM(tokens_total) AS tokens_24h, SUM(costo_usd) AS costo_24h
            FROM tenant_token_usage
            WHERE created_at >= NOW() - INTERVAL '24 hours'
            GROUP BY tenant_id
        ),
        previas AS (
            SELECT tenant_id, SUM(tokens_total) / 7.0 AS promedio_diario
            FROM tenant_token_usage
            WHERE created_at >= NOW() - INTERVAL '8 days'
              AND created_at <  NOW() - INTERVAL '24 hours'
            GROUP BY tenant_id
        )
        SELECT
            u.tenant_id,
            t.name AS nombre,
            u.tokens_24h,
            COALESCE(u.costo_24h, 0) AS costo_24h,
            ROUND(COALESCE(p.promedio_diario, 0)) AS promedio_diario
        FROM ultimas u
        JOIN tenants t ON t.id = u.tenant_id
        LEFT JOIN previas p ON p.tenant_id = u.tenant_id
        WHERE u.tokens_24h >= $1::numeric * GREATEST(COALESCE(p.promedio_diario, 0), $2::numeric)
        ORDER BY u.tokens_24h DESC
        """,
        Decimal(str(settings.CONSUMO_ANOMALO_FACTOR)),
        Decimal(settings.CONSUMO_ANOMALO_PISO_TOKENS),
    )


def _texto_anomalia(f) -> str:
    promedio = int(f["promedio_diario"])
    if promedio == 0:
        return f"{int(f['tokens_24h']):,} tokens en 24 h sin historial previo"
    veces = int(f["tokens_24h"]) / promedio
    return f"{int(f['tokens_24h']):,} tokens en 24 h, {veces:.1f}× su promedio diario ({promedio:,})"


def _datos_anomalia(f) -> dict[str, Any]:
    return {
        "tokens_24h": int(f["tokens_24h"]),
        "promedio_diario": int(f["promedio_diario"]),
        "costo_24h_usd": str(f["costo_24h"]),
    }


async def registrar_alertas_consumo() -> list[dict[str, Any]]:
    """
    Persiste las anomalías como alertas de plataforma y devuelve solo las
    NUEVAS (las que no tenían ya una alerta abierta).

    El candado es el índice único parcial de 15_gerencia_alertas.sql: una
    alerta abierta por (tipo, tenant). El job corre cada hora y mientras el
    pico dure vuelve a detectar lo mismo; sin el índice serían 24 correos
    por día por un solo incidente.
    """
    nuevas: list[dict[str, Any]] = []
    for f in await detectar_consumo_anomalo():
        fila = await fetch_all(
            """
            INSERT INTO gerencia_alertas (tipo, tenant_id, titulo, detalle)
            VALUES ($1, $2, $3, $4::jsonb)
            ON CONFLICT (tipo, tenant_id) WHERE revisada_en IS NULL
            DO NOTHING
            RETURNING id
            """,
            TIPO_ALERTA_CONSUMO,
            f["tenant_id"],
            f"Consumo anómalo en {f['nombre']}",
            _datos_anomalia(f),
        )
        if fila:
            nuevas.append({"nombre": f["nombre"], "detalle": _texto_anomalia(f)})
    return nuevas


async def avisar_alertas_por_correo(nuevas: list[dict[str, Any]]) -> int:
    """Un correo por persona del equipo de plataforma. Devuelve cuántos salieron."""
    if not nuevas:
        return 0

    destinos = await fetch_all("SELECT email FROM gerencia_users ORDER BY email")
    lineas_texto = "\n".join(f"  - {a['nombre']}: {a['detalle']}" for a in nuevas)
    lineas_html = "\n".join(
        f'<li style="margin:0 0 8px;font-size:14px"><strong>{html.escape(a["nombre"])}</strong>: '
        f"{html.escape(a['detalle'])}</li>"
        for a in nuevas
    )
    url = (
        f"{settings.FRONTEND_ORIGINS[0]}/gerencia/salud"
        if settings.FRONTEND_ORIGINS
        else "/gerencia/salud"
    )
    plural = "negocios" if len(nuevas) != 1 else "negocio"
    asunto = f"Consumo anómalo en {len(nuevas)} {plural} - OperativAI"
    texto = f"Consumo de IA fuera de lo normal en las últimas 24 h:\n\n{lineas_texto}\n\nRevisar: {url}\n"
    cuerpo_html = f"""\
<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1c1c1c">
  <p style="margin:0 0 16px;font-size:16px">Consumo de IA fuera de lo normal en las últimas 24 h:</p>
  <ul style="margin:0 0 24px;padding-left:20px">
{lineas_html}
  </ul>
  <p style="margin:0"><a href="{url}" style="font-size:13px;color:#3b82f6;text-decoration:none">Ver salud de la plataforma →</a></p>
</div>"""

    enviados = 0
    for d in destinos:
        try:
            await enviar_correo(d["email"], asunto, texto, cuerpo_html)
            enviados += 1
        except ErrorEnvioCorreo:
            log.error("No se pudo avisar el consumo anómalo a %s", d["email"])
    return enviados
