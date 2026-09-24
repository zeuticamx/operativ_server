-- ============================================================
-- MÓDULO DE CALENDARIOS (reservas para barberías/salones)
-- ============================================================
-- Opcional por tenant e independiente de agente_ia_activo y
-- gestion_vendedores_activo: un negocio puede tener las tres cosas, dos,
-- una sola, o ninguna. El interruptor es tenant_servicios.calendario_activo,
-- mismo patrón que 06_vendedores.sql.
--
-- Vocabulario de este módulo:
--   proveedores  -> el personal que atiende (barbero/estilista). Catálogo
--                   simple administrado por gerencia: SIN cuenta de portal
--                   propia (a diferencia de vendedores.portal_user_id) —
--                   acá gerencia es la única que entra al portal, el
--                   proveedor nunca inicia sesión.
--   servicios    -> lo que se puede reservar (corte, barba, tinte...), con
--                   una duración fija que define cuánto ocupa la agenda.
--   reservas     -> una cita: un proveedor, un servicio, un horario. La
--                   crea n8n (WhatsApp/IG/FB) o gerencia desde el portal
--                   (walk-in / teléfono).
--
-- Cualquier proveedor puede realizar cualquier servicio del tenant en esta
-- versión: no hay tabla puente servicio_proveedores. Es la opción más
-- simple que sigue cubriendo "varios barberos" — el filtro real de quién
-- atiende sale de la disponibilidad (horarios/excepciones), no de qué
-- servicios sabe hacer cada quien. Si más adelante hace falta restringir
-- (p. ej. "solo Fulanx hace coloración"), se agrega esa tabla puente sin
-- romper nada de acá: las consultas de disponibilidad ya reciben
-- proveedor_id y servicio_id por separado.
-- ============================================================

-- Necesaria para el índice EXCLUDE de más abajo: btree_gist agrega
-- soporte GiST a tipos con igualdad simple (uuid), que por sí solos solo
-- tienen índice btree.
CREATE EXTENSION IF NOT EXISTS btree_gist;


-- ------------------------------------------------------------
-- Interruptor del módulo + huso horario del negocio
-- ------------------------------------------------------------
ALTER TABLE tenant_servicios
    ADD COLUMN IF NOT EXISTS calendario_activo BOOLEAN NOT NULL DEFAULT false;

-- Nombre de zona IANA (p. ej. 'America/Mexico_City'). Vive acá y no en
-- `tenants` porque esa tabla la reconstruye 00_base_local.sql como espejo
-- de lo que ya existe en la base real de n8n (ver el comentario de ese
-- archivo) — agregarle una columna acá y no coordinarlo allá dejaría este
-- repo desincronizado del esquema de producción. tenant_servicios, en
-- cambio, la creó y la posee por completo el propio backend de Python
-- desde 06_vendedores.sql, así que es donde puede crecer con seguridad.
--
-- Hace falta un huso por tenant (y no asumir uno fijo) porque los horarios
-- de proveedor_horarios se cargan en hora local ("de 9 a 18") y hay que
-- convertirlos a TIMESTAMPTZ real para comparar contra reservas — sin
-- saber el huso, "9am" no significa nada. Default México porque el resto
-- del sistema de pagos (banxico.py, Mercado Pago) ya asume ese mercado.
ALTER TABLE tenant_servicios
    ADD COLUMN IF NOT EXISTS zona_horaria TEXT NOT NULL DEFAULT 'America/Mexico_City';


-- ------------------------------------------------------------
-- Proveedores (barberos / estilistas)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS proveedores (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    nombre      TEXT NOT NULL,
    -- Hex de 6 dígitos para pintar su columna en la grilla del portal.
    -- Validado también en Pydantic (ProveedorCrearIn), pero el CHECK
    -- cubre inserciones directas por SQL o un futuro script de carga.
    color       TEXT NOT NULL DEFAULT '#6366f1',
    -- Orden manual en la grilla (de izquierda a derecha). No hay
    -- estrategia automática como en la asignación de vendedores: acá el
    -- dueño decide el acomodo, típicamente por antigüedad o silla física.
    orden       SMALLINT NOT NULL DEFAULT 0,
    activo      BOOLEAN NOT NULL DEFAULT true,
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT proveedores_color_hex CHECK (color ~ '^#[0-9a-fA-F]{6}$')
);

CREATE INDEX IF NOT EXISTS idx_proveedores_tenant_activo
    ON proveedores (tenant_id, activo, orden);


-- ------------------------------------------------------------
-- Servicios (lo que se reserva)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS servicios (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    nombre              TEXT NOT NULL,
    duracion_minutos    SMALLINT NOT NULL,
    -- Nullable: hay negocios que no publican precio y lo dan solo de
    -- palabra o al cobrar. No bloquea la reserva.
    precio              NUMERIC(10,2),
    activo              BOOLEAN NOT NULL DEFAULT true,
    creado_en           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT servicios_duracion_valida CHECK (duracion_minutos BETWEEN 5 AND 480),
    CONSTRAINT servicios_precio_valido CHECK (precio IS NULL OR precio >= 0)
);

CREATE INDEX IF NOT EXISTS idx_servicios_tenant_activo
    ON servicios (tenant_id, activo);


-- ------------------------------------------------------------
-- Horario semanal recurrente por proveedor
-- ------------------------------------------------------------
-- Varias filas por (proveedor, día) son válidas a propósito: un barbero
-- puede trabajar 9-14 y 16-20 el mismo día, con un corte de mediodía que
-- es su horario normal, no una "excepción".
CREATE TABLE IF NOT EXISTS proveedor_horarios (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    proveedor_id    UUID NOT NULL REFERENCES proveedores(id) ON DELETE CASCADE,
    -- 0 = lunes … 6 = domingo, igual que Python date.weekday(): así
    -- services/calendario_slots.py no tiene que traducir convenciones
    -- entre Postgres (domingo=0 en EXTRACT(DOW)) y Python.
    dia_semana      SMALLINT NOT NULL,
    hora_inicio     TIME NOT NULL,
    hora_fin        TIME NOT NULL,
    CONSTRAINT proveedor_horarios_dia_valido CHECK (dia_semana BETWEEN 0 AND 6),
    CONSTRAINT proveedor_horarios_rango_valido CHECK (hora_fin > hora_inicio)
);

-- La consulta de disponibilidad siempre entra por proveedor + día.
CREATE INDEX IF NOT EXISTS idx_proveedor_horarios_proveedor_dia
    ON proveedor_horarios (proveedor_id, dia_semana);


-- ------------------------------------------------------------
-- Excepciones puntuales (día libre, vacaciones, horario especial)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS proveedor_excepciones (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    proveedor_id    UUID NOT NULL REFERENCES proveedores(id) ON DELETE CASCADE,
    fecha           DATE NOT NULL,
    -- false = no atiende ese día (default: vacaciones/feriado). true =
    -- atiende con un horario propio para esa fecha, que REEMPLAZA por
    -- completo al de proveedor_horarios ese día (no se combinan).
    disponible      BOOLEAN NOT NULL DEFAULT false,
    hora_inicio     TIME,
    hora_fin        TIME,
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT proveedor_excepciones_horario_coherente CHECK (
        (disponible = false AND hora_inicio IS NULL AND hora_fin IS NULL)
        OR (disponible = true AND hora_inicio IS NOT NULL AND hora_fin IS NOT NULL
            AND hora_fin > hora_inicio)
    ),
    -- Una sola excepción por proveedor y fecha: si gerencia carga dos, la
    -- segunda pisa a la primera por UPSERT, no conviven.
    UNIQUE (proveedor_id, fecha)
);

CREATE INDEX IF NOT EXISTS idx_proveedor_excepciones_proveedor_fecha
    ON proveedor_excepciones (proveedor_id, fecha);


-- ------------------------------------------------------------
-- Reservas (citas)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reservas (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- RESTRICT y no CASCADE: un proveedor con historial de citas no se
    -- puede borrar (solo desactivar, ver `activo`). No hay endpoint de
    -- DELETE para el catálogo, igual que vendedores.
    proveedor_id        UUID NOT NULL REFERENCES proveedores(id) ON DELETE RESTRICT,
    servicio_id         UUID NOT NULL REFERENCES servicios(id) ON DELETE RESTRICT,
    -- Cliente de WhatsApp/IG/FB. Nullable: el portal también carga un
    -- walk-in que nunca escribió por ningún canal.
    user_id             UUID REFERENCES users(id) ON DELETE SET NULL,
    -- Respaldo para el walk-in sin fila en `users`. Se exige uno de los
    -- dos (user_id o cliente_nombre) desde Pydantic — ver ReservaCrearIn.
    cliente_nombre      TEXT,
    cliente_telefono    TEXT,
    -- Calculados al crear la reserva (hora_inicio + duración del servicio
    -- EN ESE MOMENTO), no se recalculan al leer: ver 07_crm_campo.sql
    -- (distancia_calculada_metros) para el mismo criterio.
    hora_inicio         TIMESTAMPTZ NOT NULL,
    hora_fin            TIMESTAMPTZ NOT NULL,
    estado              TEXT NOT NULL DEFAULT 'confirmada'
        CHECK (estado IN ('confirmada', 'cancelada', 'completada', 'no_asistio')),
    notas               TEXT,
    motivo_cancelacion  TEXT,
    -- Reintentos de n8n: mismo patrón que tenant_token_usage.idempotency_key
    -- (services/gerencia.py::registrar_uso_tokens). Único por tenant, no
    -- global, para que dos negocios no choquen con la misma clave de
    -- ejecución de n8n.
    idempotency_key     TEXT,
    creado_en           TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado_en      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT reservas_rango_valido CHECK (hora_fin > hora_inicio),
    CONSTRAINT reservas_cliente_identificado CHECK (
        user_id IS NOT NULL OR cliente_nombre IS NOT NULL
    ),
    -- Se consulta con operadores de rango (&&) pero se filtra/muestra con
    -- hora_inicio/hora_fin normales, que es lo que espera el resto del
    -- código (Pydantic, el front, los listados).
    rango_horario tstzrange GENERATED ALWAYS AS (
        tstzrange(hora_inicio, hora_fin, '[)')
    ) STORED
);

-- ------------------------------------------------------------
-- Anti doble-reserva a nivel de Postgres, no solo de aplicación
-- ------------------------------------------------------------
-- Aunque el validador de Pydantic (o el cálculo de slots libres) tengan un
-- bug, dos reservas del MISMO proveedor con rangos que se traslapan no
-- pueden coexistir. Mismo espíritu que los CHECK de coordenadas de
-- 07_crm_campo.sql: un candado que no depende de que la capa de arriba se
-- acuerde de validar.
--
-- WHERE excluye 'cancelada' y 'no_asistio': una cita cancelada o a la que
-- el cliente no llegó libera el horario. Solo 'confirmada' y 'completada'
-- bloquean el traslape.
ALTER TABLE reservas DROP CONSTRAINT IF EXISTS reservas_sin_traslape;
ALTER TABLE reservas ADD CONSTRAINT reservas_sin_traslape
    EXCLUDE USING gist (
        proveedor_id WITH =,
        rango_horario WITH &&
    ) WHERE (estado NOT IN ('cancelada', 'no_asistio'));

-- La grilla del portal pide "las reservas de este proveedor en este rango".
CREATE INDEX IF NOT EXISTS idx_reservas_tenant_proveedor_horario
    ON reservas (tenant_id, proveedor_id, hora_inicio);

-- "Toda la agenda del negocio en este rango" (sin filtrar proveedor) es la
-- otra consulta de la grilla.
CREATE INDEX IF NOT EXISTS idx_reservas_tenant_horario
    ON reservas (tenant_id, hora_inicio);

-- "Las próximas citas de este cliente", para n8n (confirmar/recordar/
-- cancelar) y para su historial en el portal.
CREATE INDEX IF NOT EXISTS idx_reservas_user
    ON reservas (user_id, hora_inicio)
    WHERE user_id IS NOT NULL;

-- Reintento idempotente de n8n.
CREATE UNIQUE INDEX IF NOT EXISTS idx_reservas_idempotency
    ON reservas (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;


-- ------------------------------------------------------------
-- Nuevos tipos de alerta para el centro de notificaciones
-- ------------------------------------------------------------
-- aplicar_sql.py reaplica los archivos: DROP IF EXISTS + ADD es seguro
-- reejecutarlo, igual que el resto de este archivo.
ALTER TABLE alertas DROP CONSTRAINT IF EXISTS alertas_tipo_check;
ALTER TABLE alertas ADD CONSTRAINT alertas_tipo_check
    CHECK (tipo IN (
        'nuevo_lead', 'cambio_etapa', 'sin_actividad', 'cuota_excedida', 'cierre',
        'reserva_creada', 'reserva_cancelada'
    ));
