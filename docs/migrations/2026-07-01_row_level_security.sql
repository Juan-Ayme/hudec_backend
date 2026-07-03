-- =====================================================================
-- KAWII — Row-Level Security multi-tenant
-- =====================================================================
-- En vez de añadir `WHERE company_id = :company_id` a las ~30 CTEs de las
-- matrices KAWII (que son 1248 líneas de SQL), habilitamos RLS de Postgres.
--
-- Funciona así:
--   1. Cada tabla con company_id tiene una POLICY que filtra las filas
--      visibles según `current_setting('app.current_company')`.
--   2. Antes de cada query del backend, `SET LOCAL app.current_company = X`
--      activa el filtro para la sesión/transacción actual.
--   3. Todas las queries (incluidas las 11 SQL de kawii_matrix) se
--      filtran automáticamente sin tocar el código SQL.
--
-- USAR FORCE ROW LEVEL SECURITY:
--   El usuario `postgres` es SUPERUSER y por defecto bypassa RLS.
--   FORCE hace que RLS aplique incluso al owner de la tabla.
--
-- Aplicar:
--   psql -U postgres -d kawii_mt -f docs/migrations/2026-07-01_row_level_security.sql
-- =====================================================================

BEGIN;

-- Función helper para leer el company_id activo (o levantar si no está)
CREATE OR REPLACE FUNCTION current_company_id() RETURNS int AS $$
DECLARE
    v text;
BEGIN
    v := current_setting('app.current_company', true);
    IF v IS NULL OR v = '' THEN
        -- Sin tenant activo: devolvemos NULL → las policies filtran a 0 filas
        RETURN NULL;
    END IF;
    RETURN v::int;
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END;
$$ LANGUAGE plpgsql STABLE;


-- Tablas con company_id (todas las que sincronizamos + config)
DO $$
DECLARE
    t text;
    tables text[] := ARRAY[
        'app_config', 'app_config_history',
        'sync_log', 'data_quality_issues', 'webhook_events',
        'offices', 'document_types', 'users',
        'departments', 'categories', 'subcategories',
        'product_types', 'products', 'product_type_attributes',
        'variants', 'variant_attribute_values', 'variant_costs',
        'category_targets',
        'documents', 'document_details',
        'receptions', 'reception_details',
        'consumptions', 'consumption_details',
        'stock_levels', 'stock_history',
        'purchase_decisions'
    ];
BEGIN
    FOREACH t IN ARRAY tables LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        -- Policy: sólo filas de la empresa activa, o TODAS si el setting es NULL
        -- (útil para queries administrativas que hacen `RESET app.current_company`).
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (current_company_id() IS NULL OR company_id = current_company_id()) '
            'WITH CHECK (company_id = current_company_id())',
            t
        );
    END LOOP;
END $$;

COMMIT;
