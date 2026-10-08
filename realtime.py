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

Además cada cliente entra a una room propia (`usuario_{id}`) para las
alertas PERSONALES (alertas.portal_user_id, p. ej. el recordatorio de
perfil incompleto): esas no van a la room del tenant, así que ni los
compañeros ni gerencia de plataforma mirando con `ver_tenant` las reciben.

Un solo proceso, sin backend de mensajería (Redis, etc): alcanza para un
despliegue de una sola instancia como el actual. Si el backend llega a
correr en más de un worker/proceso a la vez, sio.emit deja de alcanzar a
los clientes conectados a otro proceso y hace falta un
`socketio.AsyncRedisManager` — no antes.
"""

import logging
from typing import Any, Iterable, Optional
from uuid import UUID

import jwt
import socketio

from config import settings
from deps import ROLES_NEGOCIO
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
# sid -> portal_user_id, para filtrar las alertas personales al marcarlas.
_usuarios: dict[str, UUID] = {}
# sids de sesiones "ver como": reciben alertas, no pueden marcarlas leídas.
_solo_lectura: set[str] = set()
# sids de gerencia de plataforma (tabla gerencia_users): son los únicos que
# pueden pedir, con `ver_tenant`, sumarse a la room de un tenant que no es
# el suyo — ver el panel de /gerencia/conversaciones.
_gerencia_plataforma: set[str] = set()
# sids de vendedores: no están en la room del negocio, así que tampoco
# pueden marcar leídas las alertas del negocio, solo las suyas.
_solo_personales: set[str] = set()


def _sala(tenant_id: UUID) -> str:
    return f"tenant_{tenant_id}"


def _sala_usuario(portal_user_id: UUID) -> str:
    return f"usuario_{portal_user_id}"


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
        """
        SELECT pu.id, pu.tenant_id, pu.role, pu.is_active,
               (gu.id IS NOT NULL) AS es_gerencia_plataforma
        FROM portal_users pu
        LEFT JOIN gerencia_users gu ON LOWER(gu.email) = LOWER(pu.email)
        WHERE pu.id = $1
        """,
        UUID(payload["sub"]),
    )
    # tenant_id nulo se acepta solo para gerencia de plataforma (staff de
    # OperativAI sin negocio propio en el portal): no tienen room de origen,
    # pero igual necesitan la conexión para pedir `ver_tenant` sobre el
    # negocio que estén mirando. Para cualquier otro caso (alta sin
    # terminar), sin tenant no hay nada que escuchar.
    if fila is None or not fila["is_active"]:
        return None
    if fila["tenant_id"] is None and not fila["es_gerencia_plataforma"]:
        return None

    # Sesión de "ver como" de plataforma: puede escuchar las alertas del
    # negocio, pero no marcarlas leídas (ver marcar_leida). La revalidación
    # del gerente la hace deps.usuario_actual en cada petición HTTP; acá
    # alcanza con que el token siga vigente, que dura IMPERSONACION_MINUTOS.
    return {**dict(fila), "solo_lectura": "imp" in payload}


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
    if usuario["solo_lectura"]:
        _solo_lectura.add(sid)
    if usuario["es_gerencia_plataforma"]:
        _gerencia_plataforma.add(sid)

    # Gerencia de plataforma sin negocio propio (tenant_id nulo): la
    # conexión se acepta igual, pero no hay room de origen a la que sumarse
    # ni alertas propias que mandarle — se queda esperando un `ver_tenant`.
    if tenant_id is None:
        logger.info("WS conectado: sid=%s (gerencia de plataforma, sin tenant propio)", sid)
        return

    _conexiones[sid] = tenant_id
    _usuarios[sid] = usuario["id"]
    # La room del negocio lleva las alertas de todos (leads con nombre,
    # reservas, conversaciones que piden una persona): el vendedor ve solo
    # lo suyo, así que se queda únicamente con su room personal. Mismo
    # criterio que deps.ROLES_NEGOCIO en HTTP.
    ve_negocio = usuario["role"] in ROLES_NEGOCIO
    if ve_negocio:
        await sio.enter_room(sid, _sala(tenant_id))
    else:
        _solo_personales.add(sid)
    await sio.enter_room(sid, _sala_usuario(usuario["id"]))
    logger.info("WS conectado: sid=%s tenant=%s role=%s", sid, tenant_id, usuario["role"])

    # Lo que se perdió mientras no había sesión abierta: las últimas no
    # leídas, no el historial completo (para eso está el GET por HTTP).
    pendientes = await fetch_all(
        """
        SELECT id, tenant_id, tipo, titulo, mensaje, datos, leido, creado_en
        FROM alertas
        WHERE tenant_id = $1 AND leido = false
          AND ((portal_user_id IS NULL AND $3) OR portal_user_id = $2)
        ORDER BY creado_en DESC
        LIMIT 10
        """,
        tenant_id,
        usuario["id"],
        ve_negocio,
    )
    await sio.emit("alertas_pendientes", [_serializar(f) for f in pendientes], room=sid)


@sio.event
async def disconnect(sid: str) -> None:
    tenant_id = _conexiones.pop(sid, None)
    _usuarios.pop(sid, None)
    _solo_lectura.discard(sid)
    _gerencia_plataforma.discard(sid)
    _solo_personales.discard(sid)
    logger.info("WS desconectado: sid=%s tenant=%s", sid, tenant_id)


@sio.event
async def ver_tenant(sid: str, data: Optional[dict]) -> None:
    """
    Gerencia de plataforma pidiendo sumarse a la room de un tenant que no
    es el suyo, para ver en vivo la conversación que está mirando en
    /gerencia/conversaciones. Se ignora para cualquier sid que no haya
    quedado marcado como gerencia de plataforma en `connect` — la
    membresía a una room ajena no depende de lo que mande el cliente, sino
    de lo que ya validó el JWT.
    """
    if sid not in _gerencia_plataforma:
        return
    tenant_id = (data or {}).get("tenant_id")
    if not tenant_id:
        return
    await sio.enter_room(sid, _sala(UUID(tenant_id)))


@sio.event
async def dejar_tenant(sid: str, data: Optional[dict]) -> None:
    """Contraparte de `ver_tenant`, al cerrar esa conversación."""
    if sid not in _gerencia_plataforma:
        return
    tenant_id = (data or {}).get("tenant_id")
    if not tenant_id:
        return
    await sio.leave_room(sid, _sala(UUID(tenant_id)))


@sio.event
async def marcar_leida(sid: str, data: Optional[dict]) -> None:
    """
    Evento que manda el cliente al marcar una alerta como leída desde la UI,
    para no depender de que además haga el PATCH por HTTP. Confía en la
    room, no en lo que mande `data`: el tenant_id sale de la conexión ya
    autenticada, nunca del payload del evento.
    """
    tenant_id = _conexiones.get(sid)
    if tenant_id is None or sid in _solo_lectura:
        return

    alerta_id = (data or {}).get("alerta_id")
    if not alerta_id:
        return

    # Una personal solo la marca su dueño; la del negocio, cualquiera de la
    # room (como siempre) — el vendedor no está en esa room.
    fila = await fetch_one(
        """
        UPDATE alertas SET leido = true
         WHERE id = $1 AND tenant_id = $2
           AND ((portal_user_id IS NULL AND $4) OR portal_user_id = $3)
        RETURNING portal_user_id
        """,
        alerta_id,
        tenant_id,
        _usuarios.get(sid),
        sid not in _solo_personales,
    )
    if fila is None:
        return
    sala = _sala_usuario(fila["portal_user_id"]) if fila["portal_user_id"] else _sala(tenant_id)
    await sio.emit("alerta_leida", {"alerta_id": alerta_id}, room=sala)


async def broadcast_alerta(
    tenant_id: UUID,
    tipo: str,
    titulo: str,
    mensaje: str,
    datos: Optional[dict] = None,
    portal_user_id: Optional[UUID] = None,
) -> None:
    """
    Punto de entrada para el resto del backend: crea la alerta en BD y la
    transmite a todos los clientes conectados de ese tenant — o, si trae
    `portal_user_id`, solo a las sesiones de ese usuario.

    sio.emit a una room sin nadie conectado es un no-op — no hace falta
    revisar antes si hay alguien escuchando.
    """
    alerta = await crear_alerta(tenant_id, tipo, titulo, mensaje, datos, portal_user_id)
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
            "portal_user_id": str(portal_user_id) if portal_user_id else None,
        },
        room=_sala_usuario(portal_user_id) if portal_user_id else _sala(tenant_id),
    )


async def _salas_de_conversacion(
    tenant_id: UUID,
    conversation_id: UUID,
    extra_usuarios: Iterable[Optional[UUID]] = (),
) -> list[str]:
    """
    La room del negocio y la room personal de quien ve la conversación sin
    estar en la del negocio (`connect`): el asignado o, si nadie la tiene, el
    proveedor / vendedor dueño por su ficha. `extra_usuarios` suma a quien la
    acaba de perder (el asignado anterior), para que se le quite de la lista.
    """
    from services.conversaciones import usuarios_con_acceso

    usuarios = await usuarios_con_acceso(conversation_id)
    usuarios.update(u for u in extra_usuarios if u is not None)
    return [_sala(tenant_id), *(_sala_usuario(u) for u in usuarios)]


async def emit_mensaje(tenant_id: UUID, conversation_id: UUID, mensaje: dict[str, Any]) -> None:
    """
    Empuja un mensaje nuevo (respuesta manual de handoff) a quien tenga
    abierta esa conversación. A diferencia de `broadcast_alerta`, esto no
    toca la tabla `alertas`: es un evento de UI en vivo, no una notificación
    persistente que alguien tenga que marcar leída.
    """
    await sio.emit(
        "mensaje_nuevo",
        {"conversation_id": str(conversation_id), **mensaje},
        room=await _salas_de_conversacion(tenant_id, conversation_id),
    )


async def emit_conversacion_estado(
    tenant_id: UUID,
    conversation_id: UUID,
    status: str,
    asignado_a: Optional[UUID] = None,
    extra_usuarios: Iterable[Optional[UUID]] = (),
) -> None:
    """
    Avisa que una conversación cambió de estado (handoff) o de responsable.
    `asignado_a` viaja para que la UI actualice el "quién la tiene" sin pedir
    otra vez el listado; None = sin asignar.
    """
    await sio.emit(
        "conversacion_actualizada",
        {
            "conversation_id": str(conversation_id),
            "status": status,
            "asignado_a": str(asignado_a) if asignado_a else None,
        },
        room=await _salas_de_conversacion(tenant_id, conversation_id, extra_usuarios),
    )


async def avisar_a_proveedor(
    tenant_id: UUID,
    portal_user_id: Optional[UUID],
    tipo: str,
    titulo: str,
    mensaje: str,
    datos: Optional[dict] = None,
) -> None:
    """
    Copia personal de una alerta del negocio para el proveedor al que le
    toca (su cita, el chat de su cliente). Con `portal_user_id` None (el
    proveedor no tiene cuenta, o está inactiva) no hace nada. La alerta del
    negocio se manda aparte, como siempre, con broadcast_alerta.
    """
    if portal_user_id is None:
        return
    await broadcast_alerta(tenant_id, tipo, titulo, mensaje, datos, portal_user_id=portal_user_id)


# Mismo mecanismo, pero ya no es solo para proveedores: también lo recibe el
# asignado de una conversación (vendedor, member, owner...). El nombre
# original se conserva porque calendario y eventos lo importan.
avisar_a_usuario = avisar_a_proveedor


async def emitir_datos(
    tenant_id: UUID,
    recurso: str,
    portal_user_ids: Iterable[Optional[UUID]] = (),
) -> None:
    """
    Aviso de UI en vivo: "los datos de `recurso` cambiaron, vuelve a pedirlos".
    Va a la room del negocio y a la room personal de cada usuario indicado
    (proveedor/vendedor afectados, que no están en la del negocio: ver
    `connect`). No toca la tabla `alertas` ni lleva datos: el cliente decide
    qué recargar y el HTTP aplica los permisos de cada rol, así que esto no
    filtra información. Nunca debe romper la operación que lo dispara.
    """
    salas = [_sala(tenant_id)]
    salas += [_sala_usuario(u) for u in set(portal_user_ids) if u is not None]
    try:
        await sio.emit("datos_actualizados", {"recurso": recurso}, room=salas)
    except Exception:  # pragma: no cover - el aviso es best-effort
        logger.exception("No se pudo emitir datos_actualizados (%s)", recurso)
