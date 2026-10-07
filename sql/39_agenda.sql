-- ============================================================
-- AGENDA DE VENTAS: seguimientos del embudo + bitácora de reprogramaciones
-- ============================================================
-- La agenda del portal (/vendedores/agenda y /mi-cartera) junta dos cosas
-- que ya existían por separado:
--
--   tareas_seguimiento   tareas del CRM de campo (07_crm_campo.sql), con
--                        su fecha_programada de siempre.
--   client_pipeline      leads del chat (06_vendedores.sql). Hasta acá no
--                        tenían fecha: un lead "en_seguimiento" no decía
--                        CUÁNDO había que volver a escribirle.
--
-- Esto agrega la fecha al lead (una sola, la próxima: un lead no tiene
-- agenda propia, tiene "lo siguiente que hay que hacer con él") y una
-- bitácora común para las dos, porque arrastrar una tarjeta a otro día en
-- el calendario es la misma acción para ambas y gerencia quiere ver quién
-- movió qué y cuántas veces.
--
-- Idempotente. Aplicar con aplicar_sql.py.
-- ============================================================


-- ------------------------------------------------------------
-- Próximo seguimiento de un lead del embudo
-- ------------------------------------------------------------
-- NULL = nada agendado, que es lo normal. Se limpia al marcarlo hecho.
-- No se toca al cambiar de etapa: pasar a 'cotizado' no dice nada sobre si
-- la llamada del jueves sigue en pie.
ALTER TABLE client_pipeline
    ADD COLUMN IF NOT EXISTS proximo_seguimiento TIMESTAMPTZ;

-- Qué hay que hacer en ese seguimiento ("mandarle la cotización").
ALTER TABLE client_pipeline
    ADD COLUMN IF NOT EXISTS seguimiento_nota TEXT;

-- La agenda pide "lo del tenant entre tal y tal fecha"; parcial porque la
-- mayoría de los leads no tiene nada agendado.
CREATE INDEX IF NOT EXISTS idx_client_pipeline_seguimiento
    ON client_pipeline (tenant_id, proximo_seguimiento)
    WHERE proximo_seguimiento IS NOT NULL;


-- ------------------------------------------------------------
-- Bitácora de reprogramaciones
-- ------------------------------------------------------------
-- Un renglón por cada vez que cambia la fecha de una tarea o de un
-- seguimiento: arrastrándola en el calendario o editándola a mano.
-- Exactamente una de las dos FKs tiene valor.
--
-- fecha_anterior NULL = se agendó por primera vez (solo seguimientos; una
-- tarea nace con fecha). fecha_nueva NULL = se marcó hecho / se quitó.
--
-- vendedor_id es el dueño del pendiente EN ESE MOMENTO, no quien lo movió:
-- así "cuántas veces se le reprogramó a Ana" no depende de reasignaciones
-- posteriores. Quién lo movió va en actor_*.
CREATE TABLE IF NOT EXISTS agenda_reprogramaciones (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tarea_id              UUID REFERENCES tareas_seguimiento(id) ON DELETE CASCADE,
    client_pipeline_id    UUID REFERENCES client_pipeline(id) ON DELETE CASCADE,
    vendedor_id           UUID REFERENCES vendedores(id) ON DELETE SET NULL,
    fecha_anterior        TIMESTAMPTZ,
    fecha_nueva           TIMESTAMPTZ,
    -- Sin FK a propósito, como en reserva_auditoria: la bitácora tiene que
    -- sobrevivir a que se borre la cuenta de quien movió la tarea.
    actor_portal_user_id  UUID,
    -- Nombre o correo legible al momento del cambio.
    actor_etiqueta        TEXT NOT NULL,
    creado_en             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT agenda_reprogramaciones_un_destino CHECK (
        (tarea_id IS NOT NULL AND client_pipeline_id IS NULL)
        OR (tarea_id IS NULL AND client_pipeline_id IS NOT NULL)
    )
);

-- El historial se pide siempre de un pendiente concreto, en orden.
CREATE INDEX IF NOT EXISTS idx_agenda_reprog_tarea
    ON agenda_reprogramaciones (tarea_id, creado_en)
    WHERE tarea_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_agenda_reprog_pipeline
    ON agenda_reprogramaciones (client_pipeline_id, creado_en)
    WHERE client_pipeline_id IS NOT NULL;


-- ------------------------------------------------------------
-- Tipo de alerta nuevo: aviso personal al vendedor cuando gerencia le
-- mueve un pendiente. Misma lista que 29_perfil_usuario.sql + el nuevo.
-- ------------------------------------------------------------
ALTER TABLE alertas DROP CONSTRAINT IF EXISTS alertas_tipo_check;
ALTER TABLE alertas ADD CONSTRAINT alertas_tipo_check
    CHECK (tipo IN (
        'nuevo_lead', 'cambio_etapa', 'sin_actividad', 'cuota_excedida', 'cierre',
        'reserva_creada', 'reserva_cancelada', 'conversacion_transferida',
        'perfil_incompleto', 'agenda_reprogramada'
    ));
