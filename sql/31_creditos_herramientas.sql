-- ============================================================
-- MIGRACIÓN: créditos del plan + consumo por llamada a herramienta
-- ============================================================
-- Regla: 1 crédito = 1 llamada a herramienta ejecutada por el agente
-- (sub-workflow ejecutar-herramienta-tenant de n8n, que antes de ejecutar
-- llama a POST /api/eventos/creditos/consumir). Ver services/creditos.py.
--
-- Dos bolsas en la misma fila de tenant_credits:
--   - creditos_disponibles: comprados (paquetes) o ajustados por gerencia.
--     NO cambia de significado: no vencen, y siguen siendo lo que
--     services/acceso_pagos.py mira para dejar operar sin suscripción.
--   - creditos_plan: la cuota del ciclo del plan. Se REINICIA (no se suma)
--     en cada activación/renovación y solo se puede gastar mientras
--     creditos_plan_vence > NOW() (NULL = sin vencimiento, para
--     suscripciones antiguas sin fecha_renovacion). Si se mezclara con la
--     otra bolsa, un negocio que dejó de pagar seguiría operando con lo que
--     le sobró del mes.
--
-- credit_transactions es el libro de movimientos de ambas bolsas. El
-- índice único (tenant_id, tipo, referencia) hace idempotentes:
--   - la carga del plan: un mismo pago/factura no carga dos veces
--     (referencia = 'tx:<uuid>' | 'stripe_invoice:<id>' | 'prueba:<uuid>')
--   - el consumo: un reintento de n8n no descuenta dos veces
--     (referencia = id de la ejecución del sub-workflow)
--
-- Idempotente.
-- ============================================================

ALTER TABLE tenant_credits
    ADD COLUMN IF NOT EXISTS creditos_plan NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE tenant_credits
    ADD COLUMN IF NOT EXISTS creditos_plan_vence TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'tenant_credits_plan_no_negativo'
    ) THEN
        ALTER TABLE tenant_credits
            ADD CONSTRAINT tenant_credits_plan_no_negativo CHECK (creditos_plan >= 0);
    END IF;
END $$;

ALTER TABLE credit_transactions ADD COLUMN IF NOT EXISTS bolsa VARCHAR(10);
ALTER TABLE credit_transactions ADD COLUMN IF NOT EXISTS herramienta VARCHAR(100);
ALTER TABLE credit_transactions ADD COLUMN IF NOT EXISTS conversation_id UUID;
ALTER TABLE credit_transactions ADD COLUMN IF NOT EXISTS referencia VARCHAR(255);

-- 'gasto' (que ya existía y nunca se usaba) es el consumo de herramienta.
ALTER TABLE credit_transactions DROP CONSTRAINT IF EXISTS credit_transactions_tipo_check;
ALTER TABLE credit_transactions
    ADD CONSTRAINT credit_transactions_tipo_check CHECK (
        tipo IN ('compra', 'gasto', 'ajuste', 'devolucion', 'asignacion_plan', 'expiracion_plan')
    );

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'credit_transactions_bolsa_check'
    ) THEN
        ALTER TABLE credit_transactions
            ADD CONSTRAINT credit_transactions_bolsa_check
            CHECK (bolsa IS NULL OR bolsa IN ('plan', 'comprados'));
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_transactions_referencia
    ON credit_transactions (tenant_id, tipo, referencia)
    WHERE referencia IS NOT NULL;

-- Historial de consumo por herramienta (reportes, "en qué se gastó").
CREATE INDEX IF NOT EXISTS idx_credit_transactions_herramienta
    ON credit_transactions (tenant_id, herramienta, created_at DESC)
    WHERE herramienta IS NOT NULL;

-- ------------------------------------------------------------
-- Carga inicial: las suscripciones vigentes al aplicar esto reciben la
-- cuota de su plan. Sin esto, el día que n8n empiece a validar saldo todos
-- los tenants se quedarían sin herramientas. La referencia 'migracion_31'
-- la hace idempotente: re-aplicar el archivo no vuelve a cargar.
-- ------------------------------------------------------------
INSERT INTO tenant_credits (tenant_id)
SELECT s.tenant_id
  FROM tenant_subscriptions s
 WHERE s.estado = 'activa'
   AND (s.fecha_renovacion IS NULL OR s.fecha_renovacion > NOW())
ON CONFLICT (tenant_id) DO NOTHING;

WITH vigentes AS (
    SELECT s.tenant_id, s.plan, s.fecha_renovacion, p.creditos_incluidos_mensual AS cuota
      FROM tenant_subscriptions s
      JOIN planes p ON p.nombre = s.plan
     WHERE s.estado = 'activa'
       AND (s.fecha_renovacion IS NULL OR s.fecha_renovacion > NOW())
       AND NOT EXISTS (
            SELECT 1 FROM credit_transactions ct
             WHERE ct.tenant_id = s.tenant_id
               AND ct.tipo = 'asignacion_plan'
               AND ct.referencia = 'migracion_31'
       )
),
asiento AS (
    INSERT INTO credit_transactions
        (tenant_id, tipo, cantidad, concepto, bolsa, referencia, saldo_anterior, saldo_nuevo)
    SELECT v.tenant_id, 'asignacion_plan', v.cuota,
           'Carga inicial del plan ' || v.plan, 'plan', 'migracion_31', c.creditos_plan, v.cuota
      FROM vigentes v
      JOIN tenant_credits c ON c.tenant_id = v.tenant_id
    RETURNING tenant_id
)
UPDATE tenant_credits c
   SET creditos_plan       = v.cuota,
       creditos_plan_vence = v.fecha_renovacion,
       updated_at          = NOW()
  FROM vigentes v
 WHERE c.tenant_id = v.tenant_id
   AND c.tenant_id IN (SELECT tenant_id FROM asiento);
