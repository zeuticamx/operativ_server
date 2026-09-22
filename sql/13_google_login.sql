-- ============================================================
-- LOGIN CON GOOGLE (opcional, sin tocar el alta con correo)
-- ============================================================
-- POST /auth/google entra con el ID token que entrega el botón de Google
-- Identity Services. Si el correo ya existe (dado de alta con contraseña),
-- se vincula el google_id a esa misma fila en vez de duplicar cuenta; si
-- no existe, se crea un negocio nuevo igual que /auth/verificar pero sin
-- password_hash.
--
-- Por eso password_hash pasa a ser opcional: una cuenta que entró siempre
-- por Google no tiene contraseña que hashear. El login con correo sigue
-- exigiendo password_hash desde la app (routers/auth.py), acá solo se
-- relaja la restricción de la columna para permitir el otro camino.
--
-- Idempotente. Aplicar con aplicar_sql.py.
-- ============================================================

ALTER TABLE portal_users
    ALTER COLUMN password_hash DROP NOT NULL;

ALTER TABLE portal_users
    ADD COLUMN IF NOT EXISTS google_id VARCHAR(255);

-- Único pero solo entre filas que sí tienen google_id: dos cuentas de solo
-- correo con google_id NULL no deberían chocar entre sí.
CREATE UNIQUE INDEX IF NOT EXISTS idx_portal_users_google_id
    ON portal_users (google_id)
    WHERE google_id IS NOT NULL;
