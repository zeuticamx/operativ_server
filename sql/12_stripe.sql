-- ============================================================
-- COBROS CON STRIPE
-- ============================================================
-- Stripe reemplaza a Mercado Pago como pasarela activa. El modelo de
-- negocio NO cambia: sigue siendo el híbrido de 11_mercado_pago.sql
-- (suscripción por plan + paquetes de créditos sueltos), con el mismo
-- ciclo de 30 días y la misma renovación manual. Lo único que cambia es
-- quién cobra.
--
-- Por eso este archivo solo agrega columnas: las tablas, los CHECK de
-- estado y el libro mayor de créditos son los mismos. Las columnas mp_*
-- se dejan intactas a propósito — Mercado Pago queda deshabilitado por
-- configuración (PAYMENT_PROVIDER), no borrado, y los cobros históricos
-- hechos con MP tienen que seguir siendo legibles en el historial.
--
-- Todo idempotente (IF NOT EXISTS): aplicar_sql.py reaplica los archivos.
-- ============================================================


-- ------------------------------------------------------------
-- tenant_transactions: identificadores del lado de Stripe
-- ------------------------------------------------------------
-- El equivalente de mp_preference_id es la Checkout Session: se crea al
-- iniciar el pago y es lo que la pantalla de retorno usa para preguntar
-- "¿cómo quedó mi pago?" (llega en la URL como ?session_id=cs_...).
ALTER TABLE tenant_transactions
    ADD COLUMN IF NOT EXISTS stripe_session_id VARCHAR(255);

-- El equivalente de mp_payment_id: el cargo concreto. Se escribe cuando
-- el webhook confirma, no al crear la sesión.
ALTER TABLE tenant_transactions
    ADD COLUMN IF NOT EXISTS stripe_payment_intent_id VARCHAR(255);

-- UNIQUE por índice aparte y no en el ADD COLUMN: Postgres no acepta
-- `ADD COLUMN IF NOT EXISTS ... UNIQUE` en una sola sentencia, y con el
-- índice separado el IF NOT EXISTS vale para las dos cosas.
--
-- Es el mismo candado que mp_payment_id: Stripe reintenta el mismo evento
-- varias veces y dos sesiones distintas nunca pueden apuntar a la misma
-- fila. (La idempotencia del saldo ya la garantiza
-- idx_credit_transactions_una_por_pago; esto es el cinturón además de los
-- tirantes.)
CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_transactions_stripe_session
    ON tenant_transactions (stripe_session_id)
    WHERE stripe_session_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tenant_transactions_stripe_intent
    ON tenant_transactions (stripe_payment_intent_id)
    WHERE stripe_payment_intent_id IS NOT NULL;


-- ------------------------------------------------------------
-- tenant_subscriptions: cliente de Stripe
-- ------------------------------------------------------------
-- Nullable igual que las mp_*: con Checkout en modo `payment` (pago único
-- que se renueva a mano) no hay una Subscription del lado de Stripe, así
-- que stripe_subscription_id queda en NULL. Existe para el día que se
-- migre a cobro recurrente de verdad (mode=subscription), que es una
-- decisión de producto todavía no tomada.
ALTER TABLE tenant_subscriptions
    ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(255);

ALTER TABLE tenant_subscriptions
    ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR(255);
