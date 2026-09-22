-- ============================================================
-- MIGRACIÓN: channel_credentials.access_token -> nullable
-- ============================================================
-- Problema: en producción la tabla channel_credentials (creada del lado de
-- n8n) tiene access_token como NOT NULL. La rama de WhatsApp/Kontesta de
-- POST /canales/whatsapp/conectar guarda esa columna en NULL a propósito
-- (ver canales.py): con Kontesta no hay token por tenant que cifrar, solo
-- el phone_number_id de la línea. Contra el NOT NULL real, set_channel_credentials
-- revienta con:
--
--   asyncpg.exceptions.NotNullViolationError: null value in column
--   "access_token" of relation "channel_credentials" violates not-null
--   constraint
--
-- 00_base_local.sql (la reconstrucción local del esquema de n8n) ya modela
-- access_token como BYTEA nullable; lo que falta es alinear la base real.
-- No afecta a Facebook/Instagram, que siempre mandan un token no nulo.
--
-- Correr una sola vez por base de datos. Es idempotente.
-- ============================================================

ALTER TABLE channel_credentials
    ALTER COLUMN access_token DROP NOT NULL;
