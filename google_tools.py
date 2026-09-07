"""
Integración con Google Sheets y Docs usando UNA sola cuenta de servicio
compartida por toda la plataforma (no hay OAuth por cliente).
"""

import json
import re
from functools import lru_cache

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import settings

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
]

_ID_PATTERN = re.compile(r"/d/([a-zA-Z0-9_-]+)")


class ToolAccessError(Exception):
    def __init__(self, mensaje: str):
        self.mensaje = mensaje
        super().__init__(mensaje)


class UrlInvalidaError(Exception):
    def __init__(self, mensaje: str = "No se pudo reconocer un ID de documento en esa URL."):
        self.mensaje = mensaje
        super().__init__(mensaje)


def extraer_id_de_url(url: str) -> str:
    match = _ID_PATTERN.search(url.strip())
    if not match:
        raise UrlInvalidaError()
    return match.group(1)


@lru_cache
def _credenciales_info() -> dict:
    raw = settings.GOOGLE_SERVICE_ACCOUNT_JSON
    if not raw:
        raise RuntimeError(
            "Falta GOOGLE_SERVICE_ACCOUNT_JSON en las variables de entorno"
        )
    return json.loads(raw)


@lru_cache
def correo_cuenta_servicio() -> str:
    return _credenciales_info()["client_email"]


def _credenciales():
    return service_account.Credentials.from_service_account_info(
        _credenciales_info(), scopes=SCOPES
    )


@lru_cache
def _sheets_service():
    return build("sheets", "v4", credentials=_credenciales(), cache_discovery=False)


@lru_cache
def _docs_service():
    return build("docs", "v1", credentials=_credenciales(), cache_discovery=False)


def _traducir_error(e: HttpError, tipo_recurso: str) -> ToolAccessError:
    status = e.resp.status if hasattr(e, "resp") else None
    correo = correo_cuenta_servicio()

    if status == 403:
        return ToolAccessError(
            f"No tenemos acceso a este {tipo_recurso}. Compártelo con "
            f"{correo} (como Lector) e inténtalo de nuevo."
        )
    if status == 404:
        return ToolAccessError(
            f"No encontramos ese {tipo_recurso}. Revisa que el enlace sea correcto."
        )
    return ToolAccessError(f"No se pudo verificar el acceso ({status or 'error'}).")


def validar_sheet(spreadsheet_id: str) -> dict:
    try:
        data = (
            _sheets_service()
            .spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields="properties.title,sheets.properties.title")
            .execute()
        )
    except HttpError as e:
        raise _traducir_error(e, "Sheet")

    hojas = [s["properties"]["title"] for s in data.get("sheets", [])]
    return {"titulo": data.get("properties", {}).get("title", "(sin título)"), "hojas": hojas}


def validar_doc(document_id: str) -> dict:
    try:
        data = _docs_service().documents().get(documentId=document_id, fields="title").execute()
    except HttpError as e:
        raise _traducir_error(e, "documento")

    return {"titulo": data.get("title", "(sin título)")}


def leer_rango(spreadsheet_id: str, rango: str) -> list[list[str]]:
    try:
        data = (
            _sheets_service()
            .spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=rango)
            .execute()
        )
    except HttpError as e:
        raise _traducir_error(e, "Sheet")
    return data.get("values", [])
