"""Endpoints analíticos: KPIs, ventas por dimensión, rankings.

Solo considera las sucursales activas.

FECHAS / ZONA HORARIA (corregido 2026-06-01):
  BSale codifica emissionDate como la MEDIANOCHE EN UTC del día calendario
  (todos los documentos quedan a las 00:00:00 UTC). Verificado contra el texto
  crudo de BSale (rawAdmissionDate): la fecha real coincide 100% al extraerla en
  UTC y 0% al extraerla en America/Lima.

  Por eso la extracción de fecha usa AT TIME ZONE 'UTC'. El intento previo
  (2026-05-06) de usar 'America/Lima' corría TODAS las fechas un día hacia atrás
  (una venta del 1-jun aparecía como 31-may), porque no hay hora real que
  preservar — el dato ya viene a medianoche UTC.
"""

import io
import math
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import Depends, APIRouter, HTTPException, Path, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from analytics.core.config import OFFICE_IDS
from app.auth import CurrentCompany, get_current_company, get_current_user, require_operador_or_admin
from app.config import get_settings
from app.database import get_db
from app.routers.config_admin import get_goals, goal_for_month, set_goals_month, get_company
from harvester.config import TIPOS_VENTA as DEFAULT_TIPOS_VENTA

# Multi-tenant: TODOS los endpoints requieren X-Company-Id (via get_current_company).
# Las metas (PUT /goals) se protegen extra en la decoración del endpoint.
router = APIRouter(
    prefix="/analytics",
    tags=["analytics"],
    dependencies=[Depends(get_current_company)],
)

_OFFICE_IDS_SQL  = ", ".join(str(i) for i in OFFICE_IDS)
_OFFICE_FILTER_DOC   = f"doc.bsale_office_id IN ({_OFFICE_IDS_SQL})"
_OFFICE_FILTER_PLAIN = f"bsale_office_id IN ({_OFFICE_IDS_SQL})"

# TZ para extraer la fecha del timestamp. BSale guarda emissionDate a medianoche
# UTC, así que la fecha real del documento se obtiene en UTC (NO en Lima, que
# correría todo un día atrás). El negocio opera en Lima, pero el dato viene en UTC.
_TZ = "UTC"


def _default_range(days: int) -> tuple[date, date]:
    today = date.today()
    return today - timedelta(days=days - 1), today + timedelta(days=1)


@router.get("/kpis")
async def kpis(
    days: int = Query(30, ge=1, le=365),
    office_id: int | None = Query(None),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    dfrom, dto = _default_range(days)
    cid = company.company_id
    company_config = await get_company(db, cid)
    tipos_venta = company_config.get("tipos_venta") or DEFAULT_TIPOS_VENTA
    if office_id is not None:
        office_filter = "bsale_office_id = :office_id"
        params = {"dfrom": dfrom, "dto": dto, "office_id": office_id,
                  "tipos_venta": tipos_venta, "cid": cid}
    else:
        office_filter = _OFFICE_FILTER_PLAIN
        params = {"dfrom": dfrom, "dto": dto, "tipos_venta": tipos_venta, "cid": cid}

    ventas_q = f"""
        SELECT COALESCE(SUM(total_amount), 0)
        FROM documents
        WHERE company_id = :cid
          AND (emission_date AT TIME ZONE '{_TZ}')::DATE >= :dfrom
          AND (emission_date AT TIME ZONE '{_TZ}')::DATE < :dto
          AND COALESCE(is_credit_note, FALSE) = FALSE
          AND bsale_document_type_id = ANY(:tipos_venta)
          AND {office_filter}
    """
    ventas = await db.scalar(text(ventas_q), params) or 0

    tickets_q = f"""
        SELECT COUNT(*)
        FROM documents
        WHERE company_id = :cid
          AND (emission_date AT TIME ZONE '{_TZ}')::DATE >= :dfrom
          AND (emission_date AT TIME ZONE '{_TZ}')::DATE < :dto
          AND COALESCE(is_credit_note, FALSE) = FALSE
          AND bsale_document_type_id = ANY(:tipos_venta)
          AND {office_filter}
    """
    tickets = await db.scalar(text(tickets_q), params) or 0

    tickets_monto_q = f"""
        SELECT COUNT(*)
        FROM documents
        WHERE company_id = :cid
          AND (emission_date AT TIME ZONE '{_TZ}')::DATE >= :dfrom
          AND (emission_date AT TIME ZONE '{_TZ}')::DATE < :dto
          AND COALESCE(is_credit_note, FALSE) = FALSE
          AND bsale_document_type_id = ANY(:tipos_venta)
          AND total_amount > 0
          AND {office_filter}
    """
    tickets_con_monto = await db.scalar(text(tickets_monto_q), params) or 0

    prod_tot = await db.scalar(text("SELECT COUNT(*) FROM products WHERE company_id = :cid"), {"cid": cid}) or 0
    prod_mapeados = await db.scalar(text(
        "SELECT COUNT(*) FROM v_products_full WHERE company_id = :cid AND department IS NOT NULL"
    ), {"cid": cid}) or 0
    variantes = await db.scalar(text("SELECT COUNT(*) FROM variants WHERE company_id = :cid"), {"cid": cid}) or 0

    stock_valor_q = """
        SELECT COALESCE(SUM(sl.quantity_available * COALESCE(vc.effective_cost, 0)), 0)
        FROM stock_levels sl
        LEFT JOIN variant_costs vc
               ON vc.company_id = sl.company_id
              AND vc.bsale_variant_id = sl.bsale_variant_id
        WHERE sl.company_id = :cid
          AND sl.quantity_available > 0
    """
    stock_valor = await db.scalar(text(stock_valor_q), {"cid": cid}) or 0
    sucursales = await db.scalar(
        text("SELECT COUNT(*) FROM offices WHERE company_id = :cid"),
        {"cid": cid},
    ) or 0

    return {
        "periodo_dias": days,
        "ventas": float(ventas),
        "tickets": tickets,
        "tickets_con_monto": tickets_con_monto,
        "ticket_promedio": float(ventas) / tickets_con_monto if tickets_con_monto else 0.0,
        "productos_total": prod_tot,
        "productos_mapeados": prod_mapeados,
        "variantes_total": variantes,
        "stock_valorizado": float(stock_valor),
        "sucursales": sucursales,
    }


# ============================================================================
# ENDPOINT COMENTADO (2026-06-20) — duplica /stock/valuation y no se consume.
# El frontend llama /stock/valuation (api.ts:387) que vive en routers/stock.py.
# Este endpoint fue creado en una migración que nunca se completó.
# Se preserva la query SQL para reactivar si vuelve a necesitarse.
# ============================================================================

# @router.get("/stock-valuation")
# async def stock_valuation(db: AsyncSession = Depends(get_db)) -> dict:
#     """Stock valorizado por sucursal (usa effective_cost).
#
#     Devuelve bsale_office_id para que el frontend pueda filtrar por el ID real
#     de BSale (1, 3, 4) en lugar de hacer un mapeo posicional.
#
#     Antes vivía en /stock/valuation; movido aquí en la limpieza de cleanup
#     porque el router /stock se eliminó pero el selector global de sucursal
#     necesita este dato en TODAS las páginas.
#     """
#     res = await db.execute(text("""
#         SELECT o.bsale_office_id, o.name AS sucursal,
#                ROUND(SUM(sl.quantity_available * COALESCE(vc.effective_cost, 0))::numeric, 2) AS valor_soles,
#                SUM(sl.quantity_available) AS unidades
#         FROM stock_levels sl
#         JOIN offices o              ON o.bsale_office_id  = sl.bsale_office_id
#         LEFT JOIN variant_costs vc  ON vc.bsale_variant_id = sl.bsale_variant_id
#         WHERE sl.quantity_available > 0
#         GROUP BY o.bsale_office_id, o.name
#         ORDER BY valor_soles DESC
#     """))
#     rows = [dict(r) for r in res.mappings().all()]
#
#     total = sum(float(r["valor_soles"] or 0) for r in rows)
#     return {
#         "total_soles": round(total, 2),
#         "por_sucursal": [
#             {
#                 "bsale_office_id": r["bsale_office_id"],
#                 "sucursal": r["sucursal"],
#                 "valor_soles": float(r["valor_soles"] or 0),
#                 "unidades": float(r["unidades"] or 0),
#             }
#             for r in rows
#         ],
#     }


@router.get("/sales-by-day")
async def sales_by_day(
    days: int = Query(30, ge=1, le=365),
    office_id: int | None = Query(None),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    dfrom, dto = _default_range(days)
    cid = company.company_id
    company_config = await get_company(db, cid)
    tipos_venta = company_config.get("tipos_venta") or DEFAULT_TIPOS_VENTA
    if office_id is not None:
        office_filter = "bsale_office_id = :office_id"
        params = {"dfrom": dfrom, "dto": dto, "office_id": office_id, "tipos_venta": tipos_venta, "cid": cid}
    else:
        office_filter = _OFFICE_FILTER_PLAIN
        params = {"dfrom": dfrom, "dto": dto, "tipos_venta": tipos_venta, "cid": cid}
    query = f"""
        SELECT (emission_date AT TIME ZONE '{_TZ}')::DATE AS dia,
               ROUND(SUM(total_amount)::numeric, 2) AS ventas,
               COUNT(*) AS tickets,
               ROUND(AVG(CASE WHEN total_amount > 0 THEN total_amount END)::numeric, 2) AS ticket_promedio
        FROM documents
        WHERE company_id = :cid
          AND (emission_date AT TIME ZONE '{_TZ}')::DATE >= :dfrom
          AND (emission_date AT TIME ZONE '{_TZ}')::DATE < :dto
          AND COALESCE(is_credit_note, FALSE) = FALSE
          AND bsale_document_type_id = ANY(:tipos_venta)
          AND {office_filter}
        GROUP BY dia
        ORDER BY dia
    """
    res = await db.execute(text(query), params)
    return [dict(r) for r in res.mappings().all()]


@router.get("/sales-by-department")
async def sales_by_department(
    days: int = Query(30, ge=1, le=365),
    office_id: int | None = Query(None),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    dfrom, dto = _default_range(days)
    cid = company.company_id
    company_config = await get_company(db, cid)
    tipos_venta = company_config.get("tipos_venta") or DEFAULT_TIPOS_VENTA
    if office_id is not None:
        office_filter = "doc.bsale_office_id = :office_id"
        params = {"dfrom": dfrom, "dto": dto, "office_id": office_id, "tipos_venta": tipos_venta, "cid": cid}
    else:
        office_filter = _OFFICE_FILTER_DOC
        params = {"dfrom": dfrom, "dto": dto, "tipos_venta": tipos_venta, "cid": cid}
    query = f"""
        SELECT vpf.department AS departamento,
               ROUND(SUM(dd.total_amount)::numeric, 2) AS ventas,
               COUNT(DISTINCT doc.bsale_document_id)   AS tickets,
               ROUND(AVG(dd.total_amount)::numeric, 2) AS ticket_promedio_linea
        FROM document_details dd
        JOIN documents doc       ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
        JOIN variants v          ON v.bsale_variant_id    = dd.bsale_variant_id  AND v.company_id   = dd.company_id
        JOIN v_products_full vpf ON vpf.bsale_product_id  = v.bsale_product_id   AND vpf.company_id = v.company_id
        WHERE dd.company_id = :cid
          AND (doc.emission_date AT TIME ZONE '{_TZ}')::DATE >= :dfrom
          AND (doc.emission_date AT TIME ZONE '{_TZ}')::DATE < :dto
          AND COALESCE(doc.is_credit_note, FALSE) = FALSE
          AND doc.bsale_document_type_id = ANY(:tipos_venta)
          AND {office_filter}
          AND vpf.department IS NOT NULL
        GROUP BY vpf.department
        ORDER BY ventas DESC
    """
    res = await db.execute(text(query), params)
    return [dict(r) for r in res.mappings().all()]


@router.get("/sales-by-category")
async def sales_by_category(
    days: int = Query(30, ge=1, le=365),
    office_id: int | None = Query(None),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    dfrom, dto = _default_range(days)
    cid = company.company_id
    company_config = await get_company(db, cid)
    tipos_venta = company_config.get("tipos_venta") or DEFAULT_TIPOS_VENTA
    if office_id is not None:
        office_filter = "doc.bsale_office_id = :office_id"
        params = {"dfrom": dfrom, "dto": dto, "office_id": office_id, "tipos_venta": tipos_venta, "cid": cid}
    else:
        office_filter = _OFFICE_FILTER_DOC
        params = {"dfrom": dfrom, "dto": dto, "tipos_venta": tipos_venta, "cid": cid}
    query = f"""
        SELECT vpf.department AS departamento, vpf.category AS categoria,
               ROUND(SUM(dd.total_amount)::numeric, 2) AS ventas,
               COUNT(DISTINCT doc.bsale_document_id)   AS tickets
        FROM document_details dd
        JOIN documents doc       ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
        JOIN variants v          ON v.bsale_variant_id    = dd.bsale_variant_id  AND v.company_id   = dd.company_id
        JOIN v_products_full vpf ON vpf.bsale_product_id  = v.bsale_product_id   AND vpf.company_id = v.company_id
        WHERE dd.company_id = :cid
          AND (doc.emission_date AT TIME ZONE '{_TZ}')::DATE >= :dfrom
          AND (doc.emission_date AT TIME ZONE '{_TZ}')::DATE < :dto
          AND COALESCE(doc.is_credit_note, FALSE) = FALSE
          AND doc.bsale_document_type_id = ANY(:tipos_venta)
          AND {office_filter}
          AND vpf.department IS NOT NULL
        GROUP BY vpf.department, vpf.category
        ORDER BY ventas DESC
    """
    res = await db.execute(text(query), params)
    return [dict(r) for r in res.mappings().all()]


# ENDPOINT COMENTADO (2026-06-20) — getSalesBySubcategory definido en api.ts
# pero ninguna página lo importa. Se preserva la query SQL.

# @router.get("/sales-by-subcategory")
# async def sales_by_subcategory(
#     days: int = Query(30, ge=1, le=365),
#     office_id: int | None = Query(None),
#     db: AsyncSession = Depends(get_db),
# ) -> list[dict]:
#     dfrom, dto = _default_range(days)
#     if office_id is not None:
#         office_filter = "doc.bsale_office_id = :office_id"
#         params = {"dfrom": dfrom, "dto": dto, "office_id": office_id}
#     else:
#         office_filter = _OFFICE_FILTER_DOC
#         params = {"dfrom": dfrom, "dto": dto, "tipos_venta": tipos_venta}
#     query = f"""
#         SELECT vpf.department AS departamento, vpf.category AS categoria, vpf.subcategory AS subcategoria,
#                ROUND(SUM(dd.total_amount)::numeric, 2) AS ventas,
#                COUNT(DISTINCT doc.bsale_document_id)   AS tickets
#         FROM document_details dd
#         JOIN documents doc       ON doc.bsale_document_id = dd.bsale_document_id
#         JOIN variants v          ON v.bsale_variant_id    = dd.bsale_variant_id
#         JOIN v_products_full vpf ON vpf.bsale_product_id  = v.bsale_product_id
#         WHERE (doc.emission_date AT TIME ZONE '{_TZ}')::DATE >= :dfrom
#           AND (doc.emission_date AT TIME ZONE '{_TZ}')::DATE < :dto
#           AND COALESCE(doc.is_credit_note, FALSE) = FALSE
#           AND {office_filter}
#           AND vpf.department IS NOT NULL
#         GROUP BY vpf.department, vpf.category, vpf.subcategory
#         ORDER BY ventas DESC
#     """
#     res = await db.execute(text(query), params)
#     return [dict(r) for r in res.mappings().all()]


@router.get("/top-products")
async def top_products(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(20, ge=1, le=200),
    office_id: int | None = Query(None),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    dfrom, dto = _default_range(days)
    cid = company.company_id
    company_config = await get_company(db, cid)
    tipos_venta = company_config.get("tipos_venta") or DEFAULT_TIPOS_VENTA
    if office_id is not None:
        office_filter = "doc.bsale_office_id = :office_id"
        params: dict[str, Any] = {"dfrom": dfrom, "dto": dto, "limit": limit, "office_id": office_id, "tipos_venta": tipos_venta, "cid": cid}
    else:
        office_filter = _OFFICE_FILTER_DOC
        params = {"dfrom": dfrom, "dto": dto, "limit": limit, "tipos_venta": tipos_venta, "cid": cid}
    query = f"""
        SELECT p.bsale_product_id, p.name AS producto,
               ROUND(SUM(dd.total_amount)::numeric, 2) AS ventas,
               SUM(dd.quantity) AS unidades
        FROM document_details dd
        JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
        JOIN variants v    ON v.bsale_variant_id    = dd.bsale_variant_id  AND v.company_id   = dd.company_id
        JOIN products p    ON p.bsale_product_id    = v.bsale_product_id   AND p.company_id   = v.company_id
        WHERE dd.company_id = :cid
          AND (doc.emission_date AT TIME ZONE '{_TZ}')::DATE >= :dfrom
          AND (doc.emission_date AT TIME ZONE '{_TZ}')::DATE < :dto
          AND COALESCE(doc.is_credit_note, FALSE) = FALSE
          AND doc.bsale_document_type_id = ANY(:tipos_venta)
          AND {office_filter}
        GROUP BY p.bsale_product_id, p.name
        ORDER BY ventas DESC
        LIMIT :limit
    """
    res = await db.execute(text(query), params)
    return [dict(r) for r in res.mappings().all()]


# ──────────────────────────────────────────────────────────────────────────────
# TICKET ANATOMY — descompone Δventas en (Δtickets × Δunds/ticket × Δ$/und)
#
# Diseño:
#   ventas = tickets × unds_por_ticket × monto_por_und
#   Tomamos log:   ln(V) = ln(T) + ln(Q) + ln(P)
#   Restamos:      Δln(V) = Δln(T) + Δln(Q) + Δln(P)   ← EXACTO (no aproximación)
#
#   Reportamos cada Δln(·) en % aproximado (multiplicando por 100). Para cambios
#   moderados (<25%), Δln(x) ≈ Δx/x = "cambio porcentual"; para cambios más
#   grandes la suma sigue siendo exacta en escala log, no en %.
#
#   Esto responde la pregunta operativa "¿la caída es por tráfico, canasta o
#   precio?" — en una mirada se ve qué factor domina.
# ──────────────────────────────────────────────────────────────────────────────

async def _period_metrics(
    db: AsyncSession,
    dfrom: date,
    dto: date,
    office_filter_plain: str,
    office_filter_doc: str,
    params: dict[str, Any],
    tipos_venta: list[int]
) -> dict[str, float]:
    """Devuelve métricas agregadas del período [dfrom, dto).
    Requiere que `params` contenga 'cid' (company_id) para aislamiento multi-tenant."""
    params["tipos_venta"] = tipos_venta
    # Query 1 — sobre documents (ventas + count de tickets, sin riesgo de
    # duplicar por JOIN con líneas)
    q1 = f"""
        SELECT COALESCE(SUM(total_amount), 0) AS ventas,
               COUNT(*)                       AS tickets
        FROM documents
        WHERE company_id = :cid
          AND (emission_date AT TIME ZONE '{_TZ}')::DATE >= :dfrom
          AND (emission_date AT TIME ZONE '{_TZ}')::DATE <  :dto
          AND COALESCE(is_credit_note, FALSE) = FALSE
          AND bsale_document_type_id = ANY(:tipos_venta)
          AND {office_filter_plain}
    """
    r1 = (await db.execute(text(q1), params)).mappings().one()

    # Query 2 — sobre líneas (unds, costo, descuentos, gratuidades).
    q2 = f"""
        SELECT
          COALESCE(SUM(dd.quantity) FILTER (WHERE NOT dd.is_gratuity), 0)             AS unds,
          COALESCE(SUM(dd.quantity * vc.effective_cost)
                   FILTER (WHERE NOT dd.is_gratuity AND vc.effective_cost IS NOT NULL), 0) AS costo,
          COALESCE(SUM(dd.net_discount) FILTER (WHERE NOT dd.is_gratuity), 0)         AS descuento,
          COALESCE(COUNT(*) FILTER (WHERE dd.is_gratuity), 0)                         AS lineas_regalo,
          COALESCE(SUM(dd.total_amount) FILTER (WHERE dd.is_gratuity), 0)             AS monto_regalo,
          COALESCE(SUM(dd.quantity) FILTER (WHERE dd.is_gratuity), 0)                 AS unds_regalo
        FROM document_details dd
        JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
        LEFT JOIN variant_costs vc ON vc.bsale_variant_id = dd.bsale_variant_id AND vc.company_id = dd.company_id
        WHERE dd.company_id = :cid
          AND (doc.emission_date AT TIME ZONE '{_TZ}')::DATE >= :dfrom
          AND (doc.emission_date AT TIME ZONE '{_TZ}')::DATE <  :dto
          AND COALESCE(doc.is_credit_note, FALSE) = FALSE
          AND doc.bsale_document_type_id = ANY(:tipos_venta)
          AND {office_filter_doc}
    """
    r2 = (await db.execute(text(q2), params)).mappings().one()

    ventas  = float(r1["ventas"])
    tickets = int(r1["tickets"])
    unds    = float(r2["unds"])
    costo   = float(r2["costo"])
    return {
        "ventas":             ventas,
        "tickets":            tickets,
        "unds":               unds,
        "unds_per_ticket":    (unds / tickets) if tickets else 0.0,
        "monto_per_und":      (ventas / unds) if unds else 0.0,
        "ticket_promedio":    (ventas / tickets) if tickets else 0.0,
        # Margen: % y monto absoluto. Si no hay costos de algunas líneas, el
        # margen estará inflado — reportamos cobertura de costos para detectarlo.
        "margen_pct":         ((ventas - costo) / ventas * 100) if ventas else 0.0,
        "margen_monto":       ventas - costo,
        "descuento_aplicado": float(r2["descuento"]),
        "lineas_regalo":      int(r2["lineas_regalo"]),
        "monto_regalo":       float(r2["monto_regalo"]),
        "unds_regalo":        float(r2["unds_regalo"]),
    }


def _pct_delta(curr: float, prev: float) -> float | None:
    """Cambio porcentual con guardia contra prev=0."""
    if prev is None or prev <= 0:
        return None
    return (curr - prev) / prev * 100


def _log_contrib(curr: float, prev: float) -> float | None:
    """Contribución log en %. La suma de las 3 contribuciones (tickets,
    unds/ticket, $/und) iguala EXACTAMENTE la contribución total de ventas."""
    if curr is None or prev is None or curr <= 0 or prev <= 0:
        return None
    return math.log(curr / prev) * 100


@router.get("/ticket-anatomy")
async def ticket_anatomy(
    days: int = Query(7, ge=1, le=180, description="Ventana del período actual (excluye hoy)"),
    compare: str = Query(
        "previous_period",
        pattern="^(previous_period|previous_week|previous_year)$",
        description="Con qué comparar: previous_period (los `days` anteriores), previous_week (-7d), previous_year (-365d)",
    ),
    office_id: int | None = Query(None),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Descompone el cambio en ventas entre el período actual y uno de comparación.

    Responde la pregunta operativa **"¿la caída es por tráfico, canasta o precio?"**

    El período actual SIEMPRE excluye HOY (que tiene data parcial). Por defecto
    compara con el período anterior espejo (`previous_period`).

    Respuesta:
      - current / previous : métricas absolutas de cada ventana
      - delta_pct          : cambio % por métrica
      - decomposition_log_pct : las 3 contribuciones suman al cambio total en escala log
    """
    today = date.today()
    # Período current: termina HOY (exclusive) — i.e. cierra ayer.
    cur_dto   = today
    cur_dfrom = today - timedelta(days=days)

    if compare == "previous_period":
        prev_dto   = cur_dfrom
        prev_dfrom = cur_dfrom - timedelta(days=days)
    elif compare == "previous_week":
        prev_dto   = cur_dto   - timedelta(days=7)
        prev_dfrom = cur_dfrom - timedelta(days=7)
    else:  # previous_year
        prev_dto   = cur_dto   - timedelta(days=365)
        prev_dfrom = cur_dfrom - timedelta(days=365)

    cid = company.company_id
    company_config = await get_company(db, cid)
    tipos_venta = company_config.get("tipos_venta") or DEFAULT_TIPOS_VENTA
    if office_id is not None:
        of_plain = "bsale_office_id = :office_id"
        of_doc   = "doc.bsale_office_id = :office_id"
        cur_params  = {"dfrom": cur_dfrom,  "dto": cur_dto,  "office_id": office_id, "cid": cid}
        prev_params = {"dfrom": prev_dfrom, "dto": prev_dto, "office_id": office_id, "cid": cid}
    else:
        of_plain = _OFFICE_FILTER_PLAIN
        of_doc   = _OFFICE_FILTER_DOC
        cur_params  = {"dfrom": cur_dfrom,  "dto": cur_dto, "cid": cid}
        prev_params = {"dfrom": prev_dfrom, "dto": prev_dto, "cid": cid}

    cur  = await _period_metrics(db, cur_dfrom,  cur_dto,  of_plain, of_doc, cur_params, tipos_venta)
    prev = await _period_metrics(db, prev_dfrom, prev_dto, of_plain, of_doc, prev_params, tipos_venta)

    delta_pct = {
        "ventas":          _pct_delta(cur["ventas"],          prev["ventas"]),
        "tickets":         _pct_delta(cur["tickets"],         prev["tickets"]),
        "unds":            _pct_delta(cur["unds"],            prev["unds"]),
        "unds_per_ticket": _pct_delta(cur["unds_per_ticket"], prev["unds_per_ticket"]),
        "monto_per_und":   _pct_delta(cur["monto_per_und"],   prev["monto_per_und"]),
        "ticket_promedio": _pct_delta(cur["ticket_promedio"], prev["ticket_promedio"]),
        # Margen: diferencia en PUNTOS PORCENTUALES (no es % de cambio, es pp).
        "margen_pp":       (cur["margen_pct"] - prev["margen_pct"]) if prev["ventas"] else None,
    }
    decomposition = {
        "tickets":         _log_contrib(cur["tickets"],         prev["tickets"]),
        "unds_per_ticket": _log_contrib(cur["unds_per_ticket"], prev["unds_per_ticket"]),
        "monto_per_und":   _log_contrib(cur["monto_per_und"],   prev["monto_per_und"]),
        "total":           _log_contrib(cur["ventas"],          prev["ventas"]),
    }

    return {
        "current":  {"from": cur_dfrom.isoformat(),  "to": (cur_dto  - timedelta(days=1)).isoformat(), **cur},
        "previous": {"from": prev_dfrom.isoformat(), "to": (prev_dto - timedelta(days=1)).isoformat(), **prev},
        "delta_pct": delta_pct,
        "decomposition_log_pct": decomposition,
        "compare": compare,
        "days": days,
    }


@router.get("/sales-by-office")
async def sales_by_office(
    days: int = Query(30, ge=1, le=365),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    dfrom, dto = _default_range(days)
    cid = company.company_id
    company_config = await get_company(db, cid)
    tipos_venta = company_config.get("tipos_venta") or DEFAULT_TIPOS_VENTA
    query = f"""
        SELECT o.name AS sucursal,
               ROUND(SUM(dd.total_amount)::numeric, 2) AS ventas,
               COUNT(DISTINCT doc.bsale_document_id)   AS tickets
        FROM document_details dd
        JOIN documents doc ON doc.bsale_document_id  = dd.bsale_document_id AND doc.company_id = dd.company_id
        LEFT JOIN offices o ON o.bsale_office_id     = doc.bsale_office_id  AND o.company_id   = doc.company_id
        WHERE dd.company_id = :cid
          AND (doc.emission_date AT TIME ZONE '{_TZ}')::DATE >= :dfrom
          AND (doc.emission_date AT TIME ZONE '{_TZ}')::DATE < :dto
          AND COALESCE(doc.is_credit_note, FALSE) = FALSE
          AND {_OFFICE_FILTER_DOC}
        GROUP BY o.name
        ORDER BY ventas DESC
    """
    res = await db.execute(text(query), {"dfrom": dfrom, "dto": dto, "tipos_venta": tipos_venta, "cid": cid})
    return [dict(r) for r in res.mappings().all()]


# ══════════════════════════════════════════════════════════════════════════════
# TABLERO SEMANAL — los 5 KPIs de revisión de gerencia.
#
#   1. Ticket promedio (diaria)      → /analytics/kpis · /analytics/sales-by-day   (ya existían)
#   2. N° de transacciones (diaria)  → idem
#   3. Venta por categoría (semanal) → /analytics/sales-by-category                (ya existía)
#   4. SKUs en quiebre (semanal)     → /analytics/stockouts                        (nuevo)
#   5. Venta acumulada vs meta       → /analytics/sales-vs-goal (+ /analytics/goals para cargar la meta)
#
#   /analytics/weekly-board junta los 5 en un payload; /weekly-board/excel los
#   exporta con UNA PESTAÑA POR INFORME.
# ══════════════════════════════════════════════════════════════════════════════


# ── KPI 4: SKUs en quiebre ──────────────────────────────────────────────────
@router.get("/stockouts")
async def stockouts(
    office_id: int | None = Query(None),
    demand_window_days: int = Query(
        30, ge=1, le=365, description="Ventana para marcar 'tenía demanda' (venta perdida)"
    ),
    limit: int = Query(500, ge=1, le=5000),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """SKUs en quiebre: stock disponible <= 0 en las tiendas de venta (offices 1,3).

    Marca `tenia_demanda` = vendió algo en los últimos `demand_window_days` días en
    ESA sucursal (quiebre con venta perdida real, lo accionable). Los con demanda van primero.
    """
    today = date.today()
    d_window = today - timedelta(days=demand_window_days)
    d_lookback = today - timedelta(days=90)  # contexto de ventas y última venta
    cid = company.company_id
    if office_id is not None:
        office_filter = "sl.bsale_office_id = :office_id"
        params: dict[str, Any] = {"office_id": office_id}
    else:
        office_filter = "sl." + _OFFICE_FILTER_PLAIN
        params = {}
    company_config = await get_company(db, cid)
    tipos_venta = company_config.get("tipos_venta") or DEFAULT_TIPOS_VENTA
    params.update({"dwin": d_window, "dlb": d_lookback, "limit": limit, "tipos_venta": tipos_venta, "cid": cid})

    query = f"""
        WITH ventas AS (
            SELECT doc.bsale_office_id,
                   dd.bsale_variant_id,
                   SUM(dd.quantity) FILTER (
                       WHERE (doc.emission_date AT TIME ZONE '{_TZ}')::DATE >= :dwin
                   )                                                       AS unds_demanda,
                   SUM(dd.quantity)                                        AS unds_90d,
                   MAX((doc.emission_date AT TIME ZONE '{_TZ}')::DATE)     AS ultima_venta
            FROM document_details dd
            JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
            WHERE dd.company_id = :cid
              AND (doc.emission_date AT TIME ZONE '{_TZ}')::DATE >= :dlb
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND {_OFFICE_FILTER_DOC}
            GROUP BY doc.bsale_office_id, dd.bsale_variant_id
        ),
        base AS (
            SELECT o.name                                   AS sucursal,
                   sl.bsale_office_id                       AS office_id,
                   v.display_code                           AS sku,
                   vpf.product_name                         AS producto,
                   vpf.department                           AS departamento,
                   vpf.category                             AS categoria,
                   ROUND(sl.quantity_available::numeric, 2) AS stock_disponible,
                   COALESCE(ve.unds_demanda, 0)             AS unds_vendidas_ventana,
                   COALESCE(ve.unds_90d, 0)                 AS unds_vendidas_90d,
                   ve.ultima_venta                          AS ultima_venta,
                   (COALESCE(ve.unds_demanda, 0) > 0)       AS tenia_demanda
            FROM stock_levels sl
            JOIN variants v  ON v.bsale_variant_id = sl.bsale_variant_id AND v.company_id = sl.company_id AND v.is_active
            JOIN products p  ON p.bsale_product_id = v.bsale_product_id  AND p.company_id = v.company_id
                            AND p.is_active AND p.stock_control
            LEFT JOIN v_products_full vpf ON vpf.bsale_product_id = v.bsale_product_id AND vpf.company_id = v.company_id
            LEFT JOIN offices o          ON o.bsale_office_id   = sl.bsale_office_id  AND o.company_id   = sl.company_id
            LEFT JOIN ventas ve          ON ve.bsale_variant_id = sl.bsale_variant_id
                                        AND ve.bsale_office_id  = sl.bsale_office_id
            WHERE sl.company_id = :cid
              AND {office_filter}
              AND sl.quantity_available <= 0
        )
        SELECT base.*,
               COUNT(*) OVER ()                              AS _total,
               COUNT(*) FILTER (WHERE tenia_demanda) OVER () AS _con_demanda
        FROM base
        ORDER BY tenia_demanda DESC, unds_vendidas_90d DESC, sucursal
        LIMIT :limit
    """
    res = await db.execute(text(query), params)
    rows = [dict(r) for r in res.mappings().all()]
    # `_total` / `_con_demanda` son COUNT() de ventana: cuentan TODO el universo en
    # quiebre aunque la lista venga recortada por LIMIT (KPI exacto).
    total = int(rows[0]["_total"]) if rows else 0
    con_demanda = int(rows[0]["_con_demanda"]) if rows else 0
    for r in rows:
        r.pop("_total", None)
        r.pop("_con_demanda", None)
    return {
        "total": total,
        "con_demanda": con_demanda,
        "sin_demanda": total - con_demanda,
        "returned": len(rows),
        "demand_window_days": demand_window_days,
        "skus": rows,
    }


# ── KPI 5: Venta acumulada vs meta (meta manual) ────────────────────────────
class GoalsBody(BaseModel):
    month: str                          # "YYYY-MM"
    meta_global: float | None = None    # meta de toda la empresa (S/)
    offices: dict[str, float] = {}      # {"1": 300000, "3": 200000} (claves = bsale_office_id)


@router.get("/goals")
async def read_goals(
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Todas las metas de venta configuradas (manual), keyed por mes 'YYYY-MM'."""
    return {"goals": await get_goals(db, company.company_id)}


@router.put("/goals", dependencies=[Depends(require_operador_or_admin)])
async def write_goals(
    body: GoalsBody,
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Carga/actualiza la meta de un mes (reemplaza la de ese mes por completo)."""
    if len(body.month) != 7 or body.month[4] != "-":
        raise HTTPException(status_code=400, detail="month debe tener formato 'YYYY-MM'")
    month_goals: dict[str, float] = {}
    if body.meta_global is not None:
        month_goals["global"] = body.meta_global
    for k, v in body.offices.items():
        month_goals[str(k)] = v
    goals = await set_goals_month(db, company.company_id, body.month, month_goals)
    return {"ok": True, "month": body.month, "saved": month_goals, "goals": goals}


def _month_str(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


@router.get("/sales-vs-goal")
async def sales_vs_goal(
    month: str | None = Query(
        None, pattern=r"^\d{4}-\d{2}$", description="Mes YYYY-MM (default: mes en curso)"
    ),
    office_id: int | None = Query(None),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Venta acumulada del mes vs meta mensual manual (cargada en PUT /analytics/goals).

        venta_acumulada       = SUM(total_amount) del 1° del mes a la fecha de corte
        meta_prorrateada_hoy  = meta * dias_transcurridos / dias_del_mes
        avance_%              = venta_acumulada / meta * 100
        cumplimiento_vs_ritmo = venta_acumulada / meta_prorrateada * 100   (>100 = adelantado)
        proyeccion_cierre_mes = venta_acumulada / dias_transcurridos * dias_del_mes
    """
    today = date.today()
    mes = month or _month_str(today)
    year, mon = int(mes[:4]), int(mes[5:7])
    dias_del_mes = monthrange(year, mon)[1]
    first = date(year, mon, 1)
    last = date(year, mon, dias_del_mes)
    if today < first:            # mes futuro: nada transcurrido
        corte, dias_transcurridos = first, 0
    elif today > last:           # mes pasado: cerrado completo
        corte, dias_transcurridos = last, dias_del_mes
    else:                        # mes en curso (hoy parcial)
        corte, dias_transcurridos = today, today.day
    dfrom, dto = first, corte + timedelta(days=1)

    cid = company.company_id
    company_config = await get_company(db, cid)
    tipos_venta = company_config.get("tipos_venta") or DEFAULT_TIPOS_VENTA

    if office_id is not None:
        office_filter = "bsale_office_id = :office_id"
        params: dict[str, Any] = {"dfrom": dfrom, "dto": dto, "office_id": office_id, "tipos_venta": tipos_venta, "cid": cid}
        scope = [office_id]
    else:
        office_filter = _OFFICE_FILTER_PLAIN
        params = {"dfrom": dfrom, "dto": dto, "tipos_venta": tipos_venta, "cid": cid}
        scope = list(OFFICE_IDS)

    query = f"""
        SELECT bsale_office_id, COALESCE(SUM(total_amount), 0) AS venta
        FROM documents
        WHERE company_id = :cid
          AND (emission_date AT TIME ZONE '{_TZ}')::DATE >= :dfrom
          AND (emission_date AT TIME ZONE '{_TZ}')::DATE < :dto
          AND COALESCE(is_credit_note, FALSE) = FALSE
          AND bsale_document_type_id = ANY(:tipos_venta)
          AND {office_filter}
        GROUP BY bsale_office_id
    """
    res = await db.execute(text(query), params)
    ventas_por_office = {int(r["bsale_office_id"]): float(r["venta"]) for r in res.mappings().all()}

    off_rows = (await db.execute(
        text("SELECT bsale_office_id, name FROM offices WHERE company_id = :cid AND bsale_office_id = ANY(:ids)"),
        {"cid": cid, "ids": scope},
    )).mappings().all()
    office_names = {int(r["bsale_office_id"]): r["name"] for r in off_rows}

    goals = await get_goals(db, cid)
    month_goals, fuente = goal_for_month(goals, mes)
    frac = (dias_transcurridos / dias_del_mes) if dias_del_mes else 0.0

    def _meta_val(key: str) -> float | None:
        v = month_goals.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _row(oid: int | None, venta: float, meta: float | None) -> dict:
        meta_prorr = (meta * frac) if meta is not None else None
        avance = (venta / meta * 100) if meta else None
        ritmo = (venta / meta_prorr * 100) if meta_prorr else None
        proy = (venta / dias_transcurridos * dias_del_mes) if dias_transcurridos else None
        return {
            "office_id": oid,
            "sucursal": "TODAS" if oid is None else office_names.get(oid, str(oid)),
            "venta_acumulada": round(venta, 2),
            "meta": meta,
            "meta_prorrateada": round(meta_prorr, 2) if meta_prorr is not None else None,
            "avance_pct": round(avance, 1) if avance is not None else None,
            "cumplimiento_vs_ritmo_pct": round(ritmo, 1) if ritmo is not None else None,
            "proyeccion_cierre_mes": round(proy, 2) if proy is not None else None,
        }

    por_sucursal = [_row(oid, ventas_por_office.get(oid, 0.0), _meta_val(str(oid))) for oid in scope]

    if office_id is not None:
        glob = por_sucursal[0]
    else:
        venta_total = sum(ventas_por_office.get(oid, 0.0) for oid in scope)
        meta_global = _meta_val("global")
        if meta_global is None:  # sin meta global → suma de metas por sucursal
            sub = [_meta_val(str(oid)) for oid in scope]
            meta_global = sum(x for x in sub if x is not None) or None
        glob = _row(None, venta_total, meta_global)

    return {
        "month": mes,
        "meta_source": fuente,
        "dias_transcurridos": dias_transcurridos,
        "dias_del_mes": dias_del_mes,
        "mes_en_curso": first <= today <= last,
        "global": glob,
        "por_sucursal": por_sucursal,
    }


# ── Tablero consolidado (los 5 KPIs) + export Excel ─────────────────────────
@router.get("/weekly-board")
async def weekly_board(
    days: int = Query(7, ge=1, le=365, description="Ventana para los KPIs diarios/semanales"),
    office_id: int | None = Query(None),
    month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$", description="Mes de la meta (default: en curso)"),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Tablero semanal de gerencia: los 5 KPIs en un solo payload.

    Reutiliza los endpoints existentes (kpis, sales-by-day, sales-by-category) +
    los nuevos (stockouts, sales-vs-goal). El día en curso es PARCIAL.
    """
    kpi = await kpis(days=days, office_id=office_id, company=company, db=db)
    serie = await sales_by_day(days=days, office_id=office_id, company=company, db=db)
    categorias = await sales_by_category(days=days, office_id=office_id, company=company, db=db)
    quiebres = await stockouts(office_id=office_id, demand_window_days=30, limit=2000, company=company, db=db)
    meta = await sales_vs_goal(month=month, office_id=office_id, company=company, db=db)

    # % participación por categoría (acá, para no alterar el contrato de sales-by-category).
    total_cat = sum(float(c["ventas"] or 0) for c in categorias) or 1.0
    for c in categorias:
        c["participacion_pct"] = round(float(c["ventas"] or 0) / total_cat * 100, 1)

    return {
        "generado": datetime.utcnow().isoformat(),
        "periodo_dias": days,
        "office_id": office_id,
        "nota": "El día en curso es parcial; para gerencia revisar días cerrados (ayer hacia atrás).",
        "kpi_resumen": {
            "ticket_promedio": kpi["ticket_promedio"],
            "transacciones": kpi["tickets"],
            "ventas": kpi["ventas"],
            "skus_en_quiebre": quiebres["total"],
            "skus_en_quiebre_con_demanda": quiebres["con_demanda"],
            "avance_meta_pct": meta["global"].get("avance_pct"),
        },
        "ticket_promedio": {
            "serie_diaria": [{"dia": r["dia"], "ticket_promedio": r["ticket_promedio"]} for r in serie]
        },
        "transacciones": {
            "serie_diaria": [{"dia": r["dia"], "tickets": r["tickets"]} for r in serie]
        },
        "venta_por_categoria": categorias,
        "skus_en_quiebre": quiebres,
        "venta_vs_meta": meta,
    }


def _classify_severidad_accion_causal(label: str) -> tuple[str, str, str]:
    """Mapea la etiqueta de clasificación KAWII a (severidad, acción, causal) para el Excel.

    Severidad: 🔴 Crítico · 🟠 Alta · 🟡 Media · ⚪ Nulo · 🟣 Exceso.
    Causal sintetiza el "por qué" en una palabra accionable para gerencia.
    """
    L = (label or "").upper()
    # Crítico — venta perdida diaria
    if "OPORTUNIDAD PERDIDA" in L:           return "🔴 Crítico", "REPONER YA", "Desabastecimiento"
    if "QUIEBRE DE BESTSELLER" in L:         return "🔴 Crítico", "COMPRAR YA", "Quiebre alta rotación"
    if "BESTSELLER ACTIVO" in L:             return "🔴 Crítico", "REPONER YA", "Sobredemanda"
    if "ALTA ROTACIÓN" in L and "LOTE" in L: return "🔴 Crítico", "REPONER YA", "Lote fresco volando"
    if "ALTA ROTACIÓN" in L:                 return "🔴 Crítico", "REPONER YA", "Alta rotación"
    # Alta — reponer pronto
    if "BESTSELLER" in L and "AGOTADO" in L: return "🟠 Alta", "REPONER", "Olvido reposición"
    if "AGOTADO CON DEMANDA" in L:           return "🟠 Alta", "REPONER", "Demanda activa"
    if "ROTACIÓN BAJANDO" in L:              return "🟠 Alta", "REPONER MENOS", "Decay"
    if "ROTACIÓN ACTIVA AL BORDE" in L:      return "🟠 Alta", "PEDIR YA", "Cobertura baja"
    if "POCO STOCK CON DEMANDA" in L:        return "🟠 Alta", "REPONER", "Cobertura baja"
    # Media — vigilar / reponer poco
    if "LENTO PERO CONSTANTE" in L:          return "🟡 Media", "REPONER POCO", "Nicho lento"
    if "EX-BESTSELLER ENFRIADO" in L:        return "🟡 Media", "EVALUAR", "Demanda enfriada"
    if "PRODUCTO EMERGENTE" in L:            return "🟡 Media", "VIGILAR", "Emergente"
    if "VENDIENDO MÁS QUE ANTES" in L:       return "🟡 Media", "VIGILAR", "En alza"
    if "RECIBIDO Y NO VENDIDO" in L:         return "🟡 Media", "AUDITAR", "Anomalía"
    if "STOCK BAJO QUIETO" in L:             return "🟡 Media", "VERIFICAR", "Visibilidad/vencimiento"
    if "RITMO PERDIDO" in L:                 return "🟡 Media", "EVALUAR", "Pausa de venta"
    if "VENDIÓ Y SE PERDIÓ" in L:            return "🟡 Media", "INVESTIGAR", "Mermas + ventas"
    # Exceso — capital atrapado
    if "EXCESO + DEMANDA CAYENDO" in L:      return "🟣 Exceso", "PROMOCIONAR YA", "Sobreabast. + decay"
    if "EXCESO" in L or "STOCK EXCESIVO" in L: return "🟣 Exceso", "PROMOCIONAR", "Sobreabastecimiento"
    if "STOCK PARADO" in L:                  return "🟣 Exceso", "LIQUIDAR", "Sin rotación"
    if "LOTE FRENADO" in L:                  return "🟣 Exceso", "LIQUIDAR", "Frenado"
    if "BAJA ROTACIÓN" in L:                 return "🟡 Media", "PEDIR MENOS", "Baja rotación"
    # Nulo — no reponer
    if "DEMANDA EXTINTA" in L:               return "⚪ Nulo", "NO REPONER", "Demanda extinta"
    if "PRODUCTO MUERTO" in L:               return "⚪ Nulo", "DESCATALOGAR", "Sin rotación histórica"
    if "BAJO VOLUMEN AGOTADO" in L:          return "⚪ Nulo", "DESCATALOGAR", "Bajo volumen"
    if "LENTO CRÓNICO" in L:                 return "⚪ Nulo", "NO REPONER", "Crónicamente lento"
    if "AGOTADO NO PRIORITARIO" in L:        return "⚪ Bajo", "ESPERAR", "Sin urgencia"
    if "PÉRDIDA DE STOCK" in L:              return "🟡 Media", "AUDITAR", "Mermas/robos"
    # Esperar — gracia
    if "PRODUCTO NUEVO" in L:                return "⚪ Bajo", "ESPERAR", "Recién llegado"
    if "RECIÉN REABASTECIDO" in L or "STOCK RECIÉN LLEGADO" in L: return "⚪ Bajo", "ESPERAR", "Recién recibido"
    if "LOTE NUEVO VENDIENDO" in L:          return "🟢 Sano", "MANTENER", "Lote nuevo rotando"
    if "TEMPORADA CERRADA" in L:             return "⚪ Bajo", "ESPERAR PRÓX. CAMPAÑA", "Estacional"
    if "SALDO DE TEMPORADA" in L:            return "⚪ Bajo", "GUARDAR", "Estacional sobrante"
    # Saludable
    if "INVENTARIO SANO" in L:               return "🟢 Sano", "MANTENER", "Equilibrado"
    if "ROTACIÓN ACTIVA" in L:               return "🟢 Sano", "MANTENER", "Rotación normal"
    return "⚪ Otro", "REVISAR", "—"


# ════════════════════════════════════════════════════════════════════════════
# COMPRAS & CATÁLOGO — Dashboard de Compras Inteligente
#
# Dos endpoints sobre la misma fuente (matriz 04b filtrada a quiebres reales):
#   - GET /compras-catalogo        → JSON (dashboard web)
#   - GET /compras-catalogo/excel  → Excel descargable (gerencia)
#
# Ambos comparten el helper `_load_compras_catalogo` para garantizar que el
# dashboard y el Excel muestren EXACTAMENTE el mismo universo de SKUs.
# ════════════════════════════════════════════════════════════════════════════

async def _load_compras_catalogo(
    db: AsyncSession,
    company_id: int,
    office_id: int | None,
) -> tuple[list[str], list[dict], str | None, str]:
    """Ejecuta la matriz 04b y filtra a SKUs en quiebre real (Crítico + Alta).

    Returns: (cols, filtered_rows, suc_name, label_col)
    """
    from app.kawii_matrix import service

    result = await service.run_matrix(db, company_id, "04b", sucursal=None, limit=None)
    cols = result["columns"]
    rows = result["rows"]

    suc_name: str | None = None
    if office_id is not None:
        srow = (await db.execute(
            text("SELECT name FROM offices WHERE company_id = :cid AND bsale_office_id = :oid"),
            {"cid": company_id, "oid": office_id},
        )).first()
        suc_name = srow[0] if srow else None

    def _is_quiebre_real(label: str) -> bool:
        sev, _, _ = _classify_severidad_accion_causal(label or "")
        return sev in ("🔴 Crítico", "🟠 Alta")

    label_col = next((c for c in cols if "lasific" in c.lower()), None)
    if label_col is None:
        raise HTTPException(500, "La matriz no expuso columna de Clasificación.")

    filtered = []
    for r in rows:
        if suc_name and str(r.get("Sucursal") or "") != suc_name:
            continue
        if not _is_quiebre_real(str(r.get(label_col) or "")):
            continue
        filtered.append(r)

    return cols, filtered, suc_name, label_col


async def _margen_por_sku(
    db: AsyncSession,
    company_id: int,
    skus: list[str],
    office_id: int | None,
    days: int = 90,
) -> dict[str, dict]:
    """Calcula venta + costo + margen (S/ y %) por SKU sobre los últimos `days`.

    Margen = venta_neta - (cantidad × effective_cost). El effective_cost viene
    de variant_costs (promedio ponderado de las recepciones); si no hay costo,
    el SKU se omite de los totales para no inflar el margen artificialmente.

    Returns: {sku_display_code: {"venta_soles", "costo_soles", "margen_soles", "margen_pct", "cobertura_costo_pct"}}
    """
    if not skus:
        return {}

    company_config = await get_company(db, company_id)
    tipos_venta = company_config.get("tipos_venta") or DEFAULT_TIPOS_VENTA

    if office_id is not None:
        office_filter = "doc.bsale_office_id = :office_id"
        params: dict[str, Any] = {"skus": skus, "office_id": office_id, "days": days, "tipos_venta": tipos_venta, "cid": company_id}
    else:
        office_filter = _OFFICE_FILTER_DOC
        params = {"skus": skus, "days": days, "tipos_venta": tipos_venta, "cid": company_id}

    query = f"""
        SELECT v.display_code AS sku,
               SUM(dd.total_amount) FILTER (WHERE NOT dd.is_gratuity) AS venta_soles,
               SUM(dd.quantity * vc.effective_cost) FILTER (
                   WHERE NOT dd.is_gratuity AND vc.effective_cost IS NOT NULL
               ) AS costo_soles,
               SUM(dd.quantity) FILTER (WHERE NOT dd.is_gratuity) AS unds_total,
               SUM(dd.quantity) FILTER (
                   WHERE NOT dd.is_gratuity AND vc.effective_cost IS NOT NULL
               ) AS unds_con_costo
        FROM document_details dd
        JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
        JOIN variants v ON v.bsale_variant_id = dd.bsale_variant_id AND v.company_id = dd.company_id
        LEFT JOIN variant_costs vc ON vc.bsale_variant_id = dd.bsale_variant_id AND vc.company_id = dd.company_id
        WHERE dd.company_id = :cid
          AND (doc.emission_date AT TIME ZONE '{_TZ}')::DATE >= (CURRENT_DATE - :days * INTERVAL '1 day')
          AND COALESCE(doc.is_credit_note, FALSE) = FALSE
          AND doc.bsale_document_type_id = ANY(:tipos_venta)
          AND {office_filter}
          AND v.display_code = ANY(:skus)
        GROUP BY v.display_code
    """
    res = await db.execute(text(query), params)
    out: dict[str, dict] = {}
    for r in res.mappings().all():
        venta = float(r["venta_soles"] or 0)
        costo = float(r["costo_soles"] or 0)
        unds_total = float(r["unds_total"] or 0)
        unds_con_costo = float(r["unds_con_costo"] or 0)
        # Si el SKU tiene poca cobertura de costos, el margen no es confiable.
        cobertura_pct = (unds_con_costo / unds_total * 100) if unds_total else 0.0
        margen_soles = venta - costo if costo > 0 else None
        margen_pct = ((venta - costo) / venta * 100) if (venta and costo > 0) else None
        out[str(r["sku"])] = {
            "venta_soles": round(venta, 2),
            "costo_soles": round(costo, 2) if costo > 0 else None,
            "margen_soles": round(margen_soles, 2) if margen_soles is not None else None,
            "margen_pct": round(margen_pct, 1) if margen_pct is not None else None,
            "cobertura_costo_pct": round(cobertura_pct, 1),
        }
    return out


def _to_float(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# Cobertura objetivo para calcular "cantidad sugerida a ordenar".
# 30 días = 1 mes de stock. Coincide con COBERTURA_OBJETIVO_DIAS de la cascada.
_COBERTURA_OBJETIVO_DIAS = 30


@router.get("/compras-catalogo")
async def compras_catalogo_json(
    office_id: int | None = Query(None, description="Filtra por sucursal (vacío = todas)"),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Dashboard de Compras Inteligente: SKUs en quiebre real con métricas para decidir compras.

    Mismos SKUs que el Excel `/compras-catalogo/excel` (severidades 🔴 Crítico y 🟠 Alta),
    pero entregados como JSON estructurado para que el frontend pinte una página
    interactiva tipo dashboard.

    Respuesta:
      - `kpis`              : totales agregados (SKUs críticos, venta 90d en riesgo, unidades a reponer)
      - `por_departamento`  : agregados por departamento (para sidebar/distribución)
      - `por_accion`        : breakdown por acción sugerida (REPONER YA, PROMOCIONAR, etc.)
      - `skus`              : lista detallada (un objeto por SKU×Sucursal con todos los campos)

    NO incluye proveedor ni lead-time (no están en la BD; campos pendientes).
    """
    cid = company.company_id
    cols, filtered_rows, suc_name, label_col = await _load_compras_catalogo(db, cid, office_id)

    # 1) Construir lista de SKUs con campos derivados (severidad, acción, cantidad sugerida).
    skus: list[dict] = []
    for r in filtered_rows:
        clasif = str(r.get(label_col) or "")
        sev, accion, causal = _classify_severidad_accion_causal(clasif)

        velocidad_30d = _to_float(r.get("Vel últimos 30d"))
        velocidad_90d = _to_float(r.get("Velocidad (uds/día)"))
        stock_actual = _to_float(r.get("Stock Disp"))
        stock_almacen = _to_float(r.get("Stock Almacén"))

        # Cantidad sugerida = max(0, (velocidad_reciente × cobertura_objetivo) − stock_actual).
        # Usa velocidad_30d si existe (más reactiva); fallback a velocidad_90d.
        vel_referencia = velocidad_30d if velocidad_30d > 0 else velocidad_90d
        cantidad_sugerida = max(0, round(vel_referencia * _COBERTURA_OBJETIVO_DIAS) - int(stock_actual))

        skus.append({
            "sku": r.get("Código SKU"),
            "producto": r.get("Producto"),
            "sucursal": r.get("Sucursal"),
            "departamento": r.get("Departamento"),
            "categoria": r.get("Categoría"),
            "subcategoria": r.get("Subcategoría"),
            "clasificacion": clasif,
            "severidad": sev,
            "accion": accion,
            "causal": causal,
            "stock_disponible": stock_actual,
            "stock_almacen": stock_almacen,
            "velocidad_30d": velocidad_30d,
            "velocidad_90d": velocidad_90d,
            "unds_vend_90d": _to_float(r.get("Unds Vend (90d)")),
            "vendido_sku_soles": _to_float(r.get("Vendido SKU S/")),
            "cobertura_dias": r.get("Cobertura"),
            "dias_sin_vender": r.get("Días sin Vender"),
            "proyeccion_30d": _to_float(r.get("Proyección 30d")),
            "ultima_venta": r.get("Fecha Últ. Venta").isoformat()
                if hasattr(r.get("Fecha Últ. Venta"), "isoformat") else r.get("Fecha Últ. Venta"),
            "tendencia": r.get("Tendencia"),
            "cantidad_sugerida": cantidad_sugerida,
            # Placeholders (data no disponible en BD por ahora):
            "margen_pct": None,
            "margen_soles": None,
            "costo_soles": None,
        })

    # 2) Enriquecer con margen (query separada para evitar JOIN pesado en la matriz).
    sku_codes = [s["sku"] for s in skus if s["sku"]]
    margen_map = await _margen_por_sku(db, cid, sku_codes, office_id, days=90)
    for s in skus:
        m = margen_map.get(s["sku"], {})
        s["margen_pct"] = m.get("margen_pct")
        s["margen_soles"] = m.get("margen_soles")
        s["costo_soles"] = m.get("costo_soles")

    # 3) KPIs globales (todo el universo filtrado).
    total_critico = sum(1 for s in skus if s["severidad"] == "🔴 Crítico")
    total_alta = sum(1 for s in skus if s["severidad"] == "🟠 Alta")
    venta_en_riesgo = sum(s["vendido_sku_soles"] for s in skus)
    unidades_reponer = sum(s["cantidad_sugerida"] for s in skus)
    # Margen promedio ponderado por venta (solo SKUs con costo conocido).
    venta_con_costo = sum(s["vendido_sku_soles"] for s in skus if s["margen_pct"] is not None)
    margen_ponderado = (
        sum(s["vendido_sku_soles"] * s["margen_pct"] for s in skus if s["margen_pct"] is not None)
        / venta_con_costo
    ) if venta_con_costo else None

    kpis = {
        "skus_criticos_total": len(skus),
        "skus_critico": total_critico,
        "skus_alta": total_alta,
        "venta_90d_en_riesgo": round(venta_en_riesgo, 2),
        "unidades_a_reponer": int(unidades_reponer),
        "margen_promedio_pct": round(margen_ponderado, 1) if margen_ponderado is not None else None,
    }

    # 4) Agregados por departamento (para sidebar y distribución).
    por_dept_map: dict[str, dict] = {}
    for s in skus:
        dept = s["departamento"] or "Sin departamento"
        d = por_dept_map.setdefault(dept, {
            "departamento": dept,
            "skus_total": 0,
            "skus_critico": 0,
            "skus_alta": 0,
            "venta_soles": 0.0,
            "unidades_reponer": 0,
        })
        d["skus_total"] += 1
        if s["severidad"] == "🔴 Crítico":
            d["skus_critico"] += 1
        else:
            d["skus_alta"] += 1
        d["venta_soles"] += s["vendido_sku_soles"]
        d["unidades_reponer"] += s["cantidad_sugerida"]
    venta_total_dept = sum(d["venta_soles"] for d in por_dept_map.values()) or 1.0
    por_dept = []
    for d in por_dept_map.values():
        por_dept.append({
            **d,
            "venta_soles": round(d["venta_soles"], 2),
            "participacion_pct": round(d["venta_soles"] / venta_total_dept * 100, 1),
        })
    por_dept.sort(key=lambda x: x["venta_soles"], reverse=True)

    # 5) Breakdown por acción (REPONER YA, COMPRAR YA, PROMOCIONAR, etc.).
    por_accion_map: dict[str, int] = {}
    for s in skus:
        por_accion_map[s["accion"]] = por_accion_map.get(s["accion"], 0) + 1
    por_accion = [
        {"accion": k, "skus": v}
        for k, v in sorted(por_accion_map.items(), key=lambda x: x[1], reverse=True)
    ]

    return {
        "generado_at": datetime.utcnow().isoformat(),
        "office_id": office_id,
        "sucursal": suc_name,
        "cobertura_objetivo_dias": _COBERTURA_OBJETIVO_DIAS,
        "kpis": kpis,
        "por_departamento": por_dept,
        "por_accion": por_accion,
        "skus": skus,
    }


# ── Informe Compras & Catálogo (Excel separado, 2 pestañas accionables) ─────
@router.get("/compras-catalogo/excel", response_class=StreamingResponse)
async def compras_catalogo_excel(
    days: int = Query(30, ge=1, le=365, description="Ventana para 'Venta por categoría'"),
    office_id: int | None = Query(None, description="Filtra ambas pestañas por sucursal"),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
):
    """Excel de 2 pestañas centradas en acción comercial inmediata.

    Pestaña 1 — **SKUs en quiebre**: solo lo que genera VENTA PERDIDA HOY
    (severidades 🔴 Crítico y 🟠 Alta de la matriz 04). No incluye demanda extinta,
    bajo volumen, exceso ni saludables.

    Pestaña 2 — **Venta por categoría**: ranking de categorías con ticket promedio
    de la categoría (ventas / tickets).
    """
    from analytics.excel_compras_catalogo import build_compras_catalogo_workbook_jerarquico

    settings = get_settings()
    cid = company.company_id

    # 1) Universo de SKUs en quiebre real (compartido con el endpoint JSON).
    cols, filtered_rows, suc_name, _label_col = await _load_compras_catalogo(db, cid, office_id)

    # 2) Venta por categoría — para la pestaña final (tabla simple con ticket promedio).
    cats = await sales_by_category(days=days, office_id=office_id, company=company, db=db)
    total_v = sum(float(c.get("ventas") or 0) for c in cats) or 1.0
    for c in cats:
        c["participacion_pct"] = round(float(c.get("ventas") or 0) / total_v * 100, 1)

    # 3) Construir workbook: estructura ejecutiva (resumen + hojas por dept) + tab categorías.
    titulo = "Compras & Catálogo — Quiebres reales por Departamento"
    descripcion = (
        "Productos que generan venta perdida HOY (severidades 🔴 Crítico y 🟠 Alta). "
        "Agrupados por Departamento → Categoría → Subcategoría → SKU. La pestaña final "
        "muestra el ranking de Venta por Categoría con ticket promedio."
    )
    wb = build_compras_catalogo_workbook_jerarquico(
        cols=cols,
        filtered_rows=filtered_rows,
        venta_categoria=cats,
        titulo=titulo,
        descripcion=descripcion,
        sucursal_filtro=suc_name,
        brand_name=settings.BRAND_NAME,
    )

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fecha = datetime.now().strftime("%Y-%m-%d")
    filename = f"compras_catalogo_{fecha}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Quiebres-Reales": str(len(filtered_rows)),
            "X-Categorias": str(len(cats)),
        },
    )


# ── Informes Gerenciales (3 Excel separados, lenguaje simple) ────────────────
@router.get("/reporte-gerencial/{tipo}/excel", response_class=StreamingResponse)
async def reporte_gerencial_excel(
    tipo: str = Path(..., pattern="^(por-agotarse|estancados|rotacion)$"),
    office_id: int | None = Query(None, description="Filtra por sucursal (vacío = todas)"),
    dias_alerta: int = Query(10, ge=1, le=60, description="Umbral de cobertura de 'Por Agotarse'"),
    dias_estancado: int = Query(60, ge=30, le=365, description="Días sin venta para considerar estancado"),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
):
    """Excel gerenciales (para lectores no técnicos) sobre la Matriz Operativa (05).

    - **por-agotarse**: productos que venden y tienen < `dias_alerta` días de stock.
    - **estancados**: stock sin venta hace `dias_estancado`+ días, valorizado al
      costo (variant_costs.effective_cost).
    - **rotacion**: frecuencia de venta traducida a "1 cada X días" + 4 niveles.

    Cada informe abre con hoja 🎯 Resumen (KPIs + cómo leerlo) seguida de una
    pestaña por Departamento (mismo esquema que el Excel de Compras & Catálogo).
    """
    from analytics import excel_gerencial
    from app.kawii_matrix import service

    settings = get_settings()
    cid = company.company_id

    result = await service.run_matrix(db, cid, "05", limit=None)
    rows = result["rows"]

    for r in rows:
        r["Último Ingreso"] = r.get("Últ. Recepción") or r.get("1ª Recepción")

    # office_id → nombre de sucursal (la matriz trae el nombre, no el ID).
    suc_name: str | None = None
    if office_id is not None:
        srow = (await db.execute(
            text("SELECT name FROM offices WHERE company_id = :cid AND bsale_office_id = :oid"),
            {"cid": cid, "oid": office_id},
        )).first()
        suc_name = srow[0] if srow else None
        if suc_name:
            rows = [r for r in rows if str(r.get("Sucursal") or "") == suc_name]

    if tipo == "por-agotarse":
        wb = excel_gerencial.build_por_agotarse_workbook(
            rows, dias_alerta=dias_alerta, sucursal=suc_name,
            brand_name=settings.BRAND_NAME,
        )
        base = "por_agotarse"
    elif tipo == "estancados":
        res = await db.execute(text("""
            SELECT v.display_code AS sku, MAX(vc.effective_cost) AS costo
            FROM variants v
            JOIN variant_costs vc
              ON vc.company_id = v.company_id
             AND vc.bsale_variant_id = v.bsale_variant_id
            WHERE v.company_id = :cid AND vc.effective_cost IS NOT NULL
            GROUP BY v.display_code
        """), {"cid": cid})
        costos = {str(r["sku"]): float(r["costo"]) for r in res.mappings().all()}
        wb = excel_gerencial.build_estancados_workbook(
            rows, costos, dias_estancado=dias_estancado, sucursal=suc_name,
            brand_name=settings.BRAND_NAME,
        )
        base = "inventario_estancado"
    else:
        wb = excel_gerencial.build_rotacion_workbook(
            rows, sucursal=suc_name, brand_name=settings.BRAND_NAME,
        )
        base = "rotacion_productos"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fecha = datetime.now().strftime("%Y-%m-%d")
    filename = f"{base}_{fecha}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Informe Diario (Excel separado, mes en curso) ────────────────────────────
@router.get("/daily-report/excel", response_class=StreamingResponse)
async def daily_report_excel(
    office_id: int | None = Query(None, description="Filtra por sucursal (vacío = todas)"),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
):
    """Excel del Informe Diario — solo mes en curso, 3 hojas (Ticket promedio · Transacciones · Venta vs meta diaria).

    Restricción de mes: este endpoint **siempre** usa el mes actual. No acepta
    parámetro `month` (a propósito: gerencia pidió que solo se pueda generar
    el informe del mes en curso para evitar confusión con cierres anteriores).
    """
    from analytics.excel_daily import build_daily_workbook

    today = date.today()
    mes = _month_str(today)
    year, mon = today.year, today.month
    dias_del_mes = monthrange(year, mon)[1]
    first = date(year, mon, 1)
    dias_transcurridos = today.day

    cid = company.company_id
    company_config = await get_company(db, cid)
    tipos_venta = company_config.get("tipos_venta") or DEFAULT_TIPOS_VENTA

    # 1) Serie diaria desde el 1° del mes hasta hoy (días cerrados + hoy parcial).
    if office_id is not None:
        office_filter = "bsale_office_id = :office_id"
        params: dict[str, Any] = {"dfrom": first, "dto": today + timedelta(days=1), "office_id": office_id, "tipos_venta": tipos_venta, "cid": cid}
        scope = [office_id]
    else:
        office_filter = _OFFICE_FILTER_PLAIN
        params = {"dfrom": first, "dto": today + timedelta(days=1), "tipos_venta": tipos_venta, "cid": cid}
        scope = list(OFFICE_IDS)

    # Serie GLOBAL (alimenta las pestañas Ticket promedio + Transacciones).
    serie_q = f"""
        SELECT (emission_date AT TIME ZONE '{_TZ}')::DATE AS dia,
               ROUND(SUM(total_amount)::numeric, 2) AS ventas,
               COUNT(*) AS tickets,
               ROUND(AVG(CASE WHEN total_amount > 0 THEN total_amount END)::numeric, 2) AS ticket_promedio
        FROM documents
        WHERE company_id = :cid
          AND (emission_date AT TIME ZONE '{_TZ}')::DATE >= :dfrom
          AND (emission_date AT TIME ZONE '{_TZ}')::DATE < :dto
          AND COALESCE(is_credit_note, FALSE) = FALSE
          AND bsale_document_type_id = ANY(:tipos_venta)
          AND {office_filter}
        GROUP BY dia
        ORDER BY dia
    """
    serie_rows = [dict(r) for r in (await db.execute(text(serie_q), params)).mappings().all()]
    serie_by_date = {r["dia"]: r for r in serie_rows}

    # Serie POR SUCURSAL (alimenta la pestaña multi-bloque 'Venta vs meta').
    serie_office_q = f"""
        SELECT bsale_office_id AS office_id,
               (emission_date AT TIME ZONE '{_TZ}')::DATE AS dia,
               ROUND(SUM(total_amount)::numeric, 2) AS ventas
        FROM documents
        WHERE company_id = :cid
          AND (emission_date AT TIME ZONE '{_TZ}')::DATE >= :dfrom
          AND (emission_date AT TIME ZONE '{_TZ}')::DATE < :dto
          AND COALESCE(is_credit_note, FALSE) = FALSE
          AND bsale_office_id = ANY(:scope_ids)
        GROUP BY office_id, dia
    """
    serie_office_rows = (await db.execute(
        text(serie_office_q),
        {"cid": cid, "dfrom": first, "dto": today + timedelta(days=1), "scope_ids": list(scope)},
    )).mappings().all()
    # Index: (office_id, fecha) -> venta_dia
    venta_by_office_day: dict[tuple[int, date], float] = {
        (int(r["office_id"]), r["dia"]): float(r["ventas"] or 0) for r in serie_office_rows
    }

    # Nombres de sucursales (para mostrar en los bloques).
    off_rows = (await db.execute(
        text("SELECT bsale_office_id, name FROM offices WHERE company_id = :cid AND bsale_office_id = ANY(:ids)"),
        {"cid": cid, "ids": list(scope)},
    )).mappings().all()
    office_names = {int(r["bsale_office_id"]): r["name"] for r in off_rows}

    # 2) Meta del mes (manual cargada con PUT /analytics/goals).
    goals = await get_goals(db, cid)
    month_goals, fuente_meta = goal_for_month(goals, mes)

    def _meta_for_scope() -> float | None:
        if office_id is not None:
            v = month_goals.get(str(office_id))
        else:
            v = month_goals.get("global")
            if v is None:  # fallback: suma metas por sucursal
                sub = [month_goals.get(str(oid)) for oid in scope]
                vals = [float(x) for x in sub if x is not None]
                v = sum(vals) if vals else None
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    meta_total = _meta_for_scope()
    meta_diaria_const = (meta_total / dias_del_mes) if (meta_total and dias_del_mes) else None

    # 3) Armar serie diaria completa + meta diario.
    serie = []
    meta_diario = []
    acum_venta = 0.0
    for day_n in range(1, dias_del_mes + 1):
        fecha = date(year, mon, day_n)
        is_futuro = fecha > today
        row = serie_by_date.get(fecha)
        if row is not None:
            venta_dia = float(row["ventas"] or 0)
            tickets = int(row["tickets"] or 0)
            ticket_prom = float(row["ticket_promedio"] or 0)
            acum_venta += venta_dia
        else:
            venta_dia, tickets, ticket_prom = (None, None, None) if is_futuro else (0.0, 0, 0.0)
        serie.append({
            "fecha": fecha.isoformat(),
            "ventas": venta_dia,
            "tickets": tickets,
            "ticket_promedio": ticket_prom,
            "estado": "futuro" if is_futuro else ("hoy" if fecha == today else "cerrado"),
        })
        meta_acum = (meta_diaria_const * day_n) if meta_diaria_const is not None else None
        if is_futuro:
            avance = None
            gap_acum = None
            ritmo_pendiente = None
            venta_acum_para_meta = None
        else:
            venta_acum_para_meta = round(acum_venta, 2)
            avance = round(acum_venta / meta_total * 100, 1) if meta_total else None
            gap_acum = round(acum_venta - meta_acum, 2) if meta_acum is not None else None
            # Ritmo restante = (meta - acum) / días restantes (excluido hoy si ya cerró)
            dias_restantes = max(0, dias_del_mes - day_n)
            if meta_total and dias_restantes > 0:
                pendiente = meta_total - acum_venta
                ritmo_pendiente = round(max(0.0, pendiente / dias_restantes), 2)
            else:
                ritmo_pendiente = None
        meta_diario.append({
            "fecha": fecha.isoformat(),
            "venta_dia": round(venta_dia, 2) if isinstance(venta_dia, (int, float)) else None,
            "venta_acumulada": venta_acum_para_meta,
            "meta_diaria": round(meta_diaria_const, 2) if meta_diaria_const is not None else None,
            "meta_acumulada": round(meta_acum, 2) if meta_acum is not None else None,
            "avance_pct": avance,
            "gap_acumulado": gap_acum,
            "ritmo_necesario_pendiente": ritmo_pendiente,
            "estado": "futuro" if is_futuro else ("hoy" if fecha == today else "cerrado"),
        })

    # Datos POR SUCURSAL para los bloques de la pestaña 'Venta vs meta'.
    def _meta_por_sucursal(oid: int) -> float | None:
        v = month_goals.get(str(oid))
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    sucursales: list[dict] = []
    for oid in scope:
        meta_suc = _meta_por_sucursal(oid)
        meta_suc_diaria = (meta_suc / dias_del_mes) if (meta_suc and dias_del_mes) else None
        venta_acum_suc = sum(
            venta_by_office_day.get((oid, date(year, mon, d)), 0.0)
            for d in range(1, dias_transcurridos + 1)
        )
        # Construir la serie diaria propia de la sucursal (incluye días futuros vacíos).
        serie_suc = []
        acum = 0.0
        for d in range(1, dias_del_mes + 1):
            fecha = date(year, mon, d)
            is_fut = fecha > today
            v_dia = venta_by_office_day.get((oid, fecha))
            if v_dia is None and not is_fut:
                v_dia = 0.0
            if v_dia is not None and not is_fut:
                acum += v_dia
            meta_acum_dia = (meta_suc_diaria * d) if meta_suc_diaria is not None else None
            serie_suc.append({
                "fecha": fecha.isoformat(),
                "dia": d,
                "venta_dia": round(v_dia, 2) if v_dia is not None else None,
                "venta_acumulada": round(acum, 2) if not is_fut else None,
                "meta_diaria": round(meta_suc_diaria, 2) if meta_suc_diaria is not None else None,
                "meta_acumulada": round(meta_acum_dia, 2) if meta_acum_dia is not None else None,
                "estado": "futuro" if is_fut else ("hoy" if fecha == today else "cerrado"),
            })
        # Indicadores derivados (corte al día actual)
        ritmo_actual = (venta_acum_suc / dias_transcurridos) if dias_transcurridos else None
        ritmo_req = (
            (meta_suc - venta_acum_suc) / (dias_del_mes - dias_transcurridos)
            if (meta_suc and dias_del_mes > dias_transcurridos)
            else None
        )
        proyeccion = (ritmo_actual * dias_del_mes) if ritmo_actual is not None else None
        meta_acum_hoy = (meta_suc_diaria * dias_transcurridos) if meta_suc_diaria is not None else None
        meta_50 = (meta_suc / 2) if meta_suc else None
        gap_vs_50 = (venta_acum_suc - meta_50) if meta_50 is not None else None
        avance_pct = (venta_acum_suc / meta_suc * 100) if meta_suc else None
        sucursales.append({
            "office_id": oid,
            "nombre": office_names.get(oid, str(oid)),
            "meta_mensual": meta_suc,
            "meta_diaria": meta_suc_diaria,
            "meta_50pct": meta_50,
            "meta_acum_hoy": meta_acum_hoy,
            "venta_acumulada": round(venta_acum_suc, 2),
            "avance_pct": round(avance_pct, 1) if avance_pct is not None else None,
            "gap_vs_50pct": round(gap_vs_50, 2) if gap_vs_50 is not None else None,
            "ritmo_actual": round(ritmo_actual, 2) if ritmo_actual is not None else None,
            "ritmo_requerido": round(ritmo_req, 2) if ritmo_req is not None else None,
            "proyeccion": round(proyeccion, 2) if proyeccion is not None else None,
            "serie": serie_suc,
        })

    daily = {
        "month": mes,
        "office_id": office_id,
        "scope": scope,
        "dias_del_mes": dias_del_mes,
        "dias_transcurridos": dias_transcurridos,
        "meta_mensual": meta_total,
        "meta_diaria": meta_diaria_const,
        "meta_source": fuente_meta,
        "serie": serie,
        "meta_diario": meta_diario,
        "sucursales": sucursales,
    }

    settings = get_settings()
    wb = build_daily_workbook(daily, brand_name=settings.BRAND_NAME)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fecha_str = today.strftime("%Y-%m-%d")
    filename = f"informe_diario_{mes}_al_{fecha_str}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Mes": mes,
            "X-Dias-Transcurridos": str(dias_transcurridos),
        },
    )


# NOTA (2026-06-18): se removió el endpoint `GET /weekly-board/excel` y el builder
# `analytics/excel_board.py`. El "Excel Semanal" fue reemplazado por los dos Excels
# focalizados:
#   - /analytics/compras-catalogo/excel  (quiebres + venta por categoría, jerárquico)
#   - /analytics/daily-report/excel      (ticket promedio, transacciones, venta vs meta — mes en curso)
# El endpoint JSON `/weekly-board` sigue activo: lo consume el tablero web.


# ════════════════════════════════════════════════════════════════════════════
# ROTACIÓN HISTÓRICA — Ventana arbitraria (año 2024, Q4, etc.)
#
# Endpoint: GET /analytics/rotacion-historica?from=YYYY-MM-DD&to=YYYY-MM-DD&office_id=N
#
# Pregunta de negocio: "¿Qué productos tuvieron alta rotación en X período?".
# Usa el SQL 04h_rotacion_historica.sql, que es una versión PARAMETRIZABLE de la
# matriz 04b: misma estructura jerárquica + totales en S/ + % participación,
# pero con cascada de clasificación adaptada (Pareto ABC + frecuencia +
# tendencia intra-ventana) porque las 38 reglas del 04b dependen del PRESENTE.
# ════════════════════════════════════════════════════════════════════════════

from pathlib import Path as _Path

_ROTACION_HISTORICA_SQL_PATH = (
    _Path(__file__).parent.parent / "kawii_matrix" / "sql" / "04h_rotacion_historica.sql"
)


async def _run_rotacion_historica(
    db: AsyncSession,
    company_id: int,
    fecha_from: date,
    fecha_to: date,
) -> list[dict]:
    """Ejecuta el SQL 04h y devuelve filas como dicts (sin postprocesar)."""
    from harvester.config import OFFICES_TIENDA, TIPOS_VENTA, TIPOS_DEVOLUCION
    from app.routers.config_admin import get_exclusions

    sql = _ROTACION_HISTORICA_SQL_PATH.read_text(encoding="utf-8")

    excl = await get_exclusions(db, company_id)
    params = {
        "company_id": company_id,
        "fecha_from": fecha_from,
        "fecha_to": fecha_to,
        "sucursales_objetivo": OFFICES_TIENDA,
        "tipos_venta": TIPOS_VENTA,
        "tipos_devolucion": TIPOS_DEVOLUCION,
        "excluded_departments": excl["departments"],
        "excluded_categories": excl["categories"],
    }

    # Performance: el SQL usa varios CTEs + window functions. Forzar hash joins
    # baja el tiempo de forma consistente (mismo truco que las matrices).
    await db.execute(text("SET LOCAL enable_nestloop = off"))
    await db.execute(text("SET LOCAL timezone = 'UTC'"))

    res = await db.execute(text(sql), params)
    return [dict(r) for r in res.mappings().all()]


def _to_float_safe(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


@router.get("/rotacion-historica")
async def rotacion_historica(
    fecha_from: date = Query(..., alias="from", description="Inicio de la ventana (inclusive, YYYY-MM-DD)"),
    fecha_to: date = Query(..., alias="to", description="Fin de la ventana (inclusive, YYYY-MM-DD)"),
    office_id: int | None = Query(None, description="Filtra por sucursal (vacío = todas las tiendas)"),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Rotación de productos en una ventana histórica arbitraria.

    Responde "¿qué se vendió más en 2024?", "Top productos del Q4", "Alta rotación
    en abril-junio del año pasado", etc. Usa el SQL 04h_rotacion_historica.sql
    (variante parametrizable del 04b).

    Respuesta:
      - `meta`              : ventana solicitada y conteos
      - `kpis`              : totales agregados de la ventana
      - `por_departamento`  : ranking por departamento (para sidebar)
      - `por_pareto`        : breakdown ABC (cuántos SKUs en A/B/C)
      - `por_clasificacion` : breakdown por bucket de clasificación
      - `skus`              : lista detallada (un objeto por SKU×Sucursal)
    """
    if fecha_to < fecha_from:
        raise HTTPException(400, "`to` debe ser >= `from`")
    if (fecha_to - fecha_from).days > 730:
        raise HTTPException(400, "Ventana máxima 730 días (2 años) para evitar timeouts")

    cid = company.company_id
    rows = await _run_rotacion_historica(db, cid, fecha_from, fecha_to)

    # Resolver nombre de sucursal si se filtra por office_id (el SQL devuelve nombre, no id).
    suc_name: str | None = None
    if office_id is not None:
        srow = (await db.execute(
            text("SELECT name FROM offices WHERE company_id = :cid AND bsale_office_id = :oid"),
            {"cid": cid, "oid": office_id},
        )).first()
        suc_name = srow[0] if srow else None
        rows = [r for r in rows if r.get("Sucursal") == suc_name]

    # Transformar a shape JSON estable (los nombres de columnas SQL tienen tildes/espacios).
    skus: list[dict] = []
    for r in rows:
        skus.append({
            "sku": r.get("Código SKU"),
            "producto": r.get("Producto"),
            "sucursal": r.get("Sucursal"),
            "departamento": r.get("Departamento"),
            "categoria": r.get("Categoría"),
            "subcategoria": r.get("Subcategoría"),
            "unds_vendidas": _to_float_safe(r.get("Unds Vendidas")),
            "vendido_sku_soles": _to_float_safe(r.get("Vendido SKU S/")),
            "vendido_subcat_soles": _to_float_safe(r.get("Vendido Subcat S/")),
            "vendido_cat_soles": _to_float_safe(r.get("Vendido Cat S/")),
            "vendido_depto_soles": _to_float_safe(r.get("Vendido Depto S/")),
            "pct_en_subcat": _to_float_safe(r.get("% S/ en Subcat")),
            "pct_en_cat": _to_float_safe(r.get("% S/ en Cat")),
            "pct_en_depto": _to_float_safe(r.get("% S/ en Depto")),
            "velocidad_uds_dia": _to_float_safe(r.get("Velocidad (uds/día)")),
            "dias_con_venta": int(_to_float_safe(r.get("Días con Venta"))),
            "pct_frecuencia": _to_float_safe(r.get("% Frecuencia")),
            "tendencia": r.get("Tendencia"),
            "unds_primera_mitad": _to_float_safe(r.get("Unds 1ª Mitad")),
            "unds_segunda_mitad": _to_float_safe(r.get("Unds 2ª Mitad")),
            "rank_sucursal": int(_to_float_safe(r.get("Rank Sucursal"))),
            "pct_acum": _to_float_safe(r.get("% Acum")),
            "pareto": r.get("Pareto"),
            "clasificacion": r.get("Clasificación"),
            "primera_venta": r.get("1ª Venta en ventana").isoformat()
                if hasattr(r.get("1ª Venta en ventana"), "isoformat") else r.get("1ª Venta en ventana"),
            "ultima_venta": r.get("Últ. Venta en ventana").isoformat()
                if hasattr(r.get("Últ. Venta en ventana"), "isoformat") else r.get("Últ. Venta en ventana"),
        })

    # KPIs globales sobre la ventana.
    total_skus = len(skus)
    total_unds = sum(s["unds_vendidas"] for s in skus)
    total_soles = sum(s["vendido_sku_soles"] for s in skus)
    skus_a = sum(1 for s in skus if s["pareto"] == "A")
    skus_b = sum(1 for s in skus if s["pareto"] == "B")
    skus_c = sum(1 for s in skus if s["pareto"] == "C")

    # Por departamento (agregados).
    por_dept_map: dict[str, dict] = {}
    for s in skus:
        dept = s["departamento"] or "Sin departamento"
        d = por_dept_map.setdefault(dept, {
            "departamento": dept,
            "skus_total": 0,
            "skus_pareto_a": 0,
            "venta_soles": 0.0,
            "unds_vendidas": 0.0,
        })
        d["skus_total"] += 1
        if s["pareto"] == "A":
            d["skus_pareto_a"] += 1
        d["venta_soles"] += s["vendido_sku_soles"]
        d["unds_vendidas"] += s["unds_vendidas"]
    venta_total_dept = sum(d["venta_soles"] for d in por_dept_map.values()) or 1.0
    por_dept = [
        {
            **d,
            "venta_soles": round(d["venta_soles"], 2),
            "unds_vendidas": int(d["unds_vendidas"]),
            "participacion_pct": round(d["venta_soles"] / venta_total_dept * 100, 1),
        }
        for d in por_dept_map.values()
    ]
    por_dept.sort(key=lambda x: x["venta_soles"], reverse=True)

    # Breakdown por clasificación (ej. "🔥 Alta rotación constante": 23).
    por_clasif_map: dict[str, int] = {}
    for s in skus:
        clasif = s["clasificacion"] or "—"
        por_clasif_map[clasif] = por_clasif_map.get(clasif, 0) + 1
    por_clasificacion = [
        {"clasificacion": k, "skus": v}
        for k, v in sorted(por_clasif_map.items(), key=lambda x: x[1], reverse=True)
    ]

    return {
        "meta": {
            "from": fecha_from.isoformat(),
            "to": fecha_to.isoformat(),
            "dias_ventana": (fecha_to - fecha_from).days + 1,
            "office_id": office_id,
            "sucursal": suc_name,
            "generado_at": datetime.utcnow().isoformat(),
        },
        "kpis": {
            "skus_con_venta": total_skus,
            "unds_vendidas": int(total_unds),
            "venta_soles": round(total_soles, 2),
            "skus_pareto_a": skus_a,
            "skus_pareto_b": skus_b,
            "skus_pareto_c": skus_c,
        },
        "por_departamento": por_dept,
        "por_pareto": [
            {"pareto": "A", "skus": skus_a, "etiqueta": "Top 80% (Pareto A)"},
            {"pareto": "B", "skus": skus_b, "etiqueta": "Siguiente 15% (Pareto B)"},
            {"pareto": "C", "skus": skus_c, "etiqueta": "Cola larga (Pareto C)"},
        ],
        "por_clasificacion": por_clasificacion,
        "skus": skus,
    }
