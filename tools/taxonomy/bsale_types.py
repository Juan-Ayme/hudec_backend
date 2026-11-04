"""
Fase 4 — alinea los tipos de producto de BSale con la taxonomía v2.

Subcomandos (todos son SIMULACIÓN salvo que se pase --apply):

  crear       4a. Crea en BSale el tipo "Categoría / Subcategoría" de cada
              subcategoría que tiene productos pero ningún tipo de BSale activo.
              No crea tipos para subcategorías vacías (quedarían como tipos sin
              uso). Si ya existe un tipo sin mapear con ese nombre, lo mapea en
              vez de crear otro. Queda mapeado a su subcategoría en la BD.

  renombrar   4b. Renombra a "Categoría / Subcategoría" el tipo principal de
              cada subcategoría, por departamento. Los tipos secundarios no se
              renombran: se vacían y eliminan en la 4c.

  mover       4c-1. Cambia en BSale el tipo de cada producto (activo o inactivo)
              al tipo de su subcategoría v2, por departamento. Solo productTypeId.
  eliminar-vacios
              4c-2. Elimina en BSale los tipos que quedaron con 0 productos,
              confirmando antes en la API que no tengan ninguno.

Cada corrida deja el detalle en Nueva_estructura/reportes/bsale_4*.csv.

Uso:
    python -m tools.taxonomy.bsale_types crear --company-id 1 [--apply]
    python -m tools.taxonomy.bsale_types renombrar --company-id 1
    python -m tools.taxonomy.bsale_types renombrar --company-id 1 --departamento "Mascotas" [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import os

from tools.taxonomy._common import REPORTS, company_session, write_csv

from sqlalchemy import text  # noqa: E402

from app.kawii_matrix import cache as matrix_cache  # noqa: E402


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


_SUBS_SIN_TIPO = """
    WITH prod AS (   -- productos (activos e inactivos) por subcategoría efectiva
        SELECT COALESCE(p.subcategory_id, pt.subcategory_id) AS sub_id,
               count(*) AS productos,
               count(*) FILTER (WHERE p.is_active) AS activos
        FROM products p
        LEFT JOIN product_types pt ON pt.company_id = p.company_id
                                  AND pt.bsale_product_type_id = p.bsale_product_type_id
        WHERE p.company_id = :c
        GROUP BY 1
    )
    SELECT s.id AS subcategory_id, d.name AS dep, c.name AS cat, s.name AS sub,
           c.name || ' / ' || s.name AS nombre_bsale,
           COALESCE(prod.productos, 0) AS productos, COALESCE(prod.activos, 0) AS activos
    FROM subcategories s
    JOIN categories c  ON c.company_id = s.company_id AND c.id = s.category_id
    JOIN departments d ON d.company_id = c.company_id AND d.id = c.department_id
    LEFT JOIN prod ON prod.sub_id = s.id
    WHERE s.company_id = :c
      AND NOT EXISTS (SELECT 1 FROM product_types pt
                      WHERE pt.company_id = s.company_id AND pt.subcategory_id = s.id AND pt.is_active)
    ORDER BY d.name, c.name, s.name
"""


async def crear(company_id: int, apply: bool) -> None:
    async with company_session(company_id) as db:
        subs = [dict(r) for r in (await db.execute(text(_SUBS_SIN_TIPO), {"c": company_id})).mappings().all()]
        existing = {r["name"].strip().lower(): dict(r) for r in (await db.execute(text(
            "SELECT bsale_product_type_id, name, subcategory_id, is_active FROM product_types WHERE company_id = :c"),
            {"c": company_id})).mappings().all()}

        plan, sin_productos = [], []
        for s in subs:
            if s["productos"] == 0:
                sin_productos.append(s)
                continue
            prev = existing.get(s["nombre_bsale"].lower())
            if prev and prev["is_active"] and prev["subcategory_id"] is None:
                plan.append({**s, "accion": "MAPEAR EXISTENTE", "bsale_product_type_id": prev["bsale_product_type_id"]})
            elif prev and prev["is_active"]:
                plan.append({**s, "accion": "REVISAR: ya existe con otro mapeo",
                             "bsale_product_type_id": prev["bsale_product_type_id"]})
            else:
                plan.append({**s, "accion": "CREAR", "bsale_product_type_id": ""})

        print(f"subcategorías sin tipo BSale activo: {len(subs)}")
        print(f"  con productos → se crea/mapea su tipo: {len(plan)}")
        print(f"  sin productos → NO se crea tipo (quedaría vacío): {len(sin_productos)}")
        for p in plan:
            print(f"  [{p['accion']}] {p['nombre_bsale']}  ({p['productos']} productos, {p['activos']} activos)")

        if not apply:
            _write(plan, sin_productos)
            print("simulación: no se llamó a BSale (usá --apply)")
            return

        token, slug = await _token(db, company_id)
        from harvester import bsale_client
        from harvester.tenant_context import tenant_scope

        def _post(name: str) -> dict:
            with tenant_scope(company_id, token, slug):
                return bsale_client.post("product_types.json", {"name": name})

        ok = 0
        for p in plan:
            if p["accion"].startswith("REVISAR"):
                p["resultado"] = "omitido"
                continue
            try:
                if p["accion"] == "CREAR":
                    resp = await asyncio.to_thread(_post, p["nombre_bsale"])
                    pt_id = resp.get("id")
                    if not pt_id:
                        raise RuntimeError(f"BSale no devolvió id: {resp}")
                    p["bsale_product_type_id"] = pt_id
                await db.execute(text("""
                    INSERT INTO product_types
                        (company_id, bsale_product_type_id, name, subcategory_id, is_active, is_mapped, synced_at)
                    VALUES (:c, :pt, :name, :sub, TRUE, TRUE, now())
                    ON CONFLICT (company_id, bsale_product_type_id) DO UPDATE SET
                        subcategory_id = EXCLUDED.subcategory_id, is_mapped = TRUE, synced_at = now()
                """), {"c": company_id, "pt": p["bsale_product_type_id"], "name": p["nombre_bsale"],
                       "sub": p["subcategory_id"]})
                await db.commit()   # uno por uno: si falla a mitad, lo creado ya queda registrado
                await db.execute(text(f"SET LOCAL app.current_company = '{int(company_id)}'"))
                p["resultado"] = "ok"
                ok += 1
            except Exception as exc:  # se informa y se sigue
                await db.rollback()
                await db.execute(text(f"SET LOCAL app.current_company = '{int(company_id)}'"))
                p["resultado"] = f"error: {exc}"
        matrix_cache.invalidate(company_id=company_id)
        _write(plan, sin_productos)
        print(f"listo: {ok} de {len(plan)} tipos creados/mapeados en BSale")


_TIPOS_MAPEADOS = """
    SELECT pt.bsale_product_type_id AS id, pt.name AS nombre_actual,
           d.name AS dep, c.name || ' / ' || s.name AS nombre_nuevo, s.id AS subcategory_id,
           (SELECT count(*) FROM products p WHERE p.company_id = pt.company_id
              AND p.bsale_product_type_id = pt.bsale_product_type_id) AS productos
    FROM product_types pt
    JOIN subcategories s ON s.company_id = pt.company_id AND s.id = pt.subcategory_id
    JOIN categories c    ON c.company_id = s.company_id  AND c.id = s.category_id
    JOIN departments d   ON d.company_id = c.company_id  AND d.id = c.department_id
    WHERE pt.company_id = :c AND pt.is_active
    ORDER BY d.name, 4, 6 DESC, 1
"""


async def renombrar(company_id: int, departamento: str | None, apply: bool) -> None:
    """4b. Renombra a 'Categoría / Subcategoría' el tipo principal de cada
    subcategoría (el que tiene más productos). Si una subcategoría tiene varios
    tipos, los demás NO se renombran (quedarían nombres repetidos): se vacían y
    eliminan en la 4c."""
    async with company_session(company_id) as db:
        rows = [dict(r) for r in (await db.execute(text(_TIPOS_MAPEADOS), {"c": company_id})).mappings().all()]
        names_in_use = {r["nombre_actual"].strip().lower(): r["id"] for r in rows}
        seen_subs: set[int] = set()
        plan, otros = [], []
        for r in rows:
            principal = r["subcategory_id"] not in seen_subs
            seen_subs.add(r["subcategory_id"])
            if not principal:
                otros.append({**r, "accion": "SE VACÍA EN 4C"})
                continue
            if r["nombre_actual"] == r["nombre_nuevo"]:
                continue
            clash = names_in_use.get(r["nombre_nuevo"].strip().lower())
            accion = "RENOMBRAR" if clash in (None, r["id"]) else f"REVISAR: el nombre ya lo usa el tipo {clash}"
            plan.append({**r, "accion": accion})

        deps = sorted({r["dep"] for r in plan})
        print("tipos a renombrar por departamento:")
        for d in deps:
            print(f"  {sum(1 for r in plan if r['dep'] == d):3d}  {d}")
        print(f"  total {len(plan)} · tipos secundarios que se vacían en la 4c: {len(otros)}")

        if departamento:
            plan = [r for r in plan if r["dep"] == departamento]
            if not plan:
                raise SystemExit(f"No hay tipos para renombrar en '{departamento}'")
            print(f"\n{departamento}:")
            for r in plan:
                print(f"  [{r['accion']}] {r['nombre_actual']}  →  {r['nombre_nuevo']}  ({r['productos']} productos)")

        out = REPORTS / f"bsale_4b_renombrar{'_' + departamento.replace(' ', '_') if departamento else ''}.csv"
        if not apply or not departamento:
            write_csv(out, plan + ([] if departamento else otros),
                      ["accion", "dep", "id", "nombre_actual", "nombre_nuevo", "productos"])
            print(f"detalle: {out}")
            print("simulación: no se llamó a BSale" + ("" if departamento else
                  " (para aplicar indicá --departamento y --apply)"))
            return

        token, slug = await _token(db, company_id)
        from harvester import bsale_client
        from harvester.tenant_context import tenant_scope

        def _put(pt_id: int, name: str) -> dict:
            with tenant_scope(company_id, token, slug):
                # BSale exige el id también en el cuerpo (sin él responde 400 "invalid product type").
                return bsale_client.put(f"product_types/{pt_id}.json", {"id": pt_id, "name": name})

        ok = 0
        for r in plan:
            if r["accion"] != "RENOMBRAR":
                r["resultado"] = "omitido"
                continue
            try:
                await asyncio.to_thread(_put, r["id"], r["nombre_nuevo"])
                await db.execute(text("UPDATE product_types SET name = :n, synced_at = now() "
                                      "WHERE company_id = :c AND bsale_product_type_id = :i"),
                                 {"n": r["nombre_nuevo"], "c": company_id, "i": r["id"]})
                await db.commit()
                await db.execute(text(f"SET LOCAL app.current_company = '{int(company_id)}'"))
                r["resultado"] = "ok"
                ok += 1
            except Exception as exc:  # se informa y se sigue
                await db.rollback()
                await db.execute(text(f"SET LOCAL app.current_company = '{int(company_id)}'"))
                r["resultado"] = f"error: {exc}"
        matrix_cache.invalidate(company_id=company_id)
        write_csv(out, plan, ["accion", "dep", "id", "nombre_actual", "nombre_nuevo", "productos", "resultado"])
        print(f"listo: {ok} de {len(plan)} tipos renombrados en BSale · detalle: {out}")


# Tipo destino por subcategoría: el que ya tiene el nombre v2; si no, el de más productos.
_DESTINOS = """
    SELECT DISTINCT ON (s.id)
           s.id AS subcategory_id, pt.bsale_product_type_id AS destino, pt.name AS destino_nombre
    FROM subcategories s
    JOIN categories c ON c.company_id = s.company_id AND c.id = s.category_id
    JOIN product_types pt ON pt.company_id = s.company_id AND pt.subcategory_id = s.id AND pt.is_active
    WHERE s.company_id = :c
    ORDER BY s.id,
             (pt.name = c.name || ' / ' || s.name) DESC,
             (SELECT count(*) FROM products p WHERE p.company_id = pt.company_id
                AND p.bsale_product_type_id = pt.bsale_product_type_id) DESC,
             pt.bsale_product_type_id
"""

_A_MOVER = """
    WITH efectiva AS (
        SELECT p.bsale_product_id, p.name, p.is_active, p.bsale_product_type_id AS tipo_actual,
               pt.name AS tipo_actual_nombre, p.subcategory_id AS override,
               COALESCE(p.subcategory_id, pt.subcategory_id) AS sub_id
        FROM products p
        LEFT JOIN product_types pt ON pt.company_id = p.company_id
                                  AND pt.bsale_product_type_id = p.bsale_product_type_id
        WHERE p.company_id = :c
    )
    SELECT e.*, d.destino, d.destino_nombre, dep.name AS dep
    FROM efectiva e
    JOIN ({destinos}) d ON d.subcategory_id = e.sub_id
    JOIN subcategories s ON s.company_id = :c AND s.id = e.sub_id
    JOIN categories c ON c.company_id = :c AND c.id = s.category_id
    JOIN departments dep ON dep.company_id = :c AND dep.id = c.department_id
    WHERE e.tipo_actual IS DISTINCT FROM d.destino
    ORDER BY dep.name, d.destino_nombre, e.name
"""


async def mover(company_id: int, departamento: str | None, apply: bool) -> None:
    """4c-1. Cambia en BSale el tipo de cada producto (activo o inactivo) cuyo
    tipo no es el tipo destino de su subcategoría v2. Solo cambia productTypeId:
    precio, stock, variantes y códigos no se tocan. En la BD actualiza el tipo y
    quita el override interno (el tipo de BSale ya dice lo mismo)."""
    async with company_session(company_id) as db:
        rows = [dict(r) for r in (await db.execute(
            text(_A_MOVER.format(destinos=_DESTINOS)), {"c": company_id})).mappings().all()]
        sin_destino = await db.scalar(text("""
            SELECT count(*) FROM products p
            LEFT JOIN product_types pt ON pt.company_id = p.company_id
                                      AND pt.bsale_product_type_id = p.bsale_product_type_id
            WHERE p.company_id = :c AND COALESCE(p.subcategory_id, pt.subcategory_id) IS NULL"""),
            {"c": company_id})

        print("productos a cambiar de tipo en BSale, por departamento:")
        for d in sorted({r["dep"] for r in rows}):
            sel = [r for r in rows if r["dep"] == d]
            print(f"  {len(sel):4d}  ({sum(r['is_active'] for r in sel)} activos)  {d}")
        print(f"  total {len(rows)} · sin clasificar (no se tocan): {sin_destino}")

        if departamento:
            rows = [r for r in rows if r["dep"] == departamento]
        out = REPORTS / f"bsale_4c_mover{'_' + departamento.replace(' ', '_') if departamento else ''}.csv"
        fields = ["dep", "bsale_product_id", "name", "is_active", "tipo_actual", "tipo_actual_nombre",
                  "destino", "destino_nombre", "resultado"]
        if not apply or not departamento:
            write_csv(out, rows, fields)
            print(f"detalle: {out}")
            print("simulación: no se llamó a BSale" + ("" if departamento else
                  " (para aplicar indicá --departamento y --apply)"))
            return

        token, slug = await _token(db, company_id)
        from concurrent.futures import ThreadPoolExecutor
        from harvester import bsale_client
        from harvester.tenant_context import tenant_scope

        def _put_all(items: list[dict]) -> None:
            def one(r: dict) -> None:
                pid = r["bsale_product_id"]
                resp = bsale_client.put(f"products/{pid}.json", {"id": pid, "productTypeId": r["destino"]})
                if not resp or "id" not in resp:
                    raise RuntimeError("BSale no confirmó el cambio")
            with tenant_scope(company_id, token, slug):
                with ThreadPoolExecutor(max_workers=6) as pool:
                    futs = {pool.submit(one, r): r for r in items}
                    for f, r in futs.items():
                        try:
                            f.result()
                            r["resultado"] = "ok"
                        except Exception as exc:  # se informa y se sigue
                            r["resultado"] = f"error: {exc}"

        await asyncio.to_thread(_put_all, rows)
        ok_ids = [r["bsale_product_id"] for r in rows if r["resultado"] == "ok"]
        for r in rows:
            if r["resultado"] != "ok":
                continue
            await db.execute(text("""
                UPDATE products SET bsale_product_type_id = :t, subcategory_id = NULL
                WHERE company_id = :c AND bsale_product_id = :p
            """), {"t": r["destino"], "c": company_id, "p": r["bsale_product_id"]})
        await db.commit()
        matrix_cache.invalidate(company_id=company_id)
        write_csv(out, rows, fields)
        print(f"listo: {len(ok_ids)} de {len(rows)} productos cambiados de tipo en BSale · detalle: {out}")


async def eliminar_vacios(company_id: int, apply: bool) -> None:
    """4c-2. Elimina en BSale los tipos ACTIVOS que quedaron con 0 productos
    (activos e inactivos). Antes de cada uno confirma en la API de BSale que no
    tenga productos; si tiene, no se toca. En la BD queda inactivo y sin mapeo."""
    async with company_session(company_id) as db:
        vacios = [dict(r) for r in (await db.execute(text("""
            SELECT pt.bsale_product_type_id AS id, pt.name
            FROM product_types pt
            WHERE pt.company_id = :c AND pt.is_active
              AND NOT EXISTS (SELECT 1 FROM products p WHERE p.company_id = pt.company_id
                              AND p.bsale_product_type_id = pt.bsale_product_type_id)
            ORDER BY pt.name"""), {"c": company_id})).mappings().all()]
        print(f"tipos activos sin ningún producto en la BD: {len(vacios)}")
        for v in vacios:
            print(f"  {v['id']:5d}  {v['name']}")
        out = REPORTS / "bsale_4c_eliminar.csv"
        if not apply:
            write_csv(out, vacios, ["id", "name", "resultado"])
            print(f"detalle: {out}")
            print("simulación: no se llamó a BSale")
            return

        token, slug = await _token(db, company_id)
        from harvester import bsale_client
        from harvester.config import BSALE_BASE_URL
        from harvester.tenant_context import tenant_scope

        def _delete_if_empty(items: list[dict]) -> None:
            with tenant_scope(company_id, token, slug):
                for v in items:
                    try:
                        # todos, y aparte los inactivos (state=1), por si el listado por defecto los omite
                        base = f"{BSALE_BASE_URL}/products.json?producttypeid={v['id']}&limit=1"
                        n = bsale_client.fetch(base).get("count")
                        n_inact = bsale_client.fetch(base + "&state=1").get("count")
                        if n is None or n_inact is None or int(n) > 0 or int(n_inact) > 0:
                            v["resultado"] = f"no se elimina: BSale reporta {n} productos ({n_inact} inactivos)"
                            continue
                        bsale_client.delete(f"product_types/{v['id']}.json")
                        v["resultado"] = "eliminado"
                    except Exception as exc:
                        v["resultado"] = f"error: {exc}"

        await asyncio.to_thread(_delete_if_empty, vacios)
        for v in vacios:
            if v["resultado"] == "eliminado":
                await db.execute(text("UPDATE product_types SET is_active = FALSE, subcategory_id = NULL, "
                                      "is_mapped = FALSE, synced_at = now() "
                                      "WHERE company_id = :c AND bsale_product_type_id = :i"),
                                 {"c": company_id, "i": v["id"]})
        await db.commit()
        matrix_cache.invalidate(company_id=company_id)
        write_csv(out, vacios, ["id", "name", "resultado"])
        n_ok = sum(v["resultado"] == "eliminado" for v in vacios)
        print(f"listo: {n_ok} de {len(vacios)} tipos vacíos eliminados en BSale · detalle: {out}")


def _write(plan: list[dict], sin_productos: list[dict]) -> None:
    rows = plan + [{**s, "accion": "NO CREAR (sin productos)", "bsale_product_type_id": ""} for s in sin_productos]
    write_csv(REPORTS / "bsale_4a_crear.csv", rows,
              ["accion", "nombre_bsale", "dep", "cat", "sub", "productos", "activos",
               "bsale_product_type_id", "resultado"])
    print(f"detalle: {REPORTS / 'bsale_4a_crear.csv'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("accion", choices=["crear", "renombrar", "mover", "eliminar-vacios"])
    ap.add_argument("--company-id", type=int, required=True)
    ap.add_argument("--departamento", help="renombrar: aplica solo a este departamento")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if args.accion == "crear":
        asyncio.run(crear(args.company_id, args.apply))
    elif args.accion == "renombrar":
        asyncio.run(renombrar(args.company_id, args.departamento, args.apply))
    elif args.accion == "mover":
        asyncio.run(mover(args.company_id, args.departamento, args.apply))
    elif args.accion == "eliminar-vacios":
        asyncio.run(eliminar_vacios(args.company_id, args.apply))


if __name__ == "__main__":
    main()
