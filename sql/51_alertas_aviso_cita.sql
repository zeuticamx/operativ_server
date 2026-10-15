-- ============================================================
-- 51 · Tipo de alerta 'aviso_cita_no_enviado'
--
-- Spec 004 (RF-7, RF-8): aviso a gerencia (owner y superadmin) cuando una
-- cancelación no pudo avisar al cliente. Un solo tipo; el motivo va en
-- `datos.motivo`. Misma lista que 46_push_lead_asignado.sql + el tipo nuevo.
--
--     python aplicar_sql.py sql/51_alertas_aviso_cita.sql
-- ============================================================

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
        'aviso_cita_no_enviado'
    ));
