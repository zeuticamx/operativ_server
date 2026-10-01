-- ============================================================
-- MIGRACIÓN: set_whatsapp_neuroapi
-- ============================================================
-- Requiere 23_channel_credentials_bsp_provider.sql.
--
-- Problema: al terminar el Embedded Signup de NeuroAPI, el backend guardaba
-- la línea con set_channel_credentials(tenant, 'whatsapp', NULL, pnid, ...).
-- Eso dejaba la fila con:
--   - access_token NULL  -> el "Prepara envio" de n8n corta con "no tiene
--                           credenciales activas" y el agente nunca contesta
--   - bsp_provider 'meta' (el default) -> aunque hubiera token, n8n mandaría
--                           por Graph API en vez de por NeuroAPI
-- Además, el ON CONFLICT de set_channel_credentials pisa access_token con el
-- NULL, así que reconectar borraba cualquier token previo.
--
-- n8n manda por NeuroAPI con `x-api-key: <access_token>` cuando
-- bsp_provider = 'neuroapi' (ver migración 23), así que aquí se guarda la
-- API key de la plataforma (NEUROAPI_API_KEY) cifrada, igual que un token de
-- Meta. Función aparte y no un parámetro más en set_channel_credentials:
-- esa es del lado de n8n y no se le cambia la firma.
--
-- phone_number_id se reemplaza (no COALESCE): reconectar con otro número
-- tiene que dejar el número nuevo. page_id / ig_user_id no se tocan.
--
-- Idempotente.
-- ============================================================

CREATE OR REPLACE FUNCTION set_whatsapp_neuroapi(
    p_tenant_id         UUID,
    p_phone_number_id   VARCHAR,
    p_api_key           TEXT
)
RETURNS VOID
LANGUAGE sql
SECURITY DEFINER
AS $$
    INSERT INTO channel_credentials
        (tenant_id, channel_type, access_token, phone_number_id, bsp_provider, is_active, updated_at)
    VALUES (
        p_tenant_id,
        'whatsapp',
        pgp_sym_encrypt(p_api_key, current_setting('app.cred_key')),
        p_phone_number_id,
        'neuroapi',
        true,
        NOW()
    )
    ON CONFLICT (tenant_id, channel_type) DO UPDATE SET
        access_token    = EXCLUDED.access_token,
        phone_number_id = EXCLUDED.phone_number_id,
        bsp_provider    = 'neuroapi',
        is_active       = true,
        updated_at      = NOW();
$$;
