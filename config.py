"""Configuración central. Todo se lee de variables de entorno."""

import logging
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

    # ---- Google (cuenta de servicio compartida, para Sheets/Docs) ----
    GOOGLE_SERVICE_ACCOUNT_JSON: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")

    # ---- SMTP (código de verificación del alta) ----
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    # 587 = STARTTLS (lo normal). 465 = TLS directo, se detecta por el número.
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM: str = os.getenv("SMTP_FROM", "")
    SMTP_FROM_NAME: str = os.getenv("SMTP_FROM_NAME", "OperativAI")
    SMTP_STARTTLS: bool = os.getenv("SMTP_STARTTLS", "true").lower() != "false"

    # ---- Verificación de correo ----
    CODIGO_VIGENCIA_MINUTOS: int = int(os.getenv("CODIGO_VIGENCIA_MINUTOS", "10"))
    # Tope de intentos fallidos antes de descartar el alta. Con 6 dígitos hay
    # un millón de combinaciones: 5 tiros dejan la probabilidad de acertar a
    # ciegas en 1 entre 200.000.
    CODIGO_MAX_INTENTOS: int = int(os.getenv("CODIGO_MAX_INTENTOS", "5"))
    # Espera mínima entre reenvíos, para que /reenviar-codigo no sirva de
    # ametralladora de correos contra una casilla ajena.
    CODIGO_REENVIO_SEGUNDOS: int = int(os.getenv("CODIGO_REENVIO_SEGUNDOS", "60"))

    # ---- CORS ----
    FRONTEND_ORIGINS: list[str] = [
        o.strip()
        for o in os.getenv("FRONTEND_ORIGINS", "http://localhost:3000").split(",")
        if o.strip()
    ]

    @property
    def graph_url(self) -> str:
        return f"https://graph.facebook.com/{self.META_API_VERSION}"

    @property
    def smtp_configurado(self) -> bool:
        """Sin host ni remitente no hay a dónde ni de parte de quién mandar."""
        return bool(self.SMTP_HOST and self.SMTP_FROM)

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

        # El SMTP no se exige: sin él la app arranca igual y los códigos van
        # al log, que es lo cómodo en local. Pero avisa fuerte, porque en un
        # servidor de verdad eso significa que nadie recibe su código.
        if not self.smtp_configurado:
            logging.getLogger("operativai.config").warning(
                "SMTP sin configurar (falta SMTP_HOST o SMTP_FROM). Los códigos "
                "de verificación se van a escribir en el log en vez de enviarse."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
