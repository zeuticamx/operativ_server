"""
Reportes de incidencias que los usuarios mandan desde el portal.

("incidencias" y no "reportes": ese nombre ya es el de los reportes
analíticos del embudo, routers/reportes.py.)

Qué se guarda y qué se ignora:
  - Quién reporta (usuario, correo, negocio) sale del JWT. Nada del
    formulario puede cambiarlo.
  - El `contexto` técnico lo manda el navegador: sirve para depurar pero no
    es de fiar, así que solo pasan las claves de CLAVES_CONTEXTO, con cada
    valor recortado. Un contexto ilegible no rompe el envío: queda vacío.
  - El adjunto es una imagen PNG/JPEG validada por su firma de bytes
    (services/imagen.py), no por lo que diga el nombre o el Content-Type.

Tope de REPORTES_MAX_POR_HORA por usuario. El conteo y el INSERT van en una
transacción con un candado por usuario: dos envíos simultáneos no pueden
colarse los dos por el mismo hueco.

El aviso al equipo es un correo a cada gerencia_users en background: el
reporte ya está guardado antes de intentarlo, y un SMTP caído no se le
muestra al usuario como un error suyo.
"""

import html
import json
import logging
import math
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from config import settings
from services.correo import ErrorEnvioCorreo, enviar_correo
from services.imagen import ImagenInvalida, validar_foto
from session import fetch_all, fetch_one, get_pool

log = logging.getLogger("operativai.reportes")

RESUMEN_MIN, RESUMEN_MAX = 5, 120
DESCRIPCION_MIN, DESCRIPCION_MAX = 10, 4000

# Lo único que se conserva del contexto del navegador, y el tope de cada
# valor de texto.
CLAVES_CONTEXTO: dict[str, int] = {
    "ruta": 500,
    "navegador": 100,
    "sistema_operativo": 100,
    "user_agent": 500,
    "idioma": 35,
    "zona_horaria": 64,
}
CLAVES_MEDIDAS = ("pantalla", "ventana")  # {"ancho": int, "alto": int}
_MEDIDA_MAX = 20_000


class ReporteInvalido(Exception):
    """Dato del reporte que no se acepta. `status_code` es el HTTP."""

    def __init__(self, status_code: int, mensaje: str):
        super().__init__(mensaje)
        self.status_code = status_code
        self.mensaje = mensaje


class LimiteDeReportes(Exception):
    def __init__(self, reintentar_en: int):
        super().__init__("Límite de reportes alcanzado")
        self.reintentar_en = reintentar_en


@dataclass(frozen=True)
class Adjunto:
    contenido: bytes
    mime: str
    ancho: int
    alto: int


# ============================================================
# Validación
# ============================================================
def limpiar_texto(valor: str, minimo: int, maximo: int, campo: str, *, una_linea: bool) -> str:
    """
    Recorta espacios y valida el largo. `una_linea` colapsa saltos y
    espacios repetidos: el resumen viaja en el asunto del correo, y un
    salto de línea ahí es una inyección de cabeceras.
    """
    texto = " ".join(valor.split()) if una_linea else valor.strip()
    if len(texto) < minimo:
        raise ReporteInvalido(422, f"{campo}: escribe al menos {minimo} caracteres.")
    if len(texto) > maximo:
        raise ReporteInvalido(422, f"{campo}: máximo {maximo} caracteres.")
    return texto


def limpiar_contexto(bruto: str | None) -> dict[str, Any]:
    """Whitelist del contexto del navegador. Nunca lanza: lo ilegible es {}."""
    if not bruto:
        return {}
    try:
        datos = json.loads(bruto)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(datos, dict):
        return {}

    limpio: dict[str, Any] = {}
    for clave, maximo in CLAVES_CONTEXTO.items():
        valor = datos.get(clave)
        if isinstance(valor, str) and valor.strip():
            limpio[clave] = valor.strip()[:maximo]

    for clave in CLAVES_MEDIDAS:
        medida = datos.get(clave)
        if not isinstance(medida, dict):
            continue
        ancho, alto = medida.get("ancho"), medida.get("alto")
        # bool es subclase de int: True no es un ancho.
        if all(
            isinstance(v, int) and not isinstance(v, bool) and 0 < v <= _MEDIDA_MAX
            for v in (ancho, alto)
        ):
            limpio[clave] = {"ancho": ancho, "alto": alto}
    return limpio


def validar_adjunto(contenido: bytes, nombre: str | None) -> Adjunto:
    if len(contenido) > settings.REPORTE_ADJUNTO_MAX_BYTES:
        mb = settings.REPORTE_ADJUNTO_MAX_BYTES // (1024 * 1024)
        raise ReporteInvalido(413, f"La imagen pesa más de {mb} MB.")
    if not contenido:
        raise ReporteInvalido(422, "El archivo adjunto está vacío.")
    try:
        info = validar_foto(contenido, nombre, settings.REPORTE_ADJUNTO_MAX_PX)
    except ImagenInvalida as e:
        if e.status_code == 415:
            raise ReporteInvalido(415, "El adjunto tiene que ser una imagen PNG o JPG.")
        raise ReporteInvalido(e.status_code, e.mensaje)
    return Adjunto(contenido=contenido, mime=info.mime, ancho=info.ancho, alto=info.alto)


# ============================================================
# Guardado
# ============================================================
async def crear_reporte(
    *,
    portal_user_id: UUID,
    tenant_id: UUID | None,
    email: str,
    resumen: str,
    descripcion: str,
    contexto: dict[str, Any],
    adjunto: Adjunto | None,
) -> UUID:
    """Guarda el reporte. Lanza LimiteDeReportes si el usuario ya llegó al tope."""
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            # Candado por usuario hasta el final de la transacción: sin él,
            # dos envíos a la vez leerían "2 de 3" los dos y pasarían los dos.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"reporte:{portal_user_id}",
            )

            recientes = await conn.fetch(
                """
                SELECT EXTRACT(EPOCH FROM (creado_en + INTERVAL '1 hour' - NOW())) AS segundos
                  FROM reportes_incidencia
                 WHERE portal_user_id = $1 AND creado_en > NOW() - INTERVAL '1 hour'
                 ORDER BY creado_en ASC
                """,
                portal_user_id,
            )
            tope = settings.REPORTES_MAX_POR_HORA
            if len(recientes) >= tope:
                # Cuándo vence el reporte que hay que esperar para tener hueco.
                libera = recientes[len(recientes) - tope]["segundos"]
                raise LimiteDeReportes(max(1, math.ceil(float(libera))))

            reporte_id = await conn.fetchval(
                """
                INSERT INTO reportes_incidencia
                    (portal_user_id, tenant_id, email, resumen, descripcion, contexto)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                RETURNING id
                """,
                portal_user_id,
                tenant_id,
                email,
                resumen,
                descripcion,
                json.dumps(contexto),
            )

            if adjunto is not None:
                await conn.execute(
                    """
                    INSERT INTO reportes_incidencia_adjuntos
                        (reporte_id, contenido, mime, ancho, alto, bytes)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    reporte_id,
                    adjunto.contenido,
                    adjunto.mime,
                    adjunto.ancho,
                    adjunto.alto,
                    len(adjunto.contenido),
                )
    return reporte_id


# ============================================================
# Aviso al equipo
# ============================================================
def _url_reportes() -> str:
    base = settings.FRONTEND_ORIGINS[0] if settings.FRONTEND_ORIGINS else ""
    return f"{base}/gerencia/incidencias"


def _medida(contexto: dict[str, Any], clave: str) -> str | None:
    m = contexto.get(clave)
    return f"{m['ancho']}x{m['alto']}" if m else None


def armar_correo(reporte: dict[str, Any], negocio: str | None) -> tuple[str, str, str]:
    """(asunto, texto, html). Todo lo que escribió el usuario va escapado en el HTML."""
    contexto = reporte["contexto"] or {}
    filas = [
        ("Negocio", negocio or "(sin negocio)"),
        ("Usuario", reporte["email"]),
        ("Pantalla", contexto.get("ruta")),
        ("Navegador", contexto.get("navegador")),
        ("Sistema operativo", contexto.get("sistema_operativo")),
        ("Resolución", _medida(contexto, "pantalla")),
        ("Ventana", _medida(contexto, "ventana")),
        ("Adjunto", "sí, una imagen" if reporte["tiene_adjunto"] else None),
    ]
    filas = [(k, v) for k, v in filas if v]
    url = _url_reportes()

    texto = (
        f"{reporte['resumen']}\n\n{reporte['descripcion']}\n\n"
        + "\n".join(f"{k}: {v}" for k, v in filas)
        + f"\n\nVer en OperativAI: {url}\n"
    )
    html_filas = "\n".join(
        f'<tr><td style="padding:2px 12px 2px 0;color:#888">{html.escape(k)}</td>'
        f'<td style="padding:2px 0">{html.escape(str(v))}</td></tr>'
        for k, v in filas
    )
    cuerpo = f"""\
<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#1c1c1c">
  <p style="margin:0 0 4px;font-size:12px;color:#888">Nuevo reporte de incidencia</p>
  <p style="margin:0 0 16px;font-size:18px;font-weight:600">{html.escape(reporte["resumen"])}</p>
  <p style="margin:0 0 20px;font-size:14px;color:#333;white-space:pre-wrap">{html.escape(reporte["descripcion"])}</p>
  <table style="font-size:13px;margin:0 0 24px">{html_filas}</table>
  <p style="margin:0"><a href="{html.escape(url)}" style="font-size:13px;color:#3b82f6;text-decoration:none">Ver en OperativAI →</a></p>
</div>"""
    return f"Reporte: {reporte['resumen']}", texto, cuerpo


async def notificar_reporte(reporte_id: UUID) -> int:
    """
    Avisa a gerencia por correo. Devuelve cuántos salieron. Nunca lanza:
    corre como BackgroundTask y el reporte ya está guardado.
    """
    try:
        reporte = await fetch_one(
            """
            SELECT r.email, r.resumen, r.descripcion, r.contexto,
                   t.name AS negocio,
                   EXISTS (SELECT 1 FROM reportes_incidencia_adjuntos a
                            WHERE a.reporte_id = r.id) AS tiene_adjunto
              FROM reportes_incidencia r
              LEFT JOIN tenants t ON t.id = r.tenant_id
             WHERE r.id = $1
            """,
            reporte_id,
        )
        if reporte is None:
            return 0

        datos = dict(reporte)
        # asyncpg devuelve jsonb como texto.
        if isinstance(datos["contexto"], str):
            datos["contexto"] = json.loads(datos["contexto"])
        asunto, texto, cuerpo = armar_correo(datos, datos["negocio"])

        enviados = 0
        for d in await fetch_all("SELECT email FROM gerencia_users ORDER BY email"):
            try:
                await enviar_correo(d["email"], asunto, texto, cuerpo)
                enviados += 1
            except ErrorEnvioCorreo:
                log.error("No se pudo avisar del reporte %s a %s", reporte_id, d["email"])
        return enviados
    except Exception:  # noqa: BLE001 - background: nada puede escapar
        log.exception("Falló el aviso del reporte %s", reporte_id)
        return 0
