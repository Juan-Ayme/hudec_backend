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

from fastapi import APIRouter, Depends, Query
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
