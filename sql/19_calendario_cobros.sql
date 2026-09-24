-- ============================================================
-- COBROS DE RESERVA (corte de caja diario)
-- ============================================================
-- Historia de usuario del dueño de la estética: quiere ver, por día, cuánto
-- se facturó y con qué método de pago, sin cálculos manuales.
--
-- `reservas` no traía ni precio ni método de pago -- el catálogo de
-- servicios sí tiene un precio (servicios.precio), pero es el precio DE
-- CATÁLOGO hoy, no el que se cobró en el momento. Si el precio de un
-- servicio cambia después, el corte de un día pasado no puede recalcularse
-- solo con el catálogo actual: por eso precio_cobrado se GUARDA en la
-- propia reserva al completarla (mismo criterio que hora_inicio/hora_fin en
-- 16_calendarios.sql, que tampoco se recalculan al leer).
--
-- Ambas columnas quedan NULL hasta que la cita se marca 'completada'
-- (services/calendario.py::cambiar_estado_reserva lo exige en ese momento,
-- ver schemas.CambiarEstadoReservaIn) -- una cita cancelada o que nunca se
-- atendió no tiene nada que cobrar.

ALTER TABLE reservas
    ADD COLUMN IF NOT EXISTS precio_cobrado NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS metodo_pago TEXT;

ALTER TABLE reservas DROP CONSTRAINT IF EXISTS reservas_precio_cobrado_valido;
ALTER TABLE reservas ADD CONSTRAINT reservas_precio_cobrado_valido
    CHECK (precio_cobrado IS NULL OR precio_cobrado >= 0);

ALTER TABLE reservas DROP CONSTRAINT IF EXISTS reservas_metodo_pago_valido;
ALTER TABLE reservas ADD CONSTRAINT reservas_metodo_pago_valido
    CHECK (metodo_pago IS NULL OR metodo_pago IN ('efectivo', 'tarjeta', 'transferencia'));

-- El corte diario filtra por tenant + estado + rango de hora_inicio: mismo
-- patrón que idx_reservas_tenant_horario, pero acotado a 'completada' para
-- que el índice sirva solo para esa consulta (no compite con las de
-- disponibilidad, que ya usan el índice general).
CREATE INDEX IF NOT EXISTS idx_reservas_tenant_completada_horario
    ON reservas (tenant_id, hora_inicio)
    WHERE estado = 'completada';
