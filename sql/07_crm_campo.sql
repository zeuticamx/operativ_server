-- ============================================================
-- CRM DE CAMPO: cartera, visitas con geocerca y seguimientos
-- ============================================================
-- Depende de 06_vendedores.sql (tabla `vendedores`). Aplicar después.
--
-- Convive con el embudo de chat de 06 sin tocarlo. Son dos carteras
-- distintas del mismo equipo:
--   client_pipeline -> contactos que escriben por WhatsApp/IG (tabla `users`)
--   clientes        -> negocios físicos que el vendedor visita en persona
-- El único punto en común es `vendedores`. Si algún día hay que enlazarlos,
-- es una columna user_id nullable acá, no una fusión de las dos tablas.
--
-- Sobre los nombres: este módulo usa marcas de tiempo en español
-- (creado_en / actualizado_en / completado_en), igual que 06_vendedores.sql.
-- El resto del portal (portal_users, tenant_tools, meta_connections) usa
-- created_at en inglés. La frontera es a propósito: el módulo de vendedores
-- entero habla español.
--
-- El rol 'vendedor' NO necesita migración: portal_users.role es
-- VARCHAR(50) sin CHECK, así que admite el valor nuevo tal cual.
-- ============================================================


-- ------------------------------------------------------------
-- Cartera de clientes
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clientes (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- Nullable: un cliente puede entrar a la cartera del negocio antes de
    -- que gerencia decida a qué vendedor le toca.
    vendedor_id             UUID REFERENCES vendedores(id) ON DELETE SET NULL,
    nombre_negocio          TEXT NOT NULL,
    contacto_nombre         TEXT,
    telefono                TEXT,
    direccion               TEXT,
    -- Centro de la geocerca. NOT NULL: sin coordenadas no se puede validar
    -- una visita, que es el motivo de existir de esta tabla.
    latitud                 DOUBLE PRECISION NOT NULL,
    longitud                DOUBLE PRECISION NOT NULL,
    -- Radio permitido para dar por buena una visita. Por cliente y no
    -- global: un local en plaza comercial necesita más margen que uno
    -- con entrada a la calle.
    radio_tolerancia_metros INTEGER NOT NULL DEFAULT 120,
    estado                  TEXT NOT NULL DEFAULT 'prospecto'
        CHECK (estado IN ('prospecto', 'activo', 'inactivo', 'perdido')),
    prioridad               TEXT NOT NULL DEFAULT 'media'
        CHECK (prioridad IN ('alta', 'media', 'baja')),
    notas                   TEXT,
    creado_en               TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado_en          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Coordenadas imposibles no deben llegar nunca a la BD, aunque el
    -- validador de Pydantic cambie o alguien inserte por psql.
    CONSTRAINT clientes_lat_valida CHECK (latitud BETWEEN -90 AND 90),
    CONSTRAINT clientes_lon_valida CHECK (longitud BETWEEN -180 AND 180),
    CONSTRAINT clientes_radio_valido CHECK (radio_tolerancia_metros > 0)
);

CREATE INDEX IF NOT EXISTS idx_clientes_vendedor ON clientes (vendedor_id);
CREATE INDEX IF NOT EXISTS idx_clientes_tenant   ON clientes (tenant_id);


-- ------------------------------------------------------------
-- Visitas / check-ins con geolocalización
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS visitas (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    vendedor_id                 UUID NOT NULL REFERENCES vendedores(id),
    cliente_id                  UUID NOT NULL REFERENCES clientes(id),
    -- Dónde estaba el teléfono al hacer el check-in.
    latitud                     DOUBLE PRECISION NOT NULL,
    longitud                    DOUBLE PRECISION NOT NULL,
    -- Precisión que reportó el GPS. Se guarda pero no decide nada: una
    -- lectura mala se ve después en el reporte.
    accuracy_metros             DOUBLE PRECISION,
    -- Se persiste el resultado del cálculo, no se recalcula al leer: si
    -- mañana mueven el pin del cliente o cambian el radio, las visitas ya
    -- validadas no pueden cambiar de veredicto retroactivamente.
    distancia_calculada_metros  DOUBLE PRECISION NOT NULL,
    dentro_de_geocerca          BOOLEAN NOT NULL,
    foto_url                    TEXT,
    comentario                  TEXT,
    -- Hora del teléfono: puede venir desfasada o manipulada, por eso
    -- convive con timestamp_servidor en vez de reemplazarlo.
    timestamp_dispositivo       TIMESTAMPTZ,
    timestamp_servidor          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Lo genera la app antes de salir a la calle. Es la llave de
    -- idempotencia de /visitas/sync: reintentar el mismo lote no duplica.
    cliente_uuid_offline        UUID,
    creado_en                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT visitas_lat_valida CHECK (latitud BETWEEN -90 AND 90),
    CONSTRAINT visitas_lon_valida CHECK (longitud BETWEEN -180 AND 180)
);

-- Parcial: las visitas hechas en línea no traen uuid offline y varias
-- pueden tener NULL sin chocar entre sí.
CREATE UNIQUE INDEX IF NOT EXISTS idx_visitas_offline_uuid
    ON visitas (cliente_uuid_offline)
    WHERE cliente_uuid_offline IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_visitas_cliente
    ON visitas (cliente_id);

CREATE INDEX IF NOT EXISTS idx_visitas_vendedor_fecha
    ON visitas (vendedor_id, timestamp_servidor);

-- El reporte de actividad agrupa por tenant y rango de fechas.
CREATE INDEX IF NOT EXISTS idx_visitas_tenant_fecha
    ON visitas (tenant_id, timestamp_servidor);


-- ------------------------------------------------------------
-- Tareas / seguimientos
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tareas_seguimiento (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    vendedor_id         UUID NOT NULL REFERENCES vendedores(id),
    cliente_id          UUID NOT NULL REFERENCES clientes(id),
    titulo              TEXT NOT NULL,
    descripcion         TEXT,
    fecha_programada    TIMESTAMPTZ NOT NULL,
    estado              TEXT NOT NULL DEFAULT 'pendiente'
        CHECK (estado IN ('pendiente', 'completada', 'vencida')),
    -- Solo tiene valor cuando estado = 'completada'.
    completado_en       TIMESTAMPTZ,
    creado_en           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tareas_completada_coherente CHECK (
        (estado = 'completada' AND completado_en IS NOT NULL)
        OR (estado <> 'completada' AND completado_en IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_tareas_vendedor_estado
    ON tareas_seguimiento (vendedor_id, estado);

-- "Qué tengo pendiente hoy" y el barrido de vencidas.
CREATE INDEX IF NOT EXISTS idx_tareas_tenant_fecha
    ON tareas_seguimiento (tenant_id, fecha_programada);


-- ------------------------------------------------------------
-- Distancia entre dos coordenadas, en metros (Haversine)
-- ------------------------------------------------------------
-- La validación de la geocerca en el check-in se hace en Python (geo.py)
-- para poder probarla sin base de datos. Esta función existe para el
-- mismo cálculo desde SQL: reportes, consultas ad-hoc y recálculos
-- masivos. Las dos usan el mismo radio terrestre (6371000 m) y hay un
-- test que compara ambas para que no se separen.
CREATE OR REPLACE FUNCTION distancia_metros(
    lat1 DOUBLE PRECISION, lon1 DOUBLE PRECISION,
    lat2 DOUBLE PRECISION, lon2 DOUBLE PRECISION
) RETURNS DOUBLE PRECISION AS $$
    SELECT 2 * 6371000 * asin(sqrt(
        sin(radians(lat2 - lat1) / 2) ^ 2 +
        cos(radians(lat1)) * cos(radians(lat2)) *
        sin(radians(lon2 - lon1) / 2) ^ 2
    ));
$$ LANGUAGE sql IMMUTABLE;
