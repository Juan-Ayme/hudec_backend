import io
import time
from datetime import date, datetime
from decimal import Decimal
from collections import defaultdict
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from analytics.excel_builder import (
    HEADER_FONT, HEADER_FILL, HEADER_ALIGN, BORDER_THIN,
    CAT_FILL, CAT_FONT, SUBCAT_FILL, SUBCAT_FONT, TAB_COLORS,
    _sheet_name, ancho_columna, _coerce_numeric, _is_money_col
)

def _is_stock_col(name: str) -> bool:
    n = name.strip().lower()
    return "stock" in n or "unidades" in n

def _is_date_col(name: str) -> bool:
    n = name.strip().lower()
    return "fecha" in n or "recepción" in n or "recepcion" in n or "venta" in n

def _is_aggregable_col(name: str) -> bool:
    """Detecta si una columna es sumable en cabeceras (stock y valorizaciones, no precios unitarios)."""
    n = name.strip()
    if "Unitario" in n or n.startswith("Precio Venta") or n.startswith("Costo "):
        return False
    return _is_stock_col(n) or _is_money_col(n)

def build_inventario_workbook(
    *,
    cols: list[str],
    rows: list[tuple],
    titulo: str,
    brand_name: str,
    sucursal: str | None = None,
    elapsed_seconds: float = 0.0,
) -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)

    idx = {name: cols.index(name) for name in cols}
    idx_depto = idx.get("Departamento")
    idx_cat = idx.get("Categoría")
    idx_subcat = idx.get("Subcategoría")

    # Columnas numéricas agregables: stock y valorizaciones en S/ (excluyendo precios unitarios)
    idx_aggregable = [
        (cols.index(c), c) for c in cols
        if _is_aggregable_col(c)
    ]

    # Agrupar por departamento
    by_dept: dict[str, list[tuple]] = defaultdict(list)
    if idx_depto is not None:
        for r in rows:
            dept = str(r[idx_depto]) if r[idx_depto] else "— Sin departamento"
            by_dept[dept].append(r)
    else:
        by_dept["Datos"] = list(rows)

    # 1. Portada
    _build_portada_inv(wb, brand_name, titulo, sucursal, len(rows), cols, rows, idx_aggregable, elapsed_seconds)

    # 2. Índice
    _build_indice_inv(wb, by_dept, cols, idx_aggregable)

    # 3. Hojas por departamento
    used_sheet_names = set(wb.sheetnames)
    sorted_depts = sorted(by_dept.items(), key=lambda kv: -len(kv[1]))
    for tab_idx, (dept_name, dept_rows) in enumerate(sorted_depts):
        _build_hoja_depto_inv(
            wb,
            dept_name=dept_name,
            dept_rows=dept_rows,
            cols=cols,
            used_sheet_names=used_sheet_names,
            idx_cat=idx_cat,
            idx_subcat=idx_subcat,
            idx_aggregable=idx_aggregable,
            tab_color=TAB_COLORS[tab_idx % len(TAB_COLORS)],
        )

    return wb

def _build_portada_inv(
    wb: Workbook, brand_name: str, titulo: str, sucursal: str | None,
    n_rows: int, cols: list[str], rows: list[tuple],
    idx_aggregable: list[tuple[int, str]], elapsed: float
) -> None:
    ws = wb.create_sheet("Portada")
    ws.sheet_properties.tabColor = "1F4E78"
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 65

    ws["A1"] = f"{brand_name.upper()} — INVENTARIO Y CATÁLOGO COMPLETO"
    ws["A1"].font = Font(size=20, bold=True, color="1F4E78")
    ws.merge_cells("A1:B1")
    ws["A1"].alignment = Alignment(horizontal="center")

    suc_txt = sucursal if sucursal else "Consolidado (Todas las sucursales)"
    metadata = [
        ("Reporte:", titulo),
        ("Sucursal:", suc_txt),
        ("Generado:", datetime.now().strftime("%d/%m/%Y %H:%M:%S")),
        ("Total SKUs en catálogo:", n_rows),
        ("Tiempo de consulta:", f"{elapsed:.2f} segundos"),
    ]
    for i, (k, v) in enumerate(metadata, 3):
        ws.cell(row=i, column=1, value=k).font = Font(bold=True)
        ws.cell(row=i, column=2, value=v)

    # Totales destacados
    card_row = 10
    ws.cell(row=card_row, column=1, value="RESUMEN VALORIZADO:").font = Font(bold=True, size=12, color="1F4E78")
    
    # Calcular totales de stock y valorización
    totals = {}
    for col_idx, col_name in idx_aggregable:
        totals[col_name] = sum(_coerce_numeric(r[col_idx]) for r in rows)

    summary_items = []
    # Buscar total disp
    for name in ["Stock Disp Total", "Stock Disp"]:
        if name in totals:
            summary_items.append(("Stock Disponible Total:", f"{totals[name]:,.0f} uds"))
            break
    for name in ["Stock Físico Total", "Stock Físico"]:
        if name in totals:
            summary_items.append(("Stock Físico Total:", f"{totals[name]:,.0f} uds"))
            break
    for name in ["Valor Stock Costo S/"]:
        if name in totals:
            summary_items.append(("Capital en Stock al Costo:", f"S/ {totals[name]:,.2f}"))
            break
    for name in ["Valor Stock Venta S/"]:
        if name in totals:
            summary_items.append(("Valor a Precio de Venta (PVP):", f"S/ {totals[name]:,.2f}"))
            break

    for offset, (k, v) in enumerate(summary_items):
        r = card_row + 1 + offset
        c1 = ws.cell(row=r, column=1, value=k)
        c1.font = Font(bold=True, size=11)
        c2 = ws.cell(row=r, column=2, value=v)
        c2.font = Font(bold=True, size=11, color="003366")

def _build_indice_inv(
    wb: Workbook,
    by_dept: dict[str, list[tuple]],
    cols: list[str],
    idx_aggregable: list[tuple[int, str]],
) -> None:
    ws = wb.create_sheet("Índice")
    ws.sheet_properties.tabColor = "548235"
    ws["A1"] = "Índice de Departamentos"
    ws["A1"].font = Font(size=16, bold=True, color="1F4E78")
    ws.merge_cells("A1:G1")

    # Columnas del índice: Departamento, SKUs, y las agregables principales
    main_agg = [
        (idx, name) for idx, name in idx_aggregable
        if "Total" in name or name in ("Stock Disp", "Stock Físico", "Valor Stock Costo S/", "Valor Stock Venta S/")
    ]
    if not main_agg:
        main_agg = idx_aggregable[:5]

    headers = ["Departamento", "Total SKUs"] + [name for _, name in main_agg]
    for c_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=3, column=c_idx, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN
        cell.border = BORDER_THIN
    ws.row_dimensions[3].height = 28

    sorted_depts = sorted(by_dept.items(), key=lambda kv: -len(kv[1]))
    used = set(wb.sheetnames)
    dept_to_sheet = {d: _sheet_name(d, used) for d, _ in sorted_depts}

    for r_offset, (dept, dept_rows) in enumerate(sorted_depts):
        r_idx = 4 + r_offset
        link_cell = ws.cell(row=r_idx, column=1, value=dept)
        link_cell.hyperlink = f"#'{dept_to_sheet[dept]}'!A1"
        link_cell.font = Font(color="1F4E78", underline="single", bold=True)
        link_cell.border = BORDER_THIN

        sku_cell = ws.cell(row=r_idx, column=2, value=len(dept_rows))
        sku_cell.alignment = Alignment(horizontal="right")
        sku_cell.border = BORDER_THIN

        for col_offset, (col_idx, col_name) in enumerate(main_agg, start=3):
            total = sum(_coerce_numeric(r[col_idx]) for r in dept_rows)
            cell = ws.cell(row=r_idx, column=col_offset, value=round(total, 2))
            cell.alignment = Alignment(horizontal="right")
            cell.border = BORDER_THIN
            if _is_money_col(col_name):
                cell.number_format = '"S/ "#,##0.00'
            else:
                cell.number_format = "#,##0"

    # Fila de totales
    tr = 4 + len(sorted_depts)
    ws.cell(row=tr, column=1, value="TOTAL").font = Font(bold=True)
    ws.cell(row=tr, column=2, value=sum(len(r) for r in by_dept.values())).font = Font(bold=True)
    ws.cell(row=tr, column=2).alignment = Alignment(horizontal="right")
    for col_offset, (col_idx, col_name) in enumerate(main_agg, start=3):
        total = sum(_coerce_numeric(r[col_idx]) for rows in by_dept.values() for r in rows)
        cell = ws.cell(row=tr, column=col_offset, value=round(total, 2))
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="right")
        if _is_money_col(col_name):
            cell.number_format = '"S/ "#,##0.00'
        else:
            cell.number_format = "#,##0"

    ws.column_dimensions["A"].width = 35
    ws.column_dimensions["B"].width = 14
    for col_offset in range(3, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col_offset)].width = 20
    ws.freeze_panes = "A4"
    wb._dept_to_sheet = dept_to_sheet

def _build_hoja_depto_inv(
    wb: Workbook,
    *,
    dept_name: str,
    dept_rows: list[tuple],
    cols: list[str],
    used_sheet_names: set[str],
    idx_cat: int | None,
    idx_subcat: int | None,
    idx_aggregable: list[tuple[int, str]],
    tab_color: str,
) -> None:
    dept_to_sheet = getattr(wb, "_dept_to_sheet", None)
    if dept_to_sheet and dept_name in dept_to_sheet:
        sheet_name = dept_to_sheet[dept_name]
        used_sheet_names.add(sheet_name)
    else:
        sheet_name = _sheet_name(dept_name, used_sheet_names)

    ws = wb.create_sheet(sheet_name)
    ws.sheet_properties.tabColor = tab_color
    ws.sheet_properties.outlinePr.summaryBelow = False

    # Headers
    for c_idx, col_name in enumerate(cols, 1):
        cell = ws.cell(row=1, column=c_idx, value=col_name)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN
        cell.border = BORDER_THIN
    ws.row_dimensions[1].height = 32

    if idx_cat is None or idx_subcat is None:
        _write_data_rows_inv(ws, dept_rows, cols, start_row=2, outline_level=0)
        _finalize_sheet_inv(ws, cols, dept_rows, last_row=1 + len(dept_rows))
        return

    cat_map: dict[str, list[tuple]] = defaultdict(list)
    for r in dept_rows:
        cat_map[str(r[idx_cat]) if r[idx_cat] else "— Sin categoría"].append(r)

    current_row = 2
    # Ordenar categorías alfabéticamente o por SKUs
    for cat in sorted(cat_map.keys()):
        cat_rows = cat_map[cat]
        _write_group_header_inv(
            ws,
            row=current_row,
            level_label=f"📂 {cat}",
            n_skus=len(cat_rows),
            n_cols=len(cols),
            agg_values={idx: sum(_coerce_numeric(r[idx]) for r in cat_rows) for idx, _ in idx_aggregable},
            fill=CAT_FILL,
            font=CAT_FONT,
            idx_aggregable=idx_aggregable,
        )
        ws.row_dimensions[current_row].outline_level = 0
        current_row += 1

        subcat_map: dict[str, list[tuple]] = defaultdict(list)
        for r in cat_rows:
            subcat_map[str(r[idx_subcat]) if r[idx_subcat] else "— Sin subcategoría"].append(r)

        for subcat in sorted(subcat_map.keys()):
            sub_rows = subcat_map[subcat]
            _write_group_header_inv(
                ws,
                row=current_row,
                level_label=f"    📁 {subcat}",
                n_skus=len(sub_rows),
                n_cols=len(cols),
                agg_values={idx: sum(_coerce_numeric(r[idx]) for r in sub_rows) for idx, _ in idx_aggregable},
                fill=SUBCAT_FILL,
                font=SUBCAT_FONT,
                idx_aggregable=idx_aggregable,
            )
            ws.row_dimensions[current_row].outline_level = 1
            current_row += 1

            _write_data_rows_inv(
                ws, sub_rows, cols, start_row=current_row, outline_level=2
            )
            current_row += len(sub_rows)

    _finalize_sheet_inv(ws, cols, dept_rows, last_row=current_row - 1)

def _write_group_header_inv(
    ws: Worksheet,
    *,
    row: int,
    level_label: str,
    n_skus: int,
    n_cols: int,
    agg_values: dict[int, float],
    fill: PatternFill,
    font: Font,
    idx_aggregable: list[tuple[int, str]],
) -> None:
    for c_idx in range(1, n_cols + 1):
        cell = ws.cell(row=row, column=c_idx)
        cell.fill = fill
        cell.font = font
        cell.border = BORDER_THIN
    ws.cell(row=row, column=1, value=level_label)
    ws.cell(row=row, column=2, value=f"{n_skus} SKU{'s' if n_skus != 1 else ''}")
    ws.cell(row=row, column=2).alignment = Alignment(horizontal="left")

    for col_idx_0based, col_name in idx_aggregable:
        total = agg_values.get(col_idx_0based, 0)
        cell = ws.cell(row=row, column=col_idx_0based + 1, value=round(total, 2))
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="right")
        cell.border = BORDER_THIN
        if _is_money_col(col_name):
            cell.number_format = '"S/ "#,##0.00'
        else:
            cell.number_format = "#,##0"

def _write_data_rows_inv(
    ws: Worksheet,
    rows: list[tuple],
    cols: list[str],
    *,
    start_row: int,
    outline_level: int,
) -> None:
    for offset, row in enumerate(rows):
        r_idx = start_row + offset
        for c_idx, (col_name, val) in enumerate(zip(cols, row), 1):
            cell = ws.cell(row=r_idx, column=c_idx)
            cell.border = BORDER_THIN
            if val is None:
                cell.value = ""
            elif isinstance(val, (date, datetime)):
                cell.value = val.strftime("%Y-%m-%d")
                cell.alignment = Alignment(horizontal="center")
            elif isinstance(val, (int, float, Decimal)):
                fval = float(val)
                cell.value = fval
                cell.alignment = Alignment(horizontal="right")
                if _is_money_col(col_name):
                    cell.number_format = '"S/ "#,##0.00'
                elif _is_stock_col(col_name):
                    cell.number_format = "#,##0"
                else:
                    cell.number_format = "#,##0.00"
            else:
                cell.value = str(val)
                cell.alignment = Alignment(horizontal="left")
        if outline_level > 0:
            ws.row_dimensions[r_idx].outline_level = outline_level

def _finalize_sheet_inv(
    ws: Worksheet, cols: list[str], all_rows: list[tuple], last_row: int,
) -> None:
    for c_idx, col_name in enumerate(cols, 1):
        col_letter = get_column_letter(c_idx)
        values = [r[c_idx - 1] for r in all_rows[:100]]
        ws.column_dimensions[col_letter].width = ancho_columna(values, col_name)
    ws.freeze_panes = "A2"
    if last_row >= 2:
        last_col_letter = get_column_letter(len(cols))
        ws.auto_filter.ref = f"A1:{last_col_letter}{last_row}"
