-- ============================================================
-- 70 · La línea de avisos de la plataforma se puede cambiar de número
-- ============================================================
-- Requiere 56_numeros_whatsapp.sql.
--
-- El negocio que aloja la línea de avisos (AVISOS_CUENTA_GLOBAL_TENANT_ID) elige
-- desde Canales cuál de sus números ya vinculados y activos hace de línea de
-- avisos (PUT /api/canales/whatsapp/avisos). El cambio se anota en la bitácora
-- del negocio, y la bitácora tiene una lista cerrada de motivos (CHECK de sql/56):
-- aquí se agrega `cambio_de_linea_avisos`.
--
-- No hay columna nueva: la elección vive en lo mismo que ya la decidía,
-- `channel_credentials.phone_number_id` del negocio de avisos, que
-- `sincronizar_linea_avisos` compara al arrancar y tras cada alta.
--
-- Idempotente:
--
--     python aplicar_sql.py sql/70_cambio_de_linea_avisos.sql
-- ============================================================

ALTER TABLE numeros_bitacora DROP CONSTRAINT IF EXISTS numeros_bitacora_motivo_check;

ALTER TABLE numeros_bitacora
    ADD CONSTRAINT numeros_bitacora_motivo_check
    CHECK (motivo IN (
        'vinculacion', 'desvinculacion', 'pausa_manual', 'reactivacion_manual',
        'pausa_por_cambio_de_plan', 'pausa_por_fin_de_suscripcion',
        'pausa_por_fin_de_prueba', 'pausa_por_pago_de_prueba',
        'pausa_por_despliegue', 'pausa_por_cupo_bajado_por_plataforma',
        'pausa_por_plan_mas_bajo_recalculado', 'cambio_de_principal',
        'principal_sin_numero_activo', 'configuracion_incompleta',
        'configuracion_completada', 'recorte_por_plan',
        'herramienta_retirada_por_plan', 'herramienta_activada',
        'herramienta_desactivada', 'prompt_cambiado',
        'agente_encendido', 'agente_apagado',
        'cambio_de_linea_avisos'
    ));
