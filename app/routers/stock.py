"""Endpoints de stock (niveles, valorizacion, historico)."""

from datetime import date, datetime, timedelta

from fastapi import Depends, APIRouter, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text

from app.auth import CurrentCompany, get_current_company
from app.database import get_db

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
               ROUND(SUM(sl.quantity_available * COALESCE(vc.effective_cost, 0))::numeric, 2) AS valor_soles,
               SUM(sl.quantity_available) AS unidades
        FROM stock_levels sl
        JOIN offices o              ON o.bsale_office_id  = sl.bsale_office_id  AND o.company_id  = sl.company_id
        LEFT JOIN variant_costs vc  ON vc.bsale_variant_id = sl.bsale_variant_id AND vc.company_id = sl.company_id
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


# @router.get("/top")
# async def stock_top(
#     limit: int = Query(20, ge=1, le=200),
#     db: AsyncSession = Depends(get_db)
#     ) -> list[dict]:
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
