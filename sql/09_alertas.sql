-- ============================================================
-- ALERTAS (notificaciones de eventos del tenant)
-- ============================================================
-- Historial de eventos (nuevo lead, cambio de etapa, cierre, etc). El
-- WebSocket (websocket.py) las transmite en vivo; esta tabla es la
-- bitácora que queda si nadie estaba conectado en el momento.

CREATE TABLE IF NOT EXISTS alertas (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tipo        VARCHAR(30) NOT NULL
        CHECK (tipo IN ('nuevo_lead', 'cambio_etapa', 'sin_actividad', 'cuota_excedida', 'cierre')),
    titulo      VARCHAR(200) NOT NULL,
    mensaje     VARCHAR(1000) NOT NULL,
    datos       JSONB NOT NULL DEFAULT '{}'::jsonb,
    leido       BOOLEAN NOT NULL DEFAULT false,
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- El panel siempre pide "las últimas del tenant" o "las no leídas del
-- tenant": ambos casos filtran por tenant_id primero.
CREATE INDEX IF NOT EXISTS idx_alertas_tenant_creado
    ON alertas (tenant_id, creado_en DESC);

CREATE INDEX IF NOT EXISTS idx_alertas_tenant_no_leidas
    ON alertas (tenant_id)
    WHERE leido = false;
