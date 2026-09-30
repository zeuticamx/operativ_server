"""
Avisos al equipo de plataforma (gerencia_users) sobre el ciclo de vida de
las suscripciones de los negocios.

No confundir con services/notificaciones.py (avisos a vendedores de UN
negocio) ni con las alertas de la campana del portal: esto le habla al
equipo de OperativAI sobre un cliente, y el cliente no lo ve.

Dos canales, los mismos que ya usa el detector de consumo anómalo
(services/gerencia_salud.py), así que no hace falta ninguna dependencia:

  - correo (services/correo.py) a cada gerencia_users, en TODOS los eventos
    — incluidas altas y renovaciones, que son buenas noticias pero gerencia
    quiere enterarse igual;
  - una fila en gerencia_alertas solo para los eventos que piden que
    alguien haga algo (cobro fallido, cancelación, pausa): son los que se
    ven en /gerencia/salud hasta que alguien los marca revisados. Una
    renovación mensual ahí sería ruido.

Quien llama ya deduplicó el evento (Stripe reintenta los webhooks): acá
cada llamada es un aviso real.
"""

import html
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from config import settings
from services.correo import ErrorEnvioCorreo, enviar_correo
from session import fetch_all, fetch_one

log = logging.getLogger("operativai.notificaciones.gerencia")

TipoEventoSuscripcion = Literal[
    "alta",
    "renovacion",
    "pago_fallido",
    "cancelacion_programada",
    "cancelacion_revertida",
    "cancelada",
    "pausada",
]


@dataclass(frozen=True)
class _InfoTipo:
    # "{negocio}" se sustituye por el nombre del tenant.
    titulo: str
    # Tipo en gerencia_alertas. None = solo correo, sin alerta abierta.
    alerta: str | None
    # Qué dice la fecha_renovacion en este evento.
    etiqueta_fecha: str


INFO_TIPOS: dict[str, _InfoTipo] = {
    "alta": _InfoTipo("Nueva suscripción: {negocio}", None, "Próxima renovación"),
    "renovacion": _InfoTipo("Suscripción renovada: {negocio}", None, "Próxima renovación"),
    "pago_fallido": _InfoTipo(
        "Cobro de suscripción fallido: {negocio}", "stripe_pago_fallido", "Vigente hasta"
    ),
    "cancelacion_programada": _InfoTipo(
        "Cancelación programada: {negocio}", "stripe_cancelacion_programada", "Vigente hasta"
    ),
    "cancelacion_revertida": _InfoTipo(
        "Cancelación revertida: {negocio}", None, "Próxima renovación"
    ),
    "cancelada": _InfoTipo(
        "Suscripción cancelada: {negocio}", "stripe_suscripcion_cancelada", "Vigente hasta"
    ),
    "pausada": _InfoTipo(
        "Suscripción pausada: {negocio}", "stripe_suscripcion_pausada", "Venció el"
    ),
}


@dataclass
class EventoSuscripcion:
    """Todo lo que gerencia necesita saber de un evento, ya interpretado."""

    tipo: TipoEventoSuscripcion
    tenant_id: UUID
    plan: str | None = None
    monto: Decimal | None = None
    moneda: str | None = None
    fecha_renovacion: datetime | None = None
    # Cobros fallidos: número de intento de Stripe y cuándo reintenta
    # (None = no va a reintentar más).
    intento: int | None = None
    proximo_intento: datetime | None = None
    # Por qué pasó, si Stripe lo dice (motivo de cancelación, error de cobro).
    motivo: str | None = None
    # Qué hizo el sistema solo, en una frase ("sigue vigente hasta X y
    # después se pausa"). Es lo que evita que gerencia tenga que adivinar.
    accion: str | None = None
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None
    stripe_invoice_id: str | None = None


def _fecha(valor: datetime | None) -> str:
    return valor.strftime("%d/%m/%Y %H:%M UTC") if valor else "—"


def _url_tenant(tenant_id: UUID) -> str:
    base = settings.FRONTEND_ORIGINS[0] if settings.FRONTEND_ORIGINS else ""
    return f"{base}/gerencia/tenants/{tenant_id}"


def renglones(evento: EventoSuscripcion, negocio: str, dueno: str | None) -> list[tuple[str, str]]:
    """El detalle del evento como pares (etiqueta, valor), para texto y HTML."""
    info = INFO_TIPOS[evento.tipo]
    filas: list[tuple[str, str]] = [
        ("Negocio", negocio),
        ("Correo del dueño", dueno or "—"),
        ("Plan", evento.plan or "—"),
    ]
    if evento.monto is not None:
        filas.append(("Monto", f"{evento.monto:,.2f} {(evento.moneda or '').upper()}".strip()))
    if evento.intento:
        filas.append(("Intento de cobro", str(evento.intento)))
    if evento.tipo == "pago_fallido":
        filas.append(
            (
                "Próximo reintento",
                _fecha(evento.proximo_intento) if evento.proximo_intento else "Stripe no reintentará",
            )
        )
    if evento.fecha_renovacion:
        filas.append((info.etiqueta_fecha, _fecha(evento.fecha_renovacion)))
    if evento.motivo:
        filas.append(("Motivo", evento.motivo))
    if evento.accion:
        filas.append(("Qué hizo el sistema", evento.accion))
    for etiqueta, valor in (
        ("Customer de Stripe", evento.stripe_customer_id),
        ("Subscription de Stripe", evento.stripe_subscription_id),
        ("Factura de Stripe", evento.stripe_invoice_id),
    ):
        if valor:
            filas.append((etiqueta, valor))
    filas.append(("Tenant ID", str(evento.tenant_id)))
    return filas


def _detalle_json(evento: EventoSuscripcion) -> dict[str, Any]:
    """Lo mismo que el correo, en el JSONB de gerencia_alertas."""
    crudo = {
        "evento": evento.tipo,
        "plan": evento.plan,
        "monto": str(evento.monto) if evento.monto is not None else None,
        "moneda": evento.moneda,
        "fecha_renovacion": evento.fecha_renovacion.isoformat() if evento.fecha_renovacion else None,
        "intento": evento.intento,
        "proximo_intento": evento.proximo_intento.isoformat() if evento.proximo_intento else None,
        "motivo": evento.motivo,
        "accion": evento.accion,
        "stripe_customer_id": evento.stripe_customer_id,
        "stripe_subscription_id": evento.stripe_subscription_id,
        "stripe_invoice_id": evento.stripe_invoice_id,
    }
    return {k: v for k, v in crudo.items() if v is not None}


async def _ficha_tenant(tenant_id: UUID) -> tuple[str, str | None]:
    """Nombre del negocio y correo del dueño (el owner activo más antiguo)."""
    fila = await fetch_one(
        """
        SELECT t.name,
               (SELECT pu.email
                  FROM portal_users pu
                 WHERE pu.tenant_id = t.id AND pu.role = 'owner' AND pu.is_active
                 ORDER BY pu.created_at
                 LIMIT 1) AS email_dueno
        FROM tenants t
        WHERE t.id = $1
        """,
        tenant_id,
    )
    if fila is None:
        return f"Tenant {tenant_id}", None
    return fila["name"], fila["email_dueno"]


async def _registrar_alerta(evento: EventoSuscripcion, titulo: str) -> None:
    """
    Abre (o actualiza) la alerta de plataforma del evento. Una sola abierta
    por (tipo, tenant) — el índice de 15_gerencia_alertas.sql —: si Stripe
    falla el segundo reintento con la del primero todavía abierta, se
    actualiza con el dato nuevo en vez de apilar otra.
    """
    tipo_alerta = INFO_TIPOS[evento.tipo].alerta
    if tipo_alerta is None:
        return
    await fetch_all(
        """
        INSERT INTO gerencia_alertas (tipo, tenant_id, titulo, detalle)
        VALUES ($1, $2, $3, $4::jsonb)
        ON CONFLICT (tipo, tenant_id) WHERE revisada_en IS NULL
        DO UPDATE SET titulo = EXCLUDED.titulo, detalle = EXCLUDED.detalle
        """,
        tipo_alerta,
        evento.tenant_id,
        titulo,
        _detalle_json(evento),
    )


def armar_correo(
    evento: EventoSuscripcion, negocio: str, dueno: str | None
) -> tuple[str, str, str]:
    """(asunto, texto, html) del aviso. Separado para poder probarlo."""
    titulo = INFO_TIPOS[evento.tipo].titulo.format(negocio=negocio)
    filas = renglones(evento, negocio, dueno)
    url = _url_tenant(evento.tenant_id)

    texto = (
        f"{titulo}\n\n"
        + "\n".join(f"  {etiqueta}: {valor}" for etiqueta, valor in filas)
        + f"\n\nVer negocio: {url}\n"
    )
    filas_html = "\n".join(
        f'    <tr><td style="padding:4px 12px 4px 0;color:#666;font-size:13px;vertical-align:top">'
        f"{html.escape(etiqueta)}</td>"
        f'<td style="padding:4px 0;font-size:13px">{html.escape(valor)}</td></tr>'
        for etiqueta, valor in filas
    )
    cuerpo_html = f"""\
<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#1c1c1c">
  <p style="margin:0 0 16px;font-size:16px;font-weight:600">{html.escape(titulo)}</p>
  <table style="border-collapse:collapse;margin:0 0 24px">
{filas_html}
  </table>
  <p style="margin:0"><a href="{html.escape(url)}" style="font-size:13px;color:#3b82f6;text-decoration:none">Ver negocio en gerencia →</a></p>
</div>"""
    return f"{titulo} - OperativAI", texto, cuerpo_html


async def notificar_evento_suscripcion(evento: EventoSuscripcion) -> int:
    """
    Avisa a gerencia del evento. Devuelve cuántos correos salieron.

    Corre como BackgroundTask del webhook o desde el job de pagos: nada
    puede escapar de acá, un SMTP caído no tiene que tumbar el
    procesamiento del cobro (que ya quedó escrito antes de llamar a esto).
    """
    try:
        negocio, dueno = await _ficha_tenant(evento.tenant_id)
        asunto, texto, cuerpo_html = armar_correo(evento, negocio, dueno)

        await _registrar_alerta(evento, INFO_TIPOS[evento.tipo].titulo.format(negocio=negocio))

        destinos = await fetch_all("SELECT email FROM gerencia_users ORDER BY email")
        enviados = 0
        for d in destinos:
            try:
                await enviar_correo(d["email"], asunto, texto, cuerpo_html)
                enviados += 1
            except ErrorEnvioCorreo:
                log.error("No se pudo avisar '%s' a %s", evento.tipo, d["email"])

        log.info(
            "Aviso a gerencia: %s del tenant %s (%s correo(s))",
            evento.tipo, evento.tenant_id, enviados,
        )
        return enviados
    except Exception:  # noqa: BLE001 - background: nada puede escapar
        log.exception("Falló el aviso a gerencia (%s, tenant %s)", evento.tipo, evento.tenant_id)
        return 0
