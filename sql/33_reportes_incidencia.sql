-- ============================================================
-- REPORTES DE INCIDENCIAS DESDE EL PORTAL
-- ============================================================
-- Un usuario reporta un problema desde el botón "Reportar un problema" del
-- sidebar (POST /api/incidencias). Queda acá y se avisa por correo a
-- gerencia_users; el equipo los ve en /gerencia/incidencias.
--
-- No se usa gerencia_alertas: su índice permite UNA alerta abierta por
-- (tipo, tenant), y un segundo reporte del mismo negocio quedaría oculto
-- detrás del primero. Cada reporte es un registro propio.
--
-- Quién reporta (portal_user_id, tenant_id, email) lo escribe el servidor
-- desde el JWT, nunca el formulario. `contexto` es lo que manda el
-- navegador (ruta, navegador, SO, resolución…): sirve para depurar, no es
-- una fuente de verdad y el backend lo recorta a una lista de claves
-- conocidas.
--
-- tenant_id sin FK y email copiado: igual que gerencia_auditoria, el
-- reporte sobrevive a que borren el negocio o al usuario.
--
-- El adjunto (una captura PNG/JPEG) va en tabla aparte, como
-- portal_user_fotos: listar reportes no tiene por qué arrastrar los bytes.
--
-- Idempotente. Aplicar con aplicar_sql.py.
-- ============================================================

CREATE TABLE IF NOT EXISTS reportes_incidencia (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portal_user_id UUID REFERENCES portal_users(id) ON DELETE SET NULL,
    tenant_id      UUID,
    email          VARCHAR(255) NOT NULL,
    resumen        VARCHAR(120) NOT NULL,
    descripcion    TEXT         NOT NULL,
    contexto       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    estado         VARCHAR(20)  NOT NULL DEFAULT 'abierto'
                   CHECK (estado IN ('abierto', 'en_revision', 'resuelto')),
    creado_en      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    atendido_por   VARCHAR(255),
    atendido_en    TIMESTAMPTZ
);

-- Tope por usuario y hora (services/incidencias.py) y listado por fecha.
CREATE INDEX IF NOT EXISTS idx_reportes_usuario_fecha
    ON reportes_incidencia (portal_user_id, creado_en DESC);
CREATE INDEX IF NOT EXISTS idx_reportes_estado_fecha
    ON reportes_incidencia (estado, creado_en DESC);

CREATE TABLE IF NOT EXISTS reportes_incidencia_adjuntos (
    reporte_id UUID PRIMARY KEY REFERENCES reportes_incidencia(id) ON DELETE CASCADE,
    contenido  BYTEA       NOT NULL,
    mime       VARCHAR(20) NOT NULL CHECK (mime IN ('image/jpeg', 'image/png')),
    ancho      INTEGER     NOT NULL,
    alto       INTEGER     NOT NULL,
    bytes      INTEGER     NOT NULL
);
