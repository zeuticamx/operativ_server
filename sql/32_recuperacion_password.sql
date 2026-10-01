-- ============================================================
-- RECUPERACIÓN DE CONTRASEÑA POR CÓDIGO
-- ============================================================
-- POST /auth/recuperar/solicitar   -> manda un código de 6 dígitos al correo
-- POST /auth/recuperar/restablecer -> con el código, cambia la contraseña
--
-- Tabla propia y no email_verifications: aquella guarda altas PENDIENTES
-- (PK = correo, nombre_negocio obligatorio); esta es de cuentas que ya
-- existen, así que la clave es el portal_user. Un código activo por usuario:
-- pedir otro pisa la fila y el anterior deja de servir en ese momento.
--
-- Vigencia ESTRICTA de 5 minutos. Tanto la emisión (expires_at = NOW() +
-- 5 min) como la comprobación (expires_at > NOW()) usan el reloj de
-- Postgres, nunca el del proceso de la app: un desfase entre ambos relojes
-- no puede estirar ni acortar la ventana.
--
-- Además, `credenciales_cambiadas_en` en portal_users: los JWT emitidos
-- antes de ese instante dejan de valer (deps.usuario_actual y /auth/refresh).
-- Sin esto, cambiar la contraseña no expulsaba a quien tuviera una sesión
-- robada: el refresh token dura 30 días.
--
-- Idempotente. Aplicar con aplicar_sql.py.
-- ============================================================

CREATE TABLE IF NOT EXISTS password_resets (
    portal_user_id  UUID PRIMARY KEY REFERENCES portal_users(id) ON DELETE CASCADE,

    -- Hash argon2 del código (security.hash_password), igual que en
    -- email_verifications: quien lea la tabla no se lleva códigos usables.
    code_hash       TEXT NOT NULL,

    -- Intentos de canje. Al llegar al tope el código se descarta.
    attempts        SMALLINT NOT NULL DEFAULT 0,

    expires_at      TIMESTAMPTZ NOT NULL,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Limpieza de vencidos en cada solicitud (DELETE ... WHERE expires_at < NOW()).
CREATE INDEX IF NOT EXISTS idx_password_resets_expires
    ON password_resets (expires_at);

ALTER TABLE portal_users
    ADD COLUMN IF NOT EXISTS credenciales_cambiadas_en TIMESTAMPTZ;
