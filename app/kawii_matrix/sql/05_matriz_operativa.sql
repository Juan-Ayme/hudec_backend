-- =============================================================
-- MATRIZ OPERATIVA v4 — Matriz operativa enriquecida con contexto histórico
-- =============================================================
-- ⚠️ Este archivo NO es una consulta completa: es el SELECT final del
--    módulo 05 sobre la CTE `matriz` de `_matriz_90d_base.sql`
--    (service._load_sql concatena base + este archivo).
--    La lógica de negocio (métricas + cascada) vive en la base.
--
-- Variante del módulo 04 que agrega contexto de VIDA del producto:
--   • "Fecha Últ. Venta" en lugar de "Últ. Venta (90d)"
--   • Bloque lifetime: Unds Vend/Recib Lifetime, % Sell-Through Lifetime,
--     Mejor Mes, Uds Mejor Mes, Índice Contribución
--   • Filtro de fantasmas (igual al 04b)
-- ⚠️ DIVERGENCIA HISTÓRICA preservada: proyecta `xyz_legacy` (variante vieja
--    de "XYZ" basada en unds_post_recep, marca 'Sin datos' de más). Para
--    alinearla al fix del 04/04b, proyectar "XYZ" directamente.
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
    "% Frecuencia",
    "Días con Venta",
    "Días sin Vender",
    xyz_legacy AS "XYZ",
    "Cobertura",
    "% Rotación Stock",
    "% Demanda vs Reposición",

    -- ===== Bloque LIFETIME (contexto histórico) =====
    "Unds Vend Lifetime",
    "Unds Recib Lifetime",
    "% Sell-Through Lifetime",
    "Mejor Mes",
    "Uds Mejor Mes",
    "Índice Contribución",

    "Tendencia",
    "Sugerencia Transferencia",
    "Clasificación"
FROM matriz
-- Filtro fantasmas: sin stock, sin ventas en 90d, sin venta histórica reciente
WHERE NOT (
    stock_disponible = 0
    AND unds_vendidas = 0
    AND (ult_venta_lifetime IS NULL OR ult_venta_lifetime < NOW() - INTERVAL '180 days')
)
ORDER BY "Sucursal", "Categoría", "% Rotación Stock" DESC NULLS LAST;
