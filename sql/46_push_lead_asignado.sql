-- ============================================================
-- 46 · Notificaciones push a la app móvil + alerta "lead asignado"
--
-- Dos cosas que llegan juntas porque las pidió el mismo cambio:
--
-- 1. dispositivos_push: los tokens de Expo Push que registra la app móvil
--    (crm-mobile-app) al iniciar sesión. Una alerta que ya va por WebSocket
--    (realtime.broadcast_alerta) también sale como push a los dispositivos
--    de quien la recibiría en vivo, para que llegue con la app cerrada.
--
-- 2. 'lead_asignado': alerta PERSONAL al vendedor cuando un lead pasa a ser
--    suyo (reparto automático, asignación manual, alta manual o reparto de
--    pendientes). Antes el vendedor no se enteraba hasta abrir su cartera.
--
--     python aplicar_sql.py sql/46_push_lead_asignado.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS dispositivos_push (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Único: un token identifica una instalación de la app. Si otra persona
    -- inicia sesión en el mismo teléfono, el token cambia de dueño (UPSERT
    -- en routers/dispositivos.py) en vez de quedar duplicado y mandarle al
    -- nuevo usuario los avisos del anterior.
    token           TEXT NOT NULL UNIQUE,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    portal_user_id  UUID NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    plataforma      VARCHAR(10) NOT NULL CHECK (plataforma IN ('ios', 'android')),
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actualizado_en  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- El envío siempre busca "los tokens de estos usuarios".
CREATE INDEX IF NOT EXISTS idx_dispositivos_push_usuario
    ON dispositivos_push (portal_user_id);


-- ------------------------------------------------------------
-- Tipo de alerta nuevo. Misma lista que 44_alertas_cambio_responsable.sql
-- + 'lead_asignado'.
-- ------------------------------------------------------------
ALTER TABLE alertas DROP CONSTRAINT IF EXISTS alertas_tipo_check;
ALTER TABLE alertas ADD CONSTRAINT alertas_tipo_check
    CHECK (tipo IN (
        'nuevo_lead', 'cambio_etapa', 'sin_actividad', 'cuota_excedida', 'cierre',
        'reserva_creada', 'reserva_cancelada', 'conversacion_transferida',
        'perfil_incompleto', 'agenda_reprogramada', 'solicitud_escritura',
        'conversacion_asignada', 'asignacion_fallida',
        'mensaje_conversacion_asignada',
        'conversacion_tomada', 'conversacion_liberada', 'conversacion_devuelta_ia',
        'lead_asignado'
    ));
