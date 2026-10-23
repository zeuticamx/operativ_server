"""
Instagram Direct vía NeuroAPI Connect Sessions: alta de la cuenta y media entrante.

Mismo trámite que WhatsApp (services/neuroapi_connect.py abre la sesión y el
webhook de routers/canales_neuroapi_webhook.py la cierra), con dos
diferencias:

  - Instagram no tiene cupo por plan (decisión de producto: sin tope de
    momento) y un negocio tiene una sola cuenta, en `channel_credentials`
    (canal 'instagram'). Vincular otra cuenta reemplaza la anterior; vincular
    por NeuroAPI una cuenta que antes se conectó por OAuth de Meta la migra.
  - Las mismas reglas de alta que un número (numeros_reglas.evaluar_alta, sin
    cupo): una cuenta activa de OTRO negocio se rechaza sin revelarlo
    (`cuenta_instagram_no_encontrada`, cuenta para el bloqueo de cinco en 24 h,
    que comparte con WhatsApp) y la propia ya vinculada por NeuroAPI no se
    toca (`cuenta_instagram_ya_vinculada`).

La API key que se guarda cifrada como `access_token` es NEUROAPI_INSTA_KEY,
igual que WhatsApp guarda NEUROAPI_API_KEY (sql/30): es lo que lee el envío
(services/mensajeria.py) para la cabecera x-api-key.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID

import asyncpg

from config import settings
from services import adjuntos
from services.numeros_bitacora import SISTEMA, ultimos_digitos
from services.numeros_reglas import VENTANA_INTENTOS, evaluar_alta
from services.numeros_whatsapp import lanzar_rechazo_alta, registrar_intento_ajeno
from session import fetch_one

log = logging.getLogger("operativai.canales.instagram_neuroapi")

SERVICE_TYPE = "instagram_messaging"

_IDS_NO_VALIDOS = {"undefined", "null", "none", "nan"}

# Códigos de numeros_reglas (pensados para números) -> los de una cuenta de Instagram.
_CODIGOS = {
    "numero_ya_vinculado": "cuenta_instagram_ya_vinculada",
    "numero_no_encontrado": "cuenta_instagram_no_encontrada",
}


@dataclass(frozen=True)
class ResultadoAltaInstagram:
    """Lo que pasó con un alta por el webhook. Se deja en `neuroapi_connect_sessions.detalle`."""

    resultado: Literal["vinculado", "propio", "ajeno", "rechazado"]
    codigo: str | None
    ig_user_id: str
    username: str | None = None


def _ahora(ahora: Optional[datetime]) -> datetime:
    return ahora if ahora is not None else datetime.now(timezone.utc)


async def _intentos_recientes(conn: asyncpg.Connection, tenant_id: UUID, ahora: datetime) -> list[datetime]:
    filas = await conn.fetch(
        "SELECT creado_en FROM intentos_alta_ajena WHERE tenant_id = $1 AND creado_en > $2",
        tenant_id,
        ahora - VENTANA_INTENTOS,
    )
    return [f["creado_en"] for f in filas]


async def comprobar_alta_previa(
    conn: asyncpg.Connection, tenant_id: UUID, *, ahora: Optional[datetime] = None
) -> None:
    """429 `demasiados_intentos` ANTES de abrir la sesión, como el de WhatsApp (sin cupo)."""
    ahora = _ahora(ahora)
    decision = evaluar_alta(
        intentos_recientes=await _intentos_recientes(conn, tenant_id, ahora),
        ahora=ahora,
        plazas_usadas=0,
        tope=None,
        relacion_numero="libre",
    )
    if not decision.permitido:
        lanzar_rechazo_alta(decision, ahora)


async def vincular_desde_proveedor(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    ig_user_id: str,
    *,
    username: Optional[str] = None,
    ahora: Optional[datetime] = None,
) -> ResultadoAltaInstagram:
    """
    Alta por el webhook (`instagram.connected`). Nunca lanza: el resultado se
    deja en la sesión para que el portal lo consulte (`.../neuroapi/estado`).

    Recibe una conexión SIN transacción abierta; la abre aquí, con el candado
    del negocio (el mismo que toman las altas de números) y otro de la cuenta,
    para que dos negocios que vinculan la misma cuenta a la vez no se la queden
    los dos.
    """
    identificador = (ig_user_id or "").strip()
    ahora = _ahora(ahora)
    # NeuroAPI puede mandar el texto "undefined"/"null" cuando no pudo leer el perfil:
    # vincularlo dejaría una cuenta que jamás recibe mensajes.
    if not identificador or identificador.lower() in _IDS_NO_VALIDOS or not identificador.isdigit():
        return ResultadoAltaInstagram("rechazado", "cuenta_instagram_no_valida", ig_user_id or "", username)

    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1::text))", str(tenant_id))
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1::text))", f"instagram:{identificador}"
        )
        intentos = await _intentos_recientes(conn, tenant_id, ahora)
        duenos = await conn.fetch(
            """
            SELECT tenant_id, bsp_provider FROM channel_credentials
             WHERE channel_type = 'instagram' AND ig_user_id = $1 AND is_active = true
            """,
            identificador,
        )
        if any(d["tenant_id"] != tenant_id for d in duenos):
            relacion = "ajeno"
        elif any(d["bsp_provider"] == "neuroapi" for d in duenos):
            relacion = "propio"
        else:
            # Libre, o propia pero conectada por OAuth de Meta: se migra a NeuroAPI.
            relacion = "libre"

        decision = evaluar_alta(
            intentos_recientes=intentos,
            ahora=ahora,
            plazas_usadas=0,
            tope=None,
            relacion_numero=relacion,
        )
        if not decision.permitido:
            if decision.cuenta_como_intento:
                await registrar_intento_ajeno(
                    conn, tenant_id, identificador, SISTEMA, ahora, len(intentos), canal="instagram"
                )
            codigo = _CODIGOS.get(str(decision.codigo), decision.codigo)
            if decision.estado_http == 409:
                return ResultadoAltaInstagram("propio", codigo, identificador, username)
            if decision.estado_http == 404:
                return ResultadoAltaInstagram("ajeno", codigo, identificador, username)
            return ResultadoAltaInstagram("rechazado", codigo, identificador, username)

        if not settings.NEUROAPI_INSTA_KEY:
            log.error(
                "NEUROAPI_INSTA_KEY sin configurar: Instagram del tenant %s queda vinculado "
                "pero no se le podrá contestar",
                tenant_id,
            )
        await conn.execute(
            "SELECT set_instagram_neuroapi($1, $2, $3)",
            tenant_id,
            identificador,
            settings.NEUROAPI_INSTA_KEY or None,
        )
        await conn.execute(
            """
            INSERT INTO tenant_channels (tenant_id, channel_type)
            VALUES ($1, 'instagram')
            ON CONFLICT (tenant_id, channel_type) DO UPDATE SET is_active = true
            """,
            tenant_id,
        )

    log.warning(
        "Instagram vinculado por NeuroAPI: tenant=%s cuenta=…%s",
        tenant_id, ultimos_digitos(identificador),
    )
    return ResultadoAltaInstagram("vinculado", None, identificador, username)


# ============================================================
# Media entrante
# ============================================================
async def guardar_adjunto_entrante(tenant_id: UUID, message_id: UUID, media_id: str) -> bool:
    """
    Baja por el proxy de NeuroAPI el archivo de un DM y lo asocia al mensaje ya
    guardado, como hace `/api/eventos/adjunto-entrante` con WhatsApp.

    Nunca lanza: un adjunto que no se pudo bajar o que no es JPG/PNG/PDF/DOCX
    deja el mensaje solo con su texto ("[image]"), que es lo que ya ve el
    agente. Devuelve si quedó guardado.
    """
    try:
        contenido = await adjuntos.descargar_media_instagram(media_id)
        nombre = adjuntos.nombre_por_contenido(contenido, f"instagram-{media_id[-12:]}")
        if nombre is None:
            log.info("Adjunto de Instagram de un tipo no admitido (tenant %s): no se guarda", tenant_id)
            return False
        info = adjuntos.validar_adjunto(contenido, nombre)
    except adjuntos.AdjuntoInvalido as e:
        log.warning(
            "No se pudo guardar el adjunto de Instagram (tenant %s): %s %s",
            tenant_id, e.status_code, e.mensaje,
        )
        return False

    try:
        await fetch_one(
            """
            INSERT INTO message_attachments
                (tenant_id, message_id, direccion, mime, nombre, bytes, contenido, token_publico)
            VALUES ($1, $2, 'in', $3, $4, $5, $6, $7)
            RETURNING id
            """,
            tenant_id,
            message_id,
            info.mime,
            info.nombre,
            len(contenido),
            contenido,
            secrets.token_urlsafe(32),
        )
    except Exception:
        log.exception("No se pudo guardar el adjunto de Instagram (tenant %s)", tenant_id)
        return False
    return True
