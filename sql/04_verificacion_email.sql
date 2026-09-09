-- ============================================================
-- MIGRACIÓN: verificación de correo al crear cuenta
-- ============================================================
-- El alta deja de ser un solo paso. Ahora:
--
--   1. POST /auth/registro   -> guarda el alta acá y manda un código de 6
--                               dígitos al correo. NO crea nada más.
--   2. POST /auth/verificar  -> si el código coincide, recién ahí se crean
--                               el tenant, su tenant_agent_config y el
--                               portal_user dueño.
--
-- Por qué una tabla aparte y no un `email_verified` en portal_users:
-- si el alta sin verificar ya ocupara una fila de portal_users, cualquiera
-- podría registrar el correo de otro y dejarlo bloqueado por el UNIQUE de
-- email, sin haber probado nunca que le pertenece. Además quedarían tenants
-- huérfanos de cuentas que nadie confirmó. Acá el alta pendiente no toca
-- ninguna de las dos tablas.
--
-- La PK es el correo: registrarse dos veces con el mismo correo pisa el
-- intento anterior (ON CONFLICT DO UPDATE) en vez de acumular filas, y de
-- paso invalida el código viejo.
--
-- Idempotente. Correr una sola vez por base de datos.
-- ============================================================

CREATE TABLE IF NOT EXISTS email_verifications (
    -- Siempre en minúsculas: lo normaliza el backend antes de escribir.
    email           VARCHAR(255) PRIMARY KEY,

    -- Hash argon2 del código, igual que una contraseña. Si alguien lee la
    -- tabla no se lleva códigos usables; con solo 5 intentos permitidos, el
    -- costo de argon2 tampoco molesta.
    code_hash       TEXT NOT NULL,

    -- La contraseña ya viene hasheada desde /registro: en claro no se
    -- guarda ni un momento, ni siquiera mientras el alta está pendiente.
    password_hash   TEXT NOT NULL,
    full_name       VARCHAR(255),
    nombre_negocio  VARCHAR(255) NOT NULL,

    -- Intentos fallidos de código. Al llegar al tope el alta se descarta.
    attempts        SMALLINT NOT NULL DEFAULT 0,

    -- TIMESTAMPTZ y no TIMESTAMP: son instantes absolutos que se comparan
    -- contra NOW(). Mismo criterio que meta_connections.token_expires_at.
    expires_at      TIMESTAMPTZ NOT NULL,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- La limpieza de vencidos la hace /registro en cada alta (DELETE ... WHERE
-- expires_at < NOW()). Es barata y evita depender de un cron.
CREATE INDEX IF NOT EXISTS idx_email_verifications_expires
    ON email_verifications (expires_at);
