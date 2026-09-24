-- ============================================================
-- BITÁCORA DE EDICIONES DE SERVICIOS (servicio_auditoria)
-- ============================================================
-- Historia de usuario del dueño de la estética: quiere poder editar
-- nombre/duración/precio de un servicio del catálogo sin recrearlo, y que
-- quede constancia de quién cambió qué y cuándo (mismo pedido que ya
-- resolvió reserva_auditoria en 18_calendario_auditoria.sql, pero para el
-- catálogo en vez de las citas).
--
-- Solo se audita EDICIÓN (PATCH), no alta ni el toggle de `activo` por
-- separado: la creación ya queda implícita en `servicios.creado_en`, y
-- activar/desactivar es la vía de baja normal del catálogo (ver
-- 16_calendarios.sql), no un cambio de dato que alguien necesite disputar
-- después. Cada fila guarda solo los campos que SÍ cambiaron (no el
-- servicio completo antes/después), mismo criterio que
-- reserva_auditoria.datos_anteriores/datos_nuevos.
--
-- Nunca hay actor "n8n" acá: el catálogo solo lo edita gerencia desde el
-- portal (no hay endpoint de edición de servicios en routers/eventos.py).

CREATE TABLE IF NOT EXISTS servicio_auditoria (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- RESTRICT y no CASCADE: los servicios nunca se borran de verdad (solo
    -- se desactivan), así que un intento de borrar uno con historial encima
    -- debe fallar en vez de arrastrar el rastro.
    servicio_id UUID NOT NULL REFERENCES servicios(id) ON DELETE RESTRICT,

    datos_anteriores JSONB NOT NULL,
    datos_nuevos     JSONB NOT NULL,

    actor_email          VARCHAR(255) NOT NULL,
    actor_portal_user_id UUID,

    creado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_servicio_auditoria_servicio
    ON servicio_auditoria (servicio_id, creado_en DESC);
