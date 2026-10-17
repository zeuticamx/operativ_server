-- ============================================================
-- 58 · Varios números (spec 005a): reglas efectivas, recorte y resolución
-- ============================================================
-- Tres cosas, todas con el mismo origen: el negocio ya no "tiene o no tiene
-- plan", tiene una SITUACIÓN COMERCIAL y de ella salen su cupo, sus límites y
-- sus herramientas (RF-9, RF-10, RF-11):
--
--   1. situacion_comercial / plan_mas_bajo_activo / limites_efectivos:
--      las reglas, en la base. Tienen que dar lo mismo que el núcleo puro
--      services/numeros_reglas.py (situacion_comercial, plan_mas_bajo_activo,
--      reglas_de_negocio); tests/test_numeros_sql.py lo comprueba con la misma
--      tabla de casos.
--
--   2. recortar_agente_a_plan (reemplaza el de 45_limites_agente_plan.sql) y
--      sus triggers. Sigue en la base porque cualquier vía puede escribir la
--      configuración (portal, onboarding, SQL a mano). Cambia QUÉ negocios
--      recorta: antes un piloto o un negocio sin suscripción no se recortaba;
--      ahora a un piloto con plan asociado se le aplican los límites de ese
--      plan, y a un negocio sin suscripción vigente los del plan más bajo
--      activo. Recorta también la referencia del negocio y los números
--      (activos y pausados). Solo BAJA un valor, nunca lo sube.
--
--   3. resuelve_tenant (reemplaza el de 52) y resuelve_numero: para WhatsApp
--      el número se busca en `agentes_canal`, no en `channel_credentials`.
--
-- Requiere 56 (agentes_canal) y 57 (plan_herramientas, max_numeros_whatsapp).
-- Idempotente: CREATE OR REPLACE y triggers con DROP IF EXISTS.
--
--     python aplicar_sql.py sql/58_numeros_limites_y_resolucion.sql
-- ============================================================


-- ------------------------------------------------------------
-- Situación comercial de un negocio
-- ------------------------------------------------------------
-- Devuelve siempre UNA fila: (situacion, plan).
--
--   prueba           el piloto (tenant_estado_plataforma.estado = 'prueba') o una
--                    suscripción vigente de origen 'prueba'. Va ANTES que la
--                    suscripción (D36). `plan` es el de la suscripción, o NULL
--                    si el piloto no tiene.
--   suscripcion      suscripción de pago vigente.
--   sin_suscripcion  cualquier otra cosa (CL-24); `plan` es NULL.
--
-- Vigente = estado 'activa' y fecha_renovacion nula o no vencida. Una 'activa'
-- con la fecha pasada se lee como no vigente sin esperar al job que la pausa
-- (igual que services/acceso_plan.py). Una cancelación programada sigue
-- 'activa' hasta fecha_renovacion (D18).
--
-- `p_ahora` existe para poder probar con una hora fija; en producción se usa
-- el valor por defecto.
CREATE OR REPLACE FUNCTION situacion_comercial(p_tenant UUID, p_ahora TIMESTAMPTZ DEFAULT NOW())
RETURNS TABLE (situacion TEXT, plan TEXT)
LANGUAGE sql
STABLE
AS $$
    WITH ctx AS (
        SELECT COALESCE(
                   (SELECT ep.estado FROM tenant_estado_plataforma ep WHERE ep.tenant_id = p_tenant),
                   'activo'
               )                                            AS estado_plataforma,
               s.plan                                       AS plan_suscripcion,
               s.origen                                     AS origen,
               COALESCE(
                   s.estado = 'activa'
                   AND (s.fecha_renovacion IS NULL OR s.fecha_renovacion >= p_ahora),
                   false
               )                                            AS vigente
          FROM (SELECT 1) AS uno
          LEFT JOIN tenant_subscriptions s ON s.tenant_id = p_tenant
    )
    SELECT CASE
               WHEN c.estado_plataforma = 'prueba' THEN 'prueba'
               WHEN c.vigente AND c.origen = 'prueba' THEN 'prueba'
               WHEN c.vigente THEN 'suscripcion'
               ELSE 'sin_suscripcion'
           END::TEXT,
           CASE
               WHEN c.estado_plataforma = 'prueba' THEN c.plan_suscripcion::TEXT
               WHEN c.vigente THEN c.plan_suscripcion::TEXT
               ELSE NULL::TEXT
           END
      FROM ctx c;
$$;


-- ------------------------------------------------------------
-- Plan más bajo activo
-- ------------------------------------------------------------
-- El de menor precio mensual. Empate: menor cupo de números (sin tope cuenta
-- como mayor) y después el nombre (D11, D31). El nombre se compara con
-- COLLATE "C" (orden por código, como Python) para que no dependa de la
-- configuración regional. NULL si no hay ningún plan activo.
CREATE OR REPLACE FUNCTION plan_mas_bajo_activo()
RETURNS TEXT
LANGUAGE sql
STABLE
AS $$
    SELECT p.nombre::TEXT
      FROM planes p
     WHERE p.activo
     ORDER BY p.precio_monthly,
              (p.max_numeros_whatsapp IS NULL),
              COALESCE(p.max_numeros_whatsapp, 0),
              p.nombre COLLATE "C"
     LIMIT 1;
$$;


-- ------------------------------------------------------------
-- Negocios sin suscripción vigente
-- ------------------------------------------------------------
-- Los que rige el plan más bajo activo. Los usan los triggers de abajo y los
-- de 59 cuando cambia ese plan (RF-11, caso 5).
CREATE OR REPLACE FUNCTION tenants_sin_suscripcion_vigente()
RETURNS SETOF UUID
LANGUAGE sql
STABLE
AS $$
    SELECT t.id
      FROM tenants t
     WHERE (SELECT sc.situacion FROM situacion_comercial(t.id) sc) = 'sin_suscripcion';
$$;


-- ------------------------------------------------------------
-- Reglas efectivas de un negocio: cupo, límites y herramientas
-- ------------------------------------------------------------
-- En el orden de RF-9, RF-10 y RF-11:
--   prueba           sin tope; modelos, historial y familias del plan asociado,
--                    o sin límites y catálogo completo si no tiene plan (D14).
--   suscripcion      todo del plan contratado, aunque esté desactivado (D20).
--   sin_suscripcion  todo del plan más bajo activo; si no hay ningún plan
--                    activo: sin tope, sin límites y catálogo completo, y
--                    `sin_plan_activo` en true (CL-20).
--
--   modelos          NULL = cualquier modelo; el primero es el default del plan.
--   historial        NULL = sin límite.
--   tope             NULL = sin tope.
--   familias         de plan_herramientas (o las cinco, sin plan).
--   plan, sin_plan_activo  de dónde salieron las reglas (para la bitácora).
CREATE OR REPLACE FUNCTION limites_efectivos(p_tenant UUID, p_ahora TIMESTAMPTZ DEFAULT NOW())
RETURNS TABLE (
    modelos          TEXT[],
    historial        INT,
    tope             INT,
    familias         TEXT[],
    plan             TEXT,
    sin_plan_activo  BOOLEAN
)
LANGUAGE plpgsql
STABLE
AS $$
#variable_conflict use_column
DECLARE
    v_situacion  TEXT;
    v_asociado   TEXT;
    v_nombre     TEXT;
    v_modelos    TEXT[];
    v_historial  INT;
    v_cupo       INT;
    v_todas      TEXT[] := ARRAY['calendario', 'google_docs', 'google_sheets', 'http', 'sql_lectura'];
BEGIN
    SELECT sc.situacion, sc.plan INTO v_situacion, v_asociado
      FROM situacion_comercial(p_tenant, p_ahora) sc;

    IF v_situacion = 'sin_suscripcion' THEN
        v_asociado := plan_mas_bajo_activo();
        IF v_asociado IS NULL THEN
            RETURN QUERY SELECT NULL::TEXT[], NULL::INT, NULL::INT, v_todas, NULL::TEXT, true;
            RETURN;
        END IF;
    END IF;

    SELECT p.nombre,
           CASE WHEN cardinality(p.modelos_permitidos) = 0 THEN NULL ELSE p.modelos_permitidos END,
           p.historial_maximo,
           p.max_numeros_whatsapp
      INTO v_nombre, v_modelos, v_historial, v_cupo
      FROM planes p
     WHERE p.nombre = v_asociado;

    IF NOT FOUND THEN
        -- Prueba sin plan asociado (o con uno que ya no está en el catálogo).
        RETURN QUERY SELECT NULL::TEXT[], NULL::INT, NULL::INT, v_todas, NULL::TEXT, false;
        RETURN;
    END IF;

    RETURN QUERY SELECT
        v_modelos,
        v_historial,
        CASE WHEN v_situacion = 'prueba' THEN NULL ELSE v_cupo END,
        ARRAY(SELECT ph.familia::TEXT FROM plan_herramientas ph
               WHERE ph.plan_nombre = v_nombre ORDER BY ph.familia),
        v_nombre::TEXT,
        false;
END
$$;


-- ------------------------------------------------------------
-- Recorte de UN negocio a los límites que le tocan
-- ------------------------------------------------------------
-- Sustituye al de 45_limites_agente_plan.sql (mismo nombre y firma). Recorta,
-- solo hacia abajo:
--   tenant_agent_config         configuración de Instagram, Facebook y widget
--   config_referencia_negocio   la referencia para números nuevos
--   agentes_canal               cada número, activo o pausado
-- Un modelo que el plan retiró (o NULL) pasa al primero de la lista del plan.
-- NO toca `agentes_canal.system_prompt`: un modelo retirado no deja el número
-- incompleto (RF-10). El aviso, la bitácora `recorte_por_plan` y la retirada
-- de herramientas son del servicio de reevaluación, no de este trigger.
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
       );
END
$$ LANGUAGE plpgsql;


-- Recorta a todos los negocios sin suscripción vigente: se usa cuando cambia
-- el plan más bajo activo o sus límites (RF-11, caso 5). Un negocio a la vez,
-- porque cada uno decide con su propia situación. Los cambios en `planes` son
-- raros (los hace gerencia a mano), así que el coste es aceptable.
CREATE OR REPLACE FUNCTION recortar_agentes_sin_suscripcion() RETURNS VOID AS $$
DECLARE
    v_tenant UUID;
BEGIN
    FOR v_tenant IN SELECT x FROM tenants_sin_suscripcion_vigente() AS x LOOP
        PERFORM recortar_agente_a_plan(v_tenant);
    END LOOP;
END
$$ LANGUAGE plpgsql;


-- ------------------------------------------------------------
-- Triggers: cualquier cosa que cambie la situación o los límites
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION trg_recortar_agente_negocio() RETURNS TRIGGER AS $$
BEGIN
    PERFORM recortar_agente_a_plan(NEW.tenant_id);
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

-- Cambian los límites de un plan (o lo que decide cuál es el más bajo): se
-- recortan los negocios que lo tienen contratado y, si afecta al plan más
-- bajo activo, los que no tienen suscripción vigente.
CREATE OR REPLACE FUNCTION trg_recortar_agente_plan() RETURNS TRIGGER AS $$
DECLARE
    v_tenant UUID;
BEGIN
    FOR v_tenant IN SELECT tenant_id FROM tenant_subscriptions WHERE plan = NEW.nombre LOOP
        PERFORM recortar_agente_a_plan(v_tenant);
    END LOOP;

    IF NEW.nombre = plan_mas_bajo_activo()
       OR OLD.precio_monthly IS DISTINCT FROM NEW.precio_monthly
       OR OLD.activo IS DISTINCT FROM NEW.activo
       OR OLD.max_numeros_whatsapp IS DISTINCT FROM NEW.max_numeros_whatsapp THEN
        PERFORM recortar_agentes_sin_suscripcion();
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

-- Alta o baja de un plan: puede cambiar cuál es el más bajo activo.
CREATE OR REPLACE FUNCTION trg_recortar_agente_plan_alta_baja() RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.activo AND NEW.nombre = plan_mas_bajo_activo() THEN
            PERFORM recortar_agentes_sin_suscripcion();
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.activo THEN
        PERFORM recortar_agentes_sin_suscripcion();
    END IF;
    RETURN OLD;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS recortar_agente_suscripcion ON tenant_subscriptions;
CREATE TRIGGER recortar_agente_suscripcion
    AFTER INSERT OR UPDATE OF plan, estado, origen, fecha_renovacion ON tenant_subscriptions
    FOR EACH ROW EXECUTE FUNCTION trg_recortar_agente_negocio();

-- Entra o sale del piloto ('prueba'): cambia qué límites rigen.
DROP TRIGGER IF EXISTS recortar_agente_estado ON tenant_estado_plataforma;
CREATE TRIGGER recortar_agente_estado
    AFTER INSERT OR UPDATE OF estado ON tenant_estado_plataforma
    FOR EACH ROW EXECUTE FUNCTION trg_recortar_agente_negocio();

DROP TRIGGER IF EXISTS recortar_agente_plan ON planes;
CREATE TRIGGER recortar_agente_plan
    AFTER UPDATE OF modelos_permitidos, historial_maximo, precio_monthly, activo, max_numeros_whatsapp
    ON planes
    FOR EACH ROW EXECUTE FUNCTION trg_recortar_agente_plan();

DROP TRIGGER IF EXISTS recortar_agente_plan_alta_baja ON planes;
CREATE TRIGGER recortar_agente_plan_alta_baja
    AFTER INSERT OR DELETE ON planes
    FOR EACH ROW EXECUTE FUNCTION trg_recortar_agente_plan_alta_baja();


-- ------------------------------------------------------------
-- resuelve_tenant: de qué negocio es el número/página que recibe
-- ------------------------------------------------------------
-- Reemplaza al de 52_agente_funciones.sql. Para WhatsApp busca en
-- `agentes_canal` entre los números VINCULADOS (activos y pausados: un número
-- pausado sigue siendo del negocio y guarda sus mensajes). Un desvinculado no
-- resuelve a ningún negocio aquí; para su último dueño está `resuelve_numero`.
-- Para el resto de canales, igual que antes (`channel_credentials`).
-- Sigue rechazando el identificador vacío (ver 52).
CREATE OR REPLACE FUNCTION public.resuelve_tenant(
    p_channel    character varying,
    p_identifier character varying
)
RETURNS uuid
LANGUAGE sql
STABLE
AS $function$
    SELECT x.tenant_id
    FROM (
        SELECT a.tenant_id
          FROM agentes_canal a
         WHERE p_channel = 'whatsapp'
           AND NULLIF(p_identifier, '') IS NOT NULL
           AND a.phone_number_id = p_identifier
           AND a.estado IN ('activo', 'pausado')
        UNION ALL
        SELECT c.tenant_id
          FROM channel_credentials c
         WHERE p_channel <> 'whatsapp'
           AND c.channel_type = p_channel
           AND c.is_active = true
           AND NULLIF(p_identifier, '') IS NOT NULL
           AND p_identifier IN (
               COALESCE(c.phone_number_id, ''),
               COALESCE(c.page_id, ''),
               COALESCE(c.ig_user_id, '')
           )
    ) x
    LIMIT 1;
$function$;


-- ------------------------------------------------------------
-- resuelve_numero: a qué número y negocio pertenece un identificador (RF-16)
-- ------------------------------------------------------------
-- Si hay un número vinculado (activo o pausado), ese. Si no, el ÚLTIMO
-- desvinculado: sus mensajes se guardan en las conversaciones ocultas de ese
-- negocio, sin respuesta (RF-14). Si no hay nada, ninguna fila: no se atribuye
-- a ningún negocio. Un identificador vacío no resuelve.
CREATE OR REPLACE FUNCTION resuelve_numero(p_identifier character varying)
RETURNS TABLE (agente_canal_id UUID, tenant_id UUID, estado TEXT)
LANGUAGE sql
STABLE
AS $$
    (
        SELECT a.id, a.tenant_id, a.estado::TEXT
          FROM agentes_canal a
         WHERE NULLIF(p_identifier, '') IS NOT NULL
           AND a.phone_number_id = p_identifier
           AND a.estado IN ('activo', 'pausado')
         LIMIT 1
    )
    UNION ALL
    (
        SELECT a.id, a.tenant_id, a.estado::TEXT
          FROM agentes_canal a
         WHERE NULLIF(p_identifier, '') IS NOT NULL
           AND a.phone_number_id = p_identifier
           AND a.estado = 'desvinculado'
           AND NOT EXISTS (
               SELECT 1 FROM agentes_canal v
                WHERE v.phone_number_id = p_identifier
                  AND v.estado IN ('activo', 'pausado')
           )
         ORDER BY a.desvinculado_en DESC, a.id
         LIMIT 1
    );
$$;
