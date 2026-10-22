-- ============================================================
-- 67 · Vista de gerencia a las conversaciones de un negocio
--
-- El equipo de plataforma (gerencia_users) abre las conversaciones de un
-- número de WhatsApp concreto (config GERENCIA_NUMERO_PHONE_NUMBER_ID) desde
-- /gerencia/conversaciones. El dueño de ese negocio puede revocar esa vista
-- desde el portal; esta tabla guarda su decisión.
--
--   - Fila ausente = vista permitida (el comportamiento de siempre).
--   - revocada = true cierra con 403 las rutas de conversaciones de ese negocio
--     en /api/gerencia/tenants/{id}/conversaciones*.
--
-- Idempotente (IF NOT EXISTS).
--
--     python aplicar_sql.py sql/67_vista_gerencia_conversaciones.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS vista_gerencia_conversaciones (
    tenant_id       UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    revocada        BOOLEAN NOT NULL DEFAULT false,
    decidida_en     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decidida_por    UUID REFERENCES portal_users(id) ON DELETE SET NULL
);
