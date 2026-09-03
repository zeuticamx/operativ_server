"""Configuración central. Todo se lee de variables de entorno."""

import os
from functools import lru_cache

from dotenv import load_dotenv

# uvicorn no carga .env por su cuenta; sin esto, arrancar con
# `uvicorn main:app` nunca ve las variables aunque el archivo exista.
load_dotenv()


class Settings:
    # ---- Base de datos ----
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql://usuario:password@localhost:5432/agente_db",
    )

    # ---- JWT ----
    JWT_SECRET: str = os.getenv("JWT_SECRET", "")
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_MINUTES: int = int(os.getenv("ACCESS_TOKEN_MINUTES", "60"))
    REFRESH_TOKEN_DAYS: int = int(os.getenv("REFRESH_TOKEN_DAYS", "30"))

    # ---- Meta ----
    META_APP_ID: str = os.getenv("META_APP_ID", "")
    META_APP_SECRET: str = os.getenv("META_APP_SECRET", "")
    META_API_VERSION: str = os.getenv("META_API_VERSION", "v26.0")
    META_REDIRECT_URI: str = os.getenv("META_REDIRECT_URI", "")

    # ---- CORS ----
    FRONTEND_ORIGINS: list[str] = [
        o.strip()
        for o in os.getenv("FRONTEND_ORIGINS", "http://localhost:3000").split(",")
        if o.strip()
    ]

    @property
    def graph_url(self) -> str:
        return f"https://graph.facebook.com/{self.META_API_VERSION}"

    def validate(self) -> None:
        """Falla temprano si falta algo crítico, en vez de a media petición."""
        faltantes = []
        if not self.JWT_SECRET:
            faltantes.append("JWT_SECRET")
        if not self.META_APP_ID:
            faltantes.append("META_APP_ID")
        if not self.META_APP_SECRET:
            faltantes.append("META_APP_SECRET")
        if faltantes:
            raise RuntimeError(
                f"Faltan variables de entorno: {', '.join(faltantes)}"
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
