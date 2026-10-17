-- ============================================================
-- 54 · Tablas que usa el envío del agente (el resto del hueco de n8n)
--
-- Mismo problema que sql/52, pero con tablas en lugar de funciones:
-- `delivery_errors`, `tenant_templates` y `conversations.esperando_reapertura`
-- las creó el lado de n8n y nunca estuvieron en sql/. Viven solo en la base
-- de producción, así que un despliegue limpio —o cualquier entorno local—
-- se queda sin ellas. Comprobado: no existían en la base local.
--
-- Con el agente ya en código eso se nota de tres formas distintas, y una es
-- silenciosa:
--
--   - `delivery_errors`: `envio._registrar_error` envuelve el INSERT en un
--     try/except para no tumbar la entrega por no poder anotar el fallo. Sin
--     la tabla no se cae nada: se pierden TODOS los diagnósticos de envío,
--     que es justo lo que gerencia consulta cuando un negocio pregunta por
--     qué no llegó su mensaje.
--   - `tenant_templates`: sin la tabla, `_reabrir_con_plantilla` levanta al
--     consultar, y es el único camino que tiene WhatsApp para contestar
--     fuera de la ventana de 24 h.
--   - `conversations.esperando_reapertura`: la escribe `envio` al mandar la
--     plantilla y la apaga `ingreso` cuando el cliente responde. Sin la
--     columna fallan las dos.
--
-- La forma de las columnas está CONTRASTADA contra producción
-- (information_schema, 16/10/2026) y hay diferencias deliberadas respecto de
-- lo que hay allí: están todas listadas al final del archivo. Todo va con
-- IF NOT EXISTS, así que en producción esto no toca nada.
--
--     python aplicar_sql.py sql/54_agente_envio_tablas.sql
-- ============================================================


-- Igual que en sql/55, y por el mismo susto: un cliente con el search_path
-- vacío busca los nombres sin esquema en `pg_catalog` y este archivo falla
-- con "relation does not exist" sobre tablas que sí existen.
SET search_path TO public;


-- ------------------------------------------------------------
-- delivery_errors: por qué no llegó un mensaje
-- ------------------------------------------------------------
-- Lo que escribe envio.py:
--     INSERT INTO delivery_errors
--         (tenant_id, channel_type, user_id, error_code, diagnostico)
--
-- Ojo con `user_id`: acá es el identificador del lado del canal —el número
-- de WhatsApp o el PSID de Meta—, un texto, y NO la FK a `users` que lleva
-- ese mismo nombre en `conversations`. Confirmado contra producción, que lo
-- tiene como character varying. Es el nombre que puso n8n y se respeta para
-- no romper lo que ya hay escrito.
--
-- No se guarda el texto del mensaje, a propósito (hallazgo 3 de la
-- auditoría): canal, destinatario y motivo bastan para diagnosticar, y un
-- registro de errores no es sitio para el contenido de una conversación.
CREATE TABLE IF NOT EXISTS delivery_errors (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- NOT NULL + FK donde producción tiene una columna suelta: ver (1) y (2)
    -- al final del archivo.
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- 50 y no 20 para igualar producción, aunque el valor más largo que
    -- existe sea 'instagram'.
    channel_type    VARCHAR(50) NOT NULL,
    -- Identificador en el canal (teléfono / PSID), no users.id.
    user_id         VARCHAR(100),
    -- Código de error de Meta (190, 131047, …). NULL cuando el fallo es de
    -- red o de credenciales y no hubo respuesta del canal que leer.
    error_code      INTEGER,
    diagnostico     TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Por lo mismo que los ALTER de `tenant_templates` más abajo: una tabla que
-- ya existe no la toca el CREATE de arriba, y la primera versión de este
-- archivo puso `channel_type` en 20. Ensanchar un varchar es solo un cambio
-- de metadatos, y en producción —que ya está en 50— no hace nada.
ALTER TABLE delivery_errors
    ALTER COLUMN channel_type TYPE VARCHAR(50);

-- Las consultas de gerencia son siempre "los últimos fallos de este
-- negocio".
CREATE INDEX IF NOT EXISTS idx_delivery_errors_tenant
    ON delivery_errors (tenant_id, created_at DESC);


-- ------------------------------------------------------------
-- tenant_templates: la plantilla aprobada de cada negocio
-- ------------------------------------------------------------
-- Lo que lee envio.py:
--     SELECT template_name, language_code
--       FROM tenant_templates
--      WHERE tenant_id = $1 AND is_active = true
--      ORDER BY created_at ASC
--      LIMIT 1
--
-- `ORDER BY created_at ASC LIMIT 1` es de n8n y se mantiene: con varias
-- activas gana la más antigua. No es un criterio brillante, pero es
-- determinista y es el que lleva usándose; cambiarlo acá cambiaría qué
-- plantilla sale en producción sin que nadie lo haya pedido.
--
-- Nada de esto crea plantillas: se dan de alta y se aprueban a mano en Meta
-- Business Manager, y el nombre aprobado se escribe aquí. Distinto de
-- `plantillas_whatsapp_globales` (sql/49), que es una sola plantilla de
-- plataforma para la cuenta global y no tiene tenant.
CREATE TABLE IF NOT EXISTS tenant_templates (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- Nombre exacto de la plantilla aprobada en Meta.
    template_name   VARCHAR(255) NOT NULL,
    -- envio.py cae a 'es_MX' si viene NULL. El default es el mismo que tiene
    -- producción.
    language_code   VARCHAR(10) DEFAULT 'es_MX',
    -- Estas dos existen en producción y ningún código las lee todavía: son la
    -- categoría de Meta (UTILITY / MARKETING / AUTHENTICATION) y una copia
    -- del cuerpo para poder mostrar la plantilla en el portal sin volver a
    -- pedírsela a Meta. Van aquí para que un despliegue limpio tenga la misma
    -- tabla que producción, no porque el agente las necesite.
    category        VARCHAR(50),
    body_preview    TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    -- Una plantilla no puede estar dos veces para el mismo negocio en el
    -- mismo idioma. Está en producción y aquí se declara igual, sin nombre
    -- explícito a propósito: el que genera Postgres
    -- (tenant_templates_tenant_id_template_name_language_code_key) es
    -- exactamente el que tiene producción.
    --
    -- Importa para la consulta del envío: con `ORDER BY created_at ASC
    -- LIMIT 1`, lo que decide qué plantilla sale es la antigüedad, y esta
    -- restricción es la que garantiza que no haya dos filas idénticas
    -- compitiendo por ese primer puesto.
    UNIQUE (tenant_id, template_name, language_code)
);

-- `category` y `body_preview` se añadieron a este archivo después de la
-- primera versión, y un CREATE TABLE IF NOT EXISTS no toca una tabla que ya
-- existe: sin estos dos ALTER, cualquier entorno que aplicó la versión
-- anterior se queda con la tabla incompleta y el archivo deja de ser
-- reproducible. Son los mismos que ya hay arriba, repetidos a propósito.
ALTER TABLE tenant_templates
    ADD COLUMN IF NOT EXISTS category     VARCHAR(50);
ALTER TABLE tenant_templates
    ADD COLUMN IF NOT EXISTS body_preview TEXT;

-- Y lo mismo con el UNIQUE, por el mismo motivo. `ADD CONSTRAINT` no tiene
-- IF NOT EXISTS en Postgres, así que se comprueba a mano (mismo patrón que
-- sql/23). Si la tabla ya traía la restricción —producción— esto no hace
-- nada.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'tenant_templates_tenant_id_template_name_language_code_key'
    ) THEN
        ALTER TABLE tenant_templates
            ADD CONSTRAINT tenant_templates_tenant_id_template_name_language_code_key
            UNIQUE (tenant_id, template_name, language_code);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_tenant_templates_activa
    ON tenant_templates (tenant_id, created_at ASC)
    WHERE is_active = true;


-- ------------------------------------------------------------
-- conversations.esperando_reapertura
-- ------------------------------------------------------------
-- Se enciende cuando la ventana de 24 h estaba cerrada y salió la plantilla
-- en lugar de la respuesta del agente; se apaga en `ingreso` cuando el
-- cliente contesta y el agente retoma. NOT NULL DEFAULT false para que las
-- conversaciones que ya existen queden como "no esperando", que es su
-- estado real.
ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS esperando_reapertura BOOLEAN NOT NULL DEFAULT false;


-- ============================================================
-- Contraste con producción — 16/10/2026
-- ============================================================
-- Consultado con:
--
--     SELECT table_name, column_name, data_type, is_nullable, column_default
--       FROM information_schema.columns
--      WHERE table_schema = 'public'
--        AND (table_name IN ('delivery_errors', 'tenant_templates')
--             OR (table_name = 'conversations'
--                 AND column_name = 'esperando_reapertura'))
--      ORDER BY table_name, ordinal_position;
--
-- Lo que importaba, y salió bien:
--
--   - `delivery_errors.user_id` es character varying, no uuid. Era el riesgo
--     grande: con uuid, todos los INSERT de envio.py vendrían fallando en
--     silencio (van dentro de un try/except) y no habría un solo diagnóstico
--     guardado. No es el caso.
--   - `error_code` es integer, así que 131047 entra.
--   - `tenant_templates.language_code` ya tenía default 'es_MX'.
--   - Ninguna columna NOT NULL sin default que estos INSERT no rellenen.
--
-- Lo que apareció y este archivo no tenía, y que ya está arriba:
--
--   - `tenant_templates.category` y `tenant_templates.body_preview`. Nadie
--     las lee todavía; se añaden para que un despliegue limpio no quede con
--     una tabla distinta de la de producción.
--   - UNIQUE (tenant_id, template_name, language_code) en
--     `tenant_templates`. Esta sí tiene consecuencia: es lo que impide que
--     haya dos filas idénticas compitiendo por el `LIMIT 1` del envío.
--
-- También se comprobaron las claves y las longitudes (pg_constraint e
-- information_schema), y las dos dudas que este archivo traía quedaron
-- resueltas EN FAVOR de producción:
--
--   - Las FKs existen. `delivery_errors` y `tenant_templates` tienen las dos
--     su FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
--     así que el DELETE FROM tenants de `services/eliminacion_tenant.py`
--     arrastra estas filas y no deja huérfanas. La sospecha que había aquí
--     —que una columna nullable delataba la falta de FK— era infundada: una
--     columna FK nullable es perfectamente válida, el NULL simplemente no
--     referencia nada.
--   - Las longitudes coinciden, incluida `template_name` en 255, que era la
--     que preocupaba. Única excepción: `delivery_errors.channel_type` es 50
--     en producción y aquí estaba en 20; ya se igualó.
--
-- Queda UNA diferencia deliberada, y va en la dirección de apretar, nunca de
-- aflojar; no puede romper el código, que siempre manda valor:
--
--   Nulabilidad. En producción son nullable `delivery_errors.tenant_id`,
--   `channel_type` y `created_at`, y también `tenant_templates.is_active` y
--   `conversations.esperando_reapertura`. Acá van NOT NULL: un despliegue
--   nuevo queda mejor y el comportamiento no cambia.
--
--   El caso de `is_active` merece una nota, porque en producción sí puede
--   morder: la consulta del envío filtra por `is_active = true`, y en SQL
--   NULL no es true, así que una plantilla con is_active NULL es invisible
--   para el envío —el negocio la ve dada de alta y la reapertura falla igual
--   que si no tuviera ninguna. Aquí no puede pasar.
--
-- Sobre `template_name` en VARCHAR(255): Meta admite nombres de hasta 512
-- caracteres, así que en teoría cabe un nombre que no quepa en la columna, y
-- eso dejaría a ese negocio sin poder reabrir conversaciones. Se deja en 255
-- porque es lo que tiene producción y el riesgo es remoto (los nombres de
-- plantilla reales son cortos: minúsculas y guiones bajos). Queda anotado por
-- si algún día aparece el caso raro.
