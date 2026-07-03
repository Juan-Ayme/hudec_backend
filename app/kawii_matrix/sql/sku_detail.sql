SELECT
    o.name                                         AS sucursal,
    mr.bsale_office_id                             AS office_id,
    v.display_code                                 AS sku,
    j.product_name                                 AS product_name,
    j.department, j.category, j.subcategory,
    j.department_id, j.category_id, j.subcategory_id,
    (mr.department_id = ANY(CAST(:seasonal_departments AS int[]))) AS is_seasonal_dept,

    mr.stock_disponible::numeric                   AS stock_disponible,
    mr.stock_reservado::numeric                    AS stock_reservado,
    COALESCE(sa.stock_almacen, 0)::numeric         AS stock_almacen,

    mr.unds_vendidas::numeric                      AS unds_vendidas,
    mr.unds_vendidas_30d::numeric                  AS unds_vendidas_30d,
    mr.dias_con_ventas                             AS dias_con_ventas,
    mr.dias_sin_venta_90d                          AS dias_sin_venta_90d,
    mr.v_recent_45d::numeric                       AS v_recent_45d,
    mr.v_old_45d::numeric                          AS v_old_45d,
    mr.vel_30d                                     AS vel_30d,
    mr.dias_con_stock_30d                          AS dias_con_stock_30d,
    mr.ultima_venta                                AS ultima_venta_90d,

    mr.unds_vendidas_lifetime::numeric             AS unds_vendidas_lifetime,
    mr.ult_venta_lifetime                          AS ult_venta_lifetime,
    mr.unds_recibidas_lifetime::numeric            AS unds_recibidas_lifetime,
    mr.primera_recepcion                           AS primera_recepcion,
    mr.unds_consumidas_lifetime::numeric           AS unds_consumidas_lifetime,
    mr.unds_trasladadas_lifetime::numeric          AS unds_trasladadas_lifetime,
    mr.pct_sellthrough_lifetime                    AS pct_sellthrough_lifetime,
    mr.edad_dias                                   AS edad_dias,

    mr.primera_recep_90d                           AS primera_recep_90d,
    mr.ultima_recepcion                            AS ultima_recepcion,
    mr.dias_desde_ultima_recep                     AS dias_desde_ultima_recep,
    mr.ult_recep_qty::numeric                      AS ult_recep_qty,
    mr.unds_post_recep::numeric                    AS unds_post_recep,
    mr.unds_recibidas_90d::numeric                 AS unds_recibidas_90d,
    mr.unds_lote_total::numeric                    AS unds_lote_total,
    mr.dias_con_stock                              AS dias_con_stock,
    mr.dias_exhibido                               AS dias_exhibido,
    mr.dias_con_venta_lote                         AS dias_con_venta_lote,
    mr.ult_venta_lote                              AS ult_venta_lote,
    mr.pri_venta_lote                              AS pri_venta_lote,
    mr.dias_absorcion_lote                         AS dias_absorcion_lote,

    mr.ventas_dia                                  AS ventas_dia,
    mr.proy_mes::numeric                           AS proy_mes,
    mr.proy_30d_reciente                           AS proy_30d_reciente,
    mr.proy_post_recep                             AS proy_post_recep,
    mr.dias_cobertura                              AS dias_cobertura,
    mr.dias_cobertura_reciente                     AS dias_cobertura_reciente,
    mr.cob_post_recep                              AS cob_post_recep,
    mr.pct_frecuencia                              AS pct_frecuencia,
    mr.tdpv::numeric                               AS tdpv,
    mr.monto_vendido_90d::numeric                  AS monto_vendido_90d,

    cb.avg_proy_cat                                AS avg_proy_cat
FROM metricas_reciente mr
JOIN variants v   ON v.bsale_variant_id  = mr.bsale_variant_id AND v.is_active
JOIN offices o    ON o.bsale_office_id   = mr.bsale_office_id
JOIN jerarquia j  ON j.bsale_product_id  = v.bsale_product_id
LEFT JOIN cat_baseline cb
       ON cb.bsale_office_id = mr.bsale_office_id
      AND cb.cat_name        = COALESCE(mr.category, '(sin)')
LEFT JOIN stock_almacen sa
       ON sa.bsale_variant_id = mr.bsale_variant_id
WHERE v.display_code = :sku_filter
ORDER BY o.name;
