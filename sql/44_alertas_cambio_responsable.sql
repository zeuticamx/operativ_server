-- ============================================================
-- ALERTAS: conversación tomada / liberada / devuelta a la IA
-- ============================================================
-- Cuando una persona toma, suelta, reasigna o devuelve a la IA una
-- conversación, el cambio deja una alerta guardada (antes solo se emitía el
-- evento en vivo `conversacion_actualizada`, que se pierde si nadie está
-- conectado).
--   conversacion_tomada       del negocio: alguien (o soporte) la tomó
--   conversacion_liberada     personal: al asignado anterior, ya no es suya
--   conversacion_devuelta_ia  del negocio (y del asignado anterior): volvió a la IA
--
-- Misma lista que 41_conversaciones_asignacion.sql + los nuevos.
ALTER TABLE alertas DROP CONSTRAINT IF EXISTS alertas_tipo_check;
ALTER TABLE alertas ADD CONSTRAINT alertas_tipo_check
    CHECK (tipo IN (
        'nuevo_lead', 'cambio_etapa', 'sin_actividad', 'cuota_excedida', 'cierre',
        'reserva_creada', 'reserva_cancelada', 'conversacion_transferida',
        'perfil_incompleto', 'agenda_reprogramada', 'solicitud_escritura',
        'conversacion_asignada', 'asignacion_fallida',
        'mensaje_conversacion_asignada',
        'conversacion_tomada', 'conversacion_liberada', 'conversacion_devuelta_ia'
    ));
