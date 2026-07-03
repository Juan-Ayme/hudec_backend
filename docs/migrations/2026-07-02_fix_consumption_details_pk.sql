-- =====================================================================
-- KAWII — Fix: consumption_details PK compuesta (multi-tenant)
-- =====================================================================
-- Bug: consumption_details fue la única tabla que quedó con
--   `id  serial PRIMARY KEY`  ← PK GLOBAL, no multi-tenant.
-- Todas las otras tablas usan `PRIMARY KEY (company_id, bsale_X_id)`.
-- Resultado: cuando Coya intenta insertar un ID de BSale que ya existe
-- para Kawii (BSale asigna IDs por cuenta, no globalmente), ON CONFLICT
-- dispara UPDATE, y RLS rechaza modificar filas de otra empresa.
--
-- Este fix:
--   1. Guarda los datos actuales.
--   2. Recrea la tabla con la estructura correcta:
--        PRIMARY KEY (company_id, bsale_consumption_detail_id)
--   3. Migra los datos: el `id` viejo era en realidad el ID de BSale
--      del detalle, así que va directo a `bsale_consumption_detail_id`.
--   4. Habilita RLS con la misma policy que las otras tablas.
--
-- Aplicar con el rol OWNER de la DB (neondb_owner en Neon, postgres local):
--   psql <URL_OWNER> -f docs/migrations/2026-07-02_fix_consumption_details_pk.sql
-- =====================================================================

BEGIN;

-- 1. Guardar datos actuales
ALTER TABLE consumption_details RENAME TO consumption_details_old;

-- 1b. Renombrar los índices de la tabla vieja: PostgreSQL NO los renombra
-- automáticamente con el RENAME TABLE, y sus nombres colisionan con los
-- índices nuevos que vamos a crear más abajo.
ALTER INDEX IF EXISTS idx_consumption_details_cons RENAME TO idx_consumption_details_cons_old;
ALTER INDEX IF EXISTS idx_consumption_details_var  RENAME TO idx_consumption_details_var_old;

-- 2. Crear tabla nueva con estructura multi-tenant correcta
CREATE TABLE consumption_details (
    company_id                    integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_consumption_detail_id   integer NOT NULL,
    bsale_consumption_id          integer NOT NULL,
    bsale_variant_id              integer NOT NULL,
    quantity                      numeric NOT NULL,
    created_at                    timestamptz DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (company_id, bsale_consumption_detail_id),
    FOREIGN KEY (company_id, bsale_consumption_id)
        REFERENCES consumptions(company_id, bsale_consumption_id) ON DELETE CASCADE
);

-- 3. Migrar datos: el `id` viejo era el ID de BSale del detalle
INSERT INTO consumption_details
    (company_id, bsale_consumption_detail_id, bsale_consumption_id,
     bsale_variant_id, quantity, created_at)
SELECT company_id, id, bsale_consumption_id, bsale_variant_id, quantity, created_at
FROM consumption_details_old;

-- 4. Recrear índices
CREATE INDEX idx_consumption_details_cons ON consumption_details (company_id, bsale_consumption_id);
CREATE INDEX idx_consumption_details_var  ON consumption_details (company_id, bsale_variant_id);

-- 5. Habilitar RLS igual que las otras tablas
ALTER TABLE consumption_details ENABLE ROW LEVEL SECURITY;
ALTER TABLE consumption_details FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON consumption_details
    USING (current_company_id() IS NULL OR company_id = current_company_id())
    WITH CHECK (company_id = current_company_id());

-- 6. Grants para el rol de la app
GRANT SELECT, INSERT, UPDATE, DELETE ON consumption_details TO hudec_app;

-- 7. Drop tabla vieja (ya migrada)
DROP TABLE consumption_details_old;

COMMIT;

-- Verificación (correr manualmente después):
--   SELECT company_id, COUNT(*) FROM consumption_details GROUP BY company_id;
