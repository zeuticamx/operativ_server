"""
Modo "Probar agente" del portal: el dueño escribe como si fuera un cliente y ve
cómo contestaría su agente, sin mandarle nada a nadie.

No es un simulador aparte: arma un `ContextoMensaje` sintético y llama al mismo
`loop_agente.responder` que usa el agente real (mismo prompt de plataforma,
mismas herramientas del número, mismos parámetros del modelo), en modo
`simulacion`. Probar con otro código no probaría nada.

Qué NO hace, por construcción:
  - No persiste nada: la conversación de prueba vive en el navegador y llega
    entera en cada petición. El `conversation_id` es un uuid que nunca existió,
    así que ninguna herramienta puede leer los datos de un cliente real.
  - No cobra créditos ni registra consumo del tenant: los tokens los absorbe la
    plataforma, y por eso hay un cupo diario (`LIMITE_DIARIO`).
  - No ejecuta herramientas con efectos (ver `herramientas.se_ejecuta_en_prueba`).

Prueba el BORRADOR del formulario (lo que aún no se guardó), validado contra
los mismos límites del plan que `PUT /api/agente`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from services.agente import loop_agente
from services.agente.modelos import ContextoMensaje
from services.agente.prompt import ZONA_POR_DEFECTO
from session import execute, fetch_one

# Mensajes del dueño por negocio y día. Cada uno cuesta tokens reales.
LIMITE_DIARIO = 20

# Topes de lo que viaja en cada petición: la conversación entera va en el body.
MAX_TURNOS = 40
MAX_CARACTERES_TURNO = 4000


class CupoAgotado(Exception):
    """Se acabaron los mensajes de prueba de hoy."""


class PruebaNoDisponible(Exception):
    """El modelo no pudo contestar (clave ausente, API caída). `mensaje` es para el dueño."""

    def __init__(self, mensaje: str) -> None:
        super().__init__(mensaje)
        self.mensaje = mensaje


@dataclass
class ResultadoPrueba:
    texto: str
    modelo: str
    transferida: bool
    herramientas: list[dict[str, Any]]
    tokens_entrada: int
    tokens_salida: int
    restantes_hoy: int


def preparar_turnos(
    turnos: list[dict[str, str]], ventana: int
) -> tuple[list[dict[str, str]], str]:
    """
    Separa la conversación de prueba en (historial, mensaje nuevo).

    El último turno debe ser del usuario: es lo que se le pregunta al agente.
    El historial se recorta a `ventana` mensajes, como haría la conversación
    real, y no puede empezar por un turno del agente (la API pide que el primero
    sea del usuario). Levanta `ValueError` con un texto apto para el dueño.
    """
    if not turnos:
        raise ValueError("Escribe un mensaje para probar al agente.")
    if len(turnos) > MAX_TURNOS:
        raise ValueError("La conversación de prueba es muy larga: reiníciala para seguir.")
    ultimo = turnos[-1]
    if ultimo["role"] != "user" or not ultimo["content"].strip():
        raise ValueError("El último mensaje debe ser tuyo y no estar vacío.")

    historial = [
        {"role": t["role"], "content": t["content"]}
        for t in turnos[:-1]
        if t["content"].strip()
    ][-max(ventana, 0):]
    while historial and historial[0]["role"] != "user":
        historial.pop(0)
    return historial, ultimo["content"]


async def consumir_cupo(tenant_id: UUID) -> int:
    """
    Descuenta un mensaje del cupo de hoy y devuelve cuántos quedan.

    Atómico: el UPSERT solo suma mientras no se llegó al tope, así que dos
    peticiones simultáneas no pueden pasarse del límite.
    """
    fila = await fetch_one(
        """
        INSERT INTO agente_pruebas_uso (tenant_id, dia, mensajes)
        VALUES ($1, (NOW() AT TIME ZONE $2)::date, 1)
        ON CONFLICT (tenant_id, dia) DO UPDATE
           SET mensajes = agente_pruebas_uso.mensajes + 1
         WHERE agente_pruebas_uso.mensajes < $3
        RETURNING mensajes
        """,
        tenant_id,
        ZONA_POR_DEFECTO,
        LIMITE_DIARIO,
    )
    if fila is None:
        raise CupoAgotado()
    return LIMITE_DIARIO - int(fila["mensajes"])


async def devolver_cupo(tenant_id: UUID) -> None:
    """Reintegra un mensaje: la prueba falló por culpa nuestra, no del dueño."""
    await execute(
        """
        UPDATE agente_pruebas_uso
           SET mensajes = GREATEST(mensajes - 1, 0)
         WHERE tenant_id = $1 AND dia = (NOW() AT TIME ZONE $2)::date
        """,
        tenant_id,
        ZONA_POR_DEFECTO,
    )


async def probar(
    *,
    tenant_id: UUID,
    config: dict[str, Any],
    turnos: list[dict[str, str]],
    agente_canal_id: Optional[UUID],
    restantes_hoy: int,
) -> ResultadoPrueba:
    """
    Corre al agente con `config` (el borrador, ya validado) sobre la conversación
    de prueba. El cupo ya lo descontó quien llama, para poder reintegrarlo si
    algo falla.
    """
    historial, mensaje = preparar_turnos(turnos, int(config["history_window"]))

    contexto = ContextoMensaje(
        tenant_id=tenant_id,
        conversation_id=uuid4(),
        user_id=uuid4(),
        # Un número de WhatsApp contesta por whatsapp; Instagram, Facebook y el
        # widget comparten config y el widget es el canal más neutro.
        canal="whatsapp" if agente_canal_id else "web",
        usuario_id="prueba",
        nombre="Cliente de prueba",
        contenido=mensaje,
        tipo="text",
        mensaje_id=None,
        db_message_id=uuid4(),
        db_message_created_at=datetime.now(timezone.utc),
        agente_canal_id=agente_canal_id,
        config=config,
    )

    try:
        respuesta = await loop_agente.responder(
            contexto,
            simulacion=True,
            # `responder` agrega `contexto.contenido` como último turno.
            historial_previo=historial,
        )
    except RuntimeError as e:
        # `_cliente()` sin ANTHROPIC_API_KEY: es de la plataforma, no del dueño.
        raise PruebaNoDisponible("El agente no está disponible en este momento.") from e

    if respuesta.fallo:
        raise PruebaNoDisponible(
            "No se pudo obtener respuesta del modelo. Inténtalo de nuevo en un momento."
        )

    return ResultadoPrueba(
        texto=respuesta.texto,
        modelo=respuesta.modelo,
        transferida=respuesta.transferida,
        herramientas=respuesta.herramientas,
        tokens_entrada=(
            respuesta.consumo.entrada
            + respuesta.consumo.cache_lectura
            + respuesta.consumo.cache_escritura
        ),
        tokens_salida=respuesta.consumo.salida,
        restantes_hoy=restantes_hoy,
    )
