-- ============================================================
-- 52 · Funciones que usa el agente (antes solo existían en la base)
--
-- `resuelve_tenant` y `dentro_de_ventana_24h` se crearon del lado de n8n y
-- nunca estuvieron en sql/: vivían únicamente en la base de producción. Un
-- despliegue limpio (o cualquier entorno local) se quedaba sin ellas, y con
-- el agente ya en código eso significa que no arranca. `aplicar_sql.py
-- --revisar` las lista desde ahora.
--
-- Se recogen tal como estaban en producción, con un solo cambio de
-- comportamiento, explicado abajo: el identificador vacío.
--
-- `obtiene_app_secret` NO está aquí a propósito: su definición tiene un
-- problema de seguridad que no se arregla copiándola. Ver 53.
--
--     python aplicar_sql.py sql/52_agente_funciones.sql
-- ============================================================


-- ------------------------------------------------------------
-- resuelve_tenant: de qué negocio es el número/página que recibe
-- ------------------------------------------------------------
-- Cambio respecto de la versión que había en producción: se rechaza el
-- identificador vacío.
--
-- El original comparaba `p_identifier IN (COALESCE(c.phone_number_id, ''), ...)`.
-- Con `p_identifier = ''` eso casa con CUALQUIER fila que tenga un NULL en
-- alguna de las tres columnas —casi todas, porque un canal de WhatsApp no
-- tiene page_id— y devuelve un tenant arbitrario. Es decir: un webhook sin
-- identificador se atribuía a un negocio al azar.
--
-- Nadie lo disparaba: n8n mandaba '(sin-id)' con un NULLIF, y
-- `services/agente/ingreso.py` corta antes si no hay identificador. Pero la
-- función no debería depender de que todos sus llamadores se acuerden.
CREATE OR REPLACE FUNCTION public.resuelve_tenant(
    p_channel    character varying,
    p_identifier character varying
)
RETURNS uuid
LANGUAGE sql
STABLE
AS $function$
    SELECT c.tenant_id
    FROM channel_credentials c
    WHERE c.channel_type = p_channel
      AND c.is_active = true
      AND NULLIF(p_identifier, '') IS NOT NULL
      AND p_identifier IN (
          COALESCE(c.phone_number_id, ''),
          COALESCE(c.page_id, ''),
          COALESCE(c.ig_user_id, '')
      )
    LIMIT 1;
$function$;


-- ------------------------------------------------------------
-- dentro_de_ventana_24h: si WhatsApp permite texto libre
-- ------------------------------------------------------------
-- Idéntica a la de producción. Sin mensajes del cliente devuelve false, que
-- es lo correcto: sin ventana abierta hay que usar plantilla aprobada.
--
-- Nota sobre tipos: `NOW()` es timestamptz y `messages.created_at` es
-- timestamp sin zona, así que Postgres convierte la segunda con la zona de
-- la sesión. Funciona mientras esa zona sea la misma con la que se
-- escribieron las filas. Es la misma inconsistencia que recorre la frontera
-- entre las tablas de n8n (sin zona) y las del portal (con zona); se deja
-- como está para no cambiar el comportamiento en una migración de rescate.
CREATE OR REPLACE FUNCTION public.dentro_de_ventana_24h(p_conversation_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
AS $function$
    SELECT COALESCE(
        (
            SELECT NOW() - MAX(created_at) < INTERVAL '24 hours'
            FROM messages
            WHERE conversation_id = p_conversation_id
              AND role = 'user'
        ),
        false
    );
$function$;
