"""
Valida el ID token que manda el botón "Sign in with Google" del portal.

`google.oauth2.id_token.verify_oauth2_token` comprueba firma, vigencia y
audiencia trayendo las claves públicas de Google por HTTP (con cache propio
de la librería). Esa llamada es síncrona (usa `requests`, no hay variante
async), así que corre en un hilo aparte para no frenar el loop de asyncio —
mismo criterio que el envío de correo por smtplib en services/correo.py.
"""

import asyncio

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from config import settings


class TokenGoogleInvalido(Exception):
    """El credential no es un ID token válido de Google para nuestro client_id."""


def _verificar_sync(credential: str) -> dict:
    try:
        return google_id_token.verify_oauth2_token(
            credential, google_requests.Request(), settings.GOOGLE_CLIENT_ID
        )
    except Exception as e:
        # Log del error exacto para debugging
        import logging
        logging.error(f"Error validando token de Google: {type(e).__name__}: {e}")
        raise


async def verificar_credential(credential: str) -> dict:
    """
    Devuelve el payload del ID token (incluye 'sub', 'email',
    'email_verified', 'name'). Lanza TokenGoogleInvalido si la firma, la
    vigencia o la audiencia no cierran.
    """
    try:
        return await asyncio.to_thread(_verificar_sync, credential)
    except ValueError as e:
        raise TokenGoogleInvalido(str(e)) from e
