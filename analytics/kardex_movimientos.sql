-- =============================================================================
-- KAWII | Kardex de Movimientos de Stock por SKU
-- =============================================================================
-- CORRECCIÓN APLICADA (2026-05-06 - Bug de Timezone):
--
--   PROBLEMA ORIGINAL:
--     Las fechas de ventas se mostraban en UTC. Documentos emitidos despues
--     de las 19:00 hora Lima (UTC-5) aparecian con la fecha del dia siguiente,
--     sin coincidir con lo que muestra BSale en su interfaz.
--
--   SOLUCIÓN:
--     Todas las fechas ahora se extraen directamente en UTC:
--       AT TIME ZONE 'UTC'
--
--     ANTES (bugged):
--       d.emission_date AS fecha_movimiento
--       → Mostraba "2025-03-16 02:30" para una venta de las 21:30 Lima
--
--     AHORA (correcto para BSale):
--       (d.emission_date AT TIME ZONE 'UTC') AS fecha_movimiento
--       → Muestra "2025-03-16 00:00" = la fecha exacta del ticket en BSale
--
-- USO: Pega esta query en pgAdmin y reemplaza 'FXT-2289' por el SKU deseado.
-- =============================================================================

WITH kardex_movimientos AS (

    -- ── BLOQUE 1: INGRESOS (Compras / Recepciones de proveedor) ──────────────
    SELECT
        -- FIX: Extraer en UTC porque BSale guarda la fecha a medianoche UTC
        (r.admission_date AT TIME ZONE 'UTC')                   AS fecha_movimiento,
        r.admission_date                                         AS fecha_movimiento_utc,  -- referencia
        o.name                                                   AS sucursal,
        'INGRESO (COMPRA/RECEPCION)'                             AS tipo_operacion,
        COALESCE(r.document_ref, 'Sin Documento')               AS comprobante,
        rd.quantity                                              AS cantidad_movida
    FROM public.reception_details rd
    JOIN public.receptions r ON rd.bsale_reception_id = r.bsale_reception_id
    JOIN public.offices    o ON r.bsale_office_id     = o.bsale_office_id
    JOIN public.variants   v ON rd.bsale_variant_id   = v.bsale_variant_id
    WHERE v.display_code = 'FXT-2289'

    UNION ALL

    -- ── BLOQUE 2: SALIDAS y DEVOLUCIONES (Ventas y Notas de Crédito) ─────────
    SELECT
        -- FIX: Extraer en UTC porque BSale guarda la fecha a medianoche UTC
        (d.emission_date AT TIME ZONE 'UTC')                    AS fecha_movimiento,
        d.emission_date                                          AS fecha_movimiento_utc,  -- referencia
        o.name                                                   AS sucursal,
        CASE
            WHEN d.is_credit_note = TRUE THEN 'INGRESO (NOTA DE CREDITO)'
            ELSE 'SALIDA (VENTA)'
        END                                                      AS tipo_operacion,
        CONCAT(d.serial_number, '-', d.doc_number)              AS comprobante,
        CASE
            WHEN d.is_credit_note = TRUE THEN  dd.quantity        -- NC: devuelve unidades
            ELSE                              (dd.quantity * -1)   -- Venta: sale stock
        END                                                      AS cantidad_movida
    FROM public.document_details dd
    JOIN public.documents d ON dd.bsale_document_id = d.bsale_document_id
    JOIN public.offices   o ON d.bsale_office_id    = o.bsale_office_id
    JOIN public.variants  v ON dd.bsale_variant_id  = v.bsale_variant_id
    WHERE v.display_code = 'FXT-2289'
      AND d.is_active = TRUE

    UNION ALL

    -- ── BLOQUE 3: SALIDAS (Consumos / Ajustes Manuales) ─────────
    SELECT
        -- FIX: Extraer en UTC porque BSale guarda la fecha a medianoche UTC
        (c.consumption_date AT TIME ZONE 'UTC')                 AS fecha_movimiento,
        c.consumption_date                                       AS fecha_movimiento_utc,  -- referencia
        o.name                                                   AS sucursal,
        'SALIDA (CONSUMO/AJUSTE)'                                AS tipo_operacion,
        COALESCE(c.note, 'Consumo Interno')                      AS comprobante,
        (cd.quantity * -1)                                       AS cantidad_movida
    FROM public.consumption_details cd
    JOIN public.consumptions c ON cd.bsale_consumption_id = c.bsale_consumption_id
    JOIN public.offices      o ON c.bsale_office_id       = o.bsale_office_id
    JOIN public.variants     v ON cd.bsale_variant_id     = v.bsale_variant_id
    WHERE v.display_code = 'FXT-2289'

)

-- ── REPORTE FINAL ─────────────────────────────────────────────────────────────
-- Columnas de diagnóstico incluidas:
--   fecha_movimiento_utc : la fecha RAW en UTC que guarda la DB
--   fecha_movimiento     : la fecha CORREGIDA (extraída en UTC)
--   desfase_horas        : diferencia (si la hubiera)
--   alerta_desfase       : marca SI la conversión Lima cambiaba la fecha
--                          (esto es el bug que causaba las inconsistencias)
SELECT
    -- Fecha corregida (igual a BSale)
    fecha_movimiento,
    -- Fecha raw de la DB (para diagnóstico)
    fecha_movimiento_utc,
    -- Alerta: si estos valores de DATE difieren, ese movimiento estaba mal clasificado
    CASE
        WHEN fecha_movimiento_utc::date
             != (fecha_movimiento_utc AT TIME ZONE 'America/Lima')::date
        THEN '*** DESFASE (ATRASO POR LIMA) ***'
        ELSE 'OK'
    END                                                          AS alerta_desfase,
    sucursal,
    tipo_operacion,
    comprobante,
    cantidad_movida,
    -- Saldo acumulado por sucursal ordenado por fecha Lima (correcto)
    SUM(cantidad_movida) OVER (
        PARTITION BY sucursal
        ORDER BY fecha_movimiento ASC, comprobante ASC
    )                                                            AS saldo_stock_calculado
FROM kardex_movimientos
ORDER BY sucursal ASC, fecha_movimiento ASC, comprobante ASC;


-- =============================================================================
-- QUERY DE VERIFICACION RAPIDA:
-- Cuántos movimientos de este SKU tenían fecha desfasada (bug UTC vs Lima)
-- =============================================================================
/*
WITH movimientos AS (
    SELECT d.emission_date
    FROM public.document_details dd
    JOIN public.documents d ON dd.bsale_document_id = d.bsale_document_id
    JOIN public.variants  v ON dd.bsale_variant_id   = v.bsale_variant_id
    WHERE v.display_code = 'FXT-2289'
      AND d.is_active = TRUE
)
SELECT
    COUNT(*) AS total_movimientos,
    COUNT(*) FILTER (
        WHERE emission_date::date
              != (emission_date AT TIME ZONE 'America/Lima')::date
    )                AS movimientos_con_desfase,
    ROUND(
        100.0 * COUNT(*) FILTER (
            WHERE emission_date::date
                  != (emission_date AT TIME ZONE 'America/Lima')::date
        ) / NULLIF(COUNT(*), 0),
    1)               AS pct_afectados
FROM movimientos;
*/
