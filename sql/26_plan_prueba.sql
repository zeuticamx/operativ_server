-- ============================================================
-- PLAN DE PRUEBA otorgado por gerencia de plataforma
-- ============================================================
-- Una prueba es una fila normal de tenant_subscriptions ('activa', con
-- fecha_renovacion = fin de la prueba). No hay tabla aparte a propósito:
-- así services/acceso_plan.py, services/acceso_pagos.py y el job que pausa
-- las vencidas (jobs/pagos_background.py) la tratan igual que a un plan
-- pagado sin una sola rama nueva. Al vencer cae a 'pausada' y el negocio
-- queda como cualquier cuenta sin plan vigente.
--
-- Lo único que hace falta es saber de dónde salió la fila:
--
--   origen        'pago'   -> la dejó activar_suscripcion (services/pagos.py)
--                 'prueba' -> la otorgó gerencia (services/pruebas.py)
--   otorgada_por  correo del gerente que la otorgó; NULL en las pagadas.
--
-- `origen` decide dos cosas: que una prueba no pise un plan pagado vigente
-- (409) y que solo una prueba se pueda revocar desde gerencia.
--
-- Las filas que ya existen son todas de un pago (antes de este archivo no
-- había otro camino para crearlas): el DEFAULT 'pago' las deja bien.
--
-- Idempotente: aplicar_sql.py reaplica los archivos.
-- ============================================================

ALTER TABLE tenant_subscriptions
    ADD COLUMN IF NOT EXISTS origen VARCHAR(10) NOT NULL DEFAULT 'pago';

ALTER TABLE tenant_subscriptions
    ADD COLUMN IF NOT EXISTS otorgada_por VARCHAR(255);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'tenant_subscriptions_origen_check'
           AND conrelid = 'tenant_subscriptions'::regclass
    ) THEN
        ALTER TABLE tenant_subscriptions
            ADD CONSTRAINT tenant_subscriptions_origen_check
            CHECK (origen IN ('pago', 'prueba'));
    END IF;
END
$$;
