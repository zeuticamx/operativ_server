-- ============================================================
-- NIVEL GERENCIA
-- ============================================================
-- Lista de correos con permisos superiores a los de cualquier rol de
-- tenant (owner/member/superadmin/vendedor en portal_users.role). No es
-- una cuenta de acceso aparte: no tiene password ni login propio. Quien
-- entra al portal sigue autenticándose contra portal_users como siempre;
-- si su correo también aparece acá, la sesión queda marcada como
-- gerencia y desbloquea lo que a futuro se reserve para ese nivel.
--
-- Por eso no hay tenant_id: gerencia no pertenece a un negocio, ve todos.
--
-- Alta manual por SQL por ahora (no hay endpoint todavía): sirve para
-- dejar sentada la tabla y la validación primero.
-- ============================================================

CREATE TABLE IF NOT EXISTS gerencia_users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       VARCHAR(255) NOT NULL UNIQUE,
    full_name   VARCHAR(255) NOT NULL,
    cargo       VARCHAR(100) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gerencia_users_email
    ON gerencia_users (LOWER(email));

-- ============================================================
-- insertar un usuario de gerencia (ejemplo)
--INSERT INTO gerencia_users (email, full_name, cargo) VALUES ('fernando.parra@operativai.com.mx', 'Fernando Ramon Parra Villanueva', 'Developer');