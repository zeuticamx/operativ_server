-- ============================================================
-- INVITACIONES AL EQUIPO DEL NEGOCIO
-- ============================================================
-- Hasta acá la única forma de tener cuenta en el portal era registrarse
-- como dueño de un negocio nuevo. Con esto el dueño invita a su gente:
--
--   member    ve y opera el negocio (conversaciones, embudo), no lo configura
--   vendedor  solo lo suyo; la invitación queda atada a una ficha de
--             `vendedores` y al aceptarla se llena vendedores.portal_user_id
--
-- POST /api/equipo/invitaciones       -> crea la fila y manda el enlace
-- POST /api/auth/aceptar-invitacion   -> con el enlace, crea la cuenta
--
-- El invitado elige su propia contraseña: el dueño nunca la ve ni la pone.
-- Rol y tenant salen de ESTA fila, nunca de lo que mande el invitado.
--
-- Token: 32 bytes aleatorios en el enlace; acá solo su SHA-256. Con esa
-- entropía un hash rápido alcanza (no hay diccionario que probar), y deja
-- buscar la fila por el hash. Quien lea la tabla no se lleva enlaces usables.
--
-- Idempotente. Aplicar con aplicar_sql.py.
-- ============================================================

CREATE TABLE IF NOT EXISTS invitaciones_equipo (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email           VARCHAR(255) NOT NULL,
    role            VARCHAR(50) NOT NULL CHECK (role IN ('member', 'vendedor')),
    -- Solo para role = 'vendedor' (ver el CHECK de abajo). CASCADE: si se
    -- borra la ficha, la invitación pendiente ya no tiene a quién ligar.
    vendedor_id     UUID REFERENCES vendedores(id) ON DELETE CASCADE,

    token_hash      TEXT NOT NULL UNIQUE,

    invitada_por    UUID REFERENCES portal_users(id) ON DELETE SET NULL,
    creada_en       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expira_en       TIMESTAMPTZ NOT NULL,
    -- Una invitación termina de una de dos formas: aceptada (y entonces
    -- portal_user_id apunta a la cuenta creada) o revocada (por el dueño,
    -- o porque la reemplazó un reenvío).
    aceptada_en     TIMESTAMPTZ,
    portal_user_id  UUID REFERENCES portal_users(id) ON DELETE SET NULL,
    revocada_en     TIMESTAMPTZ,

    CONSTRAINT invitacion_vendedor_con_ficha
        CHECK ((role = 'vendedor') = (vendedor_id IS NOT NULL))
);

-- Una sola invitación viva por correo dentro de un negocio: invitar de nuevo
-- revoca la anterior (routers/equipo.py) en vez de dejar dos enlaces útiles.
CREATE UNIQUE INDEX IF NOT EXISTS idx_invitaciones_pendiente_email
    ON invitaciones_equipo (tenant_id, LOWER(email))
    WHERE aceptada_en IS NULL AND revocada_en IS NULL;

-- Y una por ficha de vendedor, por lo mismo.
CREATE UNIQUE INDEX IF NOT EXISTS idx_invitaciones_pendiente_vendedor
    ON invitaciones_equipo (vendedor_id)
    WHERE vendedor_id IS NOT NULL AND aceptada_en IS NULL AND revocada_en IS NULL;

CREATE INDEX IF NOT EXISTS idx_invitaciones_tenant
    ON invitaciones_equipo (tenant_id, creada_en DESC);
