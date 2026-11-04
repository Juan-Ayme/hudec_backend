"""
Fase 5: nombrar las variantes que hoy se ven iguales en el POS.

El problema son los productos con VARIAS variantes y todas (o casi) sin
nombre: "BOLSA REGALO" con 6 variantes que en la caja se ven idénticas. Ningún
dato del sistema dice cuál es la rosada o la grande, así que el nombre lo pone
alguien que ve el producto. Este script arma el Excel con todo lo que ayuda a
reconocer cada variante y después sube a BSale lo que se completó.

Las variantes únicas de su producto no se tocan: el nombre del producto ya las
identifica.

  excel   → Nueva_estructura/nombrar_variantes.xlsx (una fila por variante:
            SKU, código de barras, stock por tienda, última compra y costo,
            ventas 12 meses). Se completa la columna NOMBRE VARIANTE.
  subir   → lee el Excel y pone esos nombres en BSale (description de la
            variante). Sin --apply es simulación. Nunca pisa un nombre que ya
            exista en BSale, y avisa si dos variantes del mismo producto
            quedarían con el mismo nombre.

Incluye productos inactivos (se pueden reactivar después).

Uso:
    python -m tools.taxonomy.variant_names excel --company-id 1
    python -m tools.taxonomy.variant_names subir --company-id 1
    python -m tools.taxonomy.variant_names subir --company-id 1 --apply
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import text

from app.services.attribute_inference import norm
from tools.taxonomy._common import BASE, REPORTS, company_session, write_csv
from tools.taxonomy.bsale_types import _token

XLSX = BASE / "nombrar_variantes.xlsx"
COL_NOMBRE = "NOMBRE VARIANTE"
MAX_LEN = 100

_VARIANTES = """
    WITH prod AS (
        SELECT bsale_product_id
        FROM variants
        WHERE company_id = :c
        GROUP BY bsale_product_id
        HAVING count(*) > 1
           AND count(*) FILTER (WHERE coalesce(trim(description), '') = '') > 0
    ),
    compra AS (
        SELECT DISTINCT ON (rd.bsale_variant_id)
               rd.bsale_variant_id, r.admission_date, rd.cost, rd.quantity,
               coalesce(r.document_number::text, r.document_ref::text) AS doc, r.note
        FROM reception_details rd
        JOIN receptions r ON r.company_id = rd.company_id AND r.bsale_reception_id = rd.bsale_reception_id
        WHERE rd.company_id = :c AND NOT coalesce(r.is_transfer, false)
          AND coalesce(r.note, '') NOT ILIKE 'Ajuste de Stock%%'
          AND rd.quantity BETWEEN 1 AND 50000
        ORDER BY rd.bsale_variant_id, r.admission_date DESC NULLS LAST
    ),
    venta AS (
        SELECT dd.bsale_variant_id,
               sum(dd.quantity) FILTER (WHERE d.emission_date >= now() - interval '365 days') AS und_12m,
               max(d.emission_date) AS ultima_venta
        FROM document_details dd
        JOIN documents d ON d.company_id = dd.company_id AND d.bsale_document_id = dd.bsale_document_id
        WHERE dd.company_id = :c AND d.is_active AND NOT coalesce(d.is_credit_note, false)
        GROUP BY dd.bsale_variant_id
    )
    SELECT p.bsale_product_id, p.name AS producto, p.is_active AS producto_activo,
           pt.name AS tipo_bsale,
           v.bsale_variant_id, v.code, v.bar_code, coalesce(trim(v.description), '') AS nombre_actual,
           v.is_active AS variante_activa,
           vc.effective_cost, c.admission_date AS ultima_compra, c.cost AS costo_compra, c.doc,
           ve.und_12m, ve.ultima_venta
    FROM prod
    JOIN products p ON p.company_id = :c AND p.bsale_product_id = prod.bsale_product_id
    JOIN variants v ON v.company_id = :c AND v.bsale_product_id = p.bsale_product_id
    LEFT JOIN product_types pt ON pt.company_id = :c AND pt.bsale_product_type_id = p.bsale_product_type_id
    LEFT JOIN variant_costs vc ON vc.company_id = :c AND vc.bsale_variant_id = v.bsale_variant_id
    LEFT JOIN compra c ON c.bsale_variant_id = v.bsale_variant_id
    LEFT JOIN venta ve ON ve.bsale_variant_id = v.bsale_variant_id
    ORDER BY p.name, p.bsale_product_id, v.bsale_variant_id
"""


def _fmt_date(d) -> str:
    if d is None:
        return ""
    if isinstance(d, (int, float)):  # BSale guarda unix
        import datetime as _dt
        return _dt.datetime.utcfromtimestamp(d).strftime("%Y-%m-%d")
    return str(d)[:10]


def _pista(r: dict, hermanas: list[dict]) -> str:
    """Una ayuda corta para quien completa el Excel."""
    # Diferencias chicas son vaivén del precio de compra; desde 15% suele ser otro artículo.
    costos = [float(h["effective_cost"]) for h in hermanas if h["effective_cost"]]
    if r["effective_cost"] and len(costos) > 1 and max(costos) > min(costos) * 1.15:
        return "costo muy distinto a sus hermanas: puede ser otro tamaño o modelo"
    bars = [h["bar_code"] for h in hermanas if h["bar_code"]]
    if len(set(bars)) == len(bars) and len(bars) > 1:
        return "mismo costo, código de barras distinto: suele ser otro color o diseño"
    return ""


async def build_excel(company_id: int) -> None:
    async with company_session(company_id) as db:
        offices = (await db.execute(text(
            "SELECT bsale_office_id, name FROM offices WHERE company_id = :c AND is_active "
            "AND NOT coalesce(is_virtual, false) ORDER BY bsale_office_id"), {"c": company_id})).all()
        rows = [dict(r) for r in (await db.execute(text(_VARIANTES), {"c": company_id})).mappings().all()]
        stock = defaultdict(dict)
        for vid, oid, q in (await db.execute(text(
                "SELECT bsale_variant_id, bsale_office_id, quantity FROM stock_levels WHERE company_id = :c"),
                {"c": company_id})).all():
            stock[vid][oid] = float(q or 0)

    by_prod: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_prod[r["bsale_product_id"]].append(r)

    wb = Workbook()
    ws = wb.active
    ws.title = "Variantes"
    headers = (["PRIORIDAD", "PRODUCTO", "ID PRODUCTO", "TIPO BSALE", "SKU", "CODIGO DE BARRA", "NOMBRE ACTUAL",
                COL_NOMBRE, "PISTA", "STOCK TOTAL"] + [f"STOCK {n.upper()}" for _, n in offices]
               + ["ULTIMA COMPRA", "COSTO COMPRA", "DOC COMPRA", "COSTO ACTUAL", "VENDIDAS 12M", "ULTIMA VENTA",
                  "PRODUCTO ACTIVO", "VARIANTE ACTIVA", "ID VARIANTE"])
    fill_h = PatternFill("solid", fgColor="D9D9D9")
    fill_edit = PatternFill("solid", fgColor="FFF2CC")
    fill_band = PatternFill("solid", fgColor="F2F2F2")
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=i, value=h)
        c.font, c.fill = Font(bold=True), (fill_edit if h == COL_NOMBRE else fill_h)
        c.alignment = Alignment(wrap_text=True, vertical="center")

    def prioridad(lst: list[dict]) -> int:
        st = sum(sum(stock[x["bsale_variant_id"]].values()) for x in lst)
        activo = any(x["producto_activo"] for x in lst)
        return 1 if activo and st > 0 else 2 if activo else 3

    orden = sorted(by_prod.values(), key=lambda lst: (prioridad(lst), lst[0]["producto"]))
    n = 2
    for k, lst in enumerate(orden):
        pr = prioridad(lst)
        for r in lst:
            st = stock[r["bsale_variant_id"]]
            vals = ([{1: "1 · activo con stock", 2: "2 · activo sin stock", 3: "3 · inactivo"}[pr],
                     r["producto"], r["bsale_product_id"], r["tipo_bsale"], r["code"], r["bar_code"],
                     r["nombre_actual"], "", "" if r["nombre_actual"] else _pista(r, lst),
                     sum(st.values())] + [st.get(oid, 0) for oid, _ in offices]
                    + [_fmt_date(r["ultima_compra"]), float(r["costo_compra"]) if r["costo_compra"] else None,
                       r["doc"], float(r["effective_cost"]) if r["effective_cost"] else None,
                       float(r["und_12m"] or 0), _fmt_date(r["ultima_venta"]),
                       "SI" if r["producto_activo"] else "NO", "SI" if r["variante_activa"] else "NO",
                       r["bsale_variant_id"]])
            for i, v in enumerate(vals, 1):
                c = ws.cell(row=n, column=i, value=v)
                if headers[i - 1] == COL_NOMBRE:
                    c.fill = fill_edit
                elif k % 2:
                    c.fill = fill_band
            n += 1
    widths = {"PRIORIDAD": 18, "PRODUCTO": 40, "TIPO BSALE": 34, COL_NOMBRE: 26, "PISTA": 44,
              "NOMBRE ACTUAL": 20, "SKU": 16, "CODIGO DE BARRA": 16}
    for i, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(h, 12)
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{n - 1}"

    ay = wb.create_sheet("Cómo llenarlo")
    for i, line in enumerate([
        "Qué hacer: escribir en NOMBRE VARIANTE lo que diferencia a esa variante de sus hermanas.",
        "Ejemplos: Rosado · Azul · Kuromi · Cinnamoroll · Grande · Mediano · 30x40 cm · Talla 38 · Fresa.",
        "Si son varios datos, separarlos con ' / ': Rosado / Kuromi.",
        "No repetir el nombre del producto: en la boleta ya sale 'BOLSA REGALO' + el nombre de la variante.",
        "Dos variantes del mismo producto no deberían quedar con el mismo nombre.",
        "Si no se sabe, dejar en blanco: esa variante no se toca.",
        "Si en realidad es el MISMO artículo cargado dos veces, escribir DUPLICADO (no se sube a BSale, queda anotado).",
        "Empezar por PRIORIDAD 1 (activo con stock): se puede ver el producto en la tienda.",
        "Las filas con NOMBRE ACTUAL ya tienen nombre en BSale y no se cambian.",
    ], 1):
        ay.cell(row=i, column=1, value=line)
    ay.column_dimensions["A"].width = 110
    wb.save(XLSX)

    prods = len(by_prod)
    sin = sum(1 for r in rows if not r["nombre_actual"])
    p1 = sum(1 for lst in by_prod.values() if prioridad(lst) == 1)
    print(f"productos con variantes que se ven iguales: {prods} · variantes sin nombre: {sin}")
    print(f"  prioridad 1 (activo con stock): {p1} productos")
    print(f"escrito: {XLSX}")


def _read_excel(path: Path) -> list[dict]:
    ws = load_workbook(path, data_only=True)["Variantes"]
    it = ws.iter_rows(values_only=True)
    head = [str(h or "").strip() for h in next(it)]
    out = []
    for row in it:
        r = dict(zip(head, row))
        if r.get("ID VARIANTE"):
            out.append(r)
    return out


async def subir(company_id: int, path: Path, apply: bool) -> None:
    rows = _read_excel(path)
    filled = [r for r in rows if str(r.get(COL_NOMBRE) or "").strip()]
    async with company_session(company_id) as db:
        actual = {vid: (pid, (d or "").strip()) for vid, pid, d in (await db.execute(text(
            "SELECT bsale_variant_id, bsale_product_id, description FROM variants WHERE company_id = :c"),
            {"c": company_id})).all()}
        token_slug = await _token(db, company_id) if apply else None

    plan, notas = [], []
    por_prod: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in filled:
        vid = int(r["ID VARIANTE"])
        nombre = " ".join(str(r[COL_NOMBRE]).split())[:MAX_LEN]
        pid, desc = actual.get(vid, (None, None))
        item = {"bsale_variant_id": vid, "producto": r.get("PRODUCTO"), "sku": r.get("SKU"),
                "nombre_nuevo": nombre, "resultado": ""}
        if pid is None:
            item["resultado"] = "no existe la variante"
        elif desc:
            item["resultado"] = f"no se toca: ya se llama '{desc}'"
        elif norm(nombre) == "DUPLICADO":
            item["resultado"] = "anotado como duplicado (no se sube)"
        else:
            por_prod[pid][norm(nombre)].append(item)
            plan.append(item)
        if item["resultado"]:
            notas.append(item)
    for pid, nombres in por_prod.items():
        for _, items in nombres.items():
            if len(items) > 1:
                for it in items:
                    it["resultado"] = "REVISAR: otra variante del mismo producto tiene el mismo nombre"
    listos = [p for p in plan if not p["resultado"]]

    print(f"filas con nombre en el Excel: {len(filled)} · a subir: {len(listos)} · "
          f"con nombre repetido en el producto: {sum(1 for p in plan if p['resultado'])} · "
          f"sin cambio: {len(notas)}")
    for p in listos[:15]:
        print(f"  {p['producto']}  [{p['sku']}]  →  {p['nombre_nuevo']}")
    out = REPORTS / "nombres_variantes_subida.csv"
    fields = ["bsale_variant_id", "producto", "sku", "nombre_nuevo", "resultado"]
    if not apply:
        write_csv(out, plan + notas, fields)
        print(f"detalle: {out}")
        print("simulación: no se llamó a BSale")
        return

    from concurrent.futures import ThreadPoolExecutor

    from harvester import bsale_client
    from harvester.tenant_context import tenant_scope

    token, slug = token_slug

    def _push() -> None:
        def one(p: dict) -> None:
            vid = p["bsale_variant_id"]
            cur = bsale_client.fetch(f"https://api.bsale.io/v1/variants/{vid}.json") or {}
            if (cur.get("description") or "").strip():
                p["resultado"] = f"no se toca: en BSale ya se llama '{cur['description']}'"
                return
            resp = bsale_client.put(f"variants/{vid}.json", {"id": vid, "description": p["nombre_nuevo"]})
            p["resultado"] = "ok" if resp and "id" in resp else "error: BSale no confirmó"
        with tenant_scope(company_id, token, slug):
            with ThreadPoolExecutor(max_workers=6) as pool:
                futs = {pool.submit(one, p): p for p in listos}
                for f, p in futs.items():
                    try:
                        f.result()
                    except Exception as exc:
                        p["resultado"] = f"error: {exc}"

    await asyncio.to_thread(_push)
    async with company_session(company_id) as db:
        for p in listos:
            if p["resultado"] == "ok":
                await db.execute(text("UPDATE variants SET description = :d WHERE company_id = :c "
                                      "AND bsale_variant_id = :v"),
                                 {"d": p["nombre_nuevo"], "c": company_id, "v": p["bsale_variant_id"]})
        await db.commit()
    write_csv(out, plan + notas, fields)
    print(f"listo: {sum(p['resultado'] == 'ok' for p in listos)} de {len(listos)} variantes nombradas en BSale "
          f"· detalle: {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("accion", choices=["excel", "subir"])
    ap.add_argument("--company-id", type=int, required=True)
    ap.add_argument("--xlsx", default=str(XLSX))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if a.accion == "excel":
        asyncio.run(build_excel(a.company_id))
    else:
        asyncio.run(subir(a.company_id, Path(a.xlsx), a.apply))


if __name__ == "__main__":
    main()
