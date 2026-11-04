"""
Genera las propuestas de reclasificación hacia la taxonomía v2 (SOLO LECTURA).

Salidas en Nueva_estructura/reportes/:
  - reclasificacion_v2.csv  productos cuya subcategoría v2 propuesta difiere de
                            la que tendrán tras migrar (o que hoy no tienen).
                            Columna `aplicar` vacía: marcar SI/NO al revisar.
  - mapeo_tipos_v2.csv      tipos de BSale sin mapear o con nombre fuera del
                            árbol, con la subcategoría v2 sugerida y el nombre
                            "Categoría / Subcategoría" para renombrarlos.

Incluye por defecto los productos INACTIVOS: tienen ventas históricas y, si no
se reclasifican, los reportes de meses pasados quedan con la clasificación vieja
(ej. la pistola de hidrogel P38). Con --solo-activos se excluyen.

La subcategoría "actual" de cada producto se traduce con migracion_v1_v2.json,
así la propuesta compara contra donde quedará el producto después de migrar.

Uso:
    python -m tools.taxonomy.build_reclassification --company-id 1
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from tools.taxonomy._common import (
    REPORTS,
    company_session,
    fetch_products,
    load_ops,
    load_v2_tree,
    write_csv,
)

from sqlalchemy import text  # noqa: E402

from app.services.taxonomy_migration import translate_path  # noqa: E402
from app.services.taxonomy_rules import GENERIC_TYPES, propose, remap_type  # noqa: E402
from app.services.attribute_inference import norm  # noqa: E402


def _fmt(p) -> str:
    return " > ".join(p) if p else ""


async def run(company_id: int, include_inactive: bool) -> None:
    ops = load_ops()
    tree = load_v2_tree()
    v2_paths = {(d, c, s) for d, cs in tree.items() for c, ss in cs.items() for s in ss}
    by_bsale_name = {norm(f"{c} / {s}"): (d, c, s) for d, c, s in v2_paths}

    async with company_session(company_id) as db:
        products = await fetch_products(db, company_id, include_inactive)
        types = (await db.execute(text("""
            SELECT pt.bsale_product_type_id, pt.name, pt.is_active,
                   s.name AS sub, c.name AS cat, d.name AS dep,
                   (SELECT count(*) FROM products p WHERE p.company_id = pt.company_id
                       AND p.bsale_product_type_id = pt.bsale_product_type_id AND p.is_active) AS productos_activos,
                   (SELECT count(*) FROM products p WHERE p.company_id = pt.company_id
                       AND p.bsale_product_type_id = pt.bsale_product_type_id AND NOT p.is_active) AS productos_inactivos
            FROM product_types pt
            LEFT JOIN subcategories s ON s.company_id = pt.company_id AND s.id = pt.subcategory_id
            LEFT JOIN categories c ON c.company_id = s.company_id AND c.id = s.category_id
            LEFT JOIN departments d ON d.company_id = c.company_id AND d.id = c.department_id
            WHERE pt.company_id = :c
            ORDER BY pt.name
        """), {"c": company_id})).mappings().all()

    rows, stats = [], Counter()
    for p in products:
        current = None
        if p["subcategory"]:
            current = translate_path((p["department"], p["category"], p["subcategory"]), ops)
        prop = propose(p["name"], p["type_name"], current)
        stats["productos"] += 1
        if current is None:
            stats["sin_clasificar"] += 1
        if not prop and current is not None:
            continue
        target, motivo, regla = prop if prop else (None, "Sin propuesta: clasificar a mano", "")
        stats["propuestas" if prop else "sin_propuesta"] += 1
        stats[f"regla:{regla}"] += 1 if regla else 0
        rows.append({
            "aplicar": "",
            "bsale_product_id": p["bsale_product_id"],
            "sku": p["sku"],
            "codigo_barra": p["bar_code"],
            "producto": p["name"],
            "activo": "SI" if p["is_active"] else "NO",
            "tipo_bsale_actual": p["type_name"] or "",
            "clasificacion_v2_actual": _fmt(current),
            "propuesta_v2": _fmt(target),
            "tipo_bsale_sugerido": f"{target[1]} / {target[2]}" if target else "",
            "motivo": motivo,
            "regla": regla,
        })

    type_rows = []
    for t in types:
        current = translate_path((t["dep"], t["cat"], t["sub"]), ops) if t["sub"] else None
        suggested = current or remap_type(t["name"]) or by_bsale_name.get(norm(t["name"]))
        generic = norm(t["name"]) in GENERIC_TYPES
        expected_name = f"{suggested[1]} / {suggested[2]}" if suggested else ""
        if current and expected_name == t["name"]:
            continue
        type_rows.append({
            "bsale_product_type_id": t["bsale_product_type_id"],
            "tipo_bsale": t["name"],
            "activo": "SI" if t["is_active"] else "NO",
            "productos_activos": t["productos_activos"],
            "productos_inactivos": t["productos_inactivos"],
            "mapeo_v2_actual": _fmt(current),
            "subcategoria_v2_sugerida": _fmt(suggested) if not generic else "",
            "nombre_bsale_sugerido": expected_name if not generic else "",
            "nota": ("Tipo genérico: reclasificar cada producto (ver reclasificacion_v2.csv)" if generic
                     else "" if suggested else "Sin sugerencia: mapear a mano"),
        })

    fields = ["aplicar", "bsale_product_id", "sku", "codigo_barra", "producto", "activo", "tipo_bsale_actual",
              "clasificacion_v2_actual", "propuesta_v2", "tipo_bsale_sugerido", "motivo", "regla"]
    write_csv(REPORTS / "reclasificacion_v2.csv", rows, fields)
    write_csv(REPORTS / "mapeo_tipos_v2.csv", type_rows,
              ["bsale_product_type_id", "tipo_bsale", "activo", "productos_activos", "productos_inactivos",
               "mapeo_v2_actual",
               "subcategoria_v2_sugerida", "nombre_bsale_sugerido", "nota"])

    print(f"productos analizados: {stats['productos']}  (sin clasificar hoy: {stats['sin_clasificar']})")
    print(f"propuestas de cambio: {stats['propuestas']}  ·  sin propuesta: {stats['sin_propuesta']}")
    for k, v in sorted(((k, v) for k, v in stats.items() if k.startswith("regla:")), key=lambda x: -x[1])[:25]:
        print(f"  {v:4d}  {k[6:]}")
    print(f"tipos de BSale a revisar: {len(type_rows)}")
    print(f"escrito: {REPORTS / 'reclasificacion_v2.csv'}")
    print(f"escrito: {REPORTS / 'mapeo_tipos_v2.csv'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--company-id", type=int, required=True)
    ap.add_argument("--solo-activos", action="store_true", help="Excluye productos inactivos")
    args = ap.parse_args()
    asyncio.run(run(args.company_id, not args.solo_activos))


if __name__ == "__main__":
    main()
