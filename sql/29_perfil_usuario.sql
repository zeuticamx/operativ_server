-- ============================================================
-- PERFIL DEL USUARIO + RECORDATORIOS DE PERFIL INCOMPLETO
-- ============================================================
-- Datos personales de quien entra al portal, editables desde
-- /preferencias (routers/perfil.py). Ninguno se pide al darse de alta: el
-- registro sigue igual y el perfil se completa después.
--
-- Obligatorios para considerar el perfil "completo": nombres,
-- apellido_paterno, fecha_nacimiento y genero. Opcionales: apellido_materno
-- (hay quien no tiene segundo apellido), empresa y la foto.
--
-- `full_name` NO se toca: lo siguen usando el alta y otras pantallas.
--
-- Idempotente. Aplicar con aplicar_sql.py.
-- ============================================================

ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS nombres           VARCHAR(100);
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS apellido_paterno  VARCHAR(100);
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS apellido_materno  VARCHAR(100);
-- DATE y no TIMESTAMP: es un día del calendario, no un instante.
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS fecha_nacimiento  DATE;
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS genero            VARCHAR(20);
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS empresa           VARCHAR(255);

-- Cuándo quedó completo por primera vez (NULL = incompleto). Lo escribe
-- el servidor al guardar; si después se borra un obligatorio vuelve a NULL.
-- Es lo que mira el job de recordatorios para dejar de avisar.
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS perfil_completado_en TIMESTAMPTZ;

-- Control de los recordatorios (jobs/perfil_background.py): cuántos se
-- mandaron y cuándo el último. El job reserva el envío con un UPDATE sobre
-- estas dos columnas, así dos pasadas simultáneas no duplican el correo.
ALTER TABLE portal_users
    ADD COLUMN IF NOT EXISTS recordatorios_perfil_enviados SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS ultimo_recordatorio_perfil_en TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'portal_users_genero_check'
           AND conrelid = 'portal_users'::regclass
    ) THEN
        ALTER TABLE portal_users
            ADD CONSTRAINT portal_users_genero_check
            CHECK (genero IS NULL OR genero IN ('femenino', 'masculino', 'prefiero_no_decirlo'));
    END IF;
END $$;


-- ------------------------------------------------------------
-- Foto de perfil
-- ------------------------------------------------------------
-- En la base y no en disco: el contenedor del backend no tiene volumen
-- persistente (un archivo en disco se perdería en cada despliegue) y un
-- bucket externo sería una dependencia más. Con el tope de 642x642 px y
-- 2 MB (validado en services/imagen.py) cabe de sobra.
--
-- Tabla aparte y no una columna de portal_users: deps.usuario_actual lee
-- portal_users en CADA petición y no tiene por qué arrastrar los bytes.
CREATE TABLE IF NOT EXISTS portal_user_fotos (
    portal_user_id UUID PRIMARY KEY REFERENCES portal_users(id) ON DELETE CASCADE,
    contenido      BYTEA NOT NULL,
    mime           VARCHAR(20) NOT NULL CHECK (mime IN ('image/jpeg', 'image/png')),
    ancho          SMALLINT NOT NULL CHECK (ancho BETWEEN 1 AND 642),
    alto           SMALLINT NOT NULL CHECK (alto BETWEEN 1 AND 642),
    bytes          INTEGER NOT NULL,
    actualizada_en TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ------------------------------------------------------------
-- Alertas personales
-- ------------------------------------------------------------
-- Hasta acá toda alerta era de un negocio y la veían todos sus usuarios.
-- El recordatorio de perfil es de UNA persona: portal_user_id NULL = del
-- negocio (como siempre), con valor = solo la ve ese usuario.
ALTER TABLE alertas
    ADD COLUMN IF NOT EXISTS portal_user_id UUID REFERENCES portal_users(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_alertas_usuario_no_leidas
    ON alertas (portal_user_id)
    WHERE portal_user_id IS NOT NULL AND leido = false;

ALTER TABLE alertas DROP CONSTRAINT IF EXISTS alertas_tipo_check;
ALTER TABLE alertas ADD CONSTRAINT alertas_tipo_check
    CHECK (tipo IN (
        'nuevo_lead', 'cambio_etapa', 'sin_actividad', 'cuota_excedida', 'cierre',
        'reserva_creada', 'reserva_cancelada', 'conversacion_transferida',
        'perfil_incompleto'
    ));
