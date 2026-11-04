"""
Arma UN Excel para revisar y aprobar todo lo que cambiaría (SOLO LECTURA).

Hojas: Resumen · Decisiones · Cambios estructura · Mapa v1 → v2 · Árbol v2 ·
Reclasificar · Tipos BSale (1 por subcategoría) · Tipos BSale sin mapear ·
Atributos · Nombres a corregir · Variantes sin nombre.

Las ventas son de los últimos 12 meses (documentos de venta activos, sin notas
de crédito) y el Árbol v2 ya incluye las reclasificaciones propuestas.

Requiere haber corrido antes build_reclassification e infer_attributes (usa
sus CSV de Nueva_estructura/reportes).

Uso:
    python -m tools.taxonomy.build_review_excel --company-id 1
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from tools.taxonomy._common import (
    BASE,
    REPORTS,
    company_session,
    fetch_products,
    load_ops,
    load_v2_tree,
    read_csv,
)

from sqlalchemy import text  # noqa: E402

from app.services.attribute_inference import norm  # noqa: E402
from app.services.taxonomy_migration import translate_path  # noqa: E402
from app.services.taxonomy_rules import remap_type  # noqa: E402

HEAD_FILL = PatternFill("solid", fgColor="1F4E78")
HEAD_FONT = Font(bold=True, color="FFFFFF")
CHANGE_FILL = PatternFill("solid", fgColor="FCE4D6")
NEW_FILL = PatternFill("solid", fgColor="E2EFDA")
WARN_FILL = PatternFill("solid", fgColor="FFF2CC")
WRAP = Alignment(wrap_text=True, vertical="top")
MONEY = '"S/ "#,##0'
PCT = '0.00"%"'

OP_TXT = {
    "rename": "Renombrar", "move": "Mover", "merge": "Fusionar en",
    "create": "Crear", "drop_empty": "Eliminar si queda vacío",
}
MAX_BSALE_NAME = 45

DECISIONES = [
    ("Lo asiático", "Sigue en su departamento 'Alimentos y Bebidas Asiáticas' (en BSale se ve). Nombres paralelos: "
                    "'Galletas Dulces' / 'Galletas Asiáticas'. El atributo Origen permite sumar ambos."),
    ("Perfumes", "No se fusionan: siguen Perfumes de Hombre / Mujer / Infantiles (así se busca en el POS). "
                 "Se agregan Body Spray y Sets de Regalo."),
    ("Vidrio vs plástico", "Tapers de Vidrio y Jarras de Vidrio/Plástico siguen separados (precio distinto)."),
    ("Ollas, Sartenes y Teteras", "Nueva familia 'Menaje' con una subcategoría cada una: Ollas / Sartenes / Teteras."),
    ("Regla de nombres", "Categoría = familia corta; subcategoría = el producto como lo pide el cliente. "
                         "En BSale el tipo es 'Categoría / Subcategoría'."),
    ("Nombres repetidos", "Se eliminan los 'X / X': Repostería / Moldes para Repostería, Tazas / Tazas y Mugs, "
                          "Piñatas / Piñatas Infantiles, Muñecas / Muñecas y Princesas; Peluches Kawaii y Llaveros "
                          "Kawaii conservan su nombre."),
    ("Un tipo BSale por subcategoría", "Si una subcategoría queda con varios tipos de BSale, se conserva el que tiene "
                                       "más productos; los demás se vacían moviendo sus productos y se renombran "
                                       "'(NO USAR) …'. Ningún tipo se borra."),
    ("Profundidad", "Se separa lo que más vende: Cuadernos / Blocks y Sketchbooks; Papel Bond / Cartulinas / "
                    "Lustre-Crepé-Seda / Papelotes; Colores / Crayolas y Óleos; Plumones Escolares / Pizarra y "
                    "Permanentes / Resaltadores; Lápices / Lapiceros; Galletas Dulces / Saladas / Wafers; Tomatodos "
                    "Infantiles / Deportivos; Repostería: Moldes / Utensilios."),
    ("Productos distintos, subcategorías distintas", "Revisión 3: no se junta lo que es otro producto. Cuchillos / "
                                                     "Afiladores; Ganchos / Colgadores de Ropa; Toallitas Básicas / "
                                                     "con Licencia; Borradores / Tajadores; Globos / Infladores / "
                                                     "Gorritos y Cotillón; Caramelos / Chupetines; Fuentes y Bandejas / "
                                                     "Bowls y Tazones; Esponjas / Paños y Guantes; Focos / Luces y "
                                                     "Lámparas; Mochilas / Maletas con Ruedas; Jugos / Bebidas con Nata "
                                                     "de Coco."),
    ("Categorías nuevas en Librería", "Librería es el 21% de la venta: 'Colores y Dibujo' (Colores, Crayolas y Óleos, "
                                      "Plumones Escolares), 'Pegamentos' (Siliconas, Gomas y Colas, Pistolas de "
                                      "Silicona) y 'Cintas Adhesivas' (Embalaje y Scotch, Masking, Contact)."),
    ("Hidrogel", "Tiene su propia subcategoría: 'Armas de Juguete / Pistolas de Hidrogel'. La P38 que estaba en "
                 "'Hidrogel y Pistolas de Agua' se mueve ahí; esa subcategoría queda como 'Pistolas de Agua' "
                 "(chisquetes). Así se ve el ciclo de la moda: pico dic-2025, cae en feb-2026."),
    ("Perfumes por público", "Lo infantil (colonias y sets Bywin, Hello Kitty, My Melody) se queda en Perfumes "
                             "Infantiles. Se corrigen fragancias de mujer cargadas en Hombre (Cherie, La Belle, Queen "
                             "Million, Cristal) y se marcan para revisar Prestige Ahmed/Aziz/Hassan/Malik."),
    ("Productos inactivos", "Se reclasifican también los inactivos con ventas: si no, el histórico queda con la "
                            "clasificación vieja. La columna Activo lo indica."),
    ("Deportes y Fitness","El departamento no vendió en 12 meses: pasa como categoría a Juguetes y Entretenimiento."),
    ("Tendencias Kawaii", "Se disuelve: Peluches Kawaii → Juguetes; Llaveros Kawaii → Moda. Kawaii queda como "
                          "atributo Estilo y el personaje como Licencia."),
]


def _sheet(wb, title, headers, rows, widths=None, fill_row=None, formats=None):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for c in ws[1]:
        c.fill, c.font, c.alignment = HEAD_FILL, HEAD_FONT, WRAP
    for r in rows:
        ws.append(r)
    for i, h in enumerate(headers, 1):
        w = (widths or {}).get(h) or min(60, max(12, len(h) + 2,
                                                   *(len(str(r[i - 1] or "")) + 2 for r in rows[:300])))
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for row in ws.iter_rows(min_row=2):
        for c in row:
            fmt = (formats or {}).get(headers[c.column - 1])
            if fmt:
                c.number_format = fmt
        fill = fill_row(row) if fill_row else None
        if fill:
            for c in row:
                c.fill = fill
    return ws


def _csv(name):
    p = REPORTS / name
    return read_csv(p) if p.exists() else []


def _p(s: str) -> tuple[str, ...] | None:
    return tuple(x.strip() for x in s.split(" > ")) if s else None


async def run(company_id: int) -> None:
    ops = load_ops()
    v2 = load_v2_tree()
    async with company_session(company_id) as db:
        # Inactivos incluidos: sus ventas históricas cuentan en cada subcategoría.
        products = await fetch_products(db, company_id, include_inactive=True)
        sales = dict((await db.execute(text("""
            SELECT v.bsale_product_id, SUM(dd.total_amount)
            FROM document_details dd
            JOIN documents doc ON doc.company_id = dd.company_id AND doc.bsale_document_id = dd.bsale_document_id
            JOIN variants v ON v.company_id = dd.company_id AND v.bsale_variant_id = dd.bsale_variant_id
            WHERE dd.company_id = :c AND doc.emission_date >= now() - interval '365 days'
              AND doc.is_active AND NOT doc.is_credit_note
            GROUP BY 1"""), {"c": company_id})).all())
        v1_rows = (await db.execute(text("""
            SELECT d.name AS dep, c.name AS cat, s.name AS sub FROM subcategories s
            JOIN categories c ON c.company_id = s.company_id AND c.id = s.category_id
            JOIN departments d ON d.company_id = c.company_id AND d.id = c.department_id
            WHERE s.company_id = :c ORDER BY 1, 2, 3"""), {"c": company_id})).mappings().all()
        types = (await db.execute(text("""
            SELECT pt.bsale_product_type_id AS id, pt.name, pt.is_active,
                   d.name AS dep, c.name AS cat, s.name AS sub
            FROM product_types pt
            LEFT JOIN subcategories s ON s.company_id = pt.company_id AND s.id = pt.subcategory_id
            LEFT JOIN categories c ON c.company_id = s.company_id AND c.id = s.category_id
            LEFT JOIN departments d ON d.company_id = c.company_id AND d.id = c.department_id
            WHERE pt.company_id = :c"""), {"c": company_id})).mappings().all()

    total_sales = float(sum(float(v or 0) for v in sales.values())) or 1.0
    reclas = _csv("reclasificacion_v2.csv")
    proposal = {int(r["bsale_product_id"]): _p(r["propuesta_v2"]) for r in reclas if r["propuesta_v2"]}
    tipos_csv = _csv("mapeo_tipos_v2.csv")
    attrs = _csv("atributos_inferidos.csv")
    nombres = _csv("nombres_a_corregir.csv")
    variantes = _csv("variantes_sin_nombre.csv")

    # Ventas y productos por subcategoría v1 (hoy) y v2 (tras migrar + reclasificar).
    by_v1 = defaultdict(lambda: [0, 0.0])
    by_v2 = defaultdict(lambda: [0, 0.0])
    type_products = Counter()        # activos por tipo
    type_inactive = Counter()        # inactivos por tipo (también hay que moverlos al vaciar un tipo)
    for p in products:
        venta = float(sales.get(p["bsale_product_id"]) or 0)
        v1 = (p["department"], p["category"], p["subcategory"]) if p["subcategory"] else None
        activo = 1 if p["is_active"] else 0
        if v1:
            by_v1[v1][0] += activo
            by_v1[v1][1] += venta
        v2p = proposal.get(p["bsale_product_id"]) or (translate_path(v1, ops) if v1 else None)
        key = v2p or ("(Sin clasificar)", "", "")
        by_v2[key][0] += activo
        by_v2[key][1] += venta
        if p["type_name"] is not None:
            (type_products if activo else type_inactive)[p["type_name"]] += 1

    wb = Workbook()
    wb.remove(wb.active)

    # ── Resumen ──
    mapa_rows = []
    for r in v1_rows:
        p1 = (r["dep"], r["cat"], r["sub"])
        p2 = translate_path(p1, ops)
        n, v = by_v1.get(p1, (0, 0.0))
        mapa_rows.append([" > ".join(p1), " > ".join(p2), "Sí" if p1 != p2 else "No", n, round(v),
                          round(100 * v / total_sales, 2)])
    v2_subs = [(d, c, s) for d, cs in v2.items() for c, ss in cs.items() for s in ss]
    sin_ventas = [s for s in v2_subs if by_v2.get(s, (0, 0))[1] == 0]
    n_cats = sum(len(c) for c in v2.values())
    ws = wb.create_sheet("Resumen")
    for r in [
        ["QUÉ ES ESTE ARCHIVO", "Todo lo que cambiaría al pasar a la taxonomía v2 (revisión 2, con tus ajustes). "
                                "Nada de esto está aplicado todavía."],
        ["", ""],
        ["Taxonomía actual (v1)", f"{len(v1_rows)} subcategorías"],
        ["Taxonomía nueva (v2)", f"{len(v2)} departamentos · {n_cats} categorías · {len(v2_subs)} subcategorías"],
        ["Ventas 12 meses analizadas", round(total_sales)],
        ["Subcategorías que cambian de lugar o nombre", sum(1 for r in mapa_rows if r[2] == "Sí")],
        ["Productos que se propone reclasificar", len(reclas)],
        ["Subcategorías v2 sin ventas en 12 meses", len(sin_ventas)],
        ["Productos con costo/ubicación en el nombre", len(nombres)],
        ["Variantes sin nombre", len(variantes)],
        ["", ""],
        ["GARANTÍAS", "No se borra ningún producto, variante ni venta. Los tipos de BSale no se borran. "
                      "Si la migración cambia el número de productos clasificados, se cancela sola."],
        ["CÓMO APROBAR", "1) Leé 'Decisiones'. 2) Revisá 'Árbol v2' (productos y ventas por subcategoría). "
                         "3) En 'Reclasificar' poné SI o NO en APLICAR. 4) Revisá 'Tipos BSale'. "
                         "5) Avisame qué apruebas y qué cambiarías."],
        ["COLORES", "Naranja = cambia · Verde = nuevo · Amarillo = revisar (sin ventas o nombre largo)"],
    ]:
        ws.append(r)
    ws.column_dimensions["A"].width = 45
    ws.column_dimensions["B"].width = 120
    ws["B5"].number_format = MONEY
    for row in ws.iter_rows():
        row[0].font = Font(bold=True)
        row[1].alignment = WRAP

    _sheet(wb, "Decisiones", ["Tema", "Cómo queda"], [list(d) for d in DECISIONES], {"Tema": 32, "Cómo queda": 140})

    # ── Cambios estructura ──
    op_rows = []
    for i, o in enumerate(ops, 1):
        dest = o.get("to") if o["op"] == "rename" else " > ".join(o.get("to") or o.get("into") or [])
        op_rows.append([i, OP_TXT[o["op"]], " > ".join(o["path"]), dest or "", o.get("why", "")])
    _sheet(wb, "Cambios estructura", ["#", "Acción", "Origen", "Destino / nombre nuevo", "Motivo"], op_rows,
           {"Origen": 60, "Destino / nombre nuevo": 55, "Motivo": 70},
           fill_row=lambda row: NEW_FILL if row[1].value == "Crear" else None)

    # ── Mapa v1 → v2 ──
    _sheet(wb, "Mapa v1 → v2", ["Subcategoría actual (v1)", "Queda en (v2)", "¿Cambia?", "Productos activos",
                               "Venta 12m", "% venta"],
           mapa_rows, {"Subcategoría actual (v1)": 70, "Queda en (v2)": 70},
           fill_row=lambda row: CHANGE_FILL if row[2].value == "Sí" else None,
           formats={"Venta 12m": MONEY, "% venta": PCT})

    # ── Árbol v2 ──
    created = {tuple(o["path"]) for o in ops if o["op"] == "create" and len(o["path"]) == 3}
    arbol = []
    for d, c, s in v2_subs:
        n, v = by_v2.get((d, c, s), (0, 0.0))
        name = f"{c} / {s}"
        nota = []
        if (d, c, s) in created:
            nota.append("Nueva")
        if v == 0:
            nota.append("Sin ventas 12m")
        if len(name) > MAX_BSALE_NAME:
            nota.append(f"Nombre BSale largo ({len(name)})")
        arbol.append([d, c, s, name, n, round(v), round(100 * v / total_sales, 2), " · ".join(nota)])
    n, v = by_v2.get(("(Sin clasificar)", "", ""), (0, 0.0))
    if n:
        arbol.append(["(Sin clasificar)", "", "", "", n, round(v), round(100 * v / total_sales, 2), "Clasificar a mano"])
    _sheet(wb, "Árbol v2", ["Departamento", "Categoría", "Subcategoría", "Nombre en BSale", "Productos activos",
                            "Venta 12m", "% venta", "Nota"], arbol,
           {"Nombre en BSale": 55, "Nota": 40},
           fill_row=lambda row: NEW_FILL if "Nueva" in (row[7].value or "")
           else WARN_FILL if row[7].value else None,
           formats={"Venta 12m": MONEY, "% venta": PCT})

    # ── Reclasificar ──
    _sheet(wb, "Reclasificar",
           ["APLICAR (SI/NO)", "bsale_product_id", "SKU", "Producto", "Activo", "Venta 12m", "Clasificación v2 actual",
            "Propuesta v2", "Motivo", "Tipo BSale actual", "Tipo BSale sugerido", "Regla"],
           [[r["aplicar"], int(r["bsale_product_id"]), r["sku"], r["producto"], r["activo"],
             round(float(sales.get(int(r["bsale_product_id"])) or 0)), r["clasificacion_v2_actual"],
             r["propuesta_v2"], r["motivo"], r["tipo_bsale_actual"], r["tipo_bsale_sugerido"], r["regla"]]
            for r in reclas],
           {"Producto": 50, "Clasificación v2 actual": 55, "Propuesta v2": 55, "Motivo": 35},
           formats={"Venta 12m": MONEY})

    # ── Tipos BSale: uno por subcategoría ──
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for t in types:
        if not t["is_active"]:
            continue
        target = translate_path((t["dep"], t["cat"], t["sub"]), ops) if t["sub"] else remap_type(t["name"])
        if target:
            groups[target].append({**dict(t), "productos": type_products.get(t["name"], 0),
                                   "inactivos": type_inactive.get(t["name"], 0)})
    tipo_rows = []
    for d, c, s in v2_subs:
        expected = f"{c} / {s}"
        ts = sorted(groups.get((d, c, s), []), key=lambda t: (-t["productos"], -t["inactivos"], t["id"]))
        if not ts:
            tipo_rows.append([d, expected, "", "", 0, 0, "CREAR TIPO",
                              f"Crear en BSale el tipo '{expected}'"])
            continue
        keep = ts[0]
        tipo_rows.append([d, expected, keep["id"], keep["name"], keep["productos"], keep["inactivos"],
                          "CONSERVAR" if keep["name"] == expected else "RENOMBRAR",
                          "" if keep["name"] == expected else f"Renombrar a '{expected}'"])
        for t in ts[1:]:
            tipo_rows.append([d, expected, t["id"], t["name"], t["productos"], t["inactivos"], "VACIAR",
                              f"Mover sus productos (activos e inactivos) al tipo {keep['id']} "
                              f"y renombrar '(NO USAR) {t['name']}'"])
    fills = {"CREAR TIPO": NEW_FILL, "RENOMBRAR": CHANGE_FILL, "VACIAR": WARN_FILL}
    _sheet(wb, "Tipos BSale", ["Departamento", "Tipo BSale final (Cat / Sub)", "bsale_product_type_id",
                               "Nombre actual en BSale", "Productos activos", "Productos inactivos", "Acción",
                               "Detalle"],
           tipo_rows, {"Tipo BSale final (Cat / Sub)": 55, "Nombre actual en BSale": 50, "Detalle": 75},
           fill_row=lambda row: fills.get(row[6].value))

    _sheet(wb, "Tipos BSale sin mapear",
           ["bsale_product_type_id", "Tipo BSale", "Activo", "Productos activos", "Productos inactivos",
            "Subcategoría v2 sugerida", "Nombre BSale sugerido", "Nota"],
           [[int(r["bsale_product_type_id"]), r["tipo_bsale"], r["activo"], int(r["productos_activos"] or 0),
             int(r.get("productos_inactivos") or 0), r["subcategoria_v2_sugerida"], r["nombre_bsale_sugerido"],
             r["nota"]]
            for r in tipos_csv if not r["mapeo_v2_actual"]],
           {"Tipo BSale": 50, "Subcategoría v2 sugerida": 55, "Nota": 45})

    if attrs:
        cols = list(attrs[0].keys())
        _sheet(wb, "Atributos", cols, [[r[c] for c in cols] for r in attrs],
               {"producto": 50, "clasificacion_actual": 50})
    _sheet(wb, "Nombres a corregir", ["bsale_product_id", "SKU", "Producto", "Costo en el nombre",
                                      "Prefijo / ubicación", "Activo"],
           [[r["bsale_product_id"], r["sku"], r["producto"], r["costo_en_nombre"], r["prefijo_logistico"], r["activo"]]
            for r in nombres], {"Producto": 60})
    _sheet(wb, "Variantes sin nombre", ["bsale_product_id", "Producto", "Producto activo", "bsale_variant_id",
                                        "SKU", "Variantes del producto", "Color", "Diseño", "Talla", "Sabor / aroma"],
           [[r["bsale_product_id"], r["producto"], r.get("producto_activo", ""), r["bsale_variant_id"], r["sku"],
             r["variantes_del_producto"], r["color"], r["diseno"], r["talla"], r["sabor_aroma"]] for r in variantes],
           {"Producto": 45})

    out = BASE / "REVISION_taxonomia_v2.xlsx"
    wb.save(out)
    acciones = Counter(r[6] for r in tipo_rows)
    print(f"escrito: {out}")
    print(f"tipos BSale: {dict(acciones)} · subcategorías sin ventas: {len(sin_ventas)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--company-id", type=int, required=True)
    args = ap.parse_args()
    asyncio.run(run(args.company_id))


if __name__ == "__main__":
    main()
