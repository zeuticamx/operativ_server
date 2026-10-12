-- ============================================================
-- REGLA DE CANCELACIÓN DE CITAS (antelación mínima por servicio)
-- ============================================================
-- Cada servicio puede exigir una antelación mínima, en horas, para que el
-- CLIENTE cancele su cita (por el agente de n8n). NULL = sin regla (como hasta
-- ahora); 0 = se puede cancelar hasta el inicio. El equipo (portal) nunca queda
-- sujeto a la regla, pero su cancelación también se registra.
--
-- Al cancelar, la reserva guarda una "foto" de lo que pasó, para que editar el
-- servicio después no reescriba la historia ni el reporte de plataforma:
--   cancelada_por               'cliente' (vía agente) | 'equipo' (vía portal)
--   cancelacion_tardia          true solo si la canceló el cliente fuera de plazo
--   antelacion_aplicada_horas   la regla vigente en ese momento (NULL si no había)
--   cancelada_en                instante de la cancelación
--
-- Aditiva: columnas nullables o con DEFAULT constante, sin reescritura de datos.
-- Servicios y reservas existentes quedan en NULL / NULL / false / NULL / NULL.
-- El evento de auditoría 'cancelada' ya existe; origen, antelación y segundos
-- restantes van en reserva_auditoria.datos_nuevos (JSONB), sin migración.
--
-- Idempotente. Aplicar con aplicar_sql.py.
-- ============================================================

ALTER TABLE servicios
    ADD COLUMN IF NOT EXISTS antelacion_cancelacion_horas SMALLINT
        CHECK (antelacion_cancelacion_horas IS NULL
               OR antelacion_cancelacion_horas BETWEEN 0 AND 720);

ALTER TABLE reservas
    ADD COLUMN IF NOT EXISTS cancelada_por TEXT
        CHECK (cancelada_por IS NULL OR cancelada_por IN ('cliente', 'equipo')),
    ADD COLUMN IF NOT EXISTS cancelacion_tardia BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS antelacion_aplicada_horas SMALLINT,
    ADD COLUMN IF NOT EXISTS cancelada_en TIMESTAMPTZ;


-- ------------------------------------------------------------
-- Reporte de plataforma: cancelaciones del cliente por tenant y periodo
-- ------------------------------------------------------------
-- Índice parcial: solo las canceladas por el cliente (la minoría), que son las
-- únicas que lee el reporte.
CREATE INDEX IF NOT EXISTS idx_reservas_cancelada_cliente
    ON reservas (tenant_id, cancelada_en)
    WHERE cancelada_por = 'cliente';
