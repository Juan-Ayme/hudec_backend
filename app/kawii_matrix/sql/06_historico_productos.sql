-- =============================================================
-- HISTÓRICO DE PRODUCTOS - "Autopsia" de SKUs muertos y fantasmas
-- =============================================================
-- Esta consulta analiza el CICLO DE VIDA COMPLETO de cada SKU en cada
-- sucursal: cuándo entró, cuándo murió, qué tan bien vendió, cuál fue
-- su mejor momento, etc. Se enfoca en productos que ya NO son operativos
-- (sin stock + sin ventas recientes) pero tienen historia que vale la pena
-- conservar para decisiones futuras (reactivar / descatalogar).
--
-- Sucursales: 1 (Magdalena) y 3 (Asamblea).
-- Ventana: TODA la historia (no hay corte de 60d).
-- =============================================================
WITH params AS (
    SELECT
        NOW()                                AS ahora,
        CAST(:sucursales_objetivo AS int[])          AS sucursales_objetivo,
        CAST(:tipos_venta AS int[])                  AS tipos_venta,
        CAST(:tipos_devolucion AS int[])             AS tipos_devolucion
),
-- Cada documento-detalle de venta o devolución, en una sola pasada
movimientos AS (
    SELECT
        d.bsale_office_id,
        dd.bsale_variant_id,
        d.emission_date AS ts,
        DATE(d.emission_date) AS fecha,
        TO_CHAR(d.emission_date, 'YYYY-MM') AS anio_mes,
        CASE WHEN d.bsale_document_type_id = ANY(p.tipos_venta) THEN dd.quantity ELSE 0 END AS qty_venta,
        CASE WHEN d.bsale_document_type_id = ANY(p.tipos_devolucion) THEN dd.quantity ELSE 0 END AS qty_devol
    FROM documents d
    JOIN document_details dd USING (bsale_document_id)
    CROSS JOIN params p
    WHERE d.is_active
      AND d.bsale_office_id = ANY(p.sucursales_objetivo)
      AND d.bsale_document_type_id = ANY(p.tipos_venta || p.tipos_devolucion)
),
-- Métricas lifetime: ventas totales, primer/último día con venta, días activos
lifetime AS (
    SELECT
        bsale_office_id,
        bsale_variant_id,
        SUM(qty_venta - qty_devol)                      AS unds_vendidas_neto,
        SUM(qty_venta)                                  AS unds_vendidas_brutas,
        SUM(qty_devol)                                  AS unds_devueltas,
        MIN(fecha) FILTER (WHERE qty_venta - qty_devol > 0) AS primera_venta,
        MAX(fecha) FILTER (WHERE qty_venta - qty_devol > 0) AS ultima_venta,
        COUNT(DISTINCT fecha) FILTER (WHERE qty_venta - qty_devol > 0) AS dias_con_venta_total,
        COUNT(DISTINCT TO_CHAR(ts, 'YYYY-MM')) FILTER (WHERE qty_venta - qty_devol > 0) AS meses_con_venta
    FROM movimientos
    GROUP BY 1, 2
),
-- Mejor mes histórico y peor mes (cuando vendió)
top_mes AS (
    SELECT DISTINCT ON (bsale_office_id, bsale_variant_id)
        bsale_office_id, bsale_variant_id, anio_mes AS mejor_mes, total AS mejor_mes_uds
    FROM (
        SELECT bsale_office_id, bsale_variant_id, anio_mes,
               SUM(qty_venta - qty_devol) AS total
        FROM movimientos
        GROUP BY 1, 2, 3
        HAVING SUM(qty_venta - qty_devol) > 0
    ) m
    ORDER BY bsale_office_id, bsale_variant_id, total DESC
),
-- Ventas por trimestre del año pasado vs trimestre del año actual (estacionalidad simple)
mismo_periodo_anio_anterior AS (
    SELECT
        bsale_office_id,
        bsale_variant_id,
        SUM(qty_venta - qty_devol) FILTER (WHERE ts BETWEEN NOW() - INTERVAL '1 year' - INTERVAL '90 days' AND NOW() - INTERVAL '1 year') AS uds_mismo_periodo_anio_pasado
    FROM movimientos
    GROUP BY 1, 2
),
-- Recepciones lifetime: cuánto entró, cuándo entró
recepciones AS (
    SELECT
        r.bsale_office_id,
        rd.bsale_variant_id,
        MIN(r.admission_date) AS primera_recepcion,
        MAX(r.admission_date) AS ultima_recepcion,
        SUM(rd.quantity)      AS unds_recibidas_total,
        COUNT(DISTINCT r.bsale_reception_id) AS num_recepciones
    FROM receptions r
    JOIN reception_details rd USING (bsale_reception_id)
    CROSS JOIN params p
    WHERE r.bsale_office_id = ANY(p.sucursales_objetivo)
    GROUP BY 1, 2
),
-- Stock actual (para distinguir "muerto con stock" vs "muerto agotado")
stock_actual AS (
    SELECT bsale_office_id, bsale_variant_id,
           quantity_available AS stock_disponible,
           quantity_reserved  AS stock_reservado
    FROM stock_levels sl
    CROSS JOIN params p
    WHERE sl.bsale_office_id = ANY(p.sucursales_objetivo)
),
jerarquia AS (
    SELECT
        p.bsale_product_id, p.name AS product_name,
        d.name AS department,
        c.id AS category_id, c.name AS category,
        s.name AS subcategory,
        d.id AS department_id
    FROM products p
    LEFT JOIN product_types pt ON p.bsale_product_type_id = pt.bsale_product_type_id
    LEFT JOIN subcategories s ON s.id = COALESCE(p.subcategory_id, pt.subcategory_id)
    LEFT JOIN categories c ON c.id = s.category_id
    LEFT JOIN departments d ON d.id = c.department_id
),
base AS (
    SELECT bsale_office_id, bsale_variant_id FROM lifetime
    UNION
    SELECT bsale_office_id, bsale_variant_id FROM recepciones
    UNION
    SELECT bsale_office_id, bsale_variant_id FROM stock_actual
),
consolidado AS (
    SELECT
        b.bsale_office_id, b.bsale_variant_id,
        o.name AS sucursal,
        v.display_code, v.is_active AS variante_activa,
        j.product_name, j.category, j.subcategory, j.department, j.department_id, j.category_id,
        COALESCE(l.unds_vendidas_neto, 0)::numeric AS unds_vendidas_lifetime,
        COALESCE(l.unds_devueltas, 0)::numeric     AS unds_devueltas_lifetime,
        l.primera_venta, l.ultima_venta,
        COALESCE(l.dias_con_venta_total, 0) AS dias_con_venta,
        COALESCE(l.meses_con_venta, 0)      AS meses_activos,
        COALESCE(r.unds_recibidas_total, 0)::numeric AS unds_recibidas_lifetime,
        r.primera_recepcion::date AS primera_recepcion,
        r.ultima_recepcion::date  AS ultima_recepcion,
        COALESCE(r.num_recepciones, 0) AS num_recepciones,
        COALESCE(sa.stock_disponible, 0)::numeric AS stock_disponible,
        COALESCE(sa.stock_reservado, 0)::numeric  AS stock_reservado,
        tm.mejor_mes, COALESCE(tm.mejor_mes_uds, 0)::numeric AS mejor_mes_uds,
        COALESCE(mp.uds_mismo_periodo_anio_pasado, 0)::numeric AS uds_mismo_periodo_anio_pasado
    FROM base b
    JOIN offices o   ON o.bsale_office_id = b.bsale_office_id
    JOIN variants v  ON v.bsale_variant_id = b.bsale_variant_id
    LEFT JOIN jerarquia j ON j.bsale_product_id = v.bsale_product_id
    LEFT JOIN lifetime l   ON l.bsale_office_id  = b.bsale_office_id AND l.bsale_variant_id  = b.bsale_variant_id
    LEFT JOIN recepciones r ON r.bsale_office_id = b.bsale_office_id AND r.bsale_variant_id = b.bsale_variant_id
    LEFT JOIN stock_actual sa ON sa.bsale_office_id = b.bsale_office_id AND sa.bsale_variant_id = b.bsale_variant_id
    LEFT JOIN top_mes tm   ON tm.bsale_office_id  = b.bsale_office_id AND tm.bsale_variant_id  = b.bsale_variant_id
    LEFT JOIN mismo_periodo_anio_anterior mp ON mp.bsale_office_id = b.bsale_office_id AND mp.bsale_variant_id = b.bsale_variant_id
    WHERE (j.department_id IS NULL OR NOT (j.department_id = ANY(CAST(:excluded_departments AS int[]))))
      AND (j.category_id IS NULL OR NOT (j.category_id = ANY(CAST(:excluded_categories AS int[]))))
),
calculos AS (
    SELECT
        c.*,
        -- Días totales de vida en catálogo (desde primera recepción)
        CASE WHEN c.primera_recepcion IS NOT NULL
             THEN DATE_PART('day', NOW() - c.primera_recepcion)::int END AS dias_en_catalogo,
        -- Días desde última recepción (regla negocio: lote debe rotar en ≤45d)
        CASE WHEN c.ultima_recepcion IS NOT NULL
             THEN DATE_PART('day', NOW() - c.ultima_recepcion)::int END AS dias_desde_ultima_recep,
        -- Días desde última venta (NULL si nunca vendió)
        CASE WHEN c.ultima_venta IS NOT NULL
             THEN DATE_PART('day', NOW() - c.ultima_venta)::int END AS dias_sin_venta,
        -- Días "activos" en el mercado: entre primera y última venta
        CASE WHEN c.primera_venta IS NOT NULL AND c.ultima_venta IS NOT NULL
             THEN GREATEST(1, (c.ultima_venta - c.primera_venta + 1)::int)
             ELSE NULL END AS dias_periodo_activo,
        -- Sell-Through lifetime
        CASE WHEN c.unds_recibidas_lifetime > 0
             THEN LEAST(999.99, ROUND((c.unds_vendidas_lifetime / c.unds_recibidas_lifetime * 100)::numeric, 1))
             ELSE NULL END AS pct_sellthrough_lifetime,
        -- Mermas / faltantes (recibido - vendido - stock)
        (c.unds_recibidas_lifetime - c.unds_vendidas_lifetime - c.stock_disponible) AS unds_merma_estimada
    FROM consolidado c
),
con_velocidad AS (
    SELECT
        c.*,
        -- Velocidad lifetime: unidades vendidas / días en catálogo
        CASE WHEN c.dias_en_catalogo > 0
             THEN ROUND((c.unds_vendidas_lifetime::numeric / c.dias_en_catalogo)::numeric, 3)
             ELSE NULL END AS vel_lifetime_uds_dia,
        -- Velocidad en periodo activo (más realista): unidades / días entre primera y última venta
        CASE WHEN c.dias_periodo_activo > 0
             THEN ROUND((c.unds_vendidas_lifetime::numeric / c.dias_periodo_activo)::numeric, 3)
             ELSE NULL END AS vel_periodo_activo_uds_dia
    FROM calculos c
)
SELECT
    sucursal                  AS "Sucursal",
    department                AS "Departamento",
    category                  AS "Categoría",
    subcategory               AS "Subcategoría",
    display_code              AS "Código SKU",
    product_name              AS "Producto",
    variante_activa           AS "Variante Activa",

    -- ===== Ciclo de vida =====
    primera_recepcion         AS "1ª Recepción",
    ultima_recepcion          AS "Últ. Recepción",
    primera_venta             AS "1ª Venta",
    ultima_venta              AS "Últ. Venta",
    dias_en_catalogo          AS "Días en Catálogo",
    dias_desde_ultima_recep   AS "Días desde Últ. Recep",
    dias_periodo_activo       AS "Días Activo (1ª→últ venta)",
    dias_sin_venta            AS "Días sin Vender",
    meses_activos             AS "Meses con Venta",
    dias_con_venta            AS "Días con Venta",

    -- ===== Volumen =====
    num_recepciones                                AS "# Recepciones",
    trim_scale(ROUND(unds_recibidas_lifetime, 2))  AS "Recibido Lifetime",
    trim_scale(ROUND(unds_vendidas_lifetime, 2))   AS "Vendido Lifetime",
    trim_scale(ROUND(unds_devueltas_lifetime, 2))  AS "Devuelto Lifetime",
    trim_scale(ROUND(stock_disponible, 2))         AS "Stock Hoy",
    trim_scale(ROUND(stock_reservado, 2))          AS "Stock Reservado",
    trim_scale(ROUND(unds_merma_estimada, 2))      AS "Merma Estimada",
    trim_scale(ROUND(pct_sellthrough_lifetime, 1)) AS "% Sell-Through Lifetime",

    -- ===== Velocidad (precisión dinámica: 4 decimales si <0.1) =====
    trim_scale(CASE WHEN vel_lifetime_uds_dia < 0.1
                    THEN ROUND(vel_lifetime_uds_dia, 4)
                    ELSE ROUND(vel_lifetime_uds_dia, 2)
               END)                                AS "Velocidad Lifetime (u/día)",
    trim_scale(CASE WHEN vel_periodo_activo_uds_dia < 0.1
                    THEN ROUND(vel_periodo_activo_uds_dia, 4)
                    ELSE ROUND(vel_periodo_activo_uds_dia, 2)
               END)                                AS "Velocidad Periodo Activo (u/día)",

    -- ===== Estacionalidad =====
    mejor_mes                                          AS "Mejor Mes",
    trim_scale(ROUND(mejor_mes_uds, 2))                AS "Uds Mejor Mes",
    trim_scale(ROUND(uds_mismo_periodo_anio_pasado, 2)) AS "Uds Mismo Periodo Año Pasado",

    -- ===== Diagnóstico final del ciclo =====
    CASE
        -- Nunca tuvo recepción ni venta
        WHEN unds_recibidas_lifetime = 0 AND unds_vendidas_lifetime = 0
             THEN '🌀 Fantasma de catálogo (jamás operó aquí)'

        -- Recibido pero nunca vendido
        WHEN unds_recibidas_lifetime > 0 AND unds_vendidas_lifetime = 0
             THEN '☠️ FRACASO TOTAL: recibido pero nunca vendido'

        -- Mucho stock, baja venta histórica
        WHEN pct_sellthrough_lifetime < 30 AND unds_recibidas_lifetime >= 10 AND stock_disponible > 0
             THEN '🧱 ATASCADO: vendió <30% de lo recibido'

        -- Producto descatalogado limpio (vendió todo, sin stock, hace tiempo)
        WHEN stock_disponible = 0 AND dias_sin_venta > 90 AND pct_sellthrough_lifetime >= 70
             THEN '✅ CICLO CERRADO: vendido y agotado (no se repuso)'

        -- Producto descatalogado con merma (no vendió todo, ni queda stock)
        WHEN stock_disponible = 0 AND dias_sin_venta > 90 AND pct_sellthrough_lifetime < 70
             THEN '⚰️ DESCATALOGADO con pérdidas (merma o no se halla)'

        -- Regla de permanencia: lote sin rotar >45d desde última recepción
        WHEN stock_disponible > 0 AND dias_desde_ultima_recep > 45
             THEN '🐢 LOTE SIN ROTAR (>45d desde última recep → liquidar/promocionar)'

        -- Producto inactivo con stock parado
        WHEN dias_sin_venta > 60 AND stock_disponible > 0
             THEN '🧊 CAPITAL ESTANCADO (stock parado >60d)'

        -- Posible estacional
        WHEN uds_mismo_periodo_anio_pasado >= GREATEST(5, mejor_mes_uds * 0.5) AND dias_sin_venta > 30
             THEN '🌗 ESTACIONAL: vendía bien en este periodo el año pasado'

        -- Activo (no debería caer aquí salvo casos raros)
        WHEN dias_sin_venta <= 60
             THEN '🟢 VIGENTE (también aparece en la matriz operativa)'

        ELSE '❔ Caso por revisar'
    END                       AS "Diagnóstico Ciclo Vida"

FROM con_velocidad
ORDER BY sucursal, unds_vendidas_lifetime DESC NULLS LAST, category;
