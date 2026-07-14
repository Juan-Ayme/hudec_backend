-- =====================================================================
-- KAWII — user_offices: acceso de un usuario acotado a sucursales
-- =====================================================================
-- Agrega un nivel más fino de acceso DENTRO de una empresa. Hoy la membresía
-- llega hasta empresa (`user_companies` + rol). Esta tabla restringe, además,
-- a qué SUCURSALES puede ver un usuario en esa empresa.
--
-- Regla de interpretación (retrocompatible):
--   • SIN filas para (user, company)  → el usuario ve TODAS las sucursales de
--     esa empresa (comportamiento actual; no rompe a los usuarios existentes).
--   • CON filas                        → queda restringido SOLO a esas sucursales.
--
-- Caso de uso: Deisy (rol viewer) limitada a la sucursal Asamblea.
--
-- Multi-tenant: lleva company_id + RLS con el MISMO patrón que las demás
-- tablas tenant (ver 2026-07-01_row_level_security.sql y 2026-07-10_event_log.sql).
-- Depende de la función current_company_id().
--
-- Idempotente: CREATE ... IF NOT EXISTS + DROP POLICY IF EXISTS. Re-ejecutable.
--
-- Aplicar (NO ejecutar como parte de este cambio de código):
--   psql "$DATABASE_URL" -f docs/migrations/2026-07-11_user_offices.sql
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS user_offices (
    user_id         bigint  NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    company_id      integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_office_id integer NOT NULL,
    PRIMARY KEY (user_id, company_id, bsale_office_id),
    -- La FK compuesta garantiza que la sucursal pertenece a esa empresa.
    FOREIGN KEY (company_id, bsale_office_id)
        REFERENCES offices(company_id, bsale_office_id) ON DELETE CASCADE
);

-- "¿Quién tiene acceso a esta sucursal?" (auditoría / futuros filtros inversos).
CREATE INDEX IF NOT EXISTS user_offices_company_office_idx
    ON user_offices (company_id, bsale_office_id);

-- ---------------------------------------------------------------------
-- Row-Level Security (mismo patrón que las demás tablas tenant)
-- ---------------------------------------------------------------------
ALTER TABLE user_offices ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_offices FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON user_offices;
CREATE POLICY tenant_isolation ON user_offices
    USING (current_company_id() IS NULL OR company_id = current_company_id())
    WITH CHECK (company_id = current_company_id());

COMMIT;

-- =====================================================================
-- VERIFICACIÓN (correr aparte si querés):
--   \d user_offices
--   -- Con app.current_company seteado, SELECT solo ve filas de esa empresa.
--   -- /auth/me (sin company seteada) las ve todas: correcto, arma el selector.
-- =====================================================================
