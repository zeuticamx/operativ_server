"""Dependencias compartidas: quién está autenticado y a qué tenant pertenece."""

from dataclasses import dataclass
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from security import decodificar_token
from session import fetch_one

bearer = HTTPBearer(auto_error=False)


@dataclass
class UsuarioActual:
    id: UUID
    tenant_id: UUID | None
    email: str
    role: str

    @property
    def es_superadmin(self) -> bool:
        return self.role == "superadmin"


async def usuario_actual(
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> UsuarioActual:
    if cred is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta el token de autenticación",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decodificar_token(cred.credentials, "access")
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="El token expiró",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Se relee de BD en vez de confiar solo en el JWT: si al usuario lo
    # desactivaron o lo movieron de tenant, el token viejo no debe seguir
    # sirviendo hasta que expire.
    fila = await fetch_one(
        """
        SELECT id, tenant_id, email, role, is_active
        FROM portal_users
        WHERE id = $1
        """,
        UUID(payload["sub"]),
    )

    if fila is None or not fila["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario no encontrado o inactivo",
        )

    return UsuarioActual(
        id=fila["id"],
        tenant_id=fila["tenant_id"],
        email=fila["email"],
        role=fila["role"],
    )


async def tenant_actual(
    usuario: UsuarioActual = Depends(usuario_actual),
) -> UUID:
    """
    Para endpoints que operan sobre datos de un tenant.
    Falla si el usuario todavía no tiene tenant asignado.
    """
    if usuario.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El usuario no tiene un negocio asociado todavía",
        )
    return usuario.tenant_id
