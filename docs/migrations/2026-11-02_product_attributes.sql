-- =====================================================================
-- KAWII — Atributos de producto/variante + proveedores
-- =====================================================================
-- Capa propia de atributos para analizar más allá de la taxonomía:
-- marca, origen, público, licencia, estilo, temporada, material, contenido,
-- unidades por pack, unidades por caja y grupo (nivel producto); color, diseño, talla y
-- sabor/aroma (nivel variante). El proveedor vive en su propia tabla.
--
-- Por qué tablas propias y no los atributos de BSale: en BSale un atributo se
-- define POR TIPO DE PRODUCTO y se carga por variante, y sus reportes no
-- agrupan por atributo. Acá el maestro es nuestro; los atributos de variante
-- marcados `push_to_bsale` se empujan después (tools/taxonomy/push_variant_attributes.py).
-- `variant_attribute_values` (espejo de BSale) NO se toca.
--
-- Origen de cada valor (`source`), de mayor a menor prioridad:
--   manual > driver > bsale > inferred
-- La inferencia nunca pisa un valor manual ni de driver (lo garantiza el código).
--
-- Nada de esta migración borra ni modifica productos, variantes ni ventas.
--
-- RLS: mismo patrón EXACTO que docs/migrations/2026-07-10_event_log.sql.
-- Idempotente: CREATE ... IF NOT EXISTS + DROP POLICY IF EXISTS + CREATE OR REPLACE VIEW.
--
-- Aplicar:
--   psql -U postgres -d kawii_mt -f docs/migrations/2026-11-02_product_attributes.sql
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 1. Definiciones de atributos (por empresa; se siembran desde
--    app/services/attribute_catalog.py vía ensure_definitions)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS attribute_definitions (
    company_id      integer      NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    code            varchar(40)  NOT NULL,
    name            varchar(120) NOT NULL,
    level           varchar(10)  NOT NULL CHECK (level IN ('product', 'variant')),
    data_type       varchar(10)  NOT NULL DEFAULT 'text'
                                 CHECK (data_type IN ('text', 'enum', 'number')),
    allowed_values  jsonb        NOT NULL DEFAULT '[]',
    push_to_bsale   boolean      NOT NULL DEFAULT false,
    sort_order      integer      NOT NULL DEFAULT 0,
    created_at      timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, code)
);

-- ---------------------------------------------------------------------
-- 2. Valores por producto
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_attrs (
    company_id        integer       NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_product_id  integer       NOT NULL,
    code              varchar(40)   NOT NULL,
    value_text        varchar(300),
    value_num         numeric(14,4),
    unit              varchar(20),
    source            varchar(10)   NOT NULL DEFAULT 'manual'
                                    CHECK (source IN ('manual', 'driver', 'bsale', 'inferred')),
    confidence        numeric(3,2),
    updated_by        integer       NULL,   -- id informativo del usuario (sin FK, igual que event_log)
    updated_at        timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_product_id, code),
    FOREIGN KEY (company_id, bsale_product_id)
        REFERENCES products(company_id, bsale_product_id) ON DELETE CASCADE,
    FOREIGN KEY (company_id, code)
        REFERENCES attribute_definitions(company_id, code) ON DELETE CASCADE
);
-- Agrupar ventas por un atributo (ej. todas las marcas) filtra por code.
CREATE INDEX IF NOT EXISTS idx_product_attrs_code_value
    ON product_attrs (company_id, code, value_text);

-- ---------------------------------------------------------------------
-- 3. Valores por variante (color, diseño, talla, sabor/aroma)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS variant_attrs (
    company_id        integer       NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_variant_id  integer       NOT NULL,
    code              varchar(40)   NOT NULL,
    value_text        varchar(300),
    value_num         numeric(14,4),
    unit              varchar(20),
    source            varchar(10)   NOT NULL DEFAULT 'manual'
                                    CHECK (source IN ('manual', 'driver', 'bsale', 'inferred')),
    confidence        numeric(3,2),
    updated_by        integer       NULL,
    updated_at        timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_variant_id, code),
    FOREIGN KEY (company_id, bsale_variant_id)
        REFERENCES variants(company_id, bsale_variant_id) ON DELETE CASCADE,
    FOREIGN KEY (company_id, code)
        REFERENCES attribute_definitions(company_id, code) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_variant_attrs_code_value
    ON variant_attrs (company_id, code, value_text);

-- ---------------------------------------------------------------------
-- 4. Proveedores y relación producto ↔ proveedor (alimentada por el driver)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS suppliers (
    id          serial       PRIMARY KEY,
    company_id  integer      NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name        varchar(200) NOT NULL,
    ruc         varchar(20),
    notes       text,
    created_at  timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT suppliers_company_name_key UNIQUE (company_id, name),
    CONSTRAINT suppliers_company_id_key   UNIQUE (company_id, id)
);

CREATE TABLE IF NOT EXISTS product_suppliers (
    company_id          integer       NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_product_id    integer       NOT NULL,
    supplier_id         integer       NOT NULL,
    last_unit_cost      numeric(20,4),
    last_purchase_date  date,
    is_primary          boolean       NOT NULL DEFAULT true,
    source              varchar(10)   NOT NULL DEFAULT 'driver'
                                      CHECK (source IN ('manual', 'driver', 'inferred')),
    updated_at          timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_product_id, supplier_id),
    FOREIGN KEY (company_id, bsale_product_id)
        REFERENCES products(company_id, bsale_product_id) ON DELETE CASCADE,
    FOREIGN KEY (company_id, supplier_id)
        REFERENCES suppliers(company_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_product_suppliers_supplier
    ON product_suppliers (company_id, supplier_id);

-- ---------------------------------------------------------------------
-- 5. Row-Level Security (mismo patrón que las demás tablas tenant)
-- ---------------------------------------------------------------------
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'attribute_definitions', 'product_attrs', 'variant_attrs',
        'suppliers', 'product_suppliers'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (current_company_id() IS NULL OR company_id = current_company_id()) '
            'WITH CHECK (company_id = current_company_id())',
            t
        );
    END LOOP;
END $$;

-- ---------------------------------------------------------------------
-- 6. Vista enriquecida: v_products_full + atributos en columnas.
--    v_products_full NO se modifica; los reportes que necesitan atributos
--    usan esta vista (con fallback en código si todavía no existe).
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_products_enriched AS
SELECT
    vpf.*,
    a.marca,
    a.origen,
    a.publico,
    a.licencia,
    a.estilo,
    a.temporada,
    a.material,
    a.contenido,
    a.unidades_pack,
    a.unidades_caja,
    a.grupo,
    sup.proveedor
FROM v_products_full vpf
LEFT JOIN (
    SELECT
        company_id,
        bsale_product_id,
        MAX(value_text) FILTER (WHERE code = 'marca')     AS marca,
        MAX(value_text) FILTER (WHERE code = 'origen')    AS origen,
        MAX(value_text) FILTER (WHERE code = 'publico')   AS publico,
        MAX(value_text) FILTER (WHERE code = 'licencia')  AS licencia,
        MAX(value_text) FILTER (WHERE code = 'estilo')    AS estilo,
        MAX(value_text) FILTER (WHERE code = 'temporada') AS temporada,
        MAX(value_text) FILTER (WHERE code = 'material')  AS material,
        MAX(CASE WHEN code = 'contenido'
                 THEN trim(to_char(value_num, 'FM999999990.##') || ' ' || COALESCE(unit, ''))
            END)                                           AS contenido,
        MAX(value_num)  FILTER (WHERE code = 'unidades_pack') AS unidades_pack,
        MAX(value_num)  FILTER (WHERE code = 'unidades_caja') AS unidades_caja,
        MAX(value_text) FILTER (WHERE code = 'grupo')     AS grupo
    FROM product_attrs
    GROUP BY company_id, bsale_product_id
) a ON a.company_id = vpf.company_id AND a.bsale_product_id = vpf.bsale_product_id
LEFT JOIN LATERAL (
    SELECT s.name AS proveedor
    FROM product_suppliers ps
    JOIN suppliers s ON s.company_id = ps.company_id AND s.id = ps.supplier_id
    WHERE ps.company_id = vpf.company_id
      AND ps.bsale_product_id = vpf.bsale_product_id
    ORDER BY ps.is_primary DESC, ps.last_purchase_date DESC NULLS LAST, s.name
    LIMIT 1
) sup ON true;

COMMIT;

-- =====================================================================
-- VERIFICACIÓN (correr aparte):
--   SELECT code, source, count(*) FROM product_attrs GROUP BY 1, 2 ORDER BY 1, 2;
--   SELECT marca, count(*) FROM v_products_enriched GROUP BY 1 ORDER BY 2 DESC LIMIT 20;
-- =====================================================================
