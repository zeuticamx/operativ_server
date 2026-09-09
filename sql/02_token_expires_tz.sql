-- ============================================================
-- MIGRACIÓN: meta_connections.token_expires_at -> TIMESTAMPTZ
-- ============================================================
-- Problema: la app manda un datetime con timezone (UTC), porque el
-- vencimiento del token es un instante absoluto que define Meta. Contra
-- una columna TIMESTAMP (sin huso) asyncpg falla directamente con:
--
--   asyncpg.exceptions.DataError: invalid input for query argument $4
--   (can't subtract offset-naive and offset-aware datetimes)
--
-- Quitarle el huso del lado de Python no alcanza: NOW() escribe hora
-- local del servidor de BD, así que si la app guardara UTC "pelado" y la
-- BD no está en UTC, el valor quedaría desfasado y el job que renueve el
-- token antes de que venza se dispararía tarde.
--
-- Correr una sola vez por base de datos. Es idempotente.
-- ============================================================

-- 1. Las funciones se borran antes de recrearlas: cambiar el tipo de un
--    parámetro crea una sobrecarga nueva en vez de reemplazar, y cambiar
--    el tipo de retorno directamente no está permitido.
DROP FUNCTION IF EXISTS set_meta_connection(UUID, VARCHAR, TEXT, TIMESTAMP, TEXT[]);
DROP FUNCTION IF EXISTS set_meta_connection(UUID, VARCHAR, TEXT, TIMESTAMPTZ, TEXT[]);
DROP FUNCTION IF EXISTS get_meta_connection(UUID);

-- 2. La columna. Los valores que hubiera se interpretan como UTC, que es
--    lo que la app venía calculando (datetime.now(timezone.utc)).
--
--    El IF es lo que hace segura la re-ejecución: sobre una columna que YA
--    es timestamptz, "AT TIME ZONE 'UTC'" devuelve un timestamp sin huso, y
--    al reasignarlo Postgres lo reinterpreta en el huso de la sesión,
--    corriendo las fechas. Sin esta guarda, correr el script dos veces
--    desplazaría los vencimientos.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'meta_connections'
          AND column_name = 'token_expires_at'
          AND data_type = 'timestamp without time zone'
    ) THEN
        ALTER TABLE meta_connections
            ALTER COLUMN token_expires_at TYPE TIMESTAMPTZ
            USING token_expires_at AT TIME ZONE 'UTC';
    END IF;
END $$;

-- 3. Funciones de nuevo, ya con TIMESTAMPTZ.
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
