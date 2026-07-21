-- =============================================================
-- COSTOS POR SUCURSAL — Variación + Diagnóstico de Salud
-- =============================================================
-- Analiza los costos de cada variante en cada sucursal y diagnostica
-- si están correctos o tienen problemas. Cada variante × sucursal
-- recibe un veredicto: OK, WARNING o ERROR.
--
-- Reglas de validación:
--   ❌ ERROR:   COSTO_CERO, MARGEN_NEGATIVO
--   ⚠️ WARNING: MARGEN_MUY_BAJO (<20%), MARGEN_MUY_ALTO (>70%),
--               COSTO_OUTLIER, SIN_RECEPCION,
--               COSTO_DESACTUALIZADO, VARIACION_ALTA
--
-- Trazabilidad: cada fila indica DE QUÉ TABLA sale el costo y el precio.
--
-- Parámetros (inyectados desde Python):
--   :days                — ventana de ventas para calcular impacto
--   :sucursales_objetivo — array de bsale_office_id a analizar
--   :umbral_margen_bajo  — % mínimo de margen aceptable (default 20)
--   :umbral_margen_alto  — % máximo de margen razonable (default 70)
--   :umbral_outlier_pct  — % de desviación vs promedio para outlier (default 50)
--   :umbral_desactualizado_pct — % de diferencia costo vs última recepción (default 20)
--   :umbral_ratio_max_min — ratio MAX/MIN para variación alta (default 2.0)
--
-- Multi-tenant: filtrado por RLS (app.current_company).
-- =============================================================

-- ─────────────────────────────────────────────────────────────
-- CTE 1: costos_base
-- Cruza variantes activas × sucursales activas con costo
-- (por oficina con fallback al global) + precio de venta + margen
-- ─────────────────────────────────────────────────────────────
WITH params AS (
    SELECT
        CAST(:days AS int)                          AS ventana_dias,
        CAST(:sucursales_objetivo AS int[])         AS sucursales,
        CAST(:umbral_margen_bajo AS numeric)        AS umbral_margen_bajo,
        CAST(:umbral_margen_alto AS numeric)        AS umbral_margen_alto,
        CAST(:umbral_outlier_pct AS numeric)        AS umbral_outlier_pct,
        CAST(:umbral_desactualizado_pct AS numeric) AS umbral_desactualizado_pct,
        CAST(:umbral_ratio_max_min AS numeric)      AS umbral_ratio_max_min,
        CAST(:incluir_igv_en_margen AS boolean)     AS incluir_igv_en_margen
),

costos_base AS (
    SELECT
        o.bsale_office_id,
        o.name                                              AS sucursal,
        v.bsale_variant_id,
        v.display_code                                      AS codigo_sku,
        p.name                                              AS producto,
        COALESCE(vco.effective_cost, vc.effective_cost, 0)  AS costo_efectivo,
        CASE
            WHEN COALESCE(vco.effective_cost, 0) > 0 THEN 'OFICINA'
            WHEN COALESCE(vc.effective_cost, 0) > 0  THEN 'GLOBAL'
            ELSE 'NINGUNO'
        END                                                 AS costo_origen,
        -- ══ TRAZABILIDAD: de qué tabla sale cada dato ══
        CASE
            WHEN COALESCE(vco.effective_cost, 0) > 0
                THEN 'variant_costs_by_office (costo por sucursal)'
            WHEN COALESCE(vc.effective_cost, 0) > 0
                THEN 'variant_costs (costo global)'
            ELSE 'SIN TABLA — no hay costo cargado'
        END                                                 AS tabla_costo,
        CASE
            WHEN pld.net_value IS NOT NULL
                THEN 'price_list_details → lista #' || o.default_price_list_id
            ELSE 'SIN TABLA — sin lista de precios asignada'
        END                                                 AS tabla_precio,
        COALESCE(vco.cost_source, vc.cost_source, 'NONE')  AS cost_source,
        COALESCE(pld.value_with_taxes, 0)                   AS precio_bruto,
        COALESCE(pld.net_value, 0)                          AS precio_neto,
        -- precio_venta respeta el toggle de IGV para que coincida con el margen
        CASE 
            WHEN par.incluir_igv_en_margen THEN COALESCE(pld.value_with_taxes, 0)
            ELSE COALESCE(pld.net_value, 0)
        END                                                 AS precio_venta,
        CASE 
            WHEN par.incluir_igv_en_margen THEN COALESCE(pld.value_with_taxes, 0)
            ELSE COALESCE(pld.net_value, 0)
        END                                                 AS precio_base_margen,
        (CASE 
            WHEN par.incluir_igv_en_margen THEN COALESCE(pld.value_with_taxes, 0)
            ELSE COALESCE(pld.net_value, 0)
         END) - COALESCE(vco.effective_cost, vc.effective_cost, 0) AS margen_soles,
        CASE WHEN (CASE WHEN par.incluir_igv_en_margen THEN COALESCE(pld.value_with_taxes, 0) ELSE COALESCE(pld.net_value, 0) END) > 0
             THEN ROUND(
                 ((CASE WHEN par.incluir_igv_en_margen THEN COALESCE(pld.value_with_taxes, 0) ELSE COALESCE(pld.net_value, 0) END) - COALESCE(vco.effective_cost, vc.effective_cost, 0))
                 / (CASE WHEN par.incluir_igv_en_margen THEN COALESCE(pld.value_with_taxes, 0) ELSE COALESCE(pld.net_value, 0) END) * 100, 2
             )
             ELSE NULL
        END                                                 AS margen_pct
    FROM offices o
    CROSS JOIN params par
    JOIN variants v    ON v.is_active
    JOIN products p    ON p.bsale_product_id = v.bsale_product_id AND p.is_active
    LEFT JOIN variant_costs_by_office vco
        ON vco.bsale_office_id  = o.bsale_office_id
       AND vco.bsale_variant_id = v.bsale_variant_id
    LEFT JOIN variant_costs vc
        ON vc.bsale_variant_id = v.bsale_variant_id
    LEFT JOIN price_list_details pld
        ON pld.bsale_price_list_id = o.default_price_list_id
       AND pld.bsale_variant_id    = v.bsale_variant_id
    WHERE o.is_active
      AND NOT o.is_virtual
      AND o.bsale_office_id = ANY(par.sucursales)
      -- Solo variantes que tienen al menos costo O precio (excluir fantasmas)
      AND (COALESCE(vco.effective_cost, vc.effective_cost, 0) > 0
           OR COALESCE(pld.net_value, 0) > 0)
),

-- ─────────────────────────────────────────────────────────────
-- CTE 2: stats_cross_sucursal
-- Para cada variante: estadísticas del costo entre sucursales
-- ─────────────────────────────────────────────────────────────
stats_cross AS (
    SELECT
        bsale_variant_id,
        COUNT(*)                                  AS n_sucursales,
        ROUND(AVG(costo_efectivo)::numeric, 4)    AS costo_avg,
        MIN(costo_efectivo)                       AS costo_min,
        MAX(costo_efectivo)                       AS costo_max,
        ROUND(STDDEV_POP(costo_efectivo)::numeric, 4) AS costo_stddev,
        CASE WHEN MIN(costo_efectivo) > 0
             THEN ROUND((MAX(costo_efectivo) / MIN(costo_efectivo))::numeric, 2)
             ELSE NULL
        END                                       AS ratio_max_min
    FROM costos_base
    WHERE costo_efectivo > 0
    GROUP BY bsale_variant_id
),

-- ─────────────────────────────────────────────────────────────
-- CTE 3: respaldo_recepciones
-- Último costo de recepción y conteo por variante × sucursal
-- ─────────────────────────────────────────────────────────────
respaldo_recepciones AS (
    SELECT
        r.bsale_office_id,
        rd.bsale_variant_id,
        COUNT(*) FILTER (WHERE rd.cost > 0)        AS n_recepciones,
        MAX(rd.cost) FILTER (WHERE rd.cost > 0)    AS ultimo_costo_recepcion,
        SUM(rd.quantity * rd.cost) FILTER (WHERE rd.cost > 0)
            / NULLIF(SUM(rd.quantity) FILTER (WHERE rd.cost > 0), 0)
                                                   AS avg_costo_recepcion
    FROM reception_details rd
    JOIN receptions r ON r.bsale_reception_id = rd.bsale_reception_id
    GROUP BY r.bsale_office_id, rd.bsale_variant_id
),

-- ─────────────────────────────────────────────────────────────
-- CTE 4: ventas_recientes
-- Unidades vendidas por sucursal en la ventana de tiempo
-- ─────────────────────────────────────────────────────────────
ventas_recientes AS (
    SELECT
        doc.bsale_office_id,
        dd.bsale_variant_id,
        SUM(dd.quantity)     AS uds_vendidas,
        SUM(dd.total_amount) AS monto_vendido
    FROM document_details dd
    JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id
    CROSS JOIN params par
    WHERE (doc.emission_date AT TIME ZONE 'UTC')::DATE >= CURRENT_DATE - par.ventana_dias
      AND (doc.emission_date AT TIME ZONE 'UTC')::DATE <  CURRENT_DATE
      AND COALESCE(doc.is_credit_note, FALSE) = FALSE
      AND doc.bsale_office_id = ANY(par.sucursales)
      AND NOT dd.is_gratuity
    GROUP BY doc.bsale_office_id, dd.bsale_variant_id
),

-- ─────────────────────────────────────────────────────────────
-- CTE 5: diagnostico
-- Aplica las 8 reglas de validación a cada variante × sucursal
-- ─────────────────────────────────────────────────────────────
diagnostico AS (
    SELECT
        cb.*,
        -- Stats cross-sucursal
        COALESCE(sc.n_sucursales, 0)               AS n_sucursales,
        COALESCE(sc.costo_avg, 0)                  AS costo_avg_sucursales,
        COALESCE(sc.costo_min, 0)                  AS costo_min_sucursales,
        COALESCE(sc.costo_max, 0)                  AS costo_max_sucursales,
        COALESCE(sc.ratio_max_min, 1)              AS ratio_max_min,
        -- Diferencia vs promedio
        CASE WHEN COALESCE(sc.costo_avg, 0) > 0
             THEN ROUND(((cb.costo_efectivo - sc.costo_avg) / sc.costo_avg * 100)::numeric, 2)
             ELSE 0
        END                                        AS diff_vs_avg_pct,
        -- Recepciones
        COALESCE(rr.n_recepciones, 0)              AS n_recepciones,
        COALESCE(rr.ultimo_costo_recepcion, 0)     AS ultimo_costo_recepcion,
        -- Ventas
        COALESCE(vr.uds_vendidas, 0)               AS uds_vendidas,
        COALESCE(vr.monto_vendido, 0)              AS monto_vendido,
        -- Impacto: (costo_esta_sucursal - costo_promedio) × unidades vendidas
        ROUND((
            (cb.costo_efectivo - COALESCE(sc.costo_avg, cb.costo_efectivo))
            * COALESCE(vr.uds_vendidas, 0)
        )::numeric, 2)                            AS impacto_soles,

        -- ══════════════ REGLAS DE VALIDACIÓN ══════════════

        -- Regla 1: COSTO_CERO (ERROR)
        (cb.costo_efectivo = 0)                    AS flag_costo_cero,

        -- Regla 2: MARGEN_NEGATIVO (ERROR)
        (cb.precio_base_margen > 0
         AND cb.costo_efectivo > cb.precio_base_margen)  AS flag_margen_negativo,

        -- Regla 3: MARGEN_MUY_BAJO (WARNING) — margen < 20%
        (cb.precio_base_margen > 0
         AND cb.costo_efectivo > 0
         AND cb.costo_efectivo <= cb.precio_base_margen
         AND cb.margen_pct < par.umbral_margen_bajo) AS flag_margen_bajo,

        -- Regla 3b: MARGEN_MUY_ALTO (WARNING) — margen > 70% (sospechoso: costo mal cargado?)
        (cb.precio_base_margen > 0
         AND cb.costo_efectivo > 0
         AND cb.margen_pct > par.umbral_margen_alto) AS flag_margen_alto,

        -- Regla 4: COSTO_OUTLIER (WARNING)
        (COALESCE(sc.costo_avg, 0) > 0
         AND sc.n_sucursales > 1
         AND ABS(cb.costo_efectivo - sc.costo_avg) / sc.costo_avg * 100
             > par.umbral_outlier_pct)             AS flag_outlier,

        -- Regla 5: SIN_RECEPCION (WARNING)
        (cb.cost_source = 'NONE'
         AND COALESCE(rr.n_recepciones, 0) = 0)    AS flag_sin_recepcion,

        -- Regla 6: COSTO_DESACTUALIZADO (WARNING)
        (COALESCE(rr.ultimo_costo_recepcion, 0) > 0
         AND cb.costo_efectivo > 0
         AND ABS(cb.costo_efectivo - rr.ultimo_costo_recepcion)
             / cb.costo_efectivo * 100
             > par.umbral_desactualizado_pct)      AS flag_desactualizado,

        -- Regla 7: VARIACION_ALTA (WARNING) — a nivel de variante
        (COALESCE(sc.ratio_max_min, 1)
            > par.umbral_ratio_max_min
         AND sc.n_sucursales > 1)                  AS flag_variacion_alta

    FROM costos_base cb
    CROSS JOIN params par
    LEFT JOIN stats_cross sc    ON sc.bsale_variant_id = cb.bsale_variant_id
    LEFT JOIN respaldo_recepciones rr
        ON rr.bsale_office_id  = cb.bsale_office_id
       AND rr.bsale_variant_id = cb.bsale_variant_id
    LEFT JOIN ventas_recientes vr
        ON vr.bsale_office_id  = cb.bsale_office_id
       AND vr.bsale_variant_id = cb.bsale_variant_id
)

-- ─────────────────────────────────────────────────────────────
-- SELECT FINAL
-- Resultado completo: variación + diagnóstico + impacto
-- Ordenado por severidad DESC, impacto monetario DESC
-- ─────────────────────────────────────────────────────────────
SELECT
    bsale_office_id,
    sucursal,
    bsale_variant_id,
    codigo_sku,
    producto,
    costo_efectivo,
    costo_origen,
    tabla_costo,
    precio_venta,
    tabla_precio,
    ROUND(margen_soles::numeric, 2)         AS margen_soles,
    margen_pct,
    costo_avg_sucursales,
    costo_min_sucursales,
    costo_max_sucursales,
    diff_vs_avg_pct,
    ratio_max_min,
    ultimo_costo_recepcion,
    n_recepciones,
    uds_vendidas                            AS uds_vendidas_periodo,
    impacto_soles,

    -- Severidad máxima
    CASE
        WHEN flag_costo_cero OR flag_margen_negativo THEN 'ERROR'
        WHEN flag_margen_bajo OR flag_margen_alto OR flag_outlier
             OR flag_sin_recepcion OR flag_desactualizado
             OR flag_variacion_alta THEN 'WARNING'
        ELSE 'OK'
    END                                     AS severidad,

    -- Array de alertas disparadas
    ARRAY_REMOVE(ARRAY[
        CASE WHEN flag_costo_cero       THEN 'COSTO_CERO' END,
        CASE WHEN flag_margen_negativo  THEN 'MARGEN_NEGATIVO' END,
        CASE WHEN flag_margen_bajo      THEN 'MARGEN_MUY_BAJO (<20%)' END,
        CASE WHEN flag_margen_alto      THEN 'MARGEN_MUY_ALTO (>70%)' END,
        CASE WHEN flag_outlier          THEN 'COSTO_OUTLIER' END,
        CASE WHEN flag_sin_recepcion    THEN 'SIN_RECEPCION' END,
        CASE WHEN flag_desactualizado   THEN 'COSTO_DESACTUALIZADO' END,
        CASE WHEN flag_variacion_alta   THEN 'VARIACION_ALTA' END
    ], NULL)                                AS alertas

FROM diagnostico
ORDER BY
    -- Errores primero, luego warnings, luego ok
    CASE
        WHEN flag_costo_cero OR flag_margen_negativo THEN 0
        WHEN flag_margen_bajo OR flag_margen_alto OR flag_outlier
             OR flag_sin_recepcion OR flag_desactualizado
             OR flag_variacion_alta THEN 1
        ELSE 2
    END,
    -- Dentro de cada severidad: mayor impacto monetario primero
    ABS(impacto_soles) DESC NULLS LAST,
    sucursal,
    producto;
