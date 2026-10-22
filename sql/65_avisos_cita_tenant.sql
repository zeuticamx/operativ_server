-- ============================================================
-- 65 · Plantillas de avisos de cita por tenant (spec 007)
--
-- Cada negocio decide si usa sus propias plantillas de WhatsApp para el
-- recordatorio y la cancelación de citas (se envían desde su número principal
-- activo; se retira la cuenta global de la spec 004 para estos avisos).
--
--   - avisos_cita_config: una fila por negocio. Fila ausente equivale a oferta
--     'pendiente', ambas funciones apagadas y nombres de plantilla base.
--     El nombre vigente de cada plantilla vive aquí (puede llevar sufijo _v2,
--     _v3... si hubo que recrearla); se une a `plantillas_whatsapp_tenant` por
--     nombre, sin FK.
--   - avisos_cita.motivo: se añade 'sin_numero_emisor'.
--   - alertas_tipo_check: misma lista que 60_alertas_numeros.sql + dos tipos:
--       avisos_cita_oferta_rechazada       el owner rechazó la oferta
--       avisos_cita_plantilla_inutilizable Meta rechazó/pausó/deshabilitó (o se
--                                          quitó) una plantilla con función activa
--     ('aviso_cita_no_enviado' ya existía.)
--
-- Idempotente (IF NOT EXISTS / DROP CONSTRAINT IF EXISTS).
--
--     python aplicar_sql.py sql/65_avisos_cita_tenant.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS avisos_cita_config (
    tenant_id                      UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    oferta                         VARCHAR(10) NOT NULL DEFAULT 'pendiente'
                                   CHECK (oferta IN ('pendiente', 'aceptada', 'rechazada')),
    recordatorio_activo            BOOLEAN NOT NULL DEFAULT false,
    cancelacion_activo             BOOLEAN NOT NULL DEFAULT false,
    nombre_plantilla_recordatorio  VARCHAR(512) NOT NULL DEFAULT 'recordatorio_cita',
    nombre_plantilla_cancelacion   VARCHAR(512) NOT NULL DEFAULT 'notificacion_cancelacion',
    decidido_en                    TIMESTAMPTZ,
    decidido_por                   UUID REFERENCES portal_users(id) ON DELETE SET NULL,
    actualizada_en                 TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- Motivo 'sin_numero_emisor' (el negocio no tiene número principal activo
-- coincidente con la credencial del canal). Conserva los valores de sql/48.
ALTER TABLE avisos_cita DROP CONSTRAINT IF EXISTS avisos_cita_motivo_check;
ALTER TABLE avisos_cita ADD CONSTRAINT avisos_cita_motivo_check
    CHECK (motivo IS NULL OR motivo IN (
        'sin_telefono', 'telefono_invalido', 'canal_no_soportado',
        'desactivado_por_configuracion', 'plantilla_no_aprobada',
        'plantilla_no_disponible', 'cita_no_confirmada', 'hora_cambiada',
        'cita_ya_iniciada', 'error_proveedor', 'resultado_desconocido',
        'sin_numero_emisor'
    ));


-- alertas.tipo era VARCHAR(30) (sql/09): 'avisos_cita_plantilla_inutilizable' (34) y
-- 'numero_configuracion_incompleta' (31) no cabían y el INSERT fallaba. Se ensancha
-- (idempotente: repetirlo no cambia nada).
ALTER TABLE alertas ALTER COLUMN tipo TYPE VARCHAR(60);

ALTER TABLE alertas DROP CONSTRAINT IF EXISTS alertas_tipo_check;
ALTER TABLE alertas ADD CONSTRAINT alertas_tipo_check
    CHECK (tipo IN (
        'nuevo_lead', 'cambio_etapa', 'sin_actividad', 'cuota_excedida', 'cierre',
        'reserva_creada', 'reserva_cancelada', 'conversacion_transferida',
        'perfil_incompleto', 'agenda_reprogramada', 'solicitud_escritura',
        'conversacion_asignada', 'asignacion_fallida',
        'mensaje_conversacion_asignada',
        'conversacion_tomada', 'conversacion_liberada', 'conversacion_devuelta_ia',
        'lead_asignado',
        'aviso_cita_no_enviado',
        'numeros_pausados', 'numeros_recortados', 'numero_configuracion_incompleta',
        'avisos_cita_oferta_rechazada', 'avisos_cita_plantilla_inutilizable'
    ));
