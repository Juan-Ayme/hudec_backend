"""Endpoints del Simulador de Cascada (debugger visual de la clasificación).

Dos endpoints, ambos por-SKU, livianos:

  GET /matrix-sim/sku-detail/{sku:path}
      Devuelve TODAS las métricas crudas que la cascada usa (stock, ventas en
      varias ventanas, lifetime, lote, proyecciones, baseline categoría…) para
      que el simulador en TypeScript evalúe las 38 reglas con los mismos números
      que el SQL de producción. SQL reutiliza las CTEs de _matriz_90d_base.sql y
      proyecta los campos RAW (no display) desde `metricas_reciente`.

      El converter `:path` permite SKUs con `/` (ej. `CF-60/01`) sin que el
      router los confunda con segmentos de ruta.

  GET /matrix-sim/sku-history/{sku:path}?days=180&office_id=...
      Serie temporal diaria del SKU: ventas (con devoluciones) + recepciones,
      para el gráfico de comportamiento al final de la página del simulador.

El cálculo de la clasificación NO vive acá — se computa en el frontend con la
cascada portada a TS (frontend_hudec/src/lib/cascade.ts). Eso permite sliders
de umbrales en tiempo real sin trip al backend. Un script de paridad
(tools/verify_cascade_parity.py) corre N SKUs por SQL y por TS y reporta drift.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentCompany, get_current_company
from app.database import get_db

logger = logging.getLogger("kawii.matrix")

# Métricas crudas por SKU (debugger de la clasificación). Aunque solo lee,
# expone composición de ventas / lote / lifetime de productos individuales:
# se restringe a usuarios logueados con empresa seleccionada.
router = APIRouter(
    prefix="/matrix-sim",
    tags=["matrix-sim"],
    dependencies=[Depends(get_current_company)],
)

# Reutilizamos la base SQL completa de las matrices (todas las CTEs hasta
# metricas_reciente + cat_baseline). Sobre eso, el SELECT de sku_detail proyecta
# campos crudos para un solo SKU. Es la MISMA fuente de verdad que la matriz —
# no portamos formulas.
_SQL_DIR = Path(__file__).parent.parent / "kawii_matrix" / "sql"
_BASE_SQL = (_SQL_DIR / "_matriz_90d_base.sql").read_text(encoding="utf-8")
_SKU_DETAIL_SQL = _BASE_SQL + "\n" + (_SQL_DIR / "sku_detail.sql").read_text(encoding="utf-8")


def _to_jsonable(v: Any) -> Any:
    """Convierte Decimal/date/datetime a tipos JSON-friendly. None pasa."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _row_to_detail(r: dict) -> dict:
    """Estructura una fila de sku_detail.sql en el shape que el frontend espera."""
    g = lambda k: _to_jsonable(r.get(k))
    return {
        "sucursal": g("sucursal"),
        "office_id": g("office_id"),
        "is_seasonal_dept": bool(r.get("is_seasonal_dept")),
        "stock": {
            "disponible": g("stock_disponible"),
            "reservado": g("stock_reservado"),
            "almacen_central": g("stock_almacen"),
        },
        "ventas_90d": {
            "unds_vendidas": g("unds_vendidas"),
            "unds_vendidas_30d": g("unds_vendidas_30d"),
            "dias_con_ventas": g("dias_con_ventas"),
            "dias_sin_venta_90d": g("dias_sin_venta_90d"),
            "v_recent_45d": g("v_recent_45d"),
            "v_old_45d": g("v_old_45d"),
            "vel_30d": g("vel_30d"),
            "dias_con_stock_30d": g("dias_con_stock_30d"),
            "ultima_venta_90d": g("ultima_venta_90d"),
        },
        "lifetime": {
            "unds_vendidas": g("unds_vendidas_lifetime"),
            "ult_venta": g("ult_venta_lifetime"),
            "unds_recibidas": g("unds_recibidas_lifetime"),
            "primera_recepcion": g("primera_recepcion"),
            "unds_consumidas": g("unds_consumidas_lifetime"),
            "unds_trasladadas": g("unds_trasladadas_lifetime"),
            "pct_sellthrough": g("pct_sellthrough_lifetime"),
            "edad_dias": g("edad_dias"),
        },
        "lote": {
            "primera_recep_90d": g("primera_recep_90d"),
            "ultima_recepcion": g("ultima_recepcion"),
            "dias_desde_ultima_recep": g("dias_desde_ultima_recep"),
            "ult_recep_qty": g("ult_recep_qty"),
            "unds_post_recep": g("unds_post_recep"),
            "unds_recibidas_90d": g("unds_recibidas_90d"),
            "unds_lote_total": g("unds_lote_total"),
            "dias_con_stock": g("dias_con_stock"),
            "dias_exhibido": g("dias_exhibido"),
            "dias_con_venta_lote": g("dias_con_venta_lote"),
            "ult_venta_lote": g("ult_venta_lote"),
            "pri_venta_lote": g("pri_venta_lote"),
            "dias_absorcion_lote": g("dias_absorcion_lote"),
        },
        "proyecciones": {
            "ventas_dia": g("ventas_dia"),
            "proy_mes": g("proy_mes"),
            "proy_30d_reciente": g("proy_30d_reciente"),
            "proy_post_recep": g("proy_post_recep"),
            "dias_cobertura": g("dias_cobertura"),
            "dias_cobertura_reciente": g("dias_cobertura_reciente"),
            "cob_post_recep": g("cob_post_recep"),
            "pct_frecuencia": g("pct_frecuencia"),
            "tdpv": g("tdpv"),
            "monto_vendido_90d": g("monto_vendido_90d"),
        },
        "categoria_baseline": {
            "avg_proy_cat": g("avg_proy_cat"),
        },
    }


@router.get("/sku-detail/{sku:path}")
async def get_sku_detail(
    sku: str = PathParam(..., min_length=1, max_length=100),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Métricas crudas del SKU por sucursal (alimenta la cascada del simulador).

    La query reutiliza TODAS las CTEs de la matriz, por lo que los números son
    idénticos a los de producción. Filtra al final por display_code = :sku.
    """
    from harvester.config import OFFICES_TIENDA, TIPOS_VENTA, TIPOS_DEVOLUCION, TIPOS_TRASLADO
    from app.routers.config_admin import get_exclusions, get_seasonal

    cid = company.company_id
    params = {
        "company_id": cid,
        "sucursales_objetivo": OFFICES_TIENDA,
        "tipos_venta": TIPOS_VENTA,
        "tipos_devolucion": TIPOS_DEVOLUCION,
        "tipos_traslado": TIPOS_TRASLADO,
        "excluded_departments": [],
        "excluded_categories": [],
        "seasonal_departments": [],
        "sku_filter": sku,
    }
    try:
        excl = await get_exclusions(db, cid)
        params["excluded_departments"] = excl["departments"]
        params["excluded_categories"] = excl["categories"]
        params["seasonal_departments"] = await get_seasonal(db, cid)
    except Exception as exc:
        # Degradación segura: sin exclusiones el SKU igual se resuelve. Log para
        # no perder la causa (antes se tragaba en silencio).
        logger.warning(
            "No se cargaron exclusiones para sku-detail (company=%s sku=%s): %s",
            cid, sku, exc,
        )

    # Mismos SET LOCALs que la matriz: forzar hash joins (50x) y leer fechas en UTC.
    await db.execute(text("SET LOCAL enable_nestloop = off"))
    await db.execute(text("SET LOCAL timezone = 'UTC'"))

    result = await db.execute(text(_SKU_DETAIL_SQL), params)
    rows = [dict(r) for r in result.mappings().all()]
    if not rows:
        raise HTTPException(404, f"SKU '{sku}' no encontrado en la matriz operativa (puede que esté excluido o no tenga actividad).")

    first = rows[0]
    return {
        "sku": _to_jsonable(first.get("sku")),
        "product_name": _to_jsonable(first.get("product_name")),
        "department": _to_jsonable(first.get("department")),
        "category": _to_jsonable(first.get("category")),
        "subcategory": _to_jsonable(first.get("subcategory")),
        "rows": [_row_to_detail(r) for r in rows],
    }


@router.get("/sku-history/{sku:path}")
async def get_sku_history(
    sku: str = PathParam(..., min_length=1, max_length=100),
    days: int = Query(180, ge=7, le=1460, description="Ventana hacia atrás en días (default 180, máx 4 años)"),
    office_id: int | None = Query(None, description="Filtrar por sucursal; vacío = todas"),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Serie temporal diaria del SKU (ventas + recepciones).

    Alimenta el gráfico de comportamiento al final del simulador. Une ventas
    (document_details) y recepciones (reception_details) por día calendario UTC.
    Devuelve la unión completa: un día puede tener solo recepción, solo venta o
    ambos. Las devoluciones (is_credit_note) restan unidades y monto.
    """
    from harvester.config import OFFICES_TIENDA

    await db.execute(text("SET LOCAL timezone = 'UTC'"))

    # Casts explícitos en :office_id (asyncpg no puede inferir el tipo cuando llega
    # None) y en :office_ids (acepta int[]). Si office_id viene, restringe a esa
    # oficina; si no, cae a la lista completa de tiendas.
    sql = """
        WITH params AS (
            SELECT NOW() - make_interval(days => CAST(:days AS int)) AS desde
        ),
        ventas AS (
            SELECT (d.emission_date AT TIME ZONE 'UTC')::date AS fecha,
                   SUM(dd.quantity * CASE WHEN d.is_credit_note THEN -1 ELSE 1 END) AS unds,
                   SUM(dd.total_amount * CASE WHEN d.is_credit_note THEN -1 ELSE 1 END) AS monto
            FROM document_details dd
            JOIN documents d  ON d.bsale_document_id = dd.bsale_document_id AND d.company_id = dd.company_id
            JOIN variants v   ON v.bsale_variant_id = dd.bsale_variant_id AND v.company_id = dd.company_id
            CROSS JOIN params p
            WHERE dd.company_id = :cid
              AND v.display_code = :sku
              AND d.is_active
              AND d.emission_date >= p.desde
              AND d.bsale_office_id = ANY(CAST(:office_ids AS int[]))
              AND (CAST(:office_id AS int) IS NULL OR d.bsale_office_id = CAST(:office_id AS int))
            GROUP BY 1
        ),
        recepciones AS (
            SELECT r.admission_date::date AS fecha,
                   SUM(rd.quantity) AS unds_recibidas
            FROM reception_details rd
            JOIN receptions r ON r.bsale_reception_id = rd.bsale_reception_id AND r.company_id = rd.company_id
            JOIN variants v   ON v.bsale_variant_id   = rd.bsale_variant_id  AND v.company_id  = rd.company_id
            CROSS JOIN params p
            WHERE rd.company_id = :cid
              AND v.display_code = :sku
              AND r.admission_date >= p.desde
              AND r.bsale_office_id = ANY(CAST(:office_ids AS int[]))
              AND (CAST(:office_id AS int) IS NULL OR r.bsale_office_id = CAST(:office_id AS int))
            GROUP BY 1
        )
        SELECT COALESCE(v.fecha, r.fecha)             AS fecha,
               COALESCE(v.unds,  0)::numeric          AS unds_vendidas,
               COALESCE(v.monto, 0)::numeric          AS monto,
               COALESCE(r.unds_recibidas, 0)::numeric AS unds_recibidas
        FROM ventas v
        FULL OUTER JOIN recepciones r USING (fecha)
        ORDER BY fecha
    """
    res = await db.execute(
        text(sql),
        {"cid": company.company_id, "sku": sku, "days": days, "office_id": office_id, "office_ids": list(OFFICES_TIENDA)},
    )
    points = [
        {
            "fecha": r["fecha"].isoformat() if r["fecha"] else None,
            "unds_vendidas": float(r["unds_vendidas"]),
            "monto": float(r["monto"]),
            "unds_recibidas": float(r["unds_recibidas"]),
        }
        for r in res.mappings().all()
    ]
    return {"sku": sku, "days": days, "office_id": office_id, "points": points}
