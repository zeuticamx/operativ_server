-- ============================================================
-- ASIGNACIÓN DE CONVERSACIONES A UN MIEMBRO DEL EQUIPO
-- ============================================================
-- Una conversación transferida a un humano (status='transferred') ahora puede
-- tener UN responsable: una cuenta del portal (portal_users), de cualquier rol
-- en deps.ROLES_ASIGNABLES (owner, superadmin, member, vendedor, proveedor y los
-- que nazcan después: apuntar a la cuenta y no a la ficha de vendedor/proveedor
-- es lo que hace que un rol nuevo no necesite migración).
--
-- Quién la asigna:
--   'owner'   el dueño/administrador desde el portal (PUT .../asignacion)
--   'usuario' alguien que la tomó él mismo (POST .../tomar)
--   'agente'  el agente de n8n al escalar (POST /eventos/conversacion-transferida
--             con `area`); el backend decide, el LLM solo sugiere el área
--   'sistema' el backend, al desactivar a quien la tenía: vuelve al owner
--
-- Solo el owner/superadmin se la quita a otro. El asignado puede soltarla.
-- Si la asignación automática no encuentra a quién, cae en el owner con una
-- nota (`asignacion_nota`) para que la reasigne.
--
-- Las columnas son nullables y sin DEFAULT: los INSERT de n8n sobre
-- `conversations` no las mencionan y siguen valiendo.
--
-- Idempotente. Aplicar con aplicar_sql.py.
-- ============================================================

ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS asignado_a       UUID REFERENCES portal_users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS asignado_en      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS asignado_origen  VARCHAR(10),
    ADD COLUMN IF NOT EXISTS asignacion_nota  TEXT;

ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_asignado_origen_check;
ALTER TABLE conversations ADD CONSTRAINT conversations_asignado_origen_check
    CHECK (asignado_origen IS NULL OR asignado_origen IN ('owner', 'usuario', 'agente', 'sistema'));

-- "Mis conversaciones" y la visibilidad acotada de vendedor/proveedor.
CREATE INDEX IF NOT EXISTS idx_conversations_tenant_asignado
    ON conversations (tenant_id, asignado_a)
    WHERE asignado_a IS NOT NULL;


-- ------------------------------------------------------------
-- Historial: quién se la dio a quién, cuándo y por qué
-- ------------------------------------------------------------
-- `a_portal_user_id` NULL = quedó sin asignar (la soltó el asignado, la quitó
-- el owner, o volvió a la IA). Se conserva aunque se borre la cuenta
-- (SET NULL): la bitácora no debe desaparecer con ella.
CREATE TABLE IF NOT EXISTS conversacion_asignaciones (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    conversation_id     UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    de_portal_user_id   UUID REFERENCES portal_users(id) ON DELETE SET NULL,
    a_portal_user_id    UUID REFERENCES portal_users(id) ON DELETE SET NULL,
    origen              VARCHAR(10) NOT NULL
        CHECK (origen IN ('owner', 'usuario', 'agente', 'sistema')),
    -- Quién hizo el cambio. NULL cuando fue el agente o el sistema.
    actor_portal_user_id UUID REFERENCES portal_users(id) ON DELETE SET NULL,
    nota                TEXT,
    creado_en           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conv_asignaciones_conversacion
    ON conversacion_asignaciones (conversation_id, creado_en DESC);


-- ------------------------------------------------------------
-- Tipos de alerta nuevos (personales, para el asignado o el owner)
-- ------------------------------------------------------------
-- Misma lista que 40_acceso_soporte_escritura.sql + los nuevos.
ALTER TABLE alertas DROP CONSTRAINT IF EXISTS alertas_tipo_check;
ALTER TABLE alertas ADD CONSTRAINT alertas_tipo_check
    CHECK (tipo IN (
        'nuevo_lead', 'cambio_etapa', 'sin_actividad', 'cuota_excedida', 'cierre',
        'reserva_creada', 'reserva_cancelada', 'conversacion_transferida',
        'perfil_incompleto', 'agenda_reprogramada', 'solicitud_escritura',
        'conversacion_asignada', 'asignacion_fallida',
        'mensaje_conversacion_asignada'
    ));
