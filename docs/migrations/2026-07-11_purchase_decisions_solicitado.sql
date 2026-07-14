-- =====================================================================
-- KAWII — purchase_decisions: nuevo estado 'solicitado' + actor_user_id
-- =====================================================================
-- Contexto: los encargados de tienda (rol `viewer`, ej. Deisy en Asamblea)
-- necesitan poder AVISAR que un SKU en quiebre hay que pedirlo, sin poder
-- decidir la compra. "Solicitar" es un estado más dentro del historial de
-- decisiones que ya existe (se apila como los demás; la vigente es la más
-- reciente por company_id + variant + office).
--
-- Dos cambios sobre la tabla existente (ver 2026-06-30_multitenant_schema.sql):
--   1. CHECK de `decision` suma 'solicitado'.
--   2. Columna `actor_user_id` = quién registró la decisión. Hasta ahora la
--      tabla guardaba `created_at` (fecha/hora) pero NO quién; el admin
--      necesita ver "Solicitado por Deisy". bigint para coincidir con
--      app_users.id (bigserial). ON DELETE SET NULL: si el usuario se borra,
--      la decisión se conserva sin actor.
--
-- Idempotente: DROP CONSTRAINT IF EXISTS + ADD COLUMN IF NOT EXISTS. Re-ejecutable.
-- No toca RLS (la política tenant_isolation de purchase_decisions sigue igual).
--
-- Aplicar (NO ejecutar como parte de este cambio de código):
--   psql "$DATABASE_URL" -f docs/migrations/2026-07-11_purchase_decisions_solicitado.sql
-- =====================================================================

BEGIN;

-- 1) Ampliar el CHECK de `decision` para admitir 'solicitado'.
ALTER TABLE purchase_decisions
    DROP CONSTRAINT IF EXISTS purchase_decisions_decision_check;
ALTER TABLE purchase_decisions
    ADD  CONSTRAINT purchase_decisions_decision_check
         CHECK (decision IN (
             'solicitado', 'ordenar', 'comprar_similar', 'posponer', 'ignorar'
         ));

-- 2) Quién registró la decisión (referencia informativa; app_users es GLOBAL).
ALTER TABLE purchase_decisions
    ADD COLUMN IF NOT EXISTS actor_user_id bigint NULL
        REFERENCES app_users(id) ON DELETE SET NULL;

-- 3) Índice para la "bandeja de solicitudes" del admin: decisiones vigentes
--    de tipo 'solicitado' por empresa+sucursal, más recientes primero.
--    Parcial para mantenerlo chico (solo filas 'solicitado').
CREATE INDEX IF NOT EXISTS purchase_decisions_solicitado_idx
    ON purchase_decisions (company_id, bsale_office_id, created_at DESC)
    WHERE decision = 'solicitado';

COMMIT;

-- =====================================================================
-- VERIFICACIÓN (correr aparte si querés):
--   \d purchase_decisions
--   -- Debe listar la columna actor_user_id y el CHECK con 'solicitado'.
--   SELECT conname, pg_get_constraintdef(oid)
--     FROM pg_constraint WHERE conname = 'purchase_decisions_decision_check';
-- =====================================================================
