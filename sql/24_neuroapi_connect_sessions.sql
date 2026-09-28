-- ============================================================
-- MIGRACIÓN: neuroapi_connect_sessions
-- ============================================================
-- Alta de WhatsApp Business vía NeuroAPI Connect Sessions (Embedded Signup).
--
-- El webhook de NeuroAPI llega sin JWT y sin tenant_id en la URL (se
-- autentica por firma HMAC, ver services/neuroapi_connect.py), así que hace
-- falta esta tabla para mapear de vuelta qué tenant inició cada session_id
-- cuando llega el callback. No guarda ningún secreto de larga vida por
-- tenant (el webhook_secret es uno solo para toda la plataforma,
-- NEUROAPI_CONNECT_WEBHOOK_SECRET), así que no hace falta pgcrypto acá.
-- ============================================================

CREATE TABLE IF NOT EXISTS neuroapi_connect_sessions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    session_id    TEXT NOT NULL,
    status        VARCHAR(20) NOT NULL DEFAULT 'pendiente',
    detalle       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'neuroapi_connect_sessions_status_check'
    ) THEN
        ALTER TABLE neuroapi_connect_sessions
            ADD CONSTRAINT neuroapi_connect_sessions_status_check
            CHECK (status IN ('pendiente', 'completado', 'fallido'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_neuroapi_connect_sessions_session_id
    ON neuroapi_connect_sessions (session_id);
