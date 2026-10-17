"""
Estructuras que viajan entre las etapas del agente.

Son dataclasses y no modelos de Pydantic a propósito: nunca cruzan la
frontera HTTP (eso es `schemas.py`), solo pasan de una función a otra dentro
del proceso. Mismo criterio que `ResultadoEnvio` en services/whatsapp.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

# Los cuatro canales que entran por el webhook. 'web' es el widget del sitio
# del negocio; los otros tres llegan por Meta (directo o vía NeuroAPI).
Canal = Literal["whatsapp", "instagram", "facebook", "web"]

# Columna de `users` donde se guarda el identificador del cliente en cada
# canal. El UPSERT la interpola en el SQL, así que es una lista cerrada: un
# canal que no esté acá no llega a tocar la base.
COLUMNA_POR_CANAL: dict[str, str] = {
    "whatsapp": "whatsapp_id",
    "instagram": "instagram_id",
    "facebook": "facebook_id",
    "web": "web_session_id",
}


@dataclass(frozen=True)
class PeticionVerificada:
    """
    Una petición que ya pasó el control de autenticidad de `firma.py`.

    `cuerpo` es el JSON ya parseado. `tenant_id` solo viene cuando quien
    autenticó fue el proxy interno (NeuroAPI) o el widget, que resuelven el
    tenant antes de llamar; en el camino de Meta directo se resuelve después,
    por el identificador del número o la página receptora.
    """

    cuerpo: dict[str, Any]
    origen: Literal["meta", "interno", "widget"]
    tenant_id: UUID | None = None
    # Solo en el widget: el Origin ya normalizado que se autorizó, para
    # devolverlo en Access-Control-Allow-Origin sin volver a calcularlo.
    origen_permitido: str | None = None
    proveedor: str | None = None


@dataclass(frozen=True)
class MensajeNormalizado:
    """
    El mensaje del cliente, ya desenvuelto del formato de su canal.

    `procesar=False` significa que el evento era legítimo pero no es un
    mensaje que el agente deba contestar (un acuse de entrega, una reacción,
    un eco del propio bot). No es un error: se responde 200 y se corta.
    """

    procesar: bool
    motivo: str | None = None

    canal: Canal | None = None
    tenant_id: UUID | None = None
    # Identificador del cliente dentro del canal: su teléfono en WhatsApp, el
    # sender id en IG/FB, el session_id en el widget.
    usuario_id: str | None = None
    nombre: str | None = None
    # Identificador del mensaje en el canal (wamid / mid). None en el widget.
    # Es la clave de deduplicación contra reintentos del proveedor.
    mensaje_id: str | None = None
    contenido: str | None = None
    tipo: str = "text"

    media_id: str | None = None
    media_url: str | None = None

    # Identificador de la cuenta receptora, para resolver el tenant. En
    # Instagram `ig_account_id` es el id de la CUENTA, que no es el page_id
    # contra el que luego hay que enviar: por eso van en campos distintos.
    phone_number_id: str | None = None
    page_id: str | None = None
    ig_account_id: str | None = None


@dataclass
class Destino:
    """
    Lo mínimo para escribirle a un cliente: a quién, por dónde y desde qué
    cuenta.

    Existe porque no todo lo que se entrega sale de una conversación. El
    aviso de "plan agotado" se manda antes de crearla —si el tenant no puede
    usar el agente no se abre hilo— y aun así hay que hacérselo llegar. Por
    eso `conversation_id` es opcional: sin ella no se puede comprobar la
    ventana de 24 h, pero esos avisos son respuesta inmediata a un mensaje
    del cliente, así que la ventana está abierta por definición.
    """

    tenant_id: UUID
    canal: Canal
    usuario_id: str
    nombre: str | None = None
    phone_number_id: str | None = None
    page_id: str | None = None
    conversation_id: UUID | None = None

    @property
    def es_web(self) -> bool:
        """
        El widget responde en la misma petición HTTP; los demás canales se
        contestan con una llamada aparte al proveedor. Varias decisiones del
        flujo cuelgan de esto.
        """
        return self.canal == "web"


@dataclass
class ContextoMensaje:
    """
    Todo lo que hace falta para contestar, una vez persistido el mensaje.

    Es lo que `ingreso.py` entrega a las etapas siguientes (prompt, bucle del
    agente, envío). `contenido` puede traer varias líneas si se agruparon
    mensajes de una ráfaga.
    """

    tenant_id: UUID
    conversation_id: UUID
    user_id: UUID
    canal: Canal
    usuario_id: str
    nombre: str | None
    contenido: str
    tipo: str
    mensaje_id: str | None

    # Id en BD del mensaje recién guardado: se excluye del historial para no
    # mandárselo al modelo dos veces (una como historial y otra como entrada).
    db_message_id: UUID
    db_message_created_at: datetime

    phone_number_id: str | None = None
    page_id: str | None = None

    # Número de WhatsApp por el que entró (None para Instagram, Facebook y
    # web, que usan la configuración social del negocio). Determina qué
    # configuración de agente y qué herramientas se usan (spec 005a, RF-7).
    agente_canal_id: UUID | None = None

    # Configuración de agente ya resuelta (por número o por social), para no
    # leerla dos veces. La rellena ingreso.py si se conoce; loop_agente la
    # resuelve si falta.
    config: dict[str, Any] | None = None

    conversation_status: str = "active"
    conversation_metadata: dict[str, Any] = field(default_factory=dict)

    # Ids agrupados en esta pasada (la ráfaga). Incluye `db_message_id`.
    ids_agrupados: list[UUID] = field(default_factory=list)

    @property
    def es_web(self) -> bool:
        return self.canal == "web"

    @property
    def transferida(self) -> bool:
        """La conversación está en manos de una persona: el agente calla."""
        return self.conversation_status == "transferred"

    @property
    def destino(self) -> Destino:
        """A dónde se entrega la respuesta de esta conversación."""
        return Destino(
            tenant_id=self.tenant_id,
            canal=self.canal,
            usuario_id=self.usuario_id,
            nombre=self.nombre,
            phone_number_id=self.phone_number_id,
            page_id=self.page_id,
            conversation_id=self.conversation_id,
        )


@dataclass(frozen=True)
class ResultadoIngreso:
    """
    Qué debe hacer el router con lo que devolvió `ingreso.py`.

    `respuesta_inmediata` solo se usa en el widget, que espera un cuerpo en
    la misma petición (un aviso de límite, un acuse de transferencia o el
    rechazo). Para los canales de Meta siempre es None: ahí ya se respondió
    EVENT_RECEIVED y la entrega sale por el proveedor.
    """

    contexto: ContextoMensaje | None = None
    respuesta_inmediata: str | None = None
    motivo: str | None = None
    # A dónde entregar `respuesta_inmediata` cuando el canal no es el widget.
    # Va aparte de `contexto` porque el aviso de plan agotado se manda antes
    # de que exista conversación.
    destino: Destino | None = None

    @property
    def sigue_al_agente(self) -> bool:
        return self.contexto is not None
