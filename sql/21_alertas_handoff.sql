-- ============================================================
-- Nuevo tipo de alerta: cliente transferido a un humano
-- ============================================================
-- Lo dispara n8n (workflow escalar-humano) vía
-- POST /api/eventos/conversacion-transferida, justo después de marcar la
-- conversación como 'transferred'. Sin este tipo, esa llamada fallaría en
-- el CHECK de `alertas` y gerencia solo se enteraría entrando al portal a
-- filtrar conversaciones por estado a mano.
ALTER TABLE alertas DROP CONSTRAINT IF EXISTS alertas_tipo_check;
ALTER TABLE alertas ADD CONSTRAINT alertas_tipo_check
    CHECK (tipo IN (
        'nuevo_lead', 'cambio_etapa', 'sin_actividad', 'cuota_excedida', 'cierre',
        'reserva_creada', 'reserva_cancelada', 'conversacion_transferida'
    ));
