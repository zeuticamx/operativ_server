"""Schemas de entrada y salida de la API."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


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
