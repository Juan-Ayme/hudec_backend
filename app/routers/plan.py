"""Vista 4 — Plan del mes (¿llegaré a la meta y cómo planifico el próximo?).

Endpoint `GET /plan` que responde decisiones de planificación, no de
operación diaria. Pensado para revisión mensual al cierre / al inicio del mes.

Secciones del payload:

  1. mes_en_curso             : proyección lineal vs meta cargada + estado.
                                Si NO hay meta cargada → solo proyección.
  2. sugerencia_proximo_mes   : 3 niveles (conservadora / realista / agresiva)
                                basados en YoY + crecimiento promedio reciente.
  3. pacing_semanal           : distribución semanal sugerida del próximo mes,
                                replicando la proporción semanal del mismo mes
                                del año pasado.
  4. calendario_campanas      : proyección de los próximos 6 meses con meta
                                sugerida + categoría protagonista de cada mes
                                (replica el §4 del Formato Kawii Pluss).
  5. presupuesto_compra       : presupuesto sugerido para el próximo mes
                                basado en venta proyectada × (1 - margen).
  6. resumen                  : 3-5 bullets narrativos.

Convención:
- La meta del dueño es de venta TOTAL (incluye estacional). La proyección
  y comparaciones contra meta usan venta total.
- La sugerencia de meta se calcula con venta TOTAL (es lo que el dueño quiere
  como objetivo absoluto), pero el "crecimiento" interno (tendencia YoY) se
  calcula con venta recurrente (sin estacional), para que un Día del Padre
  no infle artificialmente la tendencia esperada.
- gap_a_meta = venta − meta (negativo = falta para llegar). Misma convención
  de signo que /pulse y /salud-catalogo.
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
from app.routers.config_admin import get_company, get_goals, goal_for_month
from app.routers.diagnosis import (
    _load_exclusions,
    _office_filters,
    _pct_delta,
    _period_kpis,
    _TZ_DATE,
)
from harvester.config import TIPOS_VENTA as DEFAULT_TIPOS_VENTA


router = APIRouter(
    prefix="/plan",
    tags=["plan"],
    dependencies=[Depends(get_current_company)],
)


_MES_NOMBRES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
    7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}

# Heurística simple de "evento estacional" por mes — usada para etiquetar el
# calendario. Cuando se cargue category_targets, podemos detectar campañas
# desde la data (categorías que pican fuerte solo ciertos meses).
_CAMPANAS_MES = {
    1:  "Campaña verano / post-fiestas",
    2:  "Estable / regreso a clases (inicio)",
    3:  "Regreso a clases pico",
    4:  "Estable",
    5:  "Día de la Madre",
    6:  "Día del Padre",
    7:  "Back to school + Invierno",
    8:  "Back to school pico",
    9:  "Estable",
    10: "Hacia primavera",
    11: "Pre-Navidad",
    12: "Navidad + fin de año",
}


# ════════════════════════════════════════════════════════════════════════════
# Helpers de meses
# ════════════════════════════════════════════════════════════════════════════

def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """Devuelve (year, month) desplazado `delta` meses (puede ser negativo)."""
    idx = year * 12 + (month - 1) + delta
    return (idx // 12, idx % 12 + 1)


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    """Primer y último día del mes (inclusivos)."""
    return (date(year, month, 1), date(year, month, monthrange(year, month)[1]))


def _mes_str(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


# ════════════════════════════════════════════════════════════════════════════
# 1) Proyección del mes en curso vs meta
# ════════════════════════════════════════════════════════════════════════════

async def _proyeccion_mes_en_curso(
    db: AsyncSession, company_id: int, today: date, office_id: Optional[int],
    tipos_venta: list[int], excl: dict,
) -> dict:
    """Toma el mes en curso, calcula venta acumulada y proyecta cierre lineal.
    Compara contra meta cargada en app_config (si existe).
    """
    year, mon = today.year, today.month
    dias_del_mes = monthrange(year, mon)[1]
    if today.day == 1:
        return {"mes": _mes_str(year, mon), "dias_transcurridos": 0,
                "nota": "Mes recién empezado — sin proyección."}

    first = date(year, mon, 1)
    corte = today - timedelta(days=1)
    dias_trans = corte.day
    dias_restantes = dias_del_mes - dias_trans
    dto = corte + timedelta(days=1)

    k = await _period_kpis(db, company_id, first, dto, office_id, tipos_venta, excl)
    venta_total = k["_total"]["ventas"]
    venta_rec   = k["_recurrente"]["ventas"]

    # Meta del mes (desde app_config)
    mes_str = _mes_str(year, mon)
    goals = await get_goals(db, company_id)
    month_goals, fuente = goal_for_month(goals, mes_str)
    scope = [office_id] if office_id is not None else list(OFFICE_IDS)

    def _meta_val(key: str) -> Optional[float]:
        v = month_goals.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    if office_id is not None:
        meta = _meta_val(str(office_id))
    else:
        meta = _meta_val("global")
        if meta is None:
            sub = [_meta_val(str(oid)) for oid in scope]
            meta = sum(x for x in sub if x is not None) or None

    proy_lineal = venta_total / dias_trans * dias_del_mes if dias_trans else None
    gap = (venta_total - meta) if meta is not None else None  # venta − meta (negativo = falta)
    venta_diaria_actual = venta_total / dias_trans if dias_trans else 0
    venta_diaria_necesaria = (
        (meta - venta_total) / dias_restantes
        if (meta is not None and dias_restantes > 0 and venta_total < meta)
        else None
    )
    ritmo_multiplo = (
        venta_diaria_necesaria / venta_diaria_actual
        if (venta_diaria_necesaria is not None and venta_diaria_actual > 0)
        else None
    )

    if meta is None:
        estado = "SIN_META"
    elif venta_total >= meta:
        estado = "META_CUMPLIDA"
    elif proy_lineal is None:
        estado = "INDETERMINADO"
    elif proy_lineal >= meta * 0.95:
        estado = "EN_RITMO"
    elif proy_lineal >= meta * 0.80:
        estado = "ATRASADO_LEVE"
    else:
        estado = "RIESGO_NO_LLEGAR"

    return {
        "mes":                    mes_str,
        "dias_transcurridos":     dias_trans,
        "dias_del_mes":           dias_del_mes,
        "dias_restantes":         dias_restantes,
        "ultimo_dia_cerrado":     corte.isoformat(),
        "venta_acumulada":        round(venta_total, 2),
        "venta_acumulada_recurrente": round(venta_rec, 2),
        "venta_diaria_promedio":  round(venta_diaria_actual, 2),
        "meta":                   meta,
        "meta_source":            fuente,
        "gap_a_meta":             round(gap, 2) if gap is not None else None,
        "proyeccion_lineal":      round(proy_lineal, 2) if proy_lineal is not None else None,
        "estado":                 estado,
        "venta_diaria_necesaria": round(venta_diaria_necesaria, 2) if venta_diaria_necesaria else None,
        "ritmo_necesario_multiplo": round(ritmo_multiplo, 2) if ritmo_multiplo else None,
    }


# ════════════════════════════════════════════════════════════════════════════
# 2) Sugerencia de meta para el próximo mes (3 niveles)
# ════════════════════════════════════════════════════════════════════════════

async def _venta_mes_completo(
    db: AsyncSession, company_id: int, year: int, month: int, office_id: Optional[int],
    tipos_venta: list[int], excl: dict,
) -> dict:
    """Venta total de un mes completo (1° → último día), total y recurrente."""
    first, last = _month_bounds(year, month)
    dto = last + timedelta(days=1)
    k = await _period_kpis(db, company_id, first, dto, office_id, tipos_venta, excl)
    return {
        "total":      k["_total"]["ventas"],
        "recurrente": k["_recurrente"]["ventas"],
        "tickets":    k["_total"]["tickets"],
    }


async def _sugerencia_meta(
    db: AsyncSession, company_id: int, target_year: int, target_month: int,
    office_id: Optional[int], tipos_venta: list[int], excl: dict,
) -> dict:
    """Sugiere meta para `target_year-target_month` en 3 niveles.

    Método:
      - Base = venta TOTAL del mismo mes el año anterior (target − 12m).
      - Crecimiento = promedio del crecimiento YoY (recurrente) de los últimos
        3 meses cerrados. Si no hay datos suficientes, se usa 0%.
      - Meta conservadora = base × (1 + max(0, crecimiento))
      - Meta realista     = base × (1 + crecimiento + 5pp)
      - Meta agresiva     = max(base × (1 + crecimiento + 15pp),
                                mejor_mes_historico_target)
    """
    today = date.today()
    yoy_year, yoy_month = target_year - 1, target_month

    # Base YoY del mes objetivo
    base = await _venta_mes_completo(db, company_id, yoy_year, yoy_month, office_id, tipos_venta, excl)
    base_total = base["total"]

    # Crecimiento promedio: últimos 3 meses cerrados (ej. si hoy es 29-jun,
    # tomamos mayo, abril, marzo) y comparamos con sus YoY (mayo-25, abril-25, marzo-25)
    # — usamos recurrente para que estacionales puntuales no inflen.
    growth_samples: list[float] = []
    for delta in (1, 2, 3):
        y, m = _shift_month(today.year, today.month, -delta)
        # Saltar mes parcial: si justo es el mes en curso, ya no entra (delta>=1 lo descarta).
        # Pero si el mes a medir es el mes actual con < cierre, lo saltamos.
        last_day = monthrange(y, m)[1]
        if (y, m) == (today.year, today.month):
            continue
        if today < date(y, m, last_day):
            continue
        cur_mes = await _venta_mes_completo(db, company_id, y, m, office_id, tipos_venta, excl)
        yy, ym = y - 1, m
        prev_mes = await _venta_mes_completo(db, company_id, yy, ym, office_id, tipos_venta, excl)
        if prev_mes["recurrente"] > 0:
            g = (cur_mes["recurrente"] - prev_mes["recurrente"]) / prev_mes["recurrente"]
            growth_samples.append(g)

    growth = (sum(growth_samples) / len(growth_samples)) if growth_samples else 0.0

    # Mejor mes histórico del mismo número (target_month) en los últimos 3 años
    mejor_historico = base_total
    for back in range(1, 4):
        y, m = target_year - back, target_month
        v = await _venta_mes_completo(db, company_id, y, m, office_id, tipos_venta, excl)
        if v["total"] > mejor_historico:
            mejor_historico = v["total"]

    g_pos = max(0.0, growth)
    conservadora = base_total * (1 + g_pos)
    realista     = base_total * (1 + growth + 0.05)
    agresiva     = max(base_total * (1 + growth + 0.15), mejor_historico)

    return {
        "mes_objetivo":                _mes_str(target_year, target_month),
        "metodo":                      "yoy_mas_crecimiento_3m_promedio",
        "venta_yoy_mismo_mes":         round(base_total, 2),
        "crecimiento_yoy_3m_pct":      round(growth * 100, 2),
        "muestras_crecimiento":        len(growth_samples),
        "mejor_mes_historico":         round(mejor_historico, 2),
        "meta_conservadora":           round(conservadora, 0),
        "meta_realista":               round(realista, 0),
        "meta_agresiva":               round(agresiva, 0),
        "recomendacion":               "realista",
    }


# ════════════════════════════════════════════════════════════════════════════
# 3) Pacing semanal del próximo mes (replica la distribución semanal YoY)
# ════════════════════════════════════════════════════════════════════════════

async def _pacing_semanal(
    db: AsyncSession, company_id: int, target_year: int, target_month: int, meta_total: float,
    office_id: Optional[int], tipos_venta: list[int], excl: dict,
) -> dict:
    """Distribuye `meta_total` en semanas del mes objetivo según cómo se
    distribuyó la venta del mismo mes hace 12 meses.

    Si no hay venta YoY, distribuye proporcional a la cantidad de días.
    """
    first, last = _month_bounds(target_year, target_month)
    dias_del_mes = last.day

    yoy_year = target_year - 1
    yoy_first, yoy_last = _month_bounds(yoy_year, target_month)

    flt_plain, _, extra = _office_filters(office_id)
    q = f"""
        SELECT (emission_date AT TIME ZONE '{_TZ_DATE}')::DATE AS dia,
               COALESCE(SUM(total_amount), 0) AS venta
        FROM documents
        WHERE company_id = :cid
          AND (emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= :dfrom
          AND (emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <= :dto
          AND COALESCE(is_credit_note, FALSE) = FALSE
          AND bsale_document_type_id = ANY(:tipos_venta)
          AND {flt_plain}
        GROUP BY 1
    """
    params = {"dfrom": yoy_first, "dto": yoy_last, "tipos_venta": tipos_venta, "cid": company_id, **extra}
    rows = (await db.execute(text(q), params)).mappings().all()
    venta_por_dia_yoy = {r["dia"]: float(r["venta"]) for r in rows}
    total_yoy = sum(venta_por_dia_yoy.values())

    # Particionar el mes en semanas (lunes-domingo, las parciales en los extremos)
    semanas: list[dict] = []
    d = first
    sem_num = 1
    while d <= last:
        # fin de semana del bloque actual = próximo domingo o último día del mes
        days_to_sunday = 7 - d.isoweekday()
        end = min(last, d + timedelta(days=days_to_sunday))
        # Calcular % del mes según YoY (mapeando día a día del mes hacia YoY)
        dias_bloque = (end - d).days + 1
        # YoY del mismo bloque (mismo número de día → en YoY)
        try:
            yoy_block_from = date(yoy_year, target_month, d.day)
        except ValueError:
            yoy_block_from = yoy_first
        try:
            yoy_block_to = date(yoy_year, target_month, end.day)
        except ValueError:
            yoy_block_to = yoy_last
        yoy_block_total = sum(
            v for dd, v in venta_por_dia_yoy.items()
            if yoy_block_from <= dd <= yoy_block_to
        )
        if total_yoy > 0:
            pct = yoy_block_total / total_yoy
        else:
            pct = dias_bloque / dias_del_mes
        meta_semana = meta_total * pct
        semanas.append({
            "sem":      sem_num,
            "from":     d.isoformat(),
            "to":       end.isoformat(),
            "dias":     dias_bloque,
            "pct_mes":  round(pct * 100, 1),
            "meta":     round(meta_semana, 2),
            "yoy_venta": round(yoy_block_total, 2),
        })
        d = end + timedelta(days=1)
        sem_num += 1

    # Normalizar para que sumen exacto al meta_total (corrige redondeo)
    suma = sum(s["meta"] for s in semanas)
    if suma > 0 and abs(suma - meta_total) > 0.5:
        factor = meta_total / suma
        for s in semanas:
            s["meta"] = round(s["meta"] * factor, 2)

    return {
        "mes":              _mes_str(target_year, target_month),
        "meta_total":       round(meta_total, 2),
        "metodo":           "dist_yoy_misma_semana" if total_yoy > 0 else "proporcional_dias",
        "venta_yoy_total":  round(total_yoy, 2),
        "semanas":          semanas,
    }


# ════════════════════════════════════════════════════════════════════════════
# 4) Calendario de campañas — proyección 6 meses adelante
# ════════════════════════════════════════════════════════════════════════════

async def _categoria_protagonista_mes(
    db: AsyncSession, company_id: int, year: int, month: int, office_id: Optional[int],
    tipos_venta: list[int], excl: dict,
) -> Optional[dict]:
    """Devuelve la categoría con más venta en ese mes histórico (target − 12m).
    Excluye estacionales — buscamos la categoría motor de fondo."""
    yoy_year = year - 1
    first, last = _month_bounds(yoy_year, month)
    dto = last + timedelta(days=1)

    _, flt_doc, extra = _office_filters(office_id)
    params: dict[str, Any] = {"dfrom": first, "dto": dto, "tipos_venta": tipos_venta, "cid": company_id, **extra}
    excl_terms: list[str] = []
    if excl.get("depts"):
        params["_excl_depts"] = excl["depts"]
        excl_terms.append("AND COALESCE(vpf.department, '') <> ALL(:_excl_depts)")
    if excl.get("cats"):
        params["_excl_cats"] = excl["cats"]
        excl_terms.append("AND COALESCE(vpf.category, '') <> ALL(:_excl_cats)")
    excl_clause = "\n          ".join(excl_terms)

    q = f"""
        SELECT vpf.department, vpf.category,
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
          {excl_clause}
        GROUP BY vpf.department, vpf.category
        ORDER BY venta DESC
        LIMIT 1
    """
    r = (await db.execute(text(q), params)).mappings().first()
    if not r or not r["venta"]:
        return None
    return {
        "departamento": r["department"],
        "categoria":    r["category"],
        "venta_yoy":    round(float(r["venta"]), 2),
    }


async def _calendario_campanas(
    db: AsyncSession, company_id: int, today: date, office_id: Optional[int],
    tipos_venta: list[int], excl: dict, meses_adelante: int = 6,
) -> list[dict]:
    """Para cada uno de los próximos N meses, devuelve:
       - meta_sugerida (3 niveles)
       - venta YoY del mismo mes
       - categoría protagonista (motor histórico de ese mes)
       - campaña tag (heurística por mes)
    """
    out: list[dict] = []
    # Empezar desde el PRÓXIMO mes (no incluye el actual — ése está en mes_en_curso)
    base_y, base_m = _shift_month(today.year, today.month, 1)
    for i in range(meses_adelante):
        y, m = _shift_month(base_y, base_m, i)
        sug   = await _sugerencia_meta(db, company_id, y, m, office_id, tipos_venta, excl)
        protag = await _categoria_protagonista_mes(db, company_id, y, m, office_id, tipos_venta, excl)
        out.append({
            "mes":                 _mes_str(y, m),
            "mes_nombre":          _MES_NOMBRES[m],
            "campana_principal":   _CAMPANAS_MES.get(m, "—"),
            "venta_yoy":           sug["venta_yoy_mismo_mes"],
            "meta_conservadora":   sug["meta_conservadora"],
            "meta_realista":       sug["meta_realista"],
            "meta_agresiva":       sug["meta_agresiva"],
            "categoria_protagonista": protag,
        })
    return out


# ════════════════════════════════════════════════════════════════════════════
# 5) Presupuesto sugerido de compra para el próximo mes
# ════════════════════════════════════════════════════════════════════════════

async def _presupuesto_sugerido(
    db: AsyncSession, company_id: int, target_year: int, target_month: int, meta_total: float,
    office_id: Optional[int], tipos_venta: list[int], excl: dict,
) -> dict:
    """Calcula presupuesto sugerido de compra para alcanzar `meta_total`.

    Fórmula:
      costo_estimado = meta_total × (1 - margen_promedio_historico)
      presupuesto    = costo_estimado × factor_cobertura

    Donde:
      - margen_promedio_historico = margen recurrente de los últimos 3 meses cerrados
      - factor_cobertura = 1.0 (cubrís exactamente lo que vendés)

    Si hay filas en `category_targets`, agrega un desglose por categoría motor:
      - distribuye el presupuesto total proporcional a meta_mensual_pen de cada categoría
      - usa el margen_objetivo_pct de la fila para calcular cuánto comprar de esa categoría
    """
    today = date.today()
    margenes: list[float] = []
    for delta in (1, 2, 3):
        y, m = _shift_month(today.year, today.month, -delta)
        if (y, m) == (today.year, today.month):
            continue
        first, last = _month_bounds(y, m)
        if today < last:
            continue
        k = await _period_kpis(db, company_id, first, last + timedelta(days=1), office_id, tipos_venta, excl)
        if k["_recurrente"]["margen_pct"] > 0:
            margenes.append(k["_recurrente"]["margen_pct"] / 100.0)
    margen_prom = (sum(margenes) / len(margenes)) if margenes else 0.30

    costo_estimado = meta_total * (1 - margen_prom)
    presupuesto = costo_estimado  # factor_cobertura = 1.0

    # ── Desglose por categoría motor (si hay targets cargados) ────────────
    desglose = await _desglose_por_motor(db, company_id, office_id, meta_total)

    base = {
        "mes_objetivo":         _mes_str(target_year, target_month),
        "meta_venta":           round(meta_total, 2),
        "margen_promedio_pct":  round(margen_prom * 100, 1),
        "muestras_margen":      len(margenes),
        "costo_estimado_pen":   round(costo_estimado, 2),
        "presupuesto_compra_pen": round(presupuesto, 2),
    }
    if desglose:
        base["desglose_por_categoria"] = desglose
        base["nota"] = (
            "Desglose por categoría motor activo — usa category_targets "
            "(meta_mensual_pen + margen_objetivo_pct por fila)."
        )
    else:
        base["desglose_por_categoria"] = None
        base["nota"] = (
            "Sin category_targets cargados — solo total. Correr "
            "POST /config/category-targets/bootstrap para activar el desglose."
        )
    return base


async def _desglose_por_motor(
    db: AsyncSession, company_id: int, office_id: Optional[int], meta_total_proyectada: float,
) -> Optional[list[dict]]:
    """Distribuye `meta_total_proyectada` entre categorías motor proporcional
    a la suma de sus metas mensuales. Para cada categoría calcula:
      - cuota_meta_venta_pen      → porción de meta_total que le toca
      - costo_estimado_pen        → cuota × (1 - margen_objetivo_pct/100)
      - presupuesto_compra_pen    → costo_estimado (factor cobertura 1.0)

    Si margen_objetivo_pct no está cargado, usa 30% como default.
    Devuelve None si la tabla está vacía.
    """
    where = "WHERE ct.company_id = :cid AND ct.meta_mensual_pen IS NOT NULL AND ct.meta_mensual_pen > 0"
    params: dict[str, Any] = {"cid": company_id}
    if office_id is not None:
        where += " AND ct.bsale_office_id = :office_id"
        params["office_id"] = office_id

    q = f"""
        SELECT ct.category_id, c.name AS categoria, d.name AS departamento,
               ct.bsale_office_id, o.name AS sucursal,
               ct.rol, ct.meta_mensual_pen,
               COALESCE(ct.margen_objetivo_pct, 30.0) AS margen_objetivo_pct
        FROM category_targets ct
        JOIN categories c     ON c.id = ct.category_id AND c.company_id = ct.company_id
        LEFT JOIN departments d ON d.id = c.department_id AND d.company_id = c.company_id
        LEFT JOIN offices o   ON o.bsale_office_id = ct.bsale_office_id AND o.company_id = ct.company_id
        {where}
        ORDER BY ct.bsale_office_id, ct.meta_mensual_pen DESC
    """
    rows = (await db.execute(text(q), params)).mappings().all()
    if not rows:
        return None

    suma_metas = sum(float(r["meta_mensual_pen"]) for r in rows)
    if suma_metas <= 0:
        return None

    items: list[dict] = []
    for r in rows:
        meta_cat = float(r["meta_mensual_pen"])
        margen_cat = float(r["margen_objetivo_pct"]) / 100.0
        share = meta_cat / suma_metas
        cuota_venta = meta_total_proyectada * share
        costo = cuota_venta * (1 - margen_cat)
        items.append({
            "category_id":           int(r["category_id"]),
            "categoria":             r["categoria"],
            "departamento":          r["departamento"],
            "office_id":             int(r["bsale_office_id"]),
            "sucursal":              r["sucursal"],
            "rol":                   r["rol"],
            "meta_mensual_categoria": round(meta_cat, 2),
            "share_del_total_pct":   round(share * 100, 2),
            "cuota_meta_venta_pen":  round(cuota_venta, 2),
            "margen_objetivo_pct":   round(margen_cat * 100, 1),
            "costo_estimado_pen":    round(costo, 2),
            "presupuesto_compra_pen": round(costo, 2),
        })
    return items


# ════════════════════════════════════════════════════════════════════════════
# Cobertura de costos — para el meta
# ════════════════════════════════════════════════════════════════════════════

def _cobertura_meta_block(kpis_30d: dict) -> dict:
    pct = kpis_30d["_total"]["cobertura_costos_pct"]
    estado = "OK" if pct >= 90 else ("ADVERTENCIA" if pct >= 70 else "CRITICA")
    warning = None
    if pct < 90:
        warning = (
            f"⚠️ Solo {pct}% de la venta tiene costo cargado — el presupuesto sugerido "
            f"está distorsionado. Correr POST /config/variant-costs/backfill-from-receptions."
        )
    return {"pct_actual": pct, "estado": estado, "warning": warning}


# ════════════════════════════════════════════════════════════════════════════
# Narrativa
# ════════════════════════════════════════════════════════════════════════════

def _build_narrative(payload: dict) -> list[str]:
    out: list[str] = []
    mc = payload["mes_en_curso"]
    if mc.get("dias_transcurridos", 0) > 0:
        if mc.get("meta") is not None:
            estado = mc["estado"]
            if estado == "META_CUMPLIDA":
                out.append(f"✅ Meta de {mc['mes']} cumplida: vendiste S/ {mc['venta_acumulada']:,.0f} de S/ {mc['meta']:,.0f}.")
            elif estado == "EN_RITMO":
                out.append(f"📈 Vas en ritmo de cumplir S/ {mc['meta']:,.0f} en {mc['mes']} (proyección: S/ {mc['proyeccion_lineal']:,.0f}).")
            elif estado == "ATRASADO_LEVE":
                out.append(
                    f"⚠️ Atrasado leve en {mc['mes']}: proyección S/ {mc['proyeccion_lineal']:,.0f} vs meta S/ {mc['meta']:,.0f}. "
                    f"Necesitás S/ {mc['venta_diaria_necesaria']:,.0f}/día por {mc['dias_restantes']} días."
                )
            else:  # RIESGO_NO_LLEGAR
                out.append(
                    f"🚨 Riesgo de no llegar en {mc['mes']}: proyección S/ {mc['proyeccion_lineal']:,.0f} vs meta S/ {mc['meta']:,.0f}. "
                    f"Para llegar necesitás vender {mc['ritmo_necesario_multiplo']}× el promedio actual ({mc['venta_diaria_necesaria']:,.0f}/día)."
                )
        else:
            out.append(f"Mes {mc['mes']} sin meta cargada — vas acumulando S/ {mc['venta_acumulada']:,.0f}, proyección de cierre S/ {mc['proyeccion_lineal']:,.0f}.")

    sug = payload["sugerencia_proximo_mes"]
    out.append(
        f"Para {sug['mes_objetivo']} sugerimos meta realista de S/ {sug['meta_realista']:,.0f} "
        f"(YoY: S/ {sug['venta_yoy_mismo_mes']:,.0f}, crecimiento 3m: {sug['crecimiento_yoy_3m_pct']:+.1f}%)."
    )

    cal = payload["calendario_campanas"]
    if cal:
        # Mes con mayor meta proyectada (pico)
        top = max(cal, key=lambda c: c["meta_realista"])
        out.append(
            f"Pico proyectado del semestre: {top['mes_nombre']} con S/ {top['meta_realista']:,.0f} "
            f"({top['campana_principal']})."
        )
        # Categoría protagonista
        protags = [c["categoria_protagonista"] for c in cal if c["categoria_protagonista"]]
        if protags:
            top_cat = max(protags, key=lambda p: p["venta_yoy"])
            out.append(f"Categoría motor del semestre: {top_cat['categoria']} ({top_cat['departamento']}).")

    pres = payload["presupuesto_compra"]
    out.append(
        f"Presupuesto sugerido para {pres['mes_objetivo']}: S/ {pres['presupuesto_compra_pen']:,.0f} "
        f"(con margen promedio {pres['margen_promedio_pct']}%)."
    )

    return out


# ════════════════════════════════════════════════════════════════════════════
# Endpoint principal
# ════════════════════════════════════════════════════════════════════════════

@router.get("")
async def plan(
    office_id: Optional[int] = Query(None, description="ID de sucursal (vacío = todas)."),
    meses_calendario: int = Query(6, ge=1, le=12, description="Cuántos meses adelante incluir en el calendario."),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Plan del mes — proyección, sugerencia próximo mes, calendario y presupuesto.

    Pensado para revisión mensual (cierre/inicio de mes), no diaria.
    """
    today = date.today()
    cid = company.company_id
    company_cfg = await get_company(db, cid)
    tipos_venta = company_cfg.get("tipos_venta") or DEFAULT_TIPOS_VENTA
    excl = await _load_exclusions(db, cid)

    mes_curso = await _proyeccion_mes_en_curso(db, cid, today, office_id, tipos_venta, excl)
    mes_curso_kpis_30d = await _period_kpis(db, cid, today - timedelta(days=30), today, office_id, tipos_venta, excl)

    # Próximo mes (target)
    target_y, target_m = _shift_month(today.year, today.month, 1)
    sug   = await _sugerencia_meta(db, cid, target_y, target_m, office_id, tipos_venta, excl)
    meta_realista = sug["meta_realista"]
    pacing = await _pacing_semanal(db, cid, target_y, target_m, meta_realista, office_id, tipos_venta, excl)
    calendario = await _calendario_campanas(db, cid, today, office_id, tipos_venta, excl, meses_adelante=meses_calendario)
    presupuesto = await _presupuesto_sugerido(db, cid, target_y, target_m, meta_realista, office_id, tipos_venta, excl)

    last_sync = await db.scalar(
        text(
            "SELECT MAX(finished_at) FROM sync_log "
            "WHERE company_id = :cid AND entity IN ('documents','document_details') AND status='OK'"
        ),
        {"cid": cid},
    )

    payload: dict[str, Any] = {
        "meta": {
            "fecha":             today.isoformat(),
            "mes_actual":        _mes_str(today.year, today.month),
            "mes_objetivo":      _mes_str(target_y, target_m),
            "office_id":         office_id,
            "office_scope":      [office_id] if office_id is not None else list(OFFICE_IDS),
            "datos_sync_hasta":  last_sync.isoformat() if last_sync else None,
            "generado_at":       datetime.utcnow().isoformat() + "Z",
            "exclusiones": {
                "departamentos": excl.get("depts", []),
                "categorias":    excl.get("cats", []),
                "nota":          "Proyección y meta sugerida usan venta TOTAL (lo que el dueño cobra). El crecimiento YoY interno se calcula con recurrente para no inflar con estacionales puntuales.",
            },
            "cobertura_costos": _cobertura_meta_block(mes_curso_kpis_30d),
        },
        "mes_en_curso":            mes_curso,
        "sugerencia_proximo_mes":  sug,
        "pacing_semanal":          pacing,
        "calendario_campanas":     calendario,
        "presupuesto_compra":      presupuesto,
    }
    payload["resumen"] = _build_narrative(payload)
    return payload
