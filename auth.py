"""Registro, login y refresh de tokens."""

from uuid import UUID

import asyncpg
import jwt
from fastapi import APIRouter, Depends, HTTPException, status

from deps import UsuarioActual, usuario_actual
from security import (
    crear_access_token,
    crear_refresh_token,
    decodificar_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from session import execute, fetch_one, get_pool
from schemas import LoginIn, RefreshIn, RegistroIn, TokenOut, UsuarioOut

router = APIRouter(prefix="/auth", tags=["auth"])


PROMPT_INICIAL = """Eres el asistente virtual de {negocio}.

# TONO
Cercano, profesional y resolutivo. Español neutro.

# FORMATO
- Máximo 2 o 3 líneas por respuesta. Es un chat, no un correo.
- Ve directo a resolver, sin fórmulas de cortesía largas.

# REGLAS
- Si no sabes algo, dilo. No inventes datos ni precios.
- Usa el historial: no vuelvas a preguntar lo que el cliente ya te dijo.
- Si preguntan si eres una IA, confírmalo con naturalidad."""


@router.post("/registro", response_model=TokenOut, status_code=201)
async def registro(datos: RegistroIn):
    """
    Crea el negocio (tenant), su configuración de agente por defecto,
    y el usuario dueño. Todo en una transacción: si algo falla,
    no queda un tenant huérfano sin usuario.
    """
    pwd_hash = hash_password(datos.password)

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            existe = await conn.fetchval(
                "SELECT 1 FROM portal_users WHERE LOWER(email) = LOWER($1)",
                datos.email,
            )
            if existe:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Ya existe una cuenta con ese correo",
                )

            tenant_id = await conn.fetchval(
                "INSERT INTO tenants (name) VALUES ($1) RETURNING id",
                datos.nombre_negocio,
            )

            await conn.execute(
                """
                INSERT INTO tenant_agent_config
                    (tenant_id, agent_name, system_prompt, temperature, history_window)
                VALUES ($1, $2, $3, 0.7, 30)
                """,
                tenant_id,
                "Asistente",
                PROMPT_INICIAL.format(negocio=datos.nombre_negocio),
            )

            user_id = await conn.fetchval(
                """
                INSERT INTO portal_users (tenant_id, email, password_hash, full_name, role)
                VALUES ($1, $2, $3, $4, 'owner')
                RETURNING id
                """,
                tenant_id,
                datos.email.lower(),
                pwd_hash,
                datos.full_name,
            )

    return TokenOut(
        access_token=crear_access_token(user_id, tenant_id, "owner"),
        refresh_token=crear_refresh_token(user_id),
    )


@router.post("/login", response_model=TokenOut)
async def login(datos: LoginIn):
    fila = await fetch_one(
        """
        SELECT id, tenant_id, password_hash, role, is_active
        FROM portal_users
        WHERE LOWER(email) = LOWER($1)
        """,
        datos.email,
    )

    # Mismo mensaje y mismo costo aproximado para usuario inexistente y
    # contraseña incorrecta: no queremos que se pueda enumerar correos
    # midiendo qué respuesta llega.
    if fila is None:
        hash_password(datos.password)  # gasta el tiempo igual
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Correo o contraseña incorrectos",
        )

    if not verify_password(datos.password, fila["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Correo o contraseña incorrectos",
        )

    if not fila["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="La cuenta está desactivada",
        )

    # Si el hash quedó con parámetros viejos, se actualiza aprovechando
    # que en este momento tenemos la contraseña en claro.
    if needs_rehash(fila["password_hash"]):
        await execute(
            "UPDATE portal_users SET password_hash = $1 WHERE id = $2",
            hash_password(datos.password),
            fila["id"],
        )

    await execute(
        "UPDATE portal_users SET last_login_at = NOW() WHERE id = $1",
        fila["id"],
    )

    return TokenOut(
        access_token=crear_access_token(
            fila["id"], fila["tenant_id"], fila["role"]
        ),
        refresh_token=crear_refresh_token(fila["id"]),
    )


@router.post("/refresh", response_model=TokenOut)
async def refresh(datos: RefreshIn):
    try:
        payload = decodificar_token(datos.refresh_token, "refresh")
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token inválido o expirado",
        )

    fila = await fetch_one(
        "SELECT id, tenant_id, role, is_active FROM portal_users WHERE id = $1",
        UUID(payload["sub"]),
    )

    if fila is None or not fila["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario no encontrado o inactivo",
        )

    return TokenOut(
        access_token=crear_access_token(
            fila["id"], fila["tenant_id"], fila["role"]
        ),
        refresh_token=crear_refresh_token(fila["id"]),
    )


@router.get("/yo", response_model=UsuarioOut)
async def yo(usuario: UsuarioActual = Depends(usuario_actual)):
    nombre_negocio = None
    if usuario.tenant_id:
        nombre_negocio = await fetch_one(
            "SELECT name FROM tenants WHERE id = $1", usuario.tenant_id
        )
        nombre_negocio = nombre_negocio["name"] if nombre_negocio else None

    return UsuarioOut(
        id=usuario.id,
        email=usuario.email,
        full_name=None,
        role=usuario.role,
        tenant_id=usuario.tenant_id,
        nombre_negocio=nombre_negocio,
    )
