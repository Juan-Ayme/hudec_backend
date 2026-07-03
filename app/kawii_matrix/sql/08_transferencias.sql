-- =============================================================
-- MATRIZ 08 — SUGERENCIAS DE TRANSFERENCIA INTER-SUCURSAL
-- =============================================================
-- ⚠ ARCHIVO AUTO-GENERADO por `_beta/generar_06_transferencias.py`.
--   NO editar manualmente las CTEs base (van desde `params` hasta
--   `cat_baseline`). Si 04b cambia, regenerar con:
--       python -m _beta.generar_06_transferencias
--
-- Objetivo de esta matriz:
--   Encontrar SKUs donde una sucursal tiene STOCK SOBRANTE (exceso,
--   lote frenado, baja rotación, muerto) Y la otra sucursal tiene
--   DEMANDA INSATISFECHA (quiebre, alta rotación con poco stock,
--   agotado con demanda). Sugiere cantidad a transferir, impacto
--   estimado en S/ y prioridad.
--
-- Devuelve UNA fila por cada par (donante × receptor × SKU) válido.
--   Si ambas sucursales son candidatas a donar/recibir, sale en ambas
--   direcciones — el usuario elige.
-- =============================================================

-- =============================================================
-- MATRIZ 90D BASE (heredada de 04b) — CTEs para 06_transferencias
-- =============================================================
-- Variante del módulo 04 con 6 columnas extra de contexto jerárquico:
--   • V90 Subcat / R90 Subcat (suma total de la subcategoría)
--   • V90 Cat / R90 Cat (suma total de la categoría)
--   • V90 Depto / R90 Depto (suma total del departamento)
-- Y 3 columnas de participación del SKU:
--   • % en Subcat / % en Cat / % en Depto
-- Misma lógica de clasificación que el módulo 04.
-- =============================================================
WITH params AS (
    SELECT NOW()                              AS ahora,
        NOW() - INTERVAL '90 days'           AS fecha_corte,
        CAST(:sucursales_objetivo AS int[])          AS sucursales_objetivo,
        CAST(:tipos_venta AS int[])                  AS tipos_venta,
        CAST(:tipos_devolucion AS int[])             AS tipos_devolucion,
        7                                    AS piso_dias_lote
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
-- ★ VENTAS EN MONTO MONETARIO (90d) — para totales jerárquicos en S/
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
        SUM(CASE WHEN vd.fecha >= (p.ahora - INTERVAL '45 days')::date
                 THEN vd.qty_venta - vd.qty_devol ELSE 0 END) AS v_recent_45d,
        SUM(CASE WHEN vd.fecha < (p.ahora - INTERVAL '45 days')::date
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
    WHERE vd.fecha >= (p.ahora - INTERVAL '30 days')::date
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
        MIN(CASE WHEN r.bsale_user_id IN (2, 4, 5, 14, 16) THEN r.admission_date END) AS primera_recep_90d,  -- ★ solo recepciones de almaceneros — ajustes de caja/ADM se ignoran
        MAX(r.admission_date) AS ultima_recep_90d,
        SUM(rd.quantity)      AS unds_recibidas_90d,
        COUNT(DISTINCT r.bsale_reception_id) AS num_recepciones_90d
    FROM receptions r
    JOIN reception_details rd USING (bsale_reception_id)
    CROSS JOIN params p
    WHERE r.bsale_office_id = ANY(p.sucursales_objetivo)
      AND r.admission_date >= p.fecha_corte
      AND rd.quantity <= 50000  -- ★ P22: tope de sanidad (códigos de barras tipeados como cantidad; máx legítimo histórico: 10,000)
    GROUP BY 1, 2
),
primera_recep_total AS (
    SELECT r.bsale_office_id, rd.bsale_variant_id,
        MIN(r.admission_date) AS primera_recepcion,
        MAX(CASE WHEN r.bsale_user_id IN (2, 4, 5, 14, 16) THEN r.admission_date END) AS ultima_recepcion,  -- ★ solo recepciones de almaceneros — ajustes de caja/ADM se ignoran
        SUM(rd.quantity)      AS unds_recibidas_lifetime  -- total recibido en toda la vida del SKU
    FROM receptions r
    JOIN reception_details rd USING (bsale_reception_id)
    CROSS JOIN params p
    WHERE r.bsale_office_id = ANY(p.sucursales_objetivo)
      AND rd.quantity <= 50000  -- ★ P22: tope de sanidad
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
      AND r.bsale_user_id IN (2, 4, 5, 14, 16)  -- ★ solo recepciones de almaceneros
      AND rd.quantity <= 50000  -- ★ P22: tope de sanidad
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

-- Cambios netos del stock por día (ventas/devoluciones, recepciones, consumos).
-- NO filtra recepciones por usuario: aquí queremos TODOS los movimientos que
-- afectan stock físicamente (incluye "Sin Documento" de caja). El filtro por
-- almaceneros sigue aplicando para definir el INICIO del ciclo (recep_90d),
-- no para reconstruir saldos.
movimientos_diarios AS (
    SELECT bsale_office_id, bsale_variant_id, fecha, SUM(delta) AS delta_dia
    FROM (
        -- Ventas netas (salen del stock → delta negativo)
        SELECT bsale_office_id, bsale_variant_id, fecha,
               -(qty_venta - qty_devol) AS delta
        FROM ventas_diarias
        UNION ALL
        -- Recepciones (entran al stock → delta positivo)
        SELECT r.bsale_office_id, rd.bsale_variant_id,
               r.admission_date::date, rd.quantity
        FROM receptions r
        JOIN reception_details rd USING (bsale_reception_id)
        CROSS JOIN params p
        WHERE r.bsale_office_id = ANY(p.sucursales_objetivo)
          AND r.admission_date >= p.fecha_corte
        UNION ALL
        -- Consumos (mermas/uso interno → delta negativo)
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
-- Reconstrucción BACKWARD del stock al FIN de cada día con movimiento.
-- Fórmula: stock_fin[d] = stock_actual − Σ(delta) de días posteriores a d.
-- Entre 2 días con movimiento el stock se mantiene constante.
sku_eventos AS (
    SELECT m.bsale_office_id, m.bsale_variant_id, m.fecha,
           m.delta_dia,
           ss.stock_disponible
             - COALESCE(SUM(m.delta_dia) OVER (
                 PARTITION BY m.bsale_office_id, m.bsale_variant_id
                 ORDER BY m.fecha
                 ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING
               ), 0) AS stock_fin_dia,
           -- Siguiente evento (o "hoy+1" si es el último → cubre hasta hoy)
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
-- Días dentro del ciclo (desde inicio_ciclo) donde stock>0.
-- Entre cada movimiento d y el siguiente, si stock_fin[d]>0 se acumula
-- la cantidad de días que el SKU estuvo disponible para vender.
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
             --   inflada (caso XP-131-VA: 19/mes real clasificaba como ≥30/mes).
             --   Si al inicio del 1er evento de la ventana había stock>0, se
             --   asume stock continuo desde el inicio del ciclo (sin recepciones
             --   intermedias el stock solo pudo bajar, nunca tocar 0 y volver).
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
      AND cd.quantity <= 50000  -- ★ P22: tope de sanidad (las "correcciones" de recepciones corruptas también eran absurdas; máx legítimo: 2,250)
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
    --   podía marcarse 💎 OPORTUNIDAD PERDIDA. El filtro final de fantasmas
    --   (ult_venta_lifetime < 180d) ya asumía que estos SKUs existían.
    UNION
    SELECT d.bsale_office_id, dd.bsale_variant_id
    FROM documents d
    JOIN document_details dd USING (bsale_document_id)
    CROSS JOIN params p
    WHERE d.is_active
      AND d.bsale_office_id = ANY(p.sucursales_objetivo)
      AND d.bsale_document_type_id = ANY(p.tipos_venta)
      AND d.emission_date >= p.ahora - INTERVAL '180 days'
),
radiografia AS (
    SELECT b.bsale_office_id, b.bsale_variant_id,
        o.name AS sucursal, v.display_code,
        j.product_name, j.department, j.category, j.subcategory,
        -- ★ IDs propagados para usarlos en window functions (totales jerárquicos)
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
        -- ★ Monto vendido en 90d en S/ (para totales jerárquicos en soles)
        COALESCE(vm.monto_vendido_90d, 0)::numeric AS monto_vendido_90d,
        -- ★ Cantidad de la ÚLTIMA recepción individual (no lifetime).
        COALESCE(uri.ult_recep_qty, 0)::numeric        AS ult_recep_qty,
        -- ★ FIX días sin stock: días reales con stock>0 dentro del ciclo del lote.
        --    Usado por `calc.dias_efectivos` para no diluir velocidad con días
        --    de stock=0. NULL si no hubo movimientos (cae al cálculo viejo).
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
    LEFT JOIN ventas_monto_90d   vm ON vm.bsale_office_id  = b.bsale_office_id AND vm.bsale_variant_id  = b.bsale_variant_id
    LEFT JOIN ult_recep_info       uri ON uri.bsale_office_id = b.bsale_office_id AND uri.bsale_variant_id = b.bsale_variant_id
    LEFT JOIN dias_con_stock_ciclo dcs ON dcs.bsale_office_id = b.bsale_office_id AND dcs.bsale_variant_id = b.bsale_variant_id
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
        -- ★ dias_efectivos del CICLO ACTUAL.
        --    Inicio del ciclo: primera_recep_90d si hubo recepción en 90d, sino ultima_recepcion lifetime.
        --    Resuelve: Aloe Vera (2 lotes en 8 días = un solo ciclo de 9d, no de 1d).
        --    ★ FIX días sin stock: prioriza `dias_con_stock` (días REALES donde
        --      había stock>0). Si el CTE no devolvió valor (NULL), cae a la
        --      fórmula vieja (hoy - inicio_ciclo). Esto corrige la dilución
        --      de velocidad en SKUs que tuvieron pausas de stock=0.
        --      Ejemplo: TOALLITA BYWIN — velocidad 17→39 uds/día (+130%).
        GREATEST(p.piso_dias_lote::numeric,
            COALESCE(r.dias_con_stock,
                CASE
                    WHEN r.stock_disponible = 0
                         AND COALESCE(r.primera_recep_90d, r.ultima_recepcion) IS NOT NULL
                         AND r.ult_venta_lote IS NOT NULL
                    THEN (r.ult_venta_lote - COALESCE(r.primera_recep_90d::date, r.ultima_recepcion::date))::numeric
                    WHEN COALESCE(r.primera_recep_90d, r.ultima_recepcion) IS NOT NULL
                    THEN DATE_PART('day', p.ahora - COALESCE(r.primera_recep_90d, r.ultima_recepcion))::numeric
                    ELSE DATE_PART('day', p.ahora - p.fecha_corte)::numeric
                END
            )
        ) AS dias_efectivos
    FROM radiografia r CROSS JOIN params p
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
        -- ★ Días con stock en los últimos 30d (Opción 2: no penaliza por agotamiento).
        --    - Si aún tiene stock: 30 días completos
        --    - Si se agotó: días desde (hoy-30) hasta la última venta del lote
        --    - Si nunca vendió o se agotó hace >30d: 0
        CASE
            WHEN c.stock_disponible > 0 THEN 30
            WHEN c.ult_venta_lote IS NULL THEN 0
            WHEN c.ult_venta_lote >= ((SELECT ahora FROM params) - INTERVAL '30 days')::date
                 THEN GREATEST(1, (c.ult_venta_lote - ((SELECT ahora FROM params) - INTERVAL '30 days')::date + 1))::int
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
        END AS dias_cobertura_reciente
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

-- ════════════════════════════════════════════════════════════════════
-- SELECT FINAL — Pares (donante, receptor) para el MISMO SKU
-- ════════════════════════════════════════════════════════════════════
-- precio_ref = precio promedio observado (monto/unds de quien tenga ventas).
-- Calculado fuera del JOIN para evitar dividir por cero.
precio_por_sku AS (
    SELECT bsale_variant_id,
           bsale_office_id,
           CASE WHEN unds_vendidas > 0
                THEN ROUND((monto_vendido_90d / unds_vendidas)::numeric, 2)
                ELSE NULL
           END AS precio_prom
    FROM metricas_reciente
)

SELECT
    -- ───── Identificación del SKU ─────
    donor.bsale_variant_id,
    donor.display_code              AS "SKU",
    donor.product_name              AS "Producto",
    donor.department                AS "Depto",
    donor.category                  AS "Categoría",
    donor.subcategory               AS "Subcat",

    -- ───── DONANTE: sucursal que da ─────
    donor.sucursal                  AS "↗ Donante (sucursal)",
    donor.stock_disponible::int     AS "Donante stock",
    ROUND(donor.proy_mes, 1)        AS "Donante proy lifetime",
    ROUND(donor.proy_30d_reciente, 1) AS "Donante proy 30d real",
    donor.dias_cobertura            AS "Donante cob días",
    CASE
        WHEN donor.ult_venta_lote IS NULL THEN NULL
        ELSE (CURRENT_DATE - donor.ult_venta_lote)::int
    END                              AS "Donante DSV",

    -- ───── RECEPTOR: sucursal que recibe ─────
    recip.sucursal                  AS "↘ Receptor (sucursal)",
    recip.stock_disponible::int     AS "Receptor stock",
    ROUND(recip.proy_mes, 1)        AS "Receptor proy lifetime",
    ROUND(recip.proy_30d_reciente, 1) AS "Receptor proy 30d real",
    recip.dias_cobertura            AS "Receptor cob días",
    CASE
        WHEN recip.ult_venta_lote IS NULL THEN NULL
        ELSE (CURRENT_DATE - recip.ult_venta_lote)::int
    END                              AS "Receptor DSV",

    -- ───── RAZÓN del par ─────
    --   Por qué el donante puede ceder
    CASE
        WHEN donor.stock_disponible >= 5
             AND donor.proy_mes >= 10
             AND COALESCE(donor.proy_30d_reciente, 0) < 5
             AND COALESCE((CURRENT_DATE - donor.primera_recepcion::date), 0) >= 90
            THEN '💀 LOTE FRENADO (vendía antes, hoy parado)'
        WHEN donor.dias_cobertura > 90 AND donor.proy_mes >= 10
            THEN '🧊 EXCESO DE INVENTARIO (cob >90d)'
        WHEN donor.unds_vendidas = 0
             AND donor.stock_disponible >= 5
             AND donor.primera_recepcion < (CURRENT_DATE - INTERVAL '60 days')
            THEN '💀 STOCK MUERTO (sin venta en 90d)'
        WHEN donor.proy_mes < 10
             AND donor.stock_disponible >= 15
             AND donor.dias_cobertura > 60
            THEN '🐢 BAJA ROTACIÓN con stock alto'
        ELSE NULL
    END                              AS "Razón donante",

    --   Por qué el receptor necesita
    CASE
        WHEN recip.stock_disponible = 0 AND recip.proy_mes >= 30
            THEN '🚨 QUIEBRE de bestseller'
        WHEN recip.stock_disponible = 0 AND recip.proy_mes >= 10
            THEN '🚨 Sin stock con demanda activa'
        WHEN recip.dias_cobertura IS NOT NULL
             AND recip.dias_cobertura < 14
             AND recip.proy_mes >= 30
            THEN '🔥 Alta rotación, cob <14d'
        WHEN recip.dias_cobertura IS NOT NULL
             AND recip.dias_cobertura < 21
             AND recip.proy_mes >= 10
            THEN '⚠ Poco stock con demanda'
        WHEN recip.stock_disponible = 0 AND recip.unds_vendidas_lifetime >= 10
            THEN '💎 Agotado, hay demanda histórica'
        ELSE NULL
    END                              AS "Razón receptor",

    -- ───── UNIDADES SUGERIDAS ─────
    --   regla: mover el mínimo entre
    --     (a) excedente del donante  — si el donante está muerto/frenado,
    --         puede transferir TODO; si solo es exceso, deja 30d cob
    --     (b) déficit del receptor   — cubre 60d de demanda
    GREATEST(1, LEAST(
        CASE
            WHEN COALESCE(donor.proy_30d_reciente, 0) < 5 OR donor.unds_vendidas = 0
                THEN donor.stock_disponible::int
            ELSE FLOOR(GREATEST(0, donor.stock_disponible - donor.proy_mes))::int
        END,
        CEIL(GREATEST(0, recip.proy_mes * 2 - recip.stock_disponible))::int
    ))                               AS "Unds sugeridas",

    -- Stock del donante DESPUÉS de transferir
    GREATEST(0, donor.stock_disponible::int -
        GREATEST(1, LEAST(
            CASE
                WHEN COALESCE(donor.proy_30d_reciente, 0) < 5 OR donor.unds_vendidas = 0
                    THEN donor.stock_disponible::int
                ELSE FLOOR(GREATEST(0, donor.stock_disponible - donor.proy_mes))::int
            END,
            CEIL(GREATEST(0, recip.proy_mes * 2 - recip.stock_disponible))::int
        ))
    )                                AS "Donante stock post",

    -- Stock del receptor DESPUÉS de transferir
    (recip.stock_disponible::int +
        GREATEST(1, LEAST(
            CASE
                WHEN COALESCE(donor.proy_30d_reciente, 0) < 5 OR donor.unds_vendidas = 0
                    THEN donor.stock_disponible::int
                ELSE FLOOR(GREATEST(0, donor.stock_disponible - donor.proy_mes))::int
            END,
            CEIL(GREATEST(0, recip.proy_mes * 2 - recip.stock_disponible))::int
        ))
    )                                AS "Receptor stock post",

    -- Cobertura post del receptor (estimada con proy_mes)
    CASE
        WHEN recip.proy_mes <= 0 THEN NULL
        ELSE CEIL((
            recip.stock_disponible +
            GREATEST(1, LEAST(
                CASE
                    WHEN COALESCE(donor.proy_30d_reciente, 0) < 5 OR donor.unds_vendidas = 0
                        THEN donor.stock_disponible::int
                    ELSE FLOOR(GREATEST(0, donor.stock_disponible - donor.proy_mes))::int
                END,
                CEIL(GREATEST(0, recip.proy_mes * 2 - recip.stock_disponible))::int
            ))
        ) / (recip.proy_mes / 30.0))::int
    END                              AS "Receptor cob post",

    -- ───── IMPACTO ECONÓMICO ─────
    --   precio promedio observado (del receptor o del donante)
    COALESCE(pp_r.precio_prom, pp_d.precio_prom)        AS "Precio prom S/",

    --   impacto $ = unds_sugeridas × precio
    ROUND(
        (GREATEST(1, LEAST(
            CASE
                WHEN COALESCE(donor.proy_30d_reciente, 0) < 5 OR donor.unds_vendidas = 0
                    THEN donor.stock_disponible::int
                ELSE FLOOR(GREATEST(0, donor.stock_disponible - donor.proy_mes))::int
            END,
            CEIL(GREATEST(0, recip.proy_mes * 2 - recip.stock_disponible))::int
        )) * COALESCE(pp_r.precio_prom, pp_d.precio_prom, 0))::numeric,
        2
    )                                AS "Impacto S/ estimado",

    -- ───── PRIORIDAD ─────
    --   1 = urgentísimo (receptor sin stock + bestseller),
    --   2 = urgente, 3 = alta, 4 = media, 5 = baja
    CASE
        WHEN recip.stock_disponible = 0 AND recip.proy_mes >= 30 THEN 1
        WHEN recip.stock_disponible = 0 AND recip.proy_mes >= 10 THEN 2
        WHEN recip.dias_cobertura IS NOT NULL AND recip.dias_cobertura < 14 THEN 3
        WHEN recip.dias_cobertura IS NOT NULL AND recip.dias_cobertura < 30 THEN 4
        ELSE 5
    END                              AS "Prioridad",

    -- ───── CLASIFICACIÓN ─────
    -- Necesaria para que build_workbook (que agrupa el Excel maquetado) tenga
    -- una columna principal. Es el nivel de prioridad expresado en texto.
    CASE
        WHEN recip.stock_disponible = 0 AND recip.proy_mes >= 30
            THEN '🚨 URGENTÍSIMO: receptor sin stock + bestseller — TRANSFERIR YA'
        WHEN recip.stock_disponible = 0 AND recip.proy_mes >= 10
            THEN '🚨 URGENTE: receptor sin stock con demanda activa'
        WHEN recip.dias_cobertura IS NOT NULL AND recip.dias_cobertura < 14
            THEN '🔥 ALTA: receptor con cobertura crítica (<14 días)'
        WHEN recip.dias_cobertura IS NOT NULL AND recip.dias_cobertura < 30
            THEN '⚠ MEDIA: receptor con cobertura baja (<30 días)'
        ELSE '🐢 BAJA: transferencia oportunista (no crítica)'
    END                              AS "Clasificación"

FROM metricas_reciente donor
JOIN metricas_reciente recip
  ON donor.bsale_variant_id = recip.bsale_variant_id
 AND donor.bsale_office_id <> recip.bsale_office_id
LEFT JOIN precio_por_sku pp_d
  ON pp_d.bsale_variant_id = donor.bsale_variant_id
 AND pp_d.bsale_office_id  = donor.bsale_office_id
LEFT JOIN precio_por_sku pp_r
  ON pp_r.bsale_variant_id = recip.bsale_variant_id
 AND pp_r.bsale_office_id  = recip.bsale_office_id
WHERE
    -- DONANTE: tiene stock suficiente Y está en alguna situación de
    -- excedente (cualquier criterio activa la regla)
    donor.stock_disponible >= 5
    AND (
        -- exceso
        (donor.dias_cobertura > 90 AND donor.proy_mes >= 5)
        OR
        -- lote frenado (P15)
        (donor.proy_mes >= 10
         AND COALESCE(donor.proy_30d_reciente, 0) < 5
         AND COALESCE((CURRENT_DATE - donor.primera_recepcion::date), 0) >= 90)
        OR
        -- muerto: sin ventas 90d con stock recibido hace tiempo
        (donor.unds_vendidas = 0
         AND donor.primera_recepcion < (CURRENT_DATE - INTERVAL '60 days'))
        OR
        -- baja rotación con stock alto
        (donor.proy_mes < 10 AND donor.stock_disponible >= 15
         AND donor.dias_cobertura > 60)
    )
    -- RECEPTOR: tiene demanda activa Y necesita más stock
    AND recip.proy_mes >= 5
    AND (
        recip.stock_disponible = 0
        OR (recip.dias_cobertura IS NOT NULL AND recip.dias_cobertura < 30)
    )
    -- Que el receptor NO sea otro caso de exceso (evita transferir entre
    -- 2 sucursales que ambas tienen sobrante)
    AND NOT (recip.dias_cobertura > 60)
    -- Sanity: el SKU debe estar identificado
    AND donor.bsale_variant_id IS NOT NULL
ORDER BY
    -- 1° prioridad (urgentísimos primero), 2° impacto $ (mayor primero)
    CASE
        WHEN recip.stock_disponible = 0 AND recip.proy_mes >= 30 THEN 1
        WHEN recip.stock_disponible = 0 AND recip.proy_mes >= 10 THEN 2
        WHEN recip.dias_cobertura IS NOT NULL AND recip.dias_cobertura < 14 THEN 3
        WHEN recip.dias_cobertura IS NOT NULL AND recip.dias_cobertura < 30 THEN 4
        ELSE 5
    END ASC,
    (GREATEST(1, LEAST(
        CASE
            WHEN COALESCE(donor.proy_30d_reciente, 0) < 5 OR donor.unds_vendidas = 0
                THEN donor.stock_disponible::int
            ELSE FLOOR(GREATEST(0, donor.stock_disponible - donor.proy_mes))::int
        END,
        CEIL(GREATEST(0, recip.proy_mes * 2 - recip.stock_disponible))::int
    )) * COALESCE(pp_r.precio_prom, pp_d.precio_prom, 0)) DESC
