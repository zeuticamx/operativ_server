-- ============================================================
-- 66 · Stripe Connect: cobros propios de cada negocio
--
-- Cada tenant OWNER puede conectar su propia cuenta de Stripe (Express,
-- onboarding hospedado vía Account Links) y generar links de pago para
-- cobrar a SUS clientes (no a OperativAI). Cargo directo: el negocio
-- conectado es el comerciante, no la plataforma.
--
--   - tenant_stripe_connect: una fila por tenant, el estado de la cuenta
--     conectada, sincronizado por el webhook account.updated.
--   - tenant_cobros_connect: cada link de cobro generado y su resultado.
--     Lo genera el owner desde /cobros o el agente de IA como una tool más
--     durante una conversación (conversation_id queda NULL en el primer
--     caso, con valor en el segundo). application_fee_bps/_monto quedan
--     congelados al crearlo: cambiar el env var después no debe reescribir
--     el historial.
--
-- Idempotente (IF NOT EXISTS).
--     python aplicar_sql.py sql/66_stripe_connect.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS tenant_stripe_connect (
    tenant_id              UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    stripe_account_id      VARCHAR(255) NOT NULL,
    -- Los tres "requisitos cumplidos" que importan de account.updated:
    -- charges_enabled/payouts_enabled/details_submitted. charges_habilitado
    -- es la bandera que usa el portal (y la tool del agente) para decidir
    -- "ya puede cobrar" vs. "sigue en onboarding".
    charges_habilitado     BOOLEAN NOT NULL DEFAULT false,
    payouts_habilitado     BOOLEAN NOT NULL DEFAULT false,
    detalles_enviados      BOOLEAN NOT NULL DEFAULT false,
    -- Requisitos pendientes que Stripe reporta en
    -- requirements.currently_due; crudo (jsonb) porque la lista de claves
    -- la define Stripe, no nosotros, y cambia según el país de la cuenta.
    requisitos_pendientes  JSONB NOT NULL DEFAULT '[]'::jsonb,
    creado_en              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actualizado_en         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Único: así el webhook de account.updated encuentra la fila por
-- evento["account"] sin ambigüedad.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_stripe_connect_account
    ON tenant_stripe_connect (stripe_account_id);


CREATE TABLE IF NOT EXISTS tenant_cobros_connect (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- Quién lo generó: el owner desde /cobros (creado_por) o el agente
    -- dentro de una conversación (conversation_id). Nunca ambos a la vez,
    -- pero no hace falta un CHECK: el que no aplica simplemente queda NULL.
    creado_por                  UUID REFERENCES portal_users(id) ON DELETE SET NULL,
    conversation_id             UUID REFERENCES conversations(id) ON DELETE SET NULL,
    descripcion                 VARCHAR(255) NOT NULL,
    monto                       NUMERIC(12, 2) NOT NULL CHECK (monto > 0),
    moneda                      VARCHAR(3) NOT NULL,
    -- Product + Price de un solo uso creados sobre la cuenta conectada
    -- (header Stripe-Account), y el Payment Link que los envuelve.
    stripe_product_id           VARCHAR(255),
    stripe_price_id             VARCHAR(255),
    stripe_payment_link_id      VARCHAR(255),
    url_cobro                   TEXT,
    -- Congelado al crear el link: lo que de verdad se cobró, sin importar
    -- qué digan hoy las variables STRIPE_CONNECT_APPLICATION_FEE_*.
    application_fee_bps         INTEGER NOT NULL DEFAULT 0 CHECK (application_fee_bps >= 0),
    application_fee_monto       NUMERIC(12, 2),
    estado                      VARCHAR(20) NOT NULL DEFAULT 'activo'
                                CHECK (estado IN ('activo', 'pagado', 'desactivado')),
    stripe_checkout_session_id  VARCHAR(255),
    stripe_payment_intent_id    VARCHAR(255),
    -- Solo la usa la tool del agente (sección "tool del agente" del plan):
    -- sha256(tenant_id:conversation_id:mensaje_id), para que un reintento
    -- de webhook o un segundo turno del modelo no genere un segundo link.
    idempotency_key             VARCHAR(64),
    pagado_en                   TIMESTAMPTZ,
    creado_en                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actualizado_en              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tenant_cobros_connect_tenant
    ON tenant_cobros_connect (tenant_id, creado_en DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_cobros_connect_payment_link
    ON tenant_cobros_connect (stripe_payment_link_id)
    WHERE stripe_payment_link_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_cobros_connect_intent
    ON tenant_cobros_connect (stripe_payment_intent_id)
    WHERE stripe_payment_intent_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_cobros_connect_idempotencia
    ON tenant_cobros_connect (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
