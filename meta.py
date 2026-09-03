"""
Cliente de Meta Graph API.

Nota sobre acceso: mientras la app no tenga Acceso Avanzado aprobado
en App Review, /me/accounts solo devolverá páginas de usuarios que
tengan un rol en la app. El código no cambia cuando se apruebe;
simplemente empezarán a llegar más páginas.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import HTTPException, status

from config import settings


class MetaError(Exception):
    def __init__(self, code: int | None, mensaje: str, subcode: int | None = None):
        self.code = code
        self.subcode = subcode
        self.mensaje = mensaje
        super().__init__(mensaje)


DIAGNOSTICOS = {
    3: "La app no tiene permiso para esta operación. Revisa Acceso Avanzado.",
    10: "Faltan permisos en la app de Meta.",
    190: "El token expiró o fue revocado. Hay que reconectar la cuenta.",
    200: "Faltan permisos sobre esta página.",
    613: "Se alcanzó el límite de peticiones. Intenta más tarde.",
}


def _revisar_error(data: dict[str, Any]) -> None:
    if "error" not in data:
        return
    err = data["error"]
    code = err.get("code")
    raise MetaError(
        code=code,
        subcode=err.get("error_subcode"),
        mensaje=DIAGNOSTICOS.get(code, err.get("message", "Error de Meta")),
    )


async def _get(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    r = await client.get(f"{settings.graph_url}{path}", params=params)
    data = r.json()
    _revisar_error(data)
    return data


# ============================================================
# OAuth
# ============================================================
async def intercambiar_code(code: str, redirect_uri: str) -> dict:
    """code (del popup) → user access token de corta duración."""
    async with httpx.AsyncClient(timeout=20) as client:
        return await _get(
            client,
            "/oauth/access_token",
            {
                "client_id": settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )


async def token_larga_duracion(short_token: str) -> dict:
    """Token de corta duración → uno de ~60 días."""
    async with httpx.AsyncClient(timeout=20) as client:
        return await _get(
            client,
            "/oauth/access_token",
            {
                "grant_type": "fb_exchange_token",
                "client_id": settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "fb_exchange_token": short_token,
            },
        )


async def info_token(token: str) -> dict:
    """Debug del token: a qué app pertenece, qué scopes trae, cuándo expira."""
    app_token = f"{settings.META_APP_ID}|{settings.META_APP_SECRET}"
    async with httpx.AsyncClient(timeout=20) as client:
        data = await _get(
            client,
            "/debug_token",
            {"input_token": token, "access_token": app_token},
        )
    return data.get("data", {})


# ============================================================
# Páginas y cuentas
# ============================================================
async def listar_paginas(user_token: str) -> list[dict]:
    """
    Páginas que el usuario autorizó, con su Instagram vinculado si lo tiene.
    El access_token de cada página, derivado de un user token de larga
    duración, no expira mientras el usuario no revoque el acceso.
    """
    async with httpx.AsyncClient(timeout=25) as client:
        data = await _get(
            client,
            "/me/accounts",
            {
                "access_token": user_token,
                "fields": "id,name,access_token,instagram_business_account{id,username}",
                "limit": 100,
            },
        )
    return data.get("data", [])


async def suscribir_app_a_pagina(page_id: str, page_token: str) -> None:
    """
    Sin esto, Meta no manda los webhooks de esa página aunque el
    webhook de la app esté configurado.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{settings.graph_url}/{page_id}/subscribed_apps",
            params={
                "access_token": page_token,
                "subscribed_fields": "messages,messaging_postbacks",
            },
        )
        data = r.json()
        _revisar_error(data)


async def desuscribir_app_de_pagina(page_id: str, page_token: str) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.delete(
            f"{settings.graph_url}/{page_id}/subscribed_apps",
            params={"access_token": page_token},
        )
        data = r.json()
        _revisar_error(data)


def expira_en(segundos: int | None) -> datetime | None:
    if not segundos:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=segundos)


def a_http(e: MetaError) -> HTTPException:
    """Traduce un error de Meta a una respuesta HTTP con sentido."""
    if e.code in (190, 102):
        codigo = status.HTTP_401_UNAUTHORIZED
    elif e.code in (3, 10, 200):
        codigo = status.HTTP_403_FORBIDDEN
    elif e.code == 613:
        codigo = status.HTTP_429_TOO_MANY_REQUESTS
    else:
        codigo = status.HTTP_502_BAD_GATEWAY
    return HTTPException(status_code=codigo, detail=e.mensaje)
