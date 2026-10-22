"""
Envío de los avisos de cita desde el número principal del propio negocio (spec 007, D2/D6).

Nunca usa la cuenta global de spec 004 (`cuenta_global_wa`): el aviso sale por el
número principal ACTIVO del tenant, con plantillas del tenant.

- Decisión pura (sin DB ni red): `elegir_principal`, `coincide_credencial`,
  `motivo_de_error`.
- Capa DB/proveedor detrás de un objeto `acceso` inyectable (`AccesoEnvioBD` en
  producción); los tests usan uno falso, sin red ni base.

D6: el envío (y aceptar/activar la función) exige que la credencial del canal
(`get_channel_credentials`) corresponda, con el mismo `phone_number_id`, al principal
activo. Si no coinciden, no hay emisor: `sin_numero_emisor`, sin enviar nada.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from config import settings
from services import avisos_cita_reglas as reglas
from services.whatsapp import NeuroApiProvider, ProveedorWhatsAppError
from session import fetch_all, fetch_one

log = logging.getLogger("operativai.avisos_cita_envio")

IDIOMA_PLANTILLAS = "es_MX"
CANAL_WHATSAPP = "whatsapp"
BSP_ENVIO = "neuroapi"

# Mismos valores que `avisos_cita` (spec 004). Se repiten aquí para no importar
# `avisos_cita` (que lo importa desde T5a).
SIN_NUMERO_EMISOR = reglas.SIN_NUMERO_EMISOR
ERROR_PROVEEDOR = "error_proveedor"
PLANTILLA_NO_DISPONIBLE = "plantilla_no_disponible"
# Errores de NeuroAPI que significan "la plantilla no se pudo usar" (plan 2.9 de spec 004).
STATUS_PLANTILLA_NO_DISPONIBLE = (400, 500)

_BLANCO = re.compile(r"^\s*$")


# ------------------------------------------------------------
# Lógica pura
# ------------------------------------------------------------
def config_completa(fila: dict[str, Any]) -> bool:
    """Igual que `configuracion_incompleta` de sql/56: nombre Y prompt no en blanco."""
    return not (
        _BLANCO.match(fila.get("agent_name") or "") or _BLANCO.match(fila.get("system_prompt") or "")
    )


def elegir_principal(filas: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    Principal activo del negocio entre las filas de `agentes_canal` (D2): `es_principal`,
    `estado='activo'`, configuración completa, con `phone_number_id` y que no sea la
    línea de avisos. Sin ninguno -> None (pausado/desvinculado/incompleto = sin emisor).
    """
    for fila in filas:
        if (
            fila.get("es_principal")
            and fila.get("estado") == "activo"
            and not fila.get("es_linea_avisos")
            and (fila.get("phone_number_id") or "").strip()
            and config_completa(fila)
        ):
            return fila
    return None


def coincide_credencial(
    principal: dict[str, Any] | None, credencial: dict[str, Any] | None
) -> bool:
    """
    D6: la credencial del canal es la del principal (mismo `phone_number_id`, sin
    espacios) y es de NeuroAPI (el único proveedor por el que enviamos plantillas).
    """
    if principal is None or credencial is None:
        return False
    if (credencial.get("bsp_provider") or "") != BSP_ENVIO:
        return False
    if (principal.get("bsp_provider") or BSP_ENVIO) != BSP_ENVIO:
        return False
    id_principal = (principal.get("phone_number_id") or "").strip()
    id_credencial = (credencial.get("phone_number_id") or "").strip()
    return bool(id_principal) and id_principal == id_credencial


def motivo_de_error(status_code: int | None) -> str:
    """Error del proveedor -> motivo del aviso (400/500 = plantilla no usable; resto, genérico)."""
    return PLANTILLA_NO_DISPONIBLE if status_code in STATUS_PLANTILLA_NO_DISPONIBLE else ERROR_PROVEEDOR


@dataclass(frozen=True)
class ResultadoEnvioTenant:
    """
    Resultado de `enviar_plantilla_tenant`. Nunca se lanza por un fallo del proveedor.
    `motivo` es None si salió; si no: `sin_numero_emisor` (no se envió nada),
    `plantilla_no_disponible` o `error_proveedor`. `cuerpo` es el crudo del proveedor
    (solo para leer el código de Meta; nunca se muestra al usuario).
    """

    enviado: bool
    motivo: str | None = None
    id_mensaje: str | None = None
    status_code: int | None = None
    cuerpo: str | None = field(default=None, repr=False)


# ------------------------------------------------------------
# Capa de acceso (inyectable)
# ------------------------------------------------------------
class AccesoEnvio(Protocol):
    async def filas_principal(self, tenant_id: UUID) -> list[dict[str, Any]]: ...
    async def credencial(self, tenant_id: UUID) -> dict[str, Any] | None: ...
    def proveedor(self, credencial: dict[str, Any]) -> Any: ...


class AccesoEnvioBD:
    """Implementación real: asyncpg sin ORM y `NeuroApiProvider` de la credencial ya validada."""

    async def filas_principal(self, tenant_id: UUID) -> list[dict[str, Any]]:
        filas = await fetch_all(
            """
            SELECT id, phone_number_id, telefono_visible, bsp_provider, estado,
                   es_principal, es_linea_avisos, agent_name, system_prompt
            FROM agentes_canal
            WHERE tenant_id = $1 AND es_principal
            """,
            tenant_id,
        )
        return [dict(f) for f in filas]

    async def credencial(self, tenant_id: UUID) -> dict[str, Any] | None:
        fila = await fetch_one(
            "SELECT * FROM get_channel_credentials($1, $2)", tenant_id, CANAL_WHATSAPP
        )
        return dict(fila) if fila else None

    def proveedor(self, credencial: dict[str, Any]) -> NeuroApiProvider:
        api_key = credencial.get("access_token") or settings.NEUROAPI_API_KEY
        phone_number_id = (credencial.get("phone_number_id") or "").strip()
        if not api_key or not phone_number_id:
            raise ProveedorWhatsAppError("La línea de WhatsApp del negocio está incompleta")
        return NeuroApiProvider(
            api_key=api_key,
            webhook_secret="",
            base_url=settings.NEUROAPI_API_BASE_URL,
            phone_number_id=phone_number_id,
        )


# ------------------------------------------------------------
# API
# ------------------------------------------------------------
async def numero_emisor(
    tenant_id: UUID, *, acceso: AccesoEnvio | None = None
) -> dict[str, Any] | None:
    """Principal activo del negocio (fila de `agentes_canal`), o None si no hay (D2)."""
    a = acceso or AccesoEnvioBD()
    return elegir_principal(await a.filas_principal(tenant_id))


async def _emisor_y_credencial(
    a: AccesoEnvio, tenant_id: UUID
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    principal = await numero_emisor(tenant_id, acceso=a)
    if principal is None:
        return None
    credencial = await a.credencial(tenant_id)
    if not coincide_credencial(principal, credencial):
        return None
    assert credencial is not None
    return principal, credencial


async def emisor_ok(tenant_id: UUID, *, acceso: AccesoEnvio | None = None) -> bool:
    """D6: hay principal activo y la credencial del canal es la suya."""
    a = acceso or AccesoEnvioBD()
    return await _emisor_y_credencial(a, tenant_id) is not None


async def enviar_plantilla_tenant(
    tenant_id: UUID,
    telefono: str,
    nombre_plantilla: str,
    parametros: list[dict[str, Any]],
    *,
    idioma: str = IDIOMA_PLANTILLAS,
    acceso: AccesoEnvio | None = None,
) -> ResultadoEnvioTenant:
    """
    Envía la plantilla del negocio (`parametros` = `template.components[]` de
    `avisos_cita_reglas.parametros_de_envio`) desde su principal activo coincidente.
    Sin emisor -> `sin_numero_emisor` sin llamar al proveedor. Un solo intento.
    """
    a = acceso or AccesoEnvioBD()
    emisor = await _emisor_y_credencial(a, tenant_id)
    if emisor is None:
        return ResultadoEnvioTenant(False, SIN_NUMERO_EMISOR)
    _, credencial = emisor
    try:
        proveedor = a.proveedor(credencial)
        res = await proveedor.enviar_plantilla_con_componentes(
            telefono, nombre_plantilla, idioma, parametros
        )
    except ProveedorWhatsAppError as exc:
        motivo = motivo_de_error(exc.status_code)
        log.warning("Aviso de cita (%s) no enviado: %s [status=%s]", tenant_id, motivo, exc.status_code)
        return ResultadoEnvioTenant(False, motivo, None, exc.status_code, exc.cuerpo)
    except Exception:
        log.exception("Aviso de cita (%s): error inesperado al enviar", tenant_id)
        return ResultadoEnvioTenant(False, ERROR_PROVEEDOR)
    return ResultadoEnvioTenant(True, None, res.id_mensaje)
