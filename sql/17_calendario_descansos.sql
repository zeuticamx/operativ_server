-- ============================================================
-- DESCANSOS DE PROVEEDOR (pausas dentro de la jornada laboral)
-- ============================================================
-- Historia de usuario del barbero: quiere reservar tiempos de comida o
-- pausas activas sin que se le agenden citas ahí. Un descanso es un hueco
-- DENTRO de una ventana de atención ya configurada (proveedor_horarios o
-- proveedor_excepciones) — no define una jornada nueva, se RESTA de la que
-- ya existe (services/calendario_slots.py le da la misma prioridad que ya
-- tiene el hueco entre bloques de un turno partido).
--
-- Puede ser recurrente (dia_semana: todos los lunes, p.ej.) o puntual
-- (fecha: un día concreto) — exactamente uno de los dos, nunca ambos ni
-- ninguno, igual criterio de "uno u otro" que ya separa horario semanal de
-- excepción puntual.

CREATE TABLE IF NOT EXISTS proveedor_descansos (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    proveedor_id    UUID NOT NULL REFERENCES proveedores(id) ON DELETE CASCADE,
    dia_semana      SMALLINT,
    fecha           DATE,
    hora_inicio     TIME NOT NULL,
    hora_fin        TIME NOT NULL,
    etiqueta        TEXT,
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT proveedor_descansos_dia_o_fecha CHECK (
        (dia_semana IS NOT NULL AND fecha IS NULL)
        OR (dia_semana IS NULL AND fecha IS NOT NULL)
    ),
    CONSTRAINT proveedor_descansos_dia_valido CHECK (dia_semana IS NULL OR dia_semana BETWEEN 0 AND 6),
    CONSTRAINT proveedor_descansos_rango_valido CHECK (hora_fin > hora_inicio)
);
CREATE INDEX IF NOT EXISTS idx_proveedor_descansos_dia
    ON proveedor_descansos (proveedor_id, dia_semana) WHERE dia_semana IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_proveedor_descansos_fecha
    ON proveedor_descansos (proveedor_id, fecha) WHERE fecha IS NOT NULL;
