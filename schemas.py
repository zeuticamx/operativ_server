"""Schemas de entrada y salida de la API."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, model_validator


# ============================================================
# AUTH
# ============================================================
class RegistroIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = None
    nombre_negocio: str = Field(min_length=2, max_length=255)


class VerificacionPendienteOut(BaseModel):
    """
    Respuesta de /registro y /reenviar-codigo. No trae tokens: la cuenta
    todavía no existe. El frontend usa esto para pasar al paso del código.
    """
    email: EmailStr
    expira_en_minutos: int
    reenviar_en_segundos: int


class VerificarCodigoIn(BaseModel):
    email: EmailStr
    # Exactamente 6 dígitos. Se valida acá para no gastar un argon2 por
    # cada cadena cualquiera que llegue.
    codigo: str = Field(pattern=r"^\d{6}$")


class ReenviarCodigoIn(BaseModel):
    email: EmailStr


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class GoogleLoginIn(BaseModel):
    """ID token (JWT) que entrega el botón "Sign in with Google" del portal."""
    credential: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"


class RefreshIn(BaseModel):
    refresh_token: str


class UsuarioOut(BaseModel):
    id: UUID
    email: str
    full_name: str | None
    role: str
    tenant_id: UUID | None
    nombre_negocio: str | None = None
    es_gerencia_plataforma: bool = False


# ============================================================
# AGENTE
# ============================================================
class AgenteConfigOut(BaseModel):
    agent_name: str
    system_prompt: str
    model: str | None
    temperature: float
    history_window: int
    is_active: bool


class AgenteConfigIn(BaseModel):
    agent_name: str = Field(min_length=1, max_length=100)
    system_prompt: str = Field(min_length=10, max_length=20000)
    model: str | None = None
    temperature: float = Field(default=0.7, ge=0.0, le=1.0)
    history_window: int = Field(default=30, ge=5, le=200)
    is_active: bool = True


# ============================================================
# CANALES
# ============================================================
class CanalOut(BaseModel):
    channel_type: str
    page_id: str | None
    ig_user_id: str | None
    phone_number_id: str | None
    is_active: bool
    updated_at: datetime | None


class ConectarMetaIn(BaseModel):
    """El code que devuelve el popup de Facebook Login for Business."""
    code: str
    redirect_uri: str | None = None


class PaginaDisponible(BaseModel):
    page_id: str
    nombre: str
    ig_user_id: str | None = None
    ig_username: str | None = None
    ya_conectada: bool = False


class ActivarCanalesIn(BaseModel):
    page_ids: list[str] = Field(min_length=1)


class ConectarWhatsAppIn(BaseModel):
    """
    Alta del número de WhatsApp Business de un tenant.

    Mientras el proveedor sea Kontesta (WHATSAPP_PROVIDER=kontesta) la cuenta
    de Kontesta es una sola —la de OperativAI— y cada negocio es una línea
    dentro de ella, así que lo único que hay por tenant es el id de esa
    línea: no existe un secreto por tenant que cifrar.
    """
    phone_number_id: str = Field(min_length=1, max_length=100)


# ============================================================
# HERRAMIENTAS
# ============================================================
class HerramientaInfoOut(BaseModel):
    correo_servicio: str


class HerramientaOut(BaseModel):
    tool_key: str
    tool_type: str
    display_name: str
    description: str
    is_enabled: bool
    last_verified_at: datetime | None
    nombre_documento: str | None = None
    url_original: str | None = None


class ConectarGoogleSheetIn(BaseModel):
    url: str = Field(min_length=10)
    display_name: str = Field(min_length=2, max_length=255)
    description: str = Field(
        min_length=5,
        max_length=1000,
        description="Cuándo debe el agente consultar esta hoja (lo lee la IA para decidir).",
    )
    rango: str = Field(default="A:Z", max_length=100)


class ConectarGoogleDocIn(BaseModel):
    url: str = Field(min_length=10)
    display_name: str = Field(min_length=2, max_length=255)
    description: str = Field(min_length=5, max_length=1000)


class ActualizarHerramientaIn(BaseModel):
    display_name: str | None = Field(default=None, min_length=2, max_length=255)
    description: str | None = Field(default=None, min_length=5, max_length=1000)
    is_enabled: bool | None = None


# ============================================================
# CONVERSACIONES
# ============================================================
class ConversacionOut(BaseModel):
    id: UUID
    channel_type: str
    status: str
    started_at: datetime
    last_message_at: datetime
    usuario_nombre: str | None
    usuario_handle: str | None
    # Solo Instagram: Messenger y WhatsApp no exponen un @usuario.
    usuario_username: str | None = None
    total_mensajes: int
    ultimo_mensaje: str | None
    # Ventana de 24h de Meta (WhatsApp/Messenger/Instagram): fuera de ella
    # solo se puede mandar plantillas (WhatsApp) o nada (Messenger/
    # Instagram). Se cuenta desde el último mensaje del CLIENTE, no de
    # cualquiera — uno del asistente no la reinicia. None si el cliente
    # nunca escribió. Negativo si ya se cerró.
    ultimo_mensaje_cliente_at: datetime | None = None
    minutos_restantes_ventana: int | None = None


class MensajeOut(BaseModel):
    id: UUID
    role: str
    content: str
    created_at: datetime


class ConversacionDetalleOut(BaseModel):
    id: UUID
    channel_type: str
    status: str
    started_at: datetime
    usuario_nombre: str | None
    usuario_handle: str | None
    usuario_username: str | None = None
    ultimo_mensaje_cliente_at: datetime | None = None
    minutos_restantes_ventana: int | None = None
    mensajes: list[MensajeOut]


class ContactoOut(BaseModel):
    """Resultado de consultarle a Meta el perfil de un contacto."""
    usuario_nombre: str | None
    usuario_username: str | None
    actualizado: bool


class MetricasOut(BaseModel):
    conversaciones_activas: int
    mensajes_hoy: int
    mensajes_7d: int
    conversaciones_7d: int
    por_canal: dict[str, int]


# ============================================================
# SERVICIOS DEL TENANT
# ============================================================
class ServiciosOut(BaseModel):
    tenant_id: UUID
    agente_ia_activo: bool
    gestion_vendedores_activo: bool


class ServiciosIn(BaseModel):
    """Parcial: lo que no venga se deja como está."""
    agente_ia_activo: bool | None = None
    gestion_vendedores_activo: bool | None = None


# ============================================================
# VENDEDORES
# ============================================================
class VendedorCrearIn(BaseModel):
    nombre: str = Field(min_length=2, max_length=255)
    telefono: str | None = Field(default=None, max_length=50)
    # Opcional: enlaza al vendedor con una cuenta del portal para que pueda
    # entrar a ver su cartera. Sin esto solo existe como destinatario de leads.
    portal_user_id: UUID | None = None
    # Solo lo usa un superadmin dando de alta en nombre de otro negocio. Un
    # dueño normal lo deja vacío y se toma el tenant de su token.
    tenant_id: UUID | None = None


class VendedorActualizarIn(BaseModel):
    nombre: str | None = Field(default=None, min_length=2, max_length=255)
    telefono: str | None = Field(default=None, max_length=50)
    activo: bool | None = None


class VendedorOut(BaseModel):
    id: UUID
    tenant_id: UUID
    portal_user_id: UUID | None
    nombre: str
    telefono: str | None
    activo: bool
    creado_en: datetime
    # Leads en estados no cerrados. Es la carga que mira la asignación.
    clientes_activos: int = 0


class ReasignacionOut(BaseModel):
    """Resultado de POST /vendedores/{id}/reasignar-pendientes."""
    vendedor_id: UUID
    estrategia: str
    reasignados: int
    # Leads que no se pudieron mover porque no quedaba ningún otro vendedor
    # activo. Siguen con el vendedor original, no se quedan huérfanos.
    sin_destino: int
    destinos: dict[UUID, int] = Field(default_factory=dict)


# ============================================================
# PIPELINE
# ============================================================
class AsignarClienteIn(BaseModel):
    # None = que decida la estrategia del tenant.
    vendedor_id: UUID | None = None
    nota: str | None = Field(default=None, max_length=1000)


class CambiarEstadoIn(BaseModel):
    estado: str = Field(min_length=1, max_length=50)
    nota: str | None = Field(default=None, max_length=1000)
    monto_estimado: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    # Solo tiene sentido al pasar a 'perdido'. Al reabrir el lead se limpia.
    motivo_perdida: str | None = Field(default=None, max_length=1000)


class CrearClienteIn(BaseModel):
    tenant_id: UUID
    nombre: str = Field(min_length=1, max_length=255)
    handle: str | None = Field(default=None, max_length=255)
    canal: str = Field(default="otro", max_length=50)
    monto_estimado: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    nota: str | None = Field(default=None, max_length=1000)


class PipelineOut(BaseModel):
    id: UUID
    user_id: UUID
    cliente_nombre: str | None
    cliente_handle: str | None
    vendedor_id: UUID | None
    vendedor_nombre: str | None
    estado: str
    monto_estimado: Decimal | None
    motivo_perdida: str | None
    actualizado_en: datetime
    # A dónde puede moverse desde acá. El frontend arma el selector con esto
    # en vez de repetir la máquina de estados.
    transiciones_posibles: list[str] = Field(default_factory=list)


class HistorialOut(BaseModel):
    estado_anterior: str | None
    estado_nuevo: str
    vendedor_id: UUID | None
    vendedor_nombre: str | None
    nota: str | None
    creado_en: datetime


class HistorialVendedorOut(BaseModel):
    """Un movimiento de la bitácora del vendedor, con el cliente al que le pasó.

    A diferencia de `HistorialOut` (línea de tiempo de UN cliente), esto
    junta la bitácora de TODOS los clientes que ha tenido un vendedor, así
    que necesita decir de quién es cada fila.
    """

    user_id: UUID
    cliente_nombre: str | None
    cliente_handle: str | None
    estado_anterior: str | None
    estado_nuevo: str
    nota: str | None
    creado_en: datetime


# ============================================================
# CONFIG DE ASIGNACIÓN
# ============================================================
class ConfigAsignacionOut(BaseModel):
    tenant_id: UUID
    estrategia_asignacion: Literal["carga", "round_robin", "manual"]
    ultimo_vendedor_asignado_id: UUID | None


class ConfigAsignacionIn(BaseModel):
    estrategia_asignacion: Literal["carga", "round_robin", "manual"]


# ============================================================
# CONFIGURACIÓN DE ETAPAS DEL EMBUDO
# ============================================================
# Panel del dueño: NO es el motor que valida el embudo real (eso sigue
# siendo services/pipeline_estados.py). Ver comentario en
# sql/08_pipeline_config.sql.
class PipelineEtapaCrearIn(BaseModel):
    tenant_id: UUID
    nombre: str = Field(min_length=1, max_length=50)
    color: str = Field(default="#3987E5", pattern=r"^#[0-9A-Fa-f]{6}$")
    descripcion: str | None = Field(default=None, max_length=500)
    orden: int = 0


class PipelineEtapaActualizarIn(BaseModel):
    nombre: str | None = Field(default=None, min_length=1, max_length=50)
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    descripcion: str | None = Field(default=None, max_length=500)
    orden: int | None = None


class PipelineEtapaOut(BaseModel):
    id: UUID
    tenant_id: UUID
    nombre: str
    color: str
    descripcion: str | None
    orden: int
    creado_en: datetime
    # Leads del embudo real (client_pipeline) cuyo `estado` coincide con
    # este nombre y no está cerrado. Es de solo lectura: avisa antes de
    # borrar una etapa que corresponde a una etapa real en uso, aunque
    # esta tabla no sea la que controla esa etapa real.
    leads_activos: int = 0


class PipelineTransicionIn(BaseModel):
    tenant_id: UUID
    etapa_origen_id: UUID
    etapa_destino_id: UUID
    permitida: bool = True


class PipelineTransicionOut(BaseModel):
    id: UUID
    tenant_id: UUID
    etapa_origen_id: UUID
    etapa_origen_nombre: str
    etapa_destino_id: UUID
    etapa_destino_nombre: str
    permitida: bool


class PipelineConfigOut(BaseModel):
    etapas: list[PipelineEtapaOut]
    transiciones: list[PipelineTransicionOut]


# ============================================================
# MÉTRICAS DEL EMBUDO
# ============================================================
class MetricaEtapaOut(BaseModel):
    estado: str
    total: int
    # Sobre el total de leads del tenant.
    porcentaje: float
    # Cuánto tarda en promedio un lead en salir de esta etapa. None si
    # todavía no hay ninguno que la haya atravesado entera.
    horas_promedio: float | None


class RankingVendedorOut(BaseModel):
    vendedor_id: UUID
    nombre: str
    activo: bool
    abiertos: int
    ganados: int
    perdidos: int
    monto_ganado: Decimal
    # ganados / (ganados + perdidos). None si no cerró nada todavía —
    # distinto de 0.0, que sería "cerró y perdió todo".
    tasa_cierre: float | None


class MetricasPipelineOut(BaseModel):
    total_clientes: int
    etapas: list[MetricaEtapaOut]
    tasa_conversion_global: float | None
    ranking: list[RankingVendedorOut]


class TendenciaMesOut(BaseModel):
    # "YYYY-MM". String y no `date`: es una etiqueta de periodo, no un
    # instante — el día 1 no significa nada por sí solo.
    periodo: str
    # Leads dados de alta en el mes (primer renglón de su bitácora).
    nuevos: int
    ganados: int
    perdidos: int
    monto_ganado: Decimal


class TendenciaPipelineOut(BaseModel):
    meses: list[TendenciaMesOut]


class ConversionEtapaOut(BaseModel):
    estado: str
    # Leads del rango que pasaron por esta etapa alguna vez (no los que
    # están ahí ahora mismo — eso ya lo cubre MetricaEtapaOut).
    alcanzados: int
    # Sobre el total de leads del rango. 'nuevo' es siempre 100.
    porcentaje: float


class ConversionPipelineOut(BaseModel):
    desde: datetime | None
    hasta: datetime | None
    total_leads: int
    etapas: list[ConversionEtapaOut]


# ============================================================
# CRM DE CAMPO — CLIENTES
# ============================================================
# Marcas de tiempo en español (creado_en / actualizado_en / completado_en),
# igual que las tablas. No hay `created_at` en ningún lado de este módulo.

EstadoCliente = Literal["prospecto", "activo", "inactivo", "perdido"]
PrioridadCliente = Literal["alta", "media", "baja"]

# Rangos geográficos. Se acotan acá para que una coordenada imposible sea
# un 422 de validación y no llegue nunca al cálculo de distancia.
_LAT = Field(ge=-90, le=90)
_LON = Field(ge=-180, le=180)


class ClienteCrearIn(BaseModel):
    nombre_negocio: str = Field(min_length=2, max_length=255)
    latitud: float = _LAT
    longitud: float = _LON
    # Sin vendedor, el cliente queda en la cartera del negocio hasta que
    # gerencia decida a quién le toca.
    vendedor_id: UUID | None = None
    contacto_nombre: str | None = Field(default=None, max_length=255)
    telefono: str | None = Field(default=None, max_length=50)
    direccion: str | None = Field(default=None, max_length=500)
    radio_tolerancia_metros: int = Field(default=120, gt=0, le=5000)
    estado: EstadoCliente = "prospecto"
    prioridad: PrioridadCliente = "media"
    notas: str | None = Field(default=None, max_length=5000)


class ClienteActualizarIn(BaseModel):
    """PUT con semántica parcial: lo que no venga se deja como está."""

    nombre_negocio: str | None = Field(default=None, min_length=2, max_length=255)
    latitud: float | None = Field(default=None, ge=-90, le=90)
    longitud: float | None = Field(default=None, ge=-180, le=180)
    vendedor_id: UUID | None = None
    contacto_nombre: str | None = Field(default=None, max_length=255)
    telefono: str | None = Field(default=None, max_length=50)
    direccion: str | None = Field(default=None, max_length=500)
    radio_tolerancia_metros: int | None = Field(default=None, gt=0, le=5000)
    estado: EstadoCliente | None = None
    prioridad: PrioridadCliente | None = None
    notas: str | None = Field(default=None, max_length=5000)


class ClienteOut(BaseModel):
    id: UUID
    tenant_id: UUID
    vendedor_id: UUID | None
    vendedor_nombre: str | None = None
    nombre_negocio: str
    contacto_nombre: str | None
    telefono: str | None
    direccion: str | None
    latitud: float
    longitud: float
    radio_tolerancia_metros: int
    estado: str
    prioridad: str
    notas: str | None
    creado_en: datetime
    actualizado_en: datetime


# ============================================================
# CRM DE CAMPO — VISITAS
# ============================================================
class CheckinIn(BaseModel):
    """
    Un check-in. El vendedor y el tenant NO vienen acá: salen del JWT.
    """

    cliente_id: UUID
    latitud: float = _LAT
    longitud: float = _LON
    # Precisión que reportó el GPS. Se guarda como dato, no decide nada.
    accuracy_metros: float | None = Field(default=None, ge=0, le=100_000)
    foto_url: str | None = Field(default=None, max_length=1000)
    comentario: str | None = Field(default=None, max_length=2000)
    timestamp_dispositivo: datetime | None = None
    # Lo genera la app. Opcional en línea; obligatorio al sincronizar
    # (ver CheckinSyncIn), que es donde hace de llave de idempotencia.
    cliente_uuid_offline: UUID | None = None


class CheckinSyncIn(CheckinIn):
    """Un check-in dentro de un lote de sincronización."""

    # Sin esto no hay forma de saber si el elemento ya se procesó, y
    # reintentar el lote duplicaría visitas.
    cliente_uuid_offline: UUID


class SyncIn(BaseModel):
    # Tope por lote: un POST gigantesco de una app que estuvo semanas sin
    # señal se corta acá en vez de morir por timeout a media transacción.
    visitas: list[CheckinSyncIn] = Field(min_length=1, max_length=200)


class VisitaOut(BaseModel):
    id: UUID
    tenant_id: UUID
    vendedor_id: UUID
    vendedor_nombre: str | None = None
    cliente_id: UUID
    cliente_nombre_negocio: str | None = None
    latitud: float
    longitud: float
    accuracy_metros: float | None
    distancia_calculada_metros: float
    dentro_de_geocerca: bool
    foto_url: str | None
    comentario: str | None
    timestamp_dispositivo: datetime | None
    timestamp_servidor: datetime
    cliente_uuid_offline: UUID | None
    creado_en: datetime


class CheckinOut(BaseModel):
    """
    Respuesta del check-in: la visita más el veredicto de la geocerca.

    Un check-in fuera de la geocerca se guarda igual y responde 201: no es
    un error del cliente, es un hecho que gerencia necesita ver. Lo que
    cambia es `dentro_de_geocerca`.
    """

    visita: VisitaOut
    dentro_de_geocerca: bool
    distancia_metros: float
    radio_metros: int
    # Cuánto se pasó del radio. 0 si quedó dentro.
    exceso_metros: float
    mensaje: str
    # True si esta visita ya existía y se devolvió la guardada (solo puede
    # pasar cuando se reenvía un cliente_uuid_offline ya procesado).
    duplicada: bool = False


class SyncResultado(BaseModel):
    cliente_uuid_offline: UUID
    # null cuando el elemento se rechazó (`error` explica por qué).
    visita_id: UUID | None = None
    aceptada: bool
    # True si ya se había procesado antes: no se creó nada nuevo.
    duplicada: bool = False
    dentro_de_geocerca: bool | None = None
    distancia_metros: float | None = None
    error: str | None = None


class SyncOut(BaseModel):
    """
    Resumen del lote. Nunca es 4xx aunque haya elementos rechazados: el
    resultado va por elemento para que la app sepa cuáles borrar de su
    cola local y cuáles conservar.
    """

    recibidas: int
    creadas: int
    duplicadas: int
    rechazadas: int
    resultados: list[SyncResultado]


# ============================================================
# CRM DE CAMPO — TAREAS DE SEGUIMIENTO
# ============================================================
EstadoTarea = Literal["pendiente", "completada", "vencida"]


class TareaCrearIn(BaseModel):
    cliente_id: UUID
    titulo: str = Field(min_length=2, max_length=255)
    fecha_programada: datetime
    descripcion: str | None = Field(default=None, max_length=5000)
    # Solo gerencia puede mandarlo para asignar la tarea a otro. Un
    # vendedor lo deja vacío y la tarea es suya.
    vendedor_id: UUID | None = None


class TareaActualizarIn(BaseModel):
    titulo: str | None = Field(default=None, min_length=2, max_length=255)
    descripcion: str | None = Field(default=None, max_length=5000)
    fecha_programada: datetime | None = None
    # 'completada' no se pone por acá: usa POST /tareas/{id}/completar, que
    # es lo que sella completado_en de forma coherente.
    estado: Literal["pendiente", "vencida"] | None = None
    vendedor_id: UUID | None = None


class TareaOut(BaseModel):
    id: UUID
    tenant_id: UUID
    vendedor_id: UUID
    vendedor_nombre: str | None = None
    cliente_id: UUID
    cliente_nombre_negocio: str | None = None
    titulo: str
    descripcion: str | None
    fecha_programada: datetime
    estado: str
    completado_en: datetime | None
    creado_en: datetime


# ============================================================
# CRM DE CAMPO — REPORTES
# ============================================================
class ActividadVendedorOut(BaseModel):
    vendedor_id: UUID
    nombre: str
    activo: bool
    visitas: int
    # Las que cayeron dentro del radio del cliente.
    visitas_validadas: int
    visitas_fuera_geocerca: int
    # Clientes distintos visitados, no visitas totales.
    clientes_visitados: int
    tareas_completadas: int
    tareas_pendientes: int
    # visitas_validadas / visitas, 0-100. None si no hubo visitas —
    # distinto de 0.0, que sería "fue a todas y ninguna contó".
    porcentaje_validadas: float | None


class ReporteActividadOut(BaseModel):
    desde: datetime
    hasta: datetime
    total_visitas: int
    total_tareas_completadas: int
    vendedores: list[ActividadVendedorOut]

# ============================================================
# EVENTOS (los llama n8n, no el frontend)
# ============================================================
class MensajeEntranteIn(BaseModel):
    tenant_id: UUID
    user_id: UUID
    # El mensaje crudo tal como lo tenga n8n. No se valida su forma: este
    # endpoint no lo interpreta, solo lo pasa al aviso del vendedor.
    mensaje: dict = Field(default_factory=dict)


class MensajeEntranteOut(BaseModel):
    # Los dos flags que n8n necesita para decidir en su propio workflow si
    # sigue hacia el nodo del agente. La decisión vive allá, no acá.
    gestion_vendedores_activo: bool
    agente_ia_activo: bool
    # Informativos, para depurar desde n8n.
    pipeline_id: UUID | None = None
    vendedor_id: UUID | None = None
    asignado_ahora: bool = False

from enum import Enum
from uuid import UUID
from datetime import datetime
from typing import Optional, Dict, Any

class TipoAlerta(str, Enum):
    nuevo_lead = "nuevo_lead"
    cambio_etapa = "cambio_etapa"
    sin_actividad = "sin_actividad"
    cuota_excedida = "cuota_excedida"
    cierre = "cierre"

class AlertaOut(BaseModel):
    id: UUID
    tenant_id: UUID
    tipo: TipoAlerta
    titulo: str
    mensaje: str
    datos: Optional[Dict[str, Any]] = None
    leido: bool
    creado_en: datetime
    
    class Config:
        from_attributes = True

# ============================================================
# PAGOS (Stripe; Mercado Pago inhabilitado, ver PAYMENT_PROVIDER)
# ============================================================
# `monto`, `precio` y `creditos` viajan como Decimal: FastAPI los serializa
# a string en el JSON, igual que monto_ganado del embudo. El front los
# formatea con formatoMonto(), que ya espera string.

TipoPago = Literal["subscription", "credit_purchase"]
EstadoPago = Literal["pendiente", "aprobado", "rechazado", "cancelado", "reembolsado"]
EstadoSuscripcion = Literal["activa", "pausada", "cancelada"]
NombrePlan = Literal["starter", "pro", "enterprise"]
ProveedorPago = Literal["stripe", "mercadopago"]


class CrearPagoIn(BaseModel):
    tipo: TipoPago
    # Cuál de los dos aplica depende de `tipo`; lo cruza el validador de
    # abajo. No se acepta el monto desde el cliente a propósito: el precio
    # sale siempre de las tablas `planes`/`paquetes_creditos`, porque si no
    # cualquiera podría contratar enterprise por un peso.
    plan: NombrePlan | None = None
    creditos: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)

    @model_validator(mode="after")
    def _coherente_con_el_tipo(self) -> "CrearPagoIn":
        if self.tipo == "subscription":
            if self.plan is None:
                raise ValueError("Para una suscripción hace falta 'plan'")
            if self.creditos is not None:
                raise ValueError("'creditos' no aplica a una suscripción")
        else:
            if self.creditos is None:
                raise ValueError("Para comprar créditos hace falta 'creditos'")
            if self.plan is not None:
                raise ValueError("'plan' no aplica a una compra de créditos")
        return self


class CrearPagoOut(BaseModel):
    """
    Lo que necesita el portal para mandar al comprador a pagar.

    Los campos son neutros a propósito: con Stripe `referencia` es la
    Checkout Session (cs_...) y con Mercado Pago la preferencia. El front
    no tiene por qué saber cuál de las dos pasarelas está activa — solo
    redirige a `checkout_url`.
    """

    transaccion_id: UUID
    proveedor: ProveedorPago
    referencia: str
    checkout_url: str
    monto: Decimal
    concepto: str


class PlanOut(BaseModel):
    nombre: str
    descripcion: str | None
    precio_monthly: Decimal
    precio_annual: Decimal | None
    # None = sin tope (enterprise).
    max_vendedores: int | None
    max_leads_mensuales: int | None
    creditos_incluidos_mensual: Decimal
    agente_ia_activo: bool
    gestion_vendedores_activo: bool


class PaqueteCreditosOut(BaseModel):
    creditos: Decimal
    precio: Decimal


class CatalogoPagosOut(BaseModel):
    """Lo que el portal necesita para pintar la pantalla de suscripción."""

    planes: list[PlanOut]
    paquetes: list[PaqueteCreditosOut]


class TransaccionOut(BaseModel):
    id: UUID
    tipo: str
    concepto: str | None
    monto: Decimal
    estado_pago: EstadoPago
    metodo_pago: str | None
    ultimos_4_digitos: str | None
    creado_en: datetime


class CheckoutEstadoOut(BaseModel):
    """Cómo quedó un checkout concreto; lo consulta la pantalla de retorno."""

    id: UUID
    tipo: str
    monto: Decimal
    estado: EstadoPago
    fecha: datetime


class SuscripcionOut(BaseModel):
    """
    Estado de cobros del tenant. Todo nullable salvo los créditos: un
    tenant que nunca pagó no tiene fila en tenant_subscriptions, y eso no
    es un error — es el estado inicial.
    """

    plan: NombrePlan | None
    estado_suscripcion: EstadoSuscripcion | None
    fecha_renovacion: datetime | None
    precio_monthly: Decimal | None
    creditos_disponibles: Decimal
    creditos_gastados: Decimal


# ============================================================
# GERENCIA DE PLATAFORMA
# ============================================================
# Todo lo de acá es del nivel gerencia (tabla gerencia_users), no del rol
# owner/superadmin de un tenant: son dos cosas distintas, ver deps.py.
# Nada de esta sección se filtra por tenant — el punto es ver todos.

EstadoTenantPlataforma = Literal["activo", "prueba", "suspendido", "baja"]
OrigenTokens = Literal["agente", "herramienta", "resumen", "otro"]


class UsoTokensIn(BaseModel):
    """
    Lo que reporta n8n después de cada llamada al modelo.

    `idempotency_key` es opcional pero muy recomendable: con ella, un nodo
    de n8n que se reintenta no cuenta el consumo dos veces. Lo natural es
    mandar el id de la ejecución más el del nodo.
    """

    tenant_id: UUID
    conversation_id: UUID | None = None
    origen: OrigenTokens = "agente"
    modelo: str | None = Field(default=None, max_length=100)
    tokens_entrada: int = Field(default=0, ge=0)
    tokens_salida: int = Field(default=0, ge=0)
    costo_usd: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=6)
    idempotency_key: str | None = Field(default=None, max_length=120)


class UsoTokensOut(BaseModel):
    registrado: bool
    # False cuando la idempotency_key ya estaba: no es un error, es el
    # candado haciendo su trabajo. n8n no tiene que reintentar.
    duplicado: bool = False


class ConsumoTenantOut(BaseModel):
    """Consumo de un tenant dentro del rango consultado."""

    tokens_entrada: int
    tokens_salida: int
    tokens_total: int
    costo_usd: Decimal
    llamadas: int


class TenantGerenciaOut(BaseModel):
    tenant_id: UUID
    nombre: str
    alta: datetime | None

    estado: EstadoTenantPlataforma
    estado_motivo: str | None
    estado_actualizado_en: datetime | None
    estado_actualizado_por: str | None

    agente_ia_activo: bool
    gestion_vendedores_activo: bool
    # El efectivo, ya cruzado con pagos y suspensión: es lo que de verdad
    # responde n8n. Puede diferir de `agente_ia_activo`, y esa diferencia es
    # justo lo que gerencia necesita ver.
    agente_operando: bool

    plan: NombrePlan | None
    estado_suscripcion: EstadoSuscripcion | None
    fecha_renovacion: datetime | None
    precio_monthly: Decimal | None
    creditos_disponibles: Decimal
    creditos_gastados: Decimal

    usuarios_portal: int
    vendedores_activos: int
    canales_activos: int
    ultimo_mensaje: datetime | None

    consumo: ConsumoTenantOut


class TenantsGerenciaOut(BaseModel):
    total: int
    dias: int
    items: list[TenantGerenciaOut]


class PuntoConsumoOut(BaseModel):
    """Un día de la serie. `dia` en ISO (YYYY-MM-DD), en UTC."""

    dia: date
    tokens_total: int
    costo_usd: Decimal
    llamadas: int


class ConsumoModeloOut(BaseModel):
    modelo: str
    tokens_total: int
    costo_usd: Decimal
    llamadas: int


class ConsumoOut(BaseModel):
    desde: datetime
    hasta: datetime
    total: ConsumoTenantOut
    por_dia: list[PuntoConsumoOut]
    por_modelo: list[ConsumoModeloOut]


class ResumenGerenciaOut(BaseModel):
    """Los números de la portada del panel de plataforma."""

    dias: int

    tenants_total: int
    tenants_activos: int
    tenants_prueba: int
    tenants_suspendidos: int
    tenants_baja: int
    # Alta dentro de la ventana consultada.
    tenants_nuevos: int
    # Con al menos un mensaje en la ventana. "Activos" comercialmente no es
    # lo mismo que activos de verdad, y la brecha entre los dos números es
    # el dato que importa.
    tenants_con_actividad: int
    # Suscripción activa pero cero mensajes en la ventana: pagan y no usan.
    tenants_en_riesgo: int

    suscripciones_activas: int
    mrr: Decimal

    mensajes: int
    consumo: ConsumoTenantOut
    # Ingreso cobrado (transacciones aprobadas) dentro de la ventana.
    ingresos_periodo: Decimal


class CambiarEstadoTenantIn(BaseModel):
    estado: EstadoTenantPlataforma
    # Obligatorio si el estado no es 'activo': lo valida el router, que es
    # quien puede devolver un 400 con un mensaje decente.
    motivo: str | None = Field(default=None, max_length=1000)
    notas: str | None = Field(default=None, max_length=2000)


class CambiarServiciosTenantIn(BaseModel):
    """
    Los dos interruptores de tenant_servicios. None = no tocar, para poder
    apagar uno sin tener que mandar el estado del otro.
    """

    agente_ia_activo: bool | None = None
    gestion_vendedores_activo: bool | None = None
    motivo: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def al_menos_uno(self):
        if self.agente_ia_activo is None and self.gestion_vendedores_activo is None:
            raise ValueError("Hay que indicar al menos un servicio para cambiar")
        return self


class AjusteCreditosIn(BaseModel):
    """
    Regalo o descuento manual de créditos. Positivo suma, negativo resta —
    mismo criterio de signo que credit_transactions.cantidad.
    """

    cantidad: Decimal = Field(max_digits=12, decimal_places=2)
    motivo: str = Field(min_length=3, max_length=500)

    @model_validator(mode="after")
    def no_cero(self):
        if self.cantidad == 0:
            raise ValueError("Un ajuste de cero créditos no hace nada")
        return self


class AjusteCreditosOut(BaseModel):
    creditos_disponibles: Decimal


class EntradaAuditoriaOut(BaseModel):
    id: UUID
    actor_email: str
    accion: str
    tenant_id: UUID | None
    # Se resuelve al vuelo contra tenants: la bitácora guarda solo el UUID
    # para que el registro sobreviva al borrado del negocio.
    tenant_nombre: str | None
    detalle: dict
    creado_en: datetime
