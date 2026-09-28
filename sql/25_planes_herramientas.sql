-- ============================================================
-- HERRAMIENTAS POR PLAN + planes abiertos (sin lista fija de nombres)
-- ============================================================
-- Dos cambios que van juntos porque los dos son "el plan decide":
--
-- 1. `planes` suma un booleano por herramienta del portal que puede quedar
--    fuera de un nivel. Junto con los dos que ya existían
--    (agente_ia_activo, gestion_vendedores_activo) forman la matriz que lee
--    services/acceso_plan.py:
--
--      herramienta   columna                     starter  pro  enterprise
--      agente        agente_ia_activo               ✓      ✓      ✓
--      vendedores    gestion_vendedores_activo      ✓      ✓      ✓
--      herramientas  herramientas_activo            —      ✓      ✓
--      crm_campo     crm_campo_activo               —      ✓      ✓
--      calendario    calendario_activo              —      ✓      ✓
--
--    Vive en la tabla y no en el código para que gerencia pueda moverla
--    desde /gerencia/planes sin un deploy — mismo criterio que los precios.
--    Los defaults (true) son para planes que se den de alta por SQL a mano;
--    los tres de fábrica se ajustan abajo.
--
-- 2. tenant_subscriptions.plan dejaba de aceptar cualquier plan que no
--    fuera starter/pro/enterprise por un CHECK escrito en 11_mercado_pago.sql,
--    así que un plan nuevo creado desde /gerencia/planes no se podía
--    contratar (el UPSERT de activar_suscripcion reventaba). Se cambia por
--    una FK a planes(nombre): acepta cualquier plan que exista y sigue sin
--    aceptar uno inventado. ON UPDATE CASCADE deja preparado un renombre
--    futuro sin suscripciones huérfanas; ON DELETE RESTRICT (el default)
--    impide borrar un plan que alguien tiene contratado — para retirarlo
--    está `activo = false`.
--
-- No se hace ALTER TYPE de la columna (VARCHAR(50)): la usa la vista de
-- 14_gerencia_auditoria.sql y Postgres no deja cambiarle el tipo. Por eso
-- PlanCrearIn limita el nombre a 50 caracteres.
--
-- Idempotente: aplicar_sql.py reaplica los archivos.
-- ============================================================

ALTER TABLE planes
    ADD COLUMN IF NOT EXISTS herramientas_activo BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE planes
    ADD COLUMN IF NOT EXISTS crm_campo_activo    BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE planes
    ADD COLUMN IF NOT EXISTS calendario_activo   BOOLEAN NOT NULL DEFAULT true;

-- Solo la primera vez: si gerencia ya editó la matriz de starter desde el
-- panel, reaplicar el archivo no se la pisa. `updated_at = created_at`
-- quiere decir "nadie lo tocó desde el seed".
UPDATE planes
   SET herramientas_activo = false,
       crm_campo_activo    = false,
       calendario_activo   = false
 WHERE nombre = 'starter'
   AND updated_at = created_at;


ALTER TABLE tenant_subscriptions
    DROP CONSTRAINT IF EXISTS tenant_subscriptions_plan_check;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'tenant_subscriptions_plan_fkey'
           AND conrelid = 'tenant_subscriptions'::regclass
    ) THEN
        ALTER TABLE tenant_subscriptions
            ADD CONSTRAINT tenant_subscriptions_plan_fkey
            FOREIGN KEY (plan) REFERENCES planes (nombre) ON UPDATE CASCADE;
    END IF;
END
$$;
