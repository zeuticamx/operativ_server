"""
Envío de correo por SMTP.

Se usa `smtplib` de la biblioteca estándar en vez de un cliente async: el
único correo que manda el portal hoy es el código de verificación, uno por
alta. No justifica una dependencia más. Como smtplib bloquea, la conexión
se hace en un hilo aparte con `asyncio.to_thread` para no frenar el loop.
"""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from config import settings

log = logging.getLogger("operativai.correo")


class ErrorEnvioCorreo(RuntimeError):
    """El servidor SMTP rechazó el mensaje o no se pudo contactar."""


# ============================================================
# Transporte
# ============================================================
def _armar_mensaje(destino: str, asunto: str, texto: str, html: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((settings.SMTP_FROM_NAME, settings.SMTP_FROM))
    msg["To"] = destino
    msg["Subject"] = asunto
    # Texto plano primero y HTML como alternativa: el cliente de correo
    # elige. Sin la versión de texto, varios filtros de spam penalizan.
    msg.set_content(texto)
    msg.add_alternative(html, subtype="html")
    return msg


def _enviar_sync(msg: EmailMessage) -> None:
    contexto = ssl.create_default_context()

    # El 465 habla TLS desde el saludo (SMTPS); el 587 arranca en claro y
    # sube a TLS con STARTTLS. Son dos clases distintas, no un flag.
    if settings.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(
            settings.SMTP_HOST, settings.SMTP_PORT, context=contexto, timeout=20
        ) as servidor:
            if settings.SMTP_USER:
                servidor.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            servidor.send_message(msg)
        return

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as servidor:
        if settings.SMTP_STARTTLS:
            servidor.starttls(context=contexto)
        if settings.SMTP_USER:
            servidor.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        servidor.send_message(msg)


async def enviar_correo(destino: str, asunto: str, texto: str, html: str) -> None:
    """
    Manda el correo. Lanza ErrorEnvioCorreo si el SMTP falla.

    Sin SMTP configurado no falla: escribe el cuerpo en el log. Eso permite
    desarrollar el flujo completo sin proveedor, pero significa que si en
    producción falta la configuración los códigos terminan en el log del
    servidor y nadie recibe nada. Por eso el aviso es de nivel WARNING y
    settings.validate() lo repite al arrancar.
    """
    if not settings.smtp_configurado:
        log.warning(
            "SMTP sin configurar: el correo para %s no se envió.\n"
            "--- %s ---\n%s\n---",
            destino,
            asunto,
            texto,
        )
        return

    msg = _armar_mensaje(destino, asunto, texto, html)
    try:
        await asyncio.to_thread(_enviar_sync, msg)
    except (smtplib.SMTPException, OSError) as e:
        # El detalle va al log; al cliente solo le llega que no se pudo.
        log.error("Fallo al enviar correo a %s: %s", destino, e)
        raise ErrorEnvioCorreo(str(e)) from e


# ============================================================
# Código de verificación
# ============================================================
_TEXTO = """Tu código para crear la cuenta de {negocio} en OperativAI es:

    {codigo}

Vence en {minutos} minutos.

Si no fuiste tú quien pidió crear esta cuenta, ignora este correo: sin el
código no se crea nada.
"""

_HTML = """\
<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:420px;margin:0 auto;padding:24px;color:#1c1c1c">
  <p style="margin:0 0 20px;font-size:14px">
    Tu código para crear la cuenta de <strong>{negocio}</strong> en OperativAI:
  </p>
  <p style="margin:0 0 20px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:32px;font-weight:600;letter-spacing:8px">
    {codigo}
  </p>
  <p style="margin:0 0 20px;font-size:13px;color:#666">
    Vence en {minutos} minutos.
  </p>
  <p style="margin:0;font-size:12px;color:#888">
    Si no fuiste tú quien pidió crear esta cuenta, ignora este correo:
    sin el código no se crea nada.
  </p>
</div>"""


async def enviar_codigo_verificacion(
    destino: str, codigo: str, nombre_negocio: str
) -> None:
    minutos = settings.CODIGO_VIGENCIA_MINUTOS
    datos = {"codigo": codigo, "minutos": minutos, "negocio": nombre_negocio}
    await enviar_correo(
        destino,
        asunto=f"{codigo} es tu código de OperativAI",
        texto=_TEXTO.format(**datos),
        html=_HTML.format(**datos),
    )


# ============================================================
# Alerta crítica (envío inmediato, una alerta = un correo)
# ============================================================
_TEXTO_ALERTA = """{titulo}

{mensaje}

Ver en OperativAI: {url_panel}
"""

_HTML_ALERTA = """\
<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:480px;margin:0 auto;padding:24px;color:#1c1c1c">
  <p style="margin:0 0 16px;font-size:18px;font-weight:600">{titulo}</p>
  <p style="margin:0 0 24px;font-size:14px;color:#333">{mensaje}</p>
  <p style="margin:0">
    <a href="{url_panel}" style="font-size:13px;color:#3b82f6;text-decoration:none">
      Ver en OperativAI →
    </a>
  </p>
</div>"""


def _url_panel() -> str:
    """
    Primer origen configurado en FRONTEND_ORIGINS: es el dominio real del
    panel en producción (localhost en dev), no un valor hardcodeado aparte
    que se puede desincronizar de settings.FRONTEND_ORIGINS.
    """
    if settings.FRONTEND_ORIGINS:
        return f"{settings.FRONTEND_ORIGINS[0]}/vendedores"
    return "/vendedores"


async def enviar_alerta_critica(destino: str, titulo: str, mensaje: str) -> None:
    datos = {"titulo": titulo, "mensaje": mensaje, "url_panel": _url_panel()}
    await enviar_correo(
        destino,
        asunto=f"⚠️ {titulo} - OperativAI",
        texto=_TEXTO_ALERTA.format(**datos),
        html=_HTML_ALERTA.format(**datos),
    )


# ============================================================
# Resumen diario de alertas sin leer
# ============================================================
_TEXTO_RESUMEN_ITEM = "  - {titulo}"

_HTML_RESUMEN_ITEM = """\
        <li style="margin:0 0 8px;font-size:14px;color:#333">{titulo}</li>"""


async def enviar_resumen_alertas(
    destino: str, nombre_negocio: str, alertas: list[dict]
) -> None:
    """
    Un correo por tenant con todas las alertas que siguen sin marcarse
    leídas. `alertas` ya viene ordenada y recortada por quien llama
    (jobs.alertas_background): acá solo arma el correo.
    """
    total = len(alertas)
    url_panel = _url_panel()

    texto_items = "\n".join(
        _TEXTO_RESUMEN_ITEM.format(titulo=a["titulo"]) for a in alertas
    )
    html_items = "\n".join(
        _HTML_RESUMEN_ITEM.format(titulo=a["titulo"]) for a in alertas
    )

    plural = "alertas" if total != 1 else "alerta"
    texto = (
        f"{nombre_negocio} tiene {total} {plural} sin leer en OperativAI:\n\n"
        f"{texto_items}\n\n"
        f"Ver todas: {url_panel}\n"
    )
    html = f"""\
<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:480px;margin:0 auto;padding:24px;color:#1c1c1c">
  <p style="margin:0 0 16px;font-size:16px">
    <strong>{nombre_negocio}</strong> tiene <strong>{total}</strong> {plural} sin leer en OperativAI:
  </p>
  <ul style="margin:0 0 24px;padding-left:20px">
{html_items}
  </ul>
  <p style="margin:0">
    <a href="{url_panel}" style="font-size:13px;color:#3b82f6;text-decoration:none">
      Ver todas →
    </a>
  </p>
</div>"""

    await enviar_correo(
        destino,
        asunto=f"Resumen diario: {total} {plural} sin leer - OperativAI",
        texto=texto,
        html=html,
    )
