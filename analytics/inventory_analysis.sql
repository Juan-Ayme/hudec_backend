-- =============================================================================
-- KAWII | Análisis de Inventario y Rotación de Stock
-- =============================================================================
-- REGLAS DE NEGOCIO:
--   Sucursales de venta activas : ID 1 y 3  (tiendas físicas)
--   Almacén                     : ID 4       (solo abastece, NO genera ventas)
--   Categorías objetivo         : IDs 228, 221, 145
--   Anti-ruido                  : sin inactivos, sin notas de crédito, sin fantasmas
--   Ventana de venta            : últimos 90 días
--   Horizonte de abastecimiento : 30 días
-- IMPORTANTE:
--   NO se incluye ningún campo de costo (variant_costs excluida por completo)
--   pendiente actualización en BSale.
--
-- FIX TIMEZONE (2026-05-06):
--   Todos los filtros de fecha usan AT TIME ZONE 'America/Lima' para que las
--   ventas nocturnas (>19:00 Lima = UTC del dia siguiente) se cuenten en el
--   dia correcto, igual que muestra BSale.
-- =============================================================================


-- ─────────────────────────────────────────────────────────────────────────────
-- QUERY 1 | CTE BASE — usada por las demás consultas
-- Calcula métricas por SKU: venta diaria, meta 30d, días de stock, semáforo.
-- ─────────────────────────────────────────────────────────────────────────────

WITH

-- ── 1. Stock actual en tiendas (IDs 1 y 3) ──────────────────────────────────
stock_tiendas AS (
    SELECT
        sl.bsale_variant_id,
        SUM(sl.quantity_available)  AS stock_tiendas
    FROM stock_levels sl
    WHERE sl.bsale_office_id IN (1, 3)
    GROUP BY sl.bsale_variant_id
),

-- ── 2. Stock en almacén (ID 4) ───────────────────────────────────────────────
stock_almacen AS (
    SELECT
        sl.bsale_variant_id,
        SUM(sl.quantity_available)  AS stock_almacen
    FROM stock_levels sl
    WHERE sl.bsale_office_id = 4
    GROUP BY sl.bsale_variant_id
),

-- ── 3. Ventas reales últimos 90 días (solo tiendas 1 y 3) ───────────────────
--    Excluye: notas de crédito, documentos inactivos, gratuidades
--    FIX: usa AT TIME ZONE 'America/Lima' para que las ventas nocturnas
--         (despues de las 19:00 Lima) no se cuenten en el dia siguiente.
ventas_90d AS (
    SELECT
        dd.bsale_variant_id,
        SUM(dd.quantity)            AS unidades_vendidas_90d
    FROM document_details dd
    JOIN documents        doc ON doc.bsale_document_id = dd.bsale_document_id
    WHERE
        doc.bsale_office_id IN (1, 3)       -- solo tiendas físicas
        AND doc.is_credit_note = FALSE       -- sin notas de crédito
        AND doc.is_active      = TRUE        -- sin documentos cancelados
        AND dd.is_gratuity     = FALSE       -- sin gratuidades (no representan venta real)
        AND (doc.emission_date AT TIME ZONE 'America/Lima')::date >= CURRENT_DATE - INTERVAL '90 days'
    GROUP BY dd.bsale_variant_id
),

-- ── 4. Ensamble principal por SKU ────────────────────────────────────────────
base AS (
    SELECT
        -- Identificadores
        v.bsale_variant_id                                          AS variant_id,
        v.display_code                                              AS sku,
        v.description                                               AS variante,
        p.bsale_product_id                                          AS product_id,
        p.name                                                      AS producto,

        -- Taxonomía
        sc.id                                                       AS subcategoria_id,
        sc.name                                                     AS subcategoria,
        cat.id                                                      AS categoria_id,
        cat.name                                                    AS categoria,
        dep.name                                                    AS departamento,

        -- Stock
        COALESCE(st.stock_tiendas, 0)                               AS stock_tiendas,
        COALESCE(sa.stock_almacen, 0)                               AS stock_almacen,
        COALESCE(st.stock_tiendas, 0) + COALESCE(sa.stock_almacen, 0)
                                                                    AS stock_total,

        -- Ventas 90d y velocidad diaria
        COALESCE(vt.unidades_vendidas_90d, 0)                       AS unidades_vendidas_90d,
        ROUND(COALESCE(vt.unidades_vendidas_90d, 0) / 90.0, 4)      AS venta_diaria_real,

        -- Meta de abastecimiento 30 días
        ROUND((COALESCE(vt.unidades_vendidas_90d, 0) / 90.0) * 30, 2)
                                                                    AS meta_mensual_30d,

        -- Días de stock restante (usando solo stock en tiendas)
        CASE
            WHEN COALESCE(vt.unidades_vendidas_90d, 0) = 0 THEN NULL   -- sin ventas → no calculable
            ELSE ROUND(
                COALESCE(st.stock_tiendas, 0)
                / (COALESCE(vt.unidades_vendidas_90d, 0) / 90.0),
                1
            )
        END                                                         AS dias_stock_restante

    FROM variants v
    JOIN products             p   ON p.bsale_product_id    = v.bsale_product_id
    -- Taxonomía (subcategory es el nexo entre producto/variant y categoría)
    LEFT JOIN subcategories   sc  ON sc.id                 = p.subcategory_id
    LEFT JOIN categories      cat ON cat.id                = sc.category_id
    LEFT JOIN departments     dep ON dep.id                = cat.department_id
    -- Stock
    LEFT JOIN stock_tiendas   st  ON st.bsale_variant_id   = v.bsale_variant_id
    LEFT JOIN stock_almacen   sa  ON sa.bsale_variant_id   = v.bsale_variant_id
    -- Ventas
    LEFT JOIN ventas_90d      vt  ON vt.bsale_variant_id   = v.bsale_variant_id

    WHERE
        v.is_active  = TRUE          -- sin variantes inactivas
        AND p.is_active = TRUE       -- sin productos inactivos
        -- Filtro de categorías objetivo
        AND cat.id IN (228, 221, 145)
        -- Anti-fantasma: al menos stock > 0 O ventas históricas > 0
        AND (
            COALESCE(st.stock_tiendas, 0) > 0
            OR COALESCE(sa.stock_almacen, 0) > 0
            OR COALESCE(vt.unidades_vendidas_90d, 0) > 0
        )
)

-- ── 5. SELECT FINAL con semáforo ─────────────────────────────────────────────
SELECT
    variant_id,
    sku,
    variante,
    product_id,
    producto,
    subcategoria_id,
    subcategoria,
    categoria_id,
    categoria,
    departamento,

    stock_tiendas,
    stock_almacen,
    stock_total,

    unidades_vendidas_90d,
    venta_diaria_real,
    meta_mensual_30d,
    dias_stock_restante,

    -- ── SEMÁFORO DE ROTACIÓN ──────────────────────────────────────────────────
    CASE
        -- Baja rotación: vende menos de 10 unidades/mes (< 0.333/día)
        WHEN venta_diaria_real < (10.0 / 30)
            THEN 'BAJA_ROTACION'

        -- Stock crítico pero baja rotación: casi sin stock Y vende poco
        WHEN dias_stock_restante IS NOT NULL
             AND dias_stock_restante < 30
             AND venta_diaria_real < (10.0 / 30)
            THEN 'CRITICO_BAJA_ROTACION'

        -- Alta rotación: vende bien Y se agotará en menos de 30 días
        WHEN dias_stock_restante IS NOT NULL
             AND dias_stock_restante < 30
             AND venta_diaria_real >= (10.0 / 30)
            THEN 'ALTA_ROTACION'

        -- Media rotación: stock para 30 a 45 días
        WHEN dias_stock_restante IS NOT NULL
             AND dias_stock_restante BETWEEN 30 AND 45
            THEN 'MEDIA_ROTACION'

        -- Inventario sano: stock para más de 45 días
        WHEN dias_stock_restante IS NOT NULL
             AND dias_stock_restante > 45
            THEN 'INVENTARIO_SANO'

        -- Sin ventas históricas → no clasificable
        ELSE 'SIN_MOVIMIENTO'
    END                                                             AS semaforo

FROM base
ORDER BY
    categoria,
    subcategoria,
    producto,
    sku;


-- =============================================================================
-- QUERY 2 | RESUMEN POR CATEGORÍA
-- Vista ejecutiva: cuántos SKUs están en cada semáforo por categoría.
-- =============================================================================

WITH
stock_tiendas AS (
    SELECT bsale_variant_id, SUM(quantity_available) AS stock_tiendas
    FROM stock_levels WHERE bsale_office_id IN (1, 3)
    GROUP BY bsale_variant_id
),
stock_almacen AS (
    SELECT bsale_variant_id, SUM(quantity_available) AS stock_almacen
    FROM stock_levels WHERE bsale_office_id = 4
    GROUP BY bsale_variant_id
),
ventas_90d AS (
    SELECT dd.bsale_variant_id, SUM(dd.quantity) AS unidades_vendidas_90d
    FROM document_details dd
    JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id
    WHERE doc.bsale_office_id IN (1, 3)
      AND doc.is_credit_note = FALSE
      AND doc.is_active      = TRUE
      AND dd.is_gratuity     = FALSE
      AND (doc.emission_date AT TIME ZONE 'America/Lima')::date >= CURRENT_DATE - INTERVAL '90 days'
    GROUP BY dd.bsale_variant_id
),
clasificados AS (
    SELECT
        cat.id   AS categoria_id,
        cat.name AS categoria,

        CASE
            WHEN COALESCE(vt.unidades_vendidas_90d, 0) / 90.0 < (10.0 / 30)
                THEN 'BAJA_ROTACION'
            WHEN COALESCE(st.stock_tiendas, 0)
                 / NULLIF(COALESCE(vt.unidades_vendidas_90d, 0) / 90.0, 0) < 30
                 AND COALESCE(vt.unidades_vendidas_90d, 0) / 90.0 < (10.0 / 30)
                THEN 'CRITICO_BAJA_ROTACION'
            WHEN COALESCE(st.stock_tiendas, 0)
                 / NULLIF(COALESCE(vt.unidades_vendidas_90d, 0) / 90.0, 0) < 30
                 AND COALESCE(vt.unidades_vendidas_90d, 0) / 90.0 >= (10.0 / 30)
                THEN 'ALTA_ROTACION'
            WHEN COALESCE(st.stock_tiendas, 0)
                 / NULLIF(COALESCE(vt.unidades_vendidas_90d, 0) / 90.0, 0) BETWEEN 30 AND 45
                THEN 'MEDIA_ROTACION'
            WHEN COALESCE(st.stock_tiendas, 0)
                 / NULLIF(COALESCE(vt.unidades_vendidas_90d, 0) / 90.0, 0) > 45
                THEN 'INVENTARIO_SANO'
            ELSE 'SIN_MOVIMIENTO'
        END AS semaforo,

        COALESCE(st.stock_tiendas, 0)                                   AS stock_tiendas,
        COALESCE(sa.stock_almacen, 0)                                   AS stock_almacen,
        ROUND(COALESCE(vt.unidades_vendidas_90d, 0) / 90.0, 4)         AS venta_diaria_real,
        ROUND((COALESCE(vt.unidades_vendidas_90d, 0) / 90.0) * 30, 2)  AS meta_mensual_30d

    FROM variants v
    JOIN products           p   ON p.bsale_product_id  = v.bsale_product_id
    LEFT JOIN subcategories sc  ON sc.id = p.subcategory_id
    LEFT JOIN categories    cat ON cat.id = sc.category_id
    LEFT JOIN stock_tiendas st  ON st.bsale_variant_id  = v.bsale_variant_id
    LEFT JOIN stock_almacen sa  ON sa.bsale_variant_id  = v.bsale_variant_id
    LEFT JOIN ventas_90d    vt  ON vt.bsale_variant_id  = v.bsale_variant_id
    WHERE
        v.is_active  = TRUE
        AND p.is_active = TRUE
        AND cat.id IN (228, 221, 145)
        AND (
            COALESCE(st.stock_tiendas, 0) > 0
            OR COALESCE(sa.stock_almacen, 0) > 0
            OR COALESCE(vt.unidades_vendidas_90d, 0) > 0
        )
)
SELECT
    categoria_id,
    categoria,
    semaforo,
    COUNT(*)                        AS total_skus,
    ROUND(SUM(stock_tiendas), 0)    AS stock_tiendas_total,
    ROUND(SUM(stock_almacen), 0)    AS stock_almacen_total,
    ROUND(SUM(venta_diaria_real), 2) AS venta_diaria_total,
    ROUND(SUM(meta_mensual_30d), 0) AS meta_mensual_total
FROM clasificados
GROUP BY categoria_id, categoria, semaforo
ORDER BY categoria, semaforo;


-- =============================================================================
-- QUERY 3 | ALERTAS DE REPOSICIÓN URGENTE
-- SKUs con ALTA_ROTACION: necesitan reposición antes de 30 días.
-- Muestra cuántas unidades faltan para cumplir la meta mensual.
-- (Sin ningún campo de costo)
-- =============================================================================

WITH
stock_tiendas AS (
    SELECT bsale_variant_id, SUM(quantity_available) AS stock_tiendas
    FROM stock_levels WHERE bsale_office_id IN (1, 3)
    GROUP BY bsale_variant_id
),
stock_almacen AS (
    SELECT bsale_variant_id, SUM(quantity_available) AS stock_almacen
    FROM stock_levels WHERE bsale_office_id = 4
    GROUP BY bsale_variant_id
),
ventas_90d AS (
    SELECT dd.bsale_variant_id, SUM(dd.quantity) AS unidades_vendidas_90d
    FROM document_details dd
    JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id
    WHERE doc.bsale_office_id IN (1, 3)
      AND doc.is_credit_note = FALSE
      AND doc.is_active      = TRUE
      AND dd.is_gratuity     = FALSE
      AND (doc.emission_date AT TIME ZONE 'America/Lima')::date >= CURRENT_DATE - INTERVAL '90 days'
    GROUP BY dd.bsale_variant_id
)
SELECT
    v.bsale_variant_id                                              AS variant_id,
    v.display_code                                                  AS sku,
    v.description                                                   AS variante,
    p.name                                                          AS producto,
    cat.name                                                        AS categoria,

    ROUND(COALESCE(st.stock_tiendas, 0), 0)                        AS stock_tiendas,
    ROUND(COALESCE(sa.stock_almacen, 0), 0)                        AS stock_almacen,

    ROUND(COALESCE(vt.unidades_vendidas_90d, 0) / 90.0, 4)         AS venta_diaria_real,
    ROUND((COALESCE(vt.unidades_vendidas_90d, 0) / 90.0) * 30, 2) AS meta_mensual_30d,

    ROUND(
        COALESCE(st.stock_tiendas, 0)
        / (COALESCE(vt.unidades_vendidas_90d, 0) / 90.0),
        1
    )                                                               AS dias_stock_restante,

    -- Unidades que faltan para alcanzar la meta mensual (brecha)
    GREATEST(
        0,
        ROUND((COALESCE(vt.unidades_vendidas_90d, 0) / 90.0) * 30, 2)
        - COALESCE(st.stock_tiendas, 0)
    )                                                               AS unidades_faltantes,

    'ALTA_ROTACION'                                                 AS semaforo

FROM variants v
JOIN products           p   ON p.bsale_product_id  = v.bsale_product_id
LEFT JOIN subcategories sc  ON sc.id = p.subcategory_id
LEFT JOIN categories    cat ON cat.id = sc.category_id
LEFT JOIN stock_tiendas st  ON st.bsale_variant_id  = v.bsale_variant_id
LEFT JOIN stock_almacen sa  ON sa.bsale_variant_id  = v.bsale_variant_id
LEFT JOIN ventas_90d    vt  ON vt.bsale_variant_id  = v.bsale_variant_id
WHERE
    v.is_active  = TRUE
    AND p.is_active = TRUE
    AND cat.id IN (228, 221, 145)
    AND COALESCE(vt.unidades_vendidas_90d, 0) > 0
    -- Solo ALTA_ROTACION: vende >= 10/mes Y se agota antes de 30 días
    AND COALESCE(vt.unidades_vendidas_90d, 0) / 90.0 >= (10.0 / 30)
    AND (
        COALESCE(st.stock_tiendas, 0)
        / (COALESCE(vt.unidades_vendidas_90d, 0) / 90.0)
    ) < 30
ORDER BY dias_stock_restante ASC;   -- los más urgentes primero


-- =============================================================================
-- QUERY 4 | PRODUCTOS BAJA ROTACIÓN (candidatos a liquidación / no comprar más)
-- =============================================================================

WITH
stock_tiendas AS (
    SELECT bsale_variant_id, SUM(quantity_available) AS stock_tiendas
    FROM stock_levels WHERE bsale_office_id IN (1, 3)
    GROUP BY bsale_variant_id
),
stock_almacen AS (
    SELECT bsale_variant_id, SUM(quantity_available) AS stock_almacen
    FROM stock_levels WHERE bsale_office_id = 4
    GROUP BY bsale_variant_id
),
ventas_90d AS (
    SELECT dd.bsale_variant_id, SUM(dd.quantity) AS unidades_vendidas_90d
    FROM document_details dd
    JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id
    WHERE doc.bsale_office_id IN (1, 3)
      AND doc.is_credit_note = FALSE
      AND doc.is_active      = TRUE
      AND dd.is_gratuity     = FALSE
      AND (doc.emission_date AT TIME ZONE 'America/Lima')::date >= CURRENT_DATE - INTERVAL '90 days'
    GROUP BY dd.bsale_variant_id
)
SELECT
    v.bsale_variant_id                                              AS variant_id,
    v.display_code                                                  AS sku,
    v.description                                                   AS variante,
    p.name                                                          AS producto,
    cat.name                                                        AS categoria,
    sc.name                                                         AS subcategoria,

    ROUND(COALESCE(st.stock_tiendas, 0), 0)                        AS stock_tiendas,
    ROUND(COALESCE(sa.stock_almacen, 0), 0)                        AS stock_almacen,
    ROUND(COALESCE(st.stock_tiendas, 0) + COALESCE(sa.stock_almacen, 0), 0)
                                                                    AS stock_total,

    COALESCE(vt.unidades_vendidas_90d, 0)                          AS unidades_vendidas_90d,
    ROUND(COALESCE(vt.unidades_vendidas_90d, 0) / 90.0, 4)         AS venta_diaria_real,
    ROUND((COALESCE(vt.unidades_vendidas_90d, 0) / 90.0) * 30, 2) AS meta_mensual_30d,

    'BAJA_ROTACION'                                                 AS semaforo

FROM variants v
JOIN products           p   ON p.bsale_product_id  = v.bsale_product_id
LEFT JOIN subcategories sc  ON sc.id = COALESCE(v.subcategory_id, p.subcategory_id)
LEFT JOIN categories    cat ON cat.id = sc.category_id
LEFT JOIN stock_tiendas st  ON st.bsale_variant_id  = v.bsale_variant_id
LEFT JOIN stock_almacen sa  ON sa.bsale_variant_id  = v.bsale_variant_id
LEFT JOIN ventas_90d    vt  ON vt.bsale_variant_id  = v.bsale_variant_id
WHERE
    v.is_active  = TRUE
    AND p.is_active = TRUE
    AND cat.id IN (228, 221, 145)
    -- Baja rotación: menos de 10 unidades/mes
    AND COALESCE(vt.unidades_vendidas_90d, 0) / 90.0 < (10.0 / 30)
    -- Solo los que tienen stock (sirve para tomar decisión de liquidar)
    AND (
        COALESCE(st.stock_tiendas, 0) > 0
        OR COALESCE(sa.stock_almacen, 0) > 0
    )
ORDER BY stock_total DESC, venta_diaria_real ASC;
