-- ============================================================
-- ESQUEMA BASE PARA ENTORNO LOCAL DE PRUEBAS
-- ============================================================
-- 01_portal.sql asume que estas tablas ya existen (las crea y usa
-- el workflow de n8n en la base de datos real: agente_db). Este
-- repo no incluye ese esquema, así que para poder correr el backend
-- localmente se reconstruye aquí lo mínimo que el código Python
-- (auth.py, agente.py, canales.py, conversaciones.py) necesita.
--
-- NO es el esquema de producción — es una reconstrucción a partir
-- de las columnas que las queries realmente usan. Si en algún
-- momento hay acceso al dump real de n8n, reemplazar este archivo
-- por ese esquema y borrar este.
--
-- Orden: correr ESTE archivo antes que 01_portal.sql.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(255) NOT NULL,
    created_at  TIMESTAMP DEFAULT NOW()
);

-- ------------------------------------------------------------
-- Clientes finales que escriben por WhatsApp/IG/FB (distinto de
-- portal_users, que son los dueños de negocio).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID REFERENCES tenants(id) ON DELETE CASCADE,
    display_name    VARCHAR(255),
    whatsapp_id     VARCHAR(100),
    instagram_id    VARCHAR(100),
    facebook_id     VARCHAR(100),
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_tenant ON users (tenant_id);

-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel_type        VARCHAR(20) NOT NULL,
    status              VARCHAR(20) NOT NULL DEFAULT 'active',
    started_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    last_message_at     TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversations_tenant ON conversations (tenant_id);
CREATE INDEX IF NOT EXISTS idx_conversations_last_msg ON conversations (last_message_at DESC);

-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    conversation_id     UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role                VARCHAR(20) NOT NULL,
    content             TEXT NOT NULL,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages (conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_tenant_created ON messages (tenant_id, created_at);

-- ------------------------------------------------------------
-- Config del agente por tenant (agente.py). PK = tenant_id porque
-- el PUT usa ON CONFLICT (tenant_id) DO UPDATE.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenant_agent_config (
    tenant_id       UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    agent_name      VARCHAR(100) NOT NULL DEFAULT 'Asistente',
    system_prompt   TEXT NOT NULL,
    model           VARCHAR(100),
    temperature     NUMERIC(3,2) NOT NULL DEFAULT 0.7,
    history_window  INT NOT NULL DEFAULT 30,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- ------------------------------------------------------------
-- Qué canales tiene activos un tenant (canales.py). PK compuesta
-- porque el código hace ON CONFLICT (tenant_id, channel_type).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenant_channels (
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    channel_type    VARCHAR(20) NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    PRIMARY KEY (tenant_id, channel_type)
);

-- ------------------------------------------------------------
-- Credenciales por canal (page token / phone_number_id). El token
-- se cifra igual que meta_connections.user_token en 01_portal.sql:
-- nunca sale de Postgres en claro.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS channel_credentials (
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    channel_type        VARCHAR(20) NOT NULL,
    access_token        BYTEA,
    phone_number_id     VARCHAR(100),
    page_id             VARCHAR(100),
    ig_user_id          VARCHAR(100),
    is_active           BOOLEAN NOT NULL DEFAULT true,
    updated_at          TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (tenant_id, channel_type)
);

-- ------------------------------------------------------------
-- Guarda/actualiza credenciales de un canal. Firma inferida de las
-- llamadas en canales.py:
--   set_channel_credentials(tenant_id, 'facebook',  page_token, NULL,     page_id, NULL)
--   set_channel_credentials(tenant_id, 'instagram', page_token, NULL,     page_id, ig_user_id)
-- (el 4to parámetro sería phone_number_id, para el futuro alta de WhatsApp)
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_channel_credentials(
    p_tenant_id         UUID,
    p_channel_type      VARCHAR,
    p_access_token      TEXT,
    p_phone_number_id   VARCHAR DEFAULT NULL,
    p_page_id           VARCHAR DEFAULT NULL,
    p_ig_user_id        VARCHAR DEFAULT NULL
)
RETURNS VOID
LANGUAGE sql
SECURITY DEFINER
AS $$
    INSERT INTO channel_credentials
        (tenant_id, channel_type, access_token, phone_number_id, page_id, ig_user_id, is_active, updated_at)
    VALUES (
        p_tenant_id,
        p_channel_type,
        pgp_sym_encrypt(p_access_token, current_setting('app.cred_key')),
        p_phone_number_id,
        p_page_id,
        p_ig_user_id,
        true,
        NOW()
    )
    ON CONFLICT (tenant_id, channel_type) DO UPDATE SET
        access_token      = EXCLUDED.access_token,
        phone_number_id   = COALESCE(EXCLUDED.phone_number_id, channel_credentials.phone_number_id),
        page_id           = COALESCE(EXCLUDED.page_id, channel_credentials.page_id),
        ig_user_id        = COALESCE(EXCLUDED.ig_user_id, channel_credentials.ig_user_id),
        is_active         = true,
        updated_at        = NOW();
$$;
