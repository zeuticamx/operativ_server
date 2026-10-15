-- ============================================================
-- 49 · Estado global de las plantillas de WhatsApp de la cuenta global
--
-- Spec 004 (RF-18, RF-19). Hay UNA plantilla de cada tipo para todos los
-- negocios: es de plataforma, sin tenant_id. Solo `gerencia_users` la lee y la
-- cambia (a mano por API o por el evento de estado que reenvía NeuroAPI;
-- en ese caso `actualizada_por` queda NULL).
--
-- Las plantillas se crean y aprueban a mano en Meta Business Manager.
-- Sin tabla ni funciones de credenciales: el envío reutiliza el canal de
-- WhatsApp del tenant de la cuenta global (channel_credentials).
--
--     python aplicar_sql.py sql/49_plantillas_whatsapp_globales.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS plantillas_whatsapp_globales (
    nombre          VARCHAR(40) PRIMARY KEY
                    CHECK (nombre IN ('notificacion_cancelacion', 'recordatorio_cita')),
    estado          VARCHAR(30) NOT NULL DEFAULT 'no_solicitada'
                    CHECK (estado IN (
                        'no_solicitada', 'pendiente_aprobacion', 'aprobada',
                        'rechazada', 'pausada'
                    )),
    actualizada_en  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actualizada_por UUID REFERENCES gerencia_users(id) ON DELETE SET NULL,
    nota            TEXT
);

INSERT INTO plantillas_whatsapp_globales (nombre, estado) VALUES
    ('notificacion_cancelacion', 'no_solicitada'),
    ('recordatorio_cita', 'no_solicitada')
ON CONFLICT (nombre) DO NOTHING;
