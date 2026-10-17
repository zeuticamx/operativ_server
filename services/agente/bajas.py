"""
Quién pidió no recibir más mensajes.

Cierra el hallazgo 1 de la auditoría: `envio._reabrir_con_plantilla` es el
único punto del flujo que le escribe a un cliente sin que él haya escrito
primero, y salía igual para alguien que ya había pedido la baja. Aquí están
las dos mitades que faltaban: reconocer la petición en el mensaje entrante
(lo usa `ingreso`) y consultarla antes de mandar la plantilla (lo usa
`envio`).

Dos decisiones que conviene tener claras antes de tocar las listas de abajo:

  - **El cotejo es por mensaje COMPLETO, no por subcadena.** Es la decisión
    importante de este módulo y no es una preferencia de estilo: este agente
    atiende citas, y "cancelar" es lo que escribe un cliente que quiere
    anular su reserva. Si `cancelar` contara como baja —o si bastara con que
    apareciera dentro de la frase— cada cancelación de cita dejaría al
    negocio sin poder volver a escribirle a ese cliente, y encima el agente
    no atendería la cancelación. Por eso "cancelar cita" sigue su camino
    normal y solo "cancelar suscripcion" es una baja.

  - **La baja NO silencia al agente.** Bloquea solo la plantilla de
    reapertura. Si el cliente de baja vuelve a escribir, se le contesta: al
    escribir él se abre la ventana de 24 h y la conversación la inicia él.
    Lo que la baja prohíbe es que el negocio la arranque.

Lo que sí queda fuera, y es un límite real: una petición redactada a mano
("oigan, prefiero que no me manden más promociones") no la reconoce nadie.
Hacerlo bien pide pasarla por el modelo, que es otra decisión y otro coste
por mensaje. La lista cerrada cubre las fórmulas que Meta y los operadores
enseñan a usar, que son las que la gente escribe cuando quiere darse de baja
de verdad.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from uuid import UUID

from session import execute, fetch_value

__all__ = [
    "ACUSE_ALTA",
    "ACUSE_BAJA",
    "esta_de_baja",
    "palabra_de_alta",
    "palabra_de_baja",
    "quitar",
    "registrar",
]

log = logging.getLogger("operativai.agente.bajas")

# Lo que se le contesta a quien acaba de darse de baja. Texto fijo, no lo
# genera el modelo: es la confirmación de una decisión del cliente y tiene
# que decir siempre lo mismo, incluido cómo volver.
ACUSE_BAJA = (
    "Listo, no volveremos a escribirte. "
    "Si cambias de idea, responde ALTA y seguimos en contacto."
)

ACUSE_ALTA = "Listo, volvemos a estar en contacto. ¿En qué podemos ayudarte?"

# Mensajes que valen como petición de baja, ya normalizados (minúsculas, sin
# tildes, sin signos). Cotejo por mensaje completo: ver el docstring.
#
# No están, y no es un olvido: "cancelar", "cancelar cita", "cancelar
# reserva", "no" y "ya no". Las tres primeras son la cancelación de una cita,
# que es trabajo del agente; las otras dos son respuestas normales de una
# conversación.
_BAJA = frozenset({
    "baja",
    "stop",
    "parar",
    "dar de baja",
    "darme de baja",
    "quiero darme de baja",
    "me doy de baja",
    "no molestar",
    "no mas mensajes",
    "no quiero mas mensajes",
    "no quiero recibir mas mensajes",
    "no quiero recibir mensajes",
    "no me escriban",
    "no me escriban mas",
    "no me vuelvan a escribir",
    "cancelar suscripcion",
    "cancelar la suscripcion",
    "eliminar suscripcion",
    "unsubscribe",
    "remove me",
})

# Y las que deshacen la baja. "alta" y "start" son las que se anuncian en el
# acuse, el resto son las variantes que la gente escribe igualmente.
_ALTA = frozenset({
    "alta",
    "start",
    "dar de alta",
    "darme de alta",
    "suscribir",
    "suscribirme",
    "quiero recibir mensajes",
    "si quiero recibir mensajes",
})

# Todo lo que no sea letra o espacio se va. Quita signos, emoji y números, de
# forma que "STOP." y "stop 🙏" cuenten igual que "stop".
_NO_LETRAS = re.compile(r"[^a-z ]+")


def _normalizar(texto: str | None) -> str:
    """
    Deja el mensaje en minúsculas, sin tildes, sin signos y con un solo
    espacio entre palabras, para poder cotejarlo con las listas de arriba.

    Pura y sin base: es la parte que conviene probar con ejemplos.
    """
    if not texto:
        return ""

    descompuesto = unicodedata.normalize("NFD", texto)
    sin_tildes = "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")
    return " ".join(_NO_LETRAS.sub(" ", sin_tildes.lower()).split())


def palabra_de_baja(texto: str | None) -> str | None:
    """
    La palabra de la lista si el mensaje ES una petición de baja, o None.

    Devuelve la palabra en vez de un booleano porque es lo que se guarda en
    `agente_bajas.palabra`: deja constancia de qué disparó la baja sin
    guardar el mensaje del cliente.
    """
    normalizado = _normalizar(texto)
    return normalizado if normalizado in _BAJA else None


def palabra_de_alta(texto: str | None) -> str | None:
    """La palabra si el mensaje ES una petición de alta, o None."""
    normalizado = _normalizar(texto)
    return normalizado if normalizado in _ALTA else None


async def esta_de_baja(tenant_id: UUID, canal: str, usuario_id: str) -> bool:
    """
    Si este cliente pidió no recibir más mensajes de este negocio por este
    canal.

    Lo consulta `envio` antes de mandar la plantilla de reapertura. Ante la
    duda no hay duda: si la consulta falla se devuelve True y la plantilla no
    sale. Un mensaje de reapertura que no se manda es una conversación que no
    se reabre; uno que se manda a quien pidió la baja es un incumplimiento de
    Meta y una denuncia del cliente.
    """
    try:
        return bool(
            await fetch_value(
                """
                SELECT EXISTS (
                    SELECT 1 FROM agente_bajas
                     WHERE tenant_id = $1 AND channel_type = $2 AND user_id = $3
                )
                """,
                tenant_id,
                canal,
                usuario_id,
            )
        )
    except Exception:
        log.exception(
            "No se pudo comprobar la lista de bajas (tenant %s, canal %s): "
            "se asume baja y no se manda la plantilla",
            tenant_id,
            canal,
        )
        return True


async def registrar(
    tenant_id: UUID, canal: str, usuario_id: str, palabra: str | None = None
) -> None:
    """
    Apunta la baja. Idempotente: repetir BAJA refresca la fecha, no duplica.
    """
    await execute(
        """
        INSERT INTO agente_bajas (tenant_id, channel_type, user_id, palabra, origen)
        VALUES ($1, $2, $3, $4, 'cliente')
        ON CONFLICT (tenant_id, channel_type, user_id) DO UPDATE
            SET palabra    = EXCLUDED.palabra,
                origen     = EXCLUDED.origen,
                created_at = NOW()
        """,
        tenant_id,
        canal,
        usuario_id,
        palabra,
    )
    log.info("Baja registrada: tenant=%s canal=%s", tenant_id, canal)


async def quitar(tenant_id: UUID, canal: str, usuario_id: str) -> bool:
    """
    Levanta la baja. Devuelve si había alguna que levantar.

    Ese booleano es lo que deja `ingreso` distinguir entre un ALTA de alguien
    que estaba de baja —que merece su acuse— y la palabra "alta" suelta en
    una conversación cualquiera, que es un mensaje normal y lo contesta el
    agente.
    """
    fila = await fetch_value(
        """
        DELETE FROM agente_bajas
         WHERE tenant_id = $1 AND channel_type = $2 AND user_id = $3
        RETURNING 1
        """,
        tenant_id,
        canal,
        usuario_id,
    )
    if fila:
        log.info("Baja levantada: tenant=%s canal=%s", tenant_id, canal)
    return bool(fila)
