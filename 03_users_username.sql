-- ============================================================
-- MIGRACIÓN: users.instagram_username
-- ============================================================
-- La API de mensajería de Meta entrega, para cada contacto, solo un id
-- numérico interno de la app (PSID en Messenger, IGSID en Instagram).
-- Con ese id se puede consultar el perfil:
--
--   Instagram: GET /{IGSID}?fields=name,username  -> nombre Y @usuario
--   Messenger: GET /{PSID}?fields=first_name,last_name -> solo nombre
--
-- El nombre va a `display_name`, que ya existe. El @usuario de Instagram
-- no tiene dónde guardarse, y de ahí esta columna. Messenger no expone
-- ningún username, así que la columna queda en NULL para ese canal.
--
-- WhatsApp no tiene API de perfil: el nombre llega dentro del propio
-- webhook y lo escribe n8n, no este backend.
--
-- Aditiva y anulable: no rompe los INSERT que ya hace n8n sobre `users`.
-- Correr una sola vez por base de datos. Es idempotente.
-- ============================================================

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS instagram_username VARCHAR(255);
