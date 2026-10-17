-- ============================================================
-- 61 · Varios números (spec 005a, T8): recortes pendientes de avisar
-- ============================================================
-- `recortar_agente_a_plan` corre dentro de triggers (cambio de plan, fin de
-- prueba, límites editados por gerencia) y deja la configuración ya recortada
-- antes de que el servicio de reevaluación la vea; comparar "antes y después"
-- en Python no encuentra nada. Aquí el propio recorte deja una fila por cada
-- número (o por Instagram y Facebook) que cambió, y la reevaluación las
-- consume para escribir la bitácora `recorte_por_plan` y mandar el aviso
-- `numeros_recortados` (RF-10, RF-20).
--
-- Idempotente: tabla con IF NOT EXISTS y función con CREATE OR REPLACE
-- (mismo nombre y firma que en 58; el resto del cuerpo no cambia).
--
-- Aplicar:
--     python aplicar_sql.py sql/61_numeros_recortes_pendientes.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS numeros_recortes_pendientes (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- NULL = la configuración de Instagram, Facebook y widget.
    agente_canal_id   UUID REFERENCES agentes_canal(id) ON DELETE CASCADE,
    modelo_antes      TEXT,
    modelo_despues    TEXT,
    historial_antes   INT,
    historial_despues INT,
    creado_en         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_numeros_recortes_tenant
    ON numeros_recortes_pendientes (tenant_id, id);


CREATE OR REPLACE FUNCTION recortar_agente_a_plan(p_tenant UUID) RETURNS VOID AS $$
DECLARE
    v_modelos  TEXT[];
    v_maximo   INT;
BEGIN
    SELECT le.modelos, le.historial INTO v_modelos, v_maximo
      FROM limites_efectivos(p_tenant) le;

    IF v_modelos IS NULL AND v_maximo IS NULL THEN
        RETURN;
    END IF;

    -- Instagram, Facebook y widget.
    WITH antes AS (
        SELECT tenant_id, model, history_window
          FROM tenant_agent_config
         WHERE tenant_id = p_tenant
    ),
    nuevo AS (
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
           )
        RETURNING tenant_id, model, history_window
    )
    INSERT INTO numeros_recortes_pendientes
        (tenant_id, agente_canal_id, modelo_antes, modelo_despues, historial_antes, historial_despues)
    SELECT a.tenant_id, NULL, a.model, n.model, a.history_window, n.history_window
      FROM antes a
      JOIN nuevo n ON n.tenant_id = a.tenant_id
     WHERE a.model IS DISTINCT FROM n.model
        OR a.history_window IS DISTINCT FROM n.history_window;

    -- La referencia para números nuevos: no se avisa (no es un canal que responda).
    UPDATE config_referencia_negocio
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

    -- Cada número, activo o pausado.
    WITH antes AS (
        SELECT id, model, history_window
          FROM agentes_canal
         WHERE tenant_id = p_tenant
           AND estado IN ('activo', 'pausado')
    ),
    nuevo AS (
        UPDATE agentes_canal
           SET model = CASE
                           WHEN v_modelos IS NOT NULL
                            AND (model IS NULL OR NOT (model = ANY (v_modelos)))
                           THEN v_modelos[1]
                           ELSE model
                       END,
               history_window = LEAST(history_window, COALESCE(v_maximo, history_window)),
               updated_at = NOW()
         WHERE tenant_id = p_tenant
           AND estado IN ('activo', 'pausado')
           AND (
                (v_modelos IS NOT NULL AND (model IS NULL OR NOT (model = ANY (v_modelos))))
                OR (v_maximo IS NOT NULL AND history_window > v_maximo)
           )
        RETURNING id, model, history_window
    )
    INSERT INTO numeros_recortes_pendientes
        (tenant_id, agente_canal_id, modelo_antes, modelo_despues, historial_antes, historial_despues)
    SELECT p_tenant, a.id, a.model, n.model, a.history_window, n.history_window
      FROM antes a
      JOIN nuevo n ON n.id = a.id
     WHERE a.model IS DISTINCT FROM n.model
        OR a.history_window IS DISTINCT FROM n.history_window;
END
$$ LANGUAGE plpgsql;
