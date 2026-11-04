"""
Migra la taxonomía de una empresa a v2 con Nueva_estructura/migracion_v1_v2.json.
Mismo motor y mismas verificaciones que POST /taxonomy/migrate.

Por defecto es una SIMULACIÓN: ejecuta todo dentro de una transacción, informa
y hace rollback. Con --apply confirma, solo si:
  - ya hay atributos inferidos para la empresa (correr antes infer_attributes --apply);
  - no cambió el número de productos, productos clasificados, tipos ni metas.

Antes de --apply guarda un respaldo de la taxonomía actual en
Nueva_estructura/reportes/taxonomia_antes_de_migrar_<empresa>.json.

Uso:
    python -m tools.taxonomy.migrate --company-id 1
    python -m tools.taxonomy.migrate --company-id 1 --apply
    python -m tools.taxonomy.migrate --company-id 1 --targets-on-conflict keep_destination
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime

from tools.taxonomy._common import REPORTS, company_session, load_ops, write_csv

from sqlalchemy import text  # noqa: E402

from app.kawii_matrix import cache as matrix_cache  # noqa: E402
from app.services.taxonomy_migration import MigrationError  # noqa: E402
from app.services.taxonomy_migration_db import (  # noqa: E402
    DbMigrator,
    bsale_type_renames,
    check_invariants,
    taxonomy_snapshot,
)


async def _export(db, cid: int) -> dict:
    rows = (await db.execute(text("""
        SELECT d.name AS dep, c.name AS cat, s.name AS sub FROM departments d
        LEFT JOIN categories c ON c.company_id = d.company_id AND c.department_id = d.id
        LEFT JOIN subcategories s ON s.company_id = c.company_id AND s.category_id = c.id
        WHERE d.company_id = :c ORDER BY 1, 2, 3"""), {"c": cid})).mappings().all()
    tree: dict = {}
    for r in rows:
        d = tree.setdefault(r["dep"], {})
        if r["cat"]:
            c = d.setdefault(r["cat"], {})
            if r["sub"]:
                c[r["sub"]] = []
    return tree


async def run(company_id: int, apply: bool, targets_on_conflict: str) -> int:
    ops = load_ops()
    async with company_session(company_id) as db:
        if apply:
            has = await db.scalar(text("SELECT to_regclass('product_attrs') IS NOT NULL"))
            n = has and await db.scalar(text("SELECT count(*) FROM product_attrs WHERE company_id = :c"),
                                        {"c": company_id})
            if not n:
                print("ABORTADO: no hay atributos inferidos. Corré antes:")
                print(f"  python -m tools.taxonomy.infer_attributes --company-id {company_id} --apply")
                return 2
            backup = REPORTS / f"taxonomia_antes_de_migrar_{company_id}_{datetime.now():%Y%m%d_%H%M}.json"
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(json.dumps(await _export(db, company_id), ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"respaldo: {backup}")

        before = await taxonomy_snapshot(db, company_id)
        migrator = DbMigrator(db, company_id, targets_on_conflict)
        try:
            report = await migrator.apply(ops)
        except MigrationError as e:
            print(f"ABORTADO, no se cambió nada: {e}")
            return 1
        after = await taxonomy_snapshot(db, company_id)
        errors = check_invariants(before, after, len(migrator.targets_dropped))
        renames = await bsale_type_renames(db, company_id)

        print(f"{'APLICANDO' if apply else 'SIMULACIÓN'} — empresa {company_id}")
        for k in before:
            mark = "" if before[k] == after[k] else "  ←"
            print(f"  {k:24s} {before[k]:6d} → {after[k]:6d}{mark}")
        d = report.as_dict()
        print(f"operaciones: {d['applied']} aplicadas · {d['skipped']} omitidas · {d['refused']} rechazadas")
        for r in report.results:
            if r.status != "applied":
                print(f"  #{r.index} {r.op} {' > '.join(r.path)}: {r.status} ({r.detail})")
        if migrator.targets_dropped:
            print(f"metas descartadas (keep_destination): {len(migrator.targets_dropped)}")
        write_csv(REPORTS / "bsale_renombres_sugeridos.csv", renames,
                  ["bsale_product_type_id", "nombre_actual", "nombre_sugerido", "subcategory_id",
                   "tipos_en_la_subcategoria"])
        print(f"tipos de BSale a renombrar (sugerencia): {len(renames)} → {REPORTS / 'bsale_renombres_sugeridos.csv'}")

        if errors:
            print("ABORTADO por verificaciones, no se cambió nada:")
            for e in errors:
                print(f"  - {e}")
            return 1
        if apply:
            await db.commit()
            matrix_cache.invalidate(company_id=company_id)
            print("migración confirmada.")
        else:
            print("simulación: rollback, no se cambió nada (usá --apply para confirmar)")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--company-id", type=int, required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--targets-on-conflict", default="abort", choices=["abort", "keep_destination"])
    args = ap.parse_args()
    raise SystemExit(asyncio.run(run(args.company_id, args.apply, args.targets_on_conflict)))


if __name__ == "__main__":
    main()
