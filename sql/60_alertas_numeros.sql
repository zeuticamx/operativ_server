-- ============================================================
-- 60 · Tipos de alerta de varios números de WhatsApp (spec 005a, RF-20)
--
-- Avisos agrupados a owner y superadmin (portal y correo espejo):
--   numeros_pausados                 pausa por cualquiera de los casos de RF-11
--   numeros_recortados               recorte de un límite de plan (RF-10)
--   numero_configuracion_incompleta  número (o Instagram/Facebook) incompleto (RF-8, RF-17, RF-22)
-- El detalle (números, motivo, acción) va en `datos`. Misma lista que
-- 51_alertas_aviso_cita.sql + los tres tipos nuevos. Idempotente (DROP IF EXISTS).
--
--     python aplicar_sql.py sql/60_alertas_numeros.sql
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
        'aviso_cita_no_enviado',
        'numeros_pausados', 'numeros_recortados', 'numero_configuracion_incompleta'
    ));
