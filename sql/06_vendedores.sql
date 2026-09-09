-- ============================================================
-- MÓDULO DE GESTIÓN DE VENDEDORES (mini-CRM)
-- ============================================================
-- Opcional por tenant y totalmente independiente del agente de IA:
-- un tenant puede tener solo el agente, solo este módulo, o los dos.
-- El interruptor vive en tenant_servicios.
--
-- Nombres que se confunden fácil (ver README):
--   users         → clientes finales que escriben por WhatsApp/IG/FB
--   portal_users  → dueños/gerencia que entran al portal
--   vendedores    → personal de venta del tenant (esta tabla). Puede o no
--                   tener cuenta de portal: portal_user_id es opcional.
--
-- Ninguna tabla de n8n se toca; solo se referencian sus FKs.
-- ============================================================


-- ------------------------------------------------------------
-- Qué servicios tiene contratados/activos cada tenant
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenant_servicios (
    tenant_id                   UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    agente_ia_activo            BOOLEAN NOT NULL DEFAULT true,
    gestion_vendedores_activo   BOOLEAN NOT NULL DEFAULT false,
    actualizado_en              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Los tenants que ya existen se quedan exactamente como estaban: agente
-- encendido, módulo de vendedores apagado. Sin esto, el primer tenant que
-- pasara por el endpoint de eventos leería una fila inexistente.
INSERT INTO tenant_servicios (tenant_id)
SELECT id FROM tenants
ON CONFLICT (tenant_id) DO NOTHING;


-- ------------------------------------------------------------
-- Vendedores del tenant
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vendedores (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- Opcional: un vendedor puede existir sin cuenta de portal (lo da de
    -- alta gerencia y solo recibe leads), o tener una para entrar a ver
    -- su propia cartera.
    portal_user_id  UUID REFERENCES portal_users(id) ON DELETE SET NULL,
    nombre          TEXT NOT NULL,
    telefono        TEXT,
    activo          BOOLEAN NOT NULL DEFAULT true,
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- El orden (tenant_id, activo) es el de la consulta de asignación, que es
-- la que corre en cada mensaje entrante.
CREATE INDEX IF NOT EXISTS idx_vendedores_tenant_activo
    ON vendedores (tenant_id, activo);

-- Un portal_user no puede estar detrás de dos vendedores del mismo tenant:
-- si no, "mi cartera" sería ambigua. Parcial porque NULL es lo normal aquí.
CREATE UNIQUE INDEX IF NOT EXISTS idx_vendedores_portal_user
    ON vendedores (tenant_id, portal_user_id)
    WHERE portal_user_id IS NOT NULL;


-- ------------------------------------------------------------
-- Estrategia de reparto de leads, por tenant
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenant_vendedor_config (
    tenant_id                   UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    estrategia_asignacion       TEXT NOT NULL DEFAULT 'carga'
        CHECK (estrategia_asignacion IN ('carga', 'round_robin', 'manual')),
    -- Puntero del round-robin. ON DELETE SET NULL y no CASCADE: si se borra
    -- el vendedor, la rueda arranca de nuevo en vez de tumbar la config.
    ultimo_vendedor_asignado_id UUID REFERENCES vendedores(id) ON DELETE SET NULL
);


-- ------------------------------------------------------------
-- Pipeline: un cliente final (users) dentro del embudo de venta
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS client_pipeline (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- NULL es un estado legítimo y esperado: tenant con el módulo activo
    -- pero sin vendedores activos todavía. El lead se guarda igual.
    vendedor_id     UUID REFERENCES vendedores(id) ON DELETE SET NULL,
    estado          TEXT NOT NULL DEFAULT 'nuevo'
        CHECK (estado IN ('nuevo', 'contactado', 'en_seguimiento',
                          'cotizado', 'negociacion', 'ganado', 'perdido')),
    monto_estimado  NUMERIC(12,2),
    motivo_perdida  TEXT,
    actualizado_en  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Un cliente = una fila de embudo por tenant. Es lo que hace que
    -- reasignar sobrescriba en vez de duplicar.
    UNIQUE (tenant_id, user_id)
);

-- Cartera de un vendedor y cálculo de carga para la estrategia 'carga'.
CREATE INDEX IF NOT EXISTS idx_client_pipeline_vendedor
    ON client_pipeline (vendedor_id, estado);

-- Vista de gerencia y métricas por etapa.
CREATE INDEX IF NOT EXISTS idx_client_pipeline_tenant_estado
    ON client_pipeline (tenant_id, estado);


-- ------------------------------------------------------------
-- Bitácora del embudo
-- ------------------------------------------------------------
-- Guarda dos cosas distintas, que se distinguen por sus columnas:
--   cambio de estado → estado_anterior IS DISTINCT FROM estado_nuevo
--   asignación       → estado_anterior = estado_nuevo (el estado no se
--                      movió; lo que cambió es el vendedor)
-- Se mezclan a propósito en una sola tabla para tener la línea de tiempo
-- completa de un lead en orden, sin unir dos bitácoras.
CREATE TABLE IF NOT EXISTS pipeline_historial (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_pipeline_id  UUID NOT NULL REFERENCES client_pipeline(id) ON DELETE CASCADE,
    estado_anterior     TEXT,
    estado_nuevo        TEXT NOT NULL,
    vendedor_id         UUID REFERENCES vendedores(id) ON DELETE SET NULL,
    nota                TEXT,
    creado_en           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- La métrica de "tiempo promedio por etapa" recorre la bitácora de cada
-- lead en orden cronológico; este índice es exactamente ese acceso.
CREATE INDEX IF NOT EXISTS idx_pipeline_historial_lead
    ON pipeline_historial (client_pipeline_id, creado_en);

CREATE INDEX IF NOT EXISTS idx_pipeline_historial_vendedor
    ON pipeline_historial (vendedor_id, creado_en DESC);
