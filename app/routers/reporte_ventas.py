"""Reporte de Ventas por departamento — datos (JSON) y descarga (Excel).

Dos endpoints sobre el MISMO cálculo (`_build_data`), para que la web y el
.xlsx nunca muestren números distintos:

    GET /analytics/reporte-ventas          → JSON (lo pinta el front)
    GET /analytics/reporte-ventas/excel    → .xlsx (una pestaña por departamento)

Criterios de negocio:

  · Rango de fechas INCLUSIVO en ambos extremos (`start_date` = `end_date`
    devuelve el reporte de un solo día).
  · Se excluyen notas de crédito y se filtra por `tipos_venta` de la empresa,
    igual que el resto de analytics.
  · Se excluyen las líneas de REGALO (`document_details.is_gratuity`), mismo
    criterio que catalog_health / diagnosis / category_targets. Contarlas
    sumaría unidades y costo con ingreso ~0 y hundiría el margen.
  · Las fechas se resuelven en UTC — BSale guarda `emission_date` a medianoche
    UTC; interpretarla en America/Lima corre todo un día atrás. Ver la nota
    larga en `app/routers/analytics.py`.
  · COSTO = cantidad × costo efectivo de la variante EN LA SUCURSAL de la
    venta (`variant_costs_by_office`), con fallback al costo global
    (`variant_costs`). Es el mismo join que usa el resto del backend.

BASE DEL MARGEN — decisión de negocio, confirmada con el dueño (2026-08-24):

    ganancia   = ingresos CON IGV − costo neto
    margen_pct = ganancia / ingresos CON IGV

  Esto es DISTINTO del resto del backend (`/analytics/rentabilidad`,
  `/diagnosis`, `/catalog-health`), que calcula el margen contable clásico
  sobre la venta NETA. La diferencia es deliberada y se sostiene en dos
  hechos verificados de esta empresa:

    1. El IGV cobrado en las ventas se queda en la empresa (no se remite).
       → el ingreso económico real es el bruto, no el neto.
    2. Los proveedores NO facturan IGV en las compras.
       → el costo neto guardado ES lo que efectivamente se paga.

  Con esas dos condiciones, bruto-contra-neto es la lectura correcta y da
  ~34,5%; el margen contable clásico daría ~22,5%. Si alguna de las dos
  cambia (empiezan a remitir IGV, o un proveedor factura con IGV), este
  cálculo queda mal y hay que revisarlo: subir SOLO uno de los dos lados por
  1,18 es justamente lo que produce un margen inflado.

  Datos que respaldan el punto 2 (medidos sobre 8.605 variantes): el costo
  promedia 73,1% del precio NETO de lista y solo 26 variantes (0,3%) tienen
  costo mayor a su precio neto — el patrón de un costo neto, no bruto.

  El IGV se expone aparte (`igv`) en cada nivel para que el puente
  ingresos → costo → ganancia se pueda auditar, y se agrega `markup_pct`
  (ganancia sobre costo) como segunda lente para fijar precios.
"""

from __future__ import annotations

import io
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from analytics.excel_reporte_ventas import build_reporte_ventas_workbook
from app.auth import CurrentCompany, get_current_company
from app.database import get_db
from app.routers._office_scope import office_filter as _office_filter, office_scope_ids
from app.routers.config_admin import get_company
from harvester.config import TIPOS_VENTA as DEFAULT_TIPOS_VENTA

router = APIRouter(
    prefix="/analytics/reporte-ventas",
    tags=["analytics"],
    dependencies=[Depends(get_current_company)],
)

# Misma constante que analytics.py: BSale guarda la fecha a medianoche UTC.
_TZ = "UTC"

# Techo del rango. Un año ya cubre "Este año" del selector de fechas; más
# arriba el detalle por producto hace crecer el workbook sin que nadie lo lea.
_MAX_DIAS = 366

# Guarda de tiempo (ms) para el detalle. Medido contra Hudec: 30 días ~1.5s y
# 366 días ~8s. Si una empresa con mucho más volumen se pasa, el endpoint
# corta con 503 en vez de retener la conexión (mismo criterio que compras.py).
_TIMEOUT_MS = 60_000

SIN_DEPARTAMENTO = "(Sin departamento)"
SIN_CATEGORIA = "(Sin categoría)"


# ──────────────────────────────────────────────────────────────────────────
# Cálculo
# ──────────────────────────────────────────────────────────────────────────


def _f(v) -> float:
    """Decimal/None → float. La BD devuelve NUMERIC como Decimal."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _pct(parte: float, total: float) -> float:
    """Porcentaje redondeado a 2 decimales. Divide-por-cero → 0."""
    return round(parte / total * 100, 2) if total else 0.0


def _parse_rango(start_date: str, end_date: str) -> tuple[date, date]:
    try:
        dfrom = date.fromisoformat(start_date)
        dto = date.fromisoformat(end_date)
    except ValueError:
        raise HTTPException(400, "Formato de fecha inválido. Usar YYYY-MM-DD")
    if dfrom > dto:
        raise HTTPException(400, "start_date no puede ser posterior a end_date")
    dias = (dto - dfrom).days + 1
    if dias > _MAX_DIAS:
        raise HTTPException(400, f"El rango no puede superar {_MAX_DIAS} días (pediste {dias})")
    return dfrom, dto


# Documentos de venta del rango. Es el CTE del que cuelga todo lo demás.
#
# PERFORMANCE — dos decisiones que valen 20x, no tocar sin medir:
#
#  1. El filtro de fecha compara `emission_date` contra dos timestamptz ya
#     calculados en Python, NO `(emission_date AT TIME ZONE 'UTC')::DATE`.
#     Envolver la columna en una función la vuelve no-sargable e inutiliza
#     `idx_documents_emission`; con el rango crudo el índice sí entra. Es
#     equivalente exacto: [medianoche UTC del día desde, medianoche UTC del
#     día siguiente al hasta).
#  2. `MATERIALIZED` fuerza a resolver el rango de fechas PRIMERO. Sin eso
#     Postgres inlinea el CTE, arranca por `variants` y recorre TODO el
#     historial de `document_details` (450k filas) para recién después
#     descartar por fecha.
_CTE_DOCS = """
    docs AS MATERIALIZED (
        SELECT doc.bsale_document_id, doc.bsale_office_id
        FROM documents doc
        WHERE doc.company_id = :cid
          AND doc.emission_date >= :ts_from
          AND doc.emission_date <  :ts_to
          AND COALESCE(doc.is_credit_note, FALSE) = FALSE
          AND doc.bsale_document_type_id = ANY(:tipos_venta)
          AND {office_filter}
    )
"""

# Taxonomía por variante, resuelta UNA vez (~4k filas) para hash-joinear
# después. Antes se joineaba `v_products_full` fila a fila del detalle: 30k
# nested loops sobre una vista de 5 tablas.
_CTE_TAX = """
    tax AS MATERIALIZED (
        SELECT
            v.bsale_variant_id,
            -- display_code es NOT NULL en `variants`; `code` puede venir vacío.
            COALESCE(NULLIF(v.display_code, ''), NULLIF(v.code, ''), '—') AS sku,
            COALESCE(vpf.product_name, v.description, 'Sin nombre') AS producto,
            COALESCE(vpf.department, :sin_dep)                      AS departamento,
            COALESCE(vpf.category, :sin_cat)                        AS categoria
        FROM variants v
        LEFT JOIN v_products_full vpf
          ON vpf.bsale_product_id = v.bsale_product_id
         AND vpf.company_id = v.company_id
        WHERE v.company_id = :cid
    )
"""

# Detalle por (departamento, categoría, variante). El costo se resuelve por
# sucursal de la venta con fallback al global — mismo join que costs_audit.
# Se preagrega por (variante, sucursal) antes de decorar: colapsa ~30k líneas
# a ~6k y deja los joins de taxonomía/costo trabajando sobre ese conjunto.
_Q_DETALLE = """
    WITH {cte_docs},
    {cte_tax},
    ventas AS MATERIALIZED (
        SELECT
            dd.bsale_variant_id,
            docs.bsale_office_id,
            SUM(dd.quantity)     AS unidades,
            SUM(dd.total_amount) AS ingresos,
            SUM(dd.net_amount)   AS ingresos_netos
        FROM docs
        JOIN document_details dd
          ON dd.bsale_document_id = docs.bsale_document_id
         AND dd.company_id = :cid
        WHERE NOT COALESCE(dd.is_gratuity, FALSE)
        GROUP BY 1, 2
    )
    SELECT
        tax.departamento,
        tax.categoria,
        tax.sku,
        tax.producto,
        SUM(ventas.unidades)       AS unidades,
        SUM(ventas.ingresos)       AS ingresos,
        SUM(ventas.ingresos_netos) AS ingresos_netos,
        SUM(ventas.unidades * COALESCE(vco.effective_cost, vc.effective_cost, 0)) AS costo
    FROM ventas
    JOIN tax ON tax.bsale_variant_id = ventas.bsale_variant_id
    LEFT JOIN variant_costs vc
      ON vc.bsale_variant_id = ventas.bsale_variant_id
     AND vc.company_id = :cid
    LEFT JOIN variant_costs_by_office vco
      ON vco.bsale_variant_id = ventas.bsale_variant_id
     AND vco.company_id = :cid
     AND vco.bsale_office_id = ventas.bsale_office_id
    GROUP BY 1, 2, 3, 4
"""

# Tickets por departamento. Va aparte porque COUNT(DISTINCT documento) NO es
# sumable: un mismo ticket toca varias categorías y sumarlo por nivel lo
# duplicaría. Acá cada ticket cuenta una vez por departamento en el que compró.
_Q_TICKETS = """
    WITH {cte_docs},
    {cte_tax},
    lineas AS MATERIALIZED (
        SELECT DISTINCT dd.bsale_document_id, dd.bsale_variant_id
        FROM docs
        JOIN document_details dd
          ON dd.bsale_document_id = docs.bsale_document_id
         AND dd.company_id = :cid
        WHERE NOT COALESCE(dd.is_gratuity, FALSE)
    )
    SELECT
        tax.departamento,
        COUNT(DISTINCT lineas.bsale_document_id) AS tickets
    FROM lineas
    JOIN tax ON tax.bsale_variant_id = lineas.bsale_variant_id
    GROUP BY 1
"""

# Evolución diaria. Solo toca `documents`, así que la fecha sí se extrae acá
# (es el group by), pero el FILTRO sigue siendo el rango sargable.
_Q_SERIE = """
    SELECT
        (doc.emission_date AT TIME ZONE '{tz}')::DATE AS fecha,
        SUM(doc.total_amount)                         AS ingresos,
        COUNT(*)                                      AS tickets
    FROM documents doc
    WHERE doc.company_id = :cid
      AND doc.emission_date >= :ts_from
      AND doc.emission_date <  :ts_to
      AND COALESCE(doc.is_credit_note, FALSE) = FALSE
      AND doc.bsale_document_type_id = ANY(:tipos_venta)
      AND {office_filter}
    GROUP BY 1
    ORDER BY 1
"""


async def _office_label(db: AsyncSession, cid: int, office_id: int | None) -> str:
    if office_id is None:
        return "Todas las sucursales"
    nombre = await db.scalar(
        text("SELECT name FROM offices WHERE company_id = :cid AND bsale_office_id = :oid"),
        {"cid": cid, "oid": office_id},
    )
    return nombre or f"Sucursal {office_id}"


async def _build_data(
    db: AsyncSession,
    cid: int,
    dfrom: date,
    dto: date,
    office_id: int | None,
) -> dict:
    """Arma el reporte completo: meta + totales + departamentos + serie diaria."""
    company_config = await get_company(db, cid)
    tipos_venta = company_config.get("tipos_venta") or DEFAULT_TIPOS_VENTA
    scope = office_scope_ids(company_config, office_id)

    params = {
        "cid": cid,
        # Rango [medianoche UTC del día `dfrom`, medianoche UTC del día
        # siguiente a `dto`). Se calcula acá y no en SQL para que el filtro
        # quede sargable contra `idx_documents_emission` (ver _CTE_DOCS).
        "ts_from": datetime.combine(dfrom, time.min, tzinfo=timezone.utc),
        "ts_to": datetime.combine(dto + timedelta(days=1), time.min, tzinfo=timezone.utc),
        "tipos_venta": tipos_venta,
        "sin_dep": SIN_DEPARTAMENTO,
        "sin_cat": SIN_CATEGORIA,
    }
    of = _office_filter(scope, alias="doc")
    cte_docs = _CTE_DOCS.format(office_filter=of)
    sql_detalle = _Q_DETALLE.format(cte_docs=cte_docs, cte_tax=_CTE_TAX)
    sql_tickets = _Q_TICKETS.format(cte_docs=cte_docs, cte_tax=_CTE_TAX)
    sql_serie = _Q_SERIE.format(tz=_TZ, office_filter=of)

    # Fuerza hash joins. Mismo truco que las matrices y el informe diario: con
    # nested loops el planner elige mal sobre este volumen (ver app/routers/
    # analytics.py:2354). SET LOCAL vive solo en esta transacción.
    await db.execute(text("SET LOCAL enable_nestloop = off"))
    await db.execute(text(f"SET LOCAL statement_timeout = {_TIMEOUT_MS}"))

    try:
        detalle = (await db.execute(text(sql_detalle), params)).mappings().all()
        tickets_rows = (await db.execute(text(sql_tickets), params)).mappings().all()
        serie_rows = (await db.execute(text(sql_serie), params)).mappings().all()
    except Exception as exc:
        # statement_timeout u otro error de las queries pesadas → 503 accionable.
        msg = str(exc).lower()
        if "statement timeout" in msg or "canceling statement" in msg:
            raise HTTPException(
                503,
                "El reporte tardó demasiado para este rango. Probá un rango más "
                "corto o filtrá por sucursal.",
            )
        raise

    tickets_por_dep = {r["departamento"]: int(r["tickets"] or 0) for r in tickets_rows}
    # El total sale de la serie: cada documento aparece en un solo día, así que
    # sumarla no duplica. Los tickets por departamento SÍ se solapan (un ticket
    # con productos de dos departamentos cuenta en ambos) y por eso no suman esto.
    tickets_totales = sum(int(r["tickets"] or 0) for r in serie_rows)

    # ── Agregación en Python: dep → cat → productos ──
    # Se hace acá y no con GROUPING SETS para tener un solo recorrido de la
    # BD y armar la jerarquía anidada que consume tanto el JSON como el Excel.
    deps: dict[str, dict] = {}
    for row in detalle:
        dep_name = row["departamento"]
        cat_name = row["categoria"]

        dep = deps.setdefault(
            dep_name,
            {"departamento": dep_name, "unidades": 0.0, "ingresos": 0.0,
             "ingresos_netos": 0.0, "costo": 0.0, "categorias": {}},
        )
        cat = dep["categorias"].setdefault(
            cat_name,
            {"categoria": cat_name, "unidades": 0.0, "ingresos": 0.0,
             "ingresos_netos": 0.0, "costo": 0.0, "productos": []},
        )

        unidades = _f(row["unidades"])
        ingresos = _f(row["ingresos"])
        netos = _f(row["ingresos_netos"])
        costo = _f(row["costo"])

        cat["productos"].append({
            "sku": row["sku"],
            "producto": row["producto"],
            "unidades": unidades,
            "ingresos": round(ingresos, 2),
            "ingresos_netos": round(netos, 2),
            # El IGV se deriva de la resta y no de SUM(dd.tax_amount) para que
            # el puente ingresos = netos + igv cierre exacto en pantalla. A
            # nivel línea `total = neto + tax` cuadra sin excepción (verificado
            # sobre 30.754 líneas), así que la resta ES el IGV.
            "igv": round(ingresos - netos, 2),
            "costo": round(costo, 2),
            "ganancia": round(ingresos - costo, 2),
            "margen_pct": _pct(ingresos - costo, ingresos),
            "markup_pct": _pct(ingresos - costo, costo),
            "participacion_pct": 0.0,  # se completa al cerrar la categoría
        })

        for bucket in (dep, cat):
            bucket["unidades"] += unidades
            bucket["ingresos"] += ingresos
            bucket["ingresos_netos"] += netos
            bucket["costo"] += costo

    total_ingresos = sum(d["ingresos"] for d in deps.values())
    total_netos = sum(d["ingresos_netos"] for d in deps.values())
    total_costo = sum(d["costo"] for d in deps.values())
    total_unidades = sum(d["unidades"] for d in deps.values())

    departamentos = []
    for dep in deps.values():
        cats = []
        for cat in dep["categorias"].values():
            # % del producto dentro de SU categoría, y orden por ingresos.
            for p in cat["productos"]:
                p["participacion_pct"] = _pct(p["ingresos"], cat["ingresos"])
            cat["productos"].sort(key=lambda p: p["ingresos"], reverse=True)
            cats.append({
                "categoria": cat["categoria"],
                "unidades": cat["unidades"],
                "ingresos": round(cat["ingresos"], 2),
                "ingresos_netos": round(cat["ingresos_netos"], 2),
                "igv": round(cat["ingresos"] - cat["ingresos_netos"], 2),
                "costo": round(cat["costo"], 2),
                "ganancia": round(cat["ingresos"] - cat["costo"], 2),
                "margen_pct": _pct(cat["ingresos"] - cat["costo"], cat["ingresos"]),
                "markup_pct": _pct(cat["ingresos"] - cat["costo"], cat["costo"]),
                "participacion_pct": _pct(cat["ingresos"], dep["ingresos"]),
                "productos": cat["productos"],
            })
        cats.sort(key=lambda c: c["ingresos"], reverse=True)

        tickets = tickets_por_dep.get(dep["departamento"], 0)
        departamentos.append({
            "departamento": dep["departamento"],
            "unidades": dep["unidades"],
            "ingresos": round(dep["ingresos"], 2),
            "ingresos_netos": round(dep["ingresos_netos"], 2),
            "igv": round(dep["ingresos"] - dep["ingresos_netos"], 2),
            "costo": round(dep["costo"], 2),
            "ganancia": round(dep["ingresos"] - dep["costo"], 2),
            "margen_pct": _pct(dep["ingresos"] - dep["costo"], dep["ingresos"]),
            "markup_pct": _pct(dep["ingresos"] - dep["costo"], dep["costo"]),
            "participacion_pct": _pct(dep["ingresos"], total_ingresos),
            "tickets": tickets,
            "ticket_promedio": round(dep["ingresos"] / tickets, 2) if tickets else 0.0,
            "categorias": cats,
        })
    departamentos.sort(key=lambda d: d["ingresos"], reverse=True)

    return {
        "meta": {
            "empresa": company_config.get("brand_name") or company_config.get("name") or "KAWII",
            "desde": dfrom.isoformat(),
            "hasta": dto.isoformat(),
            "dias": (dto - dfrom).days + 1,
            "sucursal": await _office_label(db, cid, office_id),
            "office_ids": scope,
            "generado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "totales": {
            "unidades": total_unidades,
            "ingresos": round(total_ingresos, 2),
            "ingresos_netos": round(total_netos, 2),
            "igv": round(total_ingresos - total_netos, 2),
            "costo": round(total_costo, 2),
            "ganancia": round(total_ingresos - total_costo, 2),
            "margen_pct": _pct(total_ingresos - total_costo, total_ingresos),
            "markup_pct": _pct(total_ingresos - total_costo, total_costo),
            "tickets": int(tickets_totales),
            "ticket_promedio": round(total_ingresos / tickets_totales, 2) if tickets_totales else 0.0,
        },
        "serie_diaria": [
            {
                "fecha": r["fecha"].isoformat(),
                "ingresos": round(_f(r["ingresos"]), 2),
                "tickets": int(r["tickets"] or 0),
            }
            for r in serie_rows
        ],
        "departamentos": departamentos,
    }


# ──────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────


@router.get("")
async def reporte_ventas(
    start_date: str = Query(..., description="Fecha inicio YYYY-MM-DD (inclusive)"),
    end_date: str = Query(..., description="Fecha fin YYYY-MM-DD (inclusive)"),
    office_id: int | None = Query(None, description="Sucursal puntual; sin esto, todas"),
    incluir_productos: bool = Query(
        True, description="False devuelve solo el nivel categoría (payload mucho más liviano)"
    ),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Reporte de ventas del rango, agregado por departamento → categoría → producto."""
    dfrom, dto = _parse_rango(start_date, end_date)
    data = await _build_data(db, company.company_id, dfrom, dto, office_id)

    if not incluir_productos:
        for dep in data["departamentos"]:
            for cat in dep["categorias"]:
                cat.pop("productos", None)

    return data


@router.get("/excel", response_class=StreamingResponse)
async def reporte_ventas_excel(
    start_date: str = Query(..., description="Fecha inicio YYYY-MM-DD (inclusive)"),
    end_date: str = Query(..., description="Fecha fin YYYY-MM-DD (inclusive)"),
    office_id: int | None = Query(None, description="Sucursal puntual; sin esto, todas"),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Mismo reporte que `GET /analytics/reporte-ventas`, como .xlsx.

    Portada + Resumen + una pestaña por departamento, cada una con su tabla
    de categorías (productos colapsables) y sus gráficos.
    """
    dfrom, dto = _parse_rango(start_date, end_date)
    data = await _build_data(db, company.company_id, dfrom, dto, office_id)

    wb = build_reporte_ventas_workbook(data)
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)

    filename = f"Reporte_Ventas_{dfrom.isoformat()}_{dto.isoformat()}.xlsx"
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
