"""Auditoría y backfill de costos (`variant_costs`).

El job `sync_variant_costs` del harvester lee costos del endpoint
`/variants/{id}/costs.json` de BSale. Si BSale devuelve `averageCost=0` y no
hay history, el sistema graba `effective_cost=0` con `cost_source='NONE'`.

PERO los costos están en `reception_details.cost` (la mercadería que llegó),
que se sincroniza por otro flujo. Este módulo provee:

  - `GET  /config/variant-costs/audit`                       → reporte de cobertura
  - `POST /config/variant-costs/backfill-from-receptions`    → llena los effective_cost=0
        usando promedio ponderado (por unidades recibidas) y/o latest desde reception_details.

Esto rescata ~97% de los SKUs sin costo (los que sí tienen recepciones).
"""

from __future__ import annotations

from typing import Any, Optional
import io

import pandas as pd
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    CurrentCompany,
    CurrentUser,
    get_current_company,
    get_current_user,
    require_operador_or_admin,
)
from app.database import get_db
from app.events import log_event


router = APIRouter(
    prefix="/config/variant-costs",
    tags=["config"],
    dependencies=[Depends(get_current_company)],
)


# ════════════════════════════════════════════════════════════════════════════
# Auditoría — reporte sin tocar nada
# ════════════════════════════════════════════════════════════════════════════

@router.get("/audit")
async def audit(db: AsyncSession = Depends(get_db)) -> dict:
    """Reporta cobertura de costos: cuántas variantes tienen costo válido,
    cuántas no, cuántas son recuperables desde recepciones, y el impacto en
    ventas de los últimos 90 días.
    """
    q = """
        WITH variantes AS (
            SELECT v.bsale_variant_id,
                   COALESCE(vc.effective_cost, 0) AS ec,
                   COALESCE(vc.cost_source, 'NONE') AS src
            FROM variants v
            LEFT JOIN variant_costs vc ON vc.bsale_variant_id = v.bsale_variant_id
            WHERE v.is_active
        ),
        receps AS (
            SELECT rd.bsale_variant_id,
                   SUM(rd.quantity * rd.cost) FILTER (WHERE rd.cost > 0) AS suma_pond,
                   SUM(rd.quantity)            FILTER (WHERE rd.cost > 0) AS suma_qty,
                   COUNT(*)                    FILTER (WHERE rd.cost > 0) AS n_recep_validas
            FROM reception_details rd
            GROUP BY rd.bsale_variant_id
        ),
        ventas_90d AS (
            SELECT dd.bsale_variant_id, SUM(dd.total_amount) AS venta
            FROM document_details dd
            JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id
            WHERE (doc.emission_date AT TIME ZONE 'UTC')::DATE >= CURRENT_DATE - 90
              AND (doc.emission_date AT TIME ZONE 'UTC')::DATE <  CURRENT_DATE
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND doc.bsale_office_id IN (1,3)
              AND NOT dd.is_gratuity
            GROUP BY dd.bsale_variant_id
        )
        SELECT
          (SELECT COUNT(*) FROM variantes)                                AS variantes_activas,
          (SELECT COUNT(*) FROM variantes WHERE ec  > 0)                  AS con_costo,
          (SELECT COUNT(*) FROM variantes WHERE ec  = 0)                  AS sin_costo,
          (SELECT COUNT(*) FROM variantes vv
              JOIN receps r ON r.bsale_variant_id = vv.bsale_variant_id
              WHERE vv.ec = 0 AND r.suma_pond > 0)                        AS recuperables,
          (SELECT COUNT(*) FROM variantes vv
              LEFT JOIN receps r ON r.bsale_variant_id = vv.bsale_variant_id
              WHERE vv.ec = 0 AND COALESCE(r.suma_pond, 0) = 0)           AS irrecuperables,
          (SELECT COALESCE(SUM(venta), 0) FROM ventas_90d)                AS venta_90d_total,
          (SELECT COALESCE(SUM(v90.venta), 0)
              FROM ventas_90d v90
              JOIN variantes vv ON vv.bsale_variant_id = v90.bsale_variant_id
              WHERE vv.ec > 0)                                            AS venta_90d_con_costo,
          (SELECT COALESCE(SUM(v90.venta), 0)
              FROM ventas_90d v90
              JOIN variantes vv ON vv.bsale_variant_id = v90.bsale_variant_id
              JOIN receps r ON r.bsale_variant_id = v90.bsale_variant_id
              WHERE vv.ec = 0 AND r.suma_pond > 0)                        AS venta_90d_recuperable
    """
    r = (await db.execute(text(q))).mappings().one()
    venta_total = float(r["venta_90d_total"])
    venta_con   = float(r["venta_90d_con_costo"])
    venta_rec   = float(r["venta_90d_recuperable"])
    venta_sin   = venta_total - venta_con

    return {
        "variantes": {
            "total_activas":      int(r["variantes_activas"]),
            "con_costo":          int(r["con_costo"]),
            "sin_costo":          int(r["sin_costo"]),
            "recuperables":       int(r["recuperables"]),
            "irrecuperables":     int(r["irrecuperables"]),
            "pct_cobertura":      round(int(r["con_costo"]) / max(1, int(r["variantes_activas"])) * 100, 1),
        },
        "ventas_ultimos_90d": {
            "venta_total":            round(venta_total, 2),
            "venta_con_costo":        round(venta_con, 2),
            "venta_sin_costo":        round(venta_sin, 2),
            "venta_sin_costo_pct":    round(venta_sin / max(1.0, venta_total) * 100, 1),
            "venta_recuperable":      round(venta_rec, 2),
            "venta_recuperable_pct":  round(venta_rec / max(1.0, venta_total) * 100, 1),
            "cobertura_costos_pct":   round(venta_con / max(1.0, venta_total) * 100, 1),
        },
        "diagnostico": (
            "OK — cobertura alta" if venta_total > 0 and venta_con / venta_total >= 0.90 else
            f"⚠️ Cobertura {round(venta_con/max(1,venta_total)*100,1)}% — "
            f"recuperable {round(venta_rec/max(1,venta_total)*100,1)}% "
            f"con POST /config/variant-costs/backfill-from-receptions"
        ),
    }


# ════════════════════════════════════════════════════════════════════════════
# Backfill — actualizar effective_cost=0 desde recepciones
# ════════════════════════════════════════════════════════════════════════════

@router.post("/backfill-from-receptions", dependencies=[Depends(require_operador_or_admin)])
async def backfill_from_receptions(
    dry_run: bool = Query(True, description="Si true, NO escribe — solo reporta qué cambiaría."),
    company: CurrentCompany = Depends(get_current_company),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Para cada variante con `effective_cost=0`, calcula:
      - average_cost = SUM(qty × cost) / SUM(qty)   (promedio ponderado)
      - latest_cost  = cost de la recepción más reciente con cost>0
      - effective_cost = average_cost (preferido) o latest_cost
      - cost_source = 'RECEPTION_AVG' (si avg>0) o 'RECEPTION_LATEST' (si solo latest>0)

    Solo toca filas con `effective_cost = 0` — no sobrescribe costos válidos.

    Idempotente: corrida 2 da los mismos cambios que corrida 1 (a menos que
    lleguen recepciones nuevas).
    """
    # Calcular los nuevos costos desde reception_details
    q_calc = """
        WITH a_actualizar AS (
            SELECT v.bsale_variant_id
            FROM variants v
            JOIN variant_costs vc ON vc.bsale_variant_id = v.bsale_variant_id
            WHERE v.is_active
              AND (vc.effective_cost IS NULL OR vc.effective_cost = 0)
        ),
        agregados AS (
            SELECT rd.bsale_variant_id,
                   SUM(rd.quantity * rd.cost) FILTER (WHERE rd.cost > 0)
                     / NULLIF(SUM(rd.quantity) FILTER (WHERE rd.cost > 0), 0) AS avg_cost,
                   (
                     SELECT rd2.cost
                     FROM reception_details rd2
                     JOIN receptions rec2 ON rec2.bsale_reception_id = rd2.bsale_reception_id
                     WHERE rd2.bsale_variant_id = rd.bsale_variant_id
                       AND rd2.cost > 0
                     ORDER BY rec2.admission_date DESC
                     LIMIT 1
                   ) AS latest_cost,
                   COUNT(*) FILTER (WHERE rd.cost > 0) AS n_recep
            FROM reception_details rd
            WHERE rd.bsale_variant_id IN (SELECT bsale_variant_id FROM a_actualizar)
            GROUP BY rd.bsale_variant_id
        )
        SELECT a.bsale_variant_id,
               COALESCE(ag.avg_cost, 0)::numeric    AS avg_cost,
               COALESCE(ag.latest_cost, 0)::numeric AS latest_cost,
               COALESCE(ag.n_recep, 0)              AS n_recep
        FROM a_actualizar a
        LEFT JOIN agregados ag ON ag.bsale_variant_id = a.bsale_variant_id
    """
    rows = (await db.execute(text(q_calc))).mappings().all()

    cambios = []
    for r in rows:
        avg = float(r["avg_cost"])
        lat = float(r["latest_cost"])
        n_recep = int(r["n_recep"])
        if avg > 0:
            effective = avg
            source    = "RECEPTION_AVG"
        elif lat > 0:
            effective = lat
            source    = "RECEPTION_LATEST"
        else:
            continue  # no hay info en recepciones — skip
        cambios.append({
            "bsale_variant_id": int(r["bsale_variant_id"]),
            "average_cost":     round(avg, 4),
            "latest_cost":      round(lat, 4),
            "effective_cost":   round(effective, 4),
            "cost_source":      source,
            "n_recepciones":    n_recep,
        })

    if not dry_run and cambios:
        # Update batch
        for c in cambios:
            await db.execute(
                text("""
                    UPDATE variant_costs
                       SET average_cost   = :avg,
                           latest_cost    = :lat,
                           effective_cost = :eff,
                           cost_source    = :src,
                           synced_at      = NOW()
                     WHERE bsale_variant_id = :vid
                """),
                {
                    "vid": c["bsale_variant_id"],
                    "avg": c["average_cost"],
                    "lat": c["latest_cost"],
                    "eff": c["effective_cost"],
                    "src": c["cost_source"],
                },
            )
        await log_event(
            db, company_id=company.company_id, event_type="config.updated",
            actor_user_id=user.id,
            payload={"que": "variant_costs_backfill", "actualizados": len(cambios)},
            commit=False,
        )
        await db.commit()

    return {
        "dry_run":             dry_run,
        "candidatos_total":    len(rows),
        "actualizados":        len(cambios),
        "saltados_sin_recep":  len(rows) - len(cambios),
        "sample":              cambios[:10],
        "nota": (
            "Cambios escritos." if not dry_run and cambios else
            "Dry run — no se modificó nada. Pasar ?dry_run=false para aplicar."
            if dry_run else
            "Nada que actualizar."
        ),
    }


# ════════════════════════════════════════════════════════════════════════════
# Diagnóstico de costos por sucursal — variación + salud
# ════════════════════════════════════════════════════════════════════════════

from pathlib import Path
from collections import defaultdict

_SQL_DIR = Path(__file__).resolve().parent.parent / "kawii_matrix" / "sql"


@router.get("/by-office")
async def costs_by_office(
    office_id: Optional[int] = Query(None, description="Si se pasa, filtra a esa sucursal. Si no, analiza todas las tiendas."),
    days: int = Query(90, ge=7, le=365, description="Ventana de ventas en días"),
    umbral_margen_bajo: float = Query(20.0, ge=0, le=100, description="% mínimo de margen aceptable"),
    umbral_margen_alto: float = Query(70.0, ge=0, le=100, description="% máximo de margen razonable (sospechoso si supera)"),
    umbral_outlier_pct: float = Query(50.0, ge=0, description="% de desviación vs promedio para considerar outlier"),
    umbral_desactualizado_pct: float = Query(20.0, ge=0, description="% de diferencia vs última recepción"),
    umbral_ratio_max_min: float = Query(2.0, ge=1.0, description="Ratio MAX/MIN entre sucursales para variación alta"),
    solo_problemas: bool = Query(False, description="Si true, solo devuelve ERROR + WARNING"),
    incluir_igv_en_margen: bool = Query(True, description="Si es True, calcula margen usando Precio Bruto. Si es False, usa Precio Neto."),
    page: int = Query(1, ge=1, description="Número de página para resultados"),
    page_size: int = Query(100, ge=1, le=1000, description="Cantidad de resultados por página"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Diagnóstico de costos por sucursal: variación + validación de salud.

    Analiza cada variante × sucursal y diagnostica si el costo está bien o mal
    usando 8 reglas de validación. Cada fila indica DE QUÉ TABLA sale el costo
    y el precio.

    ❌ ERROR:
      - COSTO_CERO: sin costo cargado
      - MARGEN_NEGATIVO: costo > precio de venta

    ⚠️ WARNING:
      - MARGEN_MUY_BAJO: margen < 20%
      - MARGEN_MUY_ALTO: margen > 70% (sospechoso, posible costo mal cargado)
      - COSTO_OUTLIER: costo muy diferente al promedio entre sucursales
      - SIN_RECEPCION: sin respaldo de recepciones
      - COSTO_DESACTUALIZADO: costo no refleja la última compra real
      - VARIACION_ALTA: ratio MAX/MIN alto entre sucursales
    """
    # Cargar sucursales objetivo desde config
    from harvester.config import OFFICES_TIENDA
    # Si se pasa office_id, filtrar solo esa sucursal (validando que sea de tienda).
    # Si no, analizar todas las tiendas.
    if office_id is not None:
        sucursales = [office_id] if office_id in OFFICES_TIENDA else OFFICES_TIENDA
    else:
        sucursales = OFFICES_TIENDA

    # Leer el SQL
    sql_path = _SQL_DIR / "09_costos_por_sucursal.sql"
    sql_text = sql_path.read_text(encoding="utf-8")

    params = {
        "days": days,
        "sucursales_objetivo": sucursales,
        "umbral_margen_bajo": umbral_margen_bajo,
        "umbral_margen_alto": umbral_margen_alto,
        "umbral_outlier_pct": umbral_outlier_pct,
        "umbral_desactualizado_pct": umbral_desactualizado_pct,
        "umbral_ratio_max_min": umbral_ratio_max_min,
        "incluir_igv_en_margen": incluir_igv_en_margen,
    }

    result = await db.execute(text(sql_text), params)
    rows = result.mappings().all()

    # ── Procesar resultados ──────────────────────────────────────────────

    # Contadores de salud
    conteo_severidad: dict[str, int] = {"ERROR": 0, "WARNING": 0, "OK": 0}
    conteo_alertas: dict[str, int] = defaultdict(int)
    impacto_total = 0.0
    variantes_vistas: set[int] = set()

    detalle: list[dict[str, Any]] = []

    for r in rows:
        sev = r["severidad"]
        alertas = list(r["alertas"]) if r["alertas"] else []

        if solo_problemas and sev == "OK":
            continue

        conteo_severidad[sev] = conteo_severidad.get(sev, 0) + 1
        for a in alertas:
            conteo_alertas[a] += 1
        impacto_total += abs(float(r["impacto_soles"] or 0))
        variantes_vistas.add(int(r["bsale_variant_id"]))

        detalle.append({
            "bsale_office_id":         int(r["bsale_office_id"]),
            "sucursal":                r["sucursal"],
            "bsale_variant_id":        int(r["bsale_variant_id"]),
            "codigo_sku":              r["codigo_sku"],
            "producto":                r["producto"],
            "costo_efectivo":          float(r["costo_efectivo"] or 0),
            "costo_origen":            r["costo_origen"],
            "tabla_costo":             r["tabla_costo"],
            "precio_venta":            float(r["precio_venta"] or 0),
            "tabla_precio":            r["tabla_precio"],
            "margen_soles":            float(r["margen_soles"] or 0),
            "margen_pct":              float(r["margen_pct"]) if r["margen_pct"] is not None else None,
            "costo_avg_sucursales":    float(r["costo_avg_sucursales"] or 0),
            "costo_min_sucursales":    float(r["costo_min_sucursales"] or 0),
            "costo_max_sucursales":    float(r["costo_max_sucursales"] or 0),
            "diff_vs_avg_pct":         float(r["diff_vs_avg_pct"] or 0),
            "ratio_max_min":           float(r["ratio_max_min"] or 1),
            "ultimo_costo_recepcion":  float(r["ultimo_costo_recepcion"] or 0),
            "n_recepciones":           int(r["n_recepciones"] or 0),
            "uds_vendidas_periodo":    float(r["uds_vendidas_periodo"] or 0),
            "impacto_soles":           float(r["impacto_soles"] or 0),
            "severidad":               sev,
            "alertas":                 alertas,
        })

    total_filas = conteo_severidad["ERROR"] + conteo_severidad["WARNING"] + conteo_severidad["OK"]
    total_items_filtrados = len(detalle)
    total_pages = (total_items_filtrados + page_size - 1) // page_size

    # Paginar el detalle
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    paginated_detalle = detalle[start_idx:end_idx]

    nota = (
        f"Analizando sucursal {sucursales[0]}" if office_id is not None
        else f"Analizando {len(sucursales)} sucursales: {', '.join(str(s) for s in sucursales)}"
    )

    return {
        "resumen": {
            "ventana_dias":                days,
            "variantes_analizadas":        len(variantes_vistas),
            "filas_total":                 total_filas,
            "salud": {
                "ok":       conteo_severidad["OK"],
                "warning":  conteo_severidad["WARNING"],
                "error":    conteo_severidad["ERROR"],
                "pct_ok":   round(conteo_severidad["OK"] / max(1, total_filas) * 100, 1),
            },
            "problemas_por_tipo":          dict(conteo_alertas),
            "impacto_total_soles":         round(impacto_total, 2),
        },
        "paginacion": {
            "page":         page,
            "page_size":    page_size,
            "total_items":  total_items_filtrados,
            "total_pages":  total_pages,
            "has_next":     page < total_pages,
            "has_prev":     page > 1
        },
        "detalle": paginated_detalle,
        "nota": nota,
    }


# ════════════════════════════════════════════════════════════════════════════
# Exportar a Excel (solo problemas)
# ════════════════════════════════════════════════════════════════════════════

@router.get("/by-office/export", dependencies=[Depends(require_operador_or_admin)])
async def costs_by_office_export(
    office_id: Optional[int] = Query(None, description="Si se pasa, filtra a esa sucursal."),
    days: int = Query(90, ge=7, le=365, description="Ventana de ventas en días"),
    umbral_margen_bajo: float = Query(20.0, ge=0, le=100, description="% mínimo de margen aceptable"),
    umbral_margen_alto: float = Query(70.0, ge=0, le=100, description="% máximo de margen razonable (sospechoso si supera)"),
    umbral_outlier_pct: float = Query(50.0, ge=0, description="% de desviación vs promedio para considerar outlier"),
    umbral_desactualizado_pct: float = Query(20.0, ge=0, description="% de diferencia vs última recepción"),
    umbral_ratio_max_min: float = Query(2.0, ge=1.0, description="Ratio MAX/MIN entre sucursales para variación alta"),
    incluir_igv_en_margen: bool = Query(True, description="Si es True, calcula margen usando Precio Bruto. Si es False, usa Precio Neto."),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
):
    """Genera un Excel descargable con los productos que tienen problemas de costos (ERROR/WARNING)."""
    
    # Cargar sucursales objetivo desde config
    from harvester.config import OFFICES_TIENDA
    if office_id is not None:
        sucursales = [office_id] if office_id in OFFICES_TIENDA else OFFICES_TIENDA
    else:
        sucursales = OFFICES_TIENDA
    
    # 1. Leer el SQL
    sql_path = _SQL_DIR / "09_costos_por_sucursal.sql"
    sql_text = sql_path.read_text(encoding="utf-8")
    
    params = {
        "days": days,
        "sucursales_objetivo": sucursales,
        "umbral_margen_bajo": umbral_margen_bajo,
        "umbral_margen_alto": umbral_margen_alto,
        "umbral_outlier_pct": umbral_outlier_pct,
        "umbral_desactualizado_pct": umbral_desactualizado_pct,
        "umbral_ratio_max_min": umbral_ratio_max_min,
        "incluir_igv_en_margen": incluir_igv_en_margen,
    }
    
    # 2. Ejecutar la consulta
    result = await db.execute(text(sql_text), params)
    rows = result.mappings().all()
    
    # 3. Filtrar y agrupar la data por SKU
    grouped_data = {}
    for r in rows:
        sev = r["severidad"]
        if sev == "OK":
            continue
            
        vid = int(r["bsale_variant_id"])
        
        if vid not in grouped_data:
            grouped_data[vid] = {
                "SKU": r["codigo_sku"],
                "PRODUCTO": r["producto"],
                "sucursales": [],
                "severidades": set(),
                "alertas": set(),
                "costos": {},
                "precios": {},
            }
            
        item = grouped_data[vid]
        suc = r["sucursal"]
        
        item["sucursales"].append(suc)
        item["severidades"].add(sev)
        
        if r["alertas"]:
            item["alertas"].update(list(r["alertas"]))
            
        item["costos"][suc] = float(r["costo_efectivo"] or 0)
        item["precios"][suc] = float(r["precio_venta"] or 0)
        
    export_data = []
    for vid, item in grouped_data.items():
        # Severidad global (si hay un ERROR, es ERROR, sino WARNING)
        sev_global = "ERROR" if "ERROR" in item["severidades"] else "WARNING"
        
        # Alertas unificadas
        alertas_global = ", ".join(sorted(item["alertas"]))
        
        # Sucursales afectadas
        sucursales_str = ", ".join(sorted(item["sucursales"]))
        
        # Costo unificado
        costos_unicos = set(item["costos"].values())
        if len(costos_unicos) == 1:
            costo_str = str(list(costos_unicos)[0])
        else:
            costo_str = " | ".join([f"{v} ({k})" for k, v in item["costos"].items()])
            
        # Precio unificado
        precios_unicos = set(item["precios"].values())
        if len(precios_unicos) == 1:
            precio_str = str(list(precios_unicos)[0])
        else:
            precio_str = " | ".join([f"{v} ({k})" for k, v in item["precios"].items()])
            
        export_data.append({
            "BSALE_VARIANT_ID": vid,
            "SKU": item["SKU"],
            "PRODUCTO": item["PRODUCTO"],
            "SUCURSALES_AFECTADAS": sucursales_str,
            "SEVERIDAD_GLOBAL": sev_global,
            "ALERTAS_CONSOLIDADAS": alertas_global,
            "COSTO_ACTUAL": costo_str,
            "PRECIO_VENTA": precio_str,
            "NUEVO_COSTO": ""
        })
        
    # Si no hay data problemática, devolver un excel vacío con los headers
    if not export_data:
        export_data = [{
            "BSALE_VARIANT_ID": "", "SKU": "", "PRODUCTO": "", 
            "SUCURSALES_AFECTADAS": "", "SEVERIDAD_GLOBAL": "NO HAY PROBLEMAS", 
            "ALERTAS_CONSOLIDADAS": "", "COSTO_ACTUAL": "", "PRECIO_VENTA": "", 
            "NUEVO_COSTO": ""
        }]
        
    # 4. Generar el Excel en memoria
    df = pd.DataFrame(export_data)
    
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Costos_a_Corregir')
        
    buffer.seek(0)
    
    # 5. Devolver el archivo
    return StreamingResponse(
        buffer, 
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=costos_a_corregir.xlsx"}
    )
