-- ============================================================
-- 53 · obtiene_app_secret: retirar la clave de cifrado de ejemplo
--
-- ⚠️  NO SE APLICA SOLO. Lee esto entero antes de ejecutarlo.
--
-- La función en producción descifra con una clave escrita a mano:
--
--     pgp_sym_decrypt(ma.app_secret_cifrado, 'TU_CLAVE_MAESTRA')
--
-- 'TU_CLAVE_MAESTRA' es el texto de ejemplo que nunca se reemplazó. Las
-- otras dos funciones equivalentes del mismo esquema lo hacen bien y usan
-- `current_setting('app.cred_key')`:
--
--     get_channel_credentials  (sql/23_channel_credentials_bsp_provider.sql)
--     get_tool_credentials     (sql/05_herramientas.sql)
--
-- Por qué importa: `app_secret` es lo que valida la firma HMAC de los
-- webhooks de Meta (X-Hub-Signature-256). Quien lo tenga puede falsificar
-- mensajes entrantes de cualquier tenant: meter conversaciones inventadas,
-- hacer responder al agente y gastarle créditos al negocio. Y la clave es
-- legible por cualquiera que pueda consultar `pg_proc`, que es cualquiera
-- con acceso de lectura a la base.
--
-- ------------------------------------------------------------
-- Orden correcto. Los tres pasos, o ninguno.
-- ------------------------------------------------------------
-- 1. Este archivo: re-cifra los secretos con `app.cred_key` y cambia la
--    función. Va en UNA transacción (aplicar_sql.py ya lo hace así): si el
--    re-cifrado falla, la función no cambia y nada queda ilegible.
--
-- 2. Rotar los app_secret en Meta. El re-cifrado protege los secretos de
--    aquí en adelante, pero los valores actuales estuvieron accesibles bajo
--    una clave pública, así que hay que darlos por comprometidos. Se cambia
--    en developers.facebook.com → la app → Configuración → Básica, y se
--    vuelve a guardar en `meta_apps`.
--
-- 3. Hasta el paso 2, cualquier firma que valide esta función se apoya en un
--    secreto que pudo filtrarse. No es motivo para no hacer el paso 1 ya.
--
-- ------------------------------------------------------------
-- Antes de ejecutar
-- ------------------------------------------------------------
--   - Comprueba que `app.cred_key` está configurada:
--         python aplicar_sql.py --revisar
--     (la última línea la reporta; si dice NO CONFIGURADA, para aquí)
--   - Haz copia de `meta_apps`. El re-cifrado reescribe la columna:
--         CREATE TABLE meta_apps_respaldo_53 AS SELECT * FROM meta_apps;
--   - Aplicar con n8n ya apagado, o mientras no entren webhooks: entre el
--     UPDATE y el CREATE OR REPLACE la función vieja ya no sabe descifrar.
--
--     python aplicar_sql.py sql/53_app_secret_clave_maestra.sql
-- ============================================================


-- ------------------------------------------------------------
-- Paso 1a · re-cifrar con la clave de verdad
-- ------------------------------------------------------------
-- Solo las filas que todavía abren con la clave de ejemplo.
--
-- OJO, esto NO es idempotente, aunque lo parezca: `pgp_sym_decrypt` con la
-- clave equivocada LANZA ERROR ("Wrong key or corrupt data"), no devuelve
-- NULL. Si se vuelve a ejecutar este archivo después de una pasada buena,
-- las filas ya re-cifradas hacen fallar este UPDATE y la transacción entera
-- se deshace. No rompe nada —la función ya quedó bien en la primera pasada—
-- pero verás un error, no un "no hizo falta".
--
-- Ese mismo detalle es lo que garantiza que no haya re-cifrados a medias: si
-- una sola fila con secreto no abriera con la clave de ejemplo, este UPDATE
-- aborta y no se cambia nada. O se migran todas, o ninguna.
UPDATE meta_apps
   SET app_secret_cifrado = pgp_sym_encrypt(
           pgp_sym_decrypt(app_secret_cifrado, ''),
           current_setting('app.cred_key')
       )
 WHERE app_secret_cifrado IS NOT NULL
   AND pgp_sym_decrypt(app_secret_cifrado, '') IS NOT NULL;


-- ------------------------------------------------------------
-- Paso 1b · la función, igual que sus dos hermanas
-- ------------------------------------------------------------
-- Se aprovecha para cerrar el mismo agujero del identificador vacío que
-- `resuelve_tenant` (ver sql/52): sin el guard, `obtiene_app_secret(canal, '')`
-- devolvía el secreto de un tenant arbitrario.
CREATE OR REPLACE FUNCTION public.obtiene_app_secret(
    p_channel    text,
    p_identifier text
)
RETURNS text
LANGUAGE sql
STABLE
AS $function$
    SELECT pgp_sym_decrypt(ma.app_secret_cifrado, current_setting('app.cred_key'))
    FROM channel_credentials cc
    JOIN meta_apps ma ON ma.id = cc.meta_app_id
    WHERE cc.channel_type = p_channel
      AND cc.is_active = true
      AND NULLIF(p_identifier, '') IS NOT NULL
      AND p_identifier IN (
        COALESCE(cc.phone_number_id, ''),
        COALESCE(cc.page_id, ''),
        COALESCE(cc.ig_user_id, '')
      )
    LIMIT 1;
$function$;


-- ------------------------------------------------------------
-- Comprobación
-- ------------------------------------------------------------
-- Debe devolver el app_secret en claro de un canal activo.
--
--     SELECT cc.channel_type,
--            cc.phone_number_id,
--            obtiene_app_secret(cc.channel_type, cc.phone_number_id) IS NOT NULL
--                AS descifra
--       FROM channel_credentials cc
--      WHERE cc.is_active = true AND cc.phone_number_id IS NOT NULL;
--
-- Un `descifra = false` NO significa por sí solo que se haya perdido un
-- secreto, y conviene saber leerlo antes de restaurar nada. Por lo de arriba,
-- un secreto cifrado con otra clave haría FALLAR esta consulta con error; que
-- devuelva false significa que `obtiene_app_secret` devolvió NULL, y eso solo
-- pasa cuando no hay nada que descifrar: `meta_app_id` nulo o apuntando a una
-- fila que no existe (el JOIN es interno), o `app_secret_cifrado` nulo.
--
-- Es lo esperable en una línea de WhatsApp que entra por un BSP: ahí la firma
-- la valida el proxy interno con su token (`firma.py`, camino 1) y el
-- app_secret de Meta no se usa. Hoy es el camino de TODO WhatsApp.
--
-- Para distinguir un caso del otro, sin descifrar nada:
--
--     SELECT cc.phone_number_id,
--            cc.bsp_provider,
--            (cc.meta_app_id IS NULL)        AS sin_meta_app,
--            (ma.id IS NULL)                 AS meta_app_no_existe,
--            (ma.app_secret_cifrado IS NULL) AS sin_secreto_guardado
--       FROM channel_credentials cc
--       LEFT JOIN meta_apps ma ON ma.id = cc.meta_app_id
--      WHERE cc.is_active = true AND cc.phone_number_id IS NOT NULL;
--
--   - sin_meta_app / sin_secreto_guardado en true -> no había secreto. El
--     re-cifrado no tenía nada que hacer con esa fila y no se perdió nada.
--   - hay meta_app con secreto guardado y aun así no descifra -> eso sí es un
--     secreto ilegible: restaura meta_apps_respaldo_53 y revisa.
--
-- Cuando esté confirmado:  DROP TABLE meta_apps_respaldo_53;
--
-- Y recuerda que soltar la copia no cierra esto: el paso 2 —rotar los
-- app_secret en Meta— sigue pendiente, porque los valores actuales estuvieron
-- cifrados con una clave que cualquiera podía leer en pg_proc.
