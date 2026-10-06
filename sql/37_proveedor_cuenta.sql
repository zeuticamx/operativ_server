-- ============================================================
-- CUENTA PROPIA PARA LOS PROVEEDORES DEL CALENDARIO
-- ============================================================
-- Revierte una decisión de 16_calendarios.sql ("el proveedor nunca inicia
-- sesión"): ahora el dueño puede invitar a cada barbero/estilista, igual
-- que a un vendedor (36_invitaciones_equipo.sql), con el rol 'proveedor'.
--
-- Qué ve un proveedor (deps.proveedor_actual, routers/calendario.py y
-- routers/conversaciones.py):
--   - Sus citas: verlas, cancelarlas y marcarlas completada/no asistió.
--   - Su disponibilidad: descansos y días libres (el horario base lo fija el
--     dueño).
--   - Las conversaciones de los clientes cuya cita MÁS RECIENTE es con él
--     (cancelada incluida). Un cliente que reservó con dos proveedores es
--     solo del último: así una conversación nunca tiene dos dueños.
--
-- Además:
--   - planes.max_proveedores: tope de proveedores activos por plan. NULL =
--     sin tope, que es como quedan todos los planes hasta que gerencia de
--     plataforma lo defina.
--   - reserva_auditoria admite origen 'sistema': el job que da por
--     'no_asistio' las citas confirmadas que ya pasaron
--     (jobs/reservas_background.py) no es ni el portal ni n8n.
--
-- Idempotente. Aplicar con aplicar_sql.py.
-- ============================================================

-- ------------------------------------------------------------
-- La ficha del proveedor, ligada a su cuenta
-- ------------------------------------------------------------
ALTER TABLE proveedores
    ADD COLUMN IF NOT EXISTS portal_user_id UUID REFERENCES portal_users(id) ON DELETE SET NULL;

-- Una cuenta, una ficha: si no, "mis citas" sería ambiguo. Parcial porque
-- NULL (proveedor sin acceso) es lo normal.
CREATE UNIQUE INDEX IF NOT EXISTS idx_proveedores_portal_user
    ON proveedores (portal_user_id)
    WHERE portal_user_id IS NOT NULL;


-- ------------------------------------------------------------
-- Invitaciones con rol 'proveedor'
-- ------------------------------------------------------------
ALTER TABLE invitaciones_equipo
    ADD COLUMN IF NOT EXISTS proveedor_id UUID REFERENCES proveedores(id) ON DELETE CASCADE;

ALTER TABLE invitaciones_equipo DROP CONSTRAINT IF EXISTS invitaciones_equipo_role_check;
ALTER TABLE invitaciones_equipo ADD CONSTRAINT invitaciones_equipo_role_check
    CHECK (role IN ('member', 'vendedor', 'proveedor'));

-- Cada rol con ficha lleva exactamente la suya, y ningún otro lleva ficha.
ALTER TABLE invitaciones_equipo DROP CONSTRAINT IF EXISTS invitacion_vendedor_con_ficha;
ALTER TABLE invitaciones_equipo DROP CONSTRAINT IF EXISTS invitacion_ficha_segun_rol;
ALTER TABLE invitaciones_equipo ADD CONSTRAINT invitacion_ficha_segun_rol CHECK (
    ((role = 'vendedor') = (vendedor_id IS NOT NULL))
    AND ((role = 'proveedor') = (proveedor_id IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_invitaciones_pendiente_proveedor
    ON invitaciones_equipo (proveedor_id)
    WHERE proveedor_id IS NOT NULL AND aceptada_en IS NULL AND revocada_en IS NULL;


-- ------------------------------------------------------------
-- Tope de proveedores por plan
-- ------------------------------------------------------------
ALTER TABLE planes ADD COLUMN IF NOT EXISTS max_proveedores SMALLINT;


-- ------------------------------------------------------------
-- El sistema como actor de la bitácora de reservas
-- ------------------------------------------------------------
ALTER TABLE reserva_auditoria DROP CONSTRAINT IF EXISTS reserva_auditoria_origen_check;
ALTER TABLE reserva_auditoria ADD CONSTRAINT reserva_auditoria_origen_check
    CHECK (origen IN ('portal', 'n8n', 'sistema'));


-- ------------------------------------------------------------
-- Índices de los dos accesos nuevos
-- ------------------------------------------------------------
-- "¿Con quién es la cita más reciente de este cliente?": se pregunta por
-- cada conversación que lista o abre un proveedor.
CREATE INDEX IF NOT EXISTS idx_reservas_tenant_user_creada
    ON reservas (tenant_id, user_id, creado_en DESC)
    WHERE user_id IS NOT NULL;

-- El job de citas vencidas solo mira las que siguen confirmadas.
CREATE INDEX IF NOT EXISTS idx_reservas_confirmadas_fin
    ON reservas (hora_fin)
    WHERE estado = 'confirmada';
