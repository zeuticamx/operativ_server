-- ============================================================
-- ACEPTACIÓN DE TÉRMINOS Y CONDICIONES
-- ============================================================
-- Evidencia de que el dueño de la cuenta aceptó las Condiciones del
-- servicio y el Aviso de privacidad (frontend: /condiciones y /privacidad).
--
-- Un timestamp y no un booleano: NULL = no hay evidencia, y el valor dice
-- además CUÁNDO. La hora la escribe el servidor (NOW()), nunca el cliente.
-- `terminos_version` dice QUÉ texto estaba vigente (settings.TERMINOS_VERSION),
-- para el día que cambien los términos y haya que saber quién aceptó cuál.
--
-- Las cuentas que ya existían quedan en NULL a propósito: no aceptaron
-- explícitamente y no se inventa. En su próximo ingreso (POST /auth/login o
-- /auth/google) el backend responde 428 hasta que acepten, y ahí se llena.
--
-- email_verifications guarda la aceptación del paso 1 (/registro) hasta que
-- el paso 2 (/verificar) crea la cuenta y la copia a portal_users.
--
-- Idempotente. Aplicar con aplicar_sql.py.
-- ============================================================

ALTER TABLE portal_users
    ADD COLUMN IF NOT EXISTS terminos_aceptados_en TIMESTAMPTZ;

ALTER TABLE portal_users
    ADD COLUMN IF NOT EXISTS terminos_version VARCHAR(20);

ALTER TABLE email_verifications
    ADD COLUMN IF NOT EXISTS terminos_aceptados_en TIMESTAMPTZ;

ALTER TABLE email_verifications
    ADD COLUMN IF NOT EXISTS terminos_version VARCHAR(20);
