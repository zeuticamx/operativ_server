-- ============================================================
-- 69 · Instagram Direct vía NeuroAPI Connect Sessions
-- ============================================================
-- Requiere 23_channel_credentials_bsp_provider.sql y
-- 24_neuroapi_connect_sessions.sql.
--
-- El negocio vincula su cuenta profesional de Instagram con el mismo trámite
-- que WhatsApp (Connect Session de NeuroAPI, `service_type` =
-- 'instagram_messaging'). El webhook es el mismo, así que hay que saber de qué
-- canal es cada sesión al recibir `instagram.connected`.
--
-- Qué cambia:
--   neuroapi_connect_sessions.canal   'whatsapp' (todas las sesiones previas) | 'instagram'
--   set_instagram_neuroapi(...)       guarda la cuenta como canal 'instagram' con
--                                     bsp_provider 'neuroapi' y la API key cifrada
--
-- set_instagram_neuroapi deja page_id en NULL a propósito. Con NeuroAPI el
-- envío no necesita página (va por recipient_id), y una cuenta que antes se
-- conectó por OAuth de Meta deja así de resolverse por su page_id: si su
-- webhook de Meta siguiera suscrito, el cliente recibiría dos respuestas.
-- ig_user_id es el `instagramAccountId` de NeuroAPI, que es el `entry[].id`
-- de los mensajes y lo que lee `resuelve_tenant('instagram', ...)`.
--
-- Idempotente:
--
--     python aplicar_sql.py sql/69_instagram_neuroapi.sql
-- ============================================================

ALTER TABLE neuroapi_connect_sessions
    ADD COLUMN IF NOT EXISTS canal VARCHAR(20) NOT NULL DEFAULT 'whatsapp';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'neuroapi_connect_sessions_canal_check') THEN
        ALTER TABLE neuroapi_connect_sessions
            ADD CONSTRAINT neuroapi_connect_sessions_canal_check
            CHECK (canal IN ('whatsapp', 'instagram'));
    END IF;
END
$$;

-- El portal pide la última sesión de Instagram del negocio al volver de Meta
-- (GET /api/canales/instagram/neuroapi/estado), sin session_id.
CREATE INDEX IF NOT EXISTS idx_neuroapi_connect_sessions_tenant_canal
    ON neuroapi_connect_sessions (tenant_id, canal, created_at DESC);


CREATE OR REPLACE FUNCTION set_instagram_neuroapi(
    p_tenant_id   UUID,
    p_ig_user_id  VARCHAR,
    p_api_key     TEXT
)
RETURNS VOID
LANGUAGE sql
SECURITY DEFINER
AS $$
    INSERT INTO channel_credentials
        (tenant_id, channel_type, access_token, ig_user_id, page_id, phone_number_id,
         bsp_provider, is_active, updated_at)
    VALUES (
        p_tenant_id,
        'instagram',
        pgp_sym_encrypt(p_api_key, current_setting('app.cred_key')),
        p_ig_user_id,
        NULL,
        NULL,
        'neuroapi',
        true,
        NOW()
    )
    ON CONFLICT (tenant_id, channel_type) DO UPDATE SET
        access_token    = EXCLUDED.access_token,
        ig_user_id      = EXCLUDED.ig_user_id,
        page_id         = NULL,
        phone_number_id = NULL,
        bsp_provider    = 'neuroapi',
        is_active       = true,
        updated_at      = NOW();
$$;
