-- ============================================================
-- HERRAMIENTAS POR TENANT
-- ============================================================
CREATE TABLE IF NOT EXISTS tenant_tools (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tool_key            VARCHAR(100) NOT NULL,
    tool_type           VARCHAR(50) NOT NULL,
    display_name        VARCHAR(255) NOT NULL,
    description         TEXT NOT NULL,
    parametros_schema   JSONB NOT NULL DEFAULT '{}',
    config              JSONB NOT NULL,
    is_enabled          BOOLEAN DEFAULT true,
    last_verified_at    TIMESTAMP,
    created_at          TIMESTAMP DEFAULT NOW(),
    updated_at          TIMESTAMP DEFAULT NOW(),
    UNIQUE (tenant_id, tool_key)
);

CREATE INDEX IF NOT EXISTS idx_tenant_tools_lookup
    ON tenant_tools (tenant_id, is_enabled);

CREATE TABLE IF NOT EXISTS tool_credentials (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tool_key        VARCHAR(100) NOT NULL,
    credentials     BYTEA NOT NULL,
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (tenant_id, tool_key)
);

CREATE OR REPLACE FUNCTION set_tool_credentials(
    p_tenant_id UUID, p_tool_key VARCHAR, p_credentials_json TEXT
)
RETURNS UUID LANGUAGE sql SECURITY DEFINER AS $$
    INSERT INTO tool_credentials (tenant_id, tool_key, credentials)
    VALUES (p_tenant_id, p_tool_key, pgp_sym_encrypt(p_credentials_json, current_setting('app.cred_key')))
    ON CONFLICT (tenant_id, tool_key) DO UPDATE SET
        credentials = EXCLUDED.credentials
    RETURNING id;
$$;

CREATE OR REPLACE FUNCTION get_tool_credentials(p_tenant_id UUID, p_tool_key VARCHAR)
RETURNS TEXT LANGUAGE sql SECURITY DEFINER AS $$
    SELECT pgp_sym_decrypt(credentials, current_setting('app.cred_key'))
    FROM tool_credentials WHERE tenant_id = p_tenant_id AND tool_key = p_tool_key;
$$;

CREATE OR REPLACE FUNCTION ejecutar_query_dinamica(
    p_query   TEXT,
    p_valores TEXT[]
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    resultado JSONB;
    sentencia TEXT;
    n         INT;
    v         TEXT[];
BEGIN
    IF NOT (trim(p_query) ILIKE 'SELECT%') THEN
        RAISE EXCEPTION 'Solo se permiten queries SELECT';
    END IF;

    sentencia := format('SELECT COALESCE(jsonb_agg(t), ''[]''::jsonb) FROM (%s) t', p_query);

    -- EXECUTE ... USING no admite VARIADIC: la lista de parámetros tiene que
    -- ser fija en el código. Se despliega por cantidad para conservar el
    -- binding real ($1, $2, ... siguen siendo parámetros, no texto pegado).
    v := COALESCE(p_valores, ARRAY[]::TEXT[]);
    n := cardinality(v);

    CASE n
        WHEN 0 THEN EXECUTE sentencia INTO resultado;
        WHEN 1 THEN EXECUTE sentencia INTO resultado USING v[1];
        WHEN 2 THEN EXECUTE sentencia INTO resultado USING v[1], v[2];
        WHEN 3 THEN EXECUTE sentencia INTO resultado USING v[1], v[2], v[3];
        WHEN 4 THEN EXECUTE sentencia INTO resultado USING v[1], v[2], v[3], v[4];
        WHEN 5 THEN EXECUTE sentencia INTO resultado USING v[1], v[2], v[3], v[4], v[5];
        WHEN 6 THEN EXECUTE sentencia INTO resultado
                        USING v[1], v[2], v[3], v[4], v[5], v[6];
        WHEN 7 THEN EXECUTE sentencia INTO resultado
                        USING v[1], v[2], v[3], v[4], v[5], v[6], v[7];
        WHEN 8 THEN EXECUTE sentencia INTO resultado
                        USING v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8];
        WHEN 9 THEN EXECUTE sentencia INTO resultado
                        USING v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9];
        WHEN 10 THEN EXECUTE sentencia INTO resultado
                        USING v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9], v[10];
        ELSE RAISE EXCEPTION 'Demasiados parámetros: % (máximo 10)', n;
    END CASE;

    RETURN resultado;
END;
$$;

ALTER FUNCTION ejecutar_query_dinamica(TEXT, TEXT[]) SET statement_timeout = '5s';
