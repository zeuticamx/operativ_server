-- ============================================================
-- 50 · Eventos de contexto del agente por avisos de citas
--
-- Spec 004 (RF-13, RF-14, RF-15). Cuando un aviso queda `enviado`, el backend
-- deja aquí un evento ("cita cancelada por el negocio" o "recordatorio
-- enviado") para que n8n lo lea por conversation_id y el agente lo tenga en
-- cuenta. Tabla propia del backend: no se escribe en tablas de n8n. La FK a
-- `conversations` es solo referencia.
--
-- conversation_id NOT NULL: un envío sin conversación no tiene evento (RF-15).
-- aviso_id UNIQUE: un aviso genera como máximo un evento.
--
--     python aplicar_sql.py sql/50_contexto_agente_avisos.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS contexto_agente_eventos (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    conversation_id  UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    reserva_id       UUID REFERENCES reservas(id) ON DELETE SET NULL,
    aviso_id         UUID NOT NULL UNIQUE REFERENCES avisos_cita(id) ON DELETE CASCADE,
    -- cita_cancelada_por_negocio | recordatorio_cita_enviado
    tipo             VARCHAR(40) NOT NULL
                     CHECK (tipo IN ('cita_cancelada_por_negocio', 'recordatorio_cita_enviado')),
    hora_cita        TIMESTAMPTZ NOT NULL,
    servicio         TEXT,
    creado_en        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- n8n lee "los eventos de esta conversación, en orden".
CREATE INDEX IF NOT EXISTS idx_contexto_agente_eventos_conversacion
    ON contexto_agente_eventos (conversation_id, creado_en);
