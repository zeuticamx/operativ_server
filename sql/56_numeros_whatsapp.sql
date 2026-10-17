-- ============================================================
-- 56 · Varios números de WhatsApp por negocio (spec 005a): modelo de datos
-- ============================================================
-- Hasta ahora un negocio tenía UN WhatsApp: `channel_credentials` y
-- `tenant_channels` tienen PK (tenant_id, channel_type) y la configuración
-- del agente (`tenant_agent_config`) PK tenant_id. Esta migración crea la
-- tabla `agentes_canal`, una fila por número, con su estado y su propia
-- configuración de agente, y las tablas que cuelgan de ella.
--
-- `channel_credentials` NO cambia: sigue siendo la credencial (proveedor y
-- clave) por negocio y canal. Instagram, Facebook y el widget siguen en
-- `tenant_agent_config`, con su conjunto de herramientas en
-- `social_herramientas`.
--
-- Solo estructura: la copia de los números y agentes existentes está al final
-- de 59 (necesita los objetos de 57 y 58). Esta migración es aditiva: el
-- código anterior sigue funcionando.
--
-- Idempotente: aplicar_sql.py reaplica los archivos. Las restricciones van
-- dentro de CREATE TABLE IF NOT EXISTS (la tabla es nueva), los índices con
-- IF NOT EXISTS y la columna nueva de `conversations` con IF NOT EXISTS.
--
--     python aplicar_sql.py sql/56_numeros_whatsapp.sql
-- ============================================================


-- ------------------------------------------------------------
-- Configuración incompleta (RF-8)
-- ------------------------------------------------------------
-- Sin prompt, con el prompt en blanco o sin nombre de agente. Misma regla que
-- `services.numeros_reglas.configuracion_incompleta` (que usa str.strip()).
-- "En blanco" son los espacios en blanco habituales (espacio, tabulador, salto
-- de línea); un espacio exótico de Unicode depende de la configuración
-- regional de la base y no se promete.
-- Va en la base porque la usa un CHECK: el principal nunca queda incompleto,
-- ni siquiera por SQL a mano.
CREATE OR REPLACE FUNCTION configuracion_incompleta(p_agent_name TEXT, p_system_prompt TEXT)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT COALESCE(p_system_prompt, '') ~ '^[[:space:]]*$'
        OR COALESCE(p_agent_name, '')    ~ '^[[:space:]]*$';
$$;


-- ------------------------------------------------------------
-- agentes_canal: un número de WhatsApp de un negocio
-- ------------------------------------------------------------
--   estado        activo        responde (si el agente está encendido y la
--                               configuración está completa) y ocupa una plaza.
--                 pausado       sigue vinculado (el identificador es suyo, los
--                               mensajes se guardan) pero no responde ni envía.
--                               No ocupa plaza.
--                 desvinculado  ya no pertenece al negocio. La fila se conserva
--                               12 meses por sus conversaciones ocultas.
--   es_principal  fuente de copia de los números nuevos. Un solo principal por
--                 negocio, siempre activo, completo y distinto de la línea de
--                 avisos. Para pausar o desvincular al principal el servicio
--                 primero le quita el papel, en la misma transacción.
--   es_linea_avisos  la línea de WhatsApp de los avisos de la plataforma
--                 (spec 004). No cuenta para el cupo ni puede ser principal.
--                 La marca la pone el servicio (necesita una variable de
--                 entorno que SQL no ve).
--   vinculado_en  antigüedad: define quién se pausa primero y quién pasa a
--                 principal. No cambia al pausar o reactivar; solo al vincular
--                 de nuevo. Desempate: id más bajo.
CREATE TABLE IF NOT EXISTS agentes_canal (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    phone_number_id   VARCHAR(100) NOT NULL,
    -- Para la pantalla; nulo si no se conoce.
    telefono_visible  VARCHAR(40),
    bsp_provider      VARCHAR(20) NOT NULL DEFAULT 'neuroapi',

    estado            VARCHAR(12) NOT NULL DEFAULT 'activo',
    es_principal      BOOLEAN NOT NULL DEFAULT false,
    es_linea_avisos   BOOLEAN NOT NULL DEFAULT false,

    vinculado_en      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    pausado_en        TIMESTAMPTZ,
    motivo_pausa      VARCHAR(60),
    desvinculado_en   TIMESTAMPTZ,

    -- Configuración del agente de ESTE número (RF-5).
    agent_name        TEXT NOT NULL DEFAULT '',
    system_prompt     TEXT NOT NULL DEFAULT '',
    model             VARCHAR(100),
    temperature       NUMERIC(3,2) NOT NULL DEFAULT 0.70,
    history_window    INT NOT NULL DEFAULT 30,
    agente_activo     BOOLEAN NOT NULL DEFAULT true,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT agentes_canal_bsp_provider_check
        CHECK (bsp_provider IN ('meta', 'neuroapi', 'kontesta')),
    CONSTRAINT agentes_canal_estado_check
        CHECK (estado IN ('activo', 'pausado', 'desvinculado')),
    CONSTRAINT agentes_canal_temperatura_check
        CHECK (temperature BETWEEN 0 AND 1),
    -- Un desvinculado siempre dice desde cuándo: la retención de 12 meses y
    -- "el último dueño" de un número (resuelve_numero) dependen de ese dato.
    CONSTRAINT agentes_canal_desvinculado_en_check
        CHECK (estado <> 'desvinculado' OR desvinculado_en IS NOT NULL),
    -- El principal es activo y no es la línea de avisos...
    CONSTRAINT agentes_canal_principal_valido_check
        CHECK (es_principal = false OR (estado = 'activo' AND es_linea_avisos = false)),
    -- ... y nunca tiene la configuración incompleta (D33).
    CONSTRAINT agentes_canal_principal_completo_check
        CHECK (es_principal = false OR NOT configuracion_incompleta(agent_name, system_prompt)),
    -- Para que las tablas hijas puedan exigir que número y negocio coincidan.
    CONSTRAINT agentes_canal_id_tenant_uq UNIQUE (id, tenant_id)
);

-- Un identificador vinculado (activo o pausado) pertenece a un solo negocio en
-- toda la plataforma (RF-3, RF-16). Los desvinculados no bloquean: el número
-- puede volver a vincularse, a este negocio o a otro. Es también el índice de
-- `resuelve_tenant` / `resuelve_numero`, que están en la ruta de cada mensaje.
CREATE UNIQUE INDEX IF NOT EXISTS uq_agentes_canal_numero_vinculado
    ON agentes_canal (phone_number_id)
    WHERE estado IN ('activo', 'pausado');

-- Un solo principal por negocio.
CREATE UNIQUE INDEX IF NOT EXISTS uq_agentes_canal_principal
    ON agentes_canal (tenant_id)
    WHERE es_principal;

CREATE INDEX IF NOT EXISTS idx_agentes_canal_tenant_estado
    ON agentes_canal (tenant_id, estado, vinculado_en);

-- "Último dueño" de un número desvinculado y retención de 12 meses.
CREATE INDEX IF NOT EXISTS idx_agentes_canal_desvinculados
    ON agentes_canal (phone_number_id, desvinculado_en DESC)
    WHERE estado = 'desvinculado';


-- ------------------------------------------------------------
-- Configuración de referencia del negocio
-- ------------------------------------------------------------
-- Lo que hereda un número nuevo cuando el negocio no tiene principal que
-- copiar (RF-5). Si no hay fila aquí, la referencia es `tenant_agent_config`
-- (así el cuestionario de bienvenida sigue sirviendo). `herramientas` son las
-- `tool_key` activas en esa referencia.
CREATE TABLE IF NOT EXISTS config_referencia_negocio (
    tenant_id       UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    agent_name      TEXT NOT NULL DEFAULT '',
    system_prompt   TEXT NOT NULL DEFAULT '',
    model           VARCHAR(100),
    temperature     NUMERIC(3,2) NOT NULL DEFAULT 0.70,
    history_window  INT NOT NULL DEFAULT 30,
    agente_activo   BOOLEAN NOT NULL DEFAULT true,
    herramientas    TEXT[] NOT NULL DEFAULT '{}',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT config_referencia_temperatura_check
        CHECK (temperature BETWEEN 0 AND 1)
);


-- ------------------------------------------------------------
-- Herramientas activas por número, y para Instagram/Facebook/widget
-- ------------------------------------------------------------
-- La herramienta es del negocio (`tenant_tools`); aquí se dice en qué números
-- está activa. Quitarla del negocio la quita de todos (RF-6): por eso la FK
-- a `tenant_tools` en cascada. La FK compuesta a `agentes_canal` impide que
-- una fila de un negocio cuelgue de un número de otro.
CREATE TABLE IF NOT EXISTS agente_canal_herramientas (
    agente_canal_id  UUID NOT NULL,
    tenant_id        UUID NOT NULL,
    tool_key         VARCHAR(100) NOT NULL,
    PRIMARY KEY (agente_canal_id, tool_key),
    FOREIGN KEY (agente_canal_id, tenant_id)
        REFERENCES agentes_canal (id, tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, tool_key)
        REFERENCES tenant_tools (tenant_id, tool_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agente_canal_herramientas_tool
    ON agente_canal_herramientas (tenant_id, tool_key);

CREATE TABLE IF NOT EXISTS social_herramientas (
    tenant_id  UUID NOT NULL,
    tool_key   VARCHAR(100) NOT NULL,
    PRIMARY KEY (tenant_id, tool_key),
    FOREIGN KEY (tenant_id, tool_key)
        REFERENCES tenant_tools (tenant_id, tool_key) ON DELETE CASCADE
);


-- ------------------------------------------------------------
-- Bitácora del negocio (RF-21)
-- ------------------------------------------------------------
-- Una fila por hecho: vinculación, pausa, principal, configuración, recorte,
-- herramienta... con su motivo y su autor. La lista de motivos es la de la
-- spec ("Contrato de códigos") y está cerrada: agregar uno nuevo es cambiar
-- la spec. El número puede borrarse (retención de 12 meses): la fila sigue,
-- sin número (`agente_canal_id` queda en NULL); `detalle` guarda lo necesario
-- para leerla (por ejemplo los 4 últimos dígitos).
CREATE TABLE IF NOT EXISTS numeros_bitacora (
    id                    BIGSERIAL PRIMARY KEY,
    tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agente_canal_id       UUID REFERENCES agentes_canal(id) ON DELETE SET NULL,
    motivo                VARCHAR(60) NOT NULL,
    autor_tipo            VARCHAR(10) NOT NULL,
    -- Quién, cuando es una persona del negocio. Texto y no FK: la cuenta
    -- puede desactivarse o borrarse y el rastro tiene que sobrevivir.
    autor_portal_user_id  UUID,
    autor_email           VARCHAR(255),
    detalle               JSONB NOT NULL DEFAULT '{}'::jsonb,
    creada_en             TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT numeros_bitacora_autor_tipo_check
        CHECK (autor_tipo IN ('gerencia', 'plataforma', 'sistema')),
    CONSTRAINT numeros_bitacora_motivo_check
        CHECK (motivo IN (
            'vinculacion', 'desvinculacion', 'pausa_manual', 'reactivacion_manual',
            'pausa_por_cambio_de_plan', 'pausa_por_fin_de_suscripcion',
            'pausa_por_fin_de_prueba', 'pausa_por_pago_de_prueba',
            'pausa_por_despliegue', 'pausa_por_cupo_bajado_por_plataforma',
            'pausa_por_plan_mas_bajo_recalculado', 'cambio_de_principal',
            'principal_sin_numero_activo', 'configuracion_incompleta',
            'configuracion_completada', 'recorte_por_plan',
            'herramienta_retirada_por_plan', 'herramienta_activada',
            'herramienta_desactivada', 'prompt_cambiado',
            'agente_encendido', 'agente_apagado'
        ))
);

CREATE INDEX IF NOT EXISTS idx_numeros_bitacora_tenant
    ON numeros_bitacora (tenant_id, creada_en DESC);


-- ------------------------------------------------------------
-- Intentos de alta de un número ajeno (RF-2, paso 1)
-- ------------------------------------------------------------
-- Una fila por cada 404 de alta ajena. Bloqueo = 5 en las últimas 24 horas
-- (ventana deslizante). En tabla y no en memoria para que valga con varios
-- procesos. Un trabajo borra las de más de 25 horas.
CREATE TABLE IF NOT EXISTS intentos_alta_ajena (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_intentos_alta_ajena_tenant
    ON intentos_alta_ajena (tenant_id, creado_en DESC);


-- ------------------------------------------------------------
-- Cada conversación de WhatsApp queda asociada al número por el que entró
-- (RF-13). Las de Instagram, Facebook y web quedan en NULL (usan la
-- configuración social). SET NULL y no CASCADE: borrar un número no debe
-- llevarse conversaciones por sorpresa; la retención las borra aparte.
-- ------------------------------------------------------------
ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS agente_canal_id UUID REFERENCES agentes_canal(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_conversations_agente_canal
    ON conversations (tenant_id, agente_canal_id, status);
