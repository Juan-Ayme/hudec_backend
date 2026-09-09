"""Excel del Comparador temporal (dos ventanas de fecha cara a cara).

Jerárquico Departamento → Categoría → Subcategoría → SKU, con las columnas de
comparación: venta A vs B, unidades, velocidad (rotación) A vs B con su delta,
stock actual, cobertura, sugerencia de compra y veredicto. Mismo estilo visual
que `excel_compras_historico.py`.

Lo consume `app/routers/compras.py::comparador_excel`. Recibe los SKUs YA
enriquecidos (velocidades, deltas, sugerencia, veredicto ya calculados en el
router) — este módulo solo dibuja.
"""

from collections import defaultdict

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

C_DEPT_BG = "1F2937"
C_CAT_BG = "374151"
C_SUBCAT_BG = "4B5563"
C_FG = "FFFFFF"

FILL_VERDE = PatternFill("solid", fgColor="D1FAE5")   # green-100 (sugerencia)
FILL_AMBAR = PatternFill("solid", fgColor="FEF3C7")   # amber-100 (revisar)
FILL_ROJO = PatternFill("solid", fgColor="FEE2E2")    # red-100

INT_FMT = "#,##0"
DEC1_FMT = "0.0"
DEC2_FMT = "0.00"
PCT_FMT = '+0.0"%";-0.0"%"'
MONEY_FMT = '"S/" #,##0.00'

_BORDER = Border(bottom=Side(style="thin", color="E5E7EB"),
                 top=Side(style="thin", color="E5E7EB"))

# Colores por veredicto → resaltan la acción de un vistazo.
_VEREDICTO_FILL = {
    "REPONER":         FILL_VERDE,
    "REVISAR_DEMANDA": FILL_AMBAR,
    "EXCESO_STOCK":    FILL_ROJO,
}
_VEREDICTO_FG = {
    "REPONER":         ("065F46", True),
    "REVISAR_DEMANDA": ("92400E", True),
    "EN_ALZA":         ("1D4ED8", True),
    "EXCESO_STOCK":    ("991B1B", True),
    "SIN_MOVIMIENTO":  ("6B7280", False),
    "OK":              ("374151", False),
}


def _num(v) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def build_comparador_workbook(
    skus: list[dict],
    ventana_a: tuple[str, str],
    ventana_b: tuple[str, str],
    dias_cobertura: int,
    sucursal: str,
    brand_name: str,
    alertas: list[str],
) -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("Comparador")

    # ¿Alguna fila tiene stock histórico de la ventana A? Si no, se omite la col.
    hay_stock_a = any(s.get("stock_ventana_a_unds") is not None for s in skus)

    headers = [
        "Nivel / SKU", "Nombre",
        "Venta A (S/)", "Venta B (S/)", "Δ Venta %",
        "Unds A", "Unds B",
        "Veloc. A (u/d)", "Veloc. B (u/d)", "Δ Veloc %",
    ]
    if hay_stock_a:
        headers.append("Stock A")
    headers += ["Stock Actual", "Cobertura (d)",
                f"Sugerencia ({dias_cobertura}d)", "Veredicto",
                "Categoría", "Subcategoría"]

    ncols = len(headers)
    # Índices de columna (1-based) para formateo — dependen de hay_stock_a.
    def col(name: str) -> int:
        return headers.index(name) + 1

    # ── Banner ──
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
    c = ws.cell(1, 1, f"{brand_name} — Comparador temporal")
    c.font = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor="1F2937")
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 25

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncols)
    c = ws.cell(2, 1, (f"A (referencia): {ventana_a[0]} → {ventana_a[1]}   |   "
                       f"B (comparación): {ventana_b[0]} → {ventana_b[1]}   |   {sucursal}"))
    c.font = Font(name="Calibri", size=11, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor="3B82F6")
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 22

    row = 3
    if alertas:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncols)
        c = ws.cell(row, 1, "⚠ " + "  ·  ".join(alertas))
        c.font = Font(name="Calibri", size=9, italic=True, color="92400E")
        c.fill = PatternFill("solid", fgColor="FEF3C7")
        c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        ws.row_dimensions[row].height = 26
        row += 1

    # ── Headers ──
    hdr_row = row
    for i, h in enumerate(headers, 1):
        cell = ws.cell(hdr_row, i, h)
        cell.font = Font(name="Calibri", size=10, bold=True, color="1F2937")
        cell.fill = PatternFill("solid", fgColor="F3F4F6")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = Border(bottom=Side(style="thin", color="D1D5DB"))
    ws.row_dimensions[hdr_row].height = 30
    row = hdr_row + 1

    # ── Agrupar dept → cat → subcat ──
    tree: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for s in skus:
        tree[s["departamento"]][s["categoria"]][s["subcategoria"]].append(s)

    def _sum(items, key):
        return sum(_num(x.get(key)) for x in items)

    def _write_group_row(r: int, label_icon: str, label: str, items: list[dict],
                         bg: str, size: int, level: int):
        for ci in range(1, ncols + 1):
            cc = ws.cell(r, ci)
            cc.fill = PatternFill("solid", fgColor=bg)
            cc.border = _BORDER
        ws.cell(r, 1, label_icon).font = Font(size=size, bold=True, color=C_FG)
        ws.cell(r, 1).alignment = Alignment(horizontal="left", vertical="center")
        cell = ws.cell(r, 2, label)
        cell.font = Font(size=size, bold=True, color=C_FG)
        cell.alignment = Alignment(horizontal="left", vertical="center")
        # Subtotales de montos/unidades/sugerencia.
        totals = {
            "Venta A (S/)": (_sum(items, "venta_a_pen"), MONEY_FMT),
            "Venta B (S/)": (_sum(items, "venta_b_pen"), MONEY_FMT),
            "Unds A": (_sum(items, "unds_a"), INT_FMT),
            "Unds B": (_sum(items, "unds_b"), INT_FMT),
            "Stock Actual": (_sum(items, "stock_actual_unds"), INT_FMT),
            f"Sugerencia ({dias_cobertura}d)": (_sum(items, "sugerencia_compra"), INT_FMT),
        }
        for name, (val, fmt) in totals.items():
            cc = ws.cell(r, col(name), val)
            cc.font = Font(size=size, bold=True, color=C_FG)
            cc.number_format = fmt
            cc.alignment = Alignment(horizontal="right", vertical="center")
        ws.row_dimensions[r].outline_level = level

    for dept in sorted(tree):
        cat_groups = tree[dept]
        all_dept = [s for cg in cat_groups.values() for sg in cg.values() for s in sg]
        _write_group_row(row, "🏢 Departamento", dept, all_dept, C_DEPT_BG, 11, 1)
        row += 1
        for cat in sorted(cat_groups):
            sub_groups = cat_groups[cat]
            all_cat = [s for sg in sub_groups.values() for s in sg]
            _write_group_row(row, "📂 Categoría", cat, all_cat, C_CAT_BG, 10, 2)
            row += 1
            for sub in sorted(sub_groups):
                items = sub_groups[sub]
                _write_group_row(row, "📁 Subcategoría", sub, items, C_SUBCAT_BG, 10, 3)
                row += 1
                # Ordenar SKUs por sugerencia desc dentro de la subcat.
                for i, s in enumerate(sorted(items, key=lambda x: x["sugerencia_compra"], reverse=True)):
                    vals = {
                        "Nivel / SKU": s.get("sku"),
                        "Nombre": s.get("producto"),
                        "Venta A (S/)": _num(s.get("venta_a_pen")),
                        "Venta B (S/)": _num(s.get("venta_b_pen")),
                        "Δ Venta %": s.get("delta_venta_pct"),
                        "Unds A": _num(s.get("unds_a")),
                        "Unds B": _num(s.get("unds_b")),
                        "Veloc. A (u/d)": _num(s.get("velocidad_a")),
                        "Veloc. B (u/d)": _num(s.get("velocidad_b")),
                        "Δ Veloc %": s.get("delta_velocidad_pct"),
                        "Stock Actual": _num(s.get("stock_actual_unds")),
                        "Cobertura (d)": s.get("cobertura_dias"),
                        f"Sugerencia ({dias_cobertura}d)": _num(s.get("sugerencia_compra")),
                        "Veredicto": s.get("veredicto"),
                        "Categoría": cat,
                        "Subcategoría": sub,
                    }
                    if hay_stock_a:
                        vals["Stock A"] = s.get("stock_ventana_a_unds")

                    for name, val in vals.items():
                        ci = col(name)
                        cell = ws.cell(row, ci, val)
                        cell.font = Font(name="Calibri", size=10, color="1F2937")
                        cell.border = _BORDER
                        if i % 2 == 1:
                            cell.fill = PatternFill("solid", fgColor="FAFAFA")
                        # Formato numérico por columna.
                        if name in ("Venta A (S/)", "Venta B (S/)"):
                            cell.number_format = MONEY_FMT
                            cell.alignment = Alignment(horizontal="right")
                        elif name in ("Δ Venta %", "Δ Veloc %"):
                            cell.number_format = PCT_FMT
                            cell.alignment = Alignment(horizontal="right")
                            if isinstance(val, (int, float)):
                                cell.font = Font(size=10, color="065F46" if val >= 0 else "991B1B")
                        elif name in ("Veloc. A (u/d)", "Veloc. B (u/d)"):
                            cell.number_format = DEC2_FMT
                            cell.alignment = Alignment(horizontal="right")
                        elif name in ("Cobertura (d)",):
                            cell.number_format = DEC1_FMT
                            cell.alignment = Alignment(horizontal="right")
                        elif name in ("Unds A", "Unds B", "Stock Actual", "Stock A",
                                      f"Sugerencia ({dias_cobertura}d)"):
                            cell.number_format = INT_FMT
                            cell.alignment = Alignment(horizontal="right")
                        elif name == "Nivel / SKU":
                            cell.font = Font(name="Consolas", size=9, color="4B5563")
                        else:
                            cell.alignment = Alignment(horizontal="left")

                    # Resaltar sugerencia y veredicto.
                    sug_cell = ws.cell(row, col(f"Sugerencia ({dias_cobertura}d)"))
                    if _num(s.get("sugerencia_compra")) > 0:
                        sug_cell.fill = FILL_VERDE
                        sug_cell.font = Font(size=10, bold=True, color="065F46")
                    ver = s.get("veredicto")
                    ver_cell = ws.cell(row, col("Veredicto"))
                    if ver in _VEREDICTO_FILL:
                        ver_cell.fill = _VEREDICTO_FILL[ver]
                    fg, bold = _VEREDICTO_FG.get(ver, ("374151", False))
                    ver_cell.font = Font(size=9, bold=bold, color=fg)
                    ver_cell.alignment = Alignment(horizontal="center")

                    ws.row_dimensions[row].outline_level = 4
                    row += 1

    # ── Anchos ──
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 38
    for name in headers[2:]:
        letter = get_column_letter(col(name))
        ws.column_dimensions[letter].width = 13 if "Nombre" not in name else 38
    ws.column_dimensions[get_column_letter(col("Categoría"))].width = 20
    ws.column_dimensions[get_column_letter(col("Subcategoría"))].width = 20
    ws.column_dimensions[get_column_letter(col("Veredicto"))].width = 16

    ws.freeze_panes = ws.cell(hdr_row + 1, 1)

    if not skus:
        ws.cell(hdr_row + 1, 1, "No hay ventas en ninguna de las dos ventanas.")

    return wb
