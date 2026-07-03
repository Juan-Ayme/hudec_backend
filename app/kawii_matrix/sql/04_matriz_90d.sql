-- =============================================================
-- MATRIZ 90D — Foto pura del "ahora" (sin contexto histórico)
-- =============================================================
-- ⚠️ Este archivo NO es una consulta completa: es el SELECT final del
--    módulo 04 sobre la CTE `matriz` de `_matriz_90d_base.sql`
--    (service._load_sql concatena base + este archivo).
--    La lógica de negocio (métricas + cascada) vive en la base.
--
-- El 04 es la foto operativa pura: proyecta las columnas estándar,
-- SIN filtro de fantasmas (a diferencia de 04b/05) y sin columnas extra.
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
    "XYZ",
    "Cobertura",
    "% Rotación Stock",
    "% Demanda vs Reposición",
    "Tendencia",
    "Sugerencia Transferencia",
    "Clasificación"
FROM matriz
ORDER BY "Sucursal", "Categoría", "% Rotación Stock" DESC NULLS LAST;
