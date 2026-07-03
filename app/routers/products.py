"""Endpoints de productos y variantes.

Usa la vista `v_products_full` que aplica override por producto:
- Si products.subcategory_id está seteado, usa ese
- Si no, hereda del product_type (BSale)
"""

from fastapi import Depends, APIRouter, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.auth import CurrentCompany, get_current_company, require_operador_or_admin
from app.database import get_db

# Productos / variantes. Lectura para cualquier logueado (con empresa activa);
# el override de subcategoría (PATCH /{id}/subcategory) cambia clasificación
# de un SKU y se restringe a operador/admin en la decoración del endpoint.
router = APIRouter(
    prefix="/products",
    tags=["products"],
    dependencies=[Depends(get_current_company)],
)


@router.get("")
async def list_products(
    q: str | None = Query(
        None,
        description="Busqueda por nombre, bsale_product_id (numerico exacto) "
                    "o SKU/code de cualquier variante (ILIKE).",
    ),
    department: str | None = None,
    category: str | None = None,
    subcategory: str | None = None,
    mapped_only: bool = False,
    override_only: bool = Query(
        False, description="Solo productos con override individual de subcategoría."
    ),
    unmapped_only: bool = Query(
        False, description="Solo productos cuya clasificación final está sin asignar."
    ),
    product_type_id: int | None = Query(
        None, description="Filtra por bsale_product_type_id (útil para drill-down desde huérfanos)."
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    cid = company.company_id
    where = ["v.company_id = :cid"]
    params: dict = {"cid": cid}

    if q:
        # Busqueda inteligente: nombre, ID exacto si es numerico, SKU de variante.
        clauses = ["v.product_name ILIKE :q"]
        params["q"] = f"%{q}%"
        if q.strip().isdigit():
            clauses.append("v.bsale_product_id = :qid")
            params["qid"] = int(q.strip())
        clauses.append("""EXISTS (
            SELECT 1 FROM variants va
             WHERE va.bsale_product_id = v.bsale_product_id
               AND va.company_id = v.company_id
               AND va.code ILIKE :q
        )""")
        where.append("(" + " OR ".join(clauses) + ")")

    if department:
        where.append("v.department = :department")
        params["department"] = department
    if category:
        where.append("v.category = :category")
        params["category"] = category
    if subcategory:
        where.append("v.subcategory = :subcategory")
        params["subcategory"] = subcategory
    if mapped_only:
        where.append("v.is_mapped = TRUE")
    if override_only:
        where.append("v.has_override = TRUE")
    if unmapped_only:
        where.append("v.subcategory IS NULL")
    if product_type_id is not None:
        where.append("v.bsale_product_type_id = :product_type_id")
        params["product_type_id"] = product_type_id

    where_sql = "WHERE " + " AND ".join(where)

    sql_count = f"""
        SELECT COUNT(*)
        FROM v_products_full v
        {where_sql}
    """
    res_total = await db.execute(text(sql_count), params)
    total = res_total.scalar() or 0

    params["limit"] = limit
    params["offset"] = offset
    sql = f"""
        SELECT v.bsale_product_id, v.product_name AS name, v.is_active,
               v.bsale_product_type_id, v.product_type_name,
               v.subcategory, v.category, v.department,
               v.has_override,
               (SELECT STRING_AGG(va.code, ', ' ORDER BY va.code)
                  FROM (
                       SELECT code FROM variants
                        WHERE bsale_product_id = v.bsale_product_id
                          AND company_id = v.company_id
                          AND code IS NOT NULL AND code <> ''
                        ORDER BY code
                        LIMIT 3
    ) va
               ) AS skus,
               (SELECT COUNT(*) FROM variants
                 WHERE bsale_product_id = v.bsale_product_id
                   AND company_id = v.company_id) AS variantes_count
        FROM v_products_full v
        {where_sql}
        ORDER BY v.product_name
        LIMIT :limit OFFSET :offset
    """
    res = await db.execute(text(sql), params)
    rows = [dict(r) for r in res.mappings().all()]

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": rows,
    }


@router.get("/{product_id}")
async def get_product(
    product_id: int,
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    cid = company.company_id
    res_prod = await db.execute(text("""
        SELECT v.bsale_product_id, v.product_name AS name, v.is_active,
               v.bsale_product_type_id, v.product_type_name,
               v.subcategory, v.category, v.department, v.has_override
        FROM v_products_full v
        WHERE v.company_id = :cid AND v.bsale_product_id = :product_id
    """), {"product_id": product_id, "cid": cid})
    prod_row = res_prod.mappings().first()

    if not prod_row:
        raise HTTPException(404, "Producto no encontrado")

    prod = dict(prod_row)

    res_var = await db.execute(text("""
        SELECT va.bsale_variant_id, va.code, va.description,
               vc.effective_cost, vc.average_cost, vc.latest_cost, vc.cost_source
        FROM variants va
        LEFT JOIN variant_costs vc
               ON vc.bsale_variant_id = va.bsale_variant_id AND vc.company_id = va.company_id
        WHERE va.company_id = :cid AND va.bsale_product_id = :product_id
        ORDER BY va.bsale_variant_id
    """), {"product_id": product_id, "cid": cid})
    prod["variantes"] = [dict(r) for r in res_var.mappings().all()]

    res_stock = await db.execute(text("""
        SELECT o.name AS sucursal, SUM(sl.quantity_available) AS unidades
        FROM stock_levels sl
        JOIN offices o  ON o.bsale_office_id = sl.bsale_office_id AND o.company_id = sl.company_id
        JOIN variants v ON v.bsale_variant_id = sl.bsale_variant_id AND v.company_id = sl.company_id
        WHERE sl.company_id = :cid AND v.bsale_product_id = :product_id
        GROUP BY o.name
        ORDER BY o.name
    """), {"product_id": product_id, "cid": cid})
    prod["stock_por_sucursal"] = [dict(r) for r in res_stock.mappings().all()]

    return prod


@router.patch("/{product_id}/subcategory", dependencies=[Depends(require_operador_or_admin)])
async def set_product_subcategory(
    product_id: int,
    subcategory_id: int | None = None,
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Override individual: asigna una subcategoría específica a UN producto.
    Si subcategory_id es null, se elimina el override y vuelve a heredar del product_type.
    """
    cid = company.company_id
    # Validar que el producto exista EN ESTA EMPRESA
    res_exists = await db.execute(text(
        "SELECT 1 FROM products WHERE company_id = :cid AND bsale_product_id = :product_id"
    ), {"product_id": product_id, "cid": cid})
    if not res_exists.scalar():
        raise HTTPException(404, f"Producto {product_id} no existe en esta empresa")

    # Validar subcategoría si se provee (debe pertenecer a la misma empresa)
    if subcategory_id is not None:
        res_subcat = await db.execute(text(
            "SELECT id, name FROM subcategories WHERE company_id = :cid AND id = :subcategory_id"
        ), {"subcategory_id": subcategory_id, "cid": cid})
        subcat = res_subcat.mappings().first()
        if not subcat:
            raise HTTPException(404, f"Subcategoria {subcategory_id} no existe en esta empresa")

    await db.execute(text(
        "UPDATE products SET subcategory_id = :subcategory_id "
        "WHERE company_id = :cid AND bsale_product_id = :product_id"
    ), {"subcategory_id": subcategory_id, "product_id": product_id, "cid": cid})
    await db.commit()

    res_entity = await db.execute(text("""
        SELECT bsale_product_id, product_name AS name,
               department, category, subcategory, has_override
        FROM v_products_full
        WHERE company_id = :cid AND bsale_product_id = :product_id
    """), {"product_id": product_id, "cid": cid})
    return dict(res_entity.mappings().first())


@router.get("/stats/summary")
async def products_summary(
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Resumen global de la empresa activa."""
    cid = company.company_id
    p = {"cid": cid}
    res_tot_prod = await db.execute(text("SELECT COUNT(*) FROM products WHERE company_id = :cid"), p)
    res_tot_var = await db.execute(text("SELECT COUNT(*) FROM variants WHERE company_id = :cid"), p)
    res_pt_tot = await db.execute(text("SELECT COUNT(*) FROM product_types WHERE company_id = :cid"), p)
    res_pt_map = await db.execute(text("SELECT COUNT(*) FROM product_types WHERE company_id = :cid AND is_mapped = TRUE"), p)
    res_pt_unmap = await db.execute(text("SELECT COUNT(*) FROM product_types WHERE company_id = :cid AND is_mapped = FALSE"), p)
    res_prod_over = await db.execute(text("SELECT COUNT(*) FROM products WHERE company_id = :cid AND subcategory_id IS NOT NULL"), p)

    res_huerfanos = await db.execute(text("""
            SELECT pt.bsale_product_type_id AS id, pt.name,
                   COUNT(p.bsale_product_id) AS productos
            FROM product_types pt
            LEFT JOIN products p ON p.bsale_product_type_id = pt.bsale_product_type_id
                                AND p.company_id = pt.company_id
            WHERE pt.company_id = :cid
              AND NOT pt.is_mapped
              AND p.subcategory_id IS NULL
            GROUP BY 1, 2
            HAVING COUNT(p.bsale_product_id) > 0
            ORDER BY 3 DESC
        """), p)

    return {
        "total_productos":             res_tot_prod.scalar() or 0,
        "total_variantes":             res_tot_var.scalar() or 0,
        "product_types_total":         res_pt_tot.scalar() or 0,
        "product_types_mapeados":      res_pt_map.scalar() or 0,
        "product_types_sin_mapear":    res_pt_unmap.scalar() or 0,
        "productos_con_override":      res_prod_over.scalar() or 0,
        "productos_huerfanos":         [dict(r) for r in res_huerfanos.mappings().all()]
    }
