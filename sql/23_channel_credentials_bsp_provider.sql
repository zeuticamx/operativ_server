-- ============================================================
-- MIGRACIÓN: channel_credentials.bsp_provider
-- ============================================================
-- Por qué proveedor sale cada línea de WhatsApp. El workflow de n8n lo lee
-- (vía get_channel_credentials) para armar el envío:
--   'meta'     -> Graph API directo, Authorization: Bearer <token>
--   'neuroapi' -> POST {NEUROAPI_API_BASE_URL}/neuroapi/messaging/send,
--                 x-api-key: <token>
--   'kontesta' -> reservado; n8n todavía no envía por Kontesta
-- Default 'meta' para que las filas existentes sigan comportándose igual.
--
-- get_channel_credentials cambia su RETURNS TABLE, y eso no se puede con
-- CREATE OR REPLACE: hay que DROP + CREATE. Idempotente.
-- ============================================================

ALTER TABLE channel_credentials
    ADD COLUMN IF NOT EXISTS bsp_provider VARCHAR(20) NOT NULL DEFAULT 'meta';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'channel_credentials_bsp_provider_check'
    ) THEN
        ALTER TABLE channel_credentials
            ADD CONSTRAINT channel_credentials_bsp_provider_check
            CHECK (bsp_provider IN ('meta', 'neuroapi', 'kontesta'));
    END IF;
END $$;

DROP FUNCTION IF EXISTS get_channel_credentials(uuid, character varying);

CREATE FUNCTION get_channel_credentials(p_tenant_id uuid, p_channel character varying)
RETURNS TABLE(
    channel_type    character varying,
    phone_number_id character varying,
    page_id         character varying,
    ig_user_id      character varying,
    access_token    text,
    bsp_provider    character varying
)
LANGUAGE sql
SECURITY DEFINER
AS $function$
    SELECT
        c.channel_type,
        c.phone_number_id,
        c.page_id,
        c.ig_user_id,
        pgp_sym_decrypt(c.access_token, current_setting('app.cred_key')),
        c.bsp_provider
    FROM channel_credentials c
    WHERE c.tenant_id = p_tenant_id
      AND c.channel_type = p_channel
      AND c.is_active = true;
$function$;
