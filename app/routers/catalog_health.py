"""Vista 3 — Salud del catálogo (¿qué tengo que comprar / liquidar / reponer?).

Endpoint `GET /catalog-health` que responde decisiones de catálogo, no de
ventas del día. Pensado para revisar semanal o quincenal.

Secciones del payload:

  1. categorias              : top categorías por venta últ. 30 días, con YoY
                               y conteo de SKUs activos. Indica tendencia
                               (subiendo / estable / bajando) basada en YoY.
  2. huecos_yoy              : sub-categorías que cayeron > 50% vs hace
                               12 meses (replica el §3 del Formato Kawii Pluss
                               — el "Hueco de S/12,000/mes en zapatillas").
  3. capital_atrapado        : SKUs recibidos en últ. 90 días con sellthrough
                               < 20% — mercadería que llegó y no rota.
  4. candidatos_descuento    : SKUs con stock > 0 sin ventas últ. 60 días.
                               Pensado para alimentar la "promo 30%" del docx.
  5. quiebres_demanda        : resumen — el detalle vive en /diagnosis.
  6. composicion_catalogo    : qué % de la venta viene de SKUs nuevos vs
                               clásicos. Detecta si dependés demasiado del
                               catálogo viejo (vulnerable a que se enfríe).
  7. resumen                 : 3-6 bullets narrativos en español.

Nota sobre `category_targets`: cuando se cargue la tabla (Fase 2 del plan),
acá agregamos comparación de cada categoría contra su meta y su rol (motor #1,
fijo, complemento). Por ahora cada categoría se reporta sin objetivo asignado.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from analytics.core.config import OFFICE_IDS
from app.auth import CurrentCompany, get_current_company
from app.database import get_db
from app.routers.config_admin import get_company
from app.routers.diagnosis import (
    _excl_filter,
    _load_exclusions,
    _lost_sales_from_stockouts,
    _office_filters,
    _pct_delta,
    _period_kpis,
    _TZ_DATE,
)
from harvester.config import TIPOS_VENTA as DEFAULT_TIPOS_VENTA


router = APIRouter(
    prefix="/catalog-health",
    tags=["catalog-health"],
    dependencies=[Depends(get_current_company)],
)


# ════════════════════════════════════════════════════════════════════════════
# Categorías top — venta últ. 30d + YoY + SKUs activos
# ════════════════════════════════════════════════════════════════════════════

async def _categorias(
    db: AsyncSession, company_id: int, today: date, office_id: Optional[int], tipos_venta: list[int],
    top_n: int, excl: dict,
) -> dict:
    cur_to   = today
    cur_from = today - timedelta(days=30)
    yoy_to   = cur_to - timedelta(days=364)
    yoy_from = cur_from - timedelta(days=364)

    _, flt_doc, extra = _office_filters(office_id)
    cur_p: dict[str, Any] = {"dfrom": cur_from, "dto": cur_to, "tipos_venta": tipos_venta, "cid": company_id, **extra}
    yoy_p: dict[str, Any] = {"dfrom": yoy_from, "dto": yoy_to, "tipos_venta": tipos_venta, "cid": company_id, **extra}
    excl_cur = _excl_filter("vpf", excl, cur_p)
    excl_yoy = _excl_filter("vpf", excl, yoy_p)

    def _q(extra_where: str) -> str:
        return f"""
            SELECT vpf.department AS departamento,
                   vpf.category   AS categoria,
                   COALESCE(SUM(dd.total_amount), 0)              AS venta,
                   COUNT(DISTINCT v.bsale_variant_id)             AS skus_con_venta
            FROM document_details dd
            JOIN documents doc       ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
            JOIN variants v          ON v.bsale_variant_id    = dd.bsale_variant_id  AND v.company_id   = dd.company_id
            JOIN v_products_full vpf ON vpf.bsale_product_id  = v.bsale_product_id   AND vpf.company_id = v.company_id
            WHERE dd.company_id = :cid
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= :dfrom
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <  :dto
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND doc.bsale_document_type_id = ANY(:tipos_venta)
              AND {flt_doc}
              AND vpf.category IS NOT NULL
              {extra_where}
            GROUP BY vpf.department, vpf.category
        """
    cur_rows = (await db.execute(text(_q(excl_cur)), cur_p)).mappings().all()
    yoy_rows = (await db.execute(text(_q(excl_yoy)), yoy_p)).mappings().all()

    def _key(r): return (r["departamento"], r["categoria"])
    cur_map = {_key(r): {"venta": float(r["venta"]), "skus": int(r["skus_con_venta"])} for r in cur_rows}
    yoy_map = {_key(r): float(r["venta"]) for r in yoy_rows}

    rows = []
    for k, v in cur_map.items():
        venta_cur = v["venta"]
        venta_yoy = yoy_map.get(k, 0.0)
        delta_pct = _pct_delta(venta_cur, venta_yoy)
        if delta_pct is None:
            tendencia = "nuevo" if venta_yoy == 0 else "indefinido"
        elif delta_pct >= 15:
            tendencia = "subiendo"
        elif delta_pct >= -15:
            tendencia = "estable"
        elif delta_pct >= -40:
            tendencia = "bajando"
        else:
            tendencia = "hueco"
        rows.append({
            "departamento":   k[0],
            "categoria":      k[1],
            "ventas_30d":     round(venta_cur, 2),
            "ventas_30d_yoy": round(venta_yoy, 2),
            "delta_yoy_pct":  delta_pct,
            "skus_con_venta": v["skus"],
            "tendencia":      tendencia,
        })
    rows.sort(key=lambda r: r["ventas_30d"], reverse=True)

    total_cur = sum(r["ventas_30d"]     for r in rows)
    total_yoy = sum(r["ventas_30d_yoy"] for r in rows)
    return {
        "ventana":           "30d_vs_yoy",
        "from":              cur_from.isoformat(),
        "to":                (cur_to - timedelta(days=1)).isoformat(),
        "total_actual":      round(total_cur, 2),
        "total_yoy":         round(total_yoy, 2),
        "delta_yoy_pct":     _pct_delta(total_cur, total_yoy),
        "top_categorias":    rows[:top_n],
        "categorias_totales": len(rows),
    }


# ════════════════════════════════════════════════════════════════════════════
# Huecos YoY a nivel sub-categoría (criterio: cayó >50% y vendía >=500 hace 1 año)
# ════════════════════════════════════════════════════════════════════════════

async def _huecos_yoy_subcat(
    db: AsyncSession, company_id: int, today: date, office_id: Optional[int], tipos_venta: list[int],
    top_n: int, excl: dict,
) -> dict:
    cur_to   = today
    cur_from = today - timedelta(days=30)
    yoy_to   = cur_to - timedelta(days=364)
    yoy_from = cur_from - timedelta(days=364)

    _, flt_doc, extra = _office_filters(office_id)
    cur_p: dict[str, Any] = {"dfrom": cur_from, "dto": cur_to, "tipos_venta": tipos_venta, "cid": company_id, **extra}
    yoy_p: dict[str, Any] = {"dfrom": yoy_from, "dto": yoy_to, "tipos_venta": tipos_venta, "cid": company_id, **extra}
    excl_cur = _excl_filter("vpf", excl, cur_p)
    excl_yoy = _excl_filter("vpf", excl, yoy_p)

    def _q(extra_where: str) -> str:
        return f"""
            SELECT vpf.department  AS departamento,
                   vpf.category    AS categoria,
                   vpf.subcategory AS subcategoria,
                   COALESCE(SUM(dd.total_amount), 0)              AS venta,
                   COUNT(DISTINCT v.bsale_variant_id)             AS skus
            FROM document_details dd
            JOIN documents doc       ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
            JOIN variants v          ON v.bsale_variant_id    = dd.bsale_variant_id  AND v.company_id   = dd.company_id
            JOIN v_products_full vpf ON vpf.bsale_product_id  = v.bsale_product_id   AND vpf.company_id = v.company_id
            WHERE dd.company_id = :cid
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= :dfrom
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <  :dto
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND doc.bsale_document_type_id = ANY(:tipos_venta)
              AND {flt_doc}
              AND vpf.subcategory IS NOT NULL
              {extra_where}
            GROUP BY vpf.department, vpf.category, vpf.subcategory
        """
    cur_rows = (await db.execute(text(_q(excl_cur)), cur_p)).mappings().all()
    yoy_rows = (await db.execute(text(_q(excl_yoy)), yoy_p)).mappings().all()

    def _key(r): return (r["departamento"], r["categoria"], r["subcategoria"])
    cur_map = {_key(r): {"venta": float(r["venta"]), "skus": int(r["skus"])} for r in cur_rows}
    yoy_map = {_key(r): {"venta": float(r["venta"]), "skus": int(r["skus"])} for r in yoy_rows}

    huecos = []
    for k, prev in yoy_map.items():
        venta_prev = prev["venta"]
        if venta_prev < 500:           # ruido — la sub-cat no era material
            continue
        cur = cur_map.get(k, {"venta": 0.0, "skus": 0})
        delta_pct = (cur["venta"] - venta_prev) / venta_prev * 100
        if delta_pct > -50:            # solo huecos significativos (>50% caída)
            continue
        huecos.append({
            "departamento":      k[0],
            "categoria":         k[1],
            "subcategoria":      k[2],
            "venta_actual":      round(cur["venta"], 2),
            "venta_yoy":         round(venta_prev, 2),
            "hueco_pen":         round(venta_prev - cur["venta"], 2),
            "delta_pct":         round(delta_pct, 1),
            "skus_actual":       cur["skus"],
            "skus_yoy":          prev["skus"],
            "diagnostico":       "discontinuado_sin_reemplazo" if cur["venta"] == 0 else "cambio_de_demanda",
        })
    huecos.sort(key=lambda r: r["hueco_pen"], reverse=True)
    return {
        "ventana":              "30d_vs_yoy",
        "criterio":             "subcategoría que vendía ≥ S/500 hace 12 meses y cayó > 50%",
        "total_hueco_pen":      round(sum(h["hueco_pen"] for h in huecos), 2),
        "subcategorias_count":  len(huecos),
        "top_huecos":           huecos[:top_n],
    }


# ════════════════════════════════════════════════════════════════════════════
# Capital atrapado — SKUs recibidos en últ. 90d con bajo sellthrough
# ════════════════════════════════════════════════════════════════════════════

async def _capital_atrapado(
    db: AsyncSession, company_id: int, today: date, office_id: Optional[int], top_n: int
) -> dict:
    """Mercadería que llegó hace ≤90 días, vendió < 20% de lo recibido y aún
    tiene stock. Valor del capital atrapado = stock_actual × effective_cost.
    """
    flt_plain, _, extra = _office_filters(office_id)
    d90 = today - timedelta(days=90)

    q = f"""
        WITH recibidos_90d AS (
            SELECT rd.bsale_variant_id,
                   rec.bsale_office_id,
                   SUM(rd.quantity)        AS unds_recibidas,
                   MIN(rec.admission_date) AS primera_recepcion
            FROM reception_details rd
            JOIN receptions rec ON rec.bsale_reception_id = rd.bsale_reception_id AND rec.company_id = rd.company_id
            WHERE rd.company_id = :cid
              AND rec.admission_date >= :d90
              AND rec.{flt_plain}
            GROUP BY rd.bsale_variant_id, rec.bsale_office_id
        ),
        vendidos_post AS (
            SELECT dd.bsale_variant_id,
                   doc.bsale_office_id,
                   r90.primera_recepcion,
                   SUM(dd.quantity) FILTER (
                       WHERE doc.emission_date >= r90.primera_recepcion
                   ) AS unds_vendidas_post
            FROM recibidos_90d r90
            JOIN document_details dd
                 ON dd.bsale_variant_id = r90.bsale_variant_id AND dd.company_id = :cid
            JOIN documents doc
                 ON doc.bsale_document_id = dd.bsale_document_id
                AND doc.bsale_office_id   = r90.bsale_office_id
                AND doc.company_id        = dd.company_id
            WHERE doc.emission_date >= :d90
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
            GROUP BY dd.bsale_variant_id, doc.bsale_office_id, r90.primera_recepcion
        )
        SELECT v.display_code              AS sku,
               p.name                      AS producto,
               o.name                      AS sucursal,
               r90.bsale_office_id         AS office_id,
               r90.primera_recepcion::DATE AS fecha_recepcion,
               r90.unds_recibidas,
               COALESCE(vp.unds_vendidas_post, 0)   AS unds_vendidas,
               sl.quantity_available                 AS stock_actual,
               CASE WHEN r90.unds_recibidas > 0
                    THEN COALESCE(vp.unds_vendidas_post, 0) / r90.unds_recibidas * 100
                    ELSE NULL END                    AS sellthrough_pct,
               COALESCE(vc.effective_cost, 0)        AS costo_unit,
               (sl.quantity_available * COALESCE(vc.effective_cost, 0)) AS capital_atrapado_pen
        FROM recibidos_90d r90
        JOIN variants v  ON v.bsale_variant_id  = r90.bsale_variant_id AND v.company_id = :cid
        JOIN products p  ON p.bsale_product_id  = v.bsale_product_id   AND p.company_id = v.company_id
        JOIN offices  o  ON o.bsale_office_id   = r90.bsale_office_id  AND o.company_id = :cid
        LEFT JOIN vendidos_post vp
               ON vp.bsale_variant_id = r90.bsale_variant_id
              AND vp.bsale_office_id  = r90.bsale_office_id
        LEFT JOIN stock_levels sl
               ON sl.bsale_variant_id = r90.bsale_variant_id
              AND sl.bsale_office_id  = r90.bsale_office_id
              AND sl.company_id       = :cid
        LEFT JOIN variant_costs vc
               ON vc.bsale_variant_id = r90.bsale_variant_id
              AND vc.company_id       = :cid
        WHERE r90.unds_recibidas > 0
          AND COALESCE(sl.quantity_available, 0) > 0
          AND (COALESCE(vp.unds_vendidas_post, 0) / r90.unds_recibidas) < 0.20
        ORDER BY capital_atrapado_pen DESC NULLS LAST
        LIMIT :top_n
    """
    params = {"d90": d90, "top_n": top_n, "cid": company_id, **extra}
    rows = (await db.execute(text(q), params)).mappings().all()
    skus = [{
        "sku":                  r["sku"],
        "producto":             r["producto"],
        "sucursal":             r["sucursal"],
        "office_id":            int(r["office_id"]),
        "fecha_recepcion":      r["fecha_recepcion"].isoformat() if r["fecha_recepcion"] else None,
        "unds_recibidas":       float(r["unds_recibidas"]),
        "unds_vendidas":        float(r["unds_vendidas"]),
        "stock_actual":         float(r["stock_actual"] or 0),
        "sellthrough_pct":      round(float(r["sellthrough_pct"]), 1) if r["sellthrough_pct"] is not None else None,
        "costo_unit":           round(float(r["costo_unit"] or 0), 2),
        "capital_atrapado_pen": round(float(r["capital_atrapado_pen"] or 0), 2),
    } for r in rows]

    # Total agregado (sin LIMIT) — para mostrar el universo completo
    q_total = f"""
        WITH recibidos_90d AS (
            SELECT rd.bsale_variant_id,
                   rec.bsale_office_id,
                   SUM(rd.quantity) AS unds_recibidas,
                   MIN(rec.admission_date) AS primera_recepcion
            FROM reception_details rd
            JOIN receptions rec ON rec.bsale_reception_id = rd.bsale_reception_id AND rec.company_id = rd.company_id
            WHERE rd.company_id = :cid
              AND rec.admission_date >= :d90
              AND rec.{flt_plain}
            GROUP BY rd.bsale_variant_id, rec.bsale_office_id
        ),
        vendidos_post AS (
            SELECT dd.bsale_variant_id, doc.bsale_office_id,
                   SUM(dd.quantity) AS unds_vendidas
            FROM recibidos_90d r90
            JOIN document_details dd ON dd.bsale_variant_id = r90.bsale_variant_id AND dd.company_id = :cid
            JOIN documents doc       ON doc.bsale_document_id = dd.bsale_document_id
                                    AND doc.bsale_office_id   = r90.bsale_office_id
                                    AND doc.company_id        = dd.company_id
            WHERE doc.emission_date >= r90.primera_recepcion
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
            GROUP BY dd.bsale_variant_id, doc.bsale_office_id
        )
        SELECT COUNT(*) AS n,
               COALESCE(SUM(sl.quantity_available * COALESCE(vc.effective_cost, 0)), 0) AS total_pen
        FROM recibidos_90d r90
        LEFT JOIN vendidos_post vp
               ON vp.bsale_variant_id = r90.bsale_variant_id
              AND vp.bsale_office_id  = r90.bsale_office_id
        LEFT JOIN stock_levels sl
               ON sl.bsale_variant_id = r90.bsale_variant_id
              AND sl.bsale_office_id  = r90.bsale_office_id
              AND sl.company_id       = :cid
        LEFT JOIN variant_costs vc
               ON vc.bsale_variant_id = r90.bsale_variant_id
              AND vc.company_id       = :cid
        WHERE r90.unds_recibidas > 0
          AND COALESCE(sl.quantity_available, 0) > 0
          AND (COALESCE(vp.unds_vendidas, 0) / r90.unds_recibidas) < 0.20
    """
    tot = (await db.execute(text(q_total), {"d90": d90, "cid": company_id, **extra})).mappings().one()

    return {
        "criterio":            "SKU recibido en últ. 90 días con sellthrough < 20% y stock > 0",
        "ventana_recepcion":   "90d",
        "skus_count_total":    int(tot["n"]),
        "monto_total_pen":     round(float(tot["total_pen"]), 2),
        "top_skus":            skus,
    }


# ════════════════════════════════════════════════════════════════════════════
# Candidatos a descuento — stock > 0 sin ventas en últ. 60d
# ════════════════════════════════════════════════════════════════════════════

async def _candidatos_descuento(
    db: AsyncSession, company_id: int, today: date, office_id: Optional[int], top_n: int,
    excl: dict,
) -> dict:
    """SKUs con stock > 0 que no vendieron NADA en los últimos 60 días.
    Excluye SKUs de departamentos/categorías estacionales (igual no merecen
    descuento porque su demanda es cíclica).
    """
    flt_plain, flt_doc, extra = _office_filters(office_id)
    d60 = today - timedelta(days=60)
    params: dict[str, Any] = {"d60": d60, "top_n": top_n, "cid": company_id, **extra}
    excl_clause = _excl_filter("vpf", excl, params)

    q = f"""
        WITH ventas_60d AS (
            SELECT dd.bsale_variant_id, doc.bsale_office_id
            FROM document_details dd
            JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
            WHERE dd.company_id = :cid
              AND doc.emission_date >= :d60
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND {flt_doc}
            GROUP BY dd.bsale_variant_id, doc.bsale_office_id
        )
        SELECT v.display_code             AS sku,
               p.name                     AS producto,
               vpf.department,
               vpf.category,
               o.name                     AS sucursal,
               sl.bsale_office_id         AS office_id,
               sl.quantity_available      AS stock,
               COALESCE(vc.effective_cost, 0)                        AS costo_unit,
               (sl.quantity_available * COALESCE(vc.effective_cost, 0)) AS valor_inventario_pen
        FROM stock_levels sl
        JOIN variants v  ON v.bsale_variant_id = sl.bsale_variant_id AND v.company_id = sl.company_id AND v.is_active
        JOIN products p  ON p.bsale_product_id = v.bsale_product_id  AND p.company_id = v.company_id  AND p.is_active
        LEFT JOIN v_products_full vpf ON vpf.bsale_product_id = v.bsale_product_id AND vpf.company_id = v.company_id
        LEFT JOIN offices o          ON o.bsale_office_id  = sl.bsale_office_id  AND o.company_id  = sl.company_id
        LEFT JOIN variant_costs vc   ON vc.bsale_variant_id = sl.bsale_variant_id AND vc.company_id = sl.company_id
        LEFT JOIN ventas_60d vv
               ON vv.bsale_variant_id = sl.bsale_variant_id
              AND vv.bsale_office_id  = sl.bsale_office_id
        WHERE sl.company_id = :cid
          AND sl.{flt_plain}
          AND sl.quantity_available > 0
          AND vv.bsale_variant_id IS NULL
          {excl_clause}
        ORDER BY valor_inventario_pen DESC NULLS LAST
        LIMIT :top_n
    """
    rows = (await db.execute(text(q), params)).mappings().all()
    skus = [{
        "sku":                   r["sku"],
        "producto":              r["producto"],
        "departamento":          r["department"],
        "categoria":             r["category"],
        "sucursal":              r["sucursal"],
        "office_id":             int(r["office_id"]),
        "stock":                 float(r["stock"]),
        "costo_unit":            round(float(r["costo_unit"]), 2),
        "valor_inventario_pen":  round(float(r["valor_inventario_pen"] or 0), 2),
    } for r in rows]

    # Totales del universo (sin LIMIT)
    params_tot: dict[str, Any] = {"d60": d60, "cid": company_id, **extra}
    excl_tot = _excl_filter("vpf", excl, params_tot)
    q_total = f"""
        WITH ventas_60d AS (
            SELECT dd.bsale_variant_id, doc.bsale_office_id
            FROM document_details dd
            JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
            WHERE dd.company_id = :cid
              AND doc.emission_date >= :d60
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND {flt_doc}
            GROUP BY dd.bsale_variant_id, doc.bsale_office_id
        )
        SELECT COUNT(*) AS n,
               COALESCE(SUM(sl.quantity_available * COALESCE(vc.effective_cost, 0)), 0) AS total
        FROM stock_levels sl
        JOIN variants v ON v.bsale_variant_id = sl.bsale_variant_id AND v.company_id = sl.company_id AND v.is_active
        JOIN products p ON p.bsale_product_id = v.bsale_product_id  AND p.company_id = v.company_id  AND p.is_active
        LEFT JOIN v_products_full vpf ON vpf.bsale_product_id = v.bsale_product_id AND vpf.company_id = v.company_id
        LEFT JOIN variant_costs vc ON vc.bsale_variant_id = sl.bsale_variant_id AND vc.company_id = sl.company_id
        LEFT JOIN ventas_60d vv
               ON vv.bsale_variant_id = sl.bsale_variant_id
              AND vv.bsale_office_id  = sl.bsale_office_id
        WHERE sl.company_id = :cid
          AND sl.{flt_plain}
          AND sl.quantity_available > 0
          AND vv.bsale_variant_id IS NULL
          {excl_tot}
    """
    tot = (await db.execute(text(q_total), params_tot)).mappings().one()

    return {
        "criterio":            "SKU con stock > 0 sin ventas en últimos 60 días",
        "skus_count_total":    int(tot["n"]),
        "valor_inventario_pen": round(float(tot["total"]), 2),
        "top_skus":            skus,
    }


# ════════════════════════════════════════════════════════════════════════════
# Composición del catálogo — qué % de la venta viene de SKUs nuevos vs viejos
# ════════════════════════════════════════════════════════════════════════════

async def _composicion_catalogo(
    db: AsyncSession, company_id: int, today: date, office_id: Optional[int], tipos_venta: list[int],
    excl: dict,
) -> dict:
    """Para cada SKU vendido en últ. 30d, determinar 'edad' (días desde su
    primera venta lifetime) y agruparlos en buckets:
      - nuevo (≤90 días desde primera venta)
      - reciente (91-365 días)
      - clasico (>365 días)
    Reporta el % de venta y unds que aporta cada bucket.
    """
    cur_to   = today
    cur_from = today - timedelta(days=30)
    _, flt_doc, extra = _office_filters(office_id)
    params: dict[str, Any] = {"dfrom": cur_from, "dto": cur_to, "tipos_venta": tipos_venta, "cid": company_id, **extra}
    excl_clause = _excl_filter("vpf", excl, params)

    q = f"""
        WITH primera_venta AS (
            SELECT dd.bsale_variant_id,
                   MIN((doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE) AS primera
            FROM document_details dd
            JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
            WHERE dd.company_id = :cid
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND doc.bsale_document_type_id = ANY(:tipos_venta)
            GROUP BY dd.bsale_variant_id
        ),
        venta_ventana AS (
            SELECT dd.bsale_variant_id,
                   SUM(dd.total_amount) AS venta,
                   SUM(dd.quantity) FILTER (WHERE NOT dd.is_gratuity) AS unds
            FROM document_details dd
            JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
            JOIN variants v    ON v.bsale_variant_id    = dd.bsale_variant_id  AND v.company_id   = dd.company_id
            LEFT JOIN v_products_full vpf ON vpf.bsale_product_id = v.bsale_product_id AND vpf.company_id = v.company_id
            WHERE dd.company_id = :cid
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= :dfrom
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <  :dto
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND doc.bsale_document_type_id = ANY(:tipos_venta)
              AND {flt_doc}
              {excl_clause}
            GROUP BY dd.bsale_variant_id
        )
        SELECT
            CASE
                WHEN (:dto - pv.primera) <=  90 THEN 'nuevo'
                WHEN (:dto - pv.primera) <= 365 THEN 'reciente'
                ELSE 'clasico'
            END                                          AS bucket,
            COUNT(DISTINCT vv.bsale_variant_id)          AS skus,
            COALESCE(SUM(vv.venta), 0)                   AS venta,
            COALESCE(SUM(vv.unds), 0)                    AS unds
        FROM venta_ventana vv
        JOIN primera_venta pv ON pv.bsale_variant_id = vv.bsale_variant_id
        GROUP BY 1
    """
    rows = (await db.execute(text(q), params)).mappings().all()
    by_bucket = {r["bucket"]: r for r in rows}
    total_venta = sum(float(r["venta"]) for r in rows) or 1.0
    total_unds  = sum(float(r["unds"]) for r in rows) or 1.0
    total_skus  = sum(int(r["skus"]) for r in rows) or 1

    def _b(name: str, etiqueta: str) -> dict:
        r = by_bucket.get(name, {"skus": 0, "venta": 0.0, "unds": 0.0})
        venta = float(r["venta"])
        unds  = float(r["unds"])
        return {
            "bucket":          name,
            "etiqueta":        etiqueta,
            "skus":            int(r["skus"]),
            "venta_pen":       round(venta, 2),
            "unds":            round(unds, 2),
            "pct_venta":       round(venta / total_venta * 100, 1),
            "pct_unds":        round(unds  / total_unds  * 100, 1),
        }

    nuevo    = _b("nuevo",    "Primera venta ≤ 90 días")
    reciente = _b("reciente", "Primera venta 91-365 días")
    clasico  = _b("clasico",  "Primera venta > 365 días")

    # Lectura operativa
    if clasico["pct_venta"] >= 70:
        lectura = (
            f"El {clasico['pct_venta']}% de tu venta viene de catálogo viejo (>1 año). "
            "Dependés mucho de clásicos — vulnerable a que se enfríen."
        )
    elif nuevo["pct_venta"] >= 25:
        lectura = (
            f"El {nuevo['pct_venta']}% de tu venta viene de SKUs nuevos (≤90 días). "
            "Buena renovación de catálogo."
        )
    else:
        lectura = (
            f"Composición balanceada: {nuevo['pct_venta']}% nuevos, "
            f"{reciente['pct_venta']}% recientes, {clasico['pct_venta']}% clásicos."
        )

    return {
        "ventana_venta":    "30d",
        "from":             cur_from.isoformat(),
        "to":               (cur_to - timedelta(days=1)).isoformat(),
        "total_venta_pen":  round(total_venta, 2),
        "total_skus":       total_skus,
        "por_edad":         [nuevo, reciente, clasico],
        "lectura":          lectura,
    }


# ════════════════════════════════════════════════════════════════════════════
# Narrativa
# ════════════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════════════
# Bloque estable 80/20 — cruza category_targets con venta del mes en curso
# ════════════════════════════════════════════════════════════════════════════

def _estado_categoria(meta: Optional[float], ritmo: Optional[float]) -> str:
    """Mismo escalón que el de pulse._estado_meta, simplificado para la vista."""
    if meta is None:
        return "SIN_META"
    if ritmo is None:
        return "INDETERMINADO"
    if ritmo >= 100:
        return "META_CUMPLIDA"
    if ritmo >= 95:
        return "EN_RITMO"
    if ritmo >= 80:
        return "ATRASADO_LEVE"
    return "RIESGO_NO_LLEGAR"


async def _bloque_estable_80_20(
    db: AsyncSession, company_id: int, today: date, office_id: Optional[int], tipos_venta: list[int],
) -> Optional[dict]:
    """Cruza category_targets con la venta acumulada del mes en curso por
    categoría motor. Si la tabla está vacía, devuelve None (la sección no se
    incluye y la UI muestra el hint para correr /config/category-targets/bootstrap).
    """
    n_targets = await db.scalar(
        text("SELECT COUNT(*) FROM category_targets WHERE company_id = :cid"),
        {"cid": company_id},
    ) or 0
    if n_targets == 0:
        return None

    year, mon = today.year, today.month
    dias_del_mes = monthrange(year, mon)[1]
    first = date(year, mon, 1)
    if today.day == 1:
        # Mes recién empezado — sin datos cerrados
        corte = first
        dias_trans = 0
    else:
        corte = today - timedelta(days=1)
        dias_trans = corte.day
    dto = corte + timedelta(days=1) if dias_trans > 0 else first

    where_office = ""
    params: dict[str, Any] = {"dfrom": first, "dto": dto, "tipos_venta": tipos_venta, "cid": company_id}
    if office_id is not None:
        where_office = "AND ct.bsale_office_id = :office_id AND doc.bsale_office_id = :office_id"
        params["office_id"] = office_id

    # Venta acumulada del mes por (categoría × sucursal), cruzada con targets.
    # Usa LEFT JOIN para que aparezcan también categorías con target pero sin venta este mes.
    q = f"""
        WITH ventas_mes AS (
            SELECT c.id AS category_id,
                   doc.bsale_office_id,
                   COALESCE(SUM(dd.total_amount), 0) AS venta_mes
            FROM document_details dd
            JOIN documents doc       ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
            JOIN variants v          ON v.bsale_variant_id    = dd.bsale_variant_id  AND v.company_id   = dd.company_id
            JOIN v_products_full vpf ON vpf.bsale_product_id  = v.bsale_product_id   AND vpf.company_id = v.company_id
            JOIN categories c        ON c.name = vpf.category AND c.company_id = vpf.company_id
            WHERE dd.company_id = :cid
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= :dfrom
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <  :dto
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND doc.bsale_document_type_id = ANY(:tipos_venta)
              AND NOT dd.is_gratuity
              AND vpf.category IS NOT NULL
            GROUP BY c.id, doc.bsale_office_id
        ),
        skus_activos AS (
            SELECT vpf.bsale_product_id, c.id AS category_id, sl.bsale_office_id,
                   COUNT(DISTINCT v.bsale_variant_id) AS skus
            FROM stock_levels sl
            JOIN variants v          ON v.bsale_variant_id = sl.bsale_variant_id AND v.company_id = sl.company_id AND v.is_active
            JOIN v_products_full vpf ON vpf.bsale_product_id = v.bsale_product_id AND vpf.company_id = v.company_id
            JOIN categories c        ON c.name = vpf.category AND c.company_id = vpf.company_id
            WHERE sl.company_id = :cid
              AND sl.quantity_available > 0
            GROUP BY vpf.bsale_product_id, c.id, sl.bsale_office_id
        )
        SELECT ct.category_id,
               c.name      AS categoria,
               d.name      AS departamento,
               ct.bsale_office_id,
               o.name      AS sucursal,
               ct.rol,
               ct.meta_mensual_pen,
               ct.pvp_min, ct.pvp_max,
               ct.margen_objetivo_pct,
               ct.skus_min, ct.skus_max,
               COALESCE(vm.venta_mes, 0)        AS venta_mes,
               COALESCE(SUM(sa.skus), 0)        AS skus_con_stock
        FROM category_targets ct
        JOIN categories  c   ON c.id = ct.category_id AND c.company_id = ct.company_id
        LEFT JOIN departments d ON d.id = c.department_id AND d.company_id = c.company_id
        LEFT JOIN offices o   ON o.bsale_office_id = ct.bsale_office_id AND o.company_id = ct.company_id
        LEFT JOIN ventas_mes vm
               ON vm.category_id     = ct.category_id
              AND vm.bsale_office_id = ct.bsale_office_id
        LEFT JOIN skus_activos sa
               ON sa.category_id     = ct.category_id
              AND sa.bsale_office_id = ct.bsale_office_id
        WHERE ct.company_id = :cid
          {where_office}
        GROUP BY ct.category_id, c.name, d.name, ct.bsale_office_id, o.name,
                 ct.rol, ct.meta_mensual_pen, ct.pvp_min, ct.pvp_max,
                 ct.margen_objetivo_pct, ct.skus_min, ct.skus_max, vm.venta_mes
        ORDER BY ct.bsale_office_id, ct.meta_mensual_pen DESC NULLS LAST
    """
    rows = (await db.execute(text(q), params)).mappings().all()

    frac = (dias_trans / dias_del_mes) if dias_del_mes else 0.0
    items: list[dict] = []
    for r in rows:
        meta        = float(r["meta_mensual_pen"]) if r["meta_mensual_pen"] is not None else None
        venta_mes   = float(r["venta_mes"])
        skus_actual = int(r["skus_con_stock"])
        skus_min    = r["skus_min"]
        skus_max    = r["skus_max"]
        meta_prorr  = (meta * frac) if meta is not None else None
        avance      = (venta_mes / meta * 100) if (meta and meta > 0) else None
        ritmo       = (venta_mes / meta_prorr * 100) if meta_prorr else None
        proy        = (venta_mes / dias_trans * dias_del_mes) if dias_trans else None
        estado      = _estado_categoria(meta, ritmo)
        skus_estado = (
            "FALTAN_SKUS" if (skus_min is not None and skus_actual < skus_min) else
            "EXCESO_SKUS" if (skus_max is not None and skus_actual > skus_max) else
            "OK"
        )
        items.append({
            "category_id":            int(r["category_id"]),
            "categoria":              r["categoria"],
            "departamento":           r["departamento"],
            "office_id":              int(r["bsale_office_id"]),
            "sucursal":               r["sucursal"],
            "rol":                    r["rol"],
            "meta_mensual_pen":       meta,
            "meta_prorrateada":       round(meta_prorr, 2) if meta_prorr is not None else None,
            "venta_acumulada_mes":    round(venta_mes, 2),
            "gap_a_meta":             round(venta_mes - meta, 2) if meta is not None else None,
            "avance_pct":             round(avance, 1) if avance is not None else None,
            "ritmo_vs_meta_pct":      round(ritmo, 1)  if ritmo  is not None else None,
            "proyeccion_cierre":      round(proy, 2)   if proy   is not None else None,
            "estado":                 estado,
            "skus_con_stock":         skus_actual,
            "skus_min":               skus_min,
            "skus_max":               skus_max,
            "skus_estado":            skus_estado,
            "pvp_min":                float(r["pvp_min"]) if r["pvp_min"] is not None else None,
            "pvp_max":                float(r["pvp_max"]) if r["pvp_max"] is not None else None,
            "margen_objetivo_pct":    float(r["margen_objetivo_pct"]) if r["margen_objetivo_pct"] is not None else None,
        })

    # Totales por sucursal (meta vs avance)
    por_sucursal: dict[int, dict] = {}
    for it in items:
        oid = it["office_id"]
        d = por_sucursal.setdefault(oid, {
            "office_id": oid, "sucursal": it["sucursal"],
            "meta_total": 0.0, "venta_acumulada_total": 0.0, "categorias": 0,
            "cumplen": 0, "en_ritmo": 0, "atrasado_leve": 0, "riesgo": 0,
        })
        d["meta_total"]            += it["meta_mensual_pen"] or 0
        d["venta_acumulada_total"] += it["venta_acumulada_mes"]
        d["categorias"]            += 1
        if it["estado"] == "META_CUMPLIDA": d["cumplen"]       += 1
        elif it["estado"] == "EN_RITMO":     d["en_ritmo"]      += 1
        elif it["estado"] == "ATRASADO_LEVE":d["atrasado_leve"] += 1
        elif it["estado"] == "RIESGO_NO_LLEGAR": d["riesgo"]   += 1

    for d in por_sucursal.values():
        meta_total = d["meta_total"]
        venta_total = d["venta_acumulada_total"]
        meta_total_prorr = meta_total * frac
        d["meta_total"]               = round(meta_total, 2)
        d["venta_acumulada_total"]    = round(venta_total, 2)
        d["meta_prorrateada_total"]   = round(meta_total_prorr, 2)
        d["avance_pct"]               = round(venta_total / meta_total * 100, 1) if meta_total else None
        d["ritmo_vs_meta_pct"]        = round(venta_total / meta_total_prorr * 100, 1) if meta_total_prorr else None

    return {
        "mes":                  f"{year:04d}-{mon:02d}",
        "dias_transcurridos":   dias_trans,
        "dias_del_mes":         dias_del_mes,
        "ultimo_dia_cerrado":   corte.isoformat() if dias_trans > 0 else None,
        "total_categorias":     len(items),
        "por_sucursal":         list(por_sucursal.values()),
        "categorias":           items,
    }


def _build_narrative(payload: dict) -> list[str]:
    out: list[str] = []

    # Bloque 80/20 si está activo
    b = payload.get("bloque_estable_80_20")
    if b:
        for s in b["por_sucursal"]:
            if s.get("avance_pct") is not None:
                out.append(
                    f"{s['sucursal']} — bloque 80/20: avance {s['avance_pct']}% "
                    f"(S/ {s['venta_acumulada_total']:,.0f} / S/ {s['meta_total']:,.0f}). "
                    f"{s['cumplen']} categorías cumplieron, {s['riesgo']} en riesgo."
                )
        # Categoría motor más rezagada
        en_riesgo = sorted(
            [c for c in b["categorias"] if c["estado"] == "RIESGO_NO_LLEGAR"],
            key=lambda c: c["gap_a_meta"] or 0,
        )
        if en_riesgo:
            c = en_riesgo[0]
            out.append(
                f"Motor más rezagado: {c['categoria']} ({c['rol']}, {c['sucursal']}) — "
                f"S/ {c['venta_acumulada_mes']:,.0f} de meta S/ {c['meta_mensual_pen']:,.0f} ({c['avance_pct']}%)."
            )

    cats = payload["categorias"]
    if cats["delta_yoy_pct"] is not None:
        signo = "arriba" if cats["delta_yoy_pct"] > 0 else "abajo"
        out.append(
            f"El catálogo facturó S/ {cats['total_actual']:,.0f} en los últimos 30 días — "
            f"{abs(cats['delta_yoy_pct'])}% {signo} vs mismo período año anterior."
        )

    if cats["top_categorias"]:
        top1 = cats["top_categorias"][0]
        out.append(
            f"Categoría #1: {top1['categoria']} con S/ {top1['ventas_30d']:,.0f} "
            f"({top1['skus_con_venta']} SKUs activos, tendencia: {top1['tendencia']})."
        )

    huecos = payload["huecos_yoy"]
    if huecos["subcategorias_count"] > 0:
        top = huecos["top_huecos"][0]
        out.append(
            f"{huecos['subcategorias_count']} sub-categorías se enfriaron vs hace 12 meses "
            f"(hueco total ~S/ {huecos['total_hueco_pen']:,.0f}). El mayor: "
            f"{top['subcategoria']} (S/ {top['hueco_pen']:,.0f}, {top['delta_pct']}%)."
        )

    cap = payload["capital_atrapado"]
    if cap["monto_total_pen"] >= 100:
        out.append(
            f"S/ {cap['monto_total_pen']:,.0f} de capital atrapado en {cap['skus_count_total']} "
            f"SKUs recibidos hace ≤90 días con sellthrough < 20%."
        )

    desc = payload["candidatos_descuento"]
    if desc["skus_count_total"] > 0:
        out.append(
            f"{desc['skus_count_total']} SKUs candidatos a descuento "
            f"(S/ {desc['valor_inventario_pen']:,.0f} de inventario sin movimiento en 60 días)."
        )

    qd = payload["quiebres_demanda"]
    if qd["monto_estimado_pen"] >= 100:
        out.append(
            f"{qd['skus_count']} SKUs en quiebre con demanda — "
            f"~S/ {qd['monto_estimado_pen']:,.0f} de venta perdida estimada últimos 7 días."
        )

    comp = payload["composicion_catalogo"]
    out.append(comp["lectura"])

    return out


# ════════════════════════════════════════════════════════════════════════════
# Helper: bloque de cobertura de costos para el meta
# ════════════════════════════════════════════════════════════════════════════

async def _meta_cobertura(
    db: AsyncSession, company_id: int, today: date, office_id: Optional[int],
    tipos_venta: list[int], excl: dict,
) -> dict:
    """Devuelve {pct_actual, estado, warning} sobre los últimos 30 días.
    Reusa _period_kpis (que ya calcula cobertura_costos_pct)."""
    k = await _period_kpis(db, company_id, today - timedelta(days=30), today, office_id, tipos_venta, excl)
    pct = k["_total"]["cobertura_costos_pct"]
    estado = "OK" if pct >= 90 else ("ADVERTENCIA" if pct >= 70 else "CRITICA")
    warning = None
    if pct < 90:
        warning = (
            f"⚠️ Solo {pct}% de la venta tiene costo cargado — correr "
            f"POST /config/variant-costs/backfill-from-receptions para recuperar."
        )
    return {"pct_actual": pct, "estado": estado, "warning": warning}


# ════════════════════════════════════════════════════════════════════════════
# Endpoint principal
# ════════════════════════════════════════════════════════════════════════════

@router.get("")
async def catalog_health(
    office_id: Optional[int] = Query(None, description="ID de sucursal (vacío = todas)."),
    top_n: int = Query(15, ge=1, le=100, description="Cuántos ítems por lista."),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Salud del catálogo — qué comprar, qué liquidar, qué reponer.

    Pensado para revisión semanal: estado de categorías, huecos vs YoY,
    capital atrapado, candidatos a descuento, quiebres y composición por edad.
    """
    today = date.today()
    cid = company.company_id
    company_cfg = await get_company(db, cid)
    tipos_venta = company_cfg.get("tipos_venta") or DEFAULT_TIPOS_VENTA
    excl = await _load_exclusions(db, cid)

    categorias = await _categorias        (db, cid, today, office_id, tipos_venta, top_n, excl)
    huecos     = await _huecos_yoy_subcat (db, cid, today, office_id, tipos_venta, top_n, excl)
    capital    = await _capital_atrapado  (db, cid, today, office_id, top_n)
    descuentos = await _candidatos_descuento(db, cid, today, office_id, top_n, excl)
    comp       = await _composicion_catalogo(db, cid, today, office_id, tipos_venta, excl)
    bloque_80_20 = await _bloque_estable_80_20(db, cid, today, office_id, tipos_venta)

    # Quiebres con demanda: reusa la lógica de diagnosis (ventana 7d)
    q_window = {"from": today - timedelta(days=7), "to": today}
    quiebres = await _lost_sales_from_stockouts(db, cid, q_window, office_id, tipos_venta, excl)
    quiebres_resumen = {
        "skus_count":              quiebres["skus_con_perdida"],
        "monto_estimado_pen":      quiebres["monto_estimado_pen"],
        "ventana_dias":            quiebres["ventana_dias"],
        "top_skus":                quiebres["top_skus"][:5],
        "ver_detalle_en":          "/diagnosis (factores_adicionales.venta_perdida_por_quiebre)",
    }

    last_sync = await db.scalar(
        text(
            "SELECT MAX(finished_at) FROM sync_log "
            "WHERE company_id = :cid AND entity IN ('documents','receptions') AND status='OK'"
        ),
        {"cid": cid},
    )

    payload: dict[str, Any] = {
        "meta": {
            "fecha":                today.isoformat(),
            "office_id":            office_id,
            "office_scope":         [office_id] if office_id is not None else list(OFFICE_IDS),
            "datos_sync_hasta":     last_sync.isoformat() if last_sync else None,
            "generado_at":          datetime.utcnow().isoformat() + "Z",
            "exclusiones": {
                "departamentos": excl.get("depts", []),
                "categorias":    excl.get("cats", []),
                "nota": "Categorías/huecos/descuentos excluyen estacionales. Capital atrapado y quiebres se reportan completos (a nivel SKU vale considerar todos).",
            },
            "nota": (
                "Bloque estable 80/20 activo — metas/roles cargados en category_targets."
                if bloque_80_20 is not None
                else "Tabla category_targets vacía — correr POST /config/category-targets/bootstrap para activar la sección 80/20."
            ),
            "cobertura_costos": await _meta_cobertura(db, cid, today, office_id, tipos_venta, excl),
        },
        "bloque_estable_80_20":   bloque_80_20,
        "categorias":             categorias,
        "huecos_yoy":             huecos,
        "capital_atrapado":       capital,
        "candidatos_descuento":   descuentos,
        "quiebres_demanda":       quiebres_resumen,
        "composicion_catalogo":   comp,
    }
    payload["resumen"] = _build_narrative(payload)
    return payload
