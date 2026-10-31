from datetime import date, datetime, timedelta
import io

from fastapi import Depends, APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text

from app.auth import CurrentCompany, get_current_company
from app.config import get_settings
from app.database import get_db
from analytics.excel_inventario import build_inventario_workbook

router = APIRouter(
    prefix="/stock",
    tags=["stock"],
    dependencies=[Depends(get_current_company)],
)


# ============================================================================
# ENDPOINTS COMENTADOS (2026-06-20) — no consumidos por el frontend.
# Razón: getStockLevels, getStockTop y getStockHistory están definidos en
# lib/api.ts pero ninguna página los importa. Se mantiene /stock/valuation
# (usado por Dashboard y selector global de sucursal).
# Se preservan las queries SQL para reactivar si vuelven a necesitarse.
# ============================================================================

# @router.get("/levels")
# async def stock_levels(
#     office_id: int | None = Query(None, description="Filtrar por sucursal BSale"),
#     only_with_stock: bool = True,
#     limit: int = Query(200, ge=1, le=2000),
#     db: AsyncSession = Depends(get_db)
# ) -> list[dict]:
#     where = []
#     params = {}
#
#     if office_id:
#         where.append("sl.bsale_office_id = :office_id")
#         params["office_id"] = office_id
#     if only_with_stock:
#         where.append("sl.quantity_available > 0")
#     where_sql = ("WHERE " + " AND ".join(where)) if where else ""
#
#     params["limit"] = limit
#     sql = f"""
#         SELECT sl.bsale_variant_id, sl.bsale_office_id,
#                o.name AS office_name, v.code AS variant_code,
#                p.name AS product_name,
#                sl.quantity_available, sl.quantity_reserved
#         FROM stock_levels sl
#         JOIN offices o          ON o.bsale_office_id = sl.bsale_office_id
#         JOIN variants v         ON v.bsale_variant_id = sl.bsale_variant_id
#         JOIN products p         ON p.bsale_product_id = v.bsale_product_id
#         {where_sql}
#         ORDER BY sl.quantity_available DESC
#         LIMIT :limit
#     """
#     res = await db.execute(text(sql), params)
#     return [dict(r) for r in res.mappings().all()]


@router.get("/valuation")
async def stock_valuation(
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Stock valorizado por sucursal (usa effective_cost).

    Devuelve bsale_office_id para que el frontend pueda filtrar por el ID real
    de BSale (1, 3, 4) en lugar de hacer un mapeo posicional.
    """
    res = await db.execute(text("""
        SELECT o.bsale_office_id, o.name AS sucursal,
               ROUND(SUM(sl.quantity_available * COALESCE(vco.effective_cost, vc.effective_cost, 0))::numeric, 2) AS valor_soles,
               SUM(sl.quantity_available) AS unidades
        FROM stock_levels sl
        JOIN offices o              ON o.bsale_office_id  = sl.bsale_office_id  AND o.company_id  = sl.company_id
        LEFT JOIN variant_costs vc  ON vc.bsale_variant_id = sl.bsale_variant_id AND vc.company_id = sl.company_id
        LEFT JOIN variant_costs_by_office vco ON vco.bsale_variant_id = sl.bsale_variant_id AND vco.company_id = sl.company_id AND vco.bsale_office_id = sl.bsale_office_id
        WHERE sl.company_id = :cid
          AND sl.quantity_available > 0
        GROUP BY o.bsale_office_id, o.name
        ORDER BY valor_soles DESC
    """), {"cid": company.company_id})
    rows = [dict(r) for r in res.mappings().all()]

    total = sum(float(r["valor_soles"] or 0) for r in rows)
    return {
        "total_soles": round(total, 2),
        "por_sucursal": [
            {
                "bsale_office_id": r["bsale_office_id"],
                "sucursal": r["sucursal"],
                "valor_soles": float(r["valor_soles"] or 0),
                "unidades": float(r["unidades"] or 0),
            }
            for r in rows
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Descarga de Excel de Inventario y Catálogo Completo (con estética KAWII Matrix)
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/excel",
    summary="Descarga el catálogo completo con stock, precios y fechas históricas (.xlsx)",
    response_class=StreamingResponse,
)
@router.get(
    "/inventario/excel",
    summary="Descarga el catálogo completo con stock, precios y fechas históricas (.xlsx)",
    response_class=StreamingResponse,
)
async def stock_inventory_excel(
    office_id: int | None = Query(None, description="ID de sucursal BSale puntual; si es null, consolida todas las sucursales"),
    department: str | None = Query(None, description="Filtro opcional por departamento"),
    category: str | None = Query(None, description="Filtro opcional por categoría"),
    subcategory: str | None = Query(None, description="Filtro opcional por subcategoría"),
    only_with_stock: bool = Query(False, description="Si es True, solo retorna SKUs con stock disponible > 0"),
    include_inactive: bool = Query(False, description="Si es True, incluye productos o variantes inactivas"),
    q: str | None = Query(None, description="Búsqueda opcional por nombre de producto, SKU o código de barra"),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Genera un Excel maquetado idéntico a la matriz KAWII con datos frescos de stock y fechas.

    Incluye el 100% de los productos del catálogo (sin filtros de ventanas temporales):
      - Fechas históricas completas: Fecha de Creación / Primer Ingreso, Última Recepción, Última Venta.
      - Stock disponible, reservado y físico (por sucursal o consolidado).
      - Costo unitario, precio de venta con IGV y valorización económica (en costo y venta).
      - Estructura con Portada, Índice de departamentos con enlaces e hipervínculos, y pestañas
        por Departamento con outline colapsable (+/-) por Categoría y Subcategoría.
    """
    from typing import Any
    settings = get_settings()
    cid = company.company_id
    t_start = datetime.now()

    # RLS & Planner performance
    await db.execute(text(f"SET LOCAL app.current_company = '{int(cid)}'"))
    await db.execute(text("SET LOCAL enable_nestloop = off"))

    # Oficinas activas de la empresa
    res_offices = await db.execute(text("""
        SELECT bsale_office_id, name 
        FROM offices 
        WHERE company_id = :cid AND is_active = TRUE 
        ORDER BY bsale_office_id
    """), {"cid": cid})
    all_offices = [(r[0], r[1]) for r in res_offices.fetchall()]

    target_office_name: str | None = None
    if office_id is not None:
        matched = [oname for oid, oname in all_offices if oid == office_id]
        if not matched:
            raise HTTPException(400, f"Sucursal con ID {office_id} no encontrada o inactiva")
        target_office_name = matched[0]

    where_clauses = ["v.company_id = :cid"]
    params: dict[str, Any] = {"cid": cid}

    if not include_inactive:
        where_clauses.append("(v.is_active = TRUE AND pf.is_active = TRUE)")

    if department:
        where_clauses.append("pf.department = :department")
        params["department"] = department
    if category:
        where_clauses.append("pf.category = :category")
        params["category"] = category
    if subcategory:
        where_clauses.append("pf.subcategory = :subcategory")
        params["subcategory"] = subcategory
    if q:
        where_clauses.append("(pf.product_name ILIKE :q OR v.display_code ILIKE :q OR v.code ILIKE :q OR v.bar_code ILIKE :q)")
        params["q"] = f"%{q.strip()}%"

    if office_id is not None:
        # Modo Sucursal Puntual
        if only_with_stock:
            where_clauses.append("COALESCE(sl.quantity_available, 0) > 0")

        params["oid"] = office_id
        where_sql = " AND ".join(where_clauses)

        sql = f"""
        WITH recep_historia AS (
            SELECT
                rd.bsale_variant_id,
                MIN(r.admission_date)::date AS primera_recepcion,
                MAX(r.admission_date)::date AS ultima_recepcion
            FROM reception_details rd
            JOIN receptions r 
              ON r.company_id = rd.company_id 
             AND r.bsale_reception_id = rd.bsale_reception_id
            WHERE rd.company_id = :cid
              AND r.bsale_office_id = :oid
            GROUP BY rd.bsale_variant_id
        ),
        ventas_historia AS (
            SELECT
                dd.bsale_variant_id,
                MAX(d.emission_date)::date AS ultima_venta
            FROM document_details dd
            JOIN documents d 
              ON d.company_id = dd.company_id 
             AND d.bsale_document_id = dd.bsale_document_id
            WHERE dd.company_id = :cid
              AND d.is_credit_note = FALSE
              AND d.is_active = TRUE
              AND d.bsale_office_id = :oid
            GROUP BY dd.bsale_variant_id
        )
        SELECT
            '{target_office_name}'                                      AS "Sucursal",
            COALESCE(pf.department, '— Sin departamento')               AS "Departamento",
            COALESCE(pf.category, '— Sin categoría')                   AS "Categoría",
            COALESCE(pf.subcategory, '— Sin subcategoría')             AS "Subcategoría",
            v.display_code                                              AS "Código SKU",
            v.bar_code                                                  AS "Código de Barras",
            pf.product_name                                             AS "Producto",
            v.description                                               AS "Detalle / Variante",
            v.unit                                                      AS "Unidad",
            CASE WHEN v.is_active AND pf.is_active THEN 'Activo' ELSE 'Inactivo' END AS "Estado",
            
            COALESCE(rh.primera_recepcion, v.synced_at::date)           AS "Fecha Creación / Ingreso",
            rh.ultima_recepcion                                         AS "Última Recepción",
            vh.ultima_venta                                             AS "Última Venta",
            
            COALESCE(sl.quantity_available, 0)                          AS "Stock Disp",
            COALESCE(sl.quantity_reserved, 0)                           AS "Stock Reserv",
            COALESCE(sl.quantity, 0)                                    AS "Stock Físico",
            
            ROUND(COALESCE(vco.effective_cost, vc.effective_cost, 0)::numeric, 2) AS "Costo Unitario S/",
            ROUND(COALESCE(pld.value_with_taxes, 0)::numeric, 2)       AS "Precio Venta S/",
            
            ROUND((COALESCE(sl.quantity_available, 0) * COALESCE(vco.effective_cost, vc.effective_cost, 0))::numeric, 2) AS "Valor Stock Costo S/",
            ROUND((COALESCE(sl.quantity_available, 0) * COALESCE(pld.value_with_taxes, 0))::numeric, 2)                  AS "Valor Stock Venta S/"
        FROM variants v
        JOIN v_products_full pf 
          ON pf.company_id = v.company_id 
         AND pf.bsale_product_id = v.bsale_product_id
        LEFT JOIN stock_levels sl 
          ON sl.company_id = v.company_id 
         AND sl.bsale_variant_id = v.bsale_variant_id
         AND sl.bsale_office_id = :oid
        LEFT JOIN recep_historia rh 
          ON rh.bsale_variant_id = v.bsale_variant_id
        LEFT JOIN ventas_historia vh 
          ON vh.bsale_variant_id = v.bsale_variant_id
        LEFT JOIN variant_costs vc 
          ON vc.company_id = v.company_id 
         AND vc.bsale_variant_id = v.bsale_variant_id
        LEFT JOIN variant_costs_by_office vco 
          ON vco.company_id = v.company_id 
         AND vco.bsale_office_id = :oid 
         AND vco.bsale_variant_id = v.bsale_variant_id
        LEFT JOIN LATERAL (
            SELECT pld_sub.value_with_taxes
            FROM offices o
            JOIN price_list_details pld_sub 
              ON pld_sub.company_id = v.company_id 
             AND pld_sub.bsale_price_list_id = o.default_price_list_id 
             AND pld_sub.bsale_variant_id = v.bsale_variant_id
            WHERE o.company_id = v.company_id AND o.bsale_office_id = :oid
            LIMIT 1
        ) pld ON TRUE
        WHERE {where_sql}
        ORDER BY "Departamento", "Categoría", "Subcategoría", "Producto"
        """
    else:
        # Modo Consolidado (todas las sucursales activas)
        if only_with_stock:
            where_clauses.append("COALESCE(sp.stock_disp_total, 0) > 0")

        where_sql = " AND ".join(where_clauses)

        stock_case_parts = []
        stock_cols_select = []
        for oid, oname in all_offices:
            clean_name = oname.replace('"', '').strip()
            stock_case_parts.append(
                f'SUM(CASE WHEN sl.bsale_office_id = {oid} THEN sl.quantity_available ELSE 0 END) AS "Stock {clean_name}"'
            )
            stock_cols_select.append(
                f'COALESCE(sp."Stock {clean_name}", 0) AS "Stock {clean_name}"'
            )

        stock_cases_sql = ",\n        ".join(stock_case_parts) if stock_case_parts else "0 AS dummy_stock"
        stock_cols_sql = ",\n    ".join(stock_cols_select) if stock_cols_select else "0 AS dummy_stock"

        sql = f"""
        WITH recep_historia AS (
            SELECT
                rd.bsale_variant_id,
                MIN(r.admission_date)::date AS primera_recepcion,
                MAX(r.admission_date)::date AS ultima_recepcion
            FROM reception_details rd
            JOIN receptions r 
              ON r.company_id = rd.company_id 
             AND r.bsale_reception_id = rd.bsale_reception_id
            WHERE rd.company_id = :cid
            GROUP BY rd.bsale_variant_id
        ),
        ventas_historia AS (
            SELECT
                dd.bsale_variant_id,
                MAX(d.emission_date)::date AS ultima_venta
            FROM document_details dd
            JOIN documents d 
              ON d.company_id = dd.company_id 
             AND d.bsale_document_id = dd.bsale_document_id
            WHERE dd.company_id = :cid
              AND d.is_credit_note = FALSE
              AND d.is_active = TRUE
            GROUP BY dd.bsale_variant_id
        ),
        stock_pivote AS (
            SELECT
                sl.bsale_variant_id,
                {stock_cases_sql},
                SUM(sl.quantity_available) AS stock_disp_total,
                SUM(sl.quantity_reserved)  AS stock_reserv_total,
                SUM(sl.quantity)           AS stock_fisico_total
            FROM stock_levels sl
            WHERE sl.company_id = :cid
            GROUP BY sl.bsale_variant_id
        )
        SELECT
            COALESCE(pf.department, '— Sin departamento')               AS "Departamento",
            COALESCE(pf.category, '— Sin categoría')                   AS "Categoría",
            COALESCE(pf.subcategory, '— Sin subcategoría')             AS "Subcategoría",
            v.display_code                                              AS "Código SKU",
            v.bar_code                                                  AS "Código de Barras",
            pf.product_name                                             AS "Producto",
            v.description                                               AS "Detalle / Variante",
            v.unit                                                      AS "Unidad",
            CASE WHEN v.is_active AND pf.is_active THEN 'Activo' ELSE 'Inactivo' END AS "Estado",
            
            COALESCE(rh.primera_recepcion, v.synced_at::date)           AS "Fecha Creación / Ingreso",
            rh.ultima_recepcion                                         AS "Última Recepción",
            vh.ultima_venta                                             AS "Última Venta",
            
            {stock_cols_sql},
            COALESCE(sp.stock_disp_total, 0)                            AS "Stock Disp Total",
            COALESCE(sp.stock_reserv_total, 0)                          AS "Stock Reserv Total",
            COALESCE(sp.stock_fisico_total, 0)                          AS "Stock Físico Total",
            
            ROUND(COALESCE(vc.effective_cost, 0)::numeric, 2)           AS "Costo Unitario S/",
            ROUND(COALESCE(pld.value_with_taxes, 0)::numeric, 2)       AS "Precio Venta S/",
            
            ROUND((COALESCE(sp.stock_disp_total, 0) * COALESCE(vc.effective_cost, 0))::numeric, 2)     AS "Valor Stock Costo S/",
            ROUND((COALESCE(sp.stock_disp_total, 0) * COALESCE(pld.value_with_taxes, 0))::numeric, 2) AS "Valor Stock Venta S/"
        FROM variants v
        JOIN v_products_full pf 
          ON pf.company_id = v.company_id 
         AND pf.bsale_product_id = v.bsale_product_id
        LEFT JOIN stock_pivote sp 
          ON sp.bsale_variant_id = v.bsale_variant_id
        LEFT JOIN recep_historia rh 
          ON rh.bsale_variant_id = v.bsale_variant_id
        LEFT JOIN ventas_historia vh 
          ON vh.bsale_variant_id = v.bsale_variant_id
        LEFT JOIN variant_costs vc 
          ON vc.company_id = v.company_id 
         AND vc.bsale_variant_id = v.bsale_variant_id
        LEFT JOIN LATERAL (
            SELECT pld_sub.value_with_taxes
            FROM price_list_details pld_sub
            WHERE pld_sub.company_id = v.company_id
              AND pld_sub.bsale_variant_id = v.bsale_variant_id
            ORDER BY pld_sub.value_with_taxes DESC
            LIMIT 1
        ) pld ON TRUE
        WHERE {where_sql}
        ORDER BY "Departamento", "Categoría", "Subcategoría", "Producto"
        """

    result = await db.execute(text(sql), params)
    raw_columns = list(result.keys())
    raw_rows = result.fetchall()
    elapsed = (datetime.now() - t_start).total_seconds()

    titulo_reporte = f"Inventario - {target_office_name}" if target_office_name else "Inventario Consolidado"
    wb = build_inventario_workbook(
        cols=raw_columns,
        rows=raw_rows,
        titulo=titulo_reporte,
        brand_name=settings.BRAND_NAME,
        sucursal=target_office_name,
        elapsed_seconds=elapsed,
    )

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    fecha_str = datetime.now().strftime("%Y-%m-%d")
    tag_clean = target_office_name.replace(" ", "_") if target_office_name else "Consolidado"
    filename = f"Inventario_{tag_clean}_{fecha_str}.xlsx"

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Total-Rows": str(len(raw_rows)),
        },
    )
#     """Variantes con MAYOR stock total sumando todas las sucursales."""
#     res = await db.execute(text("""
#         SELECT v.bsale_variant_id, v.code, p.name AS producto,
#                SUM(sl.quantity_available) AS unidades
#         FROM stock_levels sl
#         JOIN variants v  ON v.bsale_variant_id = sl.bsale_variant_id
#         JOIN products p  ON p.bsale_product_id  = v.bsale_product_id
#         GROUP BY v.bsale_variant_id, v.code, p.name
#         HAVING SUM(sl.quantity_available) > 0
#         ORDER BY unidades DESC
#         LIMIT :limit
#     """), {"limit": limit})
#     return [dict(r) for r in res.mappings().all()]


# @router.get("/history")
# async def stock_history(
#     days: int = Query(30, ge=1, le=365),
#     variant_id: int | None = None,
#     db: AsyncSession = Depends(get_db)
#     ) -> list[dict]:
#     """Serie temporal del snapshot diario."""
#     since = date.today() - timedelta(days=days)
#     where = ["sh.snapshot_date >= :since"]
#     params = {"since": since}
#
#     if variant_id:
#         where.append("sh.bsale_variant_id = :variant_id")
#         params["variant_id"] = variant_id
#     where_sql = "WHERE " + " AND ".join(where)
#
#     res = await db.execute(text(f"""
#         SELECT sh.snapshot_date, o.name AS sucursal,
#                SUM(sh.quantity_available) AS unidades
#         FROM stock_history sh
#         JOIN offices o ON o.bsale_office_id = sh.bsale_office_id
#         {where_sql}
#         GROUP BY sh.snapshot_date, o.name
#         ORDER BY sh.snapshot_date, o.name
#     """), params)
#     return [dict(r) for r in res.mappings().all()]
