-- ============================================================
-- CONFIGURACIÓN DE ETAPAS DEL EMBUDO (panel del dueño)
-- ============================================================
-- OJO: esto es una capa de configuración, NO el motor que valida los
-- movimientos del embudo real. `client_pipeline.estado` sigue restringido
-- al CHECK de 06_vendedores.sql y las transiciones las sigue validando
-- services/pipeline_estados.py (hardcodeado, sin I/O a propósito). Esta
-- tabla es donde el dueño puede mirar y anotar cómo le gustaría que
-- funcionara su embudo; conectar eso al motor real es trabajo aparte.
--
-- Por eso el `nombre` de una etapa no tiene FK hacia ningún lado: es
-- texto libre. El único cruce con la realidad es de solo lectura (ver
-- `leads_activos` en el router), comparando por nombre contra
-- `client_pipeline.estado` para poder avisar antes de borrar una etapa
-- que coincide con una etapa real en uso.
-- ============================================================

CREATE TABLE IF NOT EXISTS pipeline_etapas (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    nombre      VARCHAR(50) NOT NULL,
    color       VARCHAR(7) NOT NULL DEFAULT '#3987E5'
        CHECK (color ~ '^#[0-9A-Fa-f]{6}$'),
    descripcion VARCHAR(500),
    orden       INT NOT NULL DEFAULT 0,
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Un nombre no se repite dentro del mismo tenant. Sí puede repetirse
    -- entre tenants distintos.
    UNIQUE (tenant_id, nombre)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_etapas_tenant_orden
    ON pipeline_etapas (tenant_id, orden);


CREATE TABLE IF NOT EXISTS pipeline_transiciones (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    etapa_origen_id     UUID NOT NULL REFERENCES pipeline_etapas(id) ON DELETE CASCADE,
    etapa_destino_id    UUID NOT NULL REFERENCES pipeline_etapas(id) ON DELETE CASCADE,
    permitida           BOOLEAN NOT NULL DEFAULT true,
    -- Una sola fila por par ordenado; el POST hace upsert sobre esto.
    UNIQUE (tenant_id, etapa_origen_id, etapa_destino_id),
    CHECK (etapa_origen_id <> etapa_destino_id)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_transiciones_tenant
    ON pipeline_transiciones (tenant_id);
