-- ============================================================
-- 63 · Plantillas de WhatsApp por negocio (caché local del catálogo de Meta)
--
-- Cada negocio crea, consulta y elimina sus plantillas por la API de
-- plantillas de NeuroAPI (`/messaging/templates`). Esta tabla es solo la
-- CACHÉ que la UI lee sin ir a Meta en cada vista: la fuente de verdad es
-- Meta. El estado se refresca por "Sincronizar" y por el evento
-- `message_template_status_update` del webhook de NeuroAPI.
--
-- No confundir con `plantillas_whatsapp_globales` (sql/49): esas son las dos
-- plantillas de los avisos de cita de la cuenta global, sin tenant_id.
--
--     python aplicar_sql.py sql/63_plantillas_whatsapp_tenant.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS plantillas_whatsapp_tenant (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    nombre            VARCHAR(512) NOT NULL,
    idioma            VARCHAR(10) NOT NULL DEFAULT 'es_MX',
    categoria         VARCHAR(20) NOT NULL
                      CHECK (categoria IN ('UTILITY', 'MARKETING', 'AUTHENTICATION')),
    estado            VARCHAR(20) NOT NULL DEFAULT 'PENDING'
                      CHECK (estado IN (
                          'PENDING', 'APPROVED', 'REJECTED', 'PAUSED', 'DISABLED'
                      )),
    motivo_rechazo    TEXT,
    componentes       JSONB NOT NULL DEFAULT '[]'::jsonb,
    meta_template_id  VARCHAR(64),
    waba_id           VARCHAR(64),
    creada_por        UUID REFERENCES portal_users(id) ON DELETE SET NULL,
    creada_en         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actualizada_en    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sincronizada_en   TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_plantillas_tenant_nombre_idioma
    ON plantillas_whatsapp_tenant (tenant_id, nombre, idioma);

CREATE INDEX IF NOT EXISTS ix_plantillas_tenant_estado
    ON plantillas_whatsapp_tenant (tenant_id, estado);

-- Para resolver el tenant de un evento de Meta (solo trae nombre/idioma/id).
CREATE INDEX IF NOT EXISTS ix_plantillas_tenant_evento
    ON plantillas_whatsapp_tenant (nombre, idioma);
