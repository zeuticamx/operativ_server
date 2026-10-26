-- 71 · Vincula channel_credentials con meta_apps automáticamente
--
-- obtiene_app_secret (sql/53) descifra el app_secret de Meta a partir de
-- channel_credentials.meta_app_id -> meta_apps.id. Pero ningún código
-- Python lo llenaba: routers/canales.py llamaba set_channel_credentials sin
-- ese dato, así que todo canal de Facebook/Instagram quedaba sin app_secret
-- vinculado y el webhook rechazaba cada mensaje real con "Sin app_secret
-- para el remitente" (403) -- nunca se había notado porque Facebook/
-- Instagram no habían recibido tráfico real hasta ahora.
--
-- Esta migración:
--   1. Agrega la columna si faltara (ya existe en producción, administrada
--      por n8n; en local/dev puede no existir todavía).
--   2. resolver_meta_app: upsert de la fila de meta_apps de la plataforma
--      (una por app_id_meta), cifrando el secreto con app.cred_key. El
--      backend la llama con settings.META_APP_ID/META_APP_SECRET -- el
--      secreto nunca queda en texto plano en ningún archivo versionado.
--   3. set_channel_credentials: nuevo parámetro p_meta_app_id (con DEFAULT
--      NULL, así que las llamadas existentes sin ese argumento no se rompen).

ALTER TABLE channel_credentials
    ADD COLUMN IF NOT EXISTS meta_app_id INTEGER REFERENCES meta_apps(id);

CREATE OR REPLACE FUNCTION resolver_meta_app(
    p_app_id_meta TEXT,
    p_app_secret  TEXT
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_id INTEGER;
BEGIN
    SELECT id INTO v_id FROM meta_apps WHERE app_id_meta = p_app_id_meta;
    IF v_id IS NULL THEN
        INSERT INTO meta_apps (nombre, app_id_meta, app_secret_cifrado, creado_en)
        VALUES ('OperativAI', p_app_id_meta,
                pgp_sym_encrypt(p_app_secret, current_setting('app.cred_key')), NOW())
        RETURNING id INTO v_id;
    ELSE
        UPDATE meta_apps
           SET app_secret_cifrado = pgp_sym_encrypt(p_app_secret, current_setting('app.cred_key'))
         WHERE id = v_id;
    END IF;
    RETURN v_id;
END;
$$;

CREATE OR REPLACE FUNCTION set_channel_credentials(
    p_tenant_id         UUID,
    p_channel_type      VARCHAR,
    p_access_token      TEXT,
    p_phone_number_id   VARCHAR DEFAULT NULL,
    p_page_id           VARCHAR DEFAULT NULL,
    p_ig_user_id        VARCHAR DEFAULT NULL,
    p_meta_app_id       INTEGER DEFAULT NULL
)
RETURNS VOID
LANGUAGE sql
SECURITY DEFINER
AS $$
    INSERT INTO channel_credentials
        (tenant_id, channel_type, access_token, phone_number_id, page_id, ig_user_id, meta_app_id, is_active, updated_at)
    VALUES (
        p_tenant_id,
        p_channel_type,
        pgp_sym_encrypt(p_access_token, current_setting('app.cred_key')),
        p_phone_number_id,
        p_page_id,
        p_ig_user_id,
        p_meta_app_id,
        true,
        NOW()
    )
    ON CONFLICT (tenant_id, channel_type) DO UPDATE SET
        access_token      = EXCLUDED.access_token,
        phone_number_id   = COALESCE(EXCLUDED.phone_number_id, channel_credentials.phone_number_id),
        page_id           = COALESCE(EXCLUDED.page_id, channel_credentials.page_id),
        ig_user_id        = COALESCE(EXCLUDED.ig_user_id, channel_credentials.ig_user_id),
        meta_app_id        = COALESCE(EXCLUDED.meta_app_id, channel_credentials.meta_app_id),
        is_active         = true,
        updated_at        = NOW();
$$;
