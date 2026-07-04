-- =============================================================
-- BASE COMPARTIDA de las matrices 04 / 04b / 05
-- =============================================================
-- Este archivo NO es una consulta completa: termina en la CTE `matriz`
-- (fila por SKU×sucursal con métricas + cascada de clasificación) y
-- `service._load_sql` le concatena el SELECT final de cada módulo
-- (04_matriz_90d.sql, 04b_matriz_90d_jerarquico.sql, 05_matriz_operativa.sql),
-- que solo PROYECTA columnas, filtra fantasmas (04b/05) y agrega las
-- ventanas jerárquicas (04b). TODA la lógica de negocio vive aquí: un fix
-- a la cascada o a una métrica se hace UNA sola vez en este archivo.
--
-- Ventana operativa: 90 días.
-- Velocidad: del LOTE ACTUAL (desde última recepción), con piso 7d.
-- =============================================================
WITH params AS (
    SELECT NOW()                                         AS ahora,
        NOW() - (:ventana_main_dias * INTERVAL '1 day') AS fecha_corte,
        CAST(:sucursales_objetivo AS int[])             AS sucursales_objetivo,
        CAST(:tipos_venta AS int[])                     AS tipos_venta,
        CAST(:tipos_devolucion AS int[])                AS tipos_devolucion,
        -- ★ Config por empresa (antes literales hardcodeados):
        CAST(:warehouse_user_ids AS int[])              AS warehouse_user_ids,
        CAST(:qty_sanity_limit AS int)                  AS qty_sanity_limit,
        CAST(:piso_dias_lote AS int)                    AS piso_dias_lote,
        CAST(:ventana_main_dias AS int)                 AS ventana_main_dias,
        CAST(:ventana_trend_split_dias AS int)          AS ventana_trend_split_dias,
        CAST(:ventana_recent_dias AS int)               AS ventana_recent_dias,
        CAST(:ventana_blind_spot_dias AS int)           AS ventana_blind_spot_dias,
        CAST(:ventana_dead_dias AS int)                 AS ventana_dead_dias,
        -- Umbrales de clasificación (política comercial).
        -- ★ Doble cast ::text::numeric: SQLAlchemy/asyncpg pasa floats Python como
        --   float8 (double precision); el cast directo a numeric arrastra la
        --   imprecisión IEEE-754 (0.2 → 0.20000000000000001). Pasar por text usa
        --   el shortest round-trip de Postgres ('0.2') y luego a numeric exacto.
        CAST(:sellthrough_exito_ratio AS numeric)       AS sellthrough_exito_ratio,
        CAST(:loss_consumo_ratio AS numeric)            AS loss_consumo_ratio,
        CAST(:loss_venta_ratio AS numeric)              AS loss_venta_ratio,
        CAST(:trend_grow_mult AS numeric)               AS trend_grow_mult,
        CAST(:trend_decay_mult AS numeric)              AS trend_decay_mult,
        CAST(:xyz_constante_pct AS int)                 AS xyz_constante_pct,
        CAST(:xyz_variable_pct AS int)                  AS xyz_variable_pct,
        CAST(:proy_mes_alta AS int)                     AS proy_mes_alta,
        CAST(:proy_mes_min_floor AS int)                AS proy_mes_min_floor,
        CAST(:proy_mes_min_cap AS int)                  AS proy_mes_min_cap,
        CAST(:proy_mes_cat_ratio AS numeric)            AS proy_mes_cat_ratio,
        CAST(:cobertura_critica_dias AS int)            AS cobertura_critica_dias,
        CAST(:cobertura_baja_dias AS int)               AS cobertura_baja_dias,
        CAST(:cobertura_objetivo_dias AS int)           AS cobertura_objetivo_dias,
        CAST(:dias_absorcion_bestseller_max AS int)     AS dias_absorcion_bestseller_max,
        CAST(:dsv_quiebre_max_dias AS int)              AS dsv_quiebre_max_dias,
        CAST(:lifetime_bestseller_min AS int)           AS lifetime_bestseller_min,
        CAST(:recien_reabastecido_dias AS int)          AS recien_reabastecido_dias,
        CAST(:lote_frenado_stock_min AS int)            AS lote_frenado_stock_min,
        CAST(:lote_frenado_proy_min AS int)             AS lote_frenado_proy_min,
        CAST(:lote_frenado_proy30_max AS int)           AS lote_frenado_proy30_max,
        CAST(:lote_frenado_edad_min AS int)             AS lote_frenado_edad_min,
        CAST(:lento_cronico_dsv_min AS int)             AS lento_cronico_dsv_min,
        CAST(:lento_cronico_lifetime_max AS int)        AS lento_cronico_lifetime_max,
        CAST(:lento_cronico_proy_max AS int)            AS lento_cronico_proy_max
),
-- ★ P22: stock en almacenes (oficinas FUERA de sucursales_objetivo, ej. Almacén
--   Central). Informativo: distingue "REPONER = comprar al proveedor" de
--   "REPONER = pedir traslado del almacén" (38 SKUs / 8,717 unds al 2026-06-11).
stock_almacen AS (
    SELECT sl.bsale_variant_id, SUM(sl.quantity_available) AS stock_almacen
    FROM stock_levels sl CROSS JOIN params p
    WHERE NOT (sl.bsale_office_id = ANY(p.sucursales_objetivo))
      AND sl.quantity_available > 0
    GROUP BY 1
),
ventas_diarias AS (
    SELECT
        d.bsale_office_id,
        dd.bsale_variant_id,
        DATE(d.emission_date) AS fecha,
        SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_venta) THEN dd.quantity ELSE 0 END) AS qty_venta,
        SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_devolucion) THEN dd.quantity ELSE 0 END) AS qty_devol
    FROM documents d
    JOIN document_details dd USING (bsale_document_id)
    CROSS JOIN params p
    WHERE d.is_active
      AND d.emission_date >= p.fecha_corte
      AND d.bsale_office_id = ANY(p.sucursales_objetivo)
      AND d.bsale_document_type_id = ANY(p.tipos_venta || p.tipos_devolucion)
    GROUP BY 1, 2, 3
),
ventas_90d AS (
    SELECT bsale_office_id, bsale_variant_id,
        SUM(qty_venta - qty_devol) AS unds_vendidas,
        MAX(fecha) FILTER (WHERE qty_venta - qty_devol > 0) AS ultima_venta,
        COUNT(DISTINCT fecha) FILTER (WHERE qty_venta - qty_devol > 0) AS dias_con_venta
    FROM ventas_diarias
    GROUP BY 1, 2
    HAVING SUM(qty_venta - qty_devol) > 0
),
-- ★ VENTAS EN MONTO MONETARIO (90d) — para totales jerárquicos en S/ (los proyecta el 04b)
ventas_monto_90d AS (
    SELECT d.bsale_office_id, dd.bsale_variant_id,
        SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_venta) THEN dd.total_amount ELSE 0 END) -
        SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_devolucion) THEN dd.total_amount ELSE 0 END) AS monto_vendido_90d
    FROM documents d
    JOIN document_details dd USING (bsale_document_id)
    CROSS JOIN params p
    WHERE d.is_active
      AND d.emission_date >= p.fecha_corte
      AND d.bsale_office_id = ANY(p.sucursales_objetivo)
      AND d.bsale_document_type_id = ANY(p.tipos_venta || p.tipos_devolucion)
    GROUP BY 1, 2
),
-- ★ Ventas separadas en dos sub-periodos para calcular tendencia:
--    v_recent_45d: últimos 45 días
--    v_old_45d:    los 45 días anteriores (45-90 días atrás)
--    Permite detectar productos creciendo/decayendo dentro de la ventana 90d.
ventas_tendencia AS (
    SELECT vd.bsale_office_id, vd.bsale_variant_id,
        SUM(CASE WHEN vd.fecha >= (p.ahora - (p.ventana_trend_split_dias * INTERVAL '1 day'))::date
                 THEN vd.qty_venta - vd.qty_devol ELSE 0 END) AS v_recent_45d,
        SUM(CASE WHEN vd.fecha < (p.ahora - (p.ventana_trend_split_dias * INTERVAL '1 day'))::date
                 THEN vd.qty_venta - vd.qty_devol ELSE 0 END) AS v_old_45d
    FROM ventas_diarias vd CROSS JOIN params p
    GROUP BY 1, 2
),
-- ★ Ventas en los ÚLTIMOS 30 DÍAS naturales.
--    Base para "Vel últimos 30d" — refleja el comportamiento RECIENTE del SKU,
--    útil para reposición cuando el producto está creciendo/decayendo.
ventas_30d AS (
    SELECT vd.bsale_office_id, vd.bsale_variant_id,
        SUM(vd.qty_venta - vd.qty_devol) AS unds_vendidas_30d
    FROM ventas_diarias vd CROSS JOIN params p
    WHERE vd.fecha >= (p.ahora - (p.ventana_recent_dias * INTERVAL '1 day'))::date
    GROUP BY 1, 2
),
stock_sucursal AS (
    SELECT bsale_office_id, bsale_variant_id,
        quantity_available AS stock_disponible,
        quantity_reserved  AS stock_reservado
    FROM stock_levels sl CROSS JOIN params p
    WHERE sl.bsale_office_id = ANY(p.sucursales_objetivo)
),
recep_90d AS (
    SELECT r.bsale_office_id, rd.bsale_variant_id,
        MIN(CASE WHEN r.bsale_user_id = ANY(p.warehouse_user_ids) THEN r.admission_date END) AS primera_recep_90d,  -- ★ solo recepciones de almaceneros — ajustes de caja/ADM se ignoran
        MAX(r.admission_date) AS ultima_recep_90d,
        SUM(rd.quantity)      AS unds_recibidas_90d,
        COUNT(DISTINCT r.bsale_reception_id) AS num_recepciones_90d
    FROM receptions r
    JOIN reception_details rd USING (bsale_reception_id)
    CROSS JOIN params p
    WHERE r.bsale_office_id = ANY(p.sucursales_objetivo)
      AND r.admission_date >= p.fecha_corte
      AND rd.quantity <= (SELECT qty_sanity_limit FROM params)  -- ★ P22: tope de sanidad (códigos de barras tipeados como cantidad; máx legítimo histórico: 10,000)
    GROUP BY 1, 2
),
primera_recep_total AS (
    SELECT r.bsale_office_id, rd.bsale_variant_id,
        MIN(r.admission_date) AS primera_recepcion,
        MAX(CASE WHEN r.bsale_user_id = ANY(p.warehouse_user_ids) THEN r.admission_date END) AS ultima_recepcion,  -- ★ solo recepciones de almaceneros — ajustes de caja/ADM se ignoran
        SUM(rd.quantity)      AS unds_recibidas_lifetime  -- total recibido en toda la vida del SKU
    FROM receptions r
    JOIN reception_details rd USING (bsale_reception_id)
    CROSS JOIN params p
    WHERE r.bsale_office_id = ANY(p.sucursales_objetivo)
      AND rd.quantity <= (SELECT qty_sanity_limit FROM params)  -- ★ P22: tope de sanidad
    GROUP BY 1, 2
),
-- ★ Última recepción individual (la fila más reciente, con su cantidad propia).
--    Distinto a primera_recep_total: aquí necesitamos la CANTIDAD del último lote,
--    no la suma de todas las recepciones del SKU.
ult_recep_info AS (
    SELECT DISTINCT ON (r.bsale_office_id, rd.bsale_variant_id)
        r.bsale_office_id,
        rd.bsale_variant_id,
        r.admission_date AS ult_recep_fecha,
        rd.quantity      AS ult_recep_qty
    FROM receptions r
    JOIN reception_details rd USING (bsale_reception_id)
    CROSS JOIN params p
    WHERE r.bsale_office_id = ANY(p.sucursales_objetivo)
      AND r.bsale_user_id = ANY(p.warehouse_user_ids)  -- ★ solo recepciones de almaceneros
      AND rd.quantity <= (SELECT qty_sanity_limit FROM params)  -- ★ P22: tope de sanidad
    ORDER BY r.bsale_office_id, rd.bsale_variant_id, r.admission_date DESC
),
-- ════════════════════════════════════════════════════════════════════════
-- ★ FIX "días con stock real" — Reconstrucción backward del stock por día.
--    Problema: cuando un SKU vendió todo, estuvo días sin stock y luego
--    recibió otro lote, la velocidad del ciclo se diluye porque dias_efectivos
--    = (hoy - inicio_ciclo) cuenta también los días que NO había stock para
--    vender. Resultado: velocidad subestimada hasta 100-200%.
--    Fix: contar SOLO los días donde físicamente había stock disponible.
--    Ejemplo medido: TOALLITA BYWIN 60301 → vel 17→39 uds/día (+130%).
-- ════════════════════════════════════════════════════════════════════════
movimientos_diarios AS (
    SELECT bsale_office_id, bsale_variant_id, fecha, SUM(delta) AS delta_dia
    FROM (
        SELECT bsale_office_id, bsale_variant_id, fecha,
               -(qty_venta - qty_devol) AS delta
        FROM ventas_diarias
        UNION ALL
        SELECT r.bsale_office_id, rd.bsale_variant_id,
               r.admission_date::date, rd.quantity
        FROM receptions r
        JOIN reception_details rd USING (bsale_reception_id)
        CROSS JOIN params p
        WHERE r.bsale_office_id = ANY(p.sucursales_objetivo)
          AND r.admission_date >= p.fecha_corte
        UNION ALL
        SELECT c.bsale_office_id, cd.bsale_variant_id,
               c.consumption_date::date, -cd.quantity
        FROM consumptions c
        JOIN consumption_details cd USING (bsale_consumption_id)
        CROSS JOIN params p
        WHERE c.bsale_office_id = ANY(p.sucursales_objetivo)
          AND c.consumption_date >= p.fecha_corte
    ) m
    GROUP BY 1, 2, 3
),
sku_eventos AS (
    SELECT m.bsale_office_id, m.bsale_variant_id, m.fecha,
           m.delta_dia,
           ss.stock_disponible
             - COALESCE(SUM(m.delta_dia) OVER (
                 PARTITION BY m.bsale_office_id, m.bsale_variant_id
                 ORDER BY m.fecha
                 ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING
               ), 0) AS stock_fin_dia,
           LEAD(m.fecha, 1, (SELECT ahora::date + 1 FROM params))
             OVER (PARTITION BY m.bsale_office_id, m.bsale_variant_id
                   ORDER BY m.fecha) AS sig_fecha,
           -- ★ FIX lotes >90d (2026-06-10): identificar el PRIMER evento de la
           --   ventana para sumar los días previos del ciclo (ver CTE siguiente).
           ROW_NUMBER() OVER (PARTITION BY m.bsale_office_id, m.bsale_variant_id
                              ORDER BY m.fecha) AS rn
    FROM movimientos_diarios m
    JOIN stock_sucursal ss USING (bsale_office_id, bsale_variant_id)
),
dias_con_stock_ciclo AS (
    SELECT e.bsale_office_id, e.bsale_variant_id,
           SUM(
             CASE WHEN e.stock_fin_dia > 0
                  THEN GREATEST(0,
                       LEAST(e.sig_fecha,
                             (SELECT ahora::date + 1 FROM params))
                       - GREATEST(e.fecha,
                                  COALESCE(r90.primera_recep_90d::date,
                                           prt.ultima_recepcion::date))
                  )
                  ELSE 0
             END
             -- ★ FIX lotes >90d (2026-06-10): los movimientos solo cubren la
             --   ventana de 90d, pero el ciclo puede haber empezado antes
             --   (ultima_recepcion lifetime). Esos días previos NO se contaban
             --   → denominador ≤90d con numerador del lote completo → velocidad
             --   inflada. Si al inicio del 1er evento de la ventana había
             --   stock>0, se asume stock continuo desde el inicio del ciclo.
             + CASE WHEN e.rn = 1
                    AND (e.stock_fin_dia - e.delta_dia) > 0
                    THEN GREATEST(0,
                         e.fecha - COALESCE(r90.primera_recep_90d::date,
                                            prt.ultima_recepcion::date))
                    ELSE 0
               END
           )::numeric AS dias_con_stock
    FROM sku_eventos e
    LEFT JOIN recep_90d r90 USING (bsale_office_id, bsale_variant_id)
    LEFT JOIN primera_recep_total prt USING (bsale_office_id, bsale_variant_id)
    GROUP BY 1, 2
),
-- ★ Consumos LIFETIME (mermas/transferencias internas/regalos).
--    NO son ventas pero SÍ son salidas de inventario. Usado en la regla
--    "💎 EXITOSO PASADO" para calcular el verdadero sell-through:
--    (ventas + consumos) / recibido. Caso XK3179: vendió 4 + consumió 2 de 6 = 100%.
consumos_lifetime AS (
    SELECT c.bsale_office_id, cd.bsale_variant_id,
        SUM(cd.quantity) AS unds_consumidas_lifetime
    FROM consumptions c
    JOIN consumption_details cd USING (bsale_consumption_id)
    CROSS JOIN params p
    WHERE c.bsale_office_id = ANY(p.sucursales_objetivo)
      AND cd.quantity <= (SELECT qty_sanity_limit FROM params)  -- ★ P22: tope de sanidad (las "correcciones" de recepciones corruptas también eran absurdas; máx legítimo: 2,250)
    GROUP BY 1, 2
),
-- ★ Traslados de SALIDA LIFETIME (IDs configurables vía :tipos_traslado).
--    En COYA el tipo 53 = TRASLADO INTERNO (el tipo 37 no existe en este sistema).
--    Cuando una sucursal envía mercadería a otra, sale del inventario pero
--    NO se vende. Antes el SQL contaba esto como "recibido sin vender", inflando
--    el sell-through aparente.
--    Casos: 252590 (recibió 96, trasladó 48, vendió 48 → 100% real).
traslados_lifetime AS (
    SELECT d.bsale_office_id, dd.bsale_variant_id,
        SUM(dd.quantity) AS unds_trasladadas_lifetime
    FROM documents d
    JOIN document_details dd USING (bsale_document_id)
    CROSS JOIN params p
    WHERE d.is_active
      AND d.bsale_office_id = ANY(p.sucursales_objetivo)
      AND d.bsale_document_type_id = ANY(CAST(:tipos_traslado AS int[]))
    GROUP BY 1, 2
),
-- Ventas posteriores a la última recepción, LIMITADAS a 90d
-- (compat: se mantiene "Vend Post Recep" pero ya no se usa para velocidad)
ventas_post_recep AS (
    SELECT
        vd.bsale_office_id, vd.bsale_variant_id,
        SUM(vd.qty_venta - vd.qty_devol) AS unds_post_recep,
        MAX(vd.fecha) FILTER (WHERE vd.qty_venta - vd.qty_devol > 0) AS ult_venta_post_recep
    FROM ventas_diarias vd
    JOIN primera_recep_total prt
      ON prt.bsale_office_id = vd.bsale_office_id
     AND prt.bsale_variant_id = vd.bsale_variant_id
    WHERE vd.fecha >= prt.ultima_recepcion::date
    GROUP BY 1, 2
),
-- ★ Ventas LIFETIME del SKU (TODAS sus ventas, sin filtro alguno).
--    Usado para la regla 💎 PRODUCTO EXITOSO AGOTADO.
ventas_total_sku AS (
    SELECT d.bsale_office_id, dd.bsale_variant_id,
        SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_venta) THEN dd.quantity ELSE 0 END) -
        SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_devolucion) THEN dd.quantity ELSE 0 END) AS unds_vendidas_lifetime,
        MAX(d.emission_date::date) FILTER (WHERE d.bsale_document_type_id = ANY(p.tipos_venta)) AS ult_venta_lifetime
    FROM documents d
    JOIN document_details dd USING (bsale_document_id)
    CROSS JOIN params p
    WHERE d.is_active
      AND d.bsale_office_id = ANY(p.sucursales_objetivo)
      AND d.bsale_document_type_id = ANY(p.tipos_venta || p.tipos_devolucion)
    GROUP BY 1, 2
),
-- ★ Mejor mes lifetime (mes con más ventas en toda la historia — lo proyecta el 05)
mejor_mes AS (
    SELECT DISTINCT ON (bsale_office_id, bsale_variant_id)
        bsale_office_id, bsale_variant_id, anio_mes, mes_total
    FROM (
        SELECT d.bsale_office_id, dd.bsale_variant_id,
               TO_CHAR(d.emission_date, 'YYYY-MM') AS anio_mes,
               SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_venta) THEN dd.quantity ELSE 0 END) -
               SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_devolucion) THEN dd.quantity ELSE 0 END) AS mes_total
        FROM documents d JOIN document_details dd USING (bsale_document_id) CROSS JOIN params p
        WHERE d.is_active
          AND d.bsale_office_id = ANY(p.sucursales_objetivo)
          AND d.bsale_document_type_id = ANY(p.tipos_venta || p.tipos_devolucion)
        GROUP BY 1, 2, 3
        HAVING SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_venta) THEN dd.quantity ELSE 0 END) -
               SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_devolucion) THEN dd.quantity ELSE 0 END) > 0
    ) m
    ORDER BY bsale_office_id, bsale_variant_id, mes_total DESC
),
-- ★ Ventas del LOTE COMPLETO (ciclo actual del producto).
--    El ciclo inicia en:
--      - primera_recep_90d (si hubo recepciones en 90d → trata lotes múltiples cercanos como uno)
--      - ultima_recepcion lifetime (si no hubo recepciones en 90d → lote viejo)
--    Resuelve casos: Aloe Vera (2 lotes cercanos), B1721 (lote viejo).
ventas_lote_total AS (
    SELECT d.bsale_office_id, dd.bsale_variant_id,
        SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_venta) THEN dd.quantity ELSE 0 END) -
        SUM(CASE WHEN d.bsale_document_type_id = ANY(p.tipos_devolucion) THEN dd.quantity ELSE 0 END) AS unds_lote_total,
        MAX(d.emission_date::date) FILTER (WHERE d.bsale_document_type_id = ANY(p.tipos_venta)) AS ult_venta_lote,
        MIN(d.emission_date::date) FILTER (WHERE d.bsale_document_type_id = ANY(p.tipos_venta)) AS pri_venta_lote,
        COUNT(DISTINCT d.emission_date::date) FILTER (WHERE d.bsale_document_type_id = ANY(p.tipos_venta)) AS dias_con_venta_lote
    FROM documents d
    JOIN document_details dd USING (bsale_document_id)
    CROSS JOIN params p
    JOIN primera_recep_total prt
      ON prt.bsale_office_id = d.bsale_office_id
     AND prt.bsale_variant_id = dd.bsale_variant_id
    LEFT JOIN recep_90d r90
      ON r90.bsale_office_id = d.bsale_office_id
     AND r90.bsale_variant_id = dd.bsale_variant_id
    WHERE d.is_active
      AND d.bsale_office_id = ANY(p.sucursales_objetivo)
      AND d.bsale_document_type_id = ANY(p.tipos_venta || p.tipos_devolucion)
      AND d.emission_date >= COALESCE(r90.primera_recep_90d, prt.ultima_recepcion)
    GROUP BY 1, 2
),
jerarquia AS (
    SELECT p.bsale_product_id, p.name AS product_name,
        d.id AS department_id, d.name AS department,
        c.id AS category_id, c.name AS category,
        s.id AS subcategory_id, s.name AS subcategory
    FROM products p
    LEFT JOIN product_types pt ON p.bsale_product_type_id = pt.bsale_product_type_id
    LEFT JOIN subcategories s ON s.id = COALESCE(p.subcategory_id, pt.subcategory_id)
    LEFT JOIN categories c ON c.id = s.category_id
    LEFT JOIN departments d ON d.id = c.department_id
    WHERE p.is_active
),
base AS (
    SELECT bsale_office_id, bsale_variant_id FROM stock_sucursal WHERE stock_disponible > 0
    UNION SELECT bsale_office_id, bsale_variant_id FROM ventas_90d
    UNION SELECT bsale_office_id, bsale_variant_id FROM recep_90d
    -- ★ FIX punto ciego (2026-06-10): incluir SKUs con ventas en los últimos
    --   180d aunque hoy no tengan stock ni actividad en la ventana de 90d.
    --   Antes, un bestseller agotado hace >90d desaparecía del reporte y nunca
    --   podía marcarse 💎 OPORTUNIDAD PERDIDA.
    UNION
    SELECT d.bsale_office_id, dd.bsale_variant_id
    FROM documents d
    JOIN document_details dd USING (bsale_document_id)
    CROSS JOIN params p
    WHERE d.is_active
      AND d.bsale_office_id = ANY(p.sucursales_objetivo)
      AND d.bsale_document_type_id = ANY(p.tipos_venta)
      AND d.emission_date >= p.ahora - (p.ventana_blind_spot_dias * INTERVAL '1 day')
),
radiografia AS (
    SELECT b.bsale_office_id, b.bsale_variant_id,
        o.name AS sucursal, v.display_code,
        j.product_name, j.department, j.category, j.subcategory,
        -- ★ department_id para la regla de estacionales (cascada);
        --   category_id/subcategory_id para las ventanas jerárquicas del 04b.
        j.department_id, j.category_id, j.subcategory_id,
        COALESCE(v90.unds_vendidas, 0)::numeric    AS unds_vendidas,
        COALESCE(ss.stock_disponible, 0)::numeric  AS stock_disponible,
        COALESCE(ss.stock_reservado, 0)::numeric   AS stock_reservado,
        COALESCE(v90.dias_con_venta, 0)            AS dias_con_ventas,
        v90.ultima_venta,
        COALESCE(r90.unds_recibidas_90d, 0)::numeric AS unds_recibidas_90d,
        r90.primera_recep_90d, r90.ultima_recep_90d,
        prt.primera_recepcion, prt.ultima_recepcion,
        COALESCE(prt.unds_recibidas_lifetime, 0)::numeric AS unds_recibidas_lifetime,
        COALESCE(vts.unds_vendidas_lifetime, 0)::numeric  AS unds_vendidas_lifetime,
        vts.ult_venta_lifetime,
        COALESCE(vpr.unds_post_recep, 0)::numeric  AS unds_post_recep,
        vpr.ult_venta_post_recep,
        -- ★ NUEVO: datos del LOTE COMPLETO (sin filtro 90d)
        COALESCE(vlt.unds_lote_total, 0)::numeric  AS unds_lote_total,
        vlt.ult_venta_lote,
        vlt.pri_venta_lote,
        COALESCE(vlt.dias_con_venta_lote, 0)       AS dias_con_venta_lote,
        -- ★ Tendencia: ventas en sub-períodos 45d
        COALESCE(vt.v_recent_45d, 0)::numeric      AS v_recent_45d,
        COALESCE(vt.v_old_45d, 0)::numeric         AS v_old_45d,
        -- ★ Ventas últimos 30d naturales (para velocidad reciente)
        COALESCE(v30.unds_vendidas_30d, 0)::numeric AS unds_vendidas_30d,
        -- ★ Consumos LIFETIME (mermas — para sell-through real)
        COALESCE(cl.unds_consumidas_lifetime, 0)::numeric AS unds_consumidas_lifetime,
        -- ★ Traslados de salida LIFETIME (tipo 53 en COYA — para sell-through real)
        COALESCE(tl.unds_trasladadas_lifetime, 0)::numeric AS unds_trasladadas_lifetime,
        -- ★ Monto vendido en 90d en S/ (para los totales jerárquicos del 04b)
        COALESCE(vm.monto_vendido_90d, 0)::numeric AS monto_vendido_90d,
        -- ★ Mejor mes histórico (columnas informativas del 05)
        mm.anio_mes AS mejor_mes_str,
        COALESCE(mm.mes_total, 0)::numeric AS mejor_mes_uds,
        -- ★ Cantidad de la ÚLTIMA recepción individual (no lifetime).
        COALESCE(uri.ult_recep_qty, 0)::numeric        AS ult_recep_qty,
        -- ★ FIX días sin stock: días reales con stock>0 dentro del ciclo del lote.
        dcs.dias_con_stock                             AS dias_con_stock
    FROM base b
    JOIN offices o   ON o.bsale_office_id = b.bsale_office_id
    JOIN variants v  ON v.bsale_variant_id = b.bsale_variant_id AND v.is_active
    JOIN jerarquia j ON j.bsale_product_id = v.bsale_product_id
    LEFT JOIN stock_sucursal  ss  ON ss.bsale_office_id = b.bsale_office_id AND ss.bsale_variant_id = b.bsale_variant_id
    LEFT JOIN ventas_90d      v90 ON v90.bsale_office_id = b.bsale_office_id AND v90.bsale_variant_id = b.bsale_variant_id
    LEFT JOIN recep_90d       r90 ON r90.bsale_office_id = b.bsale_office_id AND r90.bsale_variant_id = b.bsale_variant_id
    LEFT JOIN primera_recep_total prt ON prt.bsale_office_id = b.bsale_office_id AND prt.bsale_variant_id = b.bsale_variant_id
    LEFT JOIN ventas_post_recep vpr ON vpr.bsale_office_id = b.bsale_office_id AND vpr.bsale_variant_id = b.bsale_variant_id
    LEFT JOIN ventas_lote_total vlt ON vlt.bsale_office_id = b.bsale_office_id AND vlt.bsale_variant_id = b.bsale_variant_id
    LEFT JOIN ventas_total_sku  vts ON vts.bsale_office_id = b.bsale_office_id AND vts.bsale_variant_id = b.bsale_variant_id
    LEFT JOIN ventas_tendencia  vt  ON vt.bsale_office_id  = b.bsale_office_id AND vt.bsale_variant_id  = b.bsale_variant_id
    LEFT JOIN ventas_30d        v30 ON v30.bsale_office_id = b.bsale_office_id AND v30.bsale_variant_id = b.bsale_variant_id
    LEFT JOIN consumos_lifetime cl  ON cl.bsale_office_id  = b.bsale_office_id AND cl.bsale_variant_id  = b.bsale_variant_id
    LEFT JOIN traslados_lifetime tl ON tl.bsale_office_id  = b.bsale_office_id AND tl.bsale_variant_id  = b.bsale_variant_id
    LEFT JOIN ult_recep_info       uri ON uri.bsale_office_id = b.bsale_office_id AND uri.bsale_variant_id = b.bsale_variant_id
    LEFT JOIN dias_con_stock_ciclo dcs ON dcs.bsale_office_id = b.bsale_office_id AND dcs.bsale_variant_id = b.bsale_variant_id
    LEFT JOIN ventas_monto_90d     vm  ON vm.bsale_office_id  = b.bsale_office_id AND vm.bsale_variant_id  = b.bsale_variant_id
    LEFT JOIN mejor_mes            mm  ON mm.bsale_office_id  = b.bsale_office_id AND mm.bsale_variant_id  = b.bsale_variant_id
    WHERE (j.department_id IS NULL OR NOT (j.department_id = ANY(CAST(:excluded_departments AS int[]))))
      AND (j.category_id IS NULL OR NOT (j.category_id = ANY(CAST(:excluded_categories AS int[]))))
),
calc AS (
    SELECT r.*,
        (r.unds_vendidas + r.stock_disponible) AS tdpv,
        CASE WHEN r.ultima_venta IS NOT NULL
             THEN DATE_PART('day', p.ahora - r.ultima_venta)::numeric
             ELSE NULL END AS dias_sin_venta_90d,
        CASE WHEN r.primera_recepcion IS NOT NULL
             THEN DATE_PART('day', p.ahora - r.primera_recepcion)::int
             ELSE NULL END AS edad_dias,
        CASE WHEN r.ultima_recepcion IS NOT NULL
             THEN DATE_PART('day', p.ahora - r.ultima_recepcion)::int
             ELSE NULL END AS dias_desde_ultima_recep,
        -- ★ Días EXHIBIDO: días reales del ciclo actual en que el SKU estuvo
        --   con stock>0 en tienda (disponible para verse/venderse). Es la misma
        --   cifra que alimenta la velocidad, SIN el piso de 7 días (informativa).
        x.dias_exhibido,
        -- ★ dias_efectivos del CICLO ACTUAL.
        --    Inicio del ciclo: primera_recep_90d si hubo recepción en 90d, sino ultima_recepcion lifetime.
        --    Resuelve: Aloe Vera (2 lotes en 8 días = un solo ciclo de 9d, no de 1d).
        --    ★ FIX días sin stock: prioriza `dias_con_stock` (días REALES con stock>0).
        --      Si NULL (sin movimientos), cae a la fórmula vieja (ver dias_con_stock_ciclo).
        GREATEST(p.piso_dias_lote::numeric, x.dias_exhibido) AS dias_efectivos
    FROM radiografia r CROSS JOIN params p
    -- LATERAL: calcula dias_exhibido UNA vez y lo comparte con dias_efectivos
    CROSS JOIN LATERAL (
        SELECT COALESCE(r.dias_con_stock,
            CASE
                WHEN r.stock_disponible = 0
                     AND COALESCE(r.primera_recep_90d, r.ultima_recepcion) IS NOT NULL
                     AND r.ult_venta_lote IS NOT NULL
                THEN (r.ult_venta_lote - COALESCE(r.primera_recep_90d::date, r.ultima_recepcion::date))::numeric
                WHEN COALESCE(r.primera_recep_90d, r.ultima_recepcion) IS NOT NULL
                THEN DATE_PART('day', p.ahora - COALESCE(r.primera_recep_90d, r.ultima_recepcion))::numeric
                ELSE DATE_PART('day', p.ahora - p.fecha_corte)::numeric
            END
        ) AS dias_exhibido
    ) x
),
totales_categoria AS (
    SELECT bsale_office_id, COALESCE(category, '(sin)') AS category,
        SUM(unds_vendidas) AS cat_ventas, SUM(tdpv) AS cat_tdpv
    FROM calc GROUP BY 1, 2
),
metricas AS (
    SELECT c.*, tc.cat_ventas, tc.cat_tdpv,
        -- ★ Velocidad calculada con LOTE COMPLETO (lifetime desde última recep)
        ROUND((c.unds_lote_total / c.dias_efectivos)::numeric, 4) AS ventas_dia,
        ROUND(((c.unds_lote_total / c.dias_efectivos) * 30)::numeric, 2) AS proy_mes,
        -- % Frecuencia usa días con venta del lote completo
        LEAST(100.0, ROUND((c.dias_con_venta_lote::numeric / GREATEST(c.dias_efectivos, 1)) * 100, 2)) AS pct_frecuencia,
        -- Cobertura usa (stock_disponible + stock_reservado) porque el reservado
        -- es inventario que físicamente está pero comprometido. Incluirlo refleja
        -- el inventario REAL antes de comprar más. Impacta cuando hay reservas activas.
        CASE
            WHEN c.unds_lote_total <= 0 THEN NULL
            WHEN (c.stock_disponible + c.stock_reservado) = 0 THEN 0
            ELSE LEAST(9999, CEIL((c.stock_disponible + c.stock_reservado) / (c.unds_lote_total / c.dias_efectivos)))::int
        END AS dias_cobertura,
        -- % Sell-Through Lifetime: V_lifetime / R_lifetime, capeado a 999.99 (lo proyecta el 05)
        CASE WHEN c.unds_recibidas_lifetime > 0
             THEN LEAST(999.99, ROUND((c.unds_vendidas_lifetime / c.unds_recibidas_lifetime * 100)::numeric, 1))
             ELSE NULL END AS pct_sellthrough_lifetime,
        -- ★ Días con stock en los últimos 30d (Opción 2: no penaliza por agotamiento).
        --    - Si aún tiene stock: 30 días completos
        --    - Si se agotó: días desde (hoy-30) hasta la última venta del lote
        --    - Si nunca vendió o se agotó hace >30d: 0
        CASE
            WHEN c.stock_disponible > 0 THEN 30
            WHEN c.ult_venta_lote IS NULL THEN 0
            WHEN c.ult_venta_lote >= ((SELECT ahora FROM params) - ((SELECT ventana_recent_dias FROM params) * INTERVAL '1 day'))::date
                 THEN GREATEST(1, (c.ult_venta_lote - ((SELECT ahora FROM params) - ((SELECT ventana_recent_dias FROM params) * INTERVAL '1 day'))::date + 1))::int
            ELSE 0
        END AS dias_con_stock_30d
    FROM calc c
    LEFT JOIN totales_categoria tc
      ON tc.bsale_office_id = c.bsale_office_id
     AND tc.category = COALESCE(c.category, '(sin)')
),
-- ★ Velocidad reciente (últimos 30d) — Opción 2: solo cuenta días con stock.
--    Calculada en CTE separada para reutilizar dias_con_stock_30d y mantener
--    legibilidad en el SELECT final.
metricas_reciente AS (
    SELECT m.*,
        CASE
            WHEN m.dias_con_stock_30d > 0
                 AND m.unds_vendidas_30d > 0
            THEN ROUND((m.unds_vendidas_30d / m.dias_con_stock_30d)::numeric, 4)
            ELSE NULL
        END AS vel_30d,
        CASE
            WHEN m.dias_con_stock_30d > 0
                 AND m.unds_vendidas_30d > 0
            THEN ROUND(((m.unds_vendidas_30d / m.dias_con_stock_30d) * 30)::numeric, 2)
            ELSE NULL
        END AS proy_30d_reciente,
        -- ★ FIX P17 (2026-06-06): COBERTURA basada en velocidad RECIENTE.
        --   Cuando hay venta en los últimos 30d con stock, la cobertura
        --   refleja el ritmo de HOY, no del lote completo. Fallback al
        --   cálculo lifetime (m.dias_cobertura) si no hay datos recientes.
        --   Esto hace que SKUs acelerando se detecten más rápido como
        --   urgentes, y SKUs desacelerando muestren la realidad operativa.
        CASE
            WHEN m.unds_lote_total <= 0 THEN m.dias_cobertura
            WHEN (m.stock_disponible + m.stock_reservado) = 0 THEN 0
            WHEN m.unds_vendidas_30d > 0 AND m.dias_con_stock_30d > 0
                THEN LEAST(9999, CEIL(
                    (m.stock_disponible + m.stock_reservado) /
                    (m.unds_vendidas_30d::numeric / m.dias_con_stock_30d)
                ))::int
            ELSE m.dias_cobertura
        END AS dias_cobertura_reciente,
        -- ★ FIX P24 (2026-06-15): RITMO POST-REPOSICIÓN — variables NUMÉRICAS
        --   para la cascada (las columnas "Proy/Cob Post-Recep" son su versión
        --   de display). Sirven para detectar "lote fresco que ya está volando":
        --   un Taper que llegó hace 12d y ya vendió 14 unds tiene ritmo real
        --   ~32/mes pero el proy del ciclo lo diluye en ~21 (ver caso 1000-2).
        --   Solo definido si hubo recepción Y hubo venta post-recep > 0.
        CASE
            WHEN m.dias_desde_ultima_recep IS NULL OR m.unds_post_recep <= 0 THEN NULL
            ELSE ROUND((m.unds_post_recep / GREATEST(1, m.dias_desde_ultima_recep)::numeric * 30)::numeric, 2)
        END AS proy_post_recep,
        CASE
            WHEN m.dias_desde_ultima_recep IS NULL OR m.unds_post_recep <= 0 THEN NULL
            WHEN (m.stock_disponible + m.stock_reservado) <= 0 THEN 0
            ELSE LEAST(9999, CEIL(
                (m.stock_disponible + m.stock_reservado) /
                (m.unds_post_recep / GREATEST(1, m.dias_desde_ultima_recep)::numeric)
            ))::int
        END AS cob_post_recep,
        -- ★ FIX P25 (2026-06-15): DÍAS ABSORCIÓN LOTE (numérico para cascada).
        --   Distingue bestseller real (lote agotado ≤45d) de nicho lento que se
        --   acabó (>45d). Caso testigo: Espátula EP-9534 — vendió 188 unds en 236
        --   días (0.8/día), agotada hace 5 meses. Antes el sistema decía
        --   "💎 OPORTUNIDAD PERDIDA — REPONER YA" (falso positivo: no es
        --   bestseller, no hay venta perdida diaria). Con este campo cae en
        --   DEMANDA EXTINTA = NO REPONER.
        CASE
            WHEN m.ult_venta_lote IS NOT NULL
                 AND COALESCE(m.primera_recep_90d, m.ultima_recepcion) IS NOT NULL
            THEN GREATEST(0, (m.ult_venta_lote::date
                              - COALESCE(m.primera_recep_90d::date, m.ultima_recepcion::date)))::int
            ELSE NULL
        END AS dias_absorcion_lote
    FROM metricas m
),
-- ★ Umbral adaptativo de velocidad por CATEGORÍA.
--   En categorías de baja rotación (perfumes, muebles, etc.) un proy=5/mes
--   puede ser "exitoso" para ese perfil; no debe caer en BAJA ROTACIÓN.
--   Fórmula: umbral = MAX(3, MIN(10, avg_cat * 0.5)).
--   La columna del JOIN se llama `cat_name` (no `category`) para evitar
--   colisión con la columna `category` que viene heredada en metricas_reciente.
cat_baseline AS (
    SELECT bsale_office_id, COALESCE(category, '(sin)') AS cat_name,
           AVG(proy_mes) AS avg_proy_cat
    FROM metricas_reciente
    WHERE proy_mes > 0
    GROUP BY 1, 2
),
-- ★ Sugerencia de transferencia inter-sucursal.
-- Detecta SKUs con EXCESO en una sucursal (cob >90d) Y DÉFICIT en la otra
-- (stock < demanda mensual con proy ≥10). Calcula cuántas unidades transferir.
transferencias AS (
    SELECT
        donor.bsale_office_id  AS donor_office,
        donor.bsale_variant_id AS variant_id,
        recip.sucursal         AS sucursal_receptora,
        -- Cantidad sugerida: déficit del receptor, capado al exceso del donante.
        -- Mantiene 30 días de cobertura en el donante después de la transferencia.
        GREATEST(0, LEAST(
            FLOOR(donor.stock_disponible - donor.proy_mes)::int,   -- excedente sobre 1 mes de demanda
            CEIL(recip.proy_mes - recip.stock_disponible)::int     -- déficit del receptor
        )) AS unidades_sugeridas
    FROM metricas_reciente donor
    JOIN metricas_reciente recip
      ON donor.bsale_variant_id = recip.bsale_variant_id
     AND donor.bsale_office_id  <> recip.bsale_office_id
    WHERE donor.dias_cobertura > 90                                 -- donante con exceso
      AND donor.proy_mes > 0
      AND recip.stock_disponible < recip.proy_mes                   -- receptor con déficit
      AND recip.proy_mes >= 10                                      -- receptor vende constante
      AND donor.stock_disponible - donor.proy_mes >= 5              -- mínimo 5 unds para mover
),
-- ════════════════════════════════════════════════════════════════════════
-- matriz: una fila por SKU×sucursal con TODAS las columnas de display y las
-- crudas que necesitan los SELECT finales por módulo. Los módulos (04/04b/05)
-- solo proyectan/filtran sobre esta CTE — la lógica de negocio termina aquí.
-- ════════════════════════════════════════════════════════════════════════
matriz AS (
SELECT
    sucursal                 AS "Sucursal",
    department               AS "Departamento",
    category                 AS "Categoría",
    subcategory              AS "Subcategoría",
    display_code             AS "Código SKU",
    product_name             AS "Producto",
    primera_recepcion::date  AS "1ª Recepción",
    ultima_recepcion::date   AS "Últ. Recepción",
    ultima_venta             AS "Últ. Venta (90d)",
    edad_dias                AS "Edad SKU (días)",
    dias_desde_ultima_recep  AS "Llegó hace (días)",
    -- ★ Días EXHIBIDO (2026-06-11): días reales con stock>0 en tienda dentro del
    --   ciclo del lote actual. Complementa "Llegó hace": un lote puede llevar 60d
    --   en tienda pero solo 20 exhibido (el resto agotado). Puede superar a
    --   "Llegó hace" cuando el ciclo arrancó en una recepción anterior a la última.
    GREATEST(0, dias_exhibido)::int          AS "Días Exhibido",

    -- Bloque 90d (ventana operativa: actividad reciente)
    trim_scale(ROUND(unds_vendidas, 2))      AS "Unds Vend (90d)",
    trim_scale(ROUND(unds_recibidas_90d, 2)) AS "Unds Recib (90d)",
    -- ★ Bloque LOTE COMPLETO (comportamiento real desde la última recepción)
    trim_scale(ROUND(unds_lote_total, 2))    AS "Vend Lote Total",
    -- ★ FIX P18: sell-through del lote actual.
    ROUND((unds_lote_total / NULLIF(unds_lote_total + stock_disponible, 0) * 100)::numeric, 1)
                                             AS "Sell-through Lote %",
    -- ★ FIX P19 (2026-06-08) + P26 (2026-06-15): VIDA DEL LOTE.
    --   - Si stock=0 con absorción medida: la vida REAL del lote fue lo que
    --     tardó en venderse (caso testigo MS-3507 Magdalena: 110 unds en 28d
    --     y luego 57d agotado → la vida del lote fueron 28d, NO 85d).
    --   - Si stock>0: vida = días vividos + días que durará al ritmo reciente
    --     (proyección de cuándo se acabará).
    --   - Si recep desconocida: NULL.
    CASE
        WHEN dias_desde_ultima_recep IS NULL THEN NULL
        WHEN stock_disponible = 0 AND dias_absorcion_lote IS NOT NULL THEN dias_absorcion_lote
        WHEN dias_cobertura_reciente IS NULL THEN dias_desde_ultima_recep
        ELSE dias_desde_ultima_recep + dias_cobertura_reciente
    END                                      AS "Vida lote (días)",
    -- ★ FIX P26 (2026-06-15): DÍAS AGOTADO. Cuánto lleva sin stock un SKU desde
    --   que se acabó su lote. Hace explícita la cuenta: Llegó hace − Absorción.
    --   Para MS-3507 Mag: 85 − 28 = 57d agotado (lote bestseller olvidado).
    --   NULL si hay stock o no se puede calcular la absorción.
    CASE
        WHEN stock_disponible > 0 THEN NULL
        WHEN dias_desde_ultima_recep IS NULL OR dias_absorcion_lote IS NULL THEN NULL
        ELSE GREATEST(0, dias_desde_ultima_recep - dias_absorcion_lote)::int
    END                                      AS "Días Agotado",
    -- ★ Días ABSORCIÓN LOTE (informativa, 2026-06-15): días calendario desde el
    --   inicio del ciclo (1ª recep en 90d, o última recep lifetime) hasta la
    --   última venta del lote. Distingue "vendió rápido (≤45d)" de "se agotó lento
    --   (meses)". INFORMATIVA: todavía NO entra en la cascada de clasificación.
    --   Si Absorción ≫ "Días Exhibido", el lote pasó días agotado entre medio.
    CASE
        WHEN ult_venta_lote IS NOT NULL
             AND COALESCE(primera_recep_90d, ultima_recepcion) IS NOT NULL
        THEN GREATEST(0, (ult_venta_lote::date
                          - COALESCE(primera_recep_90d::date, ultima_recepcion::date)))::int
        ELSE NULL
    END                                      AS "Días Absorción Lote",
    ult_venta_lote                           AS "Últ. Venta Lote",
    pri_venta_lote                           AS "1ª Venta Lote",
    trim_scale(ROUND(stock_disponible, 2))   AS "Stock Disp",
    trim_scale(ROUND(stock_reservado, 2))    AS "Stock Reserv",
    -- ★ P22: stock disponible en almacén central (0 = no hay backup; si >0 y la
    --   tienda está baja, la acción correcta es TRASLADAR, no comprar).
    COALESCE(sa.stock_almacen, 0)            AS "Stock Almacén",
    -- Velocidad: precisión dinámica. Productos con vel <0.1/día usan 4 decimales
    -- para mantener Vel×30=Proy y Stk/Vel=Cob visualmente consistentes.
    -- Productos con vel ≥0.1/día mantienen 2 decimales (legibilidad).
    trim_scale(CASE WHEN ventas_dia < 0.1
                    THEN ROUND(ventas_dia, 4)
                    ELSE ROUND(ventas_dia, 2)
               END)                          AS "Velocidad (uds/día)",
    trim_scale(ROUND(proy_mes, 2))           AS "Proyección 30d",
    -- ★ Velocidad RECIENTE (últimos 30d, solo días con stock — Opción 2).
    --    Refleja cómo rota el SKU AHORA, sin diluirse con la "cola larga" del lote.
    --    Útil para decisiones de reposición: si vel_30d < ventas_dia → demanda cayó.
    trim_scale(CASE WHEN vel_30d IS NULL THEN NULL
                    WHEN vel_30d < 0.1 THEN ROUND(vel_30d, 4)
                    ELSE ROUND(vel_30d, 2)
               END)                          AS "Vel últimos 30d",
    trim_scale(ROUND(proy_30d_reciente, 2))  AS "Proy 30d (reciente)",
    -- ★ Proy/Cob POST-RECEP (FIX P24, 2026-06-15): ritmo del LOTE FRESCO desde la
    --   última recepción. Resuelve "lote llegó hace pocos días y ya está volando"
    --   (caso 1000-2 Taper: llegó 12d, vendió 14 → 32/mes, pero proy_ciclo=21).
    --   Las variables NUMÉRICAS viven en metricas_reciente; acá solo se proyectan
    --   para display. NULL = no hubo recep o no hubo venta post-recep (no inventar).
    trim_scale(proy_post_recep)              AS "Proy Post-Recep",
    cob_post_recep                           AS "Cob Post-Recep",
    trim_scale(ROUND(pct_frecuencia, 1))     AS "% Frecuencia",
    dias_con_venta_lote                      AS "Días con Venta",
    dias_sin_venta_90d::int                  AS "Días sin Vender",

    CASE
        -- FIX: "Sin datos" debe depender de si el lote tuvo ventas, no de
        -- unds_post_recep (deprecada), que marcaba "Sin datos" de más.
        WHEN dias_con_venta_lote = 0 THEN 'Sin datos'
        WHEN pct_frecuencia >= (SELECT xyz_constante_pct FROM params)  THEN 'X (Constante)'
        WHEN pct_frecuencia >= (SELECT xyz_variable_pct FROM params)   THEN 'Y (Variable)'
        ELSE 'Z (Errático / Ráfaga)'
    END                       AS "XYZ",
    CASE
        -- ★ P22: muestra la cobertura RECIENTE (la misma que usa la cascada de
        --   clasificación post-P17) — antes mostraba la lifetime y en 258 filas
        --   caía en banda distinta a la que clasificó (confundía al usuario).
        --   stock=0 SIEMPRE dice 'Agotado' (antes mostraba 's/d' en 214 filas).
        WHEN stock_disponible <= 0           THEN 'Agotado'
        WHEN dias_cobertura_reciente IS NULL THEN 's/d'
        WHEN dias_cobertura_reciente = 0     THEN 'Agotado'
        WHEN dias_cobertura_reciente >= 9999 THEN '+999 días'
        ELSE dias_cobertura_reciente || ' días'
    END                       AS "Cobertura",
    -- % Rotación del Stock: V90 / (V90 + Stock). Mide qué fracción del inventario
    -- disponible se rotó. Por construcción ≤100% (cuando stock=0 da 100%).
    CASE WHEN tdpv > 0
         THEN trim_scale(ROUND((unds_vendidas / tdpv * 100)::numeric, 1))
         ELSE 0::numeric END   AS "% Rotación Stock",
    -- % Demanda vs Reposición: V90 / R90 (puede ser >100% si vendió más de lo
    -- recibido — indica demanda excedió reposición y agotó stock previo).
    -- Es la métrica que REALMENTE detecta "demanda insatisfecha".
    CASE WHEN unds_recibidas_90d > 0
         THEN trim_scale(ROUND((unds_vendidas / unds_recibidas_90d * 100)::numeric, 1))
         ELSE NULL::numeric END AS "% Demanda vs Reposición",

    -- Tendencia: compara ventas últimos 45d vs los 45d previos (dentro de 90d total).
    -- 📈 Creciendo: v_recent >1.5x v_old (claramente subiendo)
    -- 📉 Decayendo: v_recent <0.7x v_old (claramente bajando)
    -- → Estable: relación 0.7-1.5x
    -- 🆕 Inicio: sin ventas previas, solo recientes (producto nuevo o reactivado)
    -- 💤 Pausado: solo ventas previas, ninguna reciente (en pausa)
    CASE
        -- ★ FIX P7 (2026-06-08): si stock=0, la "tendencia" mecánica
        --   v_recent vs v_old miente — recent=0 porque no hay stock, NO
        --   porque la demanda cayó. Mostramos "💤 Agotado" para que el
        --   usuario no vea "BESTSELLER ACTIVO" + "📉 Decayendo".
        WHEN stock_disponible = 0 AND unds_vendidas > 0 THEN '💤 Agotado'
        WHEN v_recent_45d = 0 AND v_old_45d = 0 THEN '—'
        WHEN v_old_45d = 0 AND v_recent_45d > 0 THEN '🆕 Inicio'
        WHEN v_recent_45d = 0 AND v_old_45d > 0 THEN '💤 Pausado'
        WHEN v_recent_45d > v_old_45d * (SELECT trend_grow_mult FROM params) THEN '📈 Creciendo'
        WHEN v_recent_45d < v_old_45d * (SELECT trend_decay_mult FROM params) THEN '📉 Decayendo'
        ELSE '→ Estable'
    END AS "Tendencia",

    -- Sugerencia de transferencia: si esta sucursal tiene exceso del SKU mientras
    -- la otra tiene déficit, sugiere cuántas unidades mover.
    CASE
        WHEN t.unidades_sugeridas IS NOT NULL AND t.unidades_sugeridas >= 5
        THEN '→ Transferir ' || t.unidades_sugeridas || ' a ' || t.sucursal_receptora
        ELSE NULL
    END AS "Sugerencia Transferencia",

    CASE
        -- ════════════════════════════════════════════════════════════════════
        -- SECCIÓN A · CASOS ESPECIALES (se evalúan primero — sobreescriben todo)
        --   Atrapan situaciones operativas que cambian la lectura del producto.
        -- ════════════════════════════════════════════════════════════════════

        -- 🌱 NUEVO: producto en ventana de gracia (≤7d desde 1ª recepción).
        --    Hasta que pase la semana no podemos juzgar rotación.
        WHEN primera_recepcion >= NOW() - ((SELECT piso_dias_lote FROM params) * INTERVAL '1 day')
             AND unds_vendidas < 15
             THEN '🌱 PRODUCTO NUEVO — ESPERAR: recién llegado (≤7d), aún no se puede evaluar rotación'

        -- ✅ TEMPORADA CERRADA: depto estacional, fuera de campaña, agotado.
        WHEN department_id = ANY(CAST(:seasonal_departments AS int[]))
             AND COALESCE(dias_sin_venta_90d, 9999) > (SELECT ventana_recent_dias FROM params)
             AND stock_disponible = 0
             AND unds_vendidas_lifetime >= 1
             THEN '✅ TEMPORADA CERRADA OK — RECOMPRAR PRÓXIMA CAMPAÑA: estacional que vendió su ciclo y se agotó'

        -- 📦 SOBRANTE DE CAMPAÑA: depto estacional, fuera de campaña, con stock.
        WHEN department_id = ANY(CAST(:seasonal_departments AS int[]))
             AND COALESCE(dias_sin_venta_90d, 9999) > (SELECT ventana_recent_dias FROM params)
             AND stock_disponible > 0
             THEN '📦 SALDO DE TEMPORADA — GUARDAR: stock sobrante de campaña pasada, NO liquidar'

        -- ⛔ PÉRDIDA TOTAL: vendió <20% lifetime + consumos dominan (no traslados).
        --    El stock salió por merma, no por venta. Revisar control físico.
        WHEN stock_disponible = 0
             AND unds_recibidas_lifetime >= 5
             AND unds_consumidas_lifetime >= unds_recibidas_lifetime * (SELECT loss_consumo_ratio FROM params)
             AND unds_vendidas_lifetime < unds_recibidas_lifetime * (SELECT loss_venta_ratio FROM params)
             AND unds_consumidas_lifetime > unds_trasladadas_lifetime
             THEN '⛔ PÉRDIDA DE STOCK — REVISAR CONTROL FÍSICO: casi todo el stock se ajustó/perdió (mermas o robos)'

        -- ⚠️ VENTAS CON PÉRDIDA: vendió 20-50% lifetime + consumos dominan.
        WHEN stock_disponible = 0
             AND unds_recibidas_lifetime >= 5
             AND unds_consumidas_lifetime > unds_vendidas_lifetime
             AND unds_vendidas_lifetime >= unds_recibidas_lifetime * (SELECT loss_venta_ratio FROM params)
             AND unds_vendidas_lifetime < unds_recibidas_lifetime * (SELECT loss_consumo_ratio FROM params)
             AND unds_consumidas_lifetime > unds_trasladadas_lifetime
             THEN '⚠️ VENDIÓ Y SE PERDIÓ — INVESTIGAR: hay ventas pero también mermas grandes (revisar inventario)'

        -- ════════════════════════════════════════════════════════════════════
        -- SECCIÓN B · STOCK = 0 · VENDIÓ TODO (sell-through lifetime ≥80%)
        --   Criterio común: el SKU logró colocar todo lo recibido. Dentro de
        --   este grupo distinguimos según VELOCIDAD (proy_mes) y RECENCIA (dsv).
        --   Sell-through = (vendido + consumido + trasladado) / recibido lifetime.
        --   El proxy_mes mide "qué tan rápido salieron las unidades del lote".
        -- ════════════════════════════════════════════════════════════════════

        -- 🔥💎 EXITOSO ACTIVO: vendió todo + velocidad ≥10/mes + venta reciente.
        --    ★ FIX P25 (2026-06-15): + dias_absorcion ≤45d (verdadero bestseller).
        --    Sin este filtro, lentos que se agotaron en meses (caso Espátula EP-9534:
        --    188 unds en 236 días = 0.8/día) se colaban como "BESTSELLER ACTIVO".
        --    Si la absorción no se puede calcular (NULL), preservamos behavior viejo.
        WHEN stock_disponible = 0
             AND unds_recibidas_lifetime >= 2
             AND (unds_vendidas_lifetime + unds_consumidas_lifetime + unds_trasladadas_lifetime) >= unds_recibidas_lifetime * (SELECT sellthrough_exito_ratio FROM params)
             AND (dias_absorcion_lote IS NULL OR dias_absorcion_lote <= (SELECT dias_absorcion_bestseller_max FROM params))
             AND proy_mes >= GREATEST((SELECT proy_mes_min_floor FROM params), LEAST((SELECT proy_mes_min_cap FROM params), COALESCE(cb.avg_proy_cat, (SELECT proy_mes_min_cap FROM params)) * (SELECT proy_mes_cat_ratio FROM params)))
             AND COALESCE(dias_sin_venta_90d, 9999) <= (SELECT ventana_recent_dias FROM params)
             THEN '🔥 BESTSELLER ACTIVO — REPONER YA: vendió todo RÁPIDO con demanda fuerte y sigue rotando — reposición urgente'

        -- ⏸️ BESTSELLER AGOTADO 1-2 MESES: vendió todo + velocidad alta, agotado
        --    hace 31-60d sin reposición. ★ P21 (2026-06-10): antes decía "la
        --    demanda se enfrió" — FALSO: sin stock la demanda no se puede medir.
        --    El dsv de un SKU agotado mide "días sin reponer", no enfriamiento.
        --    Los estacionales ya fueron capturados por TEMPORADA CERRADA (sección A),
        --    así que lo que llega acá es no-estacional → acción: REPONER.
        --    Caso testigo: GALLETAS 77204702130714 (30 unds en 9d, 30 en 24d,
        --    agotado 34d → decía "demanda se enfrió" siendo quiebre sin reponer).
        --    ★ FIX P25 (2026-06-15): + dias_absorcion ≤45d (bestseller real).
        WHEN stock_disponible = 0
             AND unds_recibidas_lifetime >= 2
             AND (unds_vendidas_lifetime + unds_consumidas_lifetime + unds_trasladadas_lifetime) >= unds_recibidas_lifetime * (SELECT sellthrough_exito_ratio FROM params)
             AND (dias_absorcion_lote IS NULL OR dias_absorcion_lote <= (SELECT dias_absorcion_bestseller_max FROM params))
             AND proy_mes >= GREATEST((SELECT proy_mes_min_floor FROM params), LEAST((SELECT proy_mes_min_cap FROM params), COALESCE(cb.avg_proy_cat, (SELECT proy_mes_min_cap FROM params)) * (SELECT proy_mes_cat_ratio FROM params)))
             AND COALESCE(dias_sin_venta_90d, 9999) <= (SELECT ventana_dead_dias FROM params)
             THEN '⏸️ BESTSELLER RÁPIDO AGOTADO 1-2 MESES — REPONER: vendió todo a buen ritmo y lleva 31-60 días sin reposición (confirmar con proveedor)'

        -- 💎 EXITOSO OLVIDADO: vendió todo + velocidad alta + >60d sin VENTA.
        --    Patrón: lote agotado RÁPIDO + nadie repuso → lleva >60d sin vender.
        --    Es OPORTUNIDAD perdida (no demanda extinta). Caso típico: B1045
        --    Asamblea (25 unds en 21d, luego 62d sin venta). Acción: REPONER.
        --    (gate real: dias_sin_venta_90d > 60, heredado de no matchear PASADO ≤60)
        --    ★ FIX P25 revertido: se eliminó dias_absorcion_lote <= 45 porque 
        --    castigaba a productos exitosos que tenían lotes grandes. La velocidad 
        --    real la garantiza la validación de proy_mes.
        WHEN stock_disponible = 0
             AND unds_recibidas_lifetime >= 2
             AND (unds_vendidas_lifetime + unds_consumidas_lifetime + unds_trasladadas_lifetime) >= unds_recibidas_lifetime * (SELECT sellthrough_exito_ratio FROM params)
             AND proy_mes >= GREATEST((SELECT proy_mes_min_floor FROM params), LEAST((SELECT proy_mes_min_cap FROM params), COALESCE(cb.avg_proy_cat, (SELECT proy_mes_min_cap FROM params)) * (SELECT proy_mes_cat_ratio FROM params)))
             THEN '💎 OPORTUNIDAD PERDIDA — REPONER YA: lote RÁPIDO y ya van +60d sin reabastecer (venta perdida diaria)'

        -- 🐢 ROTACIÓN LENTA SANA: vendió todo + lento (<10/mes) + aún viva (≤60d).
        --    Vende constante pero modesto — reponer cantidades chicas.
        WHEN stock_disponible = 0
             AND unds_recibidas_lifetime >= 2
             AND (unds_vendidas_lifetime + unds_consumidas_lifetime + unds_trasladadas_lifetime) >= unds_recibidas_lifetime * (SELECT sellthrough_exito_ratio FROM params)
             AND COALESCE(dias_sin_venta_90d, 9999) <= (SELECT ventana_dead_dias FROM params)
             AND proy_mes < GREATEST((SELECT proy_mes_min_floor FROM params), LEAST((SELECT proy_mes_min_cap FROM params), COALESCE(cb.avg_proy_cat, (SELECT proy_mes_min_cap FROM params)) * (SELECT proy_mes_cat_ratio FROM params)))
             THEN '🐢 LENTO PERO CONSTANTE — REPONER POCO: producto nicho que se agotó vendiendo despacio pero seguido'

        -- 💤 DEMANDA EXTINTA: vendió todo pero >60d sin venta.
        --    No reponer — la demanda murió.
        WHEN stock_disponible = 0
             AND unds_recibidas_lifetime >= 2
             AND (unds_vendidas_lifetime + unds_consumidas_lifetime + unds_trasladadas_lifetime) >= unds_recibidas_lifetime * (SELECT sellthrough_exito_ratio FROM params)
             THEN '💤 DEMANDA EXTINTA — NO REPONER: vendió todo pero +60d sin demanda (descatalogar)'

        -- ════════════════════════════════════════════════════════════════════
        -- SECCIÓN C · STOCK = 0 · NO VENDIÓ TODO (sell-through lifetime <80%)
        --   El SKU no colocó todo lo recibido. Distinguimos por VOLUMEN (V_life
        --   y V90), EDAD (edad_dias) y RECENCIA (dsv).
        -- ════════════════════════════════════════════════════════════════════

        -- 🚨 QUIEBRE STOCK: alta rotación + sin stock + venta muy reciente.
        --    Aunque lifetime sea bajo, AHORA está caliente. Comprar ya.
        WHEN stock_disponible = 0
             AND COALESCE(dias_sin_venta_90d, 9999) <= (SELECT dsv_quiebre_max_dias FROM params)
             AND proy_mes >= GREATEST((SELECT proy_mes_min_floor FROM params), LEAST((SELECT proy_mes_min_cap FROM params), COALESCE(cb.avg_proy_cat, (SELECT proy_mes_min_cap FROM params)) * (SELECT proy_mes_cat_ratio FROM params)))
             THEN '🚨 QUIEBRE DE BESTSELLER — COMPRAR YA: alta rotación sin stock (cada día sin stock es venta perdida)'

        -- 👻 AGOTADO POTENCIAL ACTIVO: vendió ≥50 lifetime + V90≥5 + dsv≥15.
        --    Tuvo demanda real lifetime y aún se mueve algo. Reponer prioridad.
        WHEN stock_disponible = 0
             AND COALESCE(dias_sin_venta_90d, 9999) > (SELECT dsv_quiebre_max_dias FROM params)
             AND unds_vendidas_lifetime >= (SELECT lifetime_bestseller_min FROM params)
             AND unds_vendidas >= 5
             THEN '✨ AGOTADO CON DEMANDA — REPONER: vendió ≥50 unds en su vida y la demanda continúa activa'

        -- 💤 AGOTADO HISTÓRICO: vendió ≥50 lifetime + dsv≥15 pero V90 bajo.
        --    Vendió bien en su vida pero hoy ya casi no rota. Evaluar descatalogar.
        WHEN stock_disponible = 0
             AND COALESCE(dias_sin_venta_90d, 9999) > (SELECT dsv_quiebre_max_dias FROM params)
             AND unds_vendidas_lifetime >= (SELECT lifetime_bestseller_min FROM params)
             THEN '📉 EX-BESTSELLER ENFRIADO — EVALUAR: vendía bien pero la demanda cayó (ver si vale reabastecer)'

        -- 🌿 PRODUCTO EMERGENTE: V90≥15 pero lifetime corto (<50).
        --    Producto reciente con tracción. Evaluar reposición.
        WHEN stock_disponible = 0
             AND COALESCE(dias_sin_venta_90d, 9999) > (SELECT dsv_quiebre_max_dias FROM params)
             AND unds_vendidas >= 15
             THEN '🌿 PRODUCTO EMERGENTE — VIGILAR: vendió bien en 90d pero con historial corto (observar antes de reponer fuerte)'

        -- 🪦 RESIDUO HISTÓRICO: viejo (>180d) + nunca rotó + recep marginal.
        --    Candidato a descatalogar.
        WHEN stock_disponible = 0 AND unds_vendidas = 0
             AND unds_recibidas_90d BETWEEN 1 AND 4
             AND edad_dias > (SELECT ventana_blind_spot_dias FROM params)
             AND unds_vendidas_lifetime < (SELECT lifetime_bestseller_min FROM params)
             THEN '🪦 PRODUCTO MUERTO — DESCATALOGAR: producto antiguo prácticamente sin rotación (sacar del catálogo)'

        -- ❓ RECIBIDO Y NO VENDIDO: recibió en 90d pero nada se vendió.
        --    Casi seguro mermas/transferencias no documentadas. Revisar.
        WHEN stock_disponible = 0 AND unds_vendidas = 0 AND unds_recibidas_90d > 0
             AND unds_vendidas_lifetime < (SELECT lifetime_bestseller_min FROM params)
             THEN '❓ RECIBIDO Y NO VENDIDO: revisar (mermas/transferencias)'

        -- 🪦 AGOTADO MARGINAL: catch-all para agotados con bajo volumen lifetime.
        WHEN stock_disponible = 0 AND COALESCE(dias_sin_venta_90d, 9999) > (SELECT dsv_quiebre_max_dias FROM params)
             THEN '🪦 BAJO VOLUMEN AGOTADO — DESCATALOGAR: vendió menos de 50 unds en toda su vida'

        -- 👻 FALSO AGOTADO: catch-all final stock=0 con velocidad baja.
        --    Vendió poco lifetime y poco proy_mes — no priorizar reposición.
        WHEN stock_disponible = 0 AND proy_mes < GREATEST((SELECT proy_mes_min_floor FROM params), LEAST((SELECT proy_mes_min_cap FROM params), COALESCE(cb.avg_proy_cat, (SELECT proy_mes_min_cap FROM params)) * (SELECT proy_mes_cat_ratio FROM params)))
             THEN '👻 AGOTADO NO PRIORITARIO: sin stock pero la rotación era muy baja (no urgente reabastecer)'

        -- ════════════════════════════════════════════════════════════════════
        -- SECCIÓN D · STOCK > 0 · SIN VENTAS EN 90D
        -- ════════════════════════════════════════════════════════════════════

        -- 🔄 REABASTECIDO RECIENTE: recibió ≤14d, aún sin venta — esperar.
        --    No es MUERTO — apenas tuvo tiempo de estar en góndola.
        WHEN stock_disponible > 0
             AND unds_vendidas = 0
             AND dias_desde_ultima_recep IS NOT NULL
             AND dias_desde_ultima_recep <= (SELECT recien_reabastecido_dias FROM params)
             THEN '🔄 STOCK RECIÉN LLEGADO — ESPERAR: recepción nueva (≤14d) sin ventas todavía (normal, dejar madurar)'

        -- 💀 MUERTO 90D: stock parado sin ventas en 90 días = capital estancado.
        --    Estricto: el stock actual NUNCA vendió en 90d (unds_vendidas=0).
        --    Casos como "vendió antes pero quedó stock sin moverse" NO entran aquí
        --    porque sí hubo venta (aunque vieja); se tratan en otras reglas.
        WHEN stock_disponible > 0 AND unds_vendidas = 0
             THEN '💀 STOCK PARADO 90 DÍAS — LIQUIDAR: hay stock pero no se mueve hace 3 meses (capital atrapado)'

        -- ════════════════════════════════════════════════════════════════════
        -- SECCIÓN E · STOCK > 0 · CON VENTAS — Discrimina por VELOCIDAD y COBERTURA
        --   Dimensiones primarias: proy_mes (velocidad), dias_cobertura_reciente (días que dura
        --   el stock al ritmo actual), tendencia (v_recent_45d vs v_old_45d).
        -- ════════════════════════════════════════════════════════════════════

        -- 👀 ALERTA VISUAL: stock 1-2 unds + 16-59d sin movimiento.
        --    Caso particular: tan poco stock que puede estar perdido en góndola.
        WHEN stock_disponible BETWEEN 1 AND 2 AND dias_sin_venta_90d BETWEEN 16 AND 59
             THEN '👀 STOCK BAJO QUIETO — VERIFICAR EN TIENDA: 1-2 unds sin movimiento en semanas (chequear visibilidad/vencimiento)'

        -- 🔄 REABASTECIDO ACTIVO: stock fresco (≤14d) + cobertura aparente alta (>45d)
        --    pero la velocidad real o el historial dicen que rota bien.
        WHEN stock_disponible > 0
             AND dias_desde_ultima_recep IS NOT NULL
             AND dias_desde_ultima_recep <= (SELECT recien_reabastecido_dias FROM params)
             AND dias_cobertura_reciente > (SELECT cobertura_objetivo_dias FROM params)
             AND (
                 (unds_vendidas_30d >= 2
                  AND (unds_vendidas_30d::numeric / GREATEST(1, dias_desde_ultima_recep::numeric)) * 30 >= 10)
                 OR
                 (unds_vendidas_lifetime >= 10
                  AND unds_recibidas_lifetime > 0
                  AND unds_vendidas_lifetime >= unds_recibidas_lifetime * 0.70)
             )
             THEN '🔄 LOTE NUEVO VENDIENDO BIEN: llegó stock grande y ya rota — sano (la cobertura alta es por dilución)'

        -- 🆕 RECIÉN REABASTECIDO: lote nuevo (≤7d) + tiene historial + cob alta por
        -- el nuevo lote. No es exceso real — todavía no le dimos tiempo de vender.
        -- Esperar primera semana antes de evaluar.
        WHEN stock_disponible > 0
             AND dias_desde_ultima_recep IS NOT NULL
             AND dias_desde_ultima_recep <= (SELECT piso_dias_lote FROM params)
             AND unds_vendidas_lifetime >= 1
             AND dias_cobertura_reciente > (SELECT cobertura_objetivo_dias FROM params)
             THEN '🆕 RECIÉN REABASTECIDO — ESPERAR 1 SEMANA: lote nuevo (≤7d), todavía no se puede evaluar bien'

        -- ════════════════════════════════════════════════════════════════════
        -- ★ FIX P24 (2026-06-15) · LOTE FRESCO VENDIENDO RÁPIDO
        --   Detecta SKUs donde el lote actual (llegó ≤30d) ya está volando, pero
        --   el proy del ciclo está DILUIDO por días anteriores al restock.
        --   Caso testigo: Taper 1000-2 Magdalena — llegó hace 12d, vendió 14
        --   (post-recep = 32/mes, cob real 11d) pero proy_ciclo = 21/mes → el
        --   sistema lo decía "MANTENER FLUJO" cuando es alta rotación urgente.
        --   Va ANTES de las reglas de proy_mes (BAJANDO/ALTA/ACTIVA) porque
        --   esas usan el ritmo diluido. Solo dispara cuando `cob_post_recep`
        --   está disponible Y es ≤15d (urgencia objetiva).
        -- ════════════════════════════════════════════════════════════════════

        -- 🔥 ALTA ROTACIÓN POR LOTE FRESCO: post-recep ≥30/mes + cob ≤15d.
        WHEN stock_disponible > 0
             AND dias_desde_ultima_recep IS NOT NULL
             AND dias_desde_ultima_recep <= (SELECT ventana_recent_dias FROM params)
             AND proy_post_recep IS NOT NULL AND proy_post_recep >= (SELECT proy_mes_alta FROM params)
             AND cob_post_recep IS NOT NULL  AND cob_post_recep <= (SELECT cobertura_critica_dias FROM params)
             THEN '🔥 ALTA ROTACIÓN — PRIORIDAD DE COMPRA: lote llegó hace ≤30d y ya vende ≥30/mes con solo ≤15d de cobertura (reposición urgente)'

        -- ⚡ AL BORDE POR LOTE FRESCO: post-recep 10-29/mes + cob ≤15d.
        WHEN stock_disponible > 0
             AND dias_desde_ultima_recep IS NOT NULL
             AND dias_desde_ultima_recep <= (SELECT ventana_recent_dias FROM params)
             AND proy_post_recep IS NOT NULL AND proy_post_recep >= (SELECT proy_mes_min_cap FROM params)
             AND cob_post_recep IS NOT NULL  AND cob_post_recep <= (SELECT cobertura_critica_dias FROM params)
             THEN '⚡ ROTACIÓN ACTIVA AL BORDE — REPONER YA: lote llegó hace ≤30d, vende 10-29/mes y quedan ≤15d de stock al ritmo del lote (pedir ahora)'

        -- 📉 RITMO PERDIDO: vendió antes pero >45d sin venta — la velocidad calculada
        --    del lote es histórica y engañosa. NO es muerto (sí vendió) pero ya no rota.
        --    Va ANTES de ROTACIÓN ACTIVA/ALTA/SANO/EXCESO para evitar que la velocidad
        --    histórica enmascare la pausa. Caso típico: TCB-1561 Mag (vendió 142 unds
        --    rápido y lleva 79d sin venta) o JA-PR Mag (vendió 25 unds y lleva 61d sin).
        WHEN stock_disponible > 0
             AND unds_vendidas > 0
             AND COALESCE(dias_sin_venta_90d, 9999) > 45
             THEN '📉 RITMO PERDIDO — EVALUAR ANTES DE REPONER: vendía antes pero +45d sin venta (pensar si pausar)'

        -- 💀 SALDO QUEMADO: lote viejo cuya rotación histórica (proy_mes) era
        --    alta pero la velocidad REAL de los últimos 30d es casi nula.
        --    Típico de estacionales (esmaltes verano, cuadernos campaña escolar):
        --    el grueso de ventas fue al inicio del lote, ahora queda saldo y
        --    casi no mueve. NO es "decayendo" ni "alta rotación" — es muerto
        --    con saldo a liquidar. DEBE ir antes que las reglas que usan
        --    proy_mes para evaluar rotación (de lo contrario caería en
        --    ALTA ROTACIÓN o DECAYENDO con consejos erróneos de compra).
        --    P15 (2026-06-06): el clasificador no distinguía velocidad
        --    lifetime vs velocidad reciente — caso ESMALTE-J01 MAGDALENA.
        WHEN stock_disponible >= (SELECT lote_frenado_stock_min FROM params)             -- saldo SIGNIFICATIVO a liquidar
             AND proy_mes >= (SELECT lote_frenado_proy_min FROM params)
             AND COALESCE(proy_30d_reciente, 0) < (SELECT lote_frenado_proy30_max FROM params)
             AND COALESCE(edad_dias, 0) >= (SELECT lote_frenado_edad_min FROM params)  -- evita SKUs nuevos con boom inicial
             THEN '💀 LOTE FRENADO — LIQUIDAR, NO COMPRAR MÁS: lote viejo con stock que ya casi no rota'

        -- 🔥📉 ALTA ROTACIÓN DECAYENDO: vende mucho PERO demanda cae.
        --    Reponer al ritmo de los últimos 30d, no del lote completo.
        WHEN stock_disponible > 0 AND proy_mes >= (SELECT proy_mes_alta FROM params) AND dias_cobertura_reciente < (SELECT cobertura_baja_dias FROM params)
             AND v_recent_45d > 0 AND v_old_45d > 0
             AND v_recent_45d < v_old_45d * (SELECT trend_decay_mult FROM params)
             THEN '🔥📉 ROTACIÓN BAJANDO — REPONER MENOS: vende mucho pero menos que antes (usar ritmo nuevo, no histórico)'

        -- 🔥 ALTA ROTACIÓN: vol ≥30/mes + cobertura sana.
        WHEN stock_disponible > 0 AND proy_mes >= (SELECT proy_mes_alta FROM params) AND dias_cobertura_reciente < (SELECT cobertura_baja_dias FROM params)
             THEN '🔥 ALTA ROTACIÓN — PRIORIDAD DE COMPRA: vende ≥30/mes con poco stock (reposición urgente)'

        -- ⚡ ACTIVA AL BORDE: el LOTE ACTUAL llegó hace ≤30d, vende a buen ritmo
        --    (vol 10-29/mes) y la cobertura ya está DENTRO del margen de
        --    reposición (≤15d) → pedir ahora.
        --    P23 (2026-06-12): la cascada escalaba por velocidad pero no por
        --    cobertura crítica en la franja 10-29/mes — un SKU con 1-2 unds y
        --    venta diaria decía "MANTENER FLUJO" (casos: SARTEN 18/16CM cob 5d,
        --    CORTINA 130184 cob 3d). Corte 15d = margen de reposición.
        --    ★ Guard de frescura `dias_desde_ultima_recep ≤ 30` (2 refinamientos
        --    el mismo día): el usuario acota la caja a lotes RECIENTES que vuelan
        --    — cubre nuevos, relanzamientos Y reposiciones que quedaron cortas
        --    (papel higiénico llegó 10d/cob 4d). Los veteranos con lote viejo y
        --    cob ≤15d (ej. PLATO MH52-323: llegó hace 143d, 72 vendidos, queda 1)
        --    son recompra de RUTINA → se quedan en MANTENER FLUJO. (1ª versión
        --    sin guard: 110; con exhibido≤30: 43; con llegó≤30: 53 — el guard por
        --    última recepción es aditivo: los 43 frescos también lo cumplen.)
        --    El nombre CONTIENE "ROTACIÓN ACTIVA" a propósito (el chip "Mejores"
        --    del frontend y el tono del Excel matchean esa subcadena) y
        --    "REPONER YA" lo manda al bucket de acción `reponer`.
        WHEN stock_disponible > 0 AND proy_mes >= GREATEST((SELECT proy_mes_min_floor FROM params), LEAST((SELECT proy_mes_min_cap FROM params), COALESCE(cb.avg_proy_cat, (SELECT proy_mes_min_cap FROM params)) * (SELECT proy_mes_cat_ratio FROM params))) AND dias_cobertura_reciente <= (SELECT cobertura_critica_dias FROM params)
             AND dias_desde_ultima_recep <= (SELECT ventana_recent_dias FROM params)
             THEN '⚡ ROTACIÓN ACTIVA AL BORDE — REPONER YA: el lote llegó hace ≤30 días, vende 10-29/mes y quedan ≤15 días de stock (pedir ahora)'

        -- 💫 ROTACIÓN ACTIVA: vol 10-29/mes + cobertura sana.
        WHEN stock_disponible > 0 AND proy_mes >= GREATEST((SELECT proy_mes_min_floor FROM params), LEAST((SELECT proy_mes_min_cap FROM params), COALESCE(cb.avg_proy_cat, (SELECT proy_mes_min_cap FROM params)) * (SELECT proy_mes_cat_ratio FROM params))) AND dias_cobertura_reciente < (SELECT cobertura_baja_dias FROM params)
             THEN '💫 ROTACIÓN ACTIVA — MANTENER FLUJO: vende 10-29/mes constante (reposición regular)'

        -- 🟢 INVENTARIO SANO — RITMO NORMAL: stock equilibrado con demanda (cob 30-45d, todo OK) de reposición.
        WHEN stock_disponible > 0 AND proy_mes >= GREATEST((SELECT proy_mes_min_floor FROM params), LEAST((SELECT proy_mes_min_cap FROM params), COALESCE(cb.avg_proy_cat, (SELECT proy_mes_min_cap FROM params)) * (SELECT proy_mes_cat_ratio FROM params))) AND dias_cobertura_reciente BETWEEN (SELECT cobertura_baja_dias FROM params) AND (SELECT cobertura_objetivo_dias FROM params)
             THEN '🟢 INVENTARIO SANO — RITMO NORMAL: stock equilibrado con demanda (cob 30-45d, todo OK)'

        -- 🧊📉 EXCESO LIQUIDAR: cob >45d + demanda cae.
        --    Capital estancado Y la demanda se enfría. Promocionar urgente.
        WHEN stock_disponible > 0 AND proy_mes >= GREATEST((SELECT proy_mes_min_floor FROM params), LEAST((SELECT proy_mes_min_cap FROM params), COALESCE(cb.avg_proy_cat, (SELECT proy_mes_min_cap FROM params)) * (SELECT proy_mes_cat_ratio FROM params))) AND dias_cobertura_reciente > (SELECT cobertura_objetivo_dias FROM params)
             AND v_recent_45d > 0 AND v_old_45d > 0
             AND v_recent_45d < v_old_45d * (SELECT trend_decay_mult FROM params)
             AND COALESCE(dias_desde_ultima_recep, 9999) > 7  -- ★ recién reabastecido (≤7d) NO es exceso
             THEN '🧊📉 EXCESO + DEMANDA CAYENDO — PROMOCIONAR YA: demasiado stock Y la demanda se enfría'

        -- 🧊 EXCESO DE INVENTARIO: cob >45d con demanda estable.
        WHEN stock_disponible > 0 AND proy_mes >= GREATEST((SELECT proy_mes_min_floor FROM params), LEAST((SELECT proy_mes_min_cap FROM params), COALESCE(cb.avg_proy_cat, (SELECT proy_mes_min_cap FROM params)) * (SELECT proy_mes_cat_ratio FROM params))) AND dias_cobertura_reciente > (SELECT cobertura_objetivo_dias FROM params)
             AND COALESCE(dias_desde_ultima_recep, 9999) > 7  -- ★ recién reabastecido (≤7d) NO es exceso
             THEN '🧊 STOCK EXCESIVO — PROMOCIONAR: demasiado stock para la demanda actual (capital atrapado)'

        -- 🪦 LENTO CRÓNICO: producto que lleva mucho tiempo en catálogo
        --    (≥180d) pero vendió poco en toda su vida (<60 unds totales) Y
        --    con velocidad reciente baja (<5/mes). NO vale reponer aunque
        --    aparente "demanda activa" por unas pocas ventas recientes.
        --    P18 (2026-06-08): caso testigo GFQQ-240437 REL DE PARED MAG
        --    (26 unds en 10 meses → vel ~3/mes, lifetime). El sistema lo
        --    veía como ⚠ POCO STOCK CON DEMANDA por la vel reciente de los
        --    últimos 30d, pero el patrón lifetime es claramente lento crónico.
        WHEN stock_disponible > 0
             AND COALESCE(edad_dias, 0) >= (SELECT ventana_blind_spot_dias FROM params)
             AND COALESCE(dias_desde_ultima_recep, 9999) >= (SELECT ventana_recent_dias FROM params)  -- el lote actual no es reciente
             AND COALESCE(dias_sin_venta_90d, 9999) >= (SELECT lento_cronico_dsv_min FROM params)        -- ★ P21: si vendió esta semana, deciden las reglas de stock crítico
             AND COALESCE(unds_vendidas_lifetime, 0) < (SELECT lento_cronico_lifetime_max FROM params)
             AND (COALESCE(unds_vendidas_lifetime, 0)::numeric / NULLIF(edad_dias, 0) * 30) < (SELECT lento_cronico_proy_max FROM params)
             THEN '🪦 LENTO CRÓNICO — NO REPONER: vende <5/mes en toda su vida (no vale la pena reabastecer)'

        -- ⚠️ STOCK CRÍTICO: cob <30d + velocidad baja PERO venta reciente.
        --    Reponer aunque rotación promedio sea baja — se va a agotar.
        WHEN dias_cobertura_reciente IS NOT NULL AND dias_cobertura_reciente < (SELECT cobertura_baja_dias FROM params) AND proy_mes < GREATEST((SELECT proy_mes_min_floor FROM params), LEAST((SELECT proy_mes_min_cap FROM params), COALESCE(cb.avg_proy_cat, (SELECT proy_mes_min_cap FROM params)) * (SELECT proy_mes_cat_ratio FROM params)))
             AND vel_30d IS NOT NULL AND vel_30d > 0
             THEN '⚠️ POCO STOCK CON DEMANDA — REPONER: cobertura baja con rotación lenta pero activa (evitar quiebre)'

        -- 📈 BAJO VOLUMEN EN ALZA: proy <10 pero tendencia positiva.
        --    Observar — la velocidad promedio del lote subestima al SKU.
        -- ★ FIX P16 (2026-06-06): exigir cobertura ≤45d. Si hay stock para
        --   60+ días, la tendencia positiva NO importa operativamente — ya
        --   tenés mercadería de sobra. Cae al siguiente WHEN (BAJA ROTACIÓN).
        --   Caso testigo: TRAPEADOR GF-3602 MAGDALENA (cob=80d) entraba acá
        --   con consejo "vigilar, no liquidar" — engañoso; lo correcto es
        --   "🐢 BAJA ROTACIÓN — PEDIR MENOS".
        WHEN proy_mes < GREATEST((SELECT proy_mes_min_floor FROM params), LEAST((SELECT proy_mes_min_cap FROM params), COALESCE(cb.avg_proy_cat, (SELECT proy_mes_min_cap FROM params)) * (SELECT proy_mes_cat_ratio FROM params)))
             AND v_recent_45d > 0
             AND COALESCE(dias_cobertura_reciente, 9999) <= (SELECT cobertura_objetivo_dias FROM params)
             AND (
                 (v_old_45d > 0 AND v_recent_45d > v_old_45d * (SELECT trend_grow_mult FROM params))
                 OR
                 (v_old_45d = 0 AND edad_dias > 30
                  AND unds_recibidas_lifetime > 0
                  AND unds_vendidas_lifetime >= unds_recibidas_lifetime * 0.50)
             )
             THEN '📈 VENDIENDO MÁS QUE ANTES — VIGILAR: vende poco pero la tendencia es positiva (observar, no liquidar)'

        -- 🐢 BAJA ROTACIÓN: catch-all proy<10 con stock disponible.
        --    Bajar pedido / revisar surtido.
        WHEN proy_mes < GREATEST((SELECT proy_mes_min_floor FROM params), LEAST((SELECT proy_mes_min_cap FROM params), COALESCE(cb.avg_proy_cat, (SELECT proy_mes_min_cap FROM params)) * (SELECT proy_mes_cat_ratio FROM params)))
             THEN '🐢 BAJA ROTACIÓN — PEDIR MENOS: vende menos de 10/mes (bajar próximo pedido, revisar surtido)'

        -- ════════════════════════════════════════════════════════════════════
        -- SECCIÓN F · CATCH-ALL (no debería disparar en producción)
        -- ════════════════════════════════════════════════════════════════════
        ELSE '⚖️ CASO ATÍPICO — REVISAR MANUAL: caso no cubierto por reglas (analizar a mano)'
     END                       AS "Clasificación",

    -- ════════ Columnas que NO proyectan todos los módulos ════════
    -- (04b/05) Última venta lifetime con su nombre de display:
    ult_venta_lifetime       AS "Fecha Últ. Venta",
    -- (05) Variante LEGACY de XYZ: usa unds_post_recep (deprecada) en vez de
    -- dias_con_venta_lote, por lo que marca 'Sin datos' de más. DIVERGENCIA
    -- HISTÓRICA preservada tal cual para no cambiar el output del 05.
    -- Para alinear el 05 al fix: proyectar "XYZ" en 05_matriz_operativa.sql
    -- y borrar esta columna.
    CASE
        WHEN unds_post_recep = 0 THEN 'Sin datos'
        WHEN pct_frecuencia >= (SELECT xyz_constante_pct FROM params)  THEN 'X (Constante)'
        WHEN pct_frecuencia >= (SELECT xyz_variable_pct FROM params)   THEN 'Y (Variable)'
        ELSE 'Z (Errático / Ráfaga)'
    END                       AS xyz_legacy,
    -- (05) Bloque LIFETIME (contexto histórico):
    trim_scale(ROUND(unds_vendidas_lifetime, 2))   AS "Unds Vend Lifetime",
    trim_scale(ROUND(unds_recibidas_lifetime, 2))  AS "Unds Recib Lifetime",
    trim_scale(ROUND(pct_sellthrough_lifetime, 1)) AS "% Sell-Through Lifetime",
    mejor_mes_str                                  AS "Mejor Mes",
    trim_scale(ROUND(mejor_mes_uds, 2))            AS "Uds Mejor Mes",
    -- (05) Índice de Contribución: (V_sku/V_cat) / (TDPV_sku/TDPV_cat).
    -- IC > 1 = el SKU aporta MÁS ventas de lo que ocupa en TDPV.
    -- COALESCE defensivo: si por algún edge case de NULLIF la división interna da
    -- NULL (no observado en producción hoy, pero protege contra regresiones), sale 0.
    COALESCE(
        CASE
            WHEN cat_tdpv > 0 AND cat_ventas > 0 AND tdpv > 0 THEN
                trim_scale(ROUND(((unds_vendidas / NULLIF(cat_ventas, 0)) /
                        NULLIF(tdpv / cat_tdpv, 0))::numeric, 2))
            ELSE 0::numeric
        END,
        0::numeric
    )                                              AS "Índice Contribución",
    -- (04b) Monto vendido del SKU en S/ (90d):
    ROUND(monto_vendido_90d, 2)                    AS "Vendido SKU S/",

    -- ════════ Crudas para filtros (fantasmas), ventanas jerárquicas
    --          del 04b y ORDER BY de los módulos ════════
    m.bsale_office_id,
    m.department_id, m.category_id, m.subcategory_id,
    m.monto_vendido_90d,
    m.stock_disponible, m.unds_vendidas, m.ult_venta_lifetime

FROM metricas_reciente m
LEFT JOIN stock_almacen sa ON sa.bsale_variant_id = m.bsale_variant_id
LEFT JOIN cat_baseline cb
  ON cb.bsale_office_id = m.bsale_office_id
 AND cb.cat_name = COALESCE(m.category, '(sin)')
LEFT JOIN transferencias t
  ON t.donor_office = m.bsale_office_id
 AND t.variant_id   = m.bsale_variant_id
)
