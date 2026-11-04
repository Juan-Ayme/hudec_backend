"""
Aplica las filas APROBADAS de reportes/reclasificacion_v2.csv como override
interno (`products.subcategory_id`). Correrlo DESPUÉS de migrar la taxonomía a v2.

  - Solo toma filas con `aplicar` = SI.
  - No toca BSale ni borra nada: el override se puede quitar con
    PATCH /products/{id}/subcategory. El tipo de producto en BSale se cambia
    aparte con POST /products/bulk-change-type, si se quiere.
  - Sin --apply es una simulación (informa y no escribe).

Uso:
    python -m tools.taxonomy.apply_reclassification --company-id 1
    python -m tools.taxonomy.apply_reclassification --company-id 1 --apply
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from tools.taxonomy._common import REPORTS, company_session, read_csv

from sqlalchemy import text  # noqa: E402

from app.kawii_matrix import cache as matrix_cache  # noqa: E402


async def run(company_id: int, csv_path: Path, apply: bool) -> None:
    rows = [r for r in read_csv(csv_path) if r.get("aplicar", "").strip().upper() in ("SI", "SÍ", "S", "X")]
    print(f"filas aprobadas: {len(rows)}")
    if not rows:
        return
    async with company_session(company_id) as db:
        subs = (await db.execute(text("""
            SELECT s.id, d.name AS dep, c.name AS cat, s.name AS sub
            FROM subcategories s
            JOIN categories c ON c.company_id = s.company_id AND c.id = s.category_id
            JOIN departments d ON d.company_id = c.company_id AND d.id = c.department_id
            WHERE s.company_id = :c"""), {"c": company_id})).mappings().all()
        by_path = {(r["dep"], r["cat"], r["sub"]): r["id"] for r in subs}

        ok, missing_sub, missing_prod = 0, [], []
        for r in rows:
            path = tuple(x.strip() for x in r["propuesta_v2"].split(" > "))
            sub_id = by_path.get(path)
            if sub_id is None:
                missing_sub.append(r["propuesta_v2"])
                continue
            res = await db.execute(text(
                "UPDATE products SET subcategory_id = :s WHERE company_id = :c AND bsale_product_id = :p"),
                {"c": company_id, "s": sub_id, "p": int(r["bsale_product_id"])})
            if res.rowcount:
                ok += 1
            else:
                missing_prod.append(r["bsale_product_id"])

        print(f"productos reclasificados: {ok}")
        if missing_sub:
            print(f"subcategorías v2 inexistentes (¿se migró la taxonomía?): {sorted(set(missing_sub))[:10]}")
        if missing_prod:
            print(f"productos no encontrados: {missing_prod[:10]}")
        if apply and not missing_sub:
            await db.commit()
            matrix_cache.invalidate(company_id=company_id)
            print("guardado.")
        else:
            print("simulación: no se guardó nada" + (" (hay subcategorías faltantes)" if apply and missing_sub else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--company-id", type=int, required=True)
    ap.add_argument("--csv", default=str(REPORTS / "reclasificacion_v2.csv"))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    asyncio.run(run(args.company_id, Path(args.csv), args.apply))


if __name__ == "__main__":
    main()
