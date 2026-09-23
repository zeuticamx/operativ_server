"""Configuración central. Todo se lee de variables de entorno."""

import logging
import os
from decimal import Decimal, InvalidOperation
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

    # ---- Login con Google (OAuth de portal_users, opcional) ----
    # Client ID/secret del proyecto de Google Cloud para "Sign in with
    # Google". El frontend usa el client_id para el botón de Google
    # Identity Services; el backend valida el ID token resultante contra
    # el mismo client_id como audiencia (ver services/google_login.py).
    # GOOGLE_CLIENT_SECRET no hace falta para ese flujo (no es un
    # Authorization Code exchange), se deja cargado por si hiciera falta
    # más adelante. .strip() porque el .env trae un espacio colgando al
    # final del client_id.
    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()

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

    # ---- Pasarela de cobro activa ----
    # "stripe" | "mercadopago". Mismo patrón que WHATSAPP_PROVIDER: el
    # módulo de Mercado Pago sigue entero en el código, pero mientras esto
    # diga "stripe" sus endpoints responden 503 y el portal cobra por
    # Stripe. Volver a MP es cambiar esta variable, sin tocar código.
    #
    # El historial de cobros NO depende de esto: las transacciones viejas
    # de Mercado Pago se siguen leyendo igual.
    PAYMENT_PROVIDER: str = os.getenv("PAYMENT_PROVIDER", "stripe").strip().lower()

    # ---- Stripe ----
    # sk_test_... en pruebas, sk_live_... en producción. Sin esto,
    # /api/pagos/crear-pago responde 503 en vez de un 500 al llamar a la API.
    STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "")
    # Pública a propósito: es la que usaría el navegador si algún día se
    # monta Stripe Elements en vez de redirigir al Checkout hospedado.
    STRIPE_PUBLIC_KEY: str = os.getenv("STRIPE_PUBLIC_KEY", "")
    # Secreto de firma del webhook (whsec_...). Lo da el panel de Stripe al
    # crear el endpoint, o `stripe listen` en local. No es la secret key:
    # sirve solo para validar el HMAC de la cabecera Stripe-Signature.
    #
    # Vacío = /api/pagos/stripe/webhook responde 503 y no procesa nada.
    # Mismo criterio de fallar cerrado que MERCADOPAGO_WEBHOOK_SECRET: un
    # webhook de cobros sin validar es alguien regalándose créditos.
    STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    # Stripe espera el código de moneda en minúsculas (iso 4217).
    STRIPE_CURRENCY: str = os.getenv("STRIPE_CURRENCY", "mxn").strip().lower()

    # ---- Mercado Pago (deshabilitado; ver PAYMENT_PROVIDER) ----
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

    # ---- Panel de plataforma (nivel gerencia) ----
    # Cuántas unidades de la moneda de cobro vale un dólar. El costo de los
    # modelos llega en USD (tenant_token_usage.costo_usd) y los planes se
    # cobran en STRIPE_CURRENCY / MERCADOPAGO_CURRENCY, así que el margen
    # por negocio necesita esta conversión.
    #
    # Es el respaldo manual: si BANXICO_TOKEN está configurado y la moneda
    # de cobro es MXN, services/banxico.py usa el FIX oficial y este valor
    # solo entra si Banxico falla y nunca hubo un valor en caché (ver
    # services/banxico.obtener_tipo_cambio, que decide la fuente final).
    #
    # Sin default a propósito: un tipo de cambio inventado da márgenes que
    # parecen datos. Vacío = el panel muestra el costo en USD y deja el
    # margen en null en vez de calcularlo mal. Si la moneda de cobro ya es
    # USD, se asume 1 (esa regla vive en obtener_tipo_cambio, no acá).
    TIPO_CAMBIO_USD: str = os.getenv("TIPO_CAMBIO_USD", "").strip()

    # Token del SIE API de Banxico (se pide gratis en
    # https://www.banxico.org.mx/SieAPIRest/service/v1/token). Con esto
    # configurado, el margen usa el tipo de cambio FIX del día en vez del
    # valor manual de arriba. Solo aplica si la moneda de cobro es MXN: el
    # FIX es pesos por dólar y no significa nada para otra moneda.
    BANXICO_TOKEN: str = os.getenv("BANXICO_TOKEN", "").strip()

    # Consumo anómalo: un tenant cuyo consumo de las últimas 24 h supera
    # FACTOR veces su promedio diario de los 7 días previos. El PISO es el
    # promedio mínimo que se asume, para que un negocio nuevo (promedio 0)
    # no dispare una alerta con sus primeras diez respuestas: con 5 y
    # 20 000, hacen falta 100 000 tokens en un día para que suene.
    CONSUMO_ANOMALO_FACTOR: float = float(os.getenv("CONSUMO_ANOMALO_FACTOR", "5"))
    CONSUMO_ANOMALO_PISO_TOKENS: int = int(os.getenv("CONSUMO_ANOMALO_PISO_TOKENS", "20000"))
    CONSUMO_ANOMALO_INTERVALO_HORAS: int = int(
        os.getenv("CONSUMO_ANOMALO_INTERVALO_HORAS", "1")
    )

    # Vida del token de "ver como el negocio". Corto a propósito: no hay
    # refresh, así que al vencer el gerente vuelve a su propia sesión y
    # tiene que pedir otro (con otro motivo, que queda en la bitácora).
    IMPERSONACION_MINUTOS: int = int(os.getenv("IMPERSONACION_MINUTOS", "30"))

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
    def stripe_activo(self) -> bool:
        return self.PAYMENT_PROVIDER == "stripe"

    @property
    def mercadopago_activo(self) -> bool:
        return self.PAYMENT_PROVIDER == "mercadopago"

    @property
    def stripe_configurado(self) -> bool:
        """Sin secret key no se puede crear un Checkout ni consultar un pago."""
        return bool(self.STRIPE_SECRET_KEY)

    @property
    def kontesta_configurado(self) -> bool:
        """Sin API key no se puede autenticar ninguna llamada a Kontesta."""
        return bool(self.KONTESTA_API_KEY)

    @property
    def moneda_cobro(self) -> str:
        """Moneda en la que se cobran los planes, según la pasarela activa."""
        if self.mercadopago_activo:
            return self.MERCADOPAGO_CURRENCY.lower()
        return self.STRIPE_CURRENCY

    @property
    def tipo_cambio_usd_manual(self) -> Decimal | None:
        """
        TIPO_CAMBIO_USD ya parseado, o None si está vacío o no es un número
        válido. Un valor mal escrito cuenta como no configurado: mejor sin
        margen que con uno calculado sobre un número que no es.

        Es el respaldo, no la fuente preferida — ver
        services/banxico.obtener_tipo_cambio, que decide entre esto y el
        FIX de Banxico. El atajo de "moneda de cobro ya es USD" también
        vive allá, no acá: esta propiedad es solo el parseo del valor manual.
        """
        try:
            valor = Decimal(self.TIPO_CAMBIO_USD)
        except (InvalidOperation, ValueError):
            return None
        return valor if valor > 0 else None

    @property
    def banxico_configurado(self) -> bool:
        """Sin token no hay llamada; con moneda distinta de MXN, el FIX no aplica."""
        return bool(self.BANXICO_TOKEN) and self.moneda_cobro == "mxn"

    @property
    def google_login_configurado(self) -> bool:
        """Sin client_id no se puede validar la audiencia del ID token."""
        return bool(self.GOOGLE_CLIENT_ID)

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

        # Los cobros no se exigen: un despliegue sin pasarela arranca igual.
        # Pero se avisa por separado de la llave y del secreto del webhook,
        # porque rompen cosas distintas (crear el pago vs. acreditarlo).
        log_config = logging.getLogger("operativai.config")

        if self.PAYMENT_PROVIDER not in ("stripe", "mercadopago"):
            raise RuntimeError(
                f"PAYMENT_PROVIDER inválido: {self.PAYMENT_PROVIDER!r}. "
                "Usa 'stripe' o 'mercadopago'."
            )

        if self.stripe_activo:
            if not self.stripe_configurado:
                log_config.warning(
                    "STRIPE_SECRET_KEY sin configurar: /api/pagos/crear-pago "
                    "va a responder 503."
                )
            elif not self.STRIPE_WEBHOOK_SECRET:
                log_config.warning(
                    "STRIPE_WEBHOOK_SECRET sin configurar: se pueden crear "
                    "pagos pero /api/pagos/stripe/webhook los va a rechazar, "
                    "así que ningún cobro se acreditará."
                )
        else:
            if not self.mercadopago_configurado:
                log_config.warning(
                    "MERCADOPAGO_ACCESS_TOKEN sin configurar: "
                    "/api/pagos/crear-pago va a responder 503."
                )
            elif not self.MERCADOPAGO_WEBHOOK_SECRET:
                log_config.warning(
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

        # Tampoco se exige: el alta y el login con correo siguen funcionando
        # sin esto. Pero si falta, POST /auth/google responde 503 y el botón
        # de Google del portal no sirve para nada.
        if not self.google_login_configurado:
            logging.getLogger("operativai.config").warning(
                "GOOGLE_CLIENT_ID sin configurar: POST /api/auth/google va a "
                "responder 503 y el botón de Google no va a funcionar."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
