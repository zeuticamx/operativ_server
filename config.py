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

    # ---- Endpoint interno para n8n ----
    # /api/eventos/* lo llama n8n, que no tiene sesión de portal y por lo
    # tanto no puede mandar el JWT de portal_users. Se autentica con este
    # secreto compartido en la cabecera X-Internal-Token.
    #
    # Vacío = el endpoint responde 503 y no atiende a nadie. Se prefiere
    # fallar cerrado: un endpoint que crea leads y confirma qué tenant_id
    # existe no puede quedar abierto por olvidar una variable.
    N8N_INTERNAL_TOKEN: str = os.getenv("N8N_INTERNAL_TOKEN", "")

    # ---- Meta ----
    META_APP_ID: str = os.getenv("META_APP_ID", "")
    META_APP_SECRET: str = os.getenv("META_APP_SECRET", "")
    META_API_VERSION: str = os.getenv("META_API_VERSION", "v26.0")
    META_REDIRECT_URI: str = os.getenv("META_REDIRECT_URI", "")

    # ---- WhatsApp (envío de mensajes, ver services/whatsapp.py) ----
    # Proveedor activo detrás de ProveedorWhatsApp. Hoy "kontesta", mientras
    # la app de Meta no tiene Acceso Avanzado aprobado en App Review; el día
    # que se apruebe, pasar esto a "meta" no debería tocar ningún router.
    WHATSAPP_PROVIDER: str = os.getenv("WHATSAPP_PROVIDER", "kontesta")

    # ---- Kontesta ----
    KONTESTA_API_KEY: str = os.getenv("KONTESTA_API_KEY", "")
    KONTESTA_API_BASE_URL: str = os.getenv(
        "KONTESTA_API_BASE_URL", "https://api.kontesta.app/v1"
    ).rstrip("/")
    # Secreto para validar la firma HMAC de los webhooks entrantes
    # (X-Kontesta-Signature). Vacío = verificar_webhook() rechaza todo.
    # Mismo criterio de fallar cerrado que MERCADOPAGO_WEBHOOK_SECRET.
    KONTESTA_WEBHOOK_SECRET: str = os.getenv("KONTESTA_WEBHOOK_SECRET", "")

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

    # ---- Alertas de background ----
    # Cuánto tiempo sin actualizarse hace que un lead cuente como "sin
    # actividad", y cada cuánto se revisa. Correrlo seguido no insiste con
    # el mismo lead: el propio job no repite una alerta antes de 24h.
    ALERTA_SIN_ACTIVIDAD_DIAS: int = int(os.getenv("ALERTA_SIN_ACTIVIDAD_DIAS", "7"))
    ALERTA_SIN_ACTIVIDAD_INTERVALO_HORAS: int = int(
        os.getenv("ALERTA_SIN_ACTIVIDAD_INTERVALO_HORAS", "1")
    )

    # Hora (0-23, UTC) del resumen diario de alertas sin leer por correo.
    # En UTC y no por tenant a propósito: no hay huso horario guardado por
    # negocio todavía, y una hora fija es mejor que ninguna hasta que haga
    # falta ese nivel de detalle.
    RESUMEN_ALERTAS_HORA_UTC: int = int(os.getenv("RESUMEN_ALERTAS_HORA_UTC", "21"))

    # Cada cuánto se revisan suscripciones vencidas para pausarlas (ver
    # jobs/pagos_background.py). El ciclo es de 30 días; revisarlo cada hora
    # es barato (un solo UPDATE) y deja como mucho una hora de margen entre
    # que vence y que el bloqueo de acceso_pagos.py surte efecto.
    SUSCRIPCION_REVISION_INTERVALO_HORAS: int = int(
        os.getenv("SUSCRIPCION_REVISION_INTERVALO_HORAS", "1")
    )

    # ---- Mercado Pago ----
    MERCADOPAGO_ACCESS_TOKEN: str = os.getenv("MERCADOPAGO_ACCESS_TOKEN", "")
    # Pública a propósito: es la que el navegador usa si algún día se monta
    # el Brick de checkout en vez de redirigir al init_point.
    MERCADOPAGO_PUBLIC_KEY: str = os.getenv("MERCADOPAGO_PUBLIC_KEY", "")
    # Secreto de firma del webhook (panel de MP > Webhooks > "Clave secreta").
    # No es el access token: sirve solo para validar el HMAC de x-signature.
    #
    # Vacío = /api/pagos/webhook responde 503 y no procesa nada. Mismo
    # criterio de fallar cerrado que N8N_INTERNAL_TOKEN: un webhook de
    # cobros sin validar es alguien regalándose créditos.
    MERCADOPAGO_WEBHOOK_SECRET: str = os.getenv("MERCADOPAGO_WEBHOOK_SECRET", "")
    # Moneda de los cobros. El portal formatea montos en MXN (ver
    # lib/formato.ts), así que el default acompaña.
    MERCADOPAGO_CURRENCY: str = os.getenv("MERCADOPAGO_CURRENCY", "MXN")
    # A dónde vuelve el comprador al terminar el checkout. Sin esto, las
    # back_urls apuntarían a localhost en producción.
    BASE_URL_FRONTEND: str = os.getenv("BASE_URL_FRONTEND", "http://localhost:3000").rstrip("/")
    # URL pública del backend, para la notification_url del webhook. En
    # local no sirve localhost: Mercado Pago tiene que poder alcanzarla
    # (ngrok o similar).
    BASE_URL_BACKEND: str = os.getenv("BASE_URL_BACKEND", "http://localhost:8000").rstrip("/")

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

    @property
    def mercadopago_configurado(self) -> bool:
        """Sin access token no se puede crear una preferencia ni consultar un pago."""
        return bool(self.MERCADOPAGO_ACCESS_TOKEN)

    @property
    def kontesta_configurado(self) -> bool:
        """Sin API key no se puede autenticar ninguna llamada a Kontesta."""
        return bool(self.KONTESTA_API_KEY)

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

        # Tampoco se exige: un tenant que solo usa el portal no necesita el
        # endpoint de eventos. Pero si n8n va a llamarlo y esto falta, todas
        # las llamadas se van a rechazar con 503 y conviene verlo al arrancar
        # y no cuando se pierda el primer lead.
        if not self.N8N_INTERNAL_TOKEN:
            logging.getLogger("operativai.config").warning(
                "N8N_INTERNAL_TOKEN sin configurar: /api/eventos/* va a "
                "responder 503. Llénalo si n8n tiene que reportar mensajes "
                "entrantes al módulo de vendedores."
            )

        # Tampoco se exige: un despliegue sin cobros arranca igual. Pero se
        # avisa por separado, porque faltar el token y faltar el secreto del
        # webhook rompen cosas distintas (crear el pago vs. acreditarlo).
        if not self.mercadopago_configurado:
            logging.getLogger("operativai.config").warning(
                "MERCADOPAGO_ACCESS_TOKEN sin configurar: /api/pagos/crear-pago "
                "va a responder 503."
            )
        elif not self.MERCADOPAGO_WEBHOOK_SECRET:
            logging.getLogger("operativai.config").warning(
                "MERCADOPAGO_WEBHOOK_SECRET sin configurar: se pueden crear "
                "pagos pero /api/pagos/webhook los va a rechazar, así que "
                "ningún cobro se acreditará."
            )

        # Igual criterio: un despliegue puede no mandar WhatsApp todavía.
        # Pero si WHATSAPP_PROVIDER=kontesta y falta la API key, todo envío
        # va a fallar en el primer request y conviene verlo al arrancar.
        if self.WHATSAPP_PROVIDER == "kontesta" and not self.kontesta_configurado:
            logging.getLogger("operativai.config").warning(
                "KONTESTA_API_KEY sin configurar: los envíos de WhatsApp vía "
                "Kontesta van a fallar."
            )
            if not self.KONTESTA_WEBHOOK_SECRET:
                logging.getLogger("operativai.config").warning(
                    "KONTESTA_WEBHOOK_SECRET sin configurar: "
                    "verificar_webhook() va a rechazar todos los webhooks "
                    "entrantes de Kontesta."
                )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
