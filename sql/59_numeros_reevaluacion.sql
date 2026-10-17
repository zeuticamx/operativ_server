-- ============================================================
-- 59 · Varios números (spec 005a): cola de reevaluación y migración de datos
-- ============================================================
-- Dos partes:
--
--   A. Cola de reevaluación (RF-11). Triggers sobre tenant_subscriptions,
--      tenant_estado_plataforma, planes y plan_herramientas que apuntan "este
--      negocio hay que reevaluarlo" en `numeros_reevaluacion_cola`. Un job de
--      un minuto (tarea T8) la drena: calcula el cupo con el núcleo puro,
--      pausa los sobrantes, avisa y deja la bitácora. Las vías de Python
--      llaman al servicio directamente para que el efecto sea inmediato; la
--      cola es la red de seguridad si algo escribe la suscripción por otra
--      vía. Los triggers solo ENCOLAN: no pausan nada.
--
--      Motivos de la cola (VARCHAR libre, no CHECK):
--        cambio_de_plan        caso 1: cambio de plan, alta o reactivación
--        fin_de_suscripcion    caso 2: cancelación o vencimiento de pago
--        fin_de_prueba         caso 3: prueba que termina, se pausa o cancela
--        cupo_plataforma       caso 4: gerencia cambia el cupo del plan contratado
--        plan_mas_bajo         caso 5: cambia el plan más bajo activo
--        pago_de_prueba        caso 6: prueba que pasa a plan de pago
--        herramientas_del_plan caso 5: cambia la lista de herramientas de un plan
--        reactivacion_negocio  un negocio suspendido o de baja vuelve (P8)
--      El caso 7 (despliegue) no pasa por la cola: lo lanza el job una vez.
--
--      Los negocios suspendidos o dados de baja no se encolan (P8): se
--      reevalúan cuando vuelven.
--
--   B. Migración de datos: cada WhatsApp activo de `channel_credentials` pasa
--      a ser una fila de `agentes_canal` con la configuración de su agente
--      actual (RF-22). Al final del archivo.
--
-- NO borra ni modifica `channel_credentials`, `tenant_channels` ni
-- `tenant_agent_config`: el código anterior sigue leyéndolos.
--
-- Requiere 56, 57 y 58. Idempotente: la cola y los triggers con IF NOT EXISTS /
-- DROP IF EXISTS; la copia de datos solo toca los negocios que todavía no
-- tienen ningún número en `agentes_canal` (ver B).
--
--     python aplicar_sql.py sql/59_numeros_reevaluacion.sql
-- ============================================================


-- ============================================================
-- A. Cola de reevaluación
-- ============================================================
CREATE TABLE IF NOT EXISTS numeros_reevaluacion_cola (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    motivo        VARCHAR(40) NOT NULL,
    creada_en     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    procesada_en  TIMESTAMPTZ
);

-- Lo único que se consulta de la cola es "lo pendiente".
CREATE INDEX IF NOT EXISTS idx_numeros_cola_pendiente
    ON numeros_reevaluacion_cola (creada_en)
    WHERE procesada_en IS NULL;

CREATE INDEX IF NOT EXISTS idx_numeros_cola_tenant
    ON numeros_reevaluacion_cola (tenant_id, motivo)
    WHERE procesada_en IS NULL;

-- Tareas de una sola vez (el despliegue de RF-22, la siembra de abajo): la
-- clave existe = ya se hizo.
CREATE TABLE IF NOT EXISTS numeros_despliegue (
    clave         VARCHAR(60) PRIMARY KEY,
    ejecutado_en  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- Encola un negocio, salvo que esté suspendido o de baja (P8) o que ya tenga
-- una entrada pendiente con el mismo motivo (la reevaluación es idempotente;
-- no hace falta repetirla).
CREATE OR REPLACE FUNCTION numeros_encolar_reevaluacion(p_tenant UUID, p_motivo VARCHAR)
RETURNS VOID AS $$
BEGIN
    IF COALESCE(
           (SELECT ep.estado FROM tenant_estado_plataforma ep WHERE ep.tenant_id = p_tenant),
           'activo'
       ) IN ('suspendido', 'baja') THEN
        RETURN;
    END IF;

    INSERT INTO numeros_reevaluacion_cola (tenant_id, motivo)
    SELECT p_tenant, p_motivo
     WHERE NOT EXISTS (
         SELECT 1 FROM numeros_reevaluacion_cola q
          WHERE q.tenant_id = p_tenant
            AND q.motivo = p_motivo
            AND q.procesada_en IS NULL
     );
END
$$ LANGUAGE plpgsql;


-- Encola a quien rige un plan: los que lo tienen contratado (o de prueba) y,
-- si es el plan más bajo activo, los que no tienen suscripción vigente.
CREATE OR REPLACE FUNCTION numeros_encolar_por_plan(p_plan TEXT, p_motivo VARCHAR)
RETURNS VOID AS $$
DECLARE
    v_tenant UUID;
BEGIN
    FOR v_tenant IN SELECT s.tenant_id FROM tenant_subscriptions s WHERE s.plan = p_plan LOOP
        PERFORM numeros_encolar_reevaluacion(v_tenant, p_motivo);
    END LOOP;

    IF p_plan = plan_mas_bajo_activo() THEN
        FOR v_tenant IN SELECT x FROM tenants_sin_suscripcion_vigente() AS x LOOP
            PERFORM numeros_encolar_reevaluacion(v_tenant, p_motivo);
        END LOOP;
    END IF;
END
$$ LANGUAGE plpgsql;


-- ------------------------------------------------------------
-- tenant_subscriptions: casos 1, 2, 3 y 6
-- ------------------------------------------------------------
-- "Vigente" como en situacion_comercial: activa y sin la fecha vencida.
-- Solo se encola cuando algo cambia de verdad: una suscripción que sigue
-- activa y solo cambia de fecha o de cancela_al_vencer (la cancelación
-- programada mantiene el cupo hasta el fin del periodo, D18) no encola nada.
-- Cuando una activa vence, la encola el job de vencidas (T8), porque ninguna
-- escritura lo avisa.
CREATE OR REPLACE FUNCTION trg_numeros_encolar_suscripcion() RETURNS TRIGGER AS $$
DECLARE
    v_motivo        TEXT;
    v_vigente_nueva BOOLEAN;
    v_vigente_vieja BOOLEAN;
BEGIN
    v_vigente_nueva := NEW.estado = 'activa'
                       AND (NEW.fecha_renovacion IS NULL OR NEW.fecha_renovacion >= NOW());

    IF TG_OP = 'INSERT' THEN
        v_motivo := CASE
            WHEN NOT v_vigente_nueva AND NEW.origen = 'prueba' THEN 'fin_de_prueba'
            WHEN NOT v_vigente_nueva THEN 'fin_de_suscripcion'
            ELSE 'cambio_de_plan'
        END;
    ELSE
        v_vigente_vieja := OLD.estado = 'activa'
                           AND (OLD.fecha_renovacion IS NULL OR OLD.fecha_renovacion >= NOW());
        IF OLD.origen = 'prueba' AND NEW.origen = 'pago' THEN
            v_motivo := 'pago_de_prueba';
        ELSIF v_vigente_vieja AND NOT v_vigente_nueva THEN
            v_motivo := CASE WHEN NEW.origen = 'prueba' THEN 'fin_de_prueba' ELSE 'fin_de_suscripcion' END;
        ELSIF OLD.plan IS DISTINCT FROM NEW.plan
           OR OLD.estado IS DISTINCT FROM NEW.estado
           OR OLD.origen IS DISTINCT FROM NEW.origen THEN
            v_motivo := 'cambio_de_plan';
        END IF;
    END IF;

    IF v_motivo IS NOT NULL THEN
        PERFORM numeros_encolar_reevaluacion(NEW.tenant_id, v_motivo);
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS numeros_encolar_suscripcion ON tenant_subscriptions;
CREATE TRIGGER numeros_encolar_suscripcion
    AFTER INSERT OR UPDATE OF plan, estado, origen, fecha_renovacion, cancela_al_vencer
    ON tenant_subscriptions
    FOR EACH ROW EXECUTE FUNCTION trg_numeros_encolar_suscripcion();


-- ------------------------------------------------------------
-- tenant_estado_plataforma: fin del piloto y vuelta de un negocio bloqueado
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION trg_numeros_encolar_estado_plataforma() RETURNS TRIGGER AS $$
BEGIN
    IF OLD.estado = 'prueba' AND NEW.estado <> 'prueba' THEN
        PERFORM numeros_encolar_reevaluacion(NEW.tenant_id, 'fin_de_prueba');
    ELSIF OLD.estado IN ('suspendido', 'baja') AND NEW.estado IN ('activo', 'prueba') THEN
        PERFORM numeros_encolar_reevaluacion(NEW.tenant_id, 'reactivacion_negocio');
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS numeros_encolar_estado_plataforma ON tenant_estado_plataforma;
CREATE TRIGGER numeros_encolar_estado_plataforma
    AFTER UPDATE OF estado ON tenant_estado_plataforma
    FOR EACH ROW EXECUTE FUNCTION trg_numeros_encolar_estado_plataforma();


-- ------------------------------------------------------------
-- planes: casos 4 y 5
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION trg_numeros_encolar_plan() RETURNS TRIGGER AS $$
DECLARE
    v_tenant UUID;
BEGIN
    -- Caso 4: gerencia cambia el cupo del plan contratado.
    IF OLD.max_numeros_whatsapp IS DISTINCT FROM NEW.max_numeros_whatsapp THEN
        FOR v_tenant IN
            SELECT s.tenant_id
              FROM tenant_subscriptions s
             WHERE s.plan = NEW.nombre
               AND (SELECT sc.situacion FROM situacion_comercial(s.tenant_id) sc) = 'suscripcion'
        LOOP
            PERFORM numeros_encolar_reevaluacion(v_tenant, 'cupo_plataforma');
        END LOOP;
    END IF;

    -- Caso 5: cambia el plan más bajo activo (cupo, precio, estado, límites,
    -- o cuál es). Los límites y la lista de herramientas reevalúan límites y
    -- herramientas; el cupo, el precio y el estado, el cupo: lo decide el
    -- servicio, aquí solo se avisa de que hay que mirar.
    IF NEW.nombre = plan_mas_bajo_activo()
       OR OLD.precio_monthly IS DISTINCT FROM NEW.precio_monthly
       OR OLD.activo IS DISTINCT FROM NEW.activo
       OR OLD.max_numeros_whatsapp IS DISTINCT FROM NEW.max_numeros_whatsapp THEN
        FOR v_tenant IN SELECT x FROM tenants_sin_suscripcion_vigente() AS x LOOP
            PERFORM numeros_encolar_reevaluacion(v_tenant, 'plan_mas_bajo');
        END LOOP;
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS numeros_encolar_plan ON planes;
CREATE TRIGGER numeros_encolar_plan
    AFTER UPDATE OF max_numeros_whatsapp, precio_monthly, activo, modelos_permitidos, historial_maximo
    ON planes
    FOR EACH ROW EXECUTE FUNCTION trg_numeros_encolar_plan();

-- Alta o baja de un plan: puede cambiar cuál es el más bajo activo.
CREATE OR REPLACE FUNCTION trg_numeros_encolar_plan_alta_baja() RETURNS TRIGGER AS $$
DECLARE
    v_tenant UUID;
    v_afecta BOOLEAN;
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_afecta := NEW.activo AND NEW.nombre = plan_mas_bajo_activo();
    ELSE
        v_afecta := OLD.activo;
    END IF;

    IF v_afecta THEN
        FOR v_tenant IN SELECT x FROM tenants_sin_suscripcion_vigente() AS x LOOP
            PERFORM numeros_encolar_reevaluacion(v_tenant, 'plan_mas_bajo');
        END LOOP;
    END IF;
    RETURN CASE WHEN TG_OP = 'INSERT' THEN NEW ELSE OLD END;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS numeros_encolar_plan_alta_baja ON planes;
CREATE TRIGGER numeros_encolar_plan_alta_baja
    AFTER INSERT OR DELETE ON planes
    FOR EACH ROW EXECUTE FUNCTION trg_numeros_encolar_plan_alta_baja();


-- ------------------------------------------------------------
-- plan_herramientas: cambia la lista de herramientas de un plan (caso 5)
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION trg_numeros_encolar_plan_herramientas() RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM numeros_encolar_por_plan(OLD.plan_nombre::TEXT, 'herramientas_del_plan');
        RETURN OLD;
    END IF;
    PERFORM numeros_encolar_por_plan(NEW.plan_nombre::TEXT, 'herramientas_del_plan');
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS numeros_encolar_plan_herramientas ON plan_herramientas;
CREATE TRIGGER numeros_encolar_plan_herramientas
    AFTER INSERT OR DELETE ON plan_herramientas
    FOR EACH ROW EXECUTE FUNCTION trg_numeros_encolar_plan_herramientas();


-- ============================================================
-- B. Migración de datos (RF-22, CL-17, CL-27)
-- ============================================================
-- Por cada negocio con un WhatsApp activo en `channel_credentials` (con
-- identificador) y SIN ningún número todavía en `agentes_canal`:
--
--   1. Una fila de `agentes_canal`, `estado = 'activo'`, con la configuración
--      de `tenant_agent_config` (vacía si no la hay) y `agente_activo` igual a
--      su `is_active` (un agente apagado se conserva apagado). La temperatura
--      se acota a [0, 1] por el CHECK nuevo. `vinculado_en` es el
--      `updated_at` de la credencial, lo más cercano a "desde cuándo" que se
--      conoce. Los canales desconectados (`is_active = false`) NO se migran.
--   2. Sus herramientas: todas las `tenant_tools` habilitadas del negocio, que
--      es lo que el agente usa hoy.
--   3. Principal: ese número, si tiene configuración completa (RF-22, D34). Sin
--      prompt o sin nombre queda activo, incompleto y sin principal (deja de
--      responder; el aviso lo da el despliegue, T8, D39). Cada negocio tiene
--      como mucho una credencial de WhatsApp, así que migra un solo número.
--   4. `config_referencia_negocio`: copia de la configuración del principal.
--      Sin principal no se crea y la referencia sigue siendo `tenant_agent_config`.
--   5. `conversations.agente_canal_id` de sus conversaciones de WhatsApp.
--
-- Además, UNA vez (marca en `numeros_despliegue`), `social_herramientas`
-- recibe las herramientas habilitadas de todos los negocios: es lo que usan
-- hoy Instagram, Facebook y el widget.
--
-- Idempotente: un negocio con cualquier fila en `agentes_canal` (aunque sea
-- desvinculada) se salta, así que reaplicar no recrea números que el negocio
-- desvinculó ni pisa la selección de herramientas que haya cambiado. Un
-- identificador que ya esté vinculado a otro negocio también se salta (si dos
-- negocios compartieran el mismo, gana el de credencial más reciente).
-- `channel_credentials` y `tenant_channels` no se tocan.
DO $$
DECLARE
    v_cred    RECORD;
    v_numero  UUID;
BEGIN
    FOR v_cred IN
        SELECT DISTINCT ON (cc.phone_number_id)
               cc.tenant_id,
               cc.phone_number_id,
               cc.bsp_provider,
               cc.updated_at
          FROM channel_credentials cc
         WHERE cc.channel_type = 'whatsapp'
           AND cc.is_active
           AND NULLIF(cc.phone_number_id, '') IS NOT NULL
         ORDER BY cc.phone_number_id, cc.updated_at DESC NULLS LAST, cc.tenant_id
    LOOP
        CONTINUE WHEN EXISTS (SELECT 1 FROM agentes_canal a WHERE a.tenant_id = v_cred.tenant_id);
        CONTINUE WHEN EXISTS (
            SELECT 1 FROM agentes_canal a
             WHERE a.phone_number_id = v_cred.phone_number_id
               AND a.estado IN ('activo', 'pausado')
        );

        INSERT INTO agentes_canal (
            tenant_id, phone_number_id, bsp_provider, estado, es_principal,
            vinculado_en, agent_name, system_prompt, model, temperature,
            history_window, agente_activo
        )
        SELECT v_cred.tenant_id,
               v_cred.phone_number_id,
               v_cred.bsp_provider,
               'activo',
               NOT configuracion_incompleta(c.agent_name, c.system_prompt),
               COALESCE(v_cred.updated_at, NOW()),
               COALESCE(c.agent_name, ''),
               COALESCE(c.system_prompt, ''),
               c.model,
               LEAST(GREATEST(COALESCE(c.temperature, 0.7), 0), 1),
               COALESCE(c.history_window, 30),
               COALESCE(c.is_active, true)
          FROM (SELECT 1) AS uno
          LEFT JOIN tenant_agent_config c ON c.tenant_id = v_cred.tenant_id
        RETURNING id INTO v_numero;

        INSERT INTO agente_canal_herramientas (agente_canal_id, tenant_id, tool_key)
        SELECT v_numero, t.tenant_id, t.tool_key
          FROM tenant_tools t
         WHERE t.tenant_id = v_cred.tenant_id
           AND t.is_enabled = true
        ON CONFLICT DO NOTHING;

        INSERT INTO config_referencia_negocio (
            tenant_id, agent_name, system_prompt, model, temperature,
            history_window, agente_activo, herramientas
        )
        SELECT a.tenant_id, a.agent_name, a.system_prompt, a.model, a.temperature,
               a.history_window, a.agente_activo,
               COALESCE(
                   (SELECT array_agg(h.tool_key ORDER BY h.tool_key)
                      FROM agente_canal_herramientas h
                     WHERE h.agente_canal_id = a.id),
                   '{}'
               )
          FROM agentes_canal a
         WHERE a.id = v_numero
           AND a.es_principal
        ON CONFLICT (tenant_id) DO NOTHING;

        UPDATE conversations
           SET agente_canal_id = v_numero
         WHERE tenant_id = v_cred.tenant_id
           AND channel_type = 'whatsapp'
           AND agente_canal_id IS NULL;
    END LOOP;
END
$$;


-- Herramientas de Instagram, Facebook y widget: una sola vez.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM numeros_despliegue WHERE clave = 'siembra_social_herramientas') THEN
        INSERT INTO social_herramientas (tenant_id, tool_key)
        SELECT t.tenant_id, t.tool_key
          FROM tenant_tools t
         WHERE t.is_enabled = true
        ON CONFLICT DO NOTHING;

        INSERT INTO numeros_despliegue (clave) VALUES ('siembra_social_herramientas')
        ON CONFLICT (clave) DO NOTHING;
    END IF;
END
$$;
