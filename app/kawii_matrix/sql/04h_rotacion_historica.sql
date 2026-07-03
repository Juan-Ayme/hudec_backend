-- =============================================================
-- ROTACIÓN HISTÓRICA — Ventana arbitraria con clasificación adaptada
-- =============================================================
-- Versión "histórica" del módulo 04b. Diferencias clave:
--
--   • Ventana parametrizable (:fecha_from, :fecha_to) en lugar del INTERVAL
--     '90 days' hardcoded. Sirve para responder "qué se vendió en 2024", "Top
--     productos del Q4", "alta rotación en abril-junio del año pasado", etc.
--
--   • La cascada de 38 reglas del 04b NO se reusa porque depende del PRESENTE
--     (stock actual, días sin venta vs HOY, alertas de quiebre). Para una
--     ventana histórica esas reglas no aplican —no sabemos qué stock había en
--     marzo-2024. En su lugar usamos una cascada simple basada SOLO en lo que
--     pasó dentro de la ventana: Pareto ABC + frecuencia + tendencia intra-ventana.
--
--   • Sí reusamos la estructura jerárquica del 04b: totales en S/ por
--     Subcat/Cat/Depto + % participación (window functions).
--
-- IMPORTANTE: las exclusiones de departamentos/categorías sí se respetan.
-- =============================================================
WITH params AS (
    SELECT
        CAST(:fecha_from AS DATE)              AS fecha_from,
        CAST(:fecha_to   AS DATE)              AS fecha_to,
        CAST(:sucursales_objetivo AS int[])    AS sucursales_objetivo,
        CAST(:tipos_venta         AS int[])    AS tipos_venta,
        CAST(:tipos_devolucion    AS int[])    AS tipos_devolucion,
        -- Días inclusivos de la ventana (para velocidad uds/día y % frecuencia)
        (CAST(:fecha_to AS DATE) - CAST(:fecha_from AS DATE) + 1) AS dias_ventana
),
-- 1) Ventas diarias por (oficina, variante, fecha) dentro de la ventana.
ventas_diarias AS (
    SELECT
        d.bsale_office_id,
        dd.bsale_variant_id,
        DATE(d.emission_date) AS fecha,
        SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_venta)      THEN dd.quantity     ELSE 0 END) AS qty_venta,
        SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_devolucion) THEN dd.quantity     ELSE 0 END) AS qty_devol,
        SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_venta)      THEN dd.total_amount ELSE 0 END) AS monto_venta,
        SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_devolucion) THEN dd.total_amount ELSE 0 END) AS monto_devol
    FROM documents d
    JOIN document_details dd USING (bsale_document_id)
    CROSS JOIN params p
    WHERE d.is_active
      AND DATE(d.emission_date) BETWEEN p.fecha_from AND p.fecha_to
      AND d.bsale_office_id = ANY(p.sucursales_objetivo)
      AND d.bsale_document_type_id = ANY(p.tipos_venta || p.tipos_devolucion)
    GROUP BY 1, 2, 3
),
-- 2) Agregado por variante: totales en la ventana + primera/última venta.
ventas_total AS (
    SELECT
        bsale_office_id,
        bsale_variant_id,
        SUM(qty_venta - qty_devol)                                     AS unds_vendidas,
        SUM(monto_venta - monto_devol)                                 AS monto_vendido,
        COUNT(DISTINCT fecha) FILTER (WHERE qty_venta - qty_devol > 0) AS dias_con_venta,
        MIN(fecha)            FILTER (WHERE qty_venta - qty_devol > 0) AS primera_venta_ventana,
        MAX(fecha)            FILTER (WHERE qty_venta - qty_devol > 0) AS ultima_venta_ventana
    FROM ventas_diarias
    GROUP BY 1, 2
    HAVING SUM(qty_venta - qty_devol) > 0   -- solo SKUs que vendieron algo
),
-- 3) Tendencia intra-ventana: dividimos la ventana en dos mitades y
--    comparamos unidades. Sirve para detectar productos que subieron/bajaron
--    DENTRO del período (ej: producto que arrancó fuerte y se enfrió).
ventas_tendencia AS (
    SELECT
        vd.bsale_office_id,
        vd.bsale_variant_id,
        SUM(CASE WHEN vd.fecha <  (p.fecha_from + (p.dias_ventana / 2))
                 THEN vd.qty_venta - vd.qty_devol ELSE 0 END) AS unds_primera_mitad,
        SUM(CASE WHEN vd.fecha >= (p.fecha_from + (p.dias_ventana / 2))
                 THEN vd.qty_venta - vd.qty_devol ELSE 0 END) AS unds_segunda_mitad
    FROM ventas_diarias vd CROSS JOIN params p
    GROUP BY 1, 2
),
-- 4) Jerarquía taxonomía: products → product_types → subcategories → categories → departments
--    Mismo patrón que el resto de las matrices, con prioridad al override de producto
--    sobre el mapeo del product_type.
jerarquia AS (
    SELECT
        p.bsale_product_id,
        p.name AS product_name,
        d.name AS department, d.id AS department_id,
        c.name AS category,   c.id AS category_id,
        s.name AS subcategory, s.id AS subcategory_id
    FROM products p
    LEFT JOIN product_types pt ON p.bsale_product_type_id = pt.bsale_product_type_id
    LEFT JOIN subcategories s  ON s.id = COALESCE(p.subcategory_id, pt.subcategory_id)
    LEFT JOIN categories    c  ON c.id = s.category_id
    LEFT JOIN departments   d  ON d.id = c.department_id
),
-- 5) Base consolidada: todas las dimensiones + métricas crudas + exclusiones.
base AS (
    SELECT
        vt.bsale_office_id,
        vt.bsale_variant_id,
        o.name             AS sucursal,
        v.display_code     AS sku,
        j.product_name,
        j.department,      j.department_id,
        j.category,        j.category_id,
        j.subcategory,     j.subcategory_id,
        vt.unds_vendidas,
        vt.monto_vendido,
        vt.dias_con_venta,
        vt.primera_venta_ventana,
        vt.ultima_venta_ventana,
        COALESCE(vtend.unds_primera_mitad,  0) AS unds_primera_mitad,
        COALESCE(vtend.unds_segunda_mitad,  0) AS unds_segunda_mitad,
        p.dias_ventana,
        p.fecha_from,
        p.fecha_to
    FROM ventas_total vt
    CROSS JOIN params p
    JOIN offices  o ON o.bsale_office_id   = vt.bsale_office_id
    JOIN variants v ON v.bsale_variant_id  = vt.bsale_variant_id
    LEFT JOIN jerarquia        j     ON j.bsale_product_id  = v.bsale_product_id
    LEFT JOIN ventas_tendencia vtend ON vtend.bsale_office_id = vt.bsale_office_id
                                    AND vtend.bsale_variant_id = vt.bsale_variant_id
    WHERE (j.department_id IS NULL OR NOT (j.department_id = ANY(CAST(:excluded_departments AS int[]))))
      AND (j.category_id   IS NULL OR NOT (j.category_id   = ANY(CAST(:excluded_categories   AS int[]))))
),
-- 6) Métricas derivadas: velocidad, frecuencia, tendencia, totales jerárquicos, Pareto.
con_metricas AS (
    SELECT
        b.*,
        -- Velocidad: unds vendidas / días en ventana (NO días con venta — eso sesgaría hacia productos esporádicos).
        CASE WHEN b.dias_ventana > 0
             THEN ROUND((b.unds_vendidas::numeric / b.dias_ventana)::numeric, 3)
             ELSE 0::numeric END AS velocidad_uds_dia,
        -- % Frecuencia: porcentaje de días de la ventana en los que el SKU vendió al menos 1.
        ROUND((b.dias_con_venta::numeric / b.dias_ventana * 100)::numeric, 1) AS pct_frecuencia,
        -- Tendencia intra-ventana: 2ª mitad vs 1ª mitad de la ventana.
        CASE
            WHEN b.unds_primera_mitad = 0 AND b.unds_segunda_mitad > 0                                 THEN 'Inicio en 2ª mitad'
            WHEN b.unds_primera_mitad > 0 AND b.unds_segunda_mitad = 0                                 THEN 'Murió en 1ª mitad'
            WHEN b.unds_primera_mitad > 0 AND b.unds_segunda_mitad >= b.unds_primera_mitad * 1.5       THEN 'Creciendo'
            WHEN b.unds_segunda_mitad > 0 AND b.unds_primera_mitad >= b.unds_segunda_mitad * 1.5       THEN 'Decayendo'
            ELSE 'Estable'
        END AS tendencia,
        -- Totales en S/ por nivel jerárquico (igual que 04b).
        SUM(b.monto_vendido) OVER (PARTITION BY b.bsale_office_id, b.subcategory_id) AS monto_subcat,
        SUM(b.monto_vendido) OVER (PARTITION BY b.bsale_office_id, b.category_id)    AS monto_cat,
        SUM(b.monto_vendido) OVER (PARTITION BY b.bsale_office_id, b.department_id)  AS monto_depto,
        -- Pareto ABC: acumulado del monto dentro de la sucursal, ordenado por monto desc.
        SUM(b.monto_vendido) OVER (PARTITION BY b.bsale_office_id
                                   ORDER BY b.monto_vendido DESC
                                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS monto_acum_sucursal,
        SUM(b.monto_vendido) OVER (PARTITION BY b.bsale_office_id)                  AS monto_total_sucursal,
        ROW_NUMBER() OVER (PARTITION BY b.bsale_office_id ORDER BY b.monto_vendido DESC) AS rank_sucursal
    FROM base b
)
SELECT
    sucursal              AS "Sucursal",
    department            AS "Departamento",
    category              AS "Categoría",
    subcategory           AS "Subcategoría",
    sku                   AS "Código SKU",
    product_name          AS "Producto",

    -- Ventana
    fecha_from            AS "Desde",
    fecha_to              AS "Hasta",
    dias_ventana          AS "Días ventana",
    primera_venta_ventana AS "1ª Venta en ventana",
    ultima_venta_ventana  AS "Últ. Venta en ventana",

    -- Volumen
    unds_vendidas                      AS "Unds Vendidas",
    ROUND(monto_vendido::numeric, 2)   AS "Vendido SKU S/",
    ROUND(monto_subcat::numeric, 2)    AS "Vendido Subcat S/",
    ROUND(monto_cat::numeric, 2)       AS "Vendido Cat S/",
    ROUND(monto_depto::numeric, 2)     AS "Vendido Depto S/",

    -- % participación (S/ del SKU sobre el total del nivel)
    CASE WHEN monto_subcat > 0 THEN ROUND((monto_vendido / monto_subcat * 100)::numeric, 1) ELSE 0::numeric END AS "% S/ en Subcat",
    CASE WHEN monto_cat    > 0 THEN ROUND((monto_vendido / monto_cat    * 100)::numeric, 1) ELSE 0::numeric END AS "% S/ en Cat",
    CASE WHEN monto_depto  > 0 THEN ROUND((monto_vendido / monto_depto  * 100)::numeric, 1) ELSE 0::numeric END AS "% S/ en Depto",

    -- Velocidad y frecuencia
    velocidad_uds_dia AS "Velocidad (uds/día)",
    dias_con_venta    AS "Días con Venta",
    pct_frecuencia    AS "% Frecuencia",

    -- Tendencia intra-ventana
    tendencia            AS "Tendencia",
    unds_primera_mitad   AS "Unds 1ª Mitad",
    unds_segunda_mitad   AS "Unds 2ª Mitad",

    -- Pareto ABC dentro de la sucursal
    rank_sucursal        AS "Rank Sucursal",
    ROUND((monto_acum_sucursal / NULLIF(monto_total_sucursal, 0) * 100)::numeric, 1) AS "% Acum",
    CASE
        WHEN monto_acum_sucursal / NULLIF(monto_total_sucursal, 0) <= 0.80 THEN 'A'
        WHEN monto_acum_sucursal / NULLIF(monto_total_sucursal, 0) <= 0.95 THEN 'B'
        ELSE 'C'
    END AS "Pareto",

    -- Clasificación histórica (cascada simplificada — solo usa data de la ventana).
    -- Orden de las reglas importa: primera que matchea gana.
    CASE
        -- ★ Pareto A + alta frecuencia → producto fuerte y constante
        WHEN monto_acum_sucursal / NULLIF(monto_total_sucursal, 0) <= 0.80 AND pct_frecuencia >= 60
             THEN '🔥 Alta rotación constante (Pareto A + vendió en >60% de los días)'

        -- ★ Pareto A + baja frecuencia → hit estacional/concentrado
        WHEN monto_acum_sucursal / NULLIF(monto_total_sucursal, 0) <= 0.80 AND pct_frecuencia < 20
             THEN '🌟 Hit concentrado (Pareto A + vendió en menos de 20% de los días)'

        -- ★ Pareto A "estándar"
        WHEN monto_acum_sucursal / NULLIF(monto_total_sucursal, 0) <= 0.80
             THEN '🏆 Top Pareto A'

        -- ★ Crecimiento intra-ventana (no estacional, no top, pero subiendo)
        WHEN tendencia IN ('Creciendo', 'Inicio en 2ª mitad')
             THEN '📈 En crecimiento (2ª mitad >> 1ª mitad)'

        -- ★ Caída intra-ventana
        WHEN tendencia IN ('Decayendo', 'Murió en 1ª mitad')
             THEN '📉 En caída (1ª mitad >> 2ª mitad)'

        -- ★ Pareto B
        WHEN monto_acum_sucursal / NULLIF(monto_total_sucursal, 0) <= 0.95
             THEN '📊 Pareto B (estable, contribución media)'

        -- ★ Bajo volumen
        WHEN unds_vendidas < 5
             THEN '💤 Bajo volumen (<5 unidades en la ventana)'

        ELSE '⚪ Pareto C (cola larga)'
    END AS "Clasificación"

FROM con_metricas
ORDER BY bsale_office_id, monto_vendido DESC NULLS LAST;
