-- =====================================================================
-- KAWII — precio de venta real (listas de precio BSale) + costo POR SUCURSAL
-- =====================================================================
-- Resuelve dos huecos del modelo:
--
--   1. COSTO POR SUCURSAL. Hoy `variant_costs` guarda UN costo global por
--      (empresa, variante). Pero el costo real difiere por sucursal (ej.
--      PL-01: 0.80 en oficina 1 vs 2.90 en oficina 3) y ese promedio global
--      puede superar el precio de una sucursal → margen aparente negativo.
--      Se agrega `variant_costs_by_office` derivada de reception_details
--      (que sí tiene bsale_office_id). `variant_costs` NO se toca: queda
--      como fallback global.
--
--   2. PRECIO DE VENTA. BSale nunca se sincronizaba: el "precio" se infería
--      de boletas vendidas. Se agregan `price_lists` (cabecera) y
--      `price_list_details` (precio por variante), espejando
--      /price_lists.json y /price_lists/{id}/details.json. Cada sucursal se
--      liga a su lista vía offices.default_price_list_id (campo
--      `defaultPriceList` de /offices.json).
--
-- La vista `v_variant_price_cost` une todo: precio + costo (por oficina, con
-- fallback al global) + margen, por (empresa, sucursal, variante).
--
-- Multi-tenant: company_id + RLS con el MISMO patrón que las demás tablas
-- (ver 2026-07-01_row_level_security.sql y 2026-07-11_user_offices.sql).
-- Depende de la función current_company_id().
--
-- Idempotente: CREATE ... IF NOT EXISTS + DROP POLICY IF EXISTS +
-- CREATE OR REPLACE VIEW + ADD COLUMN IF NOT EXISTS. Re-ejecutable.
--
-- Aplicar (NO como parte de este cambio de código):
--   psql "$DATABASE_URL" -f docs/migrations/2026-07-13_price_and_office_costs.sql
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 1. COSTO POR SUCURSAL
-- ---------------------------------------------------------------------
-- Sin FK a offices a propósito (mismo criterio de desacople que las tablas
-- de detalle): se puebla desde recepciones y no queremos acoplar el orden
-- de fases del sync. El aislamiento por empresa lo da company_id + RLS.
CREATE TABLE IF NOT EXISTS variant_costs_by_office (
    company_id       integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_office_id  integer NOT NULL,
    bsale_variant_id integer NOT NULL,
    average_cost     numeric(20,4) NOT NULL DEFAULT 0,
    latest_cost      numeric(20,4) NOT NULL DEFAULT 0,
    effective_cost   numeric(20,4) NOT NULL DEFAULT 0,
    cost_source      varchar(20)   NOT NULL DEFAULT 'NONE',
    synced_at        timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_office_id, bsale_variant_id)
);

-- Búsqueda por variante (join con la vista y reportes cross-sucursal).
CREATE INDEX IF NOT EXISTS variant_costs_by_office_variant_idx
    ON variant_costs_by_office (company_id, bsale_variant_id);

-- ---------------------------------------------------------------------
-- 2. LISTAS DE PRECIO (cabecera)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_lists (
    company_id          integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_price_list_id integer NOT NULL,
    name                varchar(200) NOT NULL,
    description         text,
    coin_id             integer,
    is_active           boolean NOT NULL DEFAULT true,
    synced_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_price_list_id)
);

-- ---------------------------------------------------------------------
-- 3. DETALLE DE LISTA: precio por variante
-- ---------------------------------------------------------------------
-- Sin FK a variants a propósito (mismo criterio que document_details): un
-- detalle puede referenciar una variante aún no espejada; no acoplamos el
-- orden de sync.
CREATE TABLE IF NOT EXISTS price_list_details (
    company_id          integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    bsale_price_list_id integer NOT NULL,
    bsale_variant_id    integer NOT NULL,
    net_value           numeric(20,4) NOT NULL DEFAULT 0,
    value_with_taxes    numeric(20,4) NOT NULL DEFAULT 0,
    synced_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, bsale_price_list_id, bsale_variant_id)
);

-- Búsqueda por variante (join con variant_costs y con la vista).
CREATE INDEX IF NOT EXISTS price_list_details_variant_idx
    ON price_list_details (company_id, bsale_variant_id);

-- ---------------------------------------------------------------------
-- 4. Vínculo sucursal → lista por defecto
-- ---------------------------------------------------------------------
-- Columna simple (sin FK a price_lists) para no acoplar el orden de fases:
-- offices se sincroniza antes que price_lists dentro del mismo run.
ALTER TABLE offices ADD COLUMN IF NOT EXISTS default_price_list_id integer;

-- ---------------------------------------------------------------------
-- Row-Level Security (mismo patrón que las demás tablas tenant)
-- ---------------------------------------------------------------------
ALTER TABLE variant_costs_by_office ENABLE ROW LEVEL SECURITY;
ALTER TABLE variant_costs_by_office FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON variant_costs_by_office;
CREATE POLICY tenant_isolation ON variant_costs_by_office
    USING (current_company_id() IS NULL OR company_id = current_company_id())
    WITH CHECK (company_id = current_company_id());

ALTER TABLE price_lists ENABLE ROW LEVEL SECURITY;
ALTER TABLE price_lists FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON price_lists;
CREATE POLICY tenant_isolation ON price_lists
    USING (current_company_id() IS NULL OR company_id = current_company_id())
    WITH CHECK (company_id = current_company_id());

ALTER TABLE price_list_details ENABLE ROW LEVEL SECURITY;
ALTER TABLE price_list_details FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON price_list_details;
CREATE POLICY tenant_isolation ON price_list_details
    USING (current_company_id() IS NULL OR company_id = current_company_id())
    WITH CHECK (company_id = current_company_id());

-- ---------------------------------------------------------------------
-- 5. Vista: precio de venta + costo (oficina → fallback global) + margen
-- ---------------------------------------------------------------------
-- Una fila por (empresa, sucursal, variante presente en la lista de esa
-- sucursal). El costo prioriza el de la oficina; si esa oficina no tiene
-- costo derivado de recepciones, cae al costo global de variant_costs.
-- Las vistas no soportan RLS propia: heredan el filtrado por empresa de
-- las tablas base vía el GUC app.current_company (lo setea db.get_conn()).
CREATE OR REPLACE VIEW v_variant_price_cost AS
SELECT
    o.company_id,
    o.bsale_office_id,
    pld.bsale_variant_id,
    o.default_price_list_id                             AS bsale_price_list_id,
    pld.net_value                                       AS precio_venta,
    pld.value_with_taxes                                AS precio_con_impuestos,
    COALESCE(vco.effective_cost, vc.effective_cost)     AS costo,
    CASE WHEN COALESCE(vco.effective_cost, 0) > 0
         THEN 'OFICINA' ELSE 'GLOBAL' END               AS costo_origen,
    (pld.net_value - COALESCE(vco.effective_cost, vc.effective_cost, 0)) AS margen,
    CASE WHEN pld.net_value > 0
         THEN ROUND((pld.net_value - COALESCE(vco.effective_cost, vc.effective_cost, 0))
                    / pld.net_value * 100, 2)
         ELSE NULL
    END                                                 AS margen_pct
FROM offices o
JOIN price_list_details pld
      ON pld.company_id          = o.company_id
     AND pld.bsale_price_list_id = o.default_price_list_id
LEFT JOIN variant_costs_by_office vco
      ON vco.company_id       = o.company_id
     AND vco.bsale_office_id  = o.bsale_office_id
     AND vco.bsale_variant_id = pld.bsale_variant_id
LEFT JOIN variant_costs vc
      ON vc.company_id       = pld.company_id
     AND vc.bsale_variant_id = pld.bsale_variant_id;

COMMIT;

-- =====================================================================
-- VERIFICACIÓN (correr aparte si querés, con app.current_company seteado):
--   SELECT count(*) FROM price_lists;
--   SELECT count(*) FROM price_list_details;
--   SELECT count(*) FROM variant_costs_by_office;
--   SELECT bsale_office_id, default_price_list_id FROM offices;
--   -- PL-01 (variante 2819): oficina 1 costo 0.80, oficina 3 costo 2.90
--   SELECT * FROM v_variant_price_cost WHERE bsale_variant_id = 2819
--     ORDER BY bsale_office_id;
-- =====================================================================
