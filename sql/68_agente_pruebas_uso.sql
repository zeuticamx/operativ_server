-- ============================================================
-- 68 · Cupo diario del modo "Probar agente"
--
-- El dueño prueba su agente desde /agente sin escribirle a un cliente real.
-- Cada mensaje de prueba cuesta tokens que absorbe la plataforma, así que hay
-- un tope diario por negocio (services/agente_tester.py, LIMITE_DIARIO).
--
--   - Una fila por negocio y día (día en America/Mexico_City).
--   - La conversación de prueba NO se guarda: vive en el navegador.
--
-- Idempotente (IF NOT EXISTS).
--
--     python aplicar_sql.py sql/68_agente_pruebas_uso.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS agente_pruebas_uso (
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    dia         DATE NOT NULL,
    mensajes    INTEGER NOT NULL DEFAULT 0 CHECK (mensajes >= 0),
    PRIMARY KEY (tenant_id, dia)
);
