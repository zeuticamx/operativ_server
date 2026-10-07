-- ============================================================
-- ENLACE OPCIONAL ENTRE UN CLIENTE DE CAMPO Y UN LEAD DEL EMBUDO
-- ============================================================
-- 07_crm_campo.sql ya dejaba esto anotado: "si algún día hay que enlazarlos,
-- es una columna user_id nullable acá, no una fusión de las dos tablas".
-- Siguen siendo dos carteras distintas (client_pipeline = contactos de
-- chat, clientes = negocios visitados en persona); esto solo permite
-- apuntar un `cliente` hacia un `user` que YA existe porque escribió por
-- WhatsApp/IG/FB. No crea usuarios ni filas de embudo — `users` es de n8n,
-- y el backend no escribe ahí (ver README).
--
-- Quién puede vincular: gerencia, igual que editar un cliente
-- (exigir_gerencia_crm en routers/clientes.py). Un vendedor solo lo ve.
--
-- Idempotente. Aplicar con aplicar_sql.py.
-- ============================================================

ALTER TABLE clientes
    ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE SET NULL;

-- Un mismo contacto de chat no puede representar a dos negocios distintos
-- del mismo tenant. Parcial porque NULL (sin vincular, lo normal) no cuenta.
CREATE UNIQUE INDEX IF NOT EXISTS idx_clientes_user_id
    ON clientes (tenant_id, user_id)
    WHERE user_id IS NOT NULL;
