"""
Fase 2: lleva los atributos de VARIANTE (color, diseño, talla, sabor/aroma) a BSale.

Dos partes:

  1. Nombre de la variante (--nombres): llena `description` de las variantes que
     hoy NO tienen nombre con "Color / Diseño / Talla / Sabor" desde variant_attrs.
     Es lo que se ve en el POS y la boleta. Nunca pisa un nombre existente.
     Usa PUT /variants/{id}.json.

  2. Atributos por tipo de producto (siempre, solo informe): lista qué atributos
     habría que crear en cada tipo de BSale para cargar esos valores como
     atributos de variante. Todavía NO se crean por API: hay que verificar que
     la API de BSale lo permita (hoy solo los leemos con
     /product_types/{id}/attributes.json). Mientras tanto se crean a mano en
     BSale y el harvester los trae de vuelta.

Incluye por defecto las variantes de productos INACTIVOS (así quedan con nombre
si se reactivan). Con --solo-activos se excluyen.

Sin --apply es una simulación: informa y no llama a BSale.

Uso:
    python -m tools.taxonomy.push_variant_attributes --company-id 1 --nombres
    python -m tools.taxonomy.push_variant_attributes --company-id 1 --nombres --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections import defaultdict

from tools.taxonomy._common import REPORTS, company_session, write_csv

from sqlalchemy import text  # noqa: E402

from app.services.attribute_catalog import DEFS_BY_CODE, VARIANT_CODES  # noqa: E402

_ORDER = ["color", "diseno", "talla", "sabor_aroma"]


def variant_label(values: dict[str, str]) -> str:
    """'Rosado / Kuromi / Talla 38'. Solo los atributos presentes, en orden fijo."""
    parts = []
    for code in _ORDER:
        v = (values.get(code) or "").strip()
        if not v:
            continue
        parts.append(f"Talla {v}" if code == "talla" and not v.upper().startswith("TALLA") else v)
    return " / ".join(parts)[:100]


async def _token(db, cid: int) -> tuple[str, str]:
    key = os.environ.get("TOKEN_ENCRYPTION_KEY")
    if not key:
        raise SystemExit("TOKEN_ENCRYPTION_KEY no configurada")
    row = (await db.execute(text(
        "SELECT slug, pgp_sym_decrypt(bsale_token, :key)::text AS token FROM companies "
        "WHERE id = :cid AND is_active AND bsale_token IS NOT NULL"), {"cid": cid, "key": key})).mappings().first()
    if not row:
        raise SystemExit(f"No hay token BSale activo para la empresa {cid}")
    return row["token"], row["slug"]


async def run(company_id: int, nombres: bool, apply: bool, include_inactive: bool = True) -> None:
    push_codes = [c for c in VARIANT_CODES if DEFS_BY_CODE[c].push_to_bsale]
    async with company_session(company_id) as db:
        if not await db.scalar(text("SELECT to_regclass('variant_attrs') IS NOT NULL")):
            raise SystemExit("Falta aplicar docs/migrations/2026-11-02_product_attributes.sql")
        rows = (await db.execute(text("""
            SELECT va.bsale_variant_id, va.code, va.value_text, v.description,
                   pt.bsale_product_type_id, pt.name AS tipo
            FROM variant_attrs va
            JOIN variants v ON v.company_id = va.company_id AND v.bsale_variant_id = va.bsale_variant_id
            LEFT JOIN products p ON p.company_id = v.company_id AND p.bsale_product_id = v.bsale_product_id
            LEFT JOIN product_types pt ON pt.company_id = p.company_id
                                      AND pt.bsale_product_type_id = p.bsale_product_type_id
            WHERE va.company_id = :c AND va.code = ANY(:codes) AND (v.is_active OR :inc)
        """), {"c": company_id, "codes": push_codes, "inc": include_inactive})).mappings().all()
        token_slug = await _token(db, company_id) if (nombres and apply) else None

    by_variant: dict[int, dict] = defaultdict(dict)
    meta: dict[int, dict] = {}
    by_type: dict[tuple, set] = defaultdict(set)
    for r in rows:
        by_variant[r["bsale_variant_id"]][r["code"]] = r["value_text"]
        meta[r["bsale_variant_id"]] = r
        by_type[(r["bsale_product_type_id"], r["tipo"])].add(DEFS_BY_CODE[r["code"]].name)

    write_csv(REPORTS / "atributos_bsale_por_tipo.csv",
              [{"bsale_product_type_id": k[0], "tipo_bsale": k[1], "atributos_a_crear": ", ".join(sorted(v))}
               for k, v in sorted(by_type.items(), key=lambda x: str(x[0][1]))],
              ["bsale_product_type_id", "tipo_bsale", "atributos_a_crear"])
    print(f"tipos de BSale que necesitan atributos: {len(by_type)} → {REPORTS / 'atributos_bsale_por_tipo.csv'}")

    if not nombres:
        return
    if apply:
        # El nombre inferido sale casi siempre del nombre del PRODUCTO: en productos de
        # una variante no agrega nada y en los de varias les pone a todas el mismo.
        raise SystemExit("--nombres --apply quedó reemplazado por: python -m tools.taxonomy.variant_names "
                         "(Excel para nombrar a mano las variantes que se ven iguales)")
    plan = [
        {"bsale_variant_id": vid, "nombre_nuevo": variant_label(vals)}
        for vid, vals in by_variant.items()
        if not (meta[vid]["description"] or "").strip() and variant_label(vals)
    ]
    write_csv(REPORTS / "nombres_variantes_bsale.csv", plan, ["bsale_variant_id", "nombre_nuevo"])
    print(f"variantes sin nombre que se nombrarían: {len(plan)} → {REPORTS / 'nombres_variantes_bsale.csv'}")
    if not apply:
        print("simulación: no se llamó a BSale")
        return

    from harvester import bsale_client
    from harvester.tenant_context import tenant_scope

    token, slug = token_slug
    ok, errors = 0, []

    def _push() -> None:
        nonlocal ok
        with tenant_scope(company_id, token, slug):
            for item in plan:
                try:
                    bsale_client.put(f"variants/{item['bsale_variant_id']}.json",
                                     {"id": item["bsale_variant_id"], "description": item["nombre_nuevo"]})
                    ok += 1
                except Exception as exc:  # se informa y se sigue con el resto
                    errors.append({**item, "error": str(exc)})

    await asyncio.to_thread(_push)
    print(f"variantes renombradas en BSale: {ok} · errores: {len(errors)}")
    if errors:
        write_csv(REPORTS / "nombres_variantes_errores.csv", errors, ["bsale_variant_id", "nombre_nuevo", "error"])
    print("el próximo sync trae los nombres nuevos a la BD.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--company-id", type=int, required=True)
    ap.add_argument("--nombres", action="store_true", help="Nombrar en BSale las variantes sin descripción")
    ap.add_argument("--apply", action="store_true", help="Llama a BSale (por defecto simulación)")
    ap.add_argument("--solo-activos", action="store_true", help="Excluye variantes de productos inactivos")
    args = ap.parse_args()
    asyncio.run(run(args.company_id, args.nombres, args.apply, not args.solo_activos))


if __name__ == "__main__":
    main()
