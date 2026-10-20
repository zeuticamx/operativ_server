-- ============================================================
-- 62 · Refresh token en cookie HttpOnly + rotación (spec 006)
-- ============================================================
-- El refresh token deja de vivir en localStorage del portal (spec 006,
-- D1-D3): viaja en una cookie HttpOnly y, además, se vuelve de un solo uso.
-- Antes, un JWT de tipo "refresh" era válido hasta su `exp` (30 días) sin
-- que el servidor supiera cuántos seguían vivos ni pudiera apagar uno en
-- concreto; la única defensa era `credenciales_cambiadas_en` (todo o nada).
--
-- Esta tabla es la lista de sesiones de refresh vigentes, una fila por
-- login/alta/invitación (y una por dispositivo, si entran desde varios).
-- El JWT sigue siendo la prueba criptográfica; `jti` (su claim nuevo, ver
-- security.py) es la clave para buscar la fila y decidir:
--   - no existe la fila            -> sesión cerrada (logout, limpieza)
--   - usado_en IS NULL             -> vigente, se puede canjear (y rota)
--   - usado_en IS NOT NULL         -> reuso: alguien repite un refresh ya
--     gastado. Si es la carrera normal de dos pestañas refrescando casi a
--     la vez (< 30 s), se rechaza sin más. Pasado ese margen, es la señal
--     de un refresh robado y usado por dos partes: se revocan TODAS las
--     sesiones del usuario (routers/auth.py decide el umbral, no esta
--     tabla).
--
-- Idempotente: CREATE TABLE/INDEX IF NOT EXISTS.
--
-- Aplicar:
--     python aplicar_sql.py sql/62_refresh_sesiones.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS refresh_sesiones (
    jti        UUID PRIMARY KEY,
    user_id    UUID NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    emitido_en TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expira_en  TIMESTAMPTZ NOT NULL,
    -- NULL mientras la sesión sigue vigente; se fija al canjearla por una
    -- nueva (rotación) o, de forma implícita, deja de importar cuando la
    -- fila se borra entera (logout, revocación, limpieza de vencidas).
    usado_en   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_refresh_sesiones_user ON refresh_sesiones (user_id);
