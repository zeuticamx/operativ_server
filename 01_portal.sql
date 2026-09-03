-- ============================================================
-- TABLAS DEL PORTAL OperativAI
-- ============================================================
-- IMPORTANTE sobre nombres:
--   users              → clientes finales que escriben por WhatsApp/IG/FB
--   portal_users       → dueños de negocio que entran al portal (esta tabla)
-- Son cosas distintas. No mezclar.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ------------------------------------------------------------
-- Usuarios del portal
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portal_users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID REFERENCES tenants(id) ON DELETE CASCADE,
    email           VARCHAR(255) NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    full_name       VARCHAR(255),
    role            VARCHAR(50) DEFAULT 'owner',   -- owner, member, superadmin
    is_active       BOOLEAN DEFAULT true,
    last_login_at   TIMESTAMP,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_portal_users_email
    ON portal_users (LOWER(email));

CREATE INDEX IF NOT EXISTS idx_portal_users_tenant
    ON portal_users (tenant_id);


-- ------------------------------------------------------------
-- Estado de conexión OAuth con Meta por tenant
-- Guarda el user token de larga duración, que sirve para
-- refrescar los page tokens y para saber cuándo caduca el acceso
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta_connections (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    meta_user_id        VARCHAR(100),
    user_token          BYTEA NOT NULL,          -- cifrado con pgcrypto
    -- TIMESTAMPTZ y no TIMESTAMP: es un instante absoluto que manda Meta.
    -- Con TIMESTAMP habría que decidir en qué huso se guarda, y el job que
    -- renueve el token antes de que venza compararía peras con manzanas si
    -- el servidor de la app y el de la BD no están en el mismo huso.
    token_expires_at    TIMESTAMPTZ,
    scopes              TEXT[],
    connected_at        TIMESTAMP DEFAULT NOW(),
    last_refreshed_at   TIMESTAMP,
    UNIQUE (tenant_id)
);


-- ------------------------------------------------------------
-- Guardar / leer el user token de Meta (mismo patrón que
-- set_channel_credentials: la llave nunca sale de Postgres)
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_meta_connection(
    p_tenant_id     UUID,
    p_meta_user_id  VARCHAR,
    p_user_token    TEXT,
    p_expires_at    TIMESTAMPTZ DEFAULT NULL,
    p_scopes        TEXT[] DEFAULT NULL
)
RETURNS UUID
LANGUAGE sql
SECURITY DEFINER
AS $$
    INSERT INTO meta_connections
        (tenant_id, meta_user_id, user_token, token_expires_at, scopes, last_refreshed_at)
    VALUES (
        p_tenant_id,
        p_meta_user_id,
        pgp_sym_encrypt(p_user_token, current_setting('app.cred_key')),
        p_expires_at,
        p_scopes,
        NOW()
    )
    ON CONFLICT (tenant_id) DO UPDATE SET
        meta_user_id      = EXCLUDED.meta_user_id,
        user_token        = EXCLUDED.user_token,
        token_expires_at  = EXCLUDED.token_expires_at,
        scopes            = COALESCE(EXCLUDED.scopes, meta_connections.scopes),
        last_refreshed_at = NOW()
    RETURNING id;
$$;


CREATE OR REPLACE FUNCTION get_meta_connection(p_tenant_id UUID)
RETURNS TABLE (
    meta_user_id      VARCHAR,
    user_token        TEXT,
    token_expires_at  TIMESTAMPTZ,
    scopes            TEXT[]
)
LANGUAGE sql
SECURITY DEFINER
AS $$
    SELECT
        c.meta_user_id,
        pgp_sym_decrypt(c.user_token, current_setting('app.cred_key')),
        c.token_expires_at,
        c.scopes
    FROM meta_connections c
    WHERE c.tenant_id = p_tenant_id;
$$;


-- ------------------------------------------------------------
-- Vista de canales conectados, SIN exponer tokens.
-- Es lo que el portal muestra en la pantalla de conexiones.
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW v_tenant_channels AS
SELECT
    c.tenant_id,
    c.channel_type,
    c.page_id,
    c.ig_user_id,
    c.phone_number_id,
    c.is_active,
    c.updated_at
FROM channel_credentials c;


-- ------------------------------------------------------------
-- Datos iniciales: un superadmin para arrancar.
-- El hash se genera desde Python, no aquí. Ver README.
-- ------------------------------------------------------------
