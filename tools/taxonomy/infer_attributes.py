"""
Infiere atributos (marca, licencia, público, origen, estilo, temporada,
material, contenido, unidades por pack/caja, grupo; color/talla/sabor por
variante) desde el nombre, la clasificación ACTUAL y el código de barras.

Incluye por defecto los productos INACTIVOS: tienen ventas históricas (los
reportes por marca o licencia de meses pasados los necesitan) y se pueden
reactivar después. Con --solo-activos se excluyen.

Correrlo ANTES de migrar la taxonomía a v2: la v2 junta tipos (Perfumes
Hombre/Mujer, Alimentos Nacionales/Asiáticos, Tapers de Vidrio…) y ese dato
solo sobrevive si quedó guardado como atributo.

Sin --apply es SOLO LECTURA y deja vistas previas en Nueva_estructura/reportes/:
  - atributos_inferidos.csv   un producto por fila, una columna por atributo
  - nombres_a_corregir.csv    productos con costo / ubicación / proveedor en el nombre
  - variantes_sin_nombre.csv  variantes sin descripción de productos con varias
                              variantes (para completar color / diseño / talla)

Con --apply escribe en product_attrs / variant_attrs con source='inferred'
(requiere la migración 2026-11-02). Nunca pisa valores de driver ni manuales.

Uso:
    python -m tools.taxonomy.infer_attributes --company-id 1
    python -m tools.taxonomy.infer_attributes --company-id 1 --apply
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict

from tools.taxonomy._common import REPORTS, company_session, fetch_products, write_csv

from sqlalchemy import text  # noqa: E402

from app.services.attribute_catalog import PRODUCT_CODES, VARIANT_CODES, ensure_definitions  # noqa: E402
from app.services.attribute_inference import (  # noqa: E402
    DUPLICATE_SKU_PREFIXES,
    Inferred,
    group_key,
    infer_product_attributes,
    infer_variant_attributes,
    name_issues,
)
from app.services.attribute_store import upsert_attr  # noqa: E402


def _fmt(v: Inferred) -> str:
    if v.value_text is not None:
        return v.value_text
    num = int(v.value_num) if v.value_num is not None and float(v.value_num).is_integer() else v.value_num
    return f"{num} {v.unit}".strip() if v.unit and v.unit != "und" else str(num)


async def run(company_id: int, apply: bool, include_inactive: bool) -> None:
    async with company_session(company_id) as db:
        products = await fetch_products(db, company_id, include_inactive)
        variants = (await db.execute(text(
            "SELECT bsale_variant_id, bsale_product_id, description, display_code FROM variants "
            "WHERE company_id = :c AND (is_active OR :inc)"),
            {"c": company_id, "inc": include_inactive})).mappings().all()
        by_product: dict[int, list[dict]] = defaultdict(list)
        for v in variants:
            by_product[v["bsale_product_id"]].append(dict(v))

        # Grupos: mismo artículo cargado con prefijos PL-/TED-/KAW-/JARK-.
        keys: dict[int, str] = {}
        for p in products:
            if (p["sku"] or "").upper().startswith(DUPLICATE_SKU_PREFIXES):
                keys[p["bsale_product_id"]] = group_key(p["name"])
        sizes = Counter(keys.values())

        prod_rows, issue_rows = [], []
        to_write: list[tuple[str, int, str, Inferred]] = []
        coverage = Counter()
        for p in products:
            pid = p["bsale_product_id"]
            attrs = infer_product_attributes(
                p["name"], department=p["department"], category=p["category"],
                subcategory=p["subcategory"], bar_code=p["bar_code"],
            )
            if sizes.get(keys.get(pid), 0) >= 2:
                attrs["grupo"] = Inferred(keys[pid], confidence=0.7, reason="mismo nombre con varios SKU (PL-/TED-/KAW-)")
            for code, val in attrs.items():
                coverage[code] += 1
                to_write.append(("product", pid, code, val))

            vars_ = by_product.get(pid, [])
            for v in vars_:
                vattrs = infer_variant_attributes(
                    p["name"], v["description"], department=p["department"])
                for code, val in vattrs.items():
                    # Sacado solo del nombre del producto: aplica si hay una sola variante.
                    if "variante" not in val.reason and len(vars_) > 1:
                        continue
                    coverage[code] += 1
                    to_write.append(("variant", v["bsale_variant_id"], code, val))
                    attrs.setdefault(f"v:{code}", val)

            prod_rows.append({
                "bsale_product_id": pid, "sku": p["sku"], "producto": p["name"],
                "activo": "SI" if p["is_active"] else "NO",
                "clasificacion_actual": " > ".join(x for x in (p["department"], p["category"], p["subcategory"]) if x),
                **{c: _fmt(attrs[c]) for c in PRODUCT_CODES if c in attrs},
                **{c: _fmt(attrs[f"v:{c}"]) for c in VARIANT_CODES if f"v:{c}" in attrs},
            })
            issues = name_issues(p["name"])
            if issues:
                issue_rows.append({"bsale_product_id": pid, "sku": p["sku"], "producto": p["name"],
                                   "costo_en_nombre": issues.get("costo_en_nombre", ""),
                                   "prefijo_logistico": issues.get("prefijo_logistico", ""),
                                   "activo": "SI" if p["is_active"] else "NO"})

        # Variantes sin nombre en productos con varias variantes: hoy no se
        # distinguen en ventas ni en el POS. Se completan con color/diseño/talla.
        unnamed = [
            {"bsale_product_id": p["bsale_product_id"], "producto": p["name"],
             "bsale_variant_id": v["bsale_variant_id"], "sku": v.get("display_code", ""),
             "variantes_del_producto": len(by_product[p["bsale_product_id"]]),
             "producto_activo": "SI" if p["is_active"] else "NO",
             "color": "", "diseno": "", "talla": "", "sabor_aroma": ""}
            for p in products
            for v in by_product.get(p["bsale_product_id"], [])
            if len(by_product[p["bsale_product_id"]]) > 1 and not (v["description"] or "").strip()
        ]
        write_csv(REPORTS / "variantes_sin_nombre.csv", unnamed,
                  ["bsale_product_id", "producto", "bsale_variant_id", "sku", "variantes_del_producto",
                   "producto_activo", "color", "diseno", "talla", "sabor_aroma"])

        write_csv(REPORTS / "atributos_inferidos.csv", prod_rows,
                  ["bsale_product_id", "sku", "producto", "activo", "clasificacion_actual",
                   *PRODUCT_CODES, *VARIANT_CODES])
        write_csv(REPORTS / "nombres_a_corregir.csv", issue_rows,
                  ["bsale_product_id", "sku", "producto", "costo_en_nombre", "prefijo_logistico", "activo"])

        n = len(products)
        n_inact = sum(1 for p in products if not p["is_active"])
        print(f"productos: {n} (activos {n - n_inact} · inactivos {n_inact})")
        for code in [*PRODUCT_CODES, *VARIANT_CODES]:
            print(f"  {code:15s} {coverage[code]:5d}  ({100 * coverage[code] / n if n else 0:4.1f}%)")
        print(f"nombres con costo/ubicación/proveedor: {len(issue_rows)} "
              f"(con costo: {sum(1 for r in issue_rows if r['costo_en_nombre'])})")
        print(f"variantes sin nombre (en productos con varias variantes): {len(unnamed)}")
        print(f"escrito: {REPORTS / 'variantes_sin_nombre.csv'}")
        print(f"escrito: {REPORTS / 'atributos_inferidos.csv'}")
        print(f"escrito: {REPORTS / 'nombres_a_corregir.csv'}")

        if not apply:
            print("vista previa: no se escribió nada (usá --apply para guardar)")
            return
        if not await db.scalar(text("SELECT to_regclass('product_attrs') IS NOT NULL")):
            raise SystemExit("Falta aplicar docs/migrations/2026-11-02_product_attributes.sql")
        await ensure_definitions(db, company_id)
        written = 0
        for level, entity, code, val in to_write:
            written += await upsert_attr(
                db, company_id, level, entity, code, value_text=val.value_text, value_num=val.value_num,
                unit=val.unit, source="inferred", confidence=val.confidence)
        await db.commit()
        print(f"guardados: {written} valores (los de driver/manual se respetaron: {len(to_write) - written})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--company-id", type=int, required=True)
    ap.add_argument("--apply", action="store_true", help="Escribe en la BD (por defecto solo vista previa)")
    ap.add_argument("--solo-activos", action="store_true", help="Excluye productos inactivos")
    args = ap.parse_args()
    asyncio.run(run(args.company_id, args.apply, not args.solo_activos))


if __name__ == "__main__":
    main()
