-- ============================================================
-- COBROS CON MERCADO PAGO (modelo híbrido: suscripción + créditos)
-- ============================================================
-- Un tenant paga una suscripción mensual por plan (starter/pro/enterprise)
-- y, además, puede comprar paquetes de créditos sueltos para consumo
-- variable. Son dos cosas distintas y por eso hay dos caminos:
--
--   tenant_subscriptions  -> el plan vigente (uno por tenant)
--   tenant_credits        -> el saldo de créditos (uno por tenant)
--   tenant_transactions   -> cada cobro individual, de cualquiera de los dos
--   credit_transactions   -> el libro mayor del saldo de créditos
--
-- Los CHECK de estado son los mismos strings que usa pagos.py: si acá se
-- agrega un estado nuevo, hay que tocar el mapeo de MERCADO_PAGO -> estado
-- en ese módulo, y al revés.
--
-- Todo idempotente (IF NOT EXISTS): aplicar_sql.py reaplica los archivos.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- ------------------------------------------------------------
-- Planes disponibles (catálogo; lo lee el portal para la grilla)
-- ------------------------------------------------------------
-- Va primero porque tenant_subscriptions.plan referencia estos nombres
-- por su CHECK.
CREATE TABLE IF NOT EXISTS planes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    nombre      VARCHAR(100) NOT NULL UNIQUE,   -- 'starter' | 'pro' | 'enterprise'
    descripcion TEXT,

    precio_monthly NUMERIC(10,2) NOT NULL,
    precio_annual  NUMERIC(10,2),

    -- Límites del plan. Se copian a tenant_servicios cuando el pago se
    -- aprueba: esa tabla es la que consultan el resto de los módulos.
    agente_ia_activo          BOOLEAN NOT NULL DEFAULT true,
    gestion_vendedores_activo BOOLEAN NOT NULL DEFAULT true,
    -- NULL = sin tope (enterprise).
    max_vendedores            SMALLINT,
    max_leads_mensuales       INTEGER DEFAULT 1000,
    creditos_incluidos_mensual NUMERIC(12,2) NOT NULL DEFAULT 100,

    activo      BOOLEAN NOT NULL DEFAULT true,
    orden       SMALLINT NOT NULL DEFAULT 0,

    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO planes
    (nombre, descripcion, precio_monthly, precio_annual, max_vendedores,
     creditos_incluidos_mensual, orden)
VALUES
    ('starter',    'Plan básico para pequeños negocios',   29.99,  299.90,    3,  100, 1),
    ('pro',        'Plan profesional con más vendedores',  99.99,  999.90,   15,  500, 2),
    ('enterprise', 'Plan empresarial ilimitado',          299.99, 2999.90, NULL, 2000, 3)
ON CONFLICT (nombre) DO NOTHING;


-- ------------------------------------------------------------
-- Paquetes de créditos sueltos (catálogo)
-- ------------------------------------------------------------
-- Vive en tabla y no como constante en el código para poder mover precios
-- sin un deploy — misma razón por la que `planes` es una tabla.
--
-- OJO: los precios de abajo son una escalera de ejemplo con descuento por
-- volumen (de ~0.30 a ~0.20 por crédito). Ajústalos antes de cobrarle a
-- nadie: son el único número de este archivo que no salió de una decisión
-- de negocio previa.
CREATE TABLE IF NOT EXISTS paquetes_creditos (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    creditos  NUMERIC(12,2) NOT NULL UNIQUE,
    precio    NUMERIC(10,2) NOT NULL,

    activo    BOOLEAN NOT NULL DEFAULT true,
    orden     SMALLINT NOT NULL DEFAULT 0,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT paquetes_creditos_positivos CHECK (creditos > 0 AND precio > 0)
);

INSERT INTO paquetes_creditos (creditos, precio, orden)
VALUES
    (100,    29.99, 1),
    (500,   129.99, 2),
    (1000,  239.99, 3),
    (5000,  999.99, 4)
ON CONFLICT (creditos) DO NOTHING;


-- ------------------------------------------------------------
-- Suscripción vigente por tenant
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenant_subscriptions (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    plan   VARCHAR(50) NOT NULL CHECK (plan IN ('starter', 'pro', 'enterprise')),
    estado VARCHAR(30) NOT NULL CHECK (estado IN ('activa', 'pausada', 'cancelada')),

    -- Mercado Pago. Nullable: con checkout por preferencia (pago único
    -- renovado a mano) no hay un id de suscripción del lado de MP.
    mp_subscription_id VARCHAR(100),
    mp_customer_id     VARCHAR(100),

    fecha_inicio          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fecha_renovacion      TIMESTAMPTZ,
    fecha_proximo_intento TIMESTAMPTZ,

    precio_monthly NUMERIC(10,2) NOT NULL,

    intentos_fallidos   SMALLINT NOT NULL DEFAULT 0,
    ultima_notificacion TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Una sola suscripción por tenant: el UPSERT del webhook depende de esto.
    UNIQUE (tenant_id)
);

-- El job que cobra renovaciones busca "activas que vencen antes de X".
CREATE INDEX IF NOT EXISTS idx_tenant_subs_estado_renovacion
    ON tenant_subscriptions (estado, fecha_renovacion);


-- ------------------------------------------------------------
-- Transacciones individuales (cada intento de cobro)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenant_transactions (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    tipo     VARCHAR(50) NOT NULL
        CHECK (tipo IN ('subscription', 'credit_purchase', 'refund')),
    concepto TEXT,

    -- mp_payment_id es UNIQUE a propósito: Mercado Pago reintenta el mismo
    -- webhook varias veces, y esa restricción es el último candado contra
    -- acreditar dos veces el mismo pago (ver _procesar_pago_aprobado).
    mp_payment_id    VARCHAR(100) UNIQUE,
    mp_preference_id VARCHAR(100),

    monto       NUMERIC(10,2) NOT NULL,
    estado_pago VARCHAR(30) NOT NULL CHECK (
        estado_pago IN ('pendiente', 'aprobado', 'rechazado', 'cancelado', 'reembolsado')
    ),

    -- Qué se entrega si el pago se aprueba. Para 'subscription' es el
    -- nombre del plan; para 'credit_purchase', la cantidad de créditos.
    plan_nombre       VARCHAR(50),
    creditos_comprados NUMERIC(12,2),

    metodo_pago       VARCHAR(50),   -- 'credit_card', 'account_money', ...
    ultimos_4_digitos VARCHAR(4),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- El historial del portal: "las últimas N de este tenant".
CREATE INDEX IF NOT EXISTS idx_tenant_transactions_tenant_fecha
    ON tenant_transactions (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_tenant_transactions_preferencia
    ON tenant_transactions (mp_preference_id);

CREATE INDEX IF NOT EXISTS idx_tenant_transactions_estado
    ON tenant_transactions (estado_pago);


-- ------------------------------------------------------------
-- Saldo de créditos por tenant
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenant_credits (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    creditos_disponibles NUMERIC(12,2) NOT NULL DEFAULT 0,
    creditos_gastados    NUMERIC(12,2) NOT NULL DEFAULT 0,

    fecha_ultima_compra TIMESTAMPTZ,
    fecha_expiracion    TIMESTAMPTZ,   -- NULL = no expiran

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (tenant_id),
    CONSTRAINT tenant_credits_no_negativo CHECK (creditos_disponibles >= 0)
);

CREATE INDEX IF NOT EXISTS idx_tenant_credits_expiracion
    ON tenant_credits (fecha_expiracion);


-- ------------------------------------------------------------
-- Libro mayor de créditos
-- ------------------------------------------------------------
-- Cada movimiento guarda saldo_anterior y saldo_nuevo: con eso se puede
-- auditar un saldo que no cuadra sin tener que recalcular toda la historia.
CREATE TABLE IF NOT EXISTS credit_transactions (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    tipo     VARCHAR(50) NOT NULL
        CHECK (tipo IN ('compra', 'gasto', 'ajuste', 'devolucion')),
    -- Positiva suma, negativa resta. El signo lo pone quien inserta.
    cantidad NUMERIC(12,2) NOT NULL,
    concepto TEXT,

    tenant_transaction_id UUID REFERENCES tenant_transactions(id) ON DELETE SET NULL,

    saldo_anterior NUMERIC(12,2),
    saldo_nuevo    NUMERIC(12,2),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_credit_transactions_tenant_fecha
    ON credit_transactions (tenant_id, created_at DESC);

-- Un pago aprobado acredita una sola vez, aunque el webhook llegue repetido.
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_transactions_una_por_pago
    ON credit_transactions (tenant_transaction_id)
    WHERE tenant_transaction_id IS NOT NULL AND tipo = 'compra';
