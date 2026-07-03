"""Vista 1 — Pulso (¿cómo voy hoy?).

Endpoint único `GET /pulse` que devuelve el estado del negocio para un vistazo
de 10 segundos:

  1. mes_en_curso       : venta acumulada del mes vs meta + proyección de cierre
  2. veredicto          : "voy bien / cuidado / problema" (mes en curso vs mes
                          anterior y vs mismo mes año pasado)
  3. ultimo_dia_cerrado : KPIs de AYER + comparación contra el promedio del
                          mismo día de la semana últimas 8 semanas
  4. semana_en_curso    : día por día de la semana actual (lun → ayer)
  5. ultimos_7_dias     : KPIs de los 7 días previos + delta vs semana anterior
                          y vs YoY (resumen de lo que diagnosis ya detalla)
  6. alertas            : top 3-5 cosas urgentes detectadas automáticamente
                          (quiebres críticos, categorías cayendo, día anómalo)

Reusa helpers de `diagnosis.py` para no duplicar lógica de cálculo.
"""

from __future__ import annotations

import statistics
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from analytics.core.config import OFFICE_IDS
from app.auth import CurrentCompany, get_current_company
from app.database import get_db
from app.routers.config_admin import get_goals, goal_for_month, get_company
from app.routers.diagnosis import (
    _decomp_by_category,
    _excl_filter,
    _load_exclusions,
    _lost_sales_from_stockouts,
    _office_filters,
    _pct_delta,
    _period_kpis,
    _TZ_DATE,
    _veredicto,
)
from harvester.config import TIPOS_VENTA as DEFAULT_TIPOS_VENTA


router = APIRouter(
    prefix="/pulse",
    tags=["pulse"],
    dependencies=[Depends(get_current_company)],
)


# ════════════════════════════════════════════════════════════════════════════
# Mes en curso vs meta
# ════════════════════════════════════════════════════════════════════════════

async def _mes_en_curso(
    db: AsyncSession, company_id: int, today: date, office_id: Optional[int], tipos_venta: list[int],
    excl: dict,
) -> dict:
    """Venta acumulada del mes (1° → ayer) vs meta cargada en app_config.
    Devuelve venta total y recurrente por sucursal.
    """
    year, mon = today.year, today.month
    dias_del_mes = monthrange(year, mon)[1]
    first = date(year, mon, 1)
    last = date(year, mon, dias_del_mes)
    if today.day == 1:
        corte, dias_transcurridos = first, 0
    elif today > last:
        corte, dias_transcurridos = last, dias_del_mes
    else:
        corte = today - timedelta(days=1)
        dias_transcurridos = corte.day

    dfrom = first
    dto = corte + timedelta(days=1) if dias_transcurridos > 0 else first

    flt_plain, flt_doc, extra = _office_filters(office_id)
    scope = [office_id] if office_id is not None else list(OFFICE_IDS)

    if dias_transcurridos > 0:
        params: dict[str, Any] = {"dfrom": dfrom, "dto": dto, "tipos_venta": tipos_venta, "cid": company_id, **extra}
        # Construir el filtro de exclusión inline (para usar en FILTER)
        recurrent_terms: list[str] = []
        if excl.get("depts"):
            params["_excl_depts"] = excl["depts"]
            recurrent_terms.append("COALESCE(vpf.department, '') <> ALL(:_excl_depts)")
        if excl.get("cats"):
            params["_excl_cats"] = excl["cats"]
            recurrent_terms.append("COALESCE(vpf.category, '') <> ALL(:_excl_cats)")
        recurrent_inline = (" AND " + " AND ".join(recurrent_terms)) if recurrent_terms else ""

        q = f"""
            SELECT doc.bsale_office_id,
                   COALESCE(SUM(dd.total_amount), 0)                                          AS venta_total,
                   COALESCE(SUM(dd.total_amount) FILTER (WHERE TRUE {recurrent_inline}), 0)   AS venta_recurrente
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
            GROUP BY doc.bsale_office_id
        """
        rows = (await db.execute(text(q), params)).mappings().all()
        ventas_por_office = {
            int(r["bsale_office_id"]): {
                "total": float(r["venta_total"]),
                "recurrente": float(r["venta_recurrente"]),
            }
            for r in rows
        }
    else:
        ventas_por_office = {oid: {"total": 0.0, "recurrente": 0.0} for oid in scope}

    off_rows = (await db.execute(
        text("SELECT bsale_office_id, name FROM offices WHERE company_id = :cid AND bsale_office_id = ANY(:ids)"),
        {"cid": company_id, "ids": scope},
    )).mappings().all()
    office_names = {int(r["bsale_office_id"]): r["name"] for r in off_rows}

    goals = await get_goals(db, company_id)
    mes_str = f"{year:04d}-{mon:02d}"
    month_goals, fuente = goal_for_month(goals, mes_str)
    frac = (dias_transcurridos / dias_del_mes) if dias_del_mes else 0.0

    def _meta_val(key: str) -> Optional[float]:
        v = month_goals.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _row(oid: Optional[int], venta_total: float, venta_recurrente: float,
             meta: Optional[float]) -> dict:
        # La meta se compara contra la venta TOTAL (la meta del dueño es de venta total,
        # incluye estacional). El número recurrente se reporta aparte para contexto.
        meta_prorr = (meta * frac) if meta is not None else None
        avance = (venta_total / meta * 100) if meta else None
        ritmo = (venta_total / meta_prorr * 100) if meta_prorr else None
        proy = (venta_total / dias_transcurridos * dias_del_mes) if dias_transcurridos else None
        gap = (venta_total - meta) if meta is not None else None
        dias_restantes = dias_del_mes - dias_transcurridos
        venta_diaria_nec = ((meta - venta_total) / dias_restantes) if (meta and dias_restantes > 0 and venta_total < meta) else None
        estado = _estado_meta(meta, avance, ritmo, dias_transcurridos, dias_del_mes)
        return {
            "office_id":                  oid,
            "sucursal":                   "TODAS" if oid is None else office_names.get(oid, str(oid)),
            "venta_acumulada":            round(venta_total, 2),         # venta total (compatible con meta)
            "venta_acumulada_recurrente": round(venta_recurrente, 2),    # sin estacional (operativa)
            "meta":                       meta,
            "meta_prorrateada":           round(meta_prorr, 2) if meta_prorr is not None else None,
            "gap_a_meta":                 round(gap, 2) if gap is not None else None,
            "avance_pct":                 round(avance, 1) if avance is not None else None,
            "cumplimiento_vs_ritmo_pct":  round(ritmo, 1) if ritmo is not None else None,
            "proyeccion_cierre_mes":      round(proy, 2) if proy is not None else None,
            "venta_diaria_necesaria":     round(venta_diaria_nec, 2) if venta_diaria_nec is not None else None,
            "estado":                     estado,
        }

    por_sucursal = [
        _row(oid,
             ventas_por_office.get(oid, {}).get("total", 0.0),
             ventas_por_office.get(oid, {}).get("recurrente", 0.0),
             _meta_val(str(oid)))
        for oid in scope
    ]

    if office_id is not None:
        glob = por_sucursal[0]
    else:
        venta_total_global = sum(ventas_por_office.get(oid, {}).get("total", 0.0) for oid in scope)
        venta_rec_global   = sum(ventas_por_office.get(oid, {}).get("recurrente", 0.0) for oid in scope)
        meta_global = _meta_val("global")
        if meta_global is None:
            sub = [_meta_val(str(oid)) for oid in scope]
            meta_global = sum(x for x in sub if x is not None) or None
        glob = _row(None, venta_total_global, venta_rec_global, meta_global)

    return {
        "mes":                  mes_str,
        "meta_source":          fuente,
        "dias_transcurridos":   dias_transcurridos,
        "dias_del_mes":         dias_del_mes,
        "dias_restantes":       dias_del_mes - dias_transcurridos,
        "ultimo_dia_cerrado":   corte.isoformat() if dias_transcurridos > 0 else None,
        "global":               glob,
        "por_sucursal":         por_sucursal,
    }


def _estado_meta(
    meta: Optional[float], avance: Optional[float], ritmo: Optional[float],
    dias_trans: int, dias_mes: int,
) -> str:
    if meta is None or avance is None:
        return "SIN_META"
    if avance >= 100:
        return "META_CUMPLIDA"
    if ritmo is None:
        return "EN_RITMO"
    if ritmo >= 95:
        return "ADELANTADO" if ritmo > 105 else "EN_RITMO"
    if ritmo >= 80:
        return "ATRASADO_LEVE"
    return "RIESGO_NO_LLEGAR"


# ════════════════════════════════════════════════════════════════════════════
# Veredicto del momento (mes en curso vs mes anterior y vs YoY)
# ════════════════════════════════════════════════════════════════════════════

async def _veredicto_mes(
    db: AsyncSession, company_id: int, today: date, office_id: Optional[int], tipos_venta: list[int],
    excl: dict,
) -> dict:
    """Compara los días transcurridos del mes contra:
       - mismos N días del mes anterior
       - mismos N días del mismo mes hace 12 meses
    Usa venta RECURRENTE para el veredicto (sin estacionales).
    """
    year, mon = today.year, today.month
    if today.day == 1:
        return {"codigo": "INDETERMINADO", "titulo": "Mes recién empezado",
                "explicacion": "Aún no hay días cerrados en el mes actual.",
                "delta_yoy_pct": None, "delta_mes_anterior_pct": None}

    cur_from = date(year, mon, 1)
    cur_to   = today
    dias_trans = today.day - 1

    if mon == 1:
        prev_year, prev_mon = year - 1, 12
    else:
        prev_year, prev_mon = year, mon - 1
    prev_dias_mes = monthrange(prev_year, prev_mon)[1]
    prev_from = date(prev_year, prev_mon, 1)
    prev_to   = prev_from + timedelta(days=min(dias_trans, prev_dias_mes))

    yoy_year = year - 1
    yoy_dias_mes = monthrange(yoy_year, mon)[1]
    yoy_from = date(yoy_year, mon, 1)
    yoy_to   = yoy_from + timedelta(days=min(dias_trans, yoy_dias_mes))

    k_cur  = await _period_kpis(db, company_id, cur_from,  cur_to,  office_id, tipos_venta, excl)
    k_prev = await _period_kpis(db, company_id, prev_from, prev_to, office_id, tipos_venta, excl)
    k_yoy  = await _period_kpis(db, company_id, yoy_from,  yoy_to,  office_id, tipos_venta, excl)

    # Veredicto sobre RECURRENTE
    cur_rec  = k_cur["_recurrente"]["ventas"]
    prev_rec = k_prev["_recurrente"]["ventas"]
    yoy_rec  = k_yoy["_recurrente"]["ventas"]
    d_prev = _pct_delta(cur_rec, prev_rec)
    d_yoy  = _pct_delta(cur_rec, yoy_rec)
    veredicto = _veredicto(d_yoy, d_prev)
    return {
        **veredicto,
        "delta_yoy_pct":          d_yoy,
        "delta_mes_anterior_pct": d_prev,
        "base_calculo":           "venta_recurrente",
        "ventana": {
            "actual":        {"from": cur_from.isoformat(),  "to": (cur_to  - timedelta(days=1)).isoformat(),
                              "ventas_total": k_cur["_total"]["ventas"],  "ventas_recurrente": cur_rec},
            "mes_anterior":  {"from": prev_from.isoformat(), "to": (prev_to - timedelta(days=1)).isoformat(),
                              "ventas_total": k_prev["_total"]["ventas"], "ventas_recurrente": prev_rec},
            "ano_anterior":  {"from": yoy_from.isoformat(),  "to": (yoy_to  - timedelta(days=1)).isoformat(),
                              "ventas_total": k_yoy["_total"]["ventas"],  "ventas_recurrente": yoy_rec},
        },
    }


# ════════════════════════════════════════════════════════════════════════════
# Último día cerrado + semana en curso
# ════════════════════════════════════════════════════════════════════════════

async def _ultimo_dia_cerrado(
    db: AsyncSession, company_id: int, today: date, office_id: Optional[int], tipos_venta: list[int],
    excl: dict,
) -> Optional[dict]:
    """KPIs de ayer (total + recurrente) y comparación vs promedio del mismo DoW
    de las últimas 8 semanas. La detección de "anómalo" usa el z-score sobre
    venta recurrente (sin estacionalidad)."""
    ayer = today - timedelta(days=1)
    _, flt_doc, extra = _office_filters(office_id)

    # Construir filtro inline para FILTER (recurrente)
    rec_terms: list[str] = []
    base_params: dict[str, Any] = {"tipos_venta": tipos_venta, "cid": company_id, **extra}
    if excl.get("depts"):
        base_params["_excl_depts"] = excl["depts"]
        rec_terms.append("COALESCE(vpf.department, '') <> ALL(:_excl_depts)")
    if excl.get("cats"):
        base_params["_excl_cats"] = excl["cats"]
        rec_terms.append("COALESCE(vpf.category, '') <> ALL(:_excl_cats)")
    rec_inline = (" AND " + " AND ".join(rec_terms)) if rec_terms else ""

    q_dia = f"""
        SELECT COALESCE(SUM(dd.total_amount), 0)                                        AS ventas_total,
               COALESCE(SUM(dd.total_amount) FILTER (WHERE TRUE {rec_inline}), 0)       AS ventas_rec,
               COUNT(DISTINCT doc.bsale_document_id)                                    AS tickets
        FROM document_details dd
        JOIN documents doc       ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
        JOIN variants v          ON v.bsale_variant_id    = dd.bsale_variant_id  AND v.company_id   = dd.company_id
        LEFT JOIN v_products_full vpf ON vpf.bsale_product_id = v.bsale_product_id AND vpf.company_id = v.company_id
        WHERE dd.company_id = :cid
          AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE = :dia
          AND COALESCE(doc.is_credit_note, FALSE) = FALSE
          AND doc.bsale_document_type_id = ANY(:tipos_venta)
          AND NOT dd.is_gratuity
          AND {flt_doc}
    """
    r = (await db.execute(text(q_dia), {"dia": ayer, **base_params})).mappings().one()
    ventas_total = float(r["ventas_total"])
    ventas_rec   = float(r["ventas_rec"])
    tickets = int(r["tickets"])
    if tickets == 0 and ventas_total == 0:
        return None

    # Promedio de las últimas 8 semanas del MISMO DoW (sobre RECURRENTE)
    dow_lookback_from = ayer - timedelta(days=8 * 7)
    q_dow = f"""
        SELECT (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE AS dia,
               COALESCE(SUM(dd.total_amount) FILTER (WHERE TRUE {rec_inline}), 0) AS venta
        FROM document_details dd
        JOIN documents doc       ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
        JOIN variants v          ON v.bsale_variant_id    = dd.bsale_variant_id  AND v.company_id   = dd.company_id
        LEFT JOIN v_products_full vpf ON vpf.bsale_product_id = v.bsale_product_id AND vpf.company_id = v.company_id
        WHERE dd.company_id = :cid
          AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= :dfrom
          AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <  :dto
          AND EXTRACT(ISODOW FROM (doc.emission_date AT TIME ZONE '{_TZ_DATE}'))::INT
              = EXTRACT(ISODOW FROM CAST(:dia AS DATE))::INT
          AND COALESCE(doc.is_credit_note, FALSE) = FALSE
          AND doc.bsale_document_type_id = ANY(:tipos_venta)
          AND NOT dd.is_gratuity
          AND {flt_doc}
        GROUP BY 1
        ORDER BY 1
    """
    dow_rows = (await db.execute(
        text(q_dow),
        {"dfrom": dow_lookback_from, "dto": ayer, "dia": ayer, **base_params},
    )).mappings().all()
    ventas_dow = [float(rr["venta"]) for rr in dow_rows]
    if ventas_dow:
        promedio = statistics.mean(ventas_dow)
        std = statistics.stdev(ventas_dow) if len(ventas_dow) > 1 else 0.0
        z_score = ((ventas_rec - promedio) / std) if std > 0 else None
    else:
        promedio, std, z_score = None, None, None

    NAMES = {1: "Lun", 2: "Mar", 3: "Mie", 4: "Jue", 5: "Vie", 6: "Sab", 7: "Dom"}
    dow = ayer.isoweekday()
    return {
        "fecha":                   ayer.isoformat(),
        "dia_semana":              NAMES[dow],
        "ventas":                  round(ventas_total, 2),
        "ventas_recurrente":       round(ventas_rec, 2),
        "tickets":                 tickets,
        "ticket_promedio":         round(ventas_total / tickets, 2) if tickets else 0.0,
        "ventas_promedio_mismo_dow": round(promedio, 2) if promedio is not None else None,
        "delta_vs_promedio_dow_pct": _pct_delta(ventas_rec, promedio) if promedio else None,
        "z_score":                 round(z_score, 2) if z_score is not None else None,
        "anomalo":                 (z_score is not None and z_score <= -1.5),
        "n_dias_comparacion":      len(ventas_dow),
        "base_calculo":            "venta_recurrente",
    }


async def _semana_en_curso(
    db: AsyncSession, company_id: int, today: date, office_id: Optional[int], tipos_venta: list[int],
    excl: dict,
) -> list[dict]:
    """Lunes de la semana en curso hasta ayer (días cerrados), día por día.
    Cada día reporta venta total y recurrente."""
    lunes = today - timedelta(days=today.isoweekday() - 1)
    if lunes >= today:
        return []
    _, flt_doc, extra = _office_filters(office_id)
    params: dict[str, Any] = {"dfrom": lunes, "dto": today, "tipos_venta": tipos_venta, "cid": company_id, **extra}
    rec_terms: list[str] = []
    if excl.get("depts"):
        params["_excl_depts"] = excl["depts"]
        rec_terms.append("COALESCE(vpf.department, '') <> ALL(:_excl_depts)")
    if excl.get("cats"):
        params["_excl_cats"] = excl["cats"]
        rec_terms.append("COALESCE(vpf.category, '') <> ALL(:_excl_cats)")
    rec_inline = (" AND " + " AND ".join(rec_terms)) if rec_terms else ""

    q = f"""
        SELECT (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE AS dia,
               COALESCE(SUM(dd.total_amount), 0)                                AS ventas_total,
               COALESCE(SUM(dd.total_amount) FILTER (WHERE TRUE {rec_inline}), 0) AS ventas_rec,
               COUNT(DISTINCT doc.bsale_document_id)                            AS tickets
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
        GROUP BY 1
        ORDER BY 1
    """
    rows = (await db.execute(text(q), params)).mappings().all()
    by_day = {r["dia"]: r for r in rows}

    NAMES = {1: "Lun", 2: "Mar", 3: "Mie", 4: "Jue", 5: "Vie", 6: "Sab", 7: "Dom"}
    out = []
    d = lunes
    while d < today:
        r = by_day.get(d)
        v_total = float(r["ventas_total"]) if r else 0.0
        v_rec   = float(r["ventas_rec"]) if r else 0.0
        tickets = int(r["tickets"]) if r else 0
        out.append({
            "fecha":              d.isoformat(),
            "dia":                NAMES[d.isoweekday()],
            "ventas":             round(v_total, 2),
            "ventas_recurrente":  round(v_rec, 2),
            "tickets":            tickets,
            "ticket_promedio":    round(v_total / tickets, 2) if tickets else 0.0,
        })
        d += timedelta(days=1)
    return out


# ════════════════════════════════════════════════════════════════════════════
# Últimos 7 días (resumen para enlazar con /diagnosis)
# ════════════════════════════════════════════════════════════════════════════

async def _ultimos_7_dias(
    db: AsyncSession, company_id: int, today: date, office_id: Optional[int], tipos_venta: list[int],
    excl: dict,
) -> dict:
    """Resumen ligero — el detalle está en `/diagnosis`. Devuelve total + recurrente."""
    cur_to   = today
    cur_from = today - timedelta(days=7)
    week_from = cur_from - timedelta(days=7)
    week_to   = cur_from
    yoy_from  = cur_from - timedelta(days=364)
    yoy_to    = yoy_from + timedelta(days=7)

    k_cur  = await _period_kpis(db, company_id, cur_from,  cur_to,  office_id, tipos_venta, excl)
    k_week = await _period_kpis(db, company_id, week_from, week_to, office_id, tipos_venta, excl)
    k_yoy  = await _period_kpis(db, company_id, yoy_from,  yoy_to,  office_id, tipos_venta, excl)

    cur_t = k_cur["_total"]; cur_r = k_cur["_recurrente"]
    return {
        "from":                              cur_from.isoformat(),
        "to":                                (cur_to - timedelta(days=1)).isoformat(),
        "ventas":                            cur_t["ventas"],
        "ventas_recurrente":                 cur_r["ventas"],
        "tickets":                           cur_t["tickets"],
        "ticket_promedio":                   cur_t["ticket_promedio"],
        "delta_vs_semana_anterior_pct_total":      _pct_delta(cur_t["ventas"], k_week["_total"]["ventas"]),
        "delta_vs_semana_anterior_pct_recurrente": _pct_delta(cur_r["ventas"], k_week["_recurrente"]["ventas"]),
        "delta_vs_ano_anterior_pct_total":         _pct_delta(cur_t["ventas"], k_yoy["_total"]["ventas"]),
        "delta_vs_ano_anterior_pct_recurrente":    _pct_delta(cur_r["ventas"], k_yoy["_recurrente"]["ventas"]),
    }


# ════════════════════════════════════════════════════════════════════════════
# Alertas (top 3-5 cosas urgentes)
# ════════════════════════════════════════════════════════════════════════════

_SEVERIDAD_RANK = {"CRITICA": 0, "ALTA": 1, "MEDIA": 2}


async def _alertas(
    db: AsyncSession, company_id: int, today: date, office_id: Optional[int], tipos_venta: list[int],
    ult_dia_cerrado: Optional[dict], excl: dict,
) -> list[dict]:
    """Junta alertas de varios detectores, ordena por severidad + impacto.
    Las detecciones se hacen sobre venta RECURRENTE (sin estacionales) para
    evitar falsos positivos por fechas que ya pasaron."""
    alertas: list[dict] = []

    # 1) Quiebres críticos (top 3 SKUs con mayor venta perdida estimada)
    cur_from = today - timedelta(days=7)
    quiebres = await _lost_sales_from_stockouts(db, company_id, {"from": cur_from, "to": today}, office_id, tipos_venta, excl)
    top_skus = quiebres["top_skus"][:3]
    if top_skus:
        if quiebres["monto_estimado_pen"] >= 500:
            alertas.append({
                "severidad":       "CRITICA",
                "tipo":            "quiebres_alta_rotacion",
                "titulo":          f"{quiebres['skus_con_perdida']} SKUs en quiebre con demanda activa",
                "detalle":         f"Venta perdida estimada últimos 7 días: S/ {quiebres['monto_estimado_pen']:,.0f}.",
                "impacto_pen":     -quiebres["monto_estimado_pen"],
                "accion_sugerida": "Revisar reposición urgente — listado en /diagnosis.factores_adicionales",
                "skus":            [s["sku"] for s in top_skus],
            })

    # 2) Categorías que cayeron fuerte últimos 7d vs los 7 anteriores (recurrente)
    cur = {"from": today - timedelta(days=7), "to": today}
    prev = {"from": today - timedelta(days=14), "to": today - timedelta(days=7)}
    cats = await _decomp_by_category(db, company_id, cur, prev, office_id, tipos_venta, top_n=20, excl=excl)
    for c in cats:
        if c["delta_pct"] is not None and c["delta_pct"] <= -30 and c["delta_abs"] <= -800:
            alertas.append({
                "severidad":   "ALTA",
                "tipo":        "categoria_cayendo",
                "titulo":      f"{c['categoria']} cayó {abs(c['delta_pct']):.0f}%",
                "detalle":     f"S/ {c['ventas_actual']:,.0f} vs S/ {c['ventas_prev']:,.0f} la semana anterior ({c['departamento']}).",
                "impacto_pen": c["delta_abs"],
                "accion_sugerida": "Revisar stock, precio o cambio de demanda en /diagnosis",
            })
            if len([a for a in alertas if a["tipo"] == "categoria_cayendo"]) >= 2:
                break

    # 3) Día anómalo (z-score ≤ -1.5 en ayer)
    if ult_dia_cerrado and ult_dia_cerrado.get("anomalo"):
        delta = ult_dia_cerrado.get("delta_vs_promedio_dow_pct")
        alertas.append({
            "severidad":   "MEDIA",
            "tipo":        "dia_anomalo",
            "titulo":      f"{ult_dia_cerrado['dia_semana']} bajó {abs(delta):.0f}% vs el promedio",
            "detalle":     (
                f"Ayer vendiste S/ {ult_dia_cerrado['ventas']:,.0f}; "
                f"el promedio de los últimos 8 {ult_dia_cerrado['dia_semana']} fue "
                f"S/ {ult_dia_cerrado['ventas_promedio_mismo_dow']:,.0f}."
            ),
            "impacto_pen": ult_dia_cerrado["ventas"] - (ult_dia_cerrado["ventas_promedio_mismo_dow"] or 0),
            "accion_sugerida": "Verificar si hubo evento o quiebre puntual ese día.",
        })

    # 4) Devoluciones spike (>=50% más que ventana anterior y monto > 500)
    flt_plain, _, extra = _office_filters(office_id)
    q_dev = f"""
        SELECT COALESCE(SUM(total_amount), 0) AS monto
        FROM documents
        WHERE company_id = :cid
          AND (emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= :dfrom
          AND (emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <  :dto
          AND COALESCE(is_credit_note, FALSE) = TRUE
          AND {flt_plain}
    """
    dev_cur  = float((await db.execute(text(q_dev), {"cid": company_id, "dfrom": cur["from"],  "dto": cur["to"],  **extra})).scalar() or 0)
    dev_prev = float((await db.execute(text(q_dev), {"cid": company_id, "dfrom": prev["from"], "dto": prev["to"], **extra})).scalar() or 0)
    if dev_cur >= 500 and dev_prev > 0 and dev_cur >= dev_prev * 1.5:
        alertas.append({
            "severidad":   "ALTA",
            "tipo":        "devoluciones_spike",
            "titulo":      f"Devoluciones subieron {((dev_cur/dev_prev - 1) * 100):.0f}%",
            "detalle":     f"Últimos 7 días: S/ {dev_cur:,.0f} (vs S/ {dev_prev:,.0f} la semana anterior).",
            "impacto_pen": -(dev_cur - dev_prev),
            "accion_sugerida": "Revisar si hay un lote defectuoso o cambio de criterio en post-venta.",
        })

    # Ordenar por severidad, después por |impacto|, devolver top 5
    alertas.sort(key=lambda a: (_SEVERIDAD_RANK.get(a["severidad"], 99), -abs(a.get("impacto_pen") or 0)))
    return alertas[:5]


# ════════════════════════════════════════════════════════════════════════════
# Endpoint principal
# ════════════════════════════════════════════════════════════════════════════

@router.get("")
async def pulse(
    office_id: Optional[int] = Query(None, description="ID de sucursal (vacío = todas)."),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Pulso del negocio — el vistazo de 10 segundos.

    Devuelve estado del mes vs meta, veredicto del momento, último día cerrado,
    semana en curso, últimos 7 días resumido y top 3-5 alertas.
    """
    today = date.today()

    cid = company.company_id
    company_cfg = await get_company(db, cid)
    tipos_venta = company_cfg.get("tipos_venta") or DEFAULT_TIPOS_VENTA
    excl = await _load_exclusions(db, cid)

    mes       = await _mes_en_curso(db, cid, today, office_id, tipos_venta, excl)
    veredicto = await _veredicto_mes(db, cid, today, office_id, tipos_venta, excl)
    ult_dia   = await _ultimo_dia_cerrado(db, cid, today, office_id, tipos_venta, excl)
    semana    = await _semana_en_curso(db, cid, today, office_id, tipos_venta, excl)
    ult7      = await _ultimos_7_dias(db, cid, today, office_id, tipos_venta, excl)
    alertas   = await _alertas(db, cid, today, office_id, tipos_venta, ult_dia, excl)

    last_sync = await db.scalar(
        text(
            "SELECT MAX(finished_at) FROM sync_log "
            "WHERE company_id = :cid AND entity IN ('documents','document_details') AND status='OK'"
        ),
        {"cid": cid},
    )

    # Cobertura de costos — pegado a la métrica de últ. 7d
    from app.routers.diagnosis import _period_kpis as _kp
    k7 = await _kp(db, cid, today - timedelta(days=7), today, office_id, tipos_venta, excl)
    cobertura_pct = k7["_total"]["cobertura_costos_pct"]
    cobertura_warn = None
    if cobertura_pct < 90:
        cobertura_warn = (
            f"⚠️ Solo {cobertura_pct}% de la venta tiene costo cargado — el margen "
            f"reportado solo refleja esos SKUs. Correr POST /config/variant-costs/"
            f"backfill-from-receptions para recuperar."
        )

    return {
        "meta": {
            "fecha":              today.isoformat(),
            "office_id":          office_id,
            "office_scope":       [office_id] if office_id is not None else list(OFFICE_IDS),
            "hoy_excluido":       True,
            "ultimo_dia_cerrado": ult_dia["fecha"] if ult_dia else None,
            "datos_sync_hasta":   last_sync.isoformat() if last_sync else None,
            "generado_at":        datetime.utcnow().isoformat() + "Z",
            "exclusiones": {
                "departamentos": excl.get("depts", []),
                "categorias":    excl.get("cats", []),
                "nota": "Las alertas/drivers operan sobre venta RECURRENTE. Las metas y avance % se calculan sobre venta TOTAL (lo que el dueño efectivamente vendió).",
            },
            "cobertura_costos": {
                "pct_actual":     cobertura_pct,
                "estado":         "OK" if cobertura_pct >= 90 else ("ADVERTENCIA" if cobertura_pct >= 70 else "CRITICA"),
                "warning":        cobertura_warn,
            },
        },
        "mes_en_curso":        mes,
        "veredicto":           veredicto,
        "ultimo_dia_cerrado":  ult_dia,
        "semana_en_curso":     semana,
        "ultimos_7_dias":      ult7,
        "alertas":             alertas,
    }
