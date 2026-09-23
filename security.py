"""Autenticación: hashing de contraseñas y emisión/validación de JWT."""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError

from config import settings

# Argon2id es el algoritmo recomendado actualmente para contraseñas.
# Los parámetros por defecto de argon2-cffi son razonables; no los bajes
# para "que vaya más rápido" — el costo es justamente la defensa.
_hasher = PasswordHasher()


# ============================================================
# CONTRASEÑAS
# ============================================================
def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        _hasher.verify(hashed, plain)
        return True
    except (VerifyMismatchError, VerificationError):
        return False


def needs_rehash(hashed: str) -> bool:
    """True si el hash se generó con parámetros viejos y conviene regenerarlo."""
    try:
        return _hasher.check_needs_rehash(hashed)
    except Exception:
        return False


# ============================================================
# JWT
# ============================================================
def _crear_token(payload: dict[str, Any], expira_en: timedelta) -> str:
    ahora = datetime.now(timezone.utc)
    datos = {
        **payload,
        "iat": ahora,
        "exp": ahora + expira_en,
    }
    return jwt.encode(datos, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def crear_access_token(user_id: UUID, tenant_id: UUID | None, role: str) -> str:
    return _crear_token(
        {
            "sub": str(user_id),
            "tenant_id": str(tenant_id) if tenant_id else None,
            "role": role,
            "type": "access",
        },
        timedelta(minutes=settings.ACCESS_TOKEN_MINUTES),
    )


def crear_token_impersonacion(
    user_id: UUID,
    tenant_id: UUID,
    role: str,
    gerente_id: UUID,
    minutos: int,
) -> tuple[str, datetime]:
    """
    Access token para que alguien del nivel gerencia vea el portal como lo
    ve `user_id`, en solo lectura.

    Es un access token normal (`type: access`) con dos claims extra:
    `imp` (quién está mirando de verdad) y `ro`. Que sea de tipo access y no
    uno nuevo es a propósito: así pasa por el mismo deps.usuario_actual que
    todo lo demás, y es ahí — en un solo lugar — donde se revalida al
    gerente y se bloquea cualquier método que no sea de lectura.

    No hay refresh token que lo acompañe: al vencer, se terminó. /refresh
    solo acepta tokens `type: refresh`, así que este no se puede estirar.
    """
    expira_en = timedelta(minutes=minutos)
    token = _crear_token(
        {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "role": role,
            "type": "access",
            "imp": str(gerente_id),
            "ro": True,
        },
        expira_en,
    )
    return token, datetime.now(timezone.utc) + expira_en


def crear_refresh_token(user_id: UUID) -> str:
    return _crear_token(
        {"sub": str(user_id), "type": "refresh"},
        timedelta(days=settings.REFRESH_TOKEN_DAYS),
    )


def decodificar_token(token: str, tipo_esperado: str = "access") -> dict[str, Any]:
    """
    Devuelve el payload si el token es válido.
    Lanza jwt.PyJWTError si está expirado, mal firmado, o es de otro tipo.
    """
    payload = jwt.decode(
        token,
        settings.JWT_SECRET,
        algorithms=[settings.JWT_ALGORITHM],
    )
    if payload.get("type") != tipo_esperado:
        raise jwt.InvalidTokenError(
            f"Se esperaba un token de tipo '{tipo_esperado}'"
        )
    return payload
