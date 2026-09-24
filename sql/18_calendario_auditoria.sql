-- ============================================================
-- BITÁCORA DE RESERVAS (reserva_auditoria)
-- ============================================================
-- Historia de usuario del dueño de la estética: necesita un registro
-- auditable e inmutable de qué le pasó a cada cita (alta, reprogramación,
-- cambio de barbero, cancelación, finalización) para resolver disputas y
-- llevar control operativo.
--
-- Desacoplada del estado actual de la reserva a propósito (consideración
-- técnica de la historia): cada fila es un evento con su snapshot de antes
-- y después, no una columna más de `reservas` que se sobrescribe. Mismo
-- criterio que gerencia_auditoria en 14_gerencia_auditoria.sql, pero para
-- el nivel de negocio (un tenant) en vez del nivel de plataforma.
--
-- Sin endpoint de borrado ni de edición (Escenario 5: solo lectura para
-- gerencia) — la única forma de que una bitácora sirva es que nadie pueda
-- tocarla después del hecho.
--
-- Los proveedores nunca son "actor": no tienen cuenta propia (decisión de
-- producto ya tomada para este módulo), así que todo evento lo dispara
-- gerencia desde el portal o un cliente que escribió por chat y por el que
-- actuó n8n. `origen` distingue esos dos caminos; `actor` es un texto
-- legible (el email de gerencia, o "Cliente: <nombre>") y no solo un FK,
-- para que el rastro sobreviva a que se borre ese usuario o cliente —
-- mismo criterio que gerencia_auditoria.actor_email.

CREATE TABLE IF NOT EXISTS reserva_auditoria (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- RESTRICT y no CASCADE: reservas nunca se borra de verdad (solo se
    -- cancela, ver 16_calendarios.sql), así que un intento de borrarla a
    -- mano con bitácora encima debe fallar en vez de arrastrar el rastro.
    reserva_id UUID NOT NULL REFERENCES reservas(id) ON DELETE RESTRICT,

    evento TEXT NOT NULL CHECK (evento IN (
        'creada', 'reprogramada', 'cambio_barbero', 'cancelada', 'completada', 'no_asistio'
    )),
    estado_anterior TEXT CHECK (estado_anterior IS NULL OR estado_anterior IN (
        'confirmada', 'cancelada', 'completada', 'no_asistio'
    )),
    estado_nuevo TEXT NOT NULL CHECK (estado_nuevo IN (
        'confirmada', 'cancelada', 'completada', 'no_asistio'
    )),
    motivo TEXT,

    -- Snapshot de los campos que ESE evento tocó (no la reserva completa):
    -- para 'creada' no hay "antes"; para 'reprogramada'/'cambio_barbero' el
    -- estado no cambia, lo que cambia vive acá (horario o proveedor).
    datos_anteriores JSONB,
    datos_nuevos     JSONB NOT NULL DEFAULT '{}'::jsonb,

    origen               TEXT NOT NULL CHECK (origen IN ('portal', 'n8n')),
    actor                TEXT NOT NULL,
    actor_portal_user_id UUID,
    actor_user_id        UUID,

    creado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- La bitácora general del tenant, más reciente primero (Escenario 2).
CREATE INDEX IF NOT EXISTS idx_reserva_auditoria_tenant_fecha
    ON reserva_auditoria (tenant_id, creado_en DESC);

-- El historial de una cita puntual (Escenario 4).
CREATE INDEX IF NOT EXISTS idx_reserva_auditoria_reserva
    ON reserva_auditoria (reserva_id, creado_en);
