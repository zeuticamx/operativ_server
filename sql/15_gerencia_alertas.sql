-- ============================================================
-- ALERTAS DE PLATAFORMA (para el nivel gerencia)
-- ============================================================
-- No confundir con `alertas` (09_alertas.sql): aquellas son de UN negocio y
-- las ve su dueño en la campana del portal. Estas son del equipo de
-- OperativAI y hablan de un negocio sin que el negocio las vea — "este
-- tenant multiplicó por 8 su consumo de ayer" no es algo que le toque leer
-- al cliente.
--
-- Hoy las escribe solo jobs/gerencia_background.py (consumo anómalo). El
-- `tipo` es texto libre y no CHECK para que el siguiente detector no
-- necesite migración.
-- ============================================================

CREATE TABLE IF NOT EXISTS gerencia_alertas (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    tipo VARCHAR(60) NOT NULL,
    -- Sin FK: igual que la bitácora, el rastro sobrevive al tenant.
    tenant_id UUID,

    titulo  TEXT  NOT NULL,
    detalle JSONB NOT NULL DEFAULT '{}'::jsonb,

    creada_en TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- NULL = abierta. Se cierra a mano desde el panel de salud: una alerta
    -- de gasto que se cierra sola cuando el consumo baja esconde justo el
    -- incidente que alguien tenía que mirar.
    revisada_en  TIMESTAMPTZ,
    revisada_por VARCHAR(255)
);

-- Una sola alerta ABIERTA por (tipo, tenant). El job corre cada hora y,
-- mientras el pico dure, vuelve a detectar lo mismo: sin esto serían 24
-- alertas y 24 correos por día por el mismo incidente. Cuando alguien la
-- marca revisada, el índice la suelta y un pico nuevo puede abrir otra.
CREATE UNIQUE INDEX IF NOT EXISTS idx_gerencia_alertas_una_abierta
    ON gerencia_alertas (tipo, tenant_id)
    WHERE revisada_en IS NULL;

CREATE INDEX IF NOT EXISTS idx_gerencia_alertas_fecha
    ON gerencia_alertas (creada_en DESC);
