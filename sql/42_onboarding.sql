-- ============================================================
-- CUESTIONARIO DE BIENVENIDA (onboarding del dueño)
-- ============================================================
-- Al crear una cuenta, el dueño contesta un cuestionario corto sobre su
-- negocio (giro, si agenda citas, tamaño del equipo...). Con eso
-- services/onboarding.py arma un system prompt de arranque, sugiere qué
-- módulos encender y recomienda el plan que cubre lo que necesita.
--
-- Tabla propia del portal y no columnas en `tenants` (que es de n8n) —
-- mismo criterio que tenant_servicios y tenant_estado_plataforma.
--
-- Una fila por tenant. Solo la crea el alta de una cuenta nueva
-- (routers/auth._crear_cuenta) o el primer PUT del dueño: los negocios que
-- ya existían no tienen fila y por eso el portal no los manda solo al
-- cuestionario, pero pueden abrirlo desde /agente.
--
--   respuestas        lo que contestó (JSONB validado por OnboardingRespuestasIn)
--   plan_recomendado  el plan que se le recomendó al aplicar (foto de ese
--                     momento: la matriz de `planes` puede cambiar después)
--   prompt_generado   el último prompt que escribió el onboarding en
--                     tenant_agent_config: así reaplicar sabe si el dueño lo
--                     editó a mano después y no se lo pisa sin preguntar
--   completado_en     aplicó la configuración sugerida
--   omitido_en        eligió "Omitir por ahora"
--   prueba_otorgada_en  el onboarding le dio el plan de prueba (una sola vez)
--
-- Gerencia de plataforma lee estas respuestas (GET /gerencia/tenants/{id}/onboarding);
-- está declarado en el aviso de privacidad del portal.
--
-- Idempotente: aplicar_sql.py reaplica los archivos.
-- ============================================================

CREATE TABLE IF NOT EXISTS tenant_onboarding (
    tenant_id           UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    respuestas          JSONB,
    plan_recomendado    VARCHAR(50),
    prompt_generado     TEXT,
    completado_en       TIMESTAMPTZ,
    omitido_en          TIMESTAMPTZ,
    prueba_otorgada_en  TIMESTAMPTZ,
    creado_en           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actualizado_en      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Sin índices extra: todo acceso (GET /auth/yo, el portal, gerencia) va por
-- tenant_id, que es la PK.
