-- =============================================================
-- MATRIZ 90D JERÁRQUICA — Foto del "ahora" con totales por nivel
-- =============================================================
-- ⚠️ Este archivo NO es una consulta completa: es el SELECT final del
--    módulo 04b sobre la CTE `matriz` de `_matriz_90d_base.sql`
--    (service._load_sql concatena base + este archivo).
--    La lógica de negocio (métricas + cascada) vive en la base.
--
-- Variante del módulo 04 que agrega:
--   • "Fecha Últ. Venta" (última venta lifetime)
--   • Ventas en S/ por nivel: "Vendido SKU/Subcat/Cat/Depto S/"
--   • Participación del SKU en S/: "% S/ en Subcat / Cat / Depto"
--   • Filtro de fantasmas (igual al 05)
-- Las ventanas jerárquicas se calculan AQUÍ (no en la base) porque deben
-- correr DESPUÉS del filtro de fantasmas (WHERE se aplica antes que las
-- window functions): los totales por nivel excluyen a los fantasmas.
-- =============================================================
SELECT
    "Sucursal",
    "Departamento",
    "Categoría",
    "Subcategoría",
    "Código SKU",
    "Producto",
    "1ª Recepción",
    "Últ. Recepción",
    "Últ. Venta (90d)",
    "Fecha Últ. Venta",
    "Edad SKU (días)",
    "Llegó hace (días)",
    "Días Exhibido",
    "Unds Vend (90d)",
    "Unds Recib (90d)",
    "Vend Lote Total",
    "Sell-through Lote %",
    "Vida lote (días)",
    "Días Absorción Lote",
    "Días Agotado",
    "Últ. Venta Lote",
    "1ª Venta Lote",
    "Stock Disp",
    "Stock Reserv",
    "Stock Almacén",
    "Velocidad (uds/día)",
    "Proyección 30d",
    "Vel últimos 30d",
    "Proy 30d (reciente)",
    "Proy Post-Recep",
    "Cob Post-Recep",

    -- ★ Ventas en MONTO MONETARIO (S/) — SKU individual y totales jerárquicos.
    --    Ejemplo: vendió 2 unds a S/ 19.90 = S/ 39.80. Útil para ver el peso económico.
    "Vendido SKU S/",
    ROUND(SUM(monto_vendido_90d) OVER (PARTITION BY bsale_office_id, subcategory_id), 2) AS "Vendido Subcat S/",
    ROUND(SUM(monto_vendido_90d) OVER (PARTITION BY bsale_office_id, category_id), 2) AS "Vendido Cat S/",
    ROUND(SUM(monto_vendido_90d) OVER (PARTITION BY bsale_office_id, department_id), 2) AS "Vendido Depto S/",

    -- ★ Participación del SKU en MONTO S/ (% del nivel) — importante porque
    --    un SKU caro puede pesar más en dinero aunque venda menos unidades.
    CASE WHEN SUM(monto_vendido_90d) OVER (PARTITION BY bsale_office_id, subcategory_id) > 0
         THEN ROUND((monto_vendido_90d / SUM(monto_vendido_90d) OVER (PARTITION BY bsale_office_id, subcategory_id) * 100)::numeric, 1)
         ELSE 0::numeric END AS "% S/ en Subcat",
    CASE WHEN SUM(monto_vendido_90d) OVER (PARTITION BY bsale_office_id, category_id) > 0
         THEN ROUND((monto_vendido_90d / SUM(monto_vendido_90d) OVER (PARTITION BY bsale_office_id, category_id) * 100)::numeric, 1)
         ELSE 0::numeric END AS "% S/ en Cat",
    CASE WHEN SUM(monto_vendido_90d) OVER (PARTITION BY bsale_office_id, department_id) > 0
         THEN ROUND((monto_vendido_90d / SUM(monto_vendido_90d) OVER (PARTITION BY bsale_office_id, department_id) * 100)::numeric, 1)
         ELSE 0::numeric END AS "% S/ en Depto",

    "% Frecuencia",
    "Días con Venta",
    "Días sin Vender",
    "XYZ",
    "Cobertura",
    "% Rotación Stock",
    "% Demanda vs Reposición",
    "Tendencia",
    "Sugerencia Transferencia",
    "Clasificación"
FROM matriz
-- ★ Filtro fantasmas (mismo que matriz 05): excluye SKUs con stock=0, sin ventas
-- en 90d y cuya última venta histórica es NULL o anterior a 180d. Limpia el
-- dashboard de SKUs muertos que ensucian la jerarquía sin aportar dato útil.
WHERE NOT (
    stock_disponible = 0
    AND unds_vendidas = 0
    AND (ult_venta_lifetime IS NULL OR ult_venta_lifetime < NOW() - INTERVAL '180 days')
)
ORDER BY "Sucursal", "Categoría", "% Rotación Stock" DESC NULLS LAST;
