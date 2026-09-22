-- ============================================================
-- AUDITORÍA DE PLATAFORMA (nivel gerencia)
-- ============================================================
-- Lo que necesita el equipo de OperativAI para mirar el servicio completo,
-- no un negocio: qué tenants hay, en qué estado están, cuántos tokens de
-- IA consumen y quién de gerencia tocó qué.
--
-- El nivel gerencia ya existe desde 10_gerencia.sql (tabla gerencia_users
-- + deps.gerencia_plataforma_actual); ahí quedó dicho que era para "lo que
-- a futuro se reserve para ese nivel". Esto es ese futuro.
--
-- OJO con la separación de dueños (ver CLAUDE.md): `tenants` es de n8n, así
-- que NO se le agrega ninguna columna acá. El estado comercial del tenant
-- vive en una tabla aparte del portal (tenant_estado_plataforma) que la
-- referencia. Mismo criterio que tenant_servicios en 06_vendedores.sql.
--
-- Todo idempotente (IF NOT EXISTS): aplicar_sql.py reaplica los archivos.
-- ============================================================


-- ------------------------------------------------------------
-- Estado comercial del tenant, visto desde la plataforma
-- ------------------------------------------------------------
-- No confundir con tenant_servicios (qué módulos tiene encendidos) ni con
-- tenant_subscriptions.estado (cómo viene el cobro). Esto es la decisión
-- manual de gerencia: "a este lo suspendimos", "este está de prueba".
--
--   activo      → operando normal
--   prueba      → piloto / demo, todavía no factura
--   suspendido  → apagado a mano por gerencia (impago, abuso, pedido del
--                 cliente). services/acceso_pagos.py lo lee y apaga el
--                 agente, así que esto tiene dientes, no es una etiqueta.
--   baja        → se fue. Se conserva la fila para no perder el histórico
--                 de consumo ni de cobros.
CREATE TABLE IF NOT EXISTS tenant_estado_plataforma (
    tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,

    estado VARCHAR(20) NOT NULL DEFAULT 'activo'
        CHECK (estado IN ('activo', 'prueba', 'suspendido', 'baja')),

    -- Por qué quedó así. Obligatorio por convención en la API (no por
    -- CHECK) cuando el estado no es 'activo': el dato útil de una
    -- suspensión es el motivo, no la fecha.
    motivo TEXT,
    notas  TEXT,

    -- Correo del usuario de gerencia que lo dejó en este estado. Texto y no
    -- FK: gerencia_users puede depurarse y el rastro tiene que sobrevivir.
    actualizado_por VARCHAR(255),
    actualizado_en  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Los tenants que ya existen arrancan en 'activo': sin esto, el primero que
-- pase por acceso_pagos leería una fila inexistente. Mismo patrón que el
-- backfill de tenant_servicios.
INSERT INTO tenant_estado_plataforma (tenant_id)
SELECT id FROM tenants
ON CONFLICT (tenant_id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_tenant_estado_plataforma_estado
    ON tenant_estado_plataforma (estado);


-- ------------------------------------------------------------
-- Consumo de tokens de IA por tenant
-- ------------------------------------------------------------
-- Una fila por llamada al modelo. Lo escribe n8n (el que habla con el LLM)
-- vía POST /api/eventos/uso-tokens con X-Internal-Token; el portal solo lee.
--
-- Es el libro mayor crudo, no un contador: con las filas sueltas se puede
-- responder "¿por qué este tenant gastó tanto el martes?", cosa que un
-- contador acumulado no permite. El agregado lo arma la consulta.
--
-- No reemplaza a tenant_credits: los créditos son la unidad que se cobra y
-- ya tienen su propio libro (credit_transactions). Esto es el costo real
-- del lado del proveedor del modelo. Separados a propósito — el día que se
-- cambie la equivalencia token→crédito, el histórico de consumo no se toca.
CREATE TABLE IF NOT EXISTS tenant_token_usage (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Nullable: un resumen periódico o una herramienta no siempre cuelgan
    -- de una conversación. ON DELETE SET NULL y no CASCADE: si n8n purga
    -- conversaciones viejas, el consumo facturado no puede desaparecer.
    conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,

    origen VARCHAR(30) NOT NULL DEFAULT 'agente'
        CHECK (origen IN ('agente', 'herramienta', 'resumen', 'otro')),
    modelo VARCHAR(100),

    tokens_entrada INTEGER NOT NULL DEFAULT 0 CHECK (tokens_entrada >= 0),
    tokens_salida  INTEGER NOT NULL DEFAULT 0 CHECK (tokens_salida  >= 0),
    -- Generada y no calculada al vuelo: las consultas del panel suman y
    -- ordenan por el total todo el tiempo.
    tokens_total   INTEGER GENERATED ALWAYS AS (tokens_entrada + tokens_salida) STORED,

    -- Lo que costó del lado del proveedor. NUMERIC(12,6) porque los precios
    -- por token están en millonésimas de dólar. Nullable: n8n puede reportar
    -- tokens sin saber el precio vigente.
    costo_usd NUMERIC(12,6),

    -- n8n reintenta nodos que fallaron a medias. Con la clave del run/nodo
    -- acá, el reintento choca contra el índice único en vez de duplicar el
    -- consumo. Mismo candado que mp_payment_id en tenant_transactions.
    idempotency_key VARCHAR(120),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- "El consumo de este tenant en los últimos N días", que es la consulta del
-- panel y la del detalle.
CREATE INDEX IF NOT EXISTS idx_token_usage_tenant_fecha
    ON tenant_token_usage (tenant_id, created_at DESC);

-- La serie global del resumen, sin filtrar por tenant.
CREATE INDEX IF NOT EXISTS idx_token_usage_fecha
    ON tenant_token_usage (created_at DESC);

-- Parcial porque la mayoría de las filas no traen clave: el índice solo
-- cubre a las que sí, igual que idx_portal_users_google_id.
CREATE UNIQUE INDEX IF NOT EXISTS idx_token_usage_idempotency
    ON tenant_token_usage (idempotency_key)
    WHERE idempotency_key IS NOT NULL;


-- ------------------------------------------------------------
-- Bitácora de gerencia
-- ------------------------------------------------------------
-- Gerencia puede apagarle el agente a un negocio o regalarle créditos. Eso
-- no puede pasar sin dejar rastro, y el rastro no puede vivir en los logs
-- de la aplicación (se rotan y no se consultan desde el portal).
--
-- Sin FK a tenants ni a portal_users a propósito: el registro de una baja
-- tiene que sobrevivir al borrado del tenant que documenta.
CREATE TABLE IF NOT EXISTS gerencia_auditoria (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    actor_email          VARCHAR(255) NOT NULL,
    actor_portal_user_id UUID,

    -- 'estado_tenant', 'servicios_tenant', 'ajuste_creditos', ...
    accion    VARCHAR(60) NOT NULL,
    tenant_id UUID,

    -- El antes y el después de lo que se cambió. JSONB y no columnas fijas:
    -- cada acción cambia cosas distintas y no vale la pena una tabla por tipo.
    detalle JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gerencia_auditoria_fecha
    ON gerencia_auditoria (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_gerencia_auditoria_tenant
    ON gerencia_auditoria (tenant_id, created_at DESC);


-- ------------------------------------------------------------
-- Ficha de cada tenant vista desde plataforma
-- ------------------------------------------------------------
-- Junta en un solo lugar lo que hoy está repartido en seis tablas. La usa
-- routers/gerencia.py, que le agrega por fuera el agregado de consumo (ese
-- depende del rango de fechas que pida la pantalla, así que no puede ir
-- congelado en la vista).
--
-- Los COALESCE no sobran: un tenant dado de alta antes de 06_vendedores.sql
-- o de este archivo puede no tener fila en las tablas del portal, y en el
-- listado de gerencia tiene que aparecer igual — justamente para que se vea
-- que le falta configuración.
CREATE OR REPLACE VIEW v_gerencia_tenants AS
SELECT
    t.id         AS tenant_id,
    t.name       AS nombre,
    t.created_at AS alta,

    COALESCE(ep.estado, 'activo') AS estado,
    ep.motivo                     AS estado_motivo,
    ep.actualizado_en             AS estado_actualizado_en,
    ep.actualizado_por            AS estado_actualizado_por,

    COALESCE(ts.agente_ia_activo, true)           AS agente_ia_activo,
    COALESCE(ts.gestion_vendedores_activo, false) AS gestion_vendedores_activo,

    s.plan             AS plan,
    s.estado           AS estado_suscripcion,
    s.fecha_renovacion AS fecha_renovacion,
    s.precio_monthly   AS precio_monthly,

    COALESCE(c.creditos_disponibles, 0) AS creditos_disponibles,
    COALESCE(c.creditos_gastados, 0)    AS creditos_gastados,

    (SELECT COUNT(*) FROM portal_users pu
      WHERE pu.tenant_id = t.id AND pu.is_active)          AS usuarios_portal,
    (SELECT COUNT(*) FROM vendedores v
      WHERE v.tenant_id = t.id AND v.activo)               AS vendedores_activos,
    (SELECT COUNT(*) FROM channel_credentials cc
      WHERE cc.tenant_id = t.id AND cc.is_active)          AS canales_activos,
    (SELECT MAX(m.created_at) FROM messages m
      WHERE m.tenant_id = t.id)                            AS ultimo_mensaje

FROM tenants t
LEFT JOIN tenant_estado_plataforma ep ON ep.tenant_id = t.id
LEFT JOIN tenant_servicios         ts ON ts.tenant_id = t.id
LEFT JOIN tenant_subscriptions      s ON  s.tenant_id = t.id
LEFT JOIN tenant_credits            c ON  c.tenant_id = t.id;
