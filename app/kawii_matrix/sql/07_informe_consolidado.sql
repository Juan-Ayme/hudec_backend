-- =============================================================
-- INFORME CONSOLIDADO KAWII - Vista única jerárquica (LIFETIME)
-- =============================================================
-- Cambios vs versión anterior:
--   - Las métricas de análisis (ABC, IC, contribución, velocidad,
--     cobertura, sugerencia de compra) usan TODO el histórico,
--     no solo 60 días.
--   - Las ventanas cortas (15d, 60d) se mantienen SOLO para los
--     flags de "ahora": NUEVO, escondido, lote vendido rápido.
--   - NULLs reemplazados: a nivel macro por "—",
--     a nivel SKU el ABC = el de su subcategoría padre.
--
-- Niveles:
--   ▸ DEPARTAMENTO
--       ▸▸ CATEGORÍA
--           ▸▸▸ SUBCATEGORÍA
--               ▸▸▸▸ SKU
-- =============================================================
WITH params AS (
    SELECT NOW()                              AS ahora,
        NOW() - INTERVAL '15 days'           AS fecha_nuevo,
        NOW() - INTERVAL '60 days'           AS fecha_muerte,
        NOW() - INTERVAL '90 days'           AS fecha_corte,
        CAST(:sucursales_objetivo AS int[])          AS sucursales_objetivo,
        CAST(:tipos_venta AS int[])                  AS tipos_venta,
        CAST(:tipos_devolucion AS int[])             AS tipos_devolucion,
        45::numeric                          AS cobertura_objetivo_dias,
        7                                    AS piso_dias_lote
),
-- ============================================================
-- 1. VENTAS LIFETIME (toda la historia) + ventanas cortas
-- ============================================================
ventas_lifetime AS (
    SELECT d.bsale_office_id, dd.bsale_variant_id,
        SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_venta) THEN dd.quantity ELSE 0 END) -
        SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_devolucion) THEN dd.quantity ELSE 0 END) AS unds_lifetime,
        MIN(d.emission_date::date) FILTER (WHERE d.bsale_document_type_id = ANY(p.tipos_venta)) AS primera_venta,
        MAX(d.emission_date::date) FILTER (WHERE d.bsale_document_type_id = ANY(p.tipos_venta)) AS ultima_venta,
        COUNT(DISTINCT d.emission_date::date) FILTER (WHERE d.bsale_document_type_id = ANY(p.tipos_venta)) AS dias_con_venta,
        COUNT(DISTINCT TO_CHAR(d.emission_date, 'YYYY-MM')) FILTER (WHERE d.bsale_document_type_id = ANY(p.tipos_venta)) AS meses_con_venta
    FROM documents d JOIN document_details dd USING (bsale_document_id) CROSS JOIN params p
    WHERE d.is_active
      AND d.bsale_office_id = ANY(p.sucursales_objetivo)
      AND d.bsale_document_type_id = ANY(p.tipos_venta || p.tipos_devolucion)
    GROUP BY 1, 2
),
ventas_90d AS (
    -- Ventas 90 días (para flags y comparativos recientes)
    SELECT d.bsale_office_id, dd.bsale_variant_id,
        SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_venta) THEN dd.quantity ELSE 0 END) -
        SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_devolucion) THEN dd.quantity ELSE 0 END) AS unds_90d
    FROM documents d JOIN document_details dd USING (bsale_document_id) CROSS JOIN params p
    WHERE d.is_active
      AND d.emission_date >= p.fecha_corte
      AND d.bsale_office_id = ANY(p.sucursales_objetivo)
      AND d.bsale_document_type_id = ANY(p.tipos_venta || p.tipos_devolucion)
    GROUP BY 1, 2
),
stock_sku AS (
    SELECT bsale_office_id, bsale_variant_id, quantity_available AS stock, quantity_reserved AS reservado
    FROM stock_levels sl CROSS JOIN params p
    WHERE sl.bsale_office_id = ANY(p.sucursales_objetivo)
),
recep_sku AS (
    SELECT r.bsale_office_id, rd.bsale_variant_id,
           MIN(r.admission_date) AS primera_recepcion,
           MAX(r.admission_date) AS ultima_recepcion,
           SUM(rd.quantity)      AS unds_recibidas_lifetime,
           MIN(r.admission_date) FILTER (WHERE r.admission_date >= (SELECT fecha_corte FROM params)) AS primera_recep_90d,
           SUM(rd.quantity) FILTER (WHERE r.admission_date >= (SELECT fecha_corte FROM params)) AS unds_recibidas_90d
    FROM receptions r JOIN reception_details rd USING (bsale_reception_id) CROSS JOIN params p
    WHERE r.bsale_office_id = ANY(p.sucursales_objetivo)
    GROUP BY 1, 2
),
-- NUEVO: ventas posteriores a la última recepción (para velocidad del LOTE ACTUAL)
ventas_post_recep AS (
    SELECT d.bsale_office_id, dd.bsale_variant_id,
        SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_venta) THEN dd.quantity ELSE 0 END) -
        SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_devolucion) THEN dd.quantity ELSE 0 END) AS unds_post_recep,
        MAX(d.emission_date::date) FILTER (WHERE d.bsale_document_type_id = ANY(p.tipos_venta)) AS ult_venta_post_recep
    FROM documents d
    JOIN document_details dd USING (bsale_document_id)
    CROSS JOIN params p
    JOIN recep_sku rs ON rs.bsale_office_id = d.bsale_office_id
                     AND rs.bsale_variant_id = dd.bsale_variant_id
    WHERE d.is_active
      AND d.bsale_office_id = ANY(p.sucursales_objetivo)
      AND d.bsale_document_type_id = ANY(p.tipos_venta || p.tipos_devolucion)
      AND d.emission_date >= rs.ultima_recepcion
    GROUP BY 1, 2
),
jerarquia AS (
    SELECT v.bsale_variant_id, v.display_code, p.bsale_product_id, p.name AS product_name,
        d.id AS department_id, d.name AS department,
        c.id AS category_id, c.name AS category,
        s.id AS subcategory_id, s.name AS subcategory
    FROM variants v
    JOIN products p ON v.bsale_product_id = p.bsale_product_id AND p.is_active
    LEFT JOIN product_types pt ON p.bsale_product_type_id = pt.bsale_product_type_id
    LEFT JOIN subcategories s ON s.id = COALESCE(p.subcategory_id, pt.subcategory_id)
    LEFT JOIN categories c ON c.id = s.category_id
    LEFT JOIN departments d ON d.id = c.department_id
    WHERE v.is_active
),
-- ============================================================
-- 2. SKU CON TODOS LOS DATOS COMBINADOS
-- ============================================================
sku_full AS (
    SELECT COALESCE(vl.bsale_office_id, st.bsale_office_id, rs.bsale_office_id) AS bsale_office_id,
        o.name AS sucursal,
        j.bsale_variant_id, j.display_code, j.product_name,
        j.department_id, j.department, j.category_id, j.category,
        j.subcategory_id, j.subcategory,
        COALESCE(vl.unds_lifetime, 0)::numeric  AS unds_lifetime,
        COALESCE(v90.unds_90d, 0)::numeric      AS unds_90d,
        COALESCE(vl.dias_con_venta, 0)          AS dias_con_venta,
        COALESCE(vl.meses_con_venta, 0)         AS meses_con_venta,
        vl.primera_venta, vl.ultima_venta,
        COALESCE(st.stock, 0)::numeric          AS stock,
        COALESCE(st.reservado, 0)::numeric      AS reservado,
        rs.primera_recepcion, rs.ultima_recepcion,
        COALESCE(rs.unds_recibidas_lifetime, 0)::numeric AS unds_recibidas_lifetime,
        rs.primera_recep_90d,
        COALESCE(rs.unds_recibidas_90d, 0)::numeric AS unds_recibidas_90d,
        COALESCE(vpr.unds_post_recep, 0)::numeric AS unds_post_recep,
        vpr.ult_venta_post_recep
    FROM jerarquia j
    LEFT JOIN ventas_lifetime vl ON vl.bsale_variant_id = j.bsale_variant_id
    LEFT JOIN ventas_90d      v90 ON v90.bsale_variant_id = j.bsale_variant_id AND v90.bsale_office_id = vl.bsale_office_id
    LEFT JOIN stock_sku       st ON st.bsale_variant_id = j.bsale_variant_id
                                 AND (vl.bsale_office_id IS NULL OR vl.bsale_office_id = st.bsale_office_id)
    LEFT JOIN recep_sku       rs ON rs.bsale_variant_id = j.bsale_variant_id
                                 AND rs.bsale_office_id = COALESCE(vl.bsale_office_id, st.bsale_office_id)
    LEFT JOIN ventas_post_recep vpr ON vpr.bsale_variant_id = j.bsale_variant_id
                                    AND vpr.bsale_office_id = COALESCE(vl.bsale_office_id, st.bsale_office_id, rs.bsale_office_id)
    LEFT JOIN offices o ON o.bsale_office_id = COALESCE(vl.bsale_office_id, st.bsale_office_id, rs.bsale_office_id)
     WHERE (j.department_id IS NULL OR NOT (j.department_id = ANY(CAST(:excluded_departments AS int[]))))
       AND (j.category_id IS NULL OR NOT (j.category_id = ANY(CAST(:excluded_categories AS int[]))))
      AND COALESCE(vl.bsale_office_id, st.bsale_office_id, rs.bsale_office_id) IS NOT NULL
),
sku_calc AS (
    SELECT s.*, p.cobertura_objetivo_dias, p.piso_dias_lote,
        -- Días desde la última venta (puede ser NULL si nunca vendió)
        CASE WHEN s.ultima_venta IS NOT NULL
             THEN (CURRENT_DATE - s.ultima_venta)
             ELSE NULL END AS dias_sin_venta,
        -- Días desde la última recepción del SKU (regla: un lote debe rotarse en ≤45 días)
        CASE WHEN s.ultima_recepcion IS NOT NULL
             THEN DATE_PART('day', p.ahora - s.ultima_recepcion)::int
             ELSE NULL END AS dias_desde_ultima_recep,
        -- Periodo lifetime (para ABC y contribución):
        GREATEST(1.0,
            CASE
                WHEN s.primera_venta IS NOT NULL
                    THEN (CURRENT_DATE - s.primera_venta)::numeric
                WHEN s.primera_recepcion IS NOT NULL
                    THEN DATE_PART('day', p.ahora - s.primera_recepcion)::numeric
                ELSE 1.0
            END
        ) AS dias_lifetime,
        -- Días del periodo activo: entre primera y última venta
        -- Captura productos pequeños pero exitosos (ej: vendió 42 en 16d = 2.6/d)
        CASE WHEN s.primera_venta IS NOT NULL AND s.ultima_venta IS NOT NULL
             THEN GREATEST(1, (s.ultima_venta - s.primera_venta + 1)::int)
             ELSE NULL END AS dias_periodo_activo,
        -- NUEVO: días del LOTE ACTUAL (para velocidad y sugerencia de compra)
        --   stock=0 con venta post-recep: ult_venta_post_recep - ultima_recepcion (piso 7d)
        --   stock>0 con recepción:        NOW - ultima_recepcion (piso 7d)
        --   sin recepción:                NOW - fecha_corte (piso 7d)
        GREATEST(p.piso_dias_lote::numeric,
            CASE
                WHEN s.stock = 0
                     AND s.ultima_recepcion IS NOT NULL
                     AND s.ult_venta_post_recep IS NOT NULL
                THEN (s.ult_venta_post_recep - s.ultima_recepcion::date)::numeric
                WHEN s.ultima_recepcion IS NOT NULL
                THEN DATE_PART('day', p.ahora - s.ultima_recepcion)::numeric
                ELSE DATE_PART('day', p.ahora - p.fecha_corte)::numeric
            END
        ) AS dias_lote_actual,
        (s.unds_lifetime + s.stock)::numeric AS tdpv_lifetime
    FROM sku_full s CROSS JOIN params p
),
sku_metricas AS (
    SELECT k.*,
        -- Velocidad LIFETIME (para contexto de la historia completa)
        ROUND((k.unds_lifetime / k.dias_lifetime)::numeric, 3) AS velocidad_lifetime,
        -- Velocidad PERIODO ACTIVO (entre primera y última venta).
        -- Captura productos pequeños pero exitosos (ej: M3183 vendió 42 en 16d).
        CASE WHEN k.dias_periodo_activo IS NOT NULL AND k.dias_periodo_activo > 0
             THEN ROUND((k.unds_lifetime / k.dias_periodo_activo)::numeric, 3)
             ELSE NULL END AS velocidad_periodo_activo,
        -- Velocidad del LOTE ACTUAL (desde última recepción)
        ROUND((k.unds_post_recep / k.dias_lote_actual)::numeric, 3) AS velocidad_lote,
        -- Proyección 30d basada en velocidad del LOTE ACTUAL
        ROUND(((k.unds_post_recep / k.dias_lote_actual) * 30)::numeric, 1) AS proy_30,
        -- Cobertura: cuánto durará el stock + reservado al ritmo del LOTE ACTUAL.
        -- Incluye stock_reservado porque es inventario físico (aunque comprometido).
        CASE
            WHEN k.unds_post_recep <= 0 THEN NULL
            WHEN (k.stock + k.reservado) = 0 THEN 0
            ELSE LEAST(9999, CEIL((k.stock + k.reservado) / (k.unds_post_recep / k.dias_lote_actual)))::int
        END AS dias_cob,
        -- Sugerencia de compra (basada en velocidad del LOTE ACTUAL).
        -- NO sugerir comprar si:
        --   - El producto no vende hace ≥60 días (es_muerto: alineado con flag MUERTO)
        --   - El lote actual lleva >45 días sin rotar Y proy baja
        -- ANTES: el threshold era >90 días, dejando 346 productos MUERTOS (DSV 60-90)
        -- con sugerencia de compra >0 — inconsistente con su diagnóstico de muerto.
        CASE
            WHEN k.ultima_venta IS NULL OR (CURRENT_DATE - k.ultima_venta) >= 60 THEN 0
            WHEN k.stock > 0
                 AND k.ultima_recepcion IS NOT NULL
                 AND DATE_PART('day', NOW() - k.ultima_recepcion) > 45
                 AND (k.unds_post_recep / k.dias_lote_actual) * 30 < 10 THEN 0
            ELSE GREATEST(0,
                CEIL(((k.unds_post_recep / k.dias_lote_actual) * k.cobertura_objetivo_dias) - k.stock - k.reservado)
            )::int
        END AS unds_sugeridas_compra,
        -- Velocidad histórica mensual para detectar escondidos
        CASE WHEN k.meses_con_venta > 0 THEN k.unds_lifetime / k.meses_con_venta ELSE 0 END AS vel_mensual_historica,
        -- ¿Es escondido? (stock 1-2, sin venta >15d, pero vendía bien)
        (k.stock BETWEEN 1 AND 2
            AND k.ultima_venta IS NOT NULL
            AND (CURRENT_DATE - k.ultima_venta) > 15
            AND k.meses_con_venta > 0
            AND k.unds_lifetime / k.meses_con_venta >= 10) AS es_escondido,
        -- ¿Es muerto? (sin venta hace 60+ días)
        (k.ultima_venta IS NULL OR (CURRENT_DATE - k.ultima_venta) >= 60) AS es_muerto
    FROM sku_calc k
),
-- ============================================================
-- 3. ROLLUPS POR NIVEL (todos sobre lifetime)
-- ============================================================
subcat_total AS (
    SELECT department_id, department, category_id, category, subcategory_id, subcategory,
           COUNT(*) AS skus_total,
           COUNT(*) FILTER (WHERE NOT es_muerto)                AS skus_activos,
           COUNT(*) FILTER (WHERE stock = 0 AND unds_90d > 0)   AS skus_quiebre,
           COUNT(*) FILTER (WHERE unds_90d = 0 AND stock > 0)   AS skus_parados,
           COUNT(*) FILTER (WHERE es_muerto)                    AS skus_muertos,
           COUNT(*) FILTER (WHERE es_escondido)                 AS skus_escondidos,
           COUNT(*) FILTER (WHERE stock > 0
                              AND ultima_recepcion IS NOT NULL
                              AND DATE_PART('day', NOW() - ultima_recepcion) > 45) AS skus_sin_rotar,
           SUM(unds_lifetime)         AS unds_subcat,
           SUM(stock)                 AS stock_subcat,
           SUM(unds_sugeridas_compra) AS unds_compra_sugerida
    FROM sku_metricas GROUP BY 1, 2, 3, 4, 5, 6
),
subcat_limpia AS (
    -- Para IC: excluir muertos, escondidos y datos corruptos
    SELECT department_id, category_id, subcategory_id,
           SUM(unds_lifetime) AS subcat_unds_limpia,
           SUM(tdpv_lifetime) AS subcat_tdpv_limpia
    FROM sku_metricas
    WHERE NOT es_muerto AND NOT es_escondido AND stock < 100000 AND unds_lifetime < 100000
    GROUP BY 1, 2, 3
),
cat_total AS (
    SELECT department_id, department, category_id, category,
           SUM(skus_total) AS skus_total, SUM(skus_activos) AS skus_activos,
           SUM(skus_quiebre) AS skus_quiebre, SUM(skus_parados) AS skus_parados,
           SUM(skus_muertos) AS skus_muertos, SUM(skus_escondidos) AS skus_escondidos,
           SUM(skus_sin_rotar) AS skus_sin_rotar,
           SUM(unds_subcat) AS unds_cat, SUM(stock_subcat) AS stock_cat,
           SUM(unds_compra_sugerida) AS unds_compra_sugerida
    FROM subcat_total GROUP BY 1, 2, 3, 4
),
dept_total AS (
    SELECT department_id, department,
           SUM(skus_total) AS skus_total, SUM(skus_activos) AS skus_activos,
           SUM(skus_quiebre) AS skus_quiebre, SUM(skus_parados) AS skus_parados,
           SUM(skus_muertos) AS skus_muertos, SUM(skus_escondidos) AS skus_escondidos,
           SUM(skus_sin_rotar) AS skus_sin_rotar,
           SUM(unds_cat) AS unds_dept, SUM(stock_cat) AS stock_dept,
           SUM(unds_compra_sugerida) AS unds_compra_sugerida
    FROM cat_total GROUP BY 1, 2
),
totales AS (SELECT SUM(unds_dept) AS total_unds FROM dept_total),
-- ============================================================
-- 4. ABC POR NIVEL
-- ============================================================
dept_clasif AS (
    SELECT d.*, t.total_unds,
        CASE WHEN t.total_unds > 0 THEN ROUND((d.unds_dept / t.total_unds * 100)::numeric, 1) ELSE 0 END AS pct_padre,
        SUM(d.unds_dept) OVER (ORDER BY d.unds_dept DESC ROWS UNBOUNDED PRECEDING) AS acum,
        CASE
            WHEN t.total_unds > 0 AND
                 (SUM(d.unds_dept) OVER (ORDER BY d.unds_dept DESC ROWS UNBOUNDED PRECEDING) - d.unds_dept) / t.total_unds < 0.80 THEN 'A'
            WHEN t.total_unds > 0 AND
                 (SUM(d.unds_dept) OVER (ORDER BY d.unds_dept DESC ROWS UNBOUNDED PRECEDING) - d.unds_dept) / t.total_unds < 0.95 THEN 'B'
            ELSE 'C'
        END AS abc
    FROM dept_total d CROSS JOIN totales t
),
cat_clasif AS (
    SELECT c.*, dt.unds_dept,
        CASE WHEN dt.unds_dept > 0 THEN ROUND((c.unds_cat / dt.unds_dept * 100)::numeric, 1) ELSE 0 END AS pct_padre,
        SUM(c.unds_cat) OVER (PARTITION BY c.department_id ORDER BY c.unds_cat DESC ROWS UNBOUNDED PRECEDING) AS acum,
        CASE
            WHEN dt.unds_dept > 0 AND
                 (SUM(c.unds_cat) OVER (PARTITION BY c.department_id ORDER BY c.unds_cat DESC ROWS UNBOUNDED PRECEDING) - c.unds_cat) / dt.unds_dept < 0.80 THEN 'A'
            WHEN dt.unds_dept > 0 AND
                 (SUM(c.unds_cat) OVER (PARTITION BY c.department_id ORDER BY c.unds_cat DESC ROWS UNBOUNDED PRECEDING) - c.unds_cat) / dt.unds_dept < 0.95 THEN 'B'
            ELSE 'C'
        END AS abc
    FROM cat_total c JOIN dept_total dt USING (department_id)
),
subcat_clasif AS (
    SELECT s.*, ct.unds_cat,
        CASE WHEN ct.unds_cat > 0 THEN ROUND((s.unds_subcat / ct.unds_cat * 100)::numeric, 1) ELSE 0 END AS pct_padre,
        SUM(s.unds_subcat) OVER (
            PARTITION BY s.department_id, s.category_id ORDER BY s.unds_subcat DESC ROWS UNBOUNDED PRECEDING
        ) AS acum,
        CASE
            WHEN ct.unds_cat > 0 AND
                 (SUM(s.unds_subcat) OVER (PARTITION BY s.department_id, s.category_id ORDER BY s.unds_subcat DESC ROWS UNBOUNDED PRECEDING) - s.unds_subcat) / ct.unds_cat < 0.80 THEN 'A'
            WHEN ct.unds_cat > 0 AND
                 (SUM(s.unds_subcat) OVER (PARTITION BY s.department_id, s.category_id ORDER BY s.unds_subcat DESC ROWS UNBOUNDED PRECEDING) - s.unds_subcat) / ct.unds_cat < 0.95 THEN 'B'
            ELSE 'C'
        END AS abc
    FROM subcat_total s JOIN cat_total ct USING (department_id, category_id)
),
-- ============================================================
-- 5. IC POR SKU (lifetime)
-- ============================================================
sku_ic AS (
    SELECT m.*,
        CASE
            WHEN m.stock >= 100000 OR m.unds_lifetime >= 100000 THEN NULL
            WHEN m.tdpv_lifetime > 0 AND sl.subcat_unds_limpia > 0 AND sl.subcat_tdpv_limpia > 0 THEN
                LEAST(99.99, ROUND((
                    (m.unds_lifetime / sl.subcat_unds_limpia) /
                    NULLIF(m.tdpv_lifetime / sl.subcat_tdpv_limpia, 0)
                )::numeric, 2))
            ELSE NULL
        END AS indice_contribucion,
        sc.abc AS abc_subcat
    FROM sku_metricas m
    LEFT JOIN subcat_limpia  sl USING (department_id, category_id, subcategory_id)
    LEFT JOIN subcat_clasif  sc USING (department_id, category_id, subcategory_id)
)
-- ============================================================
-- 6. UNIÓN FINAL: 4 niveles, sin NULLs molestos
-- ============================================================
SELECT
    "Nivel", "Jerarquía", "Sucursal", "Departamento", "Categoría", "Subcategoría",
    "Código SKU", "Producto",
    "ABC", "% Contrib al Padre",
    "SKUs Total", "Activos", "Quiebre", "Parados", "Muertos", "Escondidos",
    "Stock", "Vendido Lifetime", "Vel/día", "Proy 30d", "Cobertura",
    "IC", "Unds Sugeridas Compra", "Prioridad / Recomendación", "Diagnóstico"
FROM (
-- ───── NIVEL 1: DEPARTAMENTO ─────────────────────────────
SELECT
    'DEPARTAMENTO'                                        AS "Nivel",
    '▸ ' || department                                    AS "Jerarquía",
    '— todas —'::text                                     AS "Sucursal",
    department                                            AS "Departamento",
    '—'::text                                             AS "Categoría",
    '—'::text                                             AS "Subcategoría",
    '—'::text                                             AS "Código SKU",
    '— Total del departamento —'::text                    AS "Producto",
    abc                                                   AS "ABC",
    pct_padre::text                                       AS "% Contrib al Padre",
    skus_total                                            AS "SKUs Total",
    skus_activos                                          AS "Activos",
    skus_quiebre                                          AS "Quiebre",
    skus_parados                                          AS "Parados",
    skus_muertos                                          AS "Muertos",
    skus_escondidos                                       AS "Escondidos",
    trim_scale(ROUND(stock_dept, 2))::text                AS "Stock",
    trim_scale(ROUND(unds_dept, 2))::text                 AS "Vendido Lifetime",
    '—'::text                                             AS "Vel/día",
    '—'::text                                             AS "Proy 30d",
    '—'::text                                             AS "Cobertura",
    '—'::text                                             AS "IC",
    unds_compra_sugerida                                  AS "Unds Sugeridas Compra",
    CASE
        WHEN abc = 'A' AND skus_muertos::numeric / NULLIF(skus_total,0) > 0.40
             THEN '🔍 INVESTIGAR: depto top descomponiéndose'
        WHEN abc = 'A' AND skus_quiebre > skus_activos * 0.10
             THEN '🚀 BUILD: depto estrella con quiebres'
        WHEN abc = 'A' THEN '✅ MANTENER + PROTEGER (depto top)'
        WHEN abc = 'B' AND skus_muertos::numeric / NULLIF(skus_total,0) > 0.50
             THEN '✂️ RACIONALIZAR (depto B con mucho muerto)'
        WHEN abc = 'B' THEN '📊 OPTIMIZAR (depto intermedio)'
        WHEN abc = 'C' AND skus_muertos::numeric / NULLIF(skus_total,0) > 0.50
             THEN '🗑️ DELETE (depto C con mucho muerto)'
        ELSE '🎯 NICHO'
    END                                                   AS "Prioridad / Recomendación",
    'Resumen del departamento'::text                      AS "Diagnóstico",
    department                                            AS _ord_dept,
    '0_total'::text                                       AS _ord_cat,
    '0_total'::text                                       AS _ord_sub,
    '0_total'::text                                       AS _ord_sku
FROM dept_clasif

UNION ALL

-- ───── NIVEL 2: CATEGORÍA ───────────────────────────────
SELECT
    'CATEGORÍA',
    '    ▸▸ ' || category,
    '— todas —', department, category,
    '—', '—',
    '— Total de la categoría —',
    abc, pct_padre::text,
    skus_total, skus_activos, skus_quiebre, skus_parados, skus_muertos, skus_escondidos,
    trim_scale(ROUND(stock_cat, 2))::text,
    trim_scale(ROUND(unds_cat, 2))::text,
    '—', '—', '—', '—',
    unds_compra_sugerida,
    CASE
        WHEN abc = 'A' AND skus_muertos::numeric / NULLIF(skus_total,0) > 0.40
             THEN '🔍 INVESTIGAR: cat top descomponiéndose'
        WHEN abc = 'A' AND skus_quiebre > skus_activos * 0.10
             THEN '🚀 BUILD: cat estrella con quiebres'
        WHEN abc = 'A' THEN '✅ MANTENER + PROTEGER (cat top)'
        WHEN abc = 'B' AND skus_muertos::numeric / NULLIF(skus_total,0) > 0.50
             THEN '✂️ RACIONALIZAR (cat B con mucho muerto)'
        WHEN abc = 'B' THEN '📊 OPTIMIZAR (cat intermedia)'
        WHEN abc = 'C' AND skus_muertos::numeric / NULLIF(skus_total,0) > 0.50
             THEN '🗑️ DELETE (cat C con mucho muerto)'
        ELSE '🎯 NICHO'
    END,
    'Resumen de la categoría',
    department, category, '0_total', '0_total'
FROM cat_clasif

UNION ALL

-- ───── NIVEL 3: SUBCATEGORÍA ────────────────────────────
SELECT
    'SUBCATEGORÍA',
    '        ▸▸▸ ' || subcategory,
    '— todas —', department, category, subcategory,
    '—',
    '— Total de la subcategoría —',
    abc, pct_padre::text,
    skus_total, skus_activos, skus_quiebre, skus_parados, skus_muertos, skus_escondidos,
    trim_scale(ROUND(stock_subcat, 2))::text,
    trim_scale(ROUND(unds_subcat, 2))::text,
    '—', '—', '—', '—',
    unds_compra_sugerida,
    CASE
        WHEN skus_sin_rotar > skus_activos * 0.5 AND skus_activos > 0
             THEN '🐢 ' || skus_sin_rotar || ' SKU(s) con lote sin rotar >45d → LIQUIDAR/PROMOCIONAR'
        WHEN skus_escondidos > 0
             THEN '🥷 ATENCIÓN: ' || skus_escondidos || ' SKU(s) escondidos en zona ciega'
        WHEN abc = 'A' AND skus_muertos::numeric / NULLIF(skus_total,0) > 0.40
             THEN '🔍 INVESTIGAR: subcat top descomponiéndose'
        WHEN abc = 'A' AND skus_quiebre > GREATEST(1, skus_activos * 0.10)
             THEN '🚀 BUILD: subcat estrella con quiebres'
        WHEN abc = 'A' THEN '✅ MANTENER + PROTEGER (subcat top)'
        WHEN abc = 'B' AND skus_muertos::numeric / NULLIF(skus_total,0) > 0.50
             THEN '✂️ RACIONALIZAR (subcat B con mucho muerto)'
        WHEN abc = 'B' THEN '📊 OPTIMIZAR (subcat intermedia)'
        WHEN abc = 'C' AND skus_muertos::numeric / NULLIF(skus_total,0) > 0.50
             THEN '🗑️ DELETE (subcat C con mucho muerto)'
        ELSE '🎯 NICHO'
    END,
    'Resumen de la subcategoría',
    department, category, subcategory, '0_total'
FROM subcat_clasif

UNION ALL

-- ───── NIVEL 4: SKU ─────────────────────────────────────
SELECT
    'SKU',
    '            ▸▸▸▸ ' || product_name,
    sucursal, department, category, subcategory,
    display_code, product_name,
    abc_subcat,                                              -- hereda el ABC de su subcat
    COALESCE(indice_contribucion::text, '—'),                -- usa el IC como "contribución" del SKU
    1                                                  AS "SKUs Total",
    CASE WHEN NOT es_muerto THEN 1 ELSE 0 END          AS "Activos",
    CASE WHEN stock = 0 AND unds_90d > 0 THEN 1 ELSE 0 END  AS "Quiebre",
    CASE WHEN unds_90d = 0 AND stock > 0 THEN 1 ELSE 0 END  AS "Parados",
    CASE WHEN es_muerto THEN 1 ELSE 0 END              AS "Muertos",
    CASE WHEN es_escondido THEN 1 ELSE 0 END           AS "Escondidos",
    trim_scale(ROUND(stock, 2))::text,
    trim_scale(ROUND(unds_lifetime, 2))::text,
    -- Velocidad del LOTE ACTUAL (consistente con Proy 30d para que Vel×30=Proy).
    -- Antes se mostraba velocidad_lifetime (toda la vida) pero proy_30 usa lote actual,
    -- lo que causaba inconsistencia visual en 3171 SKUs (Vel×30 ≠ Proy).
    trim_scale(CASE WHEN velocidad_lote < 0.1
                    THEN ROUND(velocidad_lote, 4)
                    ELSE ROUND(velocidad_lote, 2) END)::text,
    trim_scale(ROUND(proy_30, 1))::text,
    CASE
        WHEN dias_cob IS NULL THEN 's/d'
        WHEN dias_cob = 0     THEN 'Agotado'
        WHEN dias_cob >= 9999 THEN '+999d'
        ELSE dias_cob || 'd' END,
    COALESCE(indice_contribucion::text, '—'),
    unds_sugeridas_compra,
    -- ═══════════════════════════════════════════════════════════════════════════
    -- PRIORIDAD: réplica de las 25 reglas del módulo 04, adaptadas a contexto LIFETIME.
    --   - V90 → unds_90d (ya tenemos los últimos 90d)
    --   - R90 → unds_recibidas_90d
    --   - Métricas lifetime → unds_lifetime, unds_recibidas_lifetime
    --   - Mantiene proy_30, dias_cob, dias_sin_venta del lote actual
    -- ═══════════════════════════════════════════════════════════════════════════
    CASE
        -- 1. NUEVO: edad ≤15d AND volumen lifetime bajo (todavía evaluando)
        WHEN primera_recepcion >= NOW() - INTERVAL '15 days' AND unds_lifetime < 15
             THEN 'P3 🌱 NUEVO: esperar ≥15d para evaluar rotación'

        -- 2. ESCONDIDOS (stock 1-2 + sin venta pero vendía bien históricamente)
        WHEN es_escondido
             THEN 'P3 🥷 FRENTEAR + EVALUAR (escondido en zona ciega)'

        -- 3. QUIEBRE STOCK: Lote vendido rápido (≤30d) — captura recepción reciente exitosa
        WHEN stock = 0 AND unds_recibidas_90d > 0
             AND unds_post_recep >= unds_recibidas_90d * 0.80
             AND primera_recep_90d IS NOT NULL AND ultima_venta IS NOT NULL
             AND (ultima_venta - primera_recep_90d::date) <= 30
             THEN 'P0 🚨 URGENTE: lote vendido ≤30d (reposición inmediata)'

        -- 4. MUERTO 90D con stock: stock parado sin ventas en 90d
        WHEN stock > 0 AND unds_90d = 0 AND unds_lifetime > 0
             THEN 'P5 ❌ LIQUIDAR (stock parado sin ventas 90d, vendió ' || unds_lifetime::int || ' en vida)'

        -- 5. RESIDUO HISTÓRICO: producto antiguo sin rotación Y bajo volumen lifetime.
        -- FIX: agregado unds_lifetime < 50. Antes 75 productos exitosos (SD25167 con
        -- 475 unds, 480401 con 387 unds) caían aquí erróneamente.
        WHEN stock = 0 AND unds_90d = 0
             AND unds_recibidas_90d BETWEEN 1 AND 4
             AND dias_lifetime > 180
             AND unds_lifetime < 50
             THEN 'P5 ❌ RESIDUO HISTÓRICO (antiguo sin rotación → descatalogar)'

        -- 6. RECIBIDO Y NO VENDIDO: solo si NO vendió bien en su vida.
        -- FIX: agregado unds_lifetime < 50 para evitar atrapar productos exitosos
        -- antiguos que recibieron 1-2 unds (mermas/devoluciones tardías).
        WHEN stock = 0 AND unds_90d = 0 AND unds_recibidas_90d > 0
             AND unds_lifetime < 50
             THEN 'P5 ❓ RECIBIDO SIN VENDER (revisar mermas/transferencias)'

        -- 7-9. QUIEBRE Alta Rot/Activa: stock=0 con venta reciente y demanda
        WHEN stock = 0 AND dias_sin_venta <= 14 AND proy_30 >= 30
             THEN 'P0 🚨 URGENTE: quiebre ALTA rotación (proy ≥30/mes)'
        WHEN stock = 0 AND dias_sin_venta <= 14 AND proy_30 >= 10
             THEN 'P1 🔥 URGENTE: quiebre rotación activa (proy 10-29/mes)'

        -- 10. PRODUCTO EXITOSO AGOTADO: vendió ≥70% lifetime con ciclo largo
        WHEN stock = 0
             AND unds_recibidas_lifetime >= 50
             AND unds_lifetime >= unds_recibidas_lifetime * 0.70
             AND ultima_venta IS NOT NULL AND ultima_recepcion IS NOT NULL
             AND (ultima_venta - ultima_recepcion::date) > 90
             THEN 'P1 💎 EXITOSO AGOTADO: vendió ' ||
                  ROUND(unds_lifetime / NULLIF(unds_recibidas_lifetime, 0) * 100)::text ||
                  '% lifetime, ciclo largo (reponer)'

        -- 10b. EXITOSO PEQUEÑO: volumen lifetime <50 pero ST≥80% Y velocidad activa ≥1/día.
        --      Captura M3183 (42 unds en 16d = 2.5/día - excelente velocidad).
        --      Si vendía ≥1/día cuando tenía stock Y agotó 80%+ → es un éxito demostrable.
        WHEN stock = 0
             AND unds_recibidas_lifetime >= 20
             AND unds_lifetime >= unds_recibidas_lifetime * 0.80
             AND velocidad_periodo_activo >= 1.0
             AND ultima_venta IS NOT NULL
             THEN 'P1 💎 EXITOSO PEQUEÑO: ' || unds_lifetime::int || ' unds en periodo activo (vel ' ||
                  ROUND(velocidad_periodo_activo, 1)::text || '/d) — reponer'

        -- 11. LOTE AGOTADO RÁPIDO: vendió ≥60% del lote en ≤90d con venta reciente
        WHEN stock = 0
             AND ult_venta_post_recep IS NOT NULL
             AND dias_sin_venta <= 30
             AND ultima_recepcion IS NOT NULL
             AND (ult_venta_post_recep - ultima_recepcion::date) <= 90
             AND unds_post_recep >= 3
             AND (
                 (unds_recibidas_90d > 0 AND unds_post_recep >= unds_recibidas_90d * 0.60)
                 OR
                 (unds_recibidas_90d = 0 AND unds_recibidas_lifetime > 0
                  AND unds_post_recep >= unds_recibidas_lifetime * 0.70)
             )
             THEN 'P1 🔥 LOTE AGOTADO RÁPIDO (reponer revisar demanda)'

        -- 12. STOCK PREVIO VENDIDO: vendió MÁS de lo recibido 90d O sell-through alto
        WHEN stock = 0
             AND unds_90d >= 10
             AND unds_recibidas_lifetime > 0
             AND (
                 unds_90d > unds_recibidas_90d
                 OR (unds_recibidas_90d >= 3 AND unds_90d >= unds_recibidas_90d * 0.70 AND unds_90d >= 15)
             )
             AND (
                 unds_lifetime >= unds_recibidas_lifetime * 0.50
                 OR unds_90d >= 30
             )
             THEN 'P2 🔥 STOCK PREVIO VENDIDO (consumió stock viejo, reponer)'

        -- 13. AGOTADO POTENCIAL ACTIVO: lifetime≥50 AND venta reciente en 90d
        WHEN stock = 0 AND dias_sin_venta >= 15
             AND unds_lifetime >= 50 AND unds_90d >= 5
             THEN 'P2 👻 AGOTADO POTENCIAL ACTIVO: vendió ' || unds_lifetime::int ||
                  ' lifetime, aún en demanda (reponer prioridad)'

        -- 14. AGOTADO HISTÓRICO: lifetime≥50 pero demanda decayó
        WHEN stock = 0 AND dias_sin_venta >= 15
             AND unds_lifetime >= 50
             THEN 'P4 💤 AGOTADO HISTÓRICO: vendió ' || unds_lifetime::int ||
                  ' en vida pero demanda decayó (evaluar)'

        -- 15. PRODUCTO EMERGENTE: lifetime<50 pero V90≥15 (corto historial vendiendo)
        WHEN stock = 0 AND dias_sin_venta >= 15
             AND unds_90d >= 15
             THEN 'P3 🌿 EMERGENTE: vendió ' || unds_90d::int ||
                  ' en 90d, corto historial (evaluar reposición)'

        -- 16. AGOTADO MARGINAL: bajo volumen lifetime → descatalogar
        WHEN stock = 0 AND dias_sin_venta >= 15
             THEN 'P5 🪦 AGOTADO MARGINAL: bajo volumen lifetime <50 (descatalogar)'

        -- 17. Stock=0 sin caer en otra categoría
        WHEN stock = 0
             THEN 'P5 ❌ NO COMPRAR (sin stock, sin demanda clara)'

        -- 18. BAJA ROT 45d: lote sin rotar >45d con proy <10
        WHEN stock > 0 AND dias_desde_ultima_recep > 45 AND proy_30 < 10
             THEN 'P5 🐢 BAJA ROT: lote ' || dias_desde_ultima_recep || 'd sin rotar (liquidar/promocionar)'

        -- 19. STOCK CRÍTICO BAJA ROTACIÓN: cob<30 pero proy bajo
        WHEN stock > 0 AND dias_cob IS NOT NULL AND dias_cob < 30 AND proy_30 < 10
             THEN 'P4 ⚠️ STOCK CRÍTICO baja rotación (no urgir)'

        -- 20. BAJA ROTACIÓN: proy <10/mes
        WHEN stock > 0 AND proy_30 < 10
             THEN 'P4 🐢 BAJA ROTACIÓN (proy <10/mes — bajar pedido)'

        -- 21. ALTA ROTACIÓN: proy ≥30 + cobertura <30
        WHEN stock > 0 AND proy_30 >= 30 AND dias_cob IS NOT NULL AND dias_cob < 14
             THEN 'P1 🔥 ALTA ROTACIÓN URGENTE: cob <14d, vol ≥30/mes'
        WHEN stock > 0 AND proy_30 >= 30 AND dias_cob IS NOT NULL AND dias_cob < 30
             THEN 'P2 🔥 ALTA ROTACIÓN: cob <30d, vol ≥30/mes'

        -- 22. ROTACIÓN ACTIVA: proy 10-29 + cobertura <30
        WHEN stock > 0 AND proy_30 >= 10 AND dias_cob IS NOT NULL AND dias_cob < 14
             THEN 'P2 💫 ROTACIÓN ACTIVA URGENTE: cob <14d, vol 10-29/mes'
        WHEN stock > 0 AND proy_30 >= 10 AND dias_cob IS NOT NULL AND dias_cob < 30
             THEN 'P3 💫 ROTACIÓN ACTIVA: cob <30d, vol 10-29/mes'

        -- 23. MEDIA ROTACIÓN: cob 30-45
        WHEN stock > 0 AND proy_30 >= 10 AND dias_cob BETWEEN 30 AND 45
             THEN 'P4 ⚡ MEDIA ROTACIÓN (cob 30-45d, mantener pedido)'

        -- 24. INVENTARIO SANO: cob 46-90
        WHEN stock > 0 AND proy_30 >= 10 AND dias_cob BETWEEN 46 AND 90
             THEN 'P4 🟢 INVENTARIO SANO (cob 46-90d, ritmo normal)'

        -- 25. EXCESO INVENTARIO: cob >90
        WHEN stock > 0 AND proy_30 >= 10 AND dias_cob > 90
             THEN 'P5 🧊 EXCESO INVENTARIO (cob >90d, capital estancado)'

        ELSE 'P5 ❌ NO COMPRAR (caso no clasificado)'
    END,
    -- ═══════════════════════════════════════════════════════════════════════════
    -- DIAGNÓSTICO: 25 reglas del módulo 04 + insights de IC (lifetime).
    -- Combina diagnóstico operativo (qué hacer) con análisis de contribución (IC).
    -- ═══════════════════════════════════════════════════════════════════════════
    CASE
        -- 1. NUEVO
        WHEN primera_recepcion >= NOW() - INTERVAL '15 days' AND unds_lifetime < 15
             THEN '🌱 NUEVO: esperando ≥15d para evaluar rotación'

        -- 2. ESCONDIDO
        WHEN es_escondido
             THEN '🥷 ESCONDIDO ALTO POTENCIAL: frentear YA (vel hist ' || ROUND(vel_mensual_historica, 0) || '/mes)'

        -- 3. QUIEBRE STOCK Lote vendido rápido
        WHEN stock = 0 AND unds_recibidas_90d > 0
             AND unds_post_recep >= unds_recibidas_90d * 0.80
             AND primera_recep_90d IS NOT NULL AND ultima_venta IS NOT NULL
             AND (ultima_venta - primera_recep_90d::date) <= 30
             THEN '🚨 QUIEBRE STOCK: lote vendido en ≤30d (' || unds_post_recep::int || ' unds)'

        -- 4. MUERTO 90D con stock parado
        WHEN stock > 0 AND unds_90d = 0 AND unds_lifetime > 0
             THEN '💀 MUERTO 90D: stock parado, vendió ' || unds_lifetime::int || ' en vida'

        -- 5. RESIDUO HISTÓRICO (con threshold de volumen lifetime)
        WHEN stock = 0 AND unds_90d = 0
             AND unds_recibidas_90d BETWEEN 1 AND 4
             AND dias_lifetime > 180
             AND unds_lifetime < 50
             THEN '🪦 RESIDUO HISTÓRICO: producto antiguo sin rotación'

        -- 6. RECIBIDO Y NO VENDIDO (con threshold lifetime)
        WHEN stock = 0 AND unds_90d = 0 AND unds_recibidas_90d > 0
             AND unds_lifetime < 50
             THEN '❓ RECIBIDO Y NO VENDIDO: revisar mermas/transferencias'

        -- 7-9. QUIEBRE Alta/Activa Rot
        WHEN stock = 0 AND dias_sin_venta <= 14 AND proy_30 >= 30
             THEN '🚨 QUIEBRE ALTA ROTACIÓN: agotado con demanda alta (proy ' || ROUND(proy_30) || '/mes)'
        WHEN stock = 0 AND dias_sin_venta <= 14 AND proy_30 >= 10
             THEN '🚨 QUIEBRE rotación activa: agotado vendiendo (proy ' || ROUND(proy_30) || '/mes)'

        -- 10. PRODUCTO EXITOSO AGOTADO
        WHEN stock = 0
             AND unds_recibidas_lifetime >= 50
             AND unds_lifetime >= unds_recibidas_lifetime * 0.70
             AND ultima_venta IS NOT NULL AND ultima_recepcion IS NOT NULL
             AND (ultima_venta - ultima_recepcion::date) > 90
             THEN '💎 PRODUCTO EXITOSO AGOTADO: vendió ' ||
                  ROUND(unds_lifetime / NULLIF(unds_recibidas_lifetime, 0) * 100)::text ||
                  '% lifetime (' || unds_lifetime::int || ' unds)'

        -- 10b. EXITOSO PEQUEÑO (volumen <50 pero velocidad activa >1/día)
        WHEN stock = 0
             AND unds_recibidas_lifetime >= 20
             AND unds_lifetime >= unds_recibidas_lifetime * 0.80
             AND velocidad_periodo_activo >= 1.0
             AND ultima_venta IS NOT NULL
             THEN '💎 EXITOSO PEQUEÑO: vendió ' || unds_lifetime::int || ' unds (vel ' ||
                  ROUND(velocidad_periodo_activo, 1)::text || '/d cuando tenía stock) — reponer'

        -- 11. LOTE AGOTADO RÁPIDO
        WHEN stock = 0
             AND ult_venta_post_recep IS NOT NULL
             AND dias_sin_venta <= 30
             AND ultima_recepcion IS NOT NULL
             AND (ult_venta_post_recep - ultima_recepcion::date) <= 90
             AND unds_post_recep >= 3
             AND (
                 (unds_recibidas_90d > 0 AND unds_post_recep >= unds_recibidas_90d * 0.60)
                 OR
                 (unds_recibidas_90d = 0 AND unds_recibidas_lifetime > 0
                  AND unds_post_recep >= unds_recibidas_lifetime * 0.70)
             )
             THEN '🔥 LOTE AGOTADO RÁPIDO: candidato a reposición'

        -- 12. STOCK PREVIO VENDIDO
        WHEN stock = 0
             AND unds_90d >= 10
             AND unds_recibidas_lifetime > 0
             AND (
                 unds_90d > unds_recibidas_90d
                 OR (unds_recibidas_90d >= 3 AND unds_90d >= unds_recibidas_90d * 0.70 AND unds_90d >= 15)
             )
             AND (
                 unds_lifetime >= unds_recibidas_lifetime * 0.50
                 OR unds_90d >= 30
             )
             THEN '🔥 STOCK PREVIO VENDIDO: consumió stock viejo, vendió ' || unds_90d::int || ' en 90d'

        -- 13. AGOTADO POTENCIAL ACTIVO
        WHEN stock = 0 AND dias_sin_venta >= 15
             AND unds_lifetime >= 50 AND unds_90d >= 5
             THEN '👻 AGOTADO POTENCIAL ACTIVO: vendió ' || unds_lifetime::int || ' lifetime, aún en demanda'

        -- 14. AGOTADO HISTÓRICO
        WHEN stock = 0 AND dias_sin_venta >= 15
             AND unds_lifetime >= 50
             THEN '💤 AGOTADO HISTÓRICO: vendió ' || unds_lifetime::int || ' lifetime pero demanda decayó'

        -- 15. PRODUCTO EMERGENTE
        WHEN stock = 0 AND dias_sin_venta >= 15
             AND unds_90d >= 15
             THEN '🌿 EMERGENTE: vendió ' || unds_90d::int || ' en 90d, corto historial lifetime'

        -- 16. AGOTADO MARGINAL
        WHEN stock = 0 AND dias_sin_venta >= 15
             THEN '🪦 AGOTADO MARGINAL: bajo volumen lifetime (<50 unds)'

        -- 17. Stock=0 sin caer en otra categoría
        WHEN stock = 0
             THEN '👻 FALSO AGOTADO: baja rotación + sin stock'

        -- 18. BAJA ROT 45d
        WHEN stock > 0 AND dias_desde_ultima_recep > 45 AND proy_30 < 10
             THEN '🐢 BAJA ROT (lote sin rotar ' || dias_desde_ultima_recep || 'd) → liquidar/promocionar'

        -- 19. STOCK CRÍTICO baja rotación
        WHEN stock > 0 AND dias_cob IS NOT NULL AND dias_cob < 30 AND proy_30 < 10
             THEN '⚠️ STOCK CRÍTICO pero baja rotación (no urgir)'

        -- 20. BAJA ROTACIÓN
        WHEN stock > 0 AND proy_30 < 10
             THEN '🐢 BAJA ROTACIÓN: proy <10/mes — bajar pedido'

        -- 21. ALTA ROTACIÓN
        WHEN stock > 0 AND proy_30 >= 30 AND dias_cob IS NOT NULL AND dias_cob < 30
             THEN '🔥 ALTA ROTACIÓN (proy ' || ROUND(proy_30) || '/mes, cob ' || dias_cob || 'd)'

        -- 22. ROTACIÓN ACTIVA
        WHEN stock > 0 AND proy_30 >= 10 AND dias_cob IS NOT NULL AND dias_cob < 30
             THEN '💫 ROTACIÓN ACTIVA (proy ' || ROUND(proy_30) || '/mes, cob ' || dias_cob || 'd)'

        -- 23. MEDIA ROTACIÓN
        WHEN stock > 0 AND proy_30 >= 10 AND dias_cob BETWEEN 30 AND 45
             THEN '⚡ MEDIA ROTACIÓN (cob ' || dias_cob || 'd — mantener pedido)'

        -- 24. INVENTARIO SANO
        WHEN stock > 0 AND proy_30 >= 10 AND dias_cob BETWEEN 46 AND 90
             THEN '🟢 INVENTARIO SANO (cob ' || dias_cob || 'd — ritmo normal)'

        -- 25. EXCESO INVENTARIO
        WHEN stock > 0 AND proy_30 >= 10 AND dias_cob > 90
             THEN '🧊 EXCESO INVENTARIO (cob ' || dias_cob || 'd — capital estancado)'

        -- Insights de IC (sólo si no entró a alguna regla operativa)
        WHEN indice_contribucion >= 1.5 AND abc_subcat = 'C'
             THEN '💎 JOYA OCULTA: campeón en subcat baja → mantener'
        WHEN indice_contribucion >= 1.5 AND abc_subcat = 'A'
             THEN '🏆 CAMPEÓN: top de subcat top → invertir más'
        WHEN indice_contribucion >= 1.0
             THEN '⭐ DESTACADO (IC > 1)'
        WHEN indice_contribucion < 0.3 AND stock > 0 AND abc_subcat = 'A'
             THEN '🐀 PARÁSITO en subcat top: ocupa espacio sin retornar'
        WHEN indice_contribucion < 0.5 AND stock > 0
             THEN '📉 BAJA CONTRIBUCIÓN: revisar reducir espacio'
        WHEN indice_contribucion IS NOT NULL
             THEN '➡️ NORMAL (IC 0.5 - 1.5)'
        ELSE '❔ Sin datos'
    END,
    department, category, subcategory, product_name
FROM sku_ic

) jerarquia
ORDER BY _ord_dept, _ord_cat, _ord_sub, _ord_sku;
