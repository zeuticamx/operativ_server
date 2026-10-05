-- ------------------------------------------------------------
-- Adjuntos de mensajes (imágenes y documentos de WhatsApp).
--
-- `messages` es propiedad de n8n y `content` es TEXT NOT NULL, así que los
-- archivos viven en una tabla aparte, ligada por message_id. `messages` no se
-- toca: el mensaje lleva un texto descriptivo (la leyenda o "[Imagen]") y el
-- archivo se resuelve por esta tabla.
--
-- contenido va en BYTEA, igual que portal_user_fotos: no hay almacenamiento
-- de objetos en la infraestructura. Si algún día lo hay, solo cambia dónde
-- se guarda el contenido; el resto del modelo se queda.
--
-- token_publico: NeuroAPI/Meta descargan el archivo de una URL HTTPS pública
-- (`link`), así que el endpoint que lo sirve no puede exigir JWT. El token es
-- aleatorio e impredecible (secrets.token_urlsafe(32)) y es lo único que
-- autoriza esa descarga. Solo los adjuntos salientes lo necesitan.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS message_attachments (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    message_id    UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    direccion     VARCHAR(3) NOT NULL CHECK (direccion IN ('in', 'out')),
    mime          VARCHAR(100) NOT NULL,
    nombre        VARCHAR(255) NOT NULL,
    bytes         INTEGER NOT NULL CHECK (bytes > 0),
    contenido     BYTEA NOT NULL,
    token_publico VARCHAR(64) NOT NULL UNIQUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_message_attachments_message
    ON message_attachments (message_id);
