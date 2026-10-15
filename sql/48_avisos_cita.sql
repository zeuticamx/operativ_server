-- ============================================================
-- 48 · Avisos de citas por WhatsApp: registros, ejecuciones y respuestas
--
-- Spec 004. Cada intento de plantilla de WhatsApp (cancelación del equipo o
-- recordatorio a 5 h) deja una fila en `avisos_cita`, también los omitidos.
--
--   - avisos_cita_ejecuciones: una fila por pasada del job o por cancelación.
--     Su `latido_en` dice si la ejecución sigue viva (RF-10, RF-16).
--   - avisos_cita: el registro de envío. UNIQUE (reserva_id, tipo, hora_inicio)
--     es la garantía de "como máximo un aviso por cita y hora" (D6): el
--     proceso inserta `en_curso` ANTES de llamar al proveedor y, si choca con
--     la clave, no envía (CL-5). No depende de max_instances=1.
--   - cuenta_global_respuestas: una respuesta fija cada 24 h por cliente (D13).
--
--     python aplicar_sql.py sql/48_avisos_cita.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS avisos_cita_ejecuciones (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tipo          VARCHAR(20) NOT NULL CHECK (tipo IN ('cancelacion', 'recordatorio')),
    iniciada_en   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Se renueva al menos cada minuto mientras trabaja; 5 min sin renovar = muerta.
    latido_en     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    terminada_en  TIMESTAMPTZ
);


CREATE TABLE IF NOT EXISTS avisos_cita (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- CASCADE: lo necesita services/eliminacion_tenant.py al borrar un negocio.
    reserva_id        UUID NOT NULL REFERENCES reservas(id) ON DELETE CASCADE,
    tipo              VARCHAR(20) NOT NULL CHECK (tipo IN ('cancelacion', 'recordatorio')),
    -- Hora de inicio de la reserva en el momento del intento: parte de la clave.
    hora_inicio       TIMESTAMPTZ NOT NULL,
    estado            VARCHAR(20) NOT NULL
                      CHECK (estado IN ('en_curso', 'enviado', 'omitido', 'fallido')),
    motivo            VARCHAR(40)
                      CHECK (motivo IS NULL OR motivo IN (
                          'sin_telefono', 'telefono_invalido', 'canal_no_soportado',
                          'desactivado_por_configuracion', 'plantilla_no_aprobada',
                          'plantilla_no_disponible', 'cita_no_confirmada', 'hora_cambiada',
                          'cita_ya_iniciada', 'error_proveedor', 'resultado_desconocido'
                      )),
    -- Verdadero cuando el cliente no tiene conversación (RF-15): se envía igual.
    sin_conversacion  BOOLEAN NOT NULL DEFAULT false,
    -- E.164 al que se envió; se guarda al pasar a `enviado` y permite reconocer
    -- después a quien responde en la cuenta global (D13).
    telefono_destino  TEXT,
    ejecucion_id      UUID REFERENCES avisos_cita_ejecuciones(id) ON DELETE SET NULL,
    creado_en         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cerrado_en        TIMESTAMPTZ,
    -- en_curso y enviado sin motivo; omitido y fallido siempre con motivo.
    CONSTRAINT avisos_cita_estado_motivo_check CHECK (
        (estado IN ('en_curso', 'enviado') AND motivo IS NULL)
        OR (estado IN ('omitido', 'fallido') AND motivo IS NOT NULL)
    ),
    CONSTRAINT avisos_cita_clave_unica UNIQUE (reserva_id, tipo, hora_inicio)
);

CREATE INDEX IF NOT EXISTS idx_avisos_cita_reserva
    ON avisos_cita (reserva_id);

-- Búsqueda de huérfanos: `en_curso` por ejecución.
CREATE INDEX IF NOT EXISTS idx_avisos_cita_en_curso
    ON avisos_cita (ejecucion_id) WHERE estado = 'en_curso';

-- "¿Este remitente recibió un aviso en las últimas 24 h?" (D13).
CREATE INDEX IF NOT EXISTS idx_avisos_cita_destino
    ON avisos_cita (telefono_destino, cerrado_en) WHERE estado = 'enviado';


CREATE TABLE IF NOT EXISTS cuenta_global_respuestas (
    telefono             TEXT PRIMARY KEY,
    ultima_respuesta_en  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
