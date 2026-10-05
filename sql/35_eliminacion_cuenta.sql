-- ------------------------------------------------------------
-- Borrado de cuenta por el propio dueño (routers/cuenta.py).
--
-- Borrar la cuenta exige la contraseña actual (o una credencial de Google
-- recién emitida). Para que ese endpoint no sirva para adivinar contraseñas
-- sin límite, se cuentan los fallos por usuario y se bloquea un rato al
-- llegar al tope (services/eliminacion_cuenta.py).
--
-- El borrado en sí no necesita columnas: es el mismo DELETE FROM tenants en
-- cascada que ya usa gerencia (services/eliminacion_tenant.py).
-- ------------------------------------------------------------
ALTER TABLE portal_users
    ADD COLUMN IF NOT EXISTS eliminacion_intentos SMALLINT NOT NULL DEFAULT 0;

ALTER TABLE portal_users
    ADD COLUMN IF NOT EXISTS eliminacion_bloqueada_hasta TIMESTAMPTZ;
