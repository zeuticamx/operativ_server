"""
El bucle de tool-calling: lo que hacía el nodo "Agent" de n8n.

Pide una respuesta al modelo; si pide herramientas, las ejecuta, le devuelve
los resultados y vuelve a preguntar, hasta que contesta sin pedir nada más.

Es un bucle escrito a mano y no el tool runner del SDK, que es la opción por
defecto. Tres razones concretas:

  1. La descripción de `ejecutar_herramienta` lleva incrustado el catálogo del
     tenant, así que cambia en cada petición. El runner saca el esquema de la
     firma y el docstring de una función, que son estáticos.
  2. El tenant, la conversación y el mensaje NO los decide el modelo: se
     inyectan al ejecutar. Acá eso es explícito y auditable en un sitio.
  3. Hay que sumar el consumo de tokens de cada vuelta —una respuesta con dos
     herramientas son tres llamadas al modelo— y registrarlo una sola vez.

Eso último es lo que elimina el workflow `medir-uso-tokens`: en n8n el
`tokenUsage` quedaba en una salida del sub-nodo que ningún nodo hermano podía
leer, así que un job aparte sondeaba la API de ejecuciones cada 2 minutos para
recuperarlo. Acá viene en la respuesta de cada llamada.

Nunca se escribe en el log el contenido de los mensajes ni la respuesta del
modelo: solo identificadores y conteos (hallazgo 3 de la auditoría).

Caché de prompt: se va DIRECTO a la API de Anthropic, sin el proxy que hubo
que montar para n8n porque sus nodos de Anthropic no tenían caché nativo. Hay
dos cortes, y los dos dependen de que lo estable vaya antes que lo volátil:

  - El bloque estable de `system` (reglas, prompt del tenant, catálogo), que
    se reaprovecha entre mensajes y entre conversaciones del mismo negocio.
    Lo pone `prompt.bloques_system`; la fecha y hora quedan en el bloque
    siguiente a propósito, porque dentro del cacheado invalidarían el prefijo
    en cada mensaje.
  - El último turno de `mensajes`, que se mueve en cada vuelta de este bucle
    (ver `_mover_breakpoint`). Sin él, una conversación de tres vueltas paga
    el precio completo del historial acumulado tres veces.

Las herramientas se renderizan antes de `system`, así que el primer corte
cachea las dos cosas juntas. Que eso funcione depende de que el prefijo sea
determinista byte a byte: el catálogo sale con `ORDER BY display_name` y las
reglas de citas se eligen por pertenencia a un set, nunca iterándolo —el orden
de iteración de un set de strings cambia entre procesos y haría fallar el
caché de forma intermitente e inexplicable.

El mínimo cacheable depende del modelo y NO es monótono: 512 tokens en Opus 5,
1024 en Sonnet 5 y 4096 en Haiku 4.5 — el modelo más barato tiene la barrera
más alta. Un prefijo que no llega no da error: simplemente no cachea y no
cobra escritura, así que el fallo es silencioso. Medido el 16/10/2026 sobre
este mismo código:

    tenant con el calendario completo   ~4.400-5.000 tokens  -> cachea en los
                                                                tres modelos
    tenant sin herramientas, prompt corto  ~660-760 tokens   -> solo Opus 5

O sea que el caso que no cachea es el negocio pequeño, no el modelo barato. Y
ojo con una consecuencia contraintuitiva: el catálogo del tenant viaja DOS
veces en el prefijo —una en la descripción de `ejecutar_herramienta` y otra en
el `# HERRAMIENTAS` de `system`—, y esa duplicación es justo lo que hace que un
tenant con calendario pase de 4096 y cachee en Haiku. Parece desperdicio y en
parte lo es, pero deduplicarla sin medir el efecto sobre el umbral puede sacar
a los tenants de Haiku del caché y encarecerlos.

Para comprobarlo en producción: `Consumo.cache_lectura`. Si es 0 de forma
sostenida para un tenant, su prefijo no llega al mínimo de su modelo.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Optional
from uuid import UUID

import anthropic

from config import settings
from services.agente import costos, escalar, herramientas, modelo as modelo_mod, prompt
from services.agente.modelos import ContextoMensaje
from session import execute

log = logging.getLogger("operativai.agente.loop")

# Tope de vueltas al modelo por mensaje del cliente. Una gestión real encadena
# pocas herramientas (ver citas → consultar cancelación → cancelar); pasado
# eso, el agente está dando vueltas y es mejor cortar que seguir gastando.
MAX_VUELTAS = 6

RESPUESTA_VACIA = "Disculpa, tuve un problema al procesar tu mensaje. ¿Puedes repetirlo?"

# El modelo declinó por sus clasificadores de seguridad. No es un error de la
# API: llega como 200 con stop_reason "refusal" y sin contenido utilizable.
RESPUESTA_RECHAZO = (
    "Disculpa, no puedo ayudarte con eso por este medio. ¿Quieres que te ponga en "
    "contacto con una persona del equipo?"
)


@dataclass
class Consumo:
    """Tokens sumados de todas las vueltas de este mensaje."""

    entrada: int = 0
    salida: int = 0
    cache_lectura: int = 0
    cache_escritura: int = 0
    llamadas: int = 0

    def sumar(self, usage: Any) -> None:
        self.entrada += getattr(usage, "input_tokens", 0) or 0
        self.salida += getattr(usage, "output_tokens", 0) or 0
        self.cache_lectura += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_escritura += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.llamadas += 1


@dataclass
class Respuesta:
    texto: str
    consumo: Consumo = field(default_factory=Consumo)
    modelo: str = ""
    # El agente transfirió la conversación: a partir de acá el bot calla y no
    # hay que mandar su texto como si fuera una respuesta normal.
    transferida: bool = False


@lru_cache
def _cliente() -> anthropic.AsyncAnthropic:
    """
    Una instancia por proceso, como `whatsapp.obtener_proveedor()`.

    Sin clave configurada falla al construirse y no en mitad de la primera
    conversación.
    """
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY sin configurar: el agente no puede responder")
    return anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)


async def responder(contexto: ContextoMensaje) -> Respuesta:
    """
    Genera la respuesta del agente para el mensaje ya persistido.

    Solo devuelve el texto: guardarlo y entregarlo por el canal es de quien
    llama. El consumo de tokens sí se registra acá, porque es lo único que
    conoce el detalle de cada vuelta.
    """
    config = contexto.config
    if config is None:
        if contexto.agente_canal_id is not None:
            config = await prompt.config_numero(contexto.agente_canal_id)
        else:
            config = await prompt.config_social(contexto.tenant_id)
    catalogo, filas = await prompt.catalogo_herramientas(
        contexto.tenant_id, contexto.agente_canal_id
    )
    claves = {f["tool_key"] for f in filas}
    eventos = await prompt.eventos_de_contexto(contexto.conversation_id)

    nombre_modelo = config["model"]

    # Marcar el bloque siempre: si no llega al mínimo cacheable del modelo
    # simplemente no se cachea, sin error y sin pagar escritura.
    system = prompt.bloques_system(
        contexto, config, catalogo, claves, cacheable=True, eventos=eventos
    )

    mensajes: list[dict[str, Any]] = await prompt.historial(
        contexto, int(config["history_window"])
    )
    mensajes.append({"role": "user", "content": contexto.contenido})

    herramientas_disponibles = [
        herramientas.definicion(catalogo),
        escalar.definicion(),
    ]

    consumo = Consumo()
    respuesta = Respuesta(texto="", consumo=consumo, modelo=nombre_modelo)

    try:
        await _vueltas(
            contexto=contexto,
            respuesta=respuesta,
            system=system,
            mensajes=mensajes,
            herramientas_disponibles=herramientas_disponibles,
            nombre_modelo=nombre_modelo,
            temperature=config.get("temperature"),
            agente_canal_id=contexto.agente_canal_id,
        )
    except anthropic.APIStatusError as e:
        log.error(
            "La API respondió %s para la conversación %s",
            e.status_code, contexto.conversation_id,
        )
        if e.status_code in (401, 403):
            # Casi siempre es la clave: revocada, de otro proyecto, o con
            # comillas o espacios pegados en el panel de despliegue.
            log.error(
                "Anthropic rechazó ANTHROPIC_API_KEY (%s): revisa que la clave sea "
                "vigente y no tenga comillas ni espacios en el panel de despliegue.",
                e.status_code,
            )
        respuesta.texto = RESPUESTA_VACIA
    except anthropic.APIConnectionError:
        log.error("Sin conexión con la API para la conversación %s", contexto.conversation_id)
        respuesta.texto = RESPUESTA_VACIA

    await _registrar_consumo(contexto, respuesta)

    if not respuesta.texto.strip():
        respuesta.texto = RESPUESTA_VACIA
    return respuesta


def _mover_breakpoint(mensajes: list[dict[str, Any]]) -> None:
    """
    Deja UN `cache_control` en el último bloque del último turno.

    Cada vuelta del bucle vuelve a mandar el array de mensajes ya crecido en
    una petición nueva. Sin esto, una conversación de tres vueltas paga el
    precio completo de los resultados de herramienta acumulados tres veces;
    con el corte en el último turno, la vuelta siguiente lee ese prefijo de
    caché a ~0,1x. Es el caso donde el caché nunca falla: las peticiones van a
    milisegundos una de otra, muy dentro del TTL de 5 minutos.

    **Se mueve en vez de acumular.** La petición admite 4 breakpoints como
    máximo y `bloques_system` ya gasta uno, así que marcar cada vuelta sin
    quitar la anterior daría un 400 a partir de la cuarta. Quitar la marca
    vieja no pierde nada: la entrada de caché ya está escrita y el breakpoint
    nuevo la encuentra igual, porque la búsqueda mira hacia atrás por sí sola.

    Ese "hacia atrás" tiene un límite de 20 bloques, y es el único borde a
    vigilar: una vuelta en la que el modelo pida muchas herramientas en
    paralelo puede meter más de 20 bloques de golpe, y entonces el breakpoint
    de la vuelta siguiente no alcanza la entrada anterior y se escribe de
    nuevo en vez de leerse. No rompe nada —se paga como hoy— así que no se
    complica el código por ese caso.
    """
    for mensaje in mensajes:
        contenido = mensaje.get("content")
        if isinstance(contenido, list):
            for bloque in contenido:
                # Los bloques del turno `assistant` vienen del SDK (objetos,
                # no dicts) y nunca llevan marca: no hay nada que quitarles.
                if isinstance(bloque, dict):
                    bloque.pop("cache_control", None)

    ultimo = mensajes[-1]
    contenido = ultimo["content"]
    if isinstance(contenido, str):
        # Un `content` de texto plano no tiene dónde colgar la marca: se
        # reescribe como un bloque, que renderiza igual.
        contenido = [{"type": "text", "text": contenido}]
        ultimo["content"] = contenido

    if contenido and isinstance(contenido[-1], dict):
        contenido[-1]["cache_control"] = {"type": "ephemeral"}


async def _vueltas(
    *,
    contexto: ContextoMensaje,
    respuesta: Respuesta,
    system: list[dict[str, Any]],
    mensajes: list[dict[str, Any]],
    herramientas_disponibles: list[dict[str, Any]],
    nombre_modelo: str,
    temperature: float | None,
    agente_canal_id: Optional[UUID] = None,
) -> None:
    """El ciclo pedir → ejecutar → devolver, hasta que el modelo deja de pedir."""
    cliente = _cliente()
    extra = modelo_mod.parametros_modelo(nombre_modelo, temperature=temperature)

    for vuelta in range(MAX_VUELTAS):
        mensaje = await cliente.messages.create(
            model=nombre_modelo,
            max_tokens=settings.AGENTE_MAX_TOKENS,
            system=system,
            messages=mensajes,
            tools=herramientas_disponibles,
            **extra,
        )
        respuesta.consumo.sumar(mensaje.usage)
        respuesta.modelo = mensaje.model

        # Antes de leer el contenido: en un rechazo viene vacío o a medias.
        if mensaje.stop_reason == "refusal":
            categoria = getattr(getattr(mensaje, "stop_details", None), "category", None)
            log.warning(
                "El modelo declinó la conversación %s (categoría=%s)",
                contexto.conversation_id, categoria,
            )
            respuesta.texto = RESPUESTA_RECHAZO
            return

        respuesta.texto = _texto_de(mensaje.content)

        if mensaje.stop_reason != "tool_use":
            if mensaje.stop_reason == "max_tokens":
                log.warning(
                    "Respuesta truncada por max_tokens en la conversación %s",
                    contexto.conversation_id,
                )
            return

        llamadas = [b for b in mensaje.content if b.type == "tool_use"]
        mensajes.append({"role": "assistant", "content": mensaje.content})

        resultados = await _ejecutar_llamadas(
            contexto, llamadas, vuelta, agente_canal_id=agente_canal_id
        )

        # Todos los resultados en UN solo mensaje de usuario. Repartirlos en
        # varios le enseña al modelo a dejar de pedir herramientas en paralelo.
        mensajes.append({"role": "user", "content": [r for r, _ in resultados]})

        # Acá y no antes de la primera petición: el modelo acaba de pedir
        # herramientas, así que la vuelta siguiente es segura y la escritura
        # (1,25x) se amortiza en esa misma lectura. Marcarlo antes de saberlo
        # sería apostar: en una conversación que se resuelve sin herramientas
        # —el caso normal— se pagaría el recargo sin que nadie lo lea nunca.
        _mover_breakpoint(mensajes)

        if any(transferida for _, transferida in resultados):
            # La conversación pasó a una persona. Se corta acá: el acuse al
            # cliente es un texto fijo que no pasa por el modelo, para que un
            # cliente ya transferido no reciba más respuestas de la IA.
            respuesta.transferida = True
            return

    log.warning(
        "Se agotaron las %s vueltas en la conversación %s",
        MAX_VUELTAS, contexto.conversation_id,
    )


async def _ejecutar_llamadas(
    contexto: ContextoMensaje,
    llamadas: list[Any],
    vuelta: int,
    agente_canal_id: Optional[UUID] = None,
) -> list[tuple[dict[str, Any], bool]]:
    """
    Ejecuta las herramientas que pidió el modelo, en paralelo si son varias.

    Devuelve un `tool_result` por llamada —incluidas las que fallaron, porque
    omitir uno rompe el turno— junto con si esa llamada transfirió la
    conversación.
    """
    contacto = {
        "nombre": contexto.nombre,
        "telefono": contexto.usuario_id,
        "canal": contexto.canal,
    }

    async def una(llamada: Any) -> tuple[dict[str, Any], bool]:
        entrada = llamada.input or {}

        if llamada.name == "escalar_humano":
            texto = await escalar.escalar(
                tenant_id=contexto.tenant_id,
                conversation_id=contexto.conversation_id,
                canal=contexto.canal,
                motivo=entrada.get("motivo"),
                area=entrada.get("area"),
                cliente_nombre=contexto.nombre,
                cliente_telefono=contexto.usuario_id,
            )
            transferida = True
        elif llamada.name == "ejecutar_herramienta":
            texto = await herramientas.ejecutar(
                tenant_id=contexto.tenant_id,
                tool_key=entrada.get("tool_key") or "",
                parametros=entrada.get("parametros") or {},
                conversation_id=contexto.conversation_id,
                mensaje_id=contexto.mensaje_id,
                contacto=contacto,
                # Idempotencia del cobro: el id que asigna el modelo a esta
                # llamada. Un reintento de la misma respuesta no cobra dos veces.
                referencia_cobro=f"tool:{llamada.id}",
                agente_canal_id=agente_canal_id,
            )
            transferida = False
        else:
            log.error("El modelo pidió una herramienta desconocida: %s", llamada.name)
            texto = "Esa herramienta no existe."
            transferida = False

        return (
            {"type": "tool_result", "tool_use_id": llamada.id, "content": texto},
            transferida,
        )

    log.info(
        "Vuelta %s: %s herramienta(s) en la conversación %s",
        vuelta + 1, len(llamadas), contexto.conversation_id,
    )
    return list(await asyncio.gather(*(una(ll) for ll in llamadas)))


def _texto_de(bloques: list[Any]) -> str:
    """El texto visible de la respuesta, ignorando thinking y tool_use."""
    return "\n".join(b.text for b in bloques if b.type == "text").strip()


async def _registrar_consumo(contexto: ContextoMensaje, respuesta: Respuesta) -> None:
    """
    Deja el consumo real del mensaje donde ya lo esperan el panel y la tabla
    de uso. Sustituye entero al workflow `medir-uso-tokens`.

    La clave de idempotencia sale del id del mensaje en BD: si esta pasada se
    reintentara, no se contaría dos veces.
    """
    if not respuesta.consumo.llamadas:
        return

    from services.gerencia import registrar_uso_tokens

    # Lo leído de caché también se procesó y se cobra (más barato): cuenta
    # como entrada, igual que en la factura. Esto es VOLUMEN, no precio.
    entrada = (
        respuesta.consumo.entrada
        + respuesta.consumo.cache_lectura
        + respuesta.consumo.cache_escritura
    )

    # El precio se calcula con las tres partes por separado, no con el total
    # de arriba: lo cacheado cuesta ~0,1x y lo escrito en caché 1,25x, así que
    # cobrar el total a tarifa plena sobreestimaría cada conversación. None si
    # el modelo no tiene tarifa conocida: la columna queda NULL, que dice "no
    # se sabe", en vez de 0, que diría "fue gratis".
    costo = costos.calcular_costo(
        respuesta.modelo,
        entrada=respuesta.consumo.entrada,
        salida=respuesta.consumo.salida,
        cache_lectura=respuesta.consumo.cache_lectura,
        cache_escritura=respuesta.consumo.cache_escritura,
    )

    try:
        registrado = await registrar_uso_tokens(
            contexto.tenant_id,
            conversation_id=contexto.conversation_id,
            origen="agente",
            modelo=respuesta.modelo[:100],
            tokens_entrada=entrada,
            tokens_salida=respuesta.consumo.salida,
            costo_usd=costo,
            idempotency_key=f"msg:{contexto.db_message_id}:agente",
        )
        if registrado:
            await execute(
                """
                INSERT INTO usage_events
                    (tenant_id, conversation_id, tipo, tokens_input, tokens_output)
                VALUES ($1, $2, 'mensaje_procesado', $3, $4)
                """,
                contexto.tenant_id,
                contexto.conversation_id,
                entrada,
                respuesta.consumo.salida,
            )
    except Exception:
        # Perder la medición no puede costarle la respuesta al cliente.
        log.exception(
            "No se pudo registrar el consumo de la conversación %s",
            contexto.conversation_id,
        )

    log.info(
        "Conversación %s: %s llamada(s), %s tokens entrada (%s de caché), %s salida",
        contexto.conversation_id,
        respuesta.consumo.llamadas,
        entrada,
        respuesta.consumo.cache_lectura,
        respuesta.consumo.salida,
    )
