"""Vista 2 — Diagnóstico ejecutivo de ventas (¿por qué vendo menos hoy?).

Endpoint único `GET /diagnosis` que devuelve un payload completo con:

  1. meta              : ventanas comparadas, alertas de feriados, estado del sync
  2. kpis              : ventas/tickets/unds/margen vs 3 ventanas (semana, 4 sem, YoY)
  3. veredicto         : PROBLEMA_REAL / BAJÓN_ESTACIONAL / CRECIENDO / ESTANCAMIENTO
  4. anatomia          : descomposición log de ventas en (tickets × canasta × precio)
  5. descomposicion    : suma al delta — por sucursal/categoría/DoW/franja_horaria/vendedor
  6. factores          : lentes paralelas — quiebres, descuentos, devoluciones, regalos
  7. ganadores_perdedores : top SKUs que subieron/cayeron, nuevos, enfriados
  8. huecos_yoy        : sub-categorías que se enfriaron vs hace 12 meses
  9. resumen           : narrativa auto-generada en español

Convención de fechas:
- `emission_date` está a medianoche UTC = fecha calendario del documento.
- `generation_date` sí tiene la hora real; se convierte a Lima para análisis horario.

Ventanas:
- current : [today − days, today)             — excluye HOY (parcial)
- week    : current corrido `shift` días atrás, donde shift = ceil(days/7)·7
            (garantiza alineación día-de-semana)
- base_4w : [cur_from − 28, cur_from)         — siempre 28 días; promedio diario
            × `days` da la predicción "lo que normalmente vendés en N días"
- yoy     : current corrido 364 días atrás    — 52 semanas exactas, DoW alineado
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from analytics.core.config import OFFICE_IDS
from app.auth import CurrentCompany, get_current_company
from app.calendar.holidays_pe import holidays_in_range, is_holiday
from app.database import get_db
from app.routers.config_admin import _read_cfg_names, get_company
from harvester.config import TIPOS_VENTA as DEFAULT_TIPOS_VENTA


router = APIRouter(
    prefix="/diagnosis",
    tags=["diagnosis"],
    dependencies=[Depends(get_current_company)],
)


_OFFICE_IDS_SQL  = ", ".join(str(i) for i in OFFICE_IDS)
_OFFICE_PLAIN    = f"bsale_office_id IN ({_OFFICE_IDS_SQL})"
_OFFICE_DOC      = f"doc.bsale_office_id IN ({_OFFICE_IDS_SQL})"
_TZ_DATE         = "UTC"           # emission_date está a medianoche UTC
_TZ_HOUR         = "America/Lima"  # generation_date sí tiene hora real

# Franjas horarias por defecto (Lima). El pico vespertino 17-19h es el más
# importante en Kawii (1264 + 1522 tickets en 30 días según data real).
_HOUR_BUCKETS = [
    ("08-12h",  8, 12),
    ("12-15h", 12, 15),
    ("15-17h", 15, 17),
    ("17-19h", 17, 19),
    ("19-22h", 19, 22),
]


# ════════════════════════════════════════════════════════════════════════════
# Exclusiones (estacionales/descatalogados configurados en app_config)
# ════════════════════════════════════════════════════════════════════════════

async def _load_exclusions(db: AsyncSession, company_id: int) -> dict[str, list[str]]:
    """Lee los nombres de departamentos/categorías marcados como excluidos en la empresa.

    Se guardan por NOMBRE en app_config (no IDs) — sobrevive a re-seeds de la
    taxonomía. Filtramos por nombre en las queries (vpf.department / vpf.category).
    """
    depts = await _read_cfg_names(db, company_id, "excluded_departments")
    cats  = await _read_cfg_names(db, company_id, "excluded_categories")
    return {"depts": depts, "cats": cats}


def _excl_filter(prefix: str, excl: dict, params: dict) -> str:
    """Devuelve el fragmento WHERE para excluir estacionales/categorías marcadas.

    Args:
      prefix: alias del view v_products_full en la query (típicamente 'vpf').
      excl: dict {"depts": [...], "cats": [...]} retornado por _load_exclusions.
      params: dict de parámetros — se mutan con :_excl_depts / :_excl_cats si aplica.

    Returns:
      String SQL listo para concatenar (puede ser vacío si no hay nada excluido).
    """
    out = []
    if excl.get("depts"):
        params["_excl_depts"] = excl["depts"]
        out.append(f"AND COALESCE({prefix}.department, '') <> ALL(:_excl_depts)")
    if excl.get("cats"):
        params["_excl_cats"] = excl["cats"]
        out.append(f"AND COALESCE({prefix}.category, '') <> ALL(:_excl_cats)")
    return "\n          ".join(out)


# ════════════════════════════════════════════════════════════════════════════
# Period resolution
# ════════════════════════════════════════════════════════════════════════════

def _resolve_periods(days: int) -> dict[str, dict]:
    """Calcula las 4 ventanas del diagnóstico.

    `shift` para la comparación vs semana anterior siempre es múltiplo de 7
    (ceil(days/7)·7). Esto garantiza que un sábado se compare con un sábado,
    aun si `days` no es múltiplo de 7.
    """
    today = date.today()
    cur_to   = today
    cur_from = today - timedelta(days=days)

    shift = math.ceil(days / 7) * 7
    week_from = cur_from - timedelta(days=shift)
    week_to   = week_from + timedelta(days=days)

    base_from = cur_from - timedelta(days=28)
    base_to   = cur_from

    yoy_from = cur_from - timedelta(days=364)
    yoy_to   = yoy_from + timedelta(days=days)

    return {
        "current": {"from": cur_from, "to": cur_to, "dias": days, "label": "actual"},
        "week":    {"from": week_from, "to": week_to, "dias": days, "shift": shift, "label": "semana_anterior"},
        "base_4w": {"from": base_from, "to": base_to, "dias": 28, "label": "promedio_4_semanas"},
        "yoy":     {"from": yoy_from, "to": yoy_to, "dias": days, "label": "ano_anterior"},
    }


def _office_filters(office_id: Optional[int]) -> tuple[str, str, dict]:
    """(filter_plain_sql, filter_doc_sql, extra_params)."""
    if office_id is not None:
        return (
            "bsale_office_id = :office_id",
            "doc.bsale_office_id = :office_id",
            {"office_id": office_id},
        )
    return (_OFFICE_PLAIN, _OFFICE_DOC, {})


# ════════════════════════════════════════════════════════════════════════════
# Period KPIs (métricas agregadas de una ventana)
# ════════════════════════════════════════════════════════════════════════════

async def _period_kpis(
    db: AsyncSession,
    company_id: int,
    dfrom: date,
    dto: date,
    office_id: Optional[int],
    tipos_venta: list[int],
    excl: Optional[dict] = None,
) -> dict[str, Any]:
    """Métricas agregadas del período [dfrom, dto). Devuelve TOTAL + RECURRENTE.

    El bloque "recurrente" excluye los departamentos/categorías marcados en
    app_config como estacionales — útil para drivers y decisiones operativas
    sin que un Día del Padre o vuelta-al-cole contamine el delta.

    Si `excl` es None, recurrente == total (no hay nada excluido).
    """
    flt_plain, flt_doc, extra = _office_filters(office_id)
    excl = excl or {"depts": [], "cats": []}

    # Query 1: tickets+ventas total (sin filtro estacional) — directo a documents
    params_doc = {"dfrom": dfrom, "dto": dto, "tipos_venta": tipos_venta, "cid": company_id, **extra}
    q1 = f"""
        SELECT COALESCE(SUM(total_amount), 0) AS ventas,
               COUNT(*)                       AS tickets
        FROM documents
        WHERE company_id = :cid
          AND (emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= :dfrom
          AND (emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <  :dto
          AND COALESCE(is_credit_note, FALSE) = FALSE
          AND bsale_document_type_id = ANY(:tipos_venta)
          AND {flt_plain}
    """
    r1 = (await db.execute(text(q1), params_doc)).mappings().one()

    # Query 2: ventas/tickets RECURRENTES + unds + costo + descuento (vía líneas).
    # Joinea vpf siempre — único punto de cálculo para ambos números.
    params_dd: dict[str, Any] = {"dfrom": dfrom, "dto": dto, "tipos_venta": tipos_venta, "cid": company_id, **extra}
    excl_clause = _excl_filter("vpf", excl, params_dd)
    recurrent_filter_inline = ""
    if excl_clause:
        # Mismo filtro pero en línea (no como WHERE — para FILTER de los SUM/COUNT)
        terms = []
        if excl.get("depts"):
            terms.append("COALESCE(vpf.department, '') <> ALL(:_excl_depts)")
        if excl.get("cats"):
            terms.append("COALESCE(vpf.category, '') <> ALL(:_excl_cats)")
        recurrent_filter_inline = " AND " + " AND ".join(terms)

    q2 = f"""
        SELECT
          -- TOTALES
          COALESCE(SUM(dd.total_amount) FILTER (WHERE NOT dd.is_gratuity), 0)                   AS ventas_total,
          COALESCE(SUM(dd.total_amount)
                   FILTER (WHERE NOT dd.is_gratuity AND COALESCE(vco.effective_cost, vc.effective_cost) > 0), 0)              AS ventas_con_costo_total,
          COALESCE(SUM(dd.quantity)     FILTER (WHERE NOT dd.is_gratuity), 0)                   AS unds_total,
          COALESCE(COUNT(DISTINCT doc.bsale_document_id) FILTER (WHERE NOT dd.is_gratuity), 0)  AS tickets_total,
          COALESCE(SUM(dd.quantity * COALESCE(vco.effective_cost, vc.effective_cost))
                   FILTER (WHERE NOT dd.is_gratuity AND COALESCE(vco.effective_cost, vc.effective_cost) > 0), 0)              AS costo_total,
          COALESCE(SUM(dd.net_discount) FILTER (WHERE NOT dd.is_gratuity), 0)                   AS descuento_total,
          COALESCE(SUM(dd.net_amount + dd.net_discount) FILTER (WHERE NOT dd.is_gratuity), 0)   AS bruto_total,
          -- RECURRENTES (excluye estacionales/categorías marcadas)
          COALESCE(SUM(dd.total_amount) FILTER (WHERE NOT dd.is_gratuity {recurrent_filter_inline}), 0)  AS ventas_rec,
          COALESCE(SUM(dd.total_amount)
                   FILTER (WHERE NOT dd.is_gratuity AND COALESCE(vco.effective_cost, vc.effective_cost) > 0 {recurrent_filter_inline}), 0) AS ventas_con_costo_rec,
          COALESCE(SUM(dd.quantity)     FILTER (WHERE NOT dd.is_gratuity {recurrent_filter_inline}), 0)  AS unds_rec,
          COALESCE(COUNT(DISTINCT doc.bsale_document_id) FILTER (WHERE NOT dd.is_gratuity {recurrent_filter_inline}), 0) AS tickets_rec,
          COALESCE(SUM(dd.quantity * COALESCE(vco.effective_cost, vc.effective_cost))
                   FILTER (WHERE NOT dd.is_gratuity AND COALESCE(vco.effective_cost, vc.effective_cost) > 0 {recurrent_filter_inline}), 0) AS costo_rec,
          COALESCE(SUM(dd.net_discount) FILTER (WHERE NOT dd.is_gratuity {recurrent_filter_inline}), 0)  AS descuento_rec,
          COALESCE(SUM(dd.net_amount + dd.net_discount) FILTER (WHERE NOT dd.is_gratuity {recurrent_filter_inline}), 0) AS bruto_rec,
          -- Regalos/gratuidades (no se separan total/recurrente — son globales)
          COALESCE(COUNT(*) FILTER (WHERE dd.is_gratuity), 0)            AS lineas_regalo,
          COALESCE(SUM(dd.total_amount) FILTER (WHERE dd.is_gratuity), 0) AS monto_regalo,
          COALESCE(SUM(dd.quantity)     FILTER (WHERE dd.is_gratuity), 0) AS unds_regalo
        FROM document_details dd
        JOIN documents doc       ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
        JOIN variants v          ON v.bsale_variant_id    = dd.bsale_variant_id  AND v.company_id   = dd.company_id
        LEFT JOIN v_products_full vpf ON vpf.bsale_product_id  = v.bsale_product_id AND vpf.company_id = v.company_id
        LEFT JOIN variant_costs vc   ON vc.bsale_variant_id = dd.bsale_variant_id  AND vc.company_id  = dd.company_id
        LEFT JOIN variant_costs_by_office vco ON vco.bsale_variant_id = dd.bsale_variant_id AND vco.company_id = dd.company_id AND vco.bsale_office_id = doc.bsale_office_id
        WHERE dd.company_id = :cid
          AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= :dfrom
          AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <  :dto
          AND COALESCE(doc.is_credit_note, FALSE) = FALSE
          AND doc.bsale_document_type_id = ANY(:tipos_venta)
          AND {flt_doc}
    """
    r2 = (await db.execute(text(q2), params_dd)).mappings().one()

    # Tickets viene de query 1 (documents) — los recurrentes son los de query 2 (líneas)
    ventas_total  = float(r1["ventas"])
    tickets_total = int(r1["tickets"])

    def _block(prefix: str) -> dict[str, Any]:
        v        = float(r2[f"ventas_{prefix}"])
        v_costo  = float(r2[f"ventas_con_costo_{prefix}"])
        u        = float(r2[f"unds_{prefix}"])
        t        = int(r2[f"tickets_{prefix}"])
        c        = float(r2[f"costo_{prefix}"])
        dto_     = float(r2[f"descuento_{prefix}"])
        br       = float(r2[f"bruto_{prefix}"])
        # Margen sobre la fracción CON costo (sin contaminar con SKUs sin costo).
        # Si la cobertura es alta, margen_pct ≈ realidad. Si es baja, igual el % es
        # representativo de los SKUs con costo cargado.
        margen_pct    = ((v_costo - c) / v_costo * 100) if v_costo else 0.0
        cobertura_pct = (v_costo / v * 100) if v else 0.0
        return {
            "ventas":              round(v, 2),
            "ventas_con_costo":    round(v_costo, 2),
            "tickets":             t,
            "unds":                round(u, 2),
            "unds_per_ticket":     round((u / t) if t else 0.0, 3),
            "monto_per_und":       round((v / u) if u else 0.0, 2),
            "ticket_promedio":     round((v / t) if t else 0.0, 2),
            "margen_pct":          round(margen_pct, 2),
            "margen_monto":        round(v_costo - c, 2),
            "cobertura_costos_pct": round(cobertura_pct, 2),
            "descuento_monto":     round(dto_, 2),
            "descuento_pct":       round((dto_ / br * 100) if br else 0.0, 2),
        }

    # El "total" auténtico viene de documents (más exacto para tickets);
    # las ventas matchean (la suma de líneas no-gratuitas ≈ documents.total_amount).
    total = _block("total")
    total["ventas"]  = round(ventas_total, 2)   # usa el de documents (más confiable)
    total["tickets"] = tickets_total
    # Recálculo ticket_promedio con los números de documents
    total["ticket_promedio"] = round((ventas_total / tickets_total) if tickets_total else 0.0, 2)
    total["unds_per_ticket"] = round((total["unds"] / tickets_total) if tickets_total else 0.0, 3)

    recurrente = _block("rec")

    return {
        # Por compatibilidad con código viejo, expongo el total al nivel raíz
        **total,
        # Y agrego el bloque separado
        "_total":      total,
        "_recurrente": recurrente,
        # Globales (regalos)
        "lineas_regalo": int(r2["lineas_regalo"]),
        "monto_regalo":  round(float(r2["monto_regalo"]), 2),
        "unds_regalo":   round(float(r2["unds_regalo"]), 2),
    }


# ════════════════════════════════════════════════════════════════════════════
# Helpers numéricos
# ════════════════════════════════════════════════════════════════════════════

def _pct_delta(curr: float, prev: float) -> Optional[float]:
    if prev is None or prev <= 0:
        return None
    return round((curr - prev) / prev * 100, 2)


def _delta_abs(curr: float, prev: float) -> float:
    return round((curr or 0) - (prev or 0), 2)


def _log_contrib(curr: float, prev: float) -> Optional[float]:
    if curr is None or prev is None or curr <= 0 or prev <= 0:
        return None
    return round(math.log(curr / prev) * 100, 2)


def _share(delta: float, total_delta: float) -> Optional[float]:
    if not total_delta:
        return None
    return round(delta / total_delta * 100, 1)


# ════════════════════════════════════════════════════════════════════════════
# Veredicto YoY vs vs_4w
# ════════════════════════════════════════════════════════════════════════════

def _veredicto(delta_yoy_pct: Optional[float], delta_4w_pct: Optional[float]) -> dict:
    """4 cuadrantes: YoY × vs_4_semanas → veredicto + explicación corta."""
    yoy_up   = (delta_yoy_pct is not None and delta_yoy_pct > 0)
    yoy_down = (delta_yoy_pct is not None and delta_yoy_pct < 0)
    w4_up    = (delta_4w_pct  is not None and delta_4w_pct  > 0)
    w4_down  = (delta_4w_pct  is not None and delta_4w_pct  < 0)

    if yoy_up and w4_up:
        return {
            "codigo": "CRECIENDO_FUERTE",
            "titulo": "Negocio en alza",
            "explicacion": "Estás arriba del año pasado Y arriba del último mes. Momentum positivo confirmado.",
        }
    if yoy_up and w4_down:
        return {
            "codigo": "BAJON_ESTACIONAL_NORMAL",
            "titulo": "Caída estacional, no problema",
            "explicacion": "Estás arriba del año pasado pero abajo del último mes. La caída reciente es estacional — tu negocio históricamente baja en este período.",
        }
    if yoy_down and w4_down:
        return {
            "codigo": "PROBLEMA_REAL",
            "titulo": "Bajón real — accionar",
            "explicacion": "Estás peor que el año pasado Y peor que el mes pasado. No es estacionalidad. Revisar drivers.",
        }
    if yoy_down and w4_up:
        return {
            "codigo": "ESTANCAMIENTO",
            "titulo": "Recuperación parcial",
            "explicacion": "Subiste vs el mes pasado pero seguís peor que el año pasado. Recuperación incompleta.",
        }
    return {
        "codigo": "INDETERMINADO",
        "titulo": "Sin datos suficientes",
        "explicacion": "No hay comparable suficiente (período previo o año anterior sin ventas).",
    }


# ════════════════════════════════════════════════════════════════════════════
# Descomposiciones (sumando al delta)
# ════════════════════════════════════════════════════════════════════════════

async def _decomp_by_office(
    db: AsyncSession, company_id: int, cur: dict, prev: dict, office_id: Optional[int],
    tipos_venta: list[int], excl: dict,
) -> list[dict]:
    """Descomposición por sucursal — suma al delta RECURRENTE (sin estacionales)."""
    _, flt_doc, extra = _office_filters(office_id)
    cur_p: dict[str, Any]  = {"dfrom": cur["from"],  "dto": cur["to"],  "tipos_venta": tipos_venta, "cid": company_id, **extra}
    prev_p: dict[str, Any] = {"dfrom": prev["from"], "dto": prev["to"], "tipos_venta": tipos_venta, "cid": company_id, **extra}
    excl_cur  = _excl_filter("vpf", excl, cur_p)
    excl_prev = _excl_filter("vpf", excl, prev_p)

    def _q(extra_where: str) -> str:
        return f"""
            SELECT doc.bsale_office_id, COALESCE(SUM(dd.total_amount), 0) AS venta
            FROM document_details dd
            JOIN documents doc       ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
            JOIN variants v          ON v.bsale_variant_id    = dd.bsale_variant_id  AND v.company_id   = dd.company_id
            LEFT JOIN v_products_full vpf ON vpf.bsale_product_id = v.bsale_product_id AND vpf.company_id = v.company_id
            WHERE dd.company_id = :cid
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= :dfrom
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <  :dto
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND doc.bsale_document_type_id = ANY(:tipos_venta)
              AND NOT dd.is_gratuity
              AND {flt_doc}
              {extra_where}
            GROUP BY doc.bsale_office_id
        """
    cur_rows  = (await db.execute(text(_q(excl_cur)),  cur_p)).mappings().all()
    prev_rows = (await db.execute(text(_q(excl_prev)), prev_p)).mappings().all()
    cur_map  = {int(r["bsale_office_id"]): float(r["venta"]) for r in cur_rows}
    prev_map = {int(r["bsale_office_id"]): float(r["venta"]) for r in prev_rows}

    scope = [office_id] if office_id is not None else list(OFFICE_IDS)
    off_rows = (await db.execute(
        text("SELECT bsale_office_id, name FROM offices WHERE company_id = :cid AND bsale_office_id = ANY(:ids)"),
        {"cid": company_id, "ids": scope},
    )).mappings().all()
    names = {int(r["bsale_office_id"]): r["name"] for r in off_rows}

    delta_total = sum(cur_map.values()) - sum(prev_map.values())
    out = []
    for oid in scope:
        c = cur_map.get(oid, 0.0)
        p = prev_map.get(oid, 0.0)
        d = c - p
        out.append({
            "office_id":     oid,
            "sucursal":      names.get(oid, str(oid)),
            "ventas_actual": round(c, 2),
            "ventas_prev":   round(p, 2),
            "delta_abs":     round(d, 2),
            "delta_pct":     _pct_delta(c, p),
            "share_pct":     _share(d, delta_total),
        })
    out.sort(key=lambda x: abs(x["delta_abs"]), reverse=True)
    return out


async def _decomp_by_category(
    db: AsyncSession, company_id: int, cur: dict, prev: dict, office_id: Optional[int],
    tipos_venta: list[int], top_n: int, excl: dict,
) -> list[dict]:
    """Top N categorías por |delta_abs| — excluye estacionales para no contaminar."""
    _, flt_doc, extra = _office_filters(office_id)
    cur_p: dict[str, Any]  = {"dfrom": cur["from"],  "dto": cur["to"],  "tipos_venta": tipos_venta, "cid": company_id, **extra}
    prev_p: dict[str, Any] = {"dfrom": prev["from"], "dto": prev["to"], "tipos_venta": tipos_venta, "cid": company_id, **extra}
    excl_cur  = _excl_filter("vpf", excl, cur_p)
    excl_prev = _excl_filter("vpf", excl, prev_p)

    def _q(extra_where: str) -> str:
        return f"""
            SELECT vpf.department AS departamento,
                   vpf.category   AS categoria,
                   COALESCE(SUM(dd.total_amount), 0) AS venta
            FROM document_details dd
            JOIN documents doc       ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
            JOIN variants v          ON v.bsale_variant_id    = dd.bsale_variant_id  AND v.company_id   = dd.company_id
            JOIN v_products_full vpf ON vpf.bsale_product_id  = v.bsale_product_id   AND vpf.company_id = v.company_id
            WHERE dd.company_id = :cid
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= :dfrom
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <  :dto
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND doc.bsale_document_type_id = ANY(:tipos_venta)
              AND NOT dd.is_gratuity
              AND {flt_doc}
              AND vpf.category IS NOT NULL
              {extra_where}
            GROUP BY vpf.department, vpf.category
        """
    cur_rows  = (await db.execute(text(_q(excl_cur)),  cur_p)).mappings().all()
    prev_rows = (await db.execute(text(_q(excl_prev)), prev_p)).mappings().all()

    def _key(r): return (r["departamento"], r["categoria"])
    cur_map  = {_key(r): float(r["venta"]) for r in cur_rows}
    prev_map = {_key(r): float(r["venta"]) for r in prev_rows}
    keys = set(cur_map) | set(prev_map)
    delta_total = sum(cur_map.values()) - sum(prev_map.values())

    rows = []
    for k in keys:
        c = cur_map.get(k, 0.0)
        p = prev_map.get(k, 0.0)
        d = c - p
        rows.append({
            "departamento": k[0], "categoria": k[1],
            "ventas_actual": round(c, 2), "ventas_prev": round(p, 2),
            "delta_abs": round(d, 2), "delta_pct": _pct_delta(c, p),
            "share_pct": _share(d, delta_total),
        })
    rows.sort(key=lambda r: abs(r["delta_abs"]), reverse=True)
    return rows[:top_n]


async def _decomp_by_dow(
    db: AsyncSession, company_id: int, cur: dict, prev: dict, office_id: Optional[int],
    tipos_venta: list[int], excl: dict,
) -> list[dict]:
    """Descomposición por día de la semana (1=Lun..7=Dom) — venta recurrente."""
    _, flt_doc, extra = _office_filters(office_id)
    cur_p: dict[str, Any]  = {"dfrom": cur["from"],  "dto": cur["to"],  "tipos_venta": tipos_venta, "cid": company_id, **extra}
    prev_p: dict[str, Any] = {"dfrom": prev["from"], "dto": prev["to"], "tipos_venta": tipos_venta, "cid": company_id, **extra}
    excl_cur  = _excl_filter("vpf", excl, cur_p)
    excl_prev = _excl_filter("vpf", excl, prev_p)

    def _q(extra_where: str) -> str:
        return f"""
            SELECT EXTRACT(ISODOW FROM (doc.emission_date AT TIME ZONE '{_TZ_DATE}'))::INT AS dow,
                   COALESCE(SUM(dd.total_amount), 0)              AS venta,
                   COUNT(DISTINCT doc.bsale_document_id)          AS tickets
            FROM document_details dd
            JOIN documents doc       ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
            JOIN variants v          ON v.bsale_variant_id    = dd.bsale_variant_id  AND v.company_id   = dd.company_id
            LEFT JOIN v_products_full vpf ON vpf.bsale_product_id = v.bsale_product_id AND vpf.company_id = v.company_id
            WHERE dd.company_id = :cid
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= :dfrom
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <  :dto
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND doc.bsale_document_type_id = ANY(:tipos_venta)
              AND NOT dd.is_gratuity
              AND {flt_doc}
              {extra_where}
            GROUP BY dow
        """
    cur_rows  = (await db.execute(text(_q(excl_cur)),  cur_p)).mappings().all()
    prev_rows = (await db.execute(text(_q(excl_prev)), prev_p)).mappings().all()
    cur_map  = {int(r["dow"]): (float(r["venta"]), int(r["tickets"])) for r in cur_rows}
    prev_map = {int(r["dow"]): (float(r["venta"]), int(r["tickets"])) for r in prev_rows}

    NAMES = {1: "Lun", 2: "Mar", 3: "Mie", 4: "Jue", 5: "Vie", 6: "Sab", 7: "Dom"}
    delta_total = sum(v for v, _ in cur_map.values()) - sum(v for v, _ in prev_map.values())
    out = []
    for dow in range(1, 8):
        c_v, c_t = cur_map.get(dow, (0.0, 0))
        p_v, p_t = prev_map.get(dow, (0.0, 0))
        d = c_v - p_v
        out.append({
            "dow":             dow,
            "dia":             NAMES[dow],
            "ventas_actual":   round(c_v, 2),
            "ventas_prev":     round(p_v, 2),
            "tickets_actual":  c_t,
            "tickets_prev":    p_t,
            "delta_abs":       round(d, 2),
            "delta_pct":       _pct_delta(c_v, p_v),
            "share_pct":       _share(d, delta_total),
        })
    return out


async def _decomp_by_hour(
    db: AsyncSession, company_id: int, cur: dict, prev: dict, office_id: Optional[int],
    tipos_venta: list[int], excl: dict,
) -> list[dict]:
    """Descomposición por franja horaria (generation_date → Lima) — venta recurrente.

    Documentos sin generation_date se ignoran (≈0.2% según data real).
    """
    _, flt_doc, extra = _office_filters(office_id)
    cur_p: dict[str, Any]  = {"dfrom": cur["from"],  "dto": cur["to"],  "tipos_venta": tipos_venta, "cid": company_id, **extra}
    prev_p: dict[str, Any] = {"dfrom": prev["from"], "dto": prev["to"], "tipos_venta": tipos_venta, "cid": company_id, **extra}
    excl_cur  = _excl_filter("vpf", excl, cur_p)
    excl_prev = _excl_filter("vpf", excl, prev_p)

    def _q(extra_where: str) -> str:
        return f"""
            SELECT EXTRACT(HOUR FROM (doc.generation_date AT TIME ZONE '{_TZ_HOUR}'))::INT AS hr,
                   COALESCE(SUM(dd.total_amount), 0)              AS venta,
                   COUNT(DISTINCT doc.bsale_document_id)          AS tickets
            FROM document_details dd
            JOIN documents doc       ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
            JOIN variants v          ON v.bsale_variant_id    = dd.bsale_variant_id  AND v.company_id   = dd.company_id
            LEFT JOIN v_products_full vpf ON vpf.bsale_product_id = v.bsale_product_id AND vpf.company_id = v.company_id
            WHERE dd.company_id = :cid
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= :dfrom
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <  :dto
              AND doc.generation_date IS NOT NULL
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND doc.bsale_document_type_id = ANY(:tipos_venta)
              AND NOT dd.is_gratuity
              AND {flt_doc}
              {extra_where}
            GROUP BY hr
        """
    cur_rows  = (await db.execute(text(_q(excl_cur)),  cur_p)).mappings().all()
    prev_rows = (await db.execute(text(_q(excl_prev)), prev_p)).mappings().all()

    def _bucket(hr: int) -> str:
        for label, lo, hi in _HOUR_BUCKETS:
            if lo <= hr < hi:
                return label
        return "fuera_horario"

    def _agg(rows):
        acc: dict[str, tuple[float, int]] = {b[0]: (0.0, 0) for b in _HOUR_BUCKETS}
        acc["fuera_horario"] = (0.0, 0)
        for r in rows:
            b = _bucket(int(r["hr"]))
            v, t = acc[b]
            acc[b] = (v + float(r["venta"]), t + int(r["tickets"]))
        return acc

    ca = _agg(cur_rows)
    pa = _agg(prev_rows)
    delta_total = sum(v for v, _ in ca.values()) - sum(v for v, _ in pa.values())

    out = []
    for label, _, _ in _HOUR_BUCKETS:
        c_v, c_t = ca[label]
        p_v, p_t = pa[label]
        d = c_v - p_v
        out.append({
            "franja":          label,
            "ventas_actual":   round(c_v, 2),
            "ventas_prev":     round(p_v, 2),
            "tickets_actual":  c_t,
            "tickets_prev":    p_t,
            "delta_abs":       round(d, 2),
            "delta_pct":       _pct_delta(c_v, p_v),
            "share_pct":       _share(d, delta_total),
        })
    # ordenar por |delta| para que el franja más impactante quede arriba
    out.sort(key=lambda x: abs(x["delta_abs"]), reverse=True)
    return out


async def _decomp_by_seller(
    db: AsyncSession, company_id: int, cur: dict, prev: dict, office_id: Optional[int],
    tipos_venta: list[int], top_n: int, excl: dict,
) -> list[dict]:
    """Top vendedores por |delta_abs| — venta recurrente. |delta| > 100."""
    _, flt_doc, extra = _office_filters(office_id)
    cur_p: dict[str, Any]  = {"dfrom": cur["from"],  "dto": cur["to"],  "tipos_venta": tipos_venta, "cid": company_id, **extra}
    prev_p: dict[str, Any] = {"dfrom": prev["from"], "dto": prev["to"], "tipos_venta": tipos_venta, "cid": company_id, **extra}
    excl_cur  = _excl_filter("vpf", excl, cur_p)
    excl_prev = _excl_filter("vpf", excl, prev_p)

    def _q(extra_where: str) -> str:
        return f"""
            SELECT doc.bsale_user_id,
                   COALESCE(SUM(dd.total_amount), 0)              AS venta,
                   COUNT(DISTINCT doc.bsale_document_id)          AS tickets
            FROM document_details dd
            JOIN documents doc       ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
            JOIN variants v          ON v.bsale_variant_id    = dd.bsale_variant_id  AND v.company_id   = dd.company_id
            LEFT JOIN v_products_full vpf ON vpf.bsale_product_id = v.bsale_product_id AND vpf.company_id = v.company_id
            WHERE dd.company_id = :cid
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= :dfrom
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <  :dto
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND doc.bsale_document_type_id = ANY(:tipos_venta)
              AND NOT dd.is_gratuity
              AND doc.bsale_user_id IS NOT NULL
              AND {flt_doc}
              {extra_where}
            GROUP BY doc.bsale_user_id
        """
    cur_rows  = (await db.execute(text(_q(excl_cur)),  cur_p)).mappings().all()
    prev_rows = (await db.execute(text(_q(excl_prev)), prev_p)).mappings().all()
    cur_map  = {int(r["bsale_user_id"]): (float(r["venta"]), int(r["tickets"])) for r in cur_rows}
    prev_map = {int(r["bsale_user_id"]): (float(r["venta"]), int(r["tickets"])) for r in prev_rows}
    ids = set(cur_map) | set(prev_map)
    if not ids:
        return []

    users = (await db.execute(
        text("SELECT bsale_user_id, COALESCE(first_name,'')||' '||COALESCE(last_name,'') AS nombre FROM users WHERE company_id = :cid AND bsale_user_id = ANY(:ids)"),
        {"cid": company_id, "ids": list(ids)},
    )).mappings().all()
    names = {int(r["bsale_user_id"]): (r["nombre"].strip() or f"User #{r['bsale_user_id']}") for r in users}

    delta_total = sum(v for v, _ in cur_map.values()) - sum(v for v, _ in prev_map.values())
    rows = []
    for uid in ids:
        c_v, c_t = cur_map.get(uid, (0.0, 0))
        p_v, p_t = prev_map.get(uid, (0.0, 0))
        d = c_v - p_v
        if abs(d) < 100:
            continue
        rows.append({
            "user_id":        uid,
            "nombre":         names.get(uid, f"User #{uid}"),
            "ventas_actual":  round(c_v, 2),
            "ventas_prev":    round(p_v, 2),
            "tickets_actual": c_t,
            "tickets_prev":   p_t,
            "delta_abs":      round(d, 2),
            "delta_pct":      _pct_delta(c_v, p_v),
            "share_pct":      _share(d, delta_total),
        })
    rows.sort(key=lambda r: abs(r["delta_abs"]), reverse=True)
    return rows[:top_n]


# ════════════════════════════════════════════════════════════════════════════
# Factores adicionales (lentes paralelas — NO suman al delta)
# ════════════════════════════════════════════════════════════════════════════

async def _lost_sales_from_stockouts(
    db: AsyncSession, company_id: int, cur: dict, office_id: Optional[int], tipos_venta: list[int],
    excl: Optional[dict] = None,
) -> dict:
    """Estimación de venta perdida por quiebres con demanda activa.

    Para cada SKU con stock_disponible <= 0 hoy y que vendió en los últimos
    30 días: estima la venta perdida como (días en quiebre dentro de la
    ventana current) × velocidad_30d × precio_unitario_promedio.

    Si `excl` viene, excluye SKUs de departamentos/categorías estacionales.
    """
    flt_plain, flt_doc, extra = _office_filters(office_id)
    days_in_window = (cur["to"] - cur["from"]).days
    d30 = cur["to"] - timedelta(days=30)
    excl = excl or {"depts": [], "cats": []}

    params: dict[str, Any] = {
        "dfrom": cur["from"], "dto": cur["to"], "d30": d30,
        "tipos_venta": tipos_venta, "cid": company_id, **extra,
    }
    excl_clause = _excl_filter("vpf", excl, params)

    q = f"""
        WITH ventas_30d AS (
            SELECT dd.bsale_variant_id, doc.bsale_office_id,
                   SUM(dd.quantity)                                  AS unds_30d,
                   COALESCE(NULLIF(SUM(dd.net_amount), 0), 0)        AS soles_30d,
                   COUNT(DISTINCT (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE) AS dias_con_venta
            FROM document_details dd
            JOIN documents doc       ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
            JOIN variants v          ON v.bsale_variant_id    = dd.bsale_variant_id  AND v.company_id   = dd.company_id
            LEFT JOIN v_products_full vpf ON vpf.bsale_product_id = v.bsale_product_id AND vpf.company_id = v.company_id
            WHERE dd.company_id = :cid
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= :d30
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <  :dto
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND doc.bsale_document_type_id = ANY(:tipos_venta)
              AND {flt_doc}
              {excl_clause}
            GROUP BY dd.bsale_variant_id, doc.bsale_office_id
        ),
        dias_quiebre AS (
            SELECT bsale_variant_id, bsale_office_id,
                   COUNT(*) AS dias_quiebre_en_ventana
            FROM stock_history
            WHERE company_id = :cid
              AND snapshot_date >= :dfrom
              AND snapshot_date <  :dto
              AND quantity_available <= 0
              AND {flt_plain}
            GROUP BY bsale_variant_id, bsale_office_id
        )
        SELECT v.display_code AS sku,
               p.name         AS producto,
               o.name         AS sucursal,
               dq.bsale_office_id,
               dq.bsale_variant_id,
               dq.dias_quiebre_en_ventana,
               v30.unds_30d,
               v30.soles_30d,
               (v30.unds_30d::numeric  / 30.0) AS tdpv,
               (v30.soles_30d::numeric / NULLIF(v30.unds_30d, 0)) AS precio_unit,
               (dq.dias_quiebre_en_ventana::numeric * v30.unds_30d / 30.0
                * (v30.soles_30d::numeric / NULLIF(v30.unds_30d, 0))) AS perdida_estimada
        FROM dias_quiebre dq
        JOIN ventas_30d v30
             ON v30.bsale_variant_id = dq.bsale_variant_id
            AND v30.bsale_office_id  = dq.bsale_office_id
        JOIN variants v  ON v.bsale_variant_id  = dq.bsale_variant_id AND v.company_id = :cid
        JOIN products p  ON p.bsale_product_id  = v.bsale_product_id  AND p.company_id = v.company_id
        JOIN offices  o  ON o.bsale_office_id   = dq.bsale_office_id  AND o.company_id = :cid
        WHERE v30.unds_30d > 0
        ORDER BY perdida_estimada DESC NULLS LAST
        LIMIT 25
    """
    rows = (await db.execute(text(q), params)).mappings().all()
    skus = [{
        "sku":           r["sku"],
        "producto":      r["producto"],
        "sucursal":      r["sucursal"],
        "office_id":     int(r["bsale_office_id"]),
        "dias_quiebre":  int(r["dias_quiebre_en_ventana"]),
        "tdpv":          round(float(r["tdpv"] or 0), 3),
        "precio_unit":   round(float(r["precio_unit"] or 0), 2),
        "perdida_estimada_pen": round(float(r["perdida_estimada"] or 0), 2),
    } for r in rows]
    total = sum(s["perdida_estimada_pen"] for s in skus)
    return {
        "monto_estimado_pen": round(total, 2),
        "skus_con_perdida":   len(skus),
        "metodo":             "dias_quiebre × tdpv_30d × precio_unit_30d",
        "ventana_dias":       days_in_window,
        "top_skus":           skus[:10],
    }


async def _returns_change(
    db: AsyncSession, company_id: int, cur: dict, prev: dict, office_id: Optional[int]
) -> dict:
    """Cambio en notas de crédito (devoluciones) entre ventanas."""
    flt_plain, _, extra = _office_filters(office_id)
    q = f"""
        SELECT COALESCE(SUM(total_amount), 0) AS monto,
               COUNT(*)                       AS tickets
        FROM documents
        WHERE company_id = :cid
          AND (emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= :dfrom
          AND (emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <  :dto
          AND COALESCE(is_credit_note, FALSE) = TRUE
          AND {flt_plain}
    """
    cur_r  = (await db.execute(text(q), {"cid": company_id, "dfrom": cur["from"],  "dto": cur["to"],  **extra})).mappings().one()
    prev_r = (await db.execute(text(q), {"cid": company_id, "dfrom": prev["from"], "dto": prev["to"], **extra})).mappings().one()
    return {
        "monto_actual":   round(float(cur_r["monto"]), 2),
        "monto_prev":     round(float(prev_r["monto"]), 2),
        "tickets_actual": int(cur_r["tickets"]),
        "tickets_prev":   int(prev_r["tickets"]),
        "delta_abs":      _delta_abs(float(cur_r["monto"]), float(prev_r["monto"])),
        "delta_pct":      _pct_delta(float(cur_r["monto"]), float(prev_r["monto"])),
    }


# ════════════════════════════════════════════════════════════════════════════
# Ganadores y perdedores (a nivel SKU)
# ════════════════════════════════════════════════════════════════════════════

async def _winners_and_losers(
    db: AsyncSession, company_id: int, cur: dict, prev: dict, office_id: Optional[int],
    tipos_venta: list[int], top_n: int, excl: dict,
) -> dict:
    """Top SKUs que subieron/cayeron, SKUs nuevos con tracción y enfriados.

    Excluye SKUs de departamentos/categorías estacionales (los "winners"
    estacionales son obvios y contaminan el ranking accionable).
    """
    flt_plain, flt_doc, extra = _office_filters(office_id)
    cur_p: dict[str, Any]  = {"dfrom": cur["from"],  "dto": cur["to"],  "tipos_venta": tipos_venta, "cid": company_id, **extra}
    prev_p: dict[str, Any] = {"dfrom": prev["from"], "dto": prev["to"], "tipos_venta": tipos_venta, "cid": company_id, **extra}
    excl_cur  = _excl_filter("vpf", excl, cur_p)
    excl_prev = _excl_filter("vpf", excl, prev_p)

    # SKU-level deltas
    def _q(extra_where: str) -> str:
        return f"""
            SELECT v.display_code AS sku,
                   p.name         AS producto,
                   p.bsale_product_id,
                   COALESCE(SUM(dd.total_amount), 0)              AS venta,
                   COALESCE(SUM(dd.quantity) FILTER (WHERE NOT dd.is_gratuity), 0) AS unds
            FROM document_details dd
            JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
            JOIN variants v    ON v.bsale_variant_id    = dd.bsale_variant_id  AND v.company_id   = dd.company_id
            JOIN products p    ON p.bsale_product_id    = v.bsale_product_id   AND p.company_id   = v.company_id
            LEFT JOIN v_products_full vpf ON vpf.bsale_product_id = v.bsale_product_id AND vpf.company_id = v.company_id
            WHERE dd.company_id = :cid
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= :dfrom
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <  :dto
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND doc.bsale_document_type_id = ANY(:tipos_venta)
              AND {flt_doc}
              {extra_where}
            GROUP BY v.display_code, p.name, p.bsale_product_id
        """
    cur_rows  = (await db.execute(text(_q(excl_cur)),  cur_p)).mappings().all()
    prev_rows = (await db.execute(text(_q(excl_prev)), prev_p)).mappings().all()

    cur_map  = {r["sku"]: (r["producto"], float(r["venta"]), float(r["unds"])) for r in cur_rows}
    prev_map = {r["sku"]: (r["producto"], float(r["venta"]), float(r["unds"])) for r in prev_rows}
    keys = set(cur_map) | set(prev_map)

    rows = []
    for sku in keys:
        pname_c, c_v, c_u = cur_map.get(sku, ("", 0.0, 0.0))
        pname_p, p_v, p_u = prev_map.get(sku, ("", 0.0, 0.0))
        d = c_v - p_v
        rows.append({
            "sku":           sku,
            "producto":      pname_c or pname_p,
            "ventas_actual": round(c_v, 2),
            "ventas_prev":   round(p_v, 2),
            "unds_actual":   round(c_u, 2),
            "unds_prev":     round(p_u, 2),
            "delta_abs":     round(d, 2),
            "delta_pct":     _pct_delta(c_v, p_v),
        })
    subieron = sorted([r for r in rows if r["delta_abs"] > 0], key=lambda r: r["delta_abs"], reverse=True)[:top_n]
    cayeron  = sorted([r for r in rows if r["delta_abs"] < 0], key=lambda r: r["delta_abs"])[:top_n]

    # SKUs nuevos con tracción: primera_venta_lifetime dentro de la ventana actual
    cur_p_nuevos: dict[str, Any] = {**cur_p, "top_n": top_n}
    excl_nuevos = _excl_filter("vpf", excl, cur_p_nuevos)
    q_nuevos = f"""
        WITH primera_venta AS (
            SELECT dd.bsale_variant_id,
                   MIN((doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE) AS primera
            FROM document_details dd
            JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
            WHERE dd.company_id = :cid
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND doc.bsale_document_type_id = ANY(:tipos_venta)
              AND {flt_doc}
            GROUP BY dd.bsale_variant_id
        ),
        venta_ventana AS (
            SELECT dd.bsale_variant_id,
                   SUM(dd.total_amount) AS venta,
                   SUM(dd.quantity) FILTER (WHERE NOT dd.is_gratuity) AS unds
            FROM document_details dd
            JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
            WHERE dd.company_id = :cid
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= :dfrom
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <  :dto
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND doc.bsale_document_type_id = ANY(:tipos_venta)
              AND {flt_doc}
            GROUP BY dd.bsale_variant_id
        )
        SELECT v.display_code AS sku, p.name AS producto,
               pv.primera, vv.venta, vv.unds
        FROM primera_venta pv
        JOIN venta_ventana vv ON vv.bsale_variant_id = pv.bsale_variant_id
        JOIN variants v ON v.bsale_variant_id = pv.bsale_variant_id AND v.company_id = :cid
        JOIN products p ON p.bsale_product_id = v.bsale_product_id  AND p.company_id = v.company_id
        LEFT JOIN v_products_full vpf ON vpf.bsale_product_id = v.bsale_product_id AND vpf.company_id = v.company_id
        WHERE pv.primera >= :dfrom
          AND pv.primera <  :dto
          AND vv.venta > 0
          {excl_nuevos}
        ORDER BY vv.venta DESC
        LIMIT :top_n
    """
    nuevos_rows = (await db.execute(text(q_nuevos), cur_p_nuevos)).mappings().all()
    nuevos = [{
        "sku":           r["sku"],
        "producto":      r["producto"],
        "ventas":        round(float(r["venta"] or 0), 2),
        "unds":          round(float(r["unds"] or 0), 2),
        "primera_venta": r["primera"].isoformat() if r["primera"] else None,
    } for r in nuevos_rows]

    # SKUs que se enfriaron: vendieron en `prev` (>= 100 S/) y no vendieron en `current` (0)
    enfriados = sorted(
        [r for r in rows if r["ventas_prev"] >= 100 and r["ventas_actual"] == 0],
        key=lambda r: r["ventas_prev"], reverse=True
    )[:top_n]

    return {
        "top_subieron":            subieron,
        "top_cayeron":             cayeron,
        "skus_nuevos_con_traccion": nuevos,
        "skus_que_se_enfriaron":   enfriados,
    }


# ════════════════════════════════════════════════════════════════════════════
# Huecos YoY (catálogo enfriado vs. mismo período hace 12 meses)
# Ventana de comparación: 30 días vs 30 días hace 364 días (más estable que 7d).
# ════════════════════════════════════════════════════════════════════════════

async def _huecos_yoy(
    db: AsyncSession, company_id: int, office_id: Optional[int], tipos_venta: list[int], top_n: int,
    excl: dict,
) -> list[dict]:
    today = date.today()
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
                   vpf.subcategory AS subcategoria,
                   COALESCE(SUM(dd.total_amount), 0) AS venta
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
    cur_map = {_key(r): float(r["venta"]) for r in cur_rows}
    yoy_map = {_key(r): float(r["venta"]) for r in yoy_rows}
    keys = set(yoy_map)  # solo nos importan las que vendían el año pasado

    huecos = []
    for k in keys:
        c = cur_map.get(k, 0.0)
        p = yoy_map.get(k, 0.0)
        if p < 500:                        # ruido — la sub-cat no era material el año pasado
            continue
        d = c - p
        delta_pct = (d / p * 100) if p else 0
        if delta_pct > -50:                # solo huecos significativos (cayó >50%)
            continue
        diagnostico = "discontinuado_sin_reemplazo" if c == 0 else "cambio_de_demanda"
        huecos.append({
            "departamento":   k[0],
            "categoria":      k[1],
            "subcategoria":   k[2],
            "venta_actual":   round(c, 2),
            "venta_yoy":      round(p, 2),
            "hueco_pen":      round(-d, 2),
            "delta_pct":      round(delta_pct, 1),
            "diagnostico":    diagnostico,
        })
    huecos.sort(key=lambda r: r["hueco_pen"], reverse=True)
    return huecos[:top_n]


# ════════════════════════════════════════════════════════════════════════════
# Narrativa
# ════════════════════════════════════════════════════════════════════════════

def _build_narrative(payload: dict) -> list[str]:
    out: list[str] = []
    cmp_w = payload["kpis"]["vs_semana_anterior"]
    cmp_y = payload["kpis"]["vs_ano_anterior"]
    days = payload["meta"]["current"]["dias"]

    # Si hay exclusiones cargadas, aclaramos que el delta es sobre venta recurrente
    excl = payload["meta"].get("exclusiones", {})
    tiene_excl = bool(excl.get("departamentos") or excl.get("categorias"))
    base_label = "recurrentes (sin estacionales)" if tiene_excl else "totales"

    d_w_total = cmp_w.get("delta_pct_total")
    d_w_rec   = cmp_w.get("delta_pct_recurrente")
    a_w_rec   = cmp_w.get("delta_abs_recurrente") or 0
    if d_w_rec is not None:
        signo = "cayeron" if d_w_rec < 0 else "subieron"
        if tiene_excl and d_w_total is not None and abs(d_w_total - d_w_rec) >= 5:
            out.append(
                f"Las ventas {base_label} {signo} {abs(d_w_rec)}% (S/ {abs(a_w_rec):,.0f}) "
                f"vs los {days} días previos — el total bruto cambió {d_w_total:+.1f}% (incluye estacionales)."
            )
        else:
            out.append(
                f"Las ventas {signo} {abs(d_w_rec)}% (S/ {abs(a_w_rec):,.0f}) "
                f"vs los {days} días previos."
            )
    d_y_rec = cmp_y.get("delta_pct_recurrente")
    if d_y_rec is not None:
        signo = "abajo" if d_y_rec < 0 else "arriba"
        out.append(f"Estás {abs(d_y_rec)}% {signo} respecto al mismo período del año pasado ({base_label}).")

    v = payload["veredicto"]
    out.append(f"Veredicto: {v['titulo']}. {v['explicacion']}")

    # Anatomía
    an = payload["anatomia"]["contribucion_log_pct"]
    if an.get("tickets") is not None:
        contribs = {
            "tráfico (menos tickets)": an.get("tickets"),
            "canasta más chica": an.get("unds_per_ticket"),
            "precio más bajo": an.get("monto_per_und"),
        }
        # Filter negative contributions if delta is negative; positive if positive
        dominante = max(contribs.items(), key=lambda x: abs(x[1] or 0))
        if dominante[1] is not None and abs(dominante[1]) > 1:
            out.append(f"El factor dominante es {dominante[0]} ({dominante[1]:+.1f}% en escala log).")

    # Sucursal con mayor caída
    suc = payload["descomposicion"]["por_sucursal"]
    if suc and suc[0]["delta_abs"] != 0:
        s = suc[0]
        signo = "explica el" if s["delta_abs"] < 0 else "aportó al"
        if s["share_pct"] is not None:
            out.append(f"{s['sucursal']} {signo} {abs(s['share_pct'])}% del cambio (S/ {s['delta_abs']:+,.0f}).")

    # Categoría más impactante
    cats = payload["descomposicion"]["por_categoria"]
    if cats:
        top_caida = next((c for c in cats if c["delta_abs"] < 0), None)
        if top_caida:
            out.append(f"Categoría más golpeada: {top_caida['categoria']} (S/ {top_caida['delta_abs']:+,.0f}, {top_caida['delta_pct']}%).")

    # Franja horaria
    horas = payload["descomposicion"]["por_franja_horaria"]
    if horas:
        worst = horas[0]
        if worst["delta_abs"] != 0:
            verb = "perdió" if worst["delta_abs"] < 0 else "ganó"
            out.append(f"La franja {worst['franja']} {verb} S/ {abs(worst['delta_abs']):,.0f}.")

    # Quiebres
    q = payload["factores_adicionales"]["venta_perdida_por_quiebre"]
    if q["monto_estimado_pen"] >= 100:
        out.append(f"{q['skus_con_perdida']} SKUs en quiebre con demanda: ~S/ {q['monto_estimado_pen']:,.0f} de venta perdida estimada.")

    # Devoluciones
    dev = payload["factores_adicionales"]["devoluciones"]
    if dev["delta_pct"] is not None and dev["delta_pct"] > 30 and dev["monto_actual"] > 500:
        out.append(f"Las devoluciones subieron {dev['delta_pct']}% (S/ {dev['monto_actual']:,.0f} vs S/ {dev['monto_prev']:,.0f}).")

    # Huecos YoY
    huecos = payload["huecos_yoy"]
    if huecos:
        top = huecos[0]
        out.append(f"Hueco YoY más grande: {top['subcategoria']} (S/ {top['hueco_pen']:,.0f}/mes vs hace 12 meses).")

    return out


# ════════════════════════════════════════════════════════════════════════════
# Endpoint principal
# ════════════════════════════════════════════════════════════════════════════

@router.get("")
async def diagnosis(
    days: int = Query(7, ge=1, le=90, description="Tamaño de la ventana actual en días (excluye HOY)."),
    office_id: Optional[int] = Query(None, description="ID de sucursal (vacío = todas las sucursales activas)."),
    top_n: int = Query(10, ge=1, le=50, description="Cuántos ítems por lista (categorías, vendedores, SKUs)."),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Diagnóstico ejecutivo de ventas — responde "¿por qué vendo menos hoy?".

    Devuelve KPIs vs 3 ventanas, veredicto, anatomía, descomposición por 5 ejes,
    factores adicionales, ganadores/perdedores SKU, huecos YoY y narrativa.
    """
    periods = _resolve_periods(days)
    cur, week, base, yoy = periods["current"], periods["week"], periods["base_4w"], periods["yoy"]

    cid = company.company_id
    company_cfg = await get_company(db, cid)
    tipos_venta = company_cfg.get("tipos_venta") or DEFAULT_TIPOS_VENTA
    excl = await _load_exclusions(db, cid)

    # ── KPIs por ventana (devuelve total + recurrente) ───────────────────
    k_cur  = await _period_kpis(db, cid, cur["from"],  cur["to"],  office_id, tipos_venta, excl)
    k_week = await _period_kpis(db, cid, week["from"], week["to"], office_id, tipos_venta, excl)
    k_base = await _period_kpis(db, cid, base["from"], base["to"], office_id, tipos_venta, excl)
    k_yoy  = await _period_kpis(db, cid, yoy["from"],  yoy["to"],  office_id, tipos_venta, excl)

    # Normalizar base_4w al tamaño de `days` (promedio diario × days) — para ambos bloques.
    scale = days / 28.0
    def _scale_block(b: dict) -> dict:
        return {
            **b,
            "ventas":  round(b["ventas"]  * scale, 2),
            "tickets": int(round(b["tickets"] * scale)),
            "unds":    round(b["unds"]    * scale, 2),
        }
    k_base_norm_total = _scale_block(k_base["_total"])
    k_base_norm_rec   = _scale_block(k_base["_recurrente"])

    def _make_cmp_dual(cur_blk: dict, prev_blk: dict, prev_rec: dict, cur_rec: dict,
                       prev_window: dict, label: str) -> dict:
        """Devuelve la comparación con dos deltas: total y recurrente."""
        return {
            "label":            label,
            "from":             prev_window["from"].isoformat(),
            "to":               (prev_window["to"] - timedelta(days=1)).isoformat(),
            "ventas_total":     prev_blk["ventas"],
            "ventas_recurrente": prev_rec["ventas"],
            "tickets":          prev_blk["tickets"],
            "delta_abs_total":     _delta_abs(cur_blk["ventas"], prev_blk["ventas"]),
            "delta_pct_total":     _pct_delta(cur_blk["ventas"], prev_blk["ventas"]),
            "delta_abs_recurrente": _delta_abs(cur_rec["ventas"], prev_rec["ventas"]),
            "delta_pct_recurrente": _pct_delta(cur_rec["ventas"], prev_rec["ventas"]),
        }

    kpis = {
        "actual": {
            "from":               cur["from"].isoformat(),
            "to":                 (cur["to"] - timedelta(days=1)).isoformat(),
            "ventas_total":       k_cur["_total"]["ventas"],
            "ventas_recurrente":  k_cur["_recurrente"]["ventas"],
            "tickets":            k_cur["_total"]["tickets"],
            "unds":               k_cur["_total"]["unds"],
            "ticket_promedio":    k_cur["_total"]["ticket_promedio"],
            "margen_pct_total":   k_cur["_total"]["margen_pct"],
            "margen_pct_recurrente": k_cur["_recurrente"]["margen_pct"],
            "descuento_pct":      k_cur["_total"]["descuento_pct"],
            "_detalle_total":     k_cur["_total"],
            "_detalle_recurrente": k_cur["_recurrente"],
        },
        "vs_semana_anterior":    _make_cmp_dual(k_cur["_total"], k_week["_total"], k_week["_recurrente"], k_cur["_recurrente"], week, "semana_anterior_ajustada"),
        "vs_promedio_4_semanas": _make_cmp_dual(k_cur["_total"], k_base_norm_total, k_base_norm_rec,      k_cur["_recurrente"], base, "promedio_4_semanas_normalizado"),
        "vs_ano_anterior":       _make_cmp_dual(k_cur["_total"], k_yoy["_total"],  k_yoy["_recurrente"],  k_cur["_recurrente"], yoy,  "mismo_periodo_ano_anterior"),
    }

    # ── Veredicto: usa el delta RECURRENTE (no contaminado por estacionalidad) ─
    veredicto = _veredicto(
        kpis["vs_ano_anterior"]["delta_pct_recurrente"],
        kpis["vs_promedio_4_semanas"]["delta_pct_recurrente"],
    )

    # ── Anatomía (sobre venta recurrente vs semana anterior) ────────────
    cur_rec  = k_cur["_recurrente"]
    week_rec = k_week["_recurrente"]
    anatomia = {
        "delta_pct_total":     _pct_delta(cur_rec["ventas"], week_rec["ventas"]),
        "contribucion_log_pct": {
            "tickets":         _log_contrib(cur_rec["tickets"],          week_rec["tickets"]),
            "unds_per_ticket": _log_contrib(cur_rec["unds_per_ticket"],  week_rec["unds_per_ticket"]),
            "monto_per_und":   _log_contrib(cur_rec["monto_per_und"],    week_rec["monto_per_und"]),
            "total":           _log_contrib(cur_rec["ventas"],           week_rec["ventas"]),
        },
        "comparacion_base": "semana_anterior_ajustada",
        "base_calculo":     "venta_recurrente",
        "lectura":          _anatomia_lectura(cur_rec, week_rec),
    }

    # ── Descomposiciones (todas comparan contra week sobre venta recurrente) ──
    por_sucursal  = await _decomp_by_office  (db, cid, cur, week, office_id, tipos_venta, excl)
    por_categoria = await _decomp_by_category(db, cid, cur, week, office_id, tipos_venta, top_n, excl)
    por_dow       = await _decomp_by_dow     (db, cid, cur, week, office_id, tipos_venta, excl)
    por_hora      = await _decomp_by_hour    (db, cid, cur, week, office_id, tipos_venta, excl)
    por_vendedor  = await _decomp_by_seller  (db, cid, cur, week, office_id, tipos_venta, top_n, excl)

    # ── Factores adicionales ─────────────────────────────────────────────
    quiebres = await _lost_sales_from_stockouts(db, cid, cur, office_id, tipos_venta, excl)
    devoluciones = await _returns_change(db, cid, cur, week, office_id)
    descuento_change = {
        "pct_actual":   k_cur["_recurrente"]["descuento_pct"],
        "pct_prev":     k_week["_recurrente"]["descuento_pct"],
        "delta_pp":     round(k_cur["_recurrente"]["descuento_pct"] - k_week["_recurrente"]["descuento_pct"], 2),
        "monto_actual": k_cur["_recurrente"]["descuento_monto"],
        "monto_prev":   k_week["_recurrente"]["descuento_monto"],
    }
    gratuidades = {
        "lineas_actual": k_cur["lineas_regalo"],
        "lineas_prev":   k_week["lineas_regalo"],
        "monto_actual":  k_cur["monto_regalo"],
        "monto_prev":    k_week["monto_regalo"],
        "delta_pct":     _pct_delta(k_cur["monto_regalo"], k_week["monto_regalo"]),
    }

    # ── Ganadores/perdedores SKU + huecos YoY ────────────────────────────
    ganadores = await _winners_and_losers(db, cid, cur, week, office_id, tipos_venta, top_n, excl)
    huecos = await _huecos_yoy(db, cid, office_id, tipos_venta, top_n, excl)

    # ── Meta: feriados, sync ─────────────────────────────────────────────
    feriados_cur  = holidays_in_range(cur["from"],  cur["to"])
    feriados_week = holidays_in_range(week["from"], week["to"])
    feriados_yoy  = holidays_in_range(yoy["from"],  yoy["to"])
    alertas: list[dict] = []
    if len(feriados_cur) != len(feriados_week):
        alertas.append({
            "tipo":     "feriados_desbalanceados",
            "actual":   feriados_cur,
            "previo":   feriados_week,
            "mensaje":  "Las ventanas tienen distinta cantidad de feriados — el delta vs semana anterior puede estar sesgado.",
        })
    if len(feriados_cur) != len(feriados_yoy):
        alertas.append({
            "tipo":     "feriados_desbalanceados_yoy",
            "actual":   feriados_cur,
            "yoy":      feriados_yoy,
            "mensaje":  "Distinta cantidad de feriados vs mismo período año anterior — el YoY puede estar sesgado.",
        })

    last_sync = await db.scalar(
        text(
            "SELECT MAX(finished_at) FROM sync_log "
            "WHERE company_id = :cid AND entity IN ('documents','document_details') AND status='OK'"
        ),
        {"cid": cid},
    )

    cobertura_pct = k_cur["_total"]["cobertura_costos_pct"]
    cobertura_warn = None
    if cobertura_pct < 90:
        cobertura_warn = (
            f"⚠️ Solo {cobertura_pct}% de la venta tiene costo cargado — el margen "
            f"reportado solo refleja esos SKUs. Correr POST /config/variant-costs/"
            f"backfill-from-receptions para recuperar."
        )
    meta = {
        "current":  {"from": cur["from"].isoformat(),  "to": (cur["to"]  - timedelta(days=1)).isoformat(), "dias": days},
        "semana":   {"from": week["from"].isoformat(), "to": (week["to"] - timedelta(days=1)).isoformat(), "dias": days, "shift_dias": week["shift"]},
        "base_4w":  {"from": base["from"].isoformat(), "to": (base["to"] - timedelta(days=1)).isoformat(), "dias": 28, "normalizado_a_dias": days},
        "yoy":      {"from": yoy["from"].isoformat(),  "to": (yoy["to"]  - timedelta(days=1)).isoformat(), "dias": days},
        "office_id": office_id,
        "office_scope": [office_id] if office_id is not None else list(OFFICE_IDS),
        "hoy_excluido": True,
        "datos_sync_hasta": last_sync.isoformat() if last_sync else None,
        "generado_at": datetime.utcnow().isoformat() + "Z",
        "exclusiones": {
            "departamentos": excl.get("depts", []),
            "categorias":    excl.get("cats", []),
            "nota": "Las descomposiciones, drivers y veredicto se calculan sobre la venta RECURRENTE (sin estacionales). KPIs muestran total + recurrente.",
        },
        "cobertura_costos": {
            "pct_actual":     cobertura_pct,
            "estado":         "OK" if cobertura_pct >= 90 else ("ADVERTENCIA" if cobertura_pct >= 70 else "CRITICA"),
            "warning":        cobertura_warn,
        },
        "alertas":  alertas,
    }

    payload: dict[str, Any] = {
        "meta":      meta,
        "kpis":      kpis,
        "veredicto": veredicto,
        "anatomia":  anatomia,
        "descomposicion": {
            "comparacion_base":   "semana_anterior_ajustada",
            "por_sucursal":       por_sucursal,
            "por_categoria":      por_categoria,
            "por_dia_semana":     por_dow,
            "por_franja_horaria": por_hora,
            "por_vendedor":       por_vendedor,
        },
        "factores_adicionales": {
            "venta_perdida_por_quiebre": quiebres,
            "cambio_descuentos":         descuento_change,
            "devoluciones":              devoluciones,
            "gratuidades":               gratuidades,
        },
        "ganadores_y_perdedores": ganadores,
        "huecos_yoy":             huecos,
    }
    payload["resumen"] = _build_narrative(payload)
    return payload


def _anatomia_lectura(cur: dict, prev: dict) -> str:
    """Lee la descomposición log y devuelve la frase operativa."""
    contribs = {
        "tickets":         _log_contrib(cur["tickets"],          prev["tickets"]),
        "unds_per_ticket": _log_contrib(cur["unds_per_ticket"],  prev["unds_per_ticket"]),
        "monto_per_und":   _log_contrib(cur["monto_per_und"],    prev["monto_per_und"]),
    }
    contribs = {k: v for k, v in contribs.items() if v is not None}
    if not contribs:
        return "Sin datos suficientes para descomponer."
    dom = max(contribs.items(), key=lambda x: abs(x[1]))
    mapping = {
        "tickets":         "tráfico (más/menos clientes)",
        "unds_per_ticket": "canasta (cuánto lleva cada cliente)",
        "monto_per_und":   "precio (cuánto vale lo que lleva)",
    }
    direction = "subió" if dom[1] > 0 else "bajó"
    return f"El cambio es dominado por {mapping[dom[0]]} — {direction} {abs(dom[1]):.1f}% (escala log)."
