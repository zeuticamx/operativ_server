-- 72 · set_channel_credentials debe fijar bsp_provider = 'meta'
--
-- Simétrico a set_instagram_neuroapi (que sí fija bsp_provider='neuroapi'
-- al conectar por NeuroAPI, migrando el canal fuera de Meta -- ver
-- tests/test_instagram_neuroapi.py::test_una_cuenta_conectada_por_oauth_de_meta_se_migra_a_neuroapi).
-- set_channel_credentials (sql/71), el lado Meta de la misma moneda, nunca
-- tocaba bsp_provider: si un canal ya tenía bsp_provider='neuroapi' de una
-- conexión anterior, reconectar por Meta dejaba el valor viejo intacto y
-- envio.py seguía mandando por NeuroAPI con credenciales de Meta -- el
-- proveedor de NeuroAPI respondía "MISSING_RECIPIENT" porque esperaba su
-- propio formato de payload, no el de Graph.
--
-- La última conexión explícita gana, sea cual sea el proveedor.

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
        (tenant_id, channel_type, access_token, phone_number_id, page_id, ig_user_id, meta_app_id, bsp_provider, is_active, updated_at)
    VALUES (
        p_tenant_id,
        p_channel_type,
        pgp_sym_encrypt(p_access_token, current_setting('app.cred_key')),
        p_phone_number_id,
        p_page_id,
        p_ig_user_id,
        p_meta_app_id,
        'meta',
        true,
        NOW()
    )
    ON CONFLICT (tenant_id, channel_type) DO UPDATE SET
        access_token      = EXCLUDED.access_token,
        phone_number_id   = COALESCE(EXCLUDED.phone_number_id, channel_credentials.phone_number_id),
        page_id           = COALESCE(EXCLUDED.page_id, channel_credentials.page_id),
        ig_user_id        = COALESCE(EXCLUDED.ig_user_id, channel_credentials.ig_user_id),
        meta_app_id       = COALESCE(EXCLUDED.meta_app_id, channel_credentials.meta_app_id),
        bsp_provider      = 'meta',
        is_active         = true,
        updated_at        = NOW();
$$;
