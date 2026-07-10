-- =====================================================================
-- KAWII — Tabla de auditoría por empresa: event_log
-- =====================================================================
-- Registra eventos de negocio/seguridad (logins, altas/bajas de usuarios,
-- disparos de sync, cambios de configuración) con contexto multi-tenant.
--
-- Escrita por `app/events.py::log_event`, que además emite un log estructurado.
-- El INSERT del helper corre dentro de un SAVEPOINT: si esta tabla todavía no
-- existe (deploy previo a esta migración), el fallo se captura y NO rompe el
-- request — de ahí que la migración pueda aplicarse sin coordinar downtime.
--
-- RLS: mismo patrón EXACTO que docs/migrations/2026-07-01_row_level_security.sql
--   USING       (current_company_id() IS NULL OR company_id = current_company_id())
--   WITH CHECK  (company_id = current_company_id())
-- Depende de la función current_company_id() creada en esa migración (2026-07-01),
-- y de FORCE ROW LEVEL SECURITY para que aplique incluso al owner (postgres).
--
-- Idempotente: CREATE ... IF NOT EXISTS + DROP POLICY IF EXISTS. Re-ejecutable.
--
-- Aplicar (NO ejecutar como parte de este cambio de código):
--   psql -U postgres -d kawii_mt -f docs/migrations/2026-07-10_event_log.sql
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS event_log (
    id             bigserial   PRIMARY KEY,
    company_id     integer     NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    event_type     text        NOT NULL,
    -- Sin FK a app_users: los usuarios son GLOBALes y app_users.id es bigint;
    -- guardamos el id como referencia informativa (puede ser NULL en eventos
    -- de sistema). Rango int alcanza de sobra para el volumen de usuarios.
    actor_user_id  integer     NULL,
    request_id     text        NULL,
    payload        jsonb       NOT NULL DEFAULT '{}',
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- Consulta típica: timeline de una empresa, más reciente primero.
CREATE INDEX IF NOT EXISTS idx_event_log_company_created
    ON event_log (company_id, created_at DESC);

-- Filtro por tipo de evento dentro de una empresa (ej. solo "auth.*").
CREATE INDEX IF NOT EXISTS idx_event_log_company_type
    ON event_log (company_id, event_type, created_at DESC);

-- ---------------------------------------------------------------------
-- Row-Level Security (mismo patrón que las demás tablas tenant)
-- ---------------------------------------------------------------------
ALTER TABLE event_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE event_log FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON event_log;
CREATE POLICY tenant_isolation ON event_log
    USING (current_company_id() IS NULL OR company_id = current_company_id())
    WITH CHECK (company_id = current_company_id());

COMMIT;

-- =====================================================================
-- VERIFICACIÓN (correr aparte si querés):
--   SELECT event_type, count(*) FROM event_log GROUP BY 1 ORDER BY 2 DESC;
--   -- Con app.current_company seteado, solo ves los de esa empresa.
-- =====================================================================
