"""
Registra en `data_quality_issues` las líneas de recepción con valores imposibles.

Los filtros de `app/routers/_compras_filters.py` ya DESCARTAN estas líneas de
todos los reportes, pero descartar en silencio no alcanza: si nadie las ve,
nadie las corrige en BSale y siguen ahí para siempre. Este script las deja
trazadas para que aparezcan en la auditoría de calidad de datos.

Qué busca:
  - `reception_details.quantity` > el límite de sanidad de la empresa
    (típicamente un código de barras tipeado en el campo cantidad).
  - `reception_details.cost` > el límite de costo unitario.
  - `quantity` o `cost` negativos.

En Kawii hay dos casos históricos que solos suman S/ 4.3 billones — más que
toda la venta de la empresa:

    recepción  1644  30-ago-2025  variante 1319  6 971 108 389 568 und
    recepción 10254  11-may-2026  variante 1486    100 000 000 451 und

Es IDEMPOTENTE: no re-inserta un issue que ya está registrado para la misma
línea y el mismo tipo, así que se puede correr en cada sync sin ensuciar.

Uso:
    python -m tools.maintenance.flag_reception_outliers            # todas las empresas
    python -m tools.maintenance.flag_reception_outliers --company-id 1
    python -m tools.maintenance.flag_reception_outliers --dry-run  # solo reporta
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Bootstrap path — mismo patrón que mt_sync.py
root_path = Path(__file__).resolve().parent.parent.parent
if str(root_path) not in sys.path:
    sys.path.append(str(root_path))

from dotenv import load_dotenv  # noqa: E402
import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from app.config_defaults import DEFAULT_THRESHOLDS  # noqa: E402

logger = logging.getLogger("flag_reception_outliers")

ENTITY = "reception_details"
ISSUE_QTY = "quantity_out_of_range"
ISSUE_COST = "cost_out_of_range"


def _conn():
    load_dotenv()
    import os

    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def _thresholds_for(cur, company_id: int) -> tuple[float, float]:
    """Límites de la empresa, con fallback a los defaults de código."""
    cur.execute(
        "SELECT value FROM app_config WHERE company_id = %s AND key = 'thresholds'",
        (company_id,),
    )
    row = cur.fetchone()
    qty = float(DEFAULT_THRESHOLDS["reception_qty_sanity_limit"])
    cost = float(DEFAULT_THRESHOLDS["reception_cost_sanity_limit"])
    if row and row[0]:
        try:
            data = json.loads(row[0])
            if isinstance(data, dict):
                qty = float(data.get("reception_qty_sanity_limit", qty))
                cost = float(data.get("reception_cost_sanity_limit", cost))
        except (ValueError, TypeError):
            pass
    return qty, cost


# Trae las líneas fuera de rango junto con el issue_type que les corresponde.
# El LEFT JOIN contra data_quality_issues es lo que da la idempotencia.
SQL_OUTLIERS = """
WITH malas AS (
    SELECT rd.company_id,
           rd.bsale_reception_detail_id AS detail_id,
           rd.bsale_reception_id        AS reception_id,
           rd.bsale_variant_id          AS variant_id,
           rd.quantity,
           rd.cost,
           r.admission_date::date       AS fecha,
           r.bsale_office_id            AS office_id,
           CASE
               WHEN rd.quantity > %(qty_max)s OR rd.quantity < 0 THEN %(issue_qty)s
               ELSE %(issue_cost)s
           END AS issue_type,
           CASE
               WHEN rd.quantity > %(qty_max)s OR rd.quantity < 0 THEN 'quantity'
               ELSE 'cost'
           END AS field
      FROM reception_details rd
      JOIN receptions r
        ON r.company_id = rd.company_id
       AND r.bsale_reception_id = rd.bsale_reception_id
     WHERE rd.company_id = %(cid)s
       AND (rd.quantity > %(qty_max)s OR rd.quantity < 0
            OR rd.cost > %(cost_max)s OR rd.cost < 0)
)
SELECT m.*
  FROM malas m
  LEFT JOIN data_quality_issues dq
         ON dq.company_id = m.company_id
        AND dq.entity     = %(entity)s
        AND dq.bsale_id   = m.detail_id
        AND dq.issue_type = m.issue_type
 WHERE dq.id IS NULL
 ORDER BY m.quantity * m.cost DESC
"""

SQL_INSERT = """
INSERT INTO data_quality_issues
    (company_id, entity, bsale_id, field, issue_type, description, raw_value)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""


def run(company_id: int | None, dry_run: bool) -> int:
    """Devuelve la cantidad de issues nuevos encontrados."""
    total_nuevos = 0
    with _conn() as conn:
        with conn.cursor() as cur:
            if company_id is not None:
                cur.execute(
                    "SELECT id, name FROM companies WHERE id = %s", (company_id,)
                )
            else:
                cur.execute(
                    "SELECT id, name FROM companies WHERE is_active ORDER BY id"
                )
            empresas = cur.fetchall()

        if not empresas:
            logger.warning("No hay empresas que procesar.")
            return 0

        for cid, nombre in empresas:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                qty_max, cost_max = _thresholds_for(cur, cid)
                cur.execute(
                    SQL_OUTLIERS,
                    {
                        "cid": cid,
                        "qty_max": qty_max,
                        "cost_max": cost_max,
                        "entity": ENTITY,
                        "issue_qty": ISSUE_QTY,
                        "issue_cost": ISSUE_COST,
                    },
                )
                filas = cur.fetchall()

            if not filas:
                logger.info(
                    "[%s] sin líneas fuera de rango (qty≤%.0f, costo≤%.0f).",
                    nombre,
                    qty_max,
                    cost_max,
                )
                continue

            logger.warning(
                "[%s] %d línea(s) fuera de rango sin registrar:", nombre, len(filas)
            )
            payload = []
            for f in filas:
                impacto = float(f["quantity"] or 0) * float(f["cost"] or 0)
                logger.warning(
                    "    recepción %s · %s · sucursal %s · variante %s · "
                    "%s und × S/ %s = S/ %s",
                    f["reception_id"],
                    f["fecha"],
                    f["office_id"],
                    f["variant_id"],
                    f"{float(f['quantity']):,.0f}",
                    f"{float(f['cost']):,.4f}",
                    f"{impacto:,.0f}",
                )
                descripcion = (
                    f"Línea de recepción fuera de rango: {float(f['quantity']):,.0f} und "
                    f"× S/ {float(f['cost']):,.4f} = S/ {impacto:,.0f}. "
                    f"Recepción {f['reception_id']} del {f['fecha']} "
                    f"en sucursal {f['office_id']}, variante {f['variant_id']}. "
                    f"Límites vigentes: cantidad ≤ {qty_max:,.0f}, "
                    f"costo unitario ≤ {cost_max:,.0f}. "
                    f"Excluida de los reportes de compras; corregir en BSale."
                )
                raw = json.dumps(
                    {
                        "quantity": float(f["quantity"]),
                        "cost": float(f["cost"]),
                        "bsale_reception_id": f["reception_id"],
                        "bsale_variant_id": f["variant_id"],
                        "bsale_office_id": f["office_id"],
                        "admission_date": str(f["fecha"]),
                    }
                )
                payload.append(
                    (cid, ENTITY, f["detail_id"], f["field"], f["issue_type"],
                     descripcion, raw)
                )

            total_nuevos += len(payload)
            if dry_run:
                logger.info("[%s] dry-run: no se escribió nada.", nombre)
                continue

            # `data_quality_issues` tiene Row-Level Security: la policy WITH CHECK
            # exige company_id = current_company_id(). El backend y el harvester
            # setean el tenant con SET LOCAL; acá hacemos lo mismo. SET LOCAL es
            # transaccional, así que va en el mismo cursor y antes del commit.
            with conn.cursor() as cur:
                cur.execute("SET LOCAL app.current_company = %s", (str(cid),))
                cur.executemany(SQL_INSERT, payload)
            conn.commit()
            logger.info("[%s] %d issue(s) registrados.", nombre, len(payload))

    return total_nuevos


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--company-id", type=int, default=None)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Reporta lo que encontraría sin escribir en data_quality_issues.",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )

    nuevos = run(args.company_id, args.dry_run)
    if nuevos:
        logger.info(
            "Total: %d issue(s) %s.",
            nuevos,
            "detectados (dry-run)" if args.dry_run else "registrados",
        )
    else:
        logger.info("Todo limpio — no hay líneas fuera de rango sin registrar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
