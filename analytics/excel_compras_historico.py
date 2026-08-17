from collections import defaultdict
from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# Colores y formatos
C_DEPT_BG = "1F2937"
C_CAT_BG = "374151"
C_SUBCAT_BG = "4B5563"
C_FG = "FFFFFF"

FILL_ROJO = PatternFill("solid", fgColor="FEE2E2") # red-100
FILL_VERDE = PatternFill("solid", fgColor="D1FAE5") # green-100

INT_FMT = "#,##0"
DEC1_FMT = "0.0"
MONEY_FMT = '"S/" #,##0.00'

_BORDER_ROW = Border(bottom=Side(style="thin", color="E5E7EB"), top=Side(style="thin", color="E5E7EB"))

def _num(v) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0

def build_compras_historico_workbook(
    rows: list[dict],
    start_date: str,
    end_date: str,
    dias_rango: int,
    dias_cobertura: int,
    sucursal: str | None,
    brand_name: str
) -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("Compras Histórico")
    
    # --- Title Banner ---
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=10)
    c1 = ws.cell(row=1, column=1, value=f"{brand_name} - Análisis de Compras Histórico")
    c1.font = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
    c1.fill = PatternFill("solid", fgColor="1F2937")
    c1.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 25
    
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=10)
    suc = sucursal if sucursal else "Todas las sucursales"
    c2 = ws.cell(row=2, column=1, value=f"Ventas del {start_date} al {end_date} ({dias_rango} días) | {suc}")
    c2.font = Font(name="Calibri", size=12, bold=False, color="FFFFFF")
    c2.fill = PatternFill("solid", fgColor="3B82F6") # blue-500
    c2.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 25
    ws.row_dimensions[3].height = 10

    # --- Headers ---
    headers = [
        "Nivel / SKU",
        "Nombre",
        "Ventas (S/)",
        "Unidades Vendidas",
        "Ritmo Venta (und/día)",
        "Stock Actual",
        "Estado",
        f"Sugerencia Compra ({dias_cobertura} días)",
        "Categoría",
        "Subcategoría"
    ]
    
    start_row = 4
    for c_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=start_row, column=c_idx, value=h)
        cell.font = Font(name="Calibri", size=10, bold=True, color="1F2937")
        cell.fill = PatternFill("solid", fgColor="F3F4F6")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = Border(bottom=Side(style="thin", color="D1D5DB"))
    ws.row_dimensions[start_row].height = 30
    
    # --- Group Data ---
    # dept -> cat -> subcat -> list of items
    dept_groups = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for item in rows:
        dept = item.get("departamento", "(Sin Departamento)") or "(Sin Departamento)"
        cat = item.get("categoria", "(Sin Categoría)") or "(Sin Categoría)"
        sub = item.get("subcategoria", "(Sin Subcategoría)") or "(Sin Subcategoría)"
        dept_groups[dept][cat][sub].append(item)
    
    r = start_row + 1
    
    for dept_name, cat_groups in sorted(dept_groups.items()):
        # Draw Dept row
        dept_fill = PatternFill("solid", fgColor=C_DEPT_BG)
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=r, column=col_idx)
            cell.fill = dept_fill
            cell.border = _BORDER_ROW
        
        c = ws.cell(row=r, column=1, value="🏢 Departamento")
        c.font = Font(name="Calibri", size=11, bold=True, color=C_FG)
        c.alignment = Alignment(horizontal="left", vertical="center")
        
        c = ws.cell(row=r, column=2, value=dept_name)
        c.font = Font(name="Calibri", size=11, bold=True, color=C_FG)
        c.alignment = Alignment(horizontal="left", vertical="center")
        
        # Calculate Dept Totals
        dept_ventas = dept_unds = dept_ritmo = dept_stock = dept_compra = 0.0
        for cat_name, sub_groups in cat_groups.items():
            for sub_name, prods in sub_groups.items():
                for p in prods:
                    dept_ventas += _num(p.get("total_ventas"))
                    dept_unds += _num(p.get("unidades_vendidas"))
                    dept_ritmo += _num(p.get("ritmo_venta"))
                    dept_stock += _num(p.get("stock_actual"))
                    dept_compra += max(0, _num(p.get("sugerencia_compra")))
        
        totals = [dept_ventas, dept_unds, dept_ritmo, dept_stock, None, dept_compra]
        for idx, val in enumerate(totals):
            if val is not None:
                cell = ws.cell(row=r, column=3 + idx, value=val)
                cell.font = Font(name="Calibri", size=11, bold=True, color=C_FG)
                if idx == 0:
                    cell.number_format = MONEY_FMT
                elif idx == 2:
                    cell.number_format = DEC1_FMT
                else:
                    cell.number_format = INT_FMT
                cell.alignment = Alignment(horizontal="right", vertical="center")
                
        ws.row_dimensions[r].height = 22
        ws.row_dimensions[r].outline_level = 1
        r += 1
        
        for cat_name, sub_groups in sorted(cat_groups.items()):
            # Draw Cat row
            cat_fill = PatternFill("solid", fgColor=C_CAT_BG)
            for col_idx in range(1, len(headers) + 1):
                cell = ws.cell(row=r, column=col_idx)
                cell.fill = cat_fill
                cell.border = _BORDER_ROW
                
            c = ws.cell(row=r, column=1, value="📂 Categoría")
            c.font = Font(name="Calibri", size=10, bold=True, color=C_FG)
            c.alignment = Alignment(horizontal="left", vertical="center")
            
            c = ws.cell(row=r, column=2, value=cat_name)
            c.font = Font(name="Calibri", size=10, bold=True, color=C_FG)
            c.alignment = Alignment(horizontal="left", vertical="center")
            
            cat_ventas = cat_unds = cat_ritmo = cat_stock = cat_compra = 0.0
            for sub_name, prods in sub_groups.items():
                for p in prods:
                    cat_ventas += _num(p.get("total_ventas"))
                    cat_unds += _num(p.get("unidades_vendidas"))
                    cat_ritmo += _num(p.get("ritmo_venta"))
                    cat_stock += _num(p.get("stock_actual"))
                    cat_compra += max(0, _num(p.get("sugerencia_compra")))
                    
            totals = [cat_ventas, cat_unds, cat_ritmo, cat_stock, None, cat_compra]
            for idx, val in enumerate(totals):
                if val is not None:
                    cell = ws.cell(row=r, column=3 + idx, value=val)
                    cell.font = Font(name="Calibri", size=10, bold=True, color=C_FG)
                    if idx == 0:
                        cell.number_format = MONEY_FMT
                    elif idx == 2:
                        cell.number_format = DEC1_FMT
                    else:
                        cell.number_format = INT_FMT
                    cell.alignment = Alignment(horizontal="right", vertical="center")
            
            ws.row_dimensions[r].height = 20
            ws.row_dimensions[r].outline_level = 2
            r += 1
            
            for sub_name, prods in sorted(sub_groups.items()):
                # Draw Subcat row
                subcat_fill = PatternFill("solid", fgColor=C_SUBCAT_BG)
                for col_idx in range(1, len(headers) + 1):
                    cell = ws.cell(row=r, column=col_idx)
                    cell.fill = subcat_fill
                    cell.border = _BORDER_ROW
                    
                c = ws.cell(row=r, column=1, value="📁 Subcategoría")
                c.font = Font(name="Calibri", size=10, bold=True, color=C_FG)
                c.alignment = Alignment(horizontal="left", vertical="center")
                
                c = ws.cell(row=r, column=2, value=sub_name)
                c.font = Font(name="Calibri", size=10, bold=True, color=C_FG)
                c.alignment = Alignment(horizontal="left", vertical="center")
                
                sub_ventas = sub_unds = sub_ritmo = sub_stock = sub_compra = 0.0
                for p in prods:
                    sub_ventas += _num(p.get("total_ventas"))
                    sub_unds += _num(p.get("unidades_vendidas"))
                    sub_ritmo += _num(p.get("ritmo_venta"))
                    sub_stock += _num(p.get("stock_actual"))
                    sub_compra += max(0, _num(p.get("sugerencia_compra")))
                    
                totals = [sub_ventas, sub_unds, sub_ritmo, sub_stock, None, sub_compra]
                for idx, val in enumerate(totals):
                    if val is not None:
                        cell = ws.cell(row=r, column=3 + idx, value=val)
                        cell.font = Font(name="Calibri", size=10, bold=True, color=C_FG)
                        if idx == 0:
                            cell.number_format = MONEY_FMT
                        elif idx == 2:
                            cell.number_format = DEC1_FMT
                        else:
                            cell.number_format = INT_FMT
                        cell.alignment = Alignment(horizontal="right", vertical="center")
                
                ws.row_dimensions[r].height = 18
                ws.row_dimensions[r].outline_level = 3
                r += 1
                
                for i, p in enumerate(prods):
                    # Item Row
                    vals = [
                        p.get("sku"),
                        p.get("producto"),
                        _num(p.get("total_ventas")),
                        _num(p.get("unidades_vendidas")),
                        _num(p.get("ritmo_venta")),
                        _num(p.get("stock_actual")),
                        p.get("estado_stock"),
                        _num(p.get("sugerencia_compra")),
                        cat_name,
                        sub_name
                    ]
                    
                    for c_idx, val in enumerate(vals, 1):
                        cell = ws.cell(row=r, column=c_idx, value=val)
                        cell.font = Font(name="Calibri", size=10, color="1F2937")
                        if c_idx == 1:
                            cell.font = Font(name="Consolas", size=9, color="4B5563")
                        if c_idx == 7: # Estado
                            if val == "🔴 Agotado":
                                cell.font = Font(name="Calibri", size=10, color="DC2626", bold=True)
                            elif val == "🟢 Con Stock":
                                cell.font = Font(name="Calibri", size=10, color="059669", bold=True)
                        if c_idx == 8: # Sugerencia Compra
                            cell.font = Font(name="Calibri", size=10, bold=True, color="1F2937")
                            if val > 0:
                                cell.fill = FILL_VERDE
                        
                        cell.border = _BORDER_ROW
                        if i % 2 == 1 and cell.fill.fill_type is None:
                            cell.fill = PatternFill("solid", fgColor="FAFAFA")
                            
                        # Formatting
                        if c_idx == 3:
                            cell.number_format = MONEY_FMT
                            cell.alignment = Alignment(horizontal="right", vertical="center")
                        elif c_idx == 5:
                            cell.number_format = DEC1_FMT
                            cell.alignment = Alignment(horizontal="right", vertical="center")
                        elif c_idx in (4, 6, 8):
                            cell.number_format = INT_FMT
                            cell.alignment = Alignment(horizontal="right", vertical="center")
                        else:
                            cell.alignment = Alignment(horizontal="left", vertical="center")
                    
                    ws.row_dimensions[r].outline_level = 4
                    r += 1

    # Adjust widths
    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 40
    for c in ["C", "D", "E", "F", "G", "H"]:
        ws.column_dimensions[c].width = 15
    ws.column_dimensions["I"].width = 20
    ws.column_dimensions["J"].width = 20
    
    if not dept_groups:
        ws.cell(row=start_row + 1, column=1, value="No hay datos para las fechas seleccionadas.")
        
    return wb
