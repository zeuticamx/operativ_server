-- ============================================================
-- 47 · Cortes de contexto del agente por conversación
--
-- "Limpiar el contexto" no borra mensajes: el dueño registra un corte y el
-- flujo de n8n ("Carga historial") lee solo los mensajes posteriores al
-- último corte de la conversación. El historial visible en el portal no
-- cambia. Cada pulsación del botón añade una fila (no se actualiza ninguna).
--
-- Se borra con el negocio o la conversación (ON DELETE CASCADE). Si el
-- usuario que hizo el corte se elimina, la fila se conserva con
-- realizado_por NULL. No se guarda correo: solo el id.
--
--     python aplicar_sql.py sql/47_conversacion_contexto_cortes.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS conversacion_contexto_cortes (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    conversation_id  UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    creado_en        TIMESTAMPTZ NOT NULL DEFAULT now(),
    realizado_por    UUID REFERENCES portal_users(id) ON DELETE SET NULL
);

-- La consulta siempre es "el corte más reciente de esta conversación".
CREATE INDEX IF NOT EXISTS idx_conversacion_contexto_cortes_conv
    ON conversacion_contexto_cortes (conversation_id, creado_en DESC);
