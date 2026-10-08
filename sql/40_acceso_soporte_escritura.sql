-- ============================================================
-- "VER COMO" CON ESCRITURA, AUTORIZADA POR EL DUEÑO
-- ============================================================
-- Hasta acá una sesión de "ver como" (impersonación de plataforma) era
-- siempre de solo lectura. Para que soporte pueda corregir algo por el
-- cliente, el dueño del negocio tiene que autorizarlo de forma expresa:
--
--   1. soporte (nivel gerencia con cargo permitido) pide permiso con motivo
--   2. el dueño recibe una alerta personal en su portal y un correo
--   3. el dueño aprueba (eligiendo cuánto dura), rechaza o, ya aprobada,
--      la revoca
--
-- La concesión vive acá y NO en el token: deps._validar_impersonacion la
-- consulta en cada petición, así que revocarla o vencerse surte efecto en
-- la siguiente llamada, sin esperar a que expire el token de "ver como".
--
-- Una solicitud pendiente vive 30 minutos. El enlace del correo lleva un
-- token de un solo uso: acá solo su SHA-256 (mismo criterio que
-- invitaciones_equipo), así que quien lea la tabla no se lleva enlaces
-- usables.
--
-- Idempotente. Aplicar con aplicar_sql.py.
-- ============================================================

CREATE TABLE IF NOT EXISTS acceso_soporte_solicitudes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- Quien pide: el portal_user del gerente (el claim `imp` del token).
    gerente_id      UUID NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    gerente_email   VARCHAR(255) NOT NULL,
    motivo          TEXT NOT NULL,

    estado          VARCHAR(20) NOT NULL DEFAULT 'pendiente'
                    CHECK (estado IN ('pendiente', 'aprobada', 'rechazada', 'revocada', 'cancelada')),

    token_hash      TEXT NOT NULL UNIQUE,

    creada_en       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Hasta cuándo se puede responder. Pasado esto la solicitud ya no se
    -- puede aprobar (se trata como vencida sin necesidad de un job).
    expira_en       TIMESTAMPTZ NOT NULL,

    resuelta_en     TIMESTAMPTZ,
    -- Quién respondió: el correo del dueño (o de quien abrió el enlace).
    resuelta_por    VARCHAR(255),
    -- 'portal' o 'correo': por qué canal se respondió.
    canal           VARCHAR(10) CHECK (canal IN ('portal', 'correo')),

    -- Solo aprobada: cuánto dura la escritura y hasta cuándo.
    duracion_min    INTEGER CHECK (duracion_min IN (15, 30, 60)),
    concede_hasta   TIMESTAMPTZ,

    CONSTRAINT aprobada_con_vigencia
        CHECK ((estado = 'aprobada') = (concede_hasta IS NOT NULL))
);

-- Un gerente tiene una sola solicitud viva (pendiente o aprobada) por
-- negocio: pedir de nuevo exige cerrar o dejar vencer la anterior.
CREATE UNIQUE INDEX IF NOT EXISTS idx_acceso_soporte_viva
    ON acceso_soporte_solicitudes (tenant_id, gerente_id)
    WHERE estado IN ('pendiente', 'aprobada');

CREATE INDEX IF NOT EXISTS idx_acceso_soporte_tenant
    ON acceso_soporte_solicitudes (tenant_id, creada_en DESC);

-- ------------------------------------------------------------
-- Tipo de alerta nuevo: aviso personal al dueño con la solicitud.
-- Misma lista que 39_agenda.sql + el nuevo.
-- ------------------------------------------------------------
ALTER TABLE alertas DROP CONSTRAINT IF EXISTS alertas_tipo_check;
ALTER TABLE alertas ADD CONSTRAINT alertas_tipo_check
    CHECK (tipo IN (
        'nuevo_lead', 'cambio_etapa', 'sin_actividad', 'cuota_excedida', 'cierre',
        'reserva_creada', 'reserva_cancelada', 'conversacion_transferida',
        'perfil_incompleto', 'agenda_reprogramada', 'solicitud_escritura'
    ));
