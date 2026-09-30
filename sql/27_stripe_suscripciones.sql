-- ============================================================
-- SUSCRIPCIONES RECURRENTES CON STRIPE
-- ============================================================
-- Hasta 12_stripe.sql la suscripción se cobraba con un Checkout en modo
-- `payment` (cargo único, renovación manual). Desde acá el plan se contrata
-- con `mode=subscription` sobre un Price recurrente creado en el panel de
-- Stripe, y el ciclo de vida (renovaciones, cobros fallidos, cancelaciones)
-- llega por webhook — ver services/stripe_suscripciones.py.
--
-- La compra de créditos NO cambia: sigue siendo un cargo único.
--
-- Todo idempotente (IF NOT EXISTS): aplicar_sql.py reaplica los archivos.
-- ============================================================


-- ------------------------------------------------------------
-- planes: el Price recurrente de Stripe de cada plan
-- ------------------------------------------------------------
-- Lo carga gerencia desde /gerencia/planes (price_...). Nullable: un plan
-- sin Price no se puede contratar en línea (crear-pago responde 409), pero
-- sigue sirviendo para pruebas otorgadas por gerencia.
--
-- Ojo: Stripe cobra lo que dice el Price, no `precio_monthly`. Si se
-- cambia el precio del plan hay que crear un Price nuevo en Stripe y
-- pegarlo acá; el monto real de cada cobro queda en tenant_transactions.
ALTER TABLE planes
    ADD COLUMN IF NOT EXISTS stripe_price_id VARCHAR(255);


-- ------------------------------------------------------------
-- tenant_transactions: una fila por factura de Stripe
-- ------------------------------------------------------------
-- Cada renovación (y cada factura que falla) es una Invoice de Stripe.
-- Stripe reintenta los eventos: el UNIQUE parcial es lo que impide que el
-- mismo invoice.payment_succeeded registre dos cobros o extienda dos veces
-- la suscripción.
ALTER TABLE tenant_transactions
    ADD COLUMN IF NOT EXISTS stripe_invoice_id VARCHAR(255);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_transactions_stripe_invoice
    ON tenant_transactions (stripe_invoice_id)
    WHERE stripe_invoice_id IS NOT NULL;


-- ------------------------------------------------------------
-- tenant_subscriptions: estado de cancelación
-- ------------------------------------------------------------
-- cancela_al_vencer: el dueño (o gerencia desde Stripe) pidió cancelar al
--   final del período. Sigue 'activa' hasta fecha_renovacion; el job de
--   jobs/pagos_background.py la pausa cuando vence.
-- cancelada_en: Stripe ya dio de baja la Subscription
--   (customer.subscription.deleted). Si le quedaban días pagados, sigue
--   'activa' hasta fecha_renovacion y después la pausa el mismo job.
ALTER TABLE tenant_subscriptions
    ADD COLUMN IF NOT EXISTS cancela_al_vencer BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE tenant_subscriptions
    ADD COLUMN IF NOT EXISTS cancelada_en TIMESTAMPTZ;

-- Los eventos de Subscription e Invoice llegan con estos ids, no con el
-- tenant: así se encuentra la fila.
CREATE INDEX IF NOT EXISTS idx_tenant_subs_stripe_subscription
    ON tenant_subscriptions (stripe_subscription_id)
    WHERE stripe_subscription_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tenant_subs_stripe_customer
    ON tenant_subscriptions (stripe_customer_id)
    WHERE stripe_customer_id IS NOT NULL;
