-- ============================================================
-- 57 · Planes: cupo de números de WhatsApp y herramientas por plan (spec 005a)
-- ============================================================
-- Dos datos nuevos que gerencia edita en /gerencia/planes:
--
--   planes.max_numeros_whatsapp  SMALLINT  cupo de números ACTIVOS (RF-1).
--                                          NULL = sin tope. Los planes
--                                          existentes arrancan en NULL (D21).
--                                          El panel traduce "0" a NULL.
--
--   plan_herramientas            qué herramientas del agente incluye cada
--                                plan (RF-9). Se expresa por FAMILIA y no por
--                                `tool_key`: las claves de las herramientas
--                                personalizadas son de cada negocio.
--
--      familia        qué herramientas son
--      calendario     las del calendario (claves fijas: servicios, proveedores,
--                     disponibilidad, reservas, historial, citas próximas...)
--      sql_lectura    tool_type 'sql_lectura'
--      http           tool_type 'http_generico' o 'http_post_json' que no son
--                     del calendario
--      google_sheets  tool_type 'google_sheets'
--      google_docs    tool_type 'google_docs'
--
-- Los flags `herramientas_activo` y `calendario_activo` (25_planes_herramientas)
-- los lee services/acceso_plan.py como "módulos del portal". Pasan a derivarse
-- de esta lista para no tener dos fuentes de verdad: la derivación la hace el
-- servicio que edita el plan (tarea T4); aquí solo se siembra la lista a partir
-- de los flags actuales.
--
-- Idempotente: aplicar_sql.py reaplica los archivos.
--
--     python aplicar_sql.py sql/57_planes_cupo_herramientas.sql
-- ============================================================

ALTER TABLE planes ADD COLUMN IF NOT EXISTS max_numeros_whatsapp SMALLINT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'planes_max_numeros_whatsapp_check'
                      AND conrelid = 'planes'::regclass) THEN
        ALTER TABLE planes ADD CONSTRAINT planes_max_numeros_whatsapp_check
            CHECK (max_numeros_whatsapp IS NULL OR max_numeros_whatsapp >= 1);
    END IF;
END
$$;


-- ------------------------------------------------------------
-- Lista de herramientas de cada plan
-- ------------------------------------------------------------
-- ON UPDATE CASCADE: un renombre futuro de un plan no deja huérfana su lista.
CREATE TABLE IF NOT EXISTS plan_herramientas (
    plan_nombre  VARCHAR(100) NOT NULL REFERENCES planes(nombre)
                     ON UPDATE CASCADE ON DELETE CASCADE,
    familia      VARCHAR(30)  NOT NULL,
    PRIMARY KEY (plan_nombre, familia),
    CONSTRAINT plan_herramientas_familia_check
        CHECK (familia IN ('calendario', 'sql_lectura', 'http', 'google_sheets', 'google_docs'))
);

-- Siembra desde los flags actuales. Solo la primera vez: con la tabla ya
-- poblada, reaplicar el archivo no pisa lo que gerencia haya editado.
-- (Efecto colateral aceptado: si gerencia vaciara la lista de TODOS los planes,
-- reaplicar el archivo la volvería a sembrar.)
--   calendario_activo   -> calendario
--   herramientas_activo -> sql_lectura, http, google_sheets, google_docs
-- Una sola sentencia: la comprobación "tabla vacía" se evalúa antes de insertar.
INSERT INTO plan_herramientas (plan_nombre, familia)
SELECT s.nombre, s.familia
  FROM (
        SELECT p.nombre, 'calendario'::text AS familia
          FROM planes p
         WHERE p.calendario_activo
        UNION ALL
        SELECT p.nombre, f.familia
          FROM planes p
         CROSS JOIN (VALUES ('sql_lectura'), ('http'), ('google_sheets'), ('google_docs')) AS f(familia)
         WHERE p.herramientas_activo
       ) s
 WHERE NOT EXISTS (SELECT 1 FROM plan_herramientas)
ON CONFLICT DO NOTHING;


-- ------------------------------------------------------------
-- Familia de una herramienta
-- ------------------------------------------------------------
-- Misma regla que services/herramientas_plan.familia_de (tarea T3): las claves
-- fijas del calendario son 'calendario'; el resto, por `tool_type`. Un tipo
-- desconocido da NULL (no pertenece a ninguna familia, no se puede activar).
-- Las claves son las de services/herramientas_calendario.py.
CREATE OR REPLACE FUNCTION familia_de_herramienta(p_tool_type TEXT, p_tool_key TEXT)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN p_tool_key IN (
            'consultar_servicios', 'consultar_proveedores', 'consultar_disponibilidad',
            'crear_reserva', 'cancelar_reserva', 'consultar_cancelacion_reserva',
            'historial_citas_cliente', 'consultar_citas_proximas', 'reagendar_reserva'
        ) THEN 'calendario'
        WHEN p_tool_type = 'sql_lectura' THEN 'sql_lectura'
        WHEN p_tool_type IN ('http_generico', 'http_post_json') THEN 'http'
        WHEN p_tool_type = 'google_sheets' THEN 'google_sheets'
        WHEN p_tool_type = 'google_docs' THEN 'google_docs'
        ELSE NULL
    END;
$$;
