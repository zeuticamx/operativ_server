-- ============================================================
-- LÍMITES DEL AGENTE POR PLAN: modelo y ventana de historial
-- ============================================================
-- El plan decide qué modelos puede elegir el negocio para su agente y cuántos
-- mensajes de historial (`tenant_agent_config.history_window`) puede darle.
--
--   planes.modelos_permitidos  TEXT[]  NULL = sin límite. El PRIMERO es el
--                                      modelo por defecto: es al que se
--                                      recorta cuando el actual no cabe.
--   planes.historial_maximo    INT     NULL = sin límite (5..200, igual que
--                                      el rango de AgenteConfigIn).
--
-- Los valida el backend contra services/limites_agente.py; acá solo el
-- rango, para que SQL a mano no deje un tope imposible.
--
-- POR QUÉ TRIGGERS: n8n lee tenant_agent_config directo, sin pasar por el
-- backend. Para que n8n nunca vea un modelo o historial fuera del plan, la
-- fila tiene que ESTAR siempre dentro del plan. Los puntos que pueden romper
-- eso son muchos (checkout de Stripe, piloto, onboarding, panel de gerencia,
-- SQL a mano) y un recorte en el único lugar común es más seguro que acordarse
-- de llamarlo en cada uno. El recorte solo BAJA: nunca sube un valor.
--
-- Un negocio en estado 'prueba' (piloto) y uno sin suscripción no se recortan,
-- igual que services/acceso_plan.py (el piloto tiene todo abierto).
--
-- Idempotente: aplicar_sql.py reaplica los archivos.
-- ============================================================

ALTER TABLE planes ADD COLUMN IF NOT EXISTS modelos_permitidos TEXT[];
ALTER TABLE planes ADD COLUMN IF NOT EXISTS historial_maximo   INT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'planes_historial_maximo_check'
                      AND conrelid = 'planes'::regclass) THEN
        ALTER TABLE planes ADD CONSTRAINT planes_historial_maximo_check
            CHECK (historial_maximo IS NULL OR historial_maximo BETWEEN 5 AND 200);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'planes_modelos_permitidos_check'
                      AND conrelid = 'planes'::regclass) THEN
        ALTER TABLE planes ADD CONSTRAINT planes_modelos_permitidos_check
            CHECK (modelos_permitidos IS NULL OR cardinality(modelos_permitidos) > 0);
    END IF;
END
$$;


-- ------------------------------------------------------------
-- Recorte de UN negocio a los límites de su plan
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION recortar_agente_a_plan(p_tenant UUID) RETURNS VOID AS $$
DECLARE
    v_modelos TEXT[];
    v_maximo  INT;
    v_estado  TEXT;
BEGIN
    SELECT p.modelos_permitidos, p.historial_maximo
      INTO v_modelos, v_maximo
      FROM tenant_subscriptions s
      JOIN planes p ON p.nombre = s.plan
     WHERE s.tenant_id = p_tenant;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    SELECT estado INTO v_estado FROM tenant_estado_plataforma WHERE tenant_id = p_tenant;
    IF v_estado = 'prueba' THEN
        RETURN;
    END IF;

    UPDATE tenant_agent_config
       SET model = CASE
                       WHEN v_modelos IS NOT NULL
                        AND (model IS NULL OR NOT (model = ANY (v_modelos)))
                       THEN v_modelos[1]
                       ELSE model
                   END,
           history_window = LEAST(history_window, COALESCE(v_maximo, history_window)),
           updated_at = NOW()
     WHERE tenant_id = p_tenant
       AND (
            (v_modelos IS NOT NULL AND (model IS NULL OR NOT (model = ANY (v_modelos))))
            OR (v_maximo IS NOT NULL AND history_window > v_maximo)
       );
END
$$ LANGUAGE plpgsql;


-- ------------------------------------------------------------
-- Triggers: cualquier cosa que cambie el plan efectivo o sus límites
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION trg_recortar_agente_negocio() RETURNS TRIGGER AS $$
BEGIN
    PERFORM recortar_agente_a_plan(NEW.tenant_id);
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION trg_recortar_agente_plan() RETURNS TRIGGER AS $$
DECLARE
    v_tenant UUID;
BEGIN
    FOR v_tenant IN SELECT tenant_id FROM tenant_subscriptions WHERE plan = NEW.nombre LOOP
        PERFORM recortar_agente_a_plan(v_tenant);
    END LOOP;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS recortar_agente_suscripcion ON tenant_subscriptions;
CREATE TRIGGER recortar_agente_suscripcion
    AFTER INSERT OR UPDATE OF plan ON tenant_subscriptions
    FOR EACH ROW EXECUTE FUNCTION trg_recortar_agente_negocio();

-- Sale del piloto ('prueba' → otro estado): desde ahí rigen los límites.
DROP TRIGGER IF EXISTS recortar_agente_estado ON tenant_estado_plataforma;
CREATE TRIGGER recortar_agente_estado
    AFTER INSERT OR UPDATE OF estado ON tenant_estado_plataforma
    FOR EACH ROW EXECUTE FUNCTION trg_recortar_agente_negocio();

DROP TRIGGER IF EXISTS recortar_agente_plan ON planes;
CREATE TRIGGER recortar_agente_plan
    AFTER UPDATE OF modelos_permitidos, historial_maximo ON planes
    FOR EACH ROW EXECUTE FUNCTION trg_recortar_agente_plan();


-- ------------------------------------------------------------
-- Valores de fábrica. Solo la primera vez: `updated_at = created_at` quiere
-- decir "nadie tocó el plan desde el seed" (mismo criterio que
-- 25_planes_herramientas.sql). Va DESPUÉS de crear los triggers a propósito:
-- recorta de una vez a los negocios que ya tenían más de lo que da su plan.
-- enterprise queda NULL/NULL (sin límite = todos los modelos, 200 mensajes,
-- y además entran solos los modelos que se agreguen a futuro).
-- ------------------------------------------------------------
UPDATE planes
   SET modelos_permitidos = ARRAY['claude-haiku-4-5-20251001'],
       historial_maximo   = 50
 WHERE nombre = 'starter' AND updated_at = created_at;

UPDATE planes
   SET modelos_permitidos = ARRAY['claude-haiku-4-5-20251001', 'claude-sonnet-5'],
       historial_maximo   = 100
 WHERE nombre = 'pro' AND updated_at = created_at;
