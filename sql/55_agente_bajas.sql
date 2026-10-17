-- ============================================================
-- 55 · agente_bajas: clientes que pidieron no recibir más mensajes
--
-- Cierra el hallazgo 1 de la auditoría del 15/10/2026, que estaba marcado
-- como pendiente en `services/agente/envio.py` porque no había dónde
-- guardar las bajas: la tabla no existía ni en producción ni en sql/.
--
-- El problema concreto: `envio._reabrir_con_plantilla` es el ÚNICO punto del
-- flujo que le escribe a un cliente sin que él haya escrito primero. Todo lo
-- demás sale dentro de la ventana de 24 h, o sea en respuesta a un mensaje
-- suyo. Esa plantilla salía igual para alguien que ya había pedido la baja,
-- que es exactamente lo que las políticas de Meta no permiten.
--
-- Esto es una tabla nueva del backend, no un rescate de n8n: va en
-- castellano como el resto del esquema del portal (`tenant_servicios`,
-- `dispositivos_push`, `plantillas_whatsapp_globales`).
--
--     python aplicar_sql.py sql/55_agente_bajas.sql
-- ============================================================


-- Sin esto, un cliente SQL con el search_path vacío o reducido resuelve los
-- nombres sin esquema contra `pg_catalog` —que está siempre, implícito y
-- primero— y el archivo falla de una forma que no parece lo que es:
--
--     ERROR: relation "conversations" does not exist
--     ERROR: permission denied to create "pg_catalog.agente_bajas"
--
-- Pasó de verdad al aplicar esto el 16/10/2026. No es que faltaran las
-- tablas: es que se buscaban en el esquema del sistema. `aplicar_sql.py` no
-- lo sufre porque hereda el search_path del backend, pero el archivo no
-- debería depender de con qué lo ejecutes.
SET search_path TO public;


-- ------------------------------------------------------------
-- La baja es por (negocio, canal, cliente)
-- ------------------------------------------------------------
-- Las tres columnas juntas son la PK porque son exactamente la consulta que
-- hace el envío —"¿este cliente de este negocio en este canal está de
-- baja?"— y porque un cliente que manda BAJA dos veces no debe dejar dos
-- filas. El índice de la PK sirve esa consulta sin añadir ninguno más.
--
-- `user_id` es el identificador del lado del canal (el teléfono en WhatsApp,
-- el PSID en Meta), igual que en `delivery_errors` y por el mismo motivo: es
-- lo único que tiene a mano el envío, que trabaja con un `Destino` y no con
-- una fila de `users`.
--
-- La baja es por canal, no por cliente: el mismo negocio puede tener a la
-- misma persona por WhatsApp y por Instagram, y pedir la baja en uno no dice
-- nada del otro. Que sea también por tenant es lo que evita que una baja en
-- un negocio calle al agente de otro.
CREATE TABLE IF NOT EXISTS agente_bajas (
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    channel_type    VARCHAR(20) NOT NULL,
    -- Identificador en el canal (teléfono / PSID), no users.id.
    user_id         VARCHAR(100) NOT NULL,
    -- Qué palabra de la lista cerrada de `services/agente/bajas.py` disparó
    -- la baja, ya normalizada ('baja', 'stop', 'dar de baja'…).
    --
    -- Se guarda la palabra y NO el mensaje del cliente, a propósito y por el
    -- mismo criterio que `delivery_errors` (hallazgo 3): sale de una lista
    -- cerrada del código, así que no es contenido de la conversación, y es
    -- lo que hace falta si algún día hay que justificar por qué se dejó de
    -- escribir a alguien.
    palabra         VARCHAR(40),
    -- Hoy siempre 'cliente': la pidió él escribiendo. Existe para cuando
    -- gerencia o el dueño puedan darla de baja a mano desde el portal, que
    -- es la continuación natural y no hace falta para cerrar el hallazgo.
    origen          VARCHAR(20) NOT NULL DEFAULT 'cliente',
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, channel_type, user_id)
);


-- ------------------------------------------------------------
-- Qué NO hace esta tabla
-- ------------------------------------------------------------
-- No silencia al agente. Estar de baja bloquea solo la plantilla de
-- reapertura; si el cliente vuelve a escribir, el agente le contesta
-- normalmente. Es deliberado y es lo que corresponde: al escribir él, la
-- ventana de 24 h se abre y la conversación la inicia el cliente, no el
-- negocio. Lo que la baja prohíbe es que el negocio arranque la
-- conversación, no que atienda a quien pregunta.
--
-- Quitarse la baja se hace escribiendo ALTA (ver `bajas.py`); un mensaje
-- cualquiera no la levanta, para que no se pierda sin querer.
--
-- Comprobación:
--
--     SELECT COUNT(*) FROM agente_bajas;
