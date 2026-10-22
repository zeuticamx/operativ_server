"""
Webhook de NeuroAPI Connect Sessions.

Vive aparte de routers/canales.py por el mismo motivo que el de Stripe vive
aparte de pagos.py: es el único endpoint del módulo de canales sin JWT, se
autentica por el HMAC de la cabecera `X-Hub-Signature-256`.

Al terminar el Embedded Signup, NeuroAPI dispara `whatsapp.connected` a la
`webhook_url` de la sesión. Formato observado en producción (2026-09-29):

  {"event": "whatsapp.connected", "session_id": "...", "service_type": "...",
   "timestamp": "...", "data": {"phones": [{"id": "...", "wabaId": "...",
   "wabaName": "...", "phoneNumberId": "...", "displayPhone": "...", ...}]}}

El parser acepta además variantes en snake_case, con los datos en la raíz o
en `data`, y el sobre estándar de Meta (`entry[].changes[].value`).

Instagram Direct usa esta misma URL y el mismo secreto: `instagram.connected`
trae `data.accounts[]` (con `instagramAccountId`), y la sesión dice de qué
canal es (`neuroapi_connect_sessions.canal`). Los DMs llegan con
`object: "instagram"` y se enrutan por la cuenta receptora en vez de por
phone_number_id.

Una vez vinculada la cuenta, NeuroAPI manda a esta misma URL los mensajes
entrantes. Para esos, el backend actúa de proxy (services/entrada_mensajes.py):
la firma ya se validó aquí, se identifica el tenant por el número receptor y
se reenvía el cuerpo intacto hacia n8n o hacia el propio agente en código,
según `AGENTE_EN_CODIGO` (decide `entrada_mensajes.destino_para`). Así no
hace falta una segunda llamada a NeuroAPI para apuntar los mensajes a otra
URL tras la vinculación, ni para migrar de uno al otro.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from config import settings
from services import (
    ecos_coexistencia,
    entrada_mensajes,
    neuroapi_connect,
    plantillas_tenant,
    plantillas_whatsapp,
    respuesta_cuenta_global,
)
from session import execute, fetch_one

router = APIRouter(prefix="/canales/whatsapp/neuroapi", tags=["canales"])

log = logging.getLogger("operativai.canales.neuroapi_connect")

EVENTOS_CONECTADO = {"whatsapp.connected", "instagram.connected"}

# La tabla solo admite pendiente/completado/fallido (CHECK en
# sql/24_neuroapi_connect_sessions.sql): cualquier otro valor hacía fallar el
# UPDATE con 500. Se traduce el vocabulario de NeuroAPI a esos tres.
ESTADOS: dict[str, str] = {
    "completado": "completado",
    "completed": "completado",
    "complete": "completado",
    "connected": "completado",
    "success": "completado",
    "succeeded": "completado",
    "fallido": "fallido",
    "failed": "fallido",
    "error": "fallido",
    "cancelled": "fallido",
    "canceled": "fallido",
    "expired": "fallido",
    "pendiente": "pendiente",
    "pending": "pendiente",
}

@router.post("/webhook", status_code=200)
async def webhook(request: Request) -> dict[str, bool]:
    """
    Recibe el resultado de una Connect Session (y activa el canal si terminó
    en éxito) o un mensaje entrante (y lo reenvía al flujo del tenant).

    Devuelve 200 salvo firma inválida, configuración faltante o destino caído:
    un evento de un session_id o un número que no es nuestro, o de un estado
    que no nos interesa, no se arregla con un reintento.
    """
    if not settings.NEUROAPI_CONNECT_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El webhook de NeuroAPI Connect no está configurado",
        )

    # El cuerpo crudo, no el JSON ya parseado: el HMAC se calcula sobre los
    # bytes tal cual llegaron.
    cuerpo_crudo = await request.body()

    if not neuroapi_connect.verificar_webhook(cuerpo_crudo, dict(request.headers)):
        log.warning("Evento de NeuroAPI Connect con firma inválida")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firma inválida",
        )

    try:
        evento = json.loads(cuerpo_crudo)
    except json.JSONDecodeError:
        log.warning("Evento de NeuroAPI Connect con cuerpo ilegible")
        return {"recibido": True}

    if not isinstance(evento, dict):
        log.warning("Evento de NeuroAPI Connect que no es un objeto JSON")
        return {"recibido": True}

    # Estado de las plantillas de la cuenta global (spec 004): lo manda Meta
    # por NeuroAPI, ya con la firma verificada. No es un mensaje ni un alta.
    if plantillas_whatsapp.es_evento_de_plantilla(evento):
        # 200 igualmente: un reintento de NeuroAPI no arregla un fallo propio. Cada
        # handler va por separado para que un fallo del uno no deje sin aplicar el otro.
        try:
            # Solo los eventos del WABA de la cuenta global (spec 007, hallazgo 1).
            await plantillas_whatsapp.aplicar_evento_meta(evento)
        except Exception:
            log.exception("No se pudo aplicar el evento de estado de plantilla global")
        try:
            # Plantillas propias de cada negocio (caché de /api/plantillas-whatsapp);
            # REJECTED/PAUSED/DISABLED apagan la función de aviso de cita y alertan.
            await plantillas_tenant.aplicar_evento_meta(evento)
        except Exception:
            log.exception("No se pudo aplicar el evento de estado de plantilla de un negocio")
        return {"recibido": True}

    fuentes = _fuentes(evento)
    tipo_evento = str(_campo(fuentes, "event", "type") or "").strip().lower()

    # Enrutado por `object`: un DM de Instagram Direct no trae phone_number_id
    # y su tenant sale de la cuenta receptora. `whatsapp_business_account` (y
    # lo que no traiga `object`) sigue el camino de siempre.
    if evento.get("object") == "instagram" and not _es_evento_de_conexion(tipo_evento, fuentes):
        await _reenviar_mensaje_instagram(cuerpo_crudo, evento)
        return {"recibido": True}

    if not _es_evento_de_conexion(tipo_evento, fuentes):
        await _reenviar_mensaje(cuerpo_crudo, evento, tipo_evento)
        return {"recibido": True}

    await _procesar_conexion(evento, fuentes, tipo_evento)
    return {"recibido": True}


def _es_evento_de_conexion(tipo_evento: str, fuentes: list[dict[str, Any]]) -> bool:
    """
    Todo lo que no es claramente del alta se trata como mensajería: es
    preferible reenviar a n8n un evento que ignore a perder un mensaje
    porque NeuroAPI le puso un nombre que no esperábamos.
    """
    return (
        tipo_evento in EVENTOS_CONECTADO
        or tipo_evento.startswith("connect_session")
        or _campo(fuentes, "session_id") is not None
    )


async def _reenviar_mensaje(cuerpo_crudo: bytes, evento: dict[str, Any], tipo_evento: str) -> None:
    phone_number_id = entrada_mensajes.extraer_phone_number_id(evento)
    if not phone_number_id:
        log.warning(
            "Mensaje de NeuroAPI sin phone_number_id receptor (evento=%r); %s",
            tipo_evento, _estructura(evento),
        )
        return

    # Ecos de coexistencia: lo que una persona escribió desde la app móvil de
    # WhatsApp Business. Se sincronizan (solo lectura) y NO van al agente; si
    # el mismo evento trae además mensajes de clientes, esos siguen su camino.
    if ecos_coexistencia.hay_ecos(evento):
        await ecos_coexistencia.procesar_ecos(evento, phone_number_id)
        if not ecos_coexistencia.hay_mensajes_de_clientes(evento):
            return

    # Respuesta de un cliente a un aviso de cita en la cuenta global (spec 004,
    # D13): se contesta con el texto fijo y no llega al agente del tenant.
    # Bandera apagada o cualquier otro mensaje: sigue el flujo de siempre.
    if await respuesta_cuenta_global.desviar_si_es_respuesta(evento, phone_number_id):
        return

    tenant_id = await entrada_mensajes.tenant_por_numero(phone_number_id)
    if tenant_id is None:
        # Lo normal es una línea desconectada desde el portal: NeuroAPI no
        # tiene endpoint de baja y sigue mandando hasta que se retire el
        # acceso en Meta.
        log.warning(
            "Mensaje de NeuroAPI descartado: el número %s no tiene tenant activo "
            "(desconectado o ajeno)",
            phone_number_id,
        )
        return

    await _entregar(cuerpo_crudo, tenant_id, "whatsapp", phone_number_id)


async def _reenviar_mensaje_instagram(cuerpo_crudo: bytes, evento: dict[str, Any]) -> None:
    """
    Un DM de Instagram Direct: el tenant sale de la cuenta que lo recibió
    (`entry[].id`, o `recipient.id`), que es el `ig_user_id` guardado al
    vincular. Los ecos (lo que manda el propio negocio) también se reenvían:
    los descarta `normalizar` (`is_echo`), igual que con Meta directo.
    """
    ig_account_id = entrada_mensajes.extraer_ig_account_id(evento)
    if not ig_account_id:
        log.warning("Mensaje de Instagram de NeuroAPI sin cuenta receptora; %s", _estructura(evento))
        return

    tenant_id = await entrada_mensajes.tenant_por_cuenta_instagram(ig_account_id)
    if tenant_id is None:
        # Igual que en WhatsApp: una cuenta desconectada desde el portal sigue
        # mandando hasta que el negocio retire el acceso en Meta.
        log.warning(
            "Mensaje de Instagram de NeuroAPI descartado: la cuenta …%s no tiene "
            "tenant activo (desconectada o ajena)",
            str(ig_account_id)[-4:],
        )
        return

    await _entregar(cuerpo_crudo, tenant_id, "instagram", ig_account_id)


async def _entregar(cuerpo_crudo: bytes, tenant_id: Any, canal: str, identificador: str) -> None:
    """Reenvía al destino del tenant (n8n o el agente en código)."""
    destino = entrada_mensajes.destino_para(tenant_id)
    if not destino:
        # 503 y no 200: NeuroAPI reintenta y el mensaje no se pierde mientras
        # se corrige la variable.
        log.error(
            "N8N_WEBHOOK_ENTRADA_URL sin configurar: mensaje del tenant %s sin reenviar",
            tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El reenvío de mensajes no está configurado",
        )

    try:
        await entrada_mensajes.reenviar(cuerpo_crudo, tenant_id, destino, canal=canal)
    except entrada_mensajes.ReenvioError as e:
        log.error(
            "No se pudo reenviar el mensaje de %s del tenant %s (%s): %s",
            canal, tenant_id, identificador, e,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo entregar el mensaje",
        )


async def _procesar_conexion(
    evento: dict[str, Any], fuentes: list[dict[str, Any]], tipo_evento: str
) -> None:
    def campo(*nombres: str) -> Any:
        return _campo(fuentes, *nombres)

    session_id = campo("session_id") or evento.get("id")
    phone_number_id = campo("phoneNumberId", "phone_number_id", "otp_phone_number_id")
    phone_number = campo("displayPhone", "phone_number", "display_phone_number")
    waba_id = campo("wabaId", "waba_id")
    telefonos = _telefonos(evento)
    if len(telefonos) > 1:
        log.warning(
            "NeuroAPI Connect mandó %d números para la sesión %s; se usa el primero (%s)",
            len(telefonos), session_id, phone_number_id,
        )

    if tipo_evento in EVENTOS_CONECTADO:
        estado_crudo = tipo_evento
        estado: str | None = "completado"
    else:
        estado_crudo = str(campo("status", "state") or "").strip()
        if not estado_crudo:
            # Formato "connect_session.completed": el estado va tras el punto.
            estado_crudo = tipo_evento.rsplit(".", 1)[-1]
        estado = ESTADOS.get(estado_crudo.lower())

    log.warning(
        "NeuroAPI Connect: evento=%r estado=%r session_id=%s phone_number_id=%s "
        "phone_number=%s waba_id=%s",
        tipo_evento, estado_crudo, session_id, phone_number_id, phone_number, waba_id,
    )

    if not session_id:
        log.warning("Evento de NeuroAPI Connect sin session_id; %s", _estructura(evento))
        return

    if estado is None:
        log.warning(
            "NeuroAPI Connect mandó un estado desconocido %r para la sesión %s; %s",
            estado_crudo, session_id, _estructura(evento),
        )
        estado = "pendiente"

    fila = await fetch_one(
        """
        UPDATE neuroapi_connect_sessions
           SET status = $2, detalle = $3, updated_at = NOW()
         WHERE session_id = $1
        RETURNING tenant_id, connection_mode, canal
        """,
        str(session_id),
        estado,
        campo("detail", "error")
        or (estado_crudo if estado_crudo.lower() not in ESTADOS else None),
    )
    if fila is None:
        log.warning("Evento de NeuroAPI Connect para una sesión que no es nuestra: %s", session_id)
        return

    # El canal lo dice la sesión que abrió el negocio, no el evento: un
    # `instagram.connected` sobre una sesión de WhatsApp no activa nada.
    if fila["canal"] == "instagram":
        if estado == "completado":
            await _completar_instagram(fila["tenant_id"], str(session_id), evento)
        return

    if estado == "completado":
        if phone_number_id:
            resultado = await _activar_whatsapp(
                fila["tenant_id"],
                str(phone_number_id),
                telefono_visible=phone_number,
                connection_mode=fila["connection_mode"],
            )
            await execute(
                "UPDATE neuroapi_connect_sessions SET detalle = $2, updated_at = NOW() "
                "WHERE session_id = $1",
                str(session_id),
                json.dumps(
                    {
                        "resultado": resultado.resultado,
                        "codigo": resultado.codigo,
                        "phone_number_id": resultado.phone_number_id,
                        "connection_mode": fila["connection_mode"],
                    }
                ),
            )
            log.warning(
                "WhatsApp vía NeuroAPI Connect: tenant=%s sesión=%s phone_number_id=%s resultado=%s",
                fila["tenant_id"], session_id, phone_number_id, resultado.resultado,
            )
        else:
            log.warning(
                "NeuroAPI Connect marcó la sesión %s como completada sin phone_number_id; %s",
                session_id, _estructura(evento),
            )


async def _activar_whatsapp(
    tenant_id: Any,
    phone_number_id: str,
    telefono_visible: str | None = None,
    connection_mode: str = "standard",
):
    """
    Alta por el webhook (T6): un número libre se vincula; uno propio se ignora
    (sin duplicar ni reactivar); uno ajeno se rechaza sin revelar y cuenta
    para los intentos. Devuelve el resultado para dejarlo en la sesión
    (`GET .../neuroapi/estado`).

    `connection_mode` es el que eligió el negocio al abrir la sesión: se
    marca en el número recién vinculado y también en uno propio que se volvió
    a conectar (p. ej. de solo API a coexistencia). En uno ajeno o rechazado
    no se toca nada.

    `channel_credentials` solo guarda el PRIMER número (P2): es lo que lee
    `cuenta_global_wa` para los avisos. El alta de un segundo número no la
    sobrescribe.
    """
    from services import numeros_whatsapp as numeros
    from session import conexion

    async with conexion() as conn:
        resultado = await numeros.vincular_desde_proveedor(
            conn, tenant_id, phone_number_id, telefono_visible=telefono_visible
        )
        if resultado.resultado in ("vinculado", "propio"):
            await numeros.marcar_modo_conexion(conn, tenant_id, phone_number_id, connection_mode)

    if resultado.resultado != "vinculado":
        return resultado

    if not settings.NEUROAPI_API_KEY:
        log.error(
            "NEUROAPI_API_KEY sin configurar: WhatsApp del tenant %s queda vinculado "
            "pero n8n no podrá contestar por NeuroAPI",
            tenant_id,
        )
    actual = await fetch_one(
        "SELECT phone_number_id FROM channel_credentials "
        "WHERE tenant_id = $1 AND channel_type = 'whatsapp'",
        tenant_id,
    )
    if actual is None or not actual["phone_number_id"]:
        await fetch_one(
            "SELECT set_whatsapp_neuroapi($1, $2, $3)",
            tenant_id,
            phone_number_id,
            settings.NEUROAPI_API_KEY or None,
        )
    await execute(
        """
        INSERT INTO tenant_channels (tenant_id, channel_type)
        VALUES ($1, 'whatsapp')
        ON CONFLICT (tenant_id, channel_type) DO UPDATE SET is_active = true
        """,
        tenant_id,
    )
    return resultado


async def _completar_instagram(tenant_id: Any, session_id: str, evento: dict[str, Any]) -> None:
    """
    `instagram.connected`: vincula la primera cuenta de `data.accounts[]`
    (formato observado, 2026-10-22: `instagramAccountId`, `username`) y deja
    el resultado en la sesión, que es lo que lee `GET .../instagram/neuroapi/estado`.
    """
    from services import instagram_neuroapi
    from session import conexion

    cuentas = _cuentas_instagram(evento)
    if len(cuentas) > 1:
        log.warning(
            "NeuroAPI Connect mandó %d cuentas de Instagram para la sesión %s; se usa la primera",
            len(cuentas), session_id,
        )
    cuenta = cuentas[0] if cuentas else {}
    ig_user_id = str(
        cuenta.get("instagramAccountId") or cuenta.get("instagram_account_id") or ""
    ).strip()
    username = cuenta.get("username") or None

    async with conexion() as conn:
        resultado = await instagram_neuroapi.vincular_desde_proveedor(
            conn, tenant_id, ig_user_id, username=username
        )

    # Un alta rechazada deja la sesión en 'fallido': el trámite terminó y
    # el portal tiene que dejar de esperar. La cuenta propia ya vinculada no
    # es un fallo: sigue conectada como estaba.
    await execute(
        "UPDATE neuroapi_connect_sessions SET status = $2, detalle = $3, updated_at = NOW() "
        "WHERE session_id = $1",
        session_id,
        "completado" if resultado.resultado in ("vinculado", "propio") else "fallido",
        json.dumps(
            {
                "resultado": resultado.resultado,
                "codigo": resultado.codigo,
                "username": resultado.username,
            }
        ),
    )
    log.warning(
        "Instagram vía NeuroAPI Connect: tenant=%s sesión=%s resultado=%s codigo=%s",
        tenant_id, session_id, resultado.resultado, resultado.codigo,
    )


def _cuentas_instagram(evento: dict[str, Any]) -> list[dict[str, Any]]:
    datos = evento.get("data")
    cuentas = datos.get("accounts") if isinstance(datos, dict) else None
    return [c for c in cuentas or [] if isinstance(c, dict)]


def _campo(fuentes: list[dict[str, Any]], *nombres: str) -> Any:
    """Primer valor no vacío, buscando cada nombre en todas las fuentes en orden."""
    for nombre in nombres:
        for fuente in fuentes:
            valor = fuente.get(nombre)
            if valor not in (None, ""):
                return valor
    return None


def _telefonos(evento: dict[str, Any]) -> list[dict[str, Any]]:
    datos = evento.get("data")
    telefonos = datos.get("phones") if isinstance(datos, dict) else None
    return [t for t in telefonos or [] if isinstance(t, dict)]


def _fuentes(evento: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Dónde buscar cada campo, en orden de prioridad: la raíz, `data`, los
    `data.phones[]` y cada `entry[].changes[].value` del sobre de Meta (con
    su `data` si lo trae).
    """
    fuentes: list[dict[str, Any]] = [evento]
    if isinstance(evento.get("data"), dict):
        fuentes.append(evento["data"])
    fuentes.extend(_telefonos(evento))
    for entrada in evento.get("entry") or []:
        if not isinstance(entrada, dict):
            continue
        for cambio in entrada.get("changes") or []:
            valor = cambio.get("value") if isinstance(cambio, dict) else None
            if isinstance(valor, dict):
                fuentes.append(valor)
                if isinstance(valor.get("data"), dict):
                    fuentes.append(valor["data"])
    return fuentes


def _estructura(evento: dict[str, Any]) -> str:
    """Nombres de campos (sin valores) para diagnosticar el formato."""
    partes = []
    for clave in sorted(evento):
        valor = evento[clave]
        if isinstance(valor, dict):
            partes.append(f"{clave}{{{', '.join(sorted(valor))}}}")
        else:
            partes.append(clave)
    return "campos=" + ", ".join(partes)
