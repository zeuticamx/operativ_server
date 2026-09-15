"""
WebSocket en tiempo real para alertas del panel.

OJO con el nombre del archivo: no se llama `websocket.py` a propósito.
`python-engineio` (dependencia de python-socketio) hace `import websocket`
para su cliente síncrono, y como la raíz del backend está en `sys.path`, un
`websocket.py` acá arriba tapa ese paquete y rompe la importación de
`socketio` con un `ImportError` de import circular. `realtime.py` evita el
choque de nombres sin más vueltas.

python-socketio corre como una capa ASGI aparte, montada junto a FastAPI en
main.py (`socketio.ASGIApp(sio, other_asgi_app=app)`). No es un router de
FastAPI: no hay `Depends` ni `HTTPException` disponibles en los handlers de
`sio`, así que la autenticación se rehace a mano acá con las mismas piezas
que usa deps.usuario_actual (decodificar el JWT + releer portal_users), en
vez de confiar en un token que ya pudo haber sido revocado.

Cada cliente se suscribe a la "room" de su tenant (`tenant_{tenant_id}`), no
a un namespace por usuario: todo lo que se emite ahí lo ven todas las
sesiones abiertas de ese negocio, que es lo que hace falta para que gerencia
vea alertas aunque el cambio lo haya disparado otro vendedor.

Un solo proceso, sin backend de mensajería (Redis, etc): alcanza para un
despliegue de una sola instancia como el actual. Si el backend llega a
correr en más de un worker/proceso a la vez, sio.emit deja de alcanzar a
los clientes conectados a otro proceso y hace falta un
`socketio.AsyncRedisManager` — no antes.
"""

import logging
from typing import Any, Optional
from uuid import UUID

import jwt
import socketio

from config import settings
from routers.alertas import crear_alerta
from security import decodificar_token
from session import execute, fetch_all, fetch_one

logger = logging.getLogger("operativai.websocket")

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=settings.FRONTEND_ORIGINS,
)

# sid -> tenant_id. Hace falta en `disconnect`, donde socket.io ya no dice
# de qué room salía el cliente.
_conexiones: dict[str, UUID] = {}


def _sala(tenant_id: UUID) -> str:
    return f"tenant_{tenant_id}"


def _token_de(environ: dict, auth: Optional[dict]) -> Optional[str]:
    """
    Dos formas de mandar el token: `auth` (lo que manda socket.io-client en
    browser vía `io(url, { auth: { token } })`) o el header Authorization
    (para clientes que no arman ese payload, como una prueba con curl).
    """
    if isinstance(auth, dict) and auth.get("token"):
        return auth["token"]

    header = environ.get("HTTP_AUTHORIZATION", "")
    if header.startswith("Bearer "):
        return header[len("Bearer "):]

    return None


async def _usuario_desde_token(token: str) -> Optional[dict]:
    """
    Repite la validación de deps.usuario_actual sin HTTPException: acá lo
    único que se puede hacer con un token inválido es rechazar la conexión.
    """
    try:
        payload = decodificar_token(token, "access")
    except jwt.PyJWTError:
        return None

    fila = await fetch_one(
        "SELECT id, tenant_id, role, is_active FROM portal_users WHERE id = $1",
        UUID(payload["sub"]),
    )
    if fila is None or not fila["is_active"] or fila["tenant_id"] is None:
        return None

    return dict(fila)


def _serializar(fila) -> dict[str, Any]:
    return {
        "id": str(fila["id"]),
        "tenant_id": str(fila["tenant_id"]),
        "tipo": fila["tipo"],
        "titulo": fila["titulo"],
        "mensaje": fila["mensaje"],
        "datos": fila["datos"],
        "leido": fila["leido"],
        "creado_en": fila["creado_en"].isoformat(),
    }


@sio.event
async def connect(sid: str, environ: dict, auth: Optional[dict] = None) -> None:
    token = _token_de(environ, auth)
    if token is None:
        logger.warning("Conexión WS rechazada: sin token (sid=%s)", sid)
        raise socketio.exceptions.ConnectionRefusedError("Falta el token de autenticación")

    usuario = await _usuario_desde_token(token)
    if usuario is None:
        logger.warning("Conexión WS rechazada: token inválido o usuario inactivo (sid=%s)", sid)
        raise socketio.exceptions.ConnectionRefusedError("Token inválido o usuario inactivo")

    tenant_id = usuario["tenant_id"]
    _conexiones[sid] = tenant_id
    await sio.enter_room(sid, _sala(tenant_id))
    logger.info("WS conectado: sid=%s tenant=%s role=%s", sid, tenant_id, usuario["role"])

    # Lo que se perdió mientras no había sesión abierta: las últimas no
    # leídas, no el historial completo (para eso está el GET por HTTP).
    pendientes = await fetch_all(
        """
        SELECT id, tenant_id, tipo, titulo, mensaje, datos, leido, creado_en
        FROM alertas
        WHERE tenant_id = $1 AND leido = false
        ORDER BY creado_en DESC
        LIMIT 10
        """,
        tenant_id,
    )
    await sio.emit("alertas_pendientes", [_serializar(f) for f in pendientes], room=sid)


@sio.event
async def disconnect(sid: str) -> None:
    tenant_id = _conexiones.pop(sid, None)
    logger.info("WS desconectado: sid=%s tenant=%s", sid, tenant_id)


@sio.event
async def marcar_leida(sid: str, data: Optional[dict]) -> None:
    """
    Evento que manda el cliente al marcar una alerta como leída desde la UI,
    para no depender de que además haga el PATCH por HTTP. Confía en la
    room, no en lo que mande `data`: el tenant_id sale de la conexión ya
    autenticada, nunca del payload del evento.
    """
    tenant_id = _conexiones.get(sid)
    if tenant_id is None:
        return

    alerta_id = (data or {}).get("alerta_id")
    if not alerta_id:
        return

    await execute(
        "UPDATE alertas SET leido = true WHERE id = $1 AND tenant_id = $2",
        alerta_id,
        tenant_id,
    )
    await sio.emit("alerta_leida", {"alerta_id": alerta_id}, room=_sala(tenant_id))


async def broadcast_alerta(
    tenant_id: UUID,
    tipo: str,
    titulo: str,
    mensaje: str,
    datos: Optional[dict] = None,
) -> None:
    """
    Punto de entrada para el resto del backend: crea la alerta en BD y la
    transmite a todos los clientes conectados de ese tenant.

    sio.emit a una room sin nadie conectado es un no-op — no hace falta
    revisar antes si hay alguien escuchando.
    """
    alerta = await crear_alerta(tenant_id, tipo, titulo, mensaje, datos)
    await sio.emit(
        "nueva_alerta",
        {
            "id": str(alerta.id),
            "tenant_id": str(alerta.tenant_id),
            "tipo": alerta.tipo,
            "titulo": alerta.titulo,
            "mensaje": alerta.mensaje,
            "datos": alerta.datos,
            "leido": alerta.leido,
            "creado_en": alerta.creado_en.isoformat(),
        },
        room=_sala(tenant_id),
    )
