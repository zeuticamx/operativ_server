"""
Webhook de entrada del agente: el reemplazo de `/canal-entrada` de n8n.

Es el segundo endpoint público del backend sin JWT (el otro es el de
NeuroAPI Connect), y por el mismo motivo: lo llaman Meta y los navegadores
de los visitantes, que no tienen sesión de portal. Cada camino trae su
propia prueba de identidad y la valida `services/agente/firma.py`.

Dos formas de responder, según quién llame:

  - Meta (WhatsApp / Instagram / Facebook): se acusa recibo de inmediato
    con 200 y el mensaje se procesa aparte. El proveedor corta a los pocos
    segundos y reintenta, así que la respuesta del agente nunca puede ir en
    el cuerpo de esta petición: sale después por la API del canal.
  - Widget web: la respuesta del agente SÍ va en el cuerpo, porque el
    navegador está esperándola.

Mientras `AGENTE_EN_CODIGO` esté apagada el endpoint responde 503. Es
deliberado: durante la migración n8n sigue atendiendo el mismo tráfico, y
que ambos contesten a la vez le duplicaría los mensajes al cliente.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse

from config import settings
from services.agente import envio, firma, ingreso, loop_agente
from services.agente import normalizar as normalizar_mod
from services.agente.modelos import (
    ContextoMensaje,
    Destino,
    PeticionVerificada,
    ResultadoIngreso,
)
from session import execute

log = logging.getLogger("operativai.agente.webhook")

router = APIRouter(prefix="/canales", tags=["agente"])

# Las tareas en segundo plano se guardan acá mientras corren: asyncio solo
# mantiene una referencia débil, y una tarea sin referencia fuerte puede ser
# recolectada a media ejecución.
_EN_CURSO: set[asyncio.Task] = set()


def _exigir_encendido() -> None:
    if not settings.AGENTE_EN_CODIGO:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El agente en código no está habilitado (AGENTE_EN_CODIGO)",
        )


@router.get("/entrada")
async def verificar_suscripcion(request: Request) -> PlainTextResponse:
    """
    Handshake de Meta al dar de alta el webhook: devuelve `hub.challenge`.

    No se exige `AGENTE_EN_CODIGO`: suscribir el webhook es justo el paso
    previo a encender la bandera, y tiene que poder hacerse antes.
    """
    try:
        challenge = firma.resolver_challenge(dict(request.query_params))
    except firma.FirmaInvalida as e:
        log.warning("Handshake de Meta rechazado: %s", e.motivo)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return PlainTextResponse(challenge)


@router.options("/entrada")
async def preflight() -> Response:
    """
    Preflight del widget. El navegador lo manda antes del POST con
    Content-Type: application/json desde otro origen.

    Responde permisivo porque en este punto todavía no hay cuerpo del que
    sacar la `widget_key`, así que no se sabe qué origen autorizar. Lo que
    de verdad decide es el POST, que sí valida el Origin contra los dominios
    del tenant.
    """
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
        },
    )


@router.post("/entrada")
async def entrada(request: Request) -> Response:
    """
    Recibe un mensaje de cualquiera de los cuatro canales.

    Nunca devuelve 5xx por un mensaje que no se pueda atender: el proveedor
    lo reintentaría y el cliente recibiría la misma respuesta dos veces. Los
    únicos errores son 403 (no se pudo autenticar) y 503 (apagado).
    """
    _exigir_encendido()

    cuerpo_crudo = await request.body()
    cabeceras = {k.lower(): v for k, v in request.headers.items()}

    try:
        peticion = await _autenticar(cuerpo_crudo, cabeceras)
    except firma.FirmaInvalida as e:
        log.warning("Petición rechazada en /canales/entrada: %s", e.motivo)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    if peticion.origen == "widget":
        return await _atender_widget(peticion)

    # Meta espera el acuse ya. El resto corre suelto: el agente puede
    # tardar varios segundos entre el modelo y las herramientas.
    tarea = asyncio.create_task(_procesar_en_segundo_plano(peticion))
    _EN_CURSO.add(tarea)
    tarea.add_done_callback(_EN_CURSO.discard)

    return PlainTextResponse("EVENT_RECEIVED")


async def _autenticar(cuerpo_crudo: bytes, cabeceras: dict[str, str]) -> PeticionVerificada:
    """
    Elige el camino por las cabeceras y devuelve la petición ya verificada.

    El orden importa: el proxy interno se reconoce por su cabecera antes que
    nada, porque ese cuerpo ya no trae la firma de Meta (la validó él).
    """
    if cabeceras.get("x-internal-token"):
        tenant_id = firma.verificar_token_interno(cabeceras)
        return PeticionVerificada(
            cuerpo=_parsear(cuerpo_crudo),
            origen="interno",
            tenant_id=tenant_id,
            proveedor=cabeceras.get("x-proveedor"),
        )

    if cabeceras.get("x-hub-signature-256"):
        cuerpo = await firma.verificar_firma_meta(cuerpo_crudo, cabeceras)
        return PeticionVerificada(cuerpo=cuerpo, origen="meta")

    cuerpo = _parsear(cuerpo_crudo)
    if cuerpo.get("object") != "web_widget":
        # Sin firma, sin token y sin ser del widget: no hay forma de saber
        # quién es. Se rechaza en vez de intentar adivinar el canal.
        raise firma.FirmaInvalida("Petición sin credenciales reconocibles")

    autorizado = await firma.verificar_widget(cuerpo, cabeceras)
    return PeticionVerificada(
        cuerpo=cuerpo,
        origen="widget",
        tenant_id=autorizado.tenant_id,
        origen_permitido=autorizado.origen_permitido,
    )


def _parsear(cuerpo_crudo: bytes) -> dict[str, Any]:
    try:
        cuerpo = json.loads(cuerpo_crudo)
    except json.JSONDecodeError:
        raise firma.FirmaInvalida("Cuerpo no es JSON válido") from None
    if not isinstance(cuerpo, dict):
        raise firma.FirmaInvalida("Cuerpo no es un objeto JSON")
    return cuerpo


async def _atender_widget(peticion: PeticionVerificada) -> JSONResponse:
    """
    Procesa y responde en la misma petición: el navegador está esperando.

    Siempre 200, incluso cuando no hay nada que contestar: un 4xx dejaría al
    visitante viendo un error de red en vez de un mensaje.
    """
    cabeceras = {
        "Access-Control-Allow-Origin": peticion.origen_permitido or "*",
        "Content-Type": "application/json",
    }

    try:
        resultado = await ingreso.procesar(peticion)
    except Exception:
        log.exception("Fallo al procesar un mensaje del widget")
        return JSONResponse(
            {"respuesta": "No pudimos procesar tu mensaje en este momento. Intenta de nuevo."},
            headers=cabeceras,
        )

    respuesta = await _resolver_respuesta(resultado)
    return JSONResponse({"respuesta": respuesta}, headers=cabeceras)


async def _procesar_en_segundo_plano(peticion: PeticionVerificada) -> None:
    """
    El camino de Meta, ya con el acuse enviado.

    Se traga cualquier excepción: no hay nadie esperando esta corrutina, y
    una que termine en error dejaría un "Task exception was never retrieved"
    sin contexto en el log.
    """
    # Meta puede agrupar varios eventos en un POST: se atiende cada uno por
    # separado y a la vez, igual que si hubieran llegado en POST distintos,
    # para que la espera anti-ráfaga de `ingreso` los junte en una respuesta.
    sobres = normalizar_mod.dividir_sobre(peticion.cuerpo)
    await asyncio.gather(
        *(_procesar_un_evento(replace(peticion, cuerpo=s)) for s in sobres)
    )


async def _procesar_un_evento(peticion: PeticionVerificada) -> None:
    try:
        resultado = await ingreso.procesar(peticion)
        if resultado.motivo:
            log.info("Mensaje no atendido: %s", resultado.motivo)
        await _resolver_respuesta(resultado)
    except Exception:
        log.exception("Fallo al procesar un mensaje entrante")


async def _resolver_respuesta(resultado: ResultadoIngreso) -> str | None:
    """
    Entrega lo que haya que entregar y devuelve el texto, si lo hay.

    `respuesta_inmediata` es un texto fijo que no pasa por el modelo (aviso
    de plan agotado, acuse de transferencia, acuse de baja o alta). Cuando en
    cambio hay contexto, le toca al agente.
    """
    if resultado.respuesta_inmediata is not None:
        await _entregar(resultado.destino, resultado.respuesta_inmediata)
        return resultado.respuesta_inmediata

    contexto = resultado.contexto
    if contexto is None:
        return None

    respuesta = await loop_agente.responder(contexto)

    # Si el agente transfirió, el bot calla: el acuse es un texto fijo que
    # manda `ingreso` con el siguiente mensaje del cliente.
    if respuesta.transferida:
        return None

    await _guardar_respuesta(contexto, respuesta.texto)
    await _entregar(contexto.destino, respuesta.texto)
    return respuesta.texto


async def _entregar(destino: Destino | None, texto: str) -> None:
    """
    Manda el texto por el canal del cliente.

    El widget no pasa por aquí: su respuesta viaja en el cuerpo de esta misma
    petición HTTP, que es lo que el navegador está esperando.
    """
    if destino is None or destino.es_web:
        return

    resultado = await envio.entregar(destino, texto)
    if not resultado.entregado:
        # Ya quedó en `delivery_errors` con su diagnóstico. El mensaje está
        # guardado en el historial y la conversación sigue viva, así que no
        # se levanta: reintentar tampoco arreglaría un token caducado.
        log.warning(
            "Sin entregar por %s al tenant %s: %s",
            destino.canal, destino.tenant_id, resultado.motivo,
        )


async def _guardar_respuesta(contexto: ContextoMensaje, texto: str) -> None:
    """Deja la respuesta en el historial, para que la siguiente vuelta la vea."""
    await execute(
        """
        INSERT INTO messages (conversation_id, tenant_id, role, content)
        VALUES ($1, $2, 'assistant', $3)
        """,
        contexto.conversation_id,
        contexto.tenant_id,
        texto,
    )
    await execute(
        "UPDATE conversations SET last_message_at = NOW() WHERE id = $1",
        contexto.conversation_id,
    )
