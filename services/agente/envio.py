"""
Entrega la respuesta por el canal del cliente.

Port de la última parte de `entrada-canal-universal`: cargar credenciales,
comprobar la ventana de 24 h, partir el texto, enviarlo y registrar el fallo
si lo hay.

En n8n esto estaban tres ramas casi idénticas —"Prepara envio", "Prepara
envio (bloqueo)" y "Prepara envio acuse"— que existían solo porque un
workflow no puede llamar a una función: cada rama necesitaba su propia copia
de la misma lógica, con su propio nodo de credenciales. Acá es una sola
función y las tres la llaman.

Dos cosas que vienen de la auditoría:

  - El `access_token` no entra nunca en un dict de datos. Se lee de
    `get_channel_credentials` y se usa al construir la cabecera, dentro de la
    función que hace la petición (hallazgo 2). En n8n viajaba en el item y
    quedaba guardado en el historial de ejecuciones.
  - La plantilla de reapertura comprueba la lista de bajas antes de salir
    (hallazgo 1, ver `bajas.py`). Es el único envío del flujo que no
    responde a un mensaje del cliente, así que es el único que la necesita:
    todo lo demás va dentro de la ventana de 24 h, o sea porque el cliente
    escribió primero.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from config import settings
from services import mensajeria
from services.agente import bajas
from services.agente.modelos import Destino
from session import execute, fetch_one, fetch_value

__all__ = ["Resultado", "SinCredenciales", "entregar", "partir"]

log = logging.getLogger("operativai.agente.envio")

TIMEOUT = httpx.Timeout(20.0)

# Tope de caracteres por mensaje. WhatsApp corta en 4096 e Instagram y
# Facebook en 1000; se deja margen porque el recorte se hace por palabras.
LIMITES = {"whatsapp": 4000, "instagram": 950, "facebook": 950}
LIMITE_POR_DEFECTO = 950

# Qué significa cada código de error de Meta, para que el registro sirva
# para algo cuando el negocio pregunte por qué no llegó su mensaje.
DIAGNOSTICOS = {
    190: "Token inválido o expirado — el tenant debe reconectar el canal",
    10: "Faltan permisos en la app de Meta",
    200: "Faltan permisos en la app de Meta",
    613: "Rate limit alcanzado",
    131047: "Ventana de 24 h cerrada — se requiere plantilla aprobada (WhatsApp)",
    100: "Parámetro inválido — revisa el ID de destino o el payload",
}

_CODIGO = re.compile(r'\\?"code\\?"\s*:\s*(\d+)')
_MENSAJE = re.compile(r'\\?"message\\?"\s*:\s*\\?"([^"\\]+)')


@dataclass(frozen=True)
class Resultado:
    entregado: bool
    partes: int = 0
    motivo: str | None = None
    # La ventana estaba cerrada y se mandó la plantilla de reapertura en su
    # lugar: el texto del agente NO llegó.
    con_plantilla: bool = False


class SinCredenciales(Exception):
    """El tenant no tiene el canal conectado."""


# ============================================================
# Partir el texto
# ============================================================
def partir(texto: str, limite: int) -> list[str]:
    """
    Corta en trozos que quepan, prefiriendo final de párrafo, luego de línea
    y luego de palabra. Si ninguno cae en la segunda mitad del trozo se parte
    en seco: más vale cortar una palabra que mandar un muro que el canal
    rechaza.
    """
    if len(texto) <= limite:
        return [texto]

    partes: list[str] = []
    resto = texto

    while len(resto) > limite:
        corte = resto.rfind("\n\n", 0, limite)
        if corte < limite // 2:
            corte = resto.rfind("\n", 0, limite)
        if corte < limite // 2:
            corte = resto.rfind(" ", 0, limite)
        if corte < limite // 2:
            corte = limite

        partes.append(resto[:corte].strip())
        resto = resto[corte:].strip()

    if resto:
        partes.append(resto)
    return partes


# ============================================================
# Credenciales y destino
# ============================================================
async def _credenciales(tenant_id: UUID, canal: str) -> dict[str, Any]:
    fila = await fetch_one(
        "SELECT * FROM get_channel_credentials($1, $2)", tenant_id, canal
    )
    if fila is None or not fila["access_token"]:
        raise SinCredenciales(
            f"El tenant {tenant_id} no tiene credenciales activas para {canal}"
        )
    return dict(fila)


def _destino_whatsapp(
    cred: dict[str, Any], pnid: str, cuerpo: dict[str, Any]
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """
    WhatsApp sale por Graph directo o por NeuroAPI, según `bsp_provider`.

    El token se usa acá, al armar la cabecera, y no sale de esta función.
    """
    if cred.get("bsp_provider") == "neuroapi":
        base = settings.NEUROAPI_API_BASE_URL.rstrip("/")
        sin_sobre = {
            k: v for k, v in cuerpo.items()
            if k not in ("messaging_product", "recipient_type")
        }
        return (
            f"{base}/neuroapi/messaging/send",
            {"x-api-key": cred["access_token"]},
            {**sin_sobre, "from_phone_number_id": pnid},
        )

    return (
        f"{settings.graph_url}/{pnid}/messages",
        {"Authorization": f"Bearer {cred['access_token']}"},
        cuerpo,
    )


def _peticion_texto(
    destino: Destino, cred: dict[str, Any], texto: str
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """URL, cabeceras y cuerpo para un mensaje de texto, según el canal."""
    canal = destino.canal

    if canal == "whatsapp":
        pnid = destino.phone_number_id or cred.get("phone_number_id")
        if not pnid:
            raise SinCredenciales(
                f"Tenant {destino.tenant_id}: falta phone_number_id para WhatsApp"
            )
        return _destino_whatsapp(cred, pnid, {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": destino.usuario_id,
            "type": "text",
            "text": {"preview_url": False, "body": texto},
        })

    if mensajeria.es_instagram_neuroapi(canal, cred):
        # Sin página: NeuroAPI envía por el IGSID del cliente. La respuesta es
        # del agente, así que sin HUMAN_AGENT (vale solo para personas).
        try:
            p = mensajeria.peticion_instagram(cred, destino.usuario_id, texto)
        except mensajeria.SinCredenciales as e:
            raise SinCredenciales(f"Tenant {destino.tenant_id}: {e}") from e
        return p.url, p.cabeceras, p.cuerpo

    cabeceras = {"Authorization": f"Bearer {cred['access_token']}"}

    if canal == "facebook":
        pid = destino.page_id or cred.get("page_id")
        if not pid:
            raise SinCredenciales(
                f"Tenant {destino.tenant_id}: falta page_id para Facebook"
            )
        return (
            f"{settings.graph_url}/{pid}/messages",
            cabeceras,
            {
                "messaging_type": "RESPONSE",
                "recipient": {"id": destino.usuario_id},
                "message": {"text": texto},
            },
        )

    if canal == "instagram":
        # Solo `cred.page_id`, nunca `destino.page_id`: en Instagram el
        # `entry.id` del webhook es el id de la CUENTA, y el envío va contra
        # la página vinculada. Confundirlos hacía fallar la respuesta.
        pid = cred.get("page_id")
        if not pid:
            raise SinCredenciales(
                f"Tenant {destino.tenant_id}: falta page_id en tenant_channels "
                "para enviar por Instagram"
            )
        return (
            f"{settings.graph_url}/{pid}/messages",
            cabeceras,
            {"recipient": {"id": destino.usuario_id}, "message": {"text": texto}},
        )

    raise SinCredenciales(f"Canal no soportado en envío: {canal}")


# ============================================================
# Entrega
# ============================================================
async def entregar(destino: Destino, texto: str) -> Resultado:
    """
    Manda `texto` al cliente por su canal.

    El widget no pasa por aquí: su respuesta viaja en el cuerpo de la misma
    petición HTTP (ver routers/agente_webhook).

    Nunca levanta. Un fallo de entrega queda registrado en `delivery_errors`
    y se devuelve en el resultado: el mensaje ya está guardado en el
    historial y la conversación sigue viva.
    """
    if destino.es_web:
        raise ValueError("El canal web no se entrega por aquí: va en la respuesta HTTP")

    try:
        cred = await _credenciales(destino.tenant_id, destino.canal)
    except SinCredenciales as e:
        log.error("%s", e)
        await _registrar_error(destino, None, str(e))
        return Resultado(entregado=False, motivo=str(e))

    # Fuera de la ventana de 24 h, WhatsApp no deja mandar texto libre: hay
    # que reabrir con una plantilla aprobada. El resto de canales no tienen
    # esta restricción.
    if destino.canal == "whatsapp" and not await _ventana_abierta(destino):
        return await _reabrir_con_plantilla(destino, cred)

    return await _enviar_texto(destino, cred, texto)


async def _ventana_abierta(destino: Destino) -> bool:
    """
    Sin conversación no hay nada que comprobar: los avisos de texto fijo
    (plan agotado) salen como respuesta inmediata a un mensaje del cliente,
    así que la ventana está abierta por definición.
    """
    if destino.conversation_id is None:
        return True
    return bool(
        await fetch_value("SELECT dentro_de_ventana_24h($1)", destino.conversation_id)
    )


async def _enviar_texto(
    destino: Destino, cred: dict[str, Any], texto: str
) -> Resultado:
    limite = LIMITES.get(destino.canal, LIMITE_POR_DEFECTO)
    fragmentos = partir(texto, limite)

    for numero, fragmento in enumerate(fragmentos, start=1):
        try:
            url, cabeceras, cuerpo = _peticion_texto(destino, cred, fragmento)
        except SinCredenciales as e:
            log.error("%s", e)
            await _registrar_error(destino, None, str(e))
            return Resultado(entregado=False, partes=numero - 1, motivo=str(e))

        async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
            try:
                respuesta = await cliente.post(url, json=cuerpo, headers=cabeceras)
            except httpx.HTTPError as e:
                motivo = f"No se pudo contactar al canal: {e}"
                log.error(
                    "Fallo de red enviando a %s (tenant %s)",
                    destino.canal, destino.tenant_id,
                )
                await _registrar_error(destino, None, motivo)
                return Resultado(entregado=False, partes=numero - 1, motivo=motivo)

        if respuesta.status_code >= 400 or _rechazo_en_cuerpo(respuesta):
            codigo, diagnostico = _diagnosticar(respuesta.text)
            log.warning(
                "Envío fallido tenant=%s canal=%s parte=%s/%s code=%s :: %s",
                destino.tenant_id, destino.canal, numero, len(fragmentos),
                codigo, diagnostico,
            )
            await _registrar_error(destino, codigo, diagnostico)

            # 131047 es la ventana cerrada. Puede pasar aunque la hayamos
            # comprobado: entre la consulta y el envío pudieron cruzarse las
            # 24 h, o el canal lleva su propio reloj.
            if codigo == 131047 and destino.canal == "whatsapp":
                return await _reabrir_con_plantilla(destino, cred)

            return Resultado(entregado=False, partes=numero - 1, motivo=diagnostico)

    log.info(
        "Entregado a %s (tenant %s) en %s parte(s)",
        destino.canal, destino.tenant_id, len(fragmentos),
    )
    return Resultado(entregado=True, partes=len(fragmentos))


def _rechazo_en_cuerpo(respuesta: httpx.Response) -> bool:
    """
    NeuroAPI puede contestar 200 con `success: false` o un `error` en el
    cuerpo (igual que mensajeria.enviar). Sin esto el rechazo pasaba por
    entregado y no quedaba rastro en delivery_errors.
    """
    try:
        datos = respuesta.json()
    except (ValueError, AttributeError):
        return False
    return isinstance(datos, dict) and (datos.get("success") is False or "error" in datos)


def _diagnosticar(cuerpo: str) -> tuple[int | None, str]:
    """
    Saca el código de error de Meta del cuerpo de la respuesta.

    Se busca con expresión regular y no parseando JSON porque el error llega
    anidado y a veces con las comillas escapadas, según por dónde venga
    (Graph directo o el BSP envolviéndolo).
    """
    codigo = None
    match = _CODIGO.search(cuerpo or "")
    if match:
        codigo = int(match.group(1))

    if codigo in DIAGNOSTICOS:
        return codigo, DIAGNOSTICOS[codigo]

    mensaje = _MENSAJE.search(cuerpo or "")
    if mensaje:
        return codigo, mensaje.group(1)[:500]
    return codigo, f"Error desconocido: {(cuerpo or '')[:300]}"


async def _registrar_error(
    destino: Destino, codigo: int | None, diagnostico: str
) -> None:
    """
    Deja el fallo en `delivery_errors` para que gerencia lo vea.

    No se guarda el texto del mensaje: solo el canal, el destinatario y el
    motivo (hallazgo 3).
    """
    try:
        await execute(
            """
            INSERT INTO delivery_errors
                (tenant_id, channel_type, user_id, error_code, diagnostico)
            VALUES ($1, $2, $3, $4, $5)
            """,
            destino.tenant_id,
            destino.canal,
            destino.usuario_id,
            codigo,
            diagnostico[:500],
        )
    except Exception:
        log.exception("No se pudo registrar el error de envío")


# ============================================================
# Ventana cerrada: plantilla de reapertura
# ============================================================
async def _reabrir_con_plantilla(
    destino: Destino, cred: dict[str, Any]
) -> Resultado:
    """
    Manda la plantilla aprobada del tenant para reabrir la conversación.

    El texto del agente NO se entrega: fuera de la ventana Meta solo admite
    plantillas. La conversación queda marcada como `esperando_reapertura`, y
    cuando el cliente conteste, `ingreso` la apaga y el agente retoma.

    Es el único punto del flujo que escribe al cliente sin que él haya
    escrito primero, así que es donde se comprueba la lista de bajas
    (hallazgo 1). La comprobación va antes de cargar la plantilla: si el
    cliente pidió la baja no hay nada que preparar.
    """
    if await bajas.esta_de_baja(destino.tenant_id, destino.canal, destino.usuario_id):
        motivo = (
            "El cliente pidió no recibir más mensajes: no se manda la plantilla "
            "de reapertura"
        )
        # Info y no warning: no es un fallo, es el sistema haciendo lo
        # correcto. Queda en delivery_errors igual, porque desde fuera el
        # efecto es el mismo —la respuesta del agente no llegó— y el negocio
        # tiene que poder ver por qué.
        log.info("%s (tenant %s)", motivo, destino.tenant_id)
        await _registrar_error(destino, None, motivo)
        return Resultado(entregado=False, motivo=motivo)

    plantilla = await fetch_one(
        """
        SELECT template_name, language_code
          FROM tenant_templates
         WHERE tenant_id = $1 AND is_active = true
         ORDER BY created_at ASC
         LIMIT 1
        """,
        destino.tenant_id,
    )
    if plantilla is None or not plantilla["template_name"]:
        motivo = (
            f"Tenant {destino.tenant_id} sin plantilla activa: no se puede reabrir "
            "la conversación fuera de la ventana de 24 h"
        )
        log.error("%s", motivo)
        await _registrar_error(destino, 131047, motivo)
        return Resultado(entregado=False, motivo=motivo)

    pnid = destino.phone_number_id or cred.get("phone_number_id")
    if not pnid:
        motivo = f"Tenant {destino.tenant_id}: falta phone_number_id para la plantilla"
        log.error("%s", motivo)
        await _registrar_error(destino, None, motivo)
        return Resultado(entregado=False, motivo=motivo)

    cuerpo_plantilla = {
        "name": plantilla["template_name"],
        "language": {"code": plantilla["language_code"] or "es_MX"},
        "components": [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": destino.nombre or "cliente"}
                ],
            }
        ],
    }
    url, cabeceras, cuerpo = _destino_whatsapp(cred, pnid, {
        "messaging_product": "whatsapp",
        "to": destino.usuario_id,
        "type": "template",
        "template": cuerpo_plantilla,
    })

    async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
        try:
            respuesta = await cliente.post(url, json=cuerpo, headers=cabeceras)
        except httpx.HTTPError as e:
            motivo = f"No se pudo contactar al canal para la plantilla: {e}"
            log.error("%s", motivo)
            await _registrar_error(destino, None, motivo)
            return Resultado(entregado=False, motivo=motivo)

    if respuesta.status_code >= 400:
        codigo, diagnostico = _diagnosticar(respuesta.text)
        log.warning(
            "Plantilla rechazada tenant=%s code=%s :: %s",
            destino.tenant_id, codigo, diagnostico,
        )
        await _registrar_error(destino, codigo, diagnostico)
        return Resultado(entregado=False, motivo=diagnostico)

    await execute(
        "UPDATE conversations SET esperando_reapertura = true WHERE id = $1",
        destino.conversation_id,
    )
    log.info(
        "Ventana cerrada en la conversación %s: se mandó la plantilla de reapertura",
        destino.conversation_id,
    )
    return Resultado(entregado=True, partes=1, con_plantilla=True)
