-- ============================================================
-- 64 · Modo coexistencia de WhatsApp (app móvil + API)
-- ============================================================
-- Al vincular un número por NeuroAPI Connect Sessions (Embedded Signup v4) el
-- negocio elige cómo lo quiere:
--
--   standard      Solo API: el número opera 100% por la integración y el agente.
--   coexistence   La línea sigue viva en la app de WhatsApp Business del móvil
--                 y en la API a la vez. Lo que una persona escribe desde el
--                 móvil llega por el webhook como un "eco"
--                 (`smb_message_echoes`) y se sincroniza en solo lectura.
--
-- Qué cambia:
--   planes.coexistencia_activa            gerencia decide qué planes la incluyen
--   neuroapi_connect_sessions.connection_mode   lo que se pidió al abrir el trámite
--   agentes_canal.connection_mode         cómo quedó vinculado el número
--   messages.channel_msg_id               wamid del canal (dedupe de ecos)
--   messages.desde_app_movil              mensaje escrito en la app móvil
--   coexistencia_permitida(tenant)        si el plan vigente del negocio la incluye
--
-- `messages.channel_msg_id` ya lo usa el agente en código
-- (services/agente/ingreso.py) para deduplicar reintentos del proveedor, pero
-- ninguna migración del repositorio la creaba: aquí se declara de forma
-- idempotente. Si la base ya tiene la columna y su índice único, no cambia nada
-- (el índice de abajo es equivalente y solo se crea si falta su nombre).
--
-- Idempotente: aplicar_sql.py reaplica los archivos.
--
--     python aplicar_sql.py sql/64_coexistencia_whatsapp.sql
-- ============================================================

ALTER TABLE planes ADD COLUMN IF NOT EXISTS coexistencia_activa BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE neuroapi_connect_sessions
    ADD COLUMN IF NOT EXISTS connection_mode VARCHAR(12) NOT NULL DEFAULT 'standard';

ALTER TABLE agentes_canal
    ADD COLUMN IF NOT EXISTS connection_mode VARCHAR(12) NOT NULL DEFAULT 'standard';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'neuroapi_connect_sessions_connection_mode_check') THEN
        ALTER TABLE neuroapi_connect_sessions
            ADD CONSTRAINT neuroapi_connect_sessions_connection_mode_check
            CHECK (connection_mode IN ('standard', 'coexistence'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'agentes_canal_connection_mode_check') THEN
        ALTER TABLE agentes_canal
            ADD CONSTRAINT agentes_canal_connection_mode_check
            CHECK (connection_mode IN ('standard', 'coexistence'));
    END IF;
END
$$;


-- ------------------------------------------------------------
-- Mensajes: id del canal y origen móvil
-- ------------------------------------------------------------
ALTER TABLE messages ADD COLUMN IF NOT EXISTS channel_msg_id TEXT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS desde_app_movil BOOLEAN NOT NULL DEFAULT false;

-- Un wamid solo puede estar una vez por conversación: es la deduplicación de
-- los ecos (y de los reintentos del proveedor).
CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_conversation_channel_msg
    ON messages (conversation_id, channel_msg_id)
    WHERE channel_msg_id IS NOT NULL;


-- ------------------------------------------------------------
-- ¿El plan vigente del negocio incluye la coexistencia?
-- ------------------------------------------------------------
-- Usa el mismo plan que `limites_efectivos` (prueba válida → suscripción vigente
-- → plan activo más bajo). Sin plan que aplique (piloto sin plan asociado o
-- ningún plan activo) no hay topes en ninguna otra regla, así que tampoco aquí.
CREATE OR REPLACE FUNCTION coexistencia_permitida(p_tenant UUID, p_ahora TIMESTAMPTZ DEFAULT NOW())
RETURNS BOOLEAN
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE(
        (SELECT p.coexistencia_activa
           FROM limites_efectivos(p_tenant, p_ahora) l
           JOIN planes p ON p.nombre = l.plan),
        true
    );
$$;
