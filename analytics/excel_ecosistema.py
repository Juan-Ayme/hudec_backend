from __future__ import annotations

from typing import Any
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from analytics.excel_builder import HEADER_ALIGN, HEADER_FILL, HEADER_FONT, BORDER_THIN


# Colores por veredicto, en concordancia con el frontend
VEREDICTO_COLORS: dict[str, tuple[str, str]] = {
    "RIESGO_QUIEBRE": ("FFB3B3", "8B0000"),  # Rojo claro, texto rojo oscuro
    "SOBRECOMPRA":    ("FFE6B3", "8B5A00"),  # Amarillo, texto marron
    "EXCESO_STOCK":   ("B3D9FF", "003366"),  # Azul claro, texto azul oscuro
    "SIN_REPOSICION": ("E0BBE4", "4B0082"),  # Violeta, texto indigo
    "SIN_MOVIMIENTO": ("D3D3D3", "595959"),  # Gris, texto gris oscuro
    "EQUILIBRADO":    ("D4F1D4", "006400"),  # Verde claro, texto verde
}


def build_ecosistema_workbook(
    items: list[dict[str, Any]],
    meta: dict[str, Any],
    kpis: dict[str, Any],
    sucursal_nombre: str | None = None,
) -> Workbook:
    """Construye un Excel con dos hojas: Resumen y Ecosistema (tabla completa)."""
    wb = Workbook()
    
    # --------------------------------------------------------------------------
    # 1. Hoja de Resumen
    # --------------------------------------------------------------------------
    ws_resumen = wb.active
    assert ws_resumen is not None
    ws_resumen.title = "Resumen"
    
    # Titulo y metadata
    ws_resumen.column_dimensions["A"].width = 25
    ws_resumen.column_dimensions["B"].width = 35
    
    _write_pair(ws_resumen, 1, "Reporte", "Ecosistema de Compras", bold_val=True)
    _write_pair(ws_resumen, 2, "Generado", meta.get("generado_at", ""))
    _write_pair(ws_resumen, 3, "Nivel", meta.get("nivel", "").capitalize())
    _write_pair(ws_resumen, 4, "Ventana (días)", meta.get("dias", ""))
    _write_pair(ws_resumen, 5, "Sucursal", sucursal_nombre or "Todas las sucursales")
    _write_pair(ws_resumen, 6, "Nota", meta.get("nota", ""))
    
    # KPIs
    _write_pair(ws_resumen, 8, "Venta Total", kpis.get("venta_total_pen", 0), is_currency=True)
    _write_pair(ws_resumen, 9, "Compra Total", kpis.get("compra_total_pen", 0), is_currency=True)
    _write_pair(ws_resumen, 10, "Stock Total", kpis.get("stock_total_pen", 0), is_currency=True)
    
    mult_glob = kpis.get("multiplicador_global")
    _write_pair(ws_resumen, 11, "Multiplicador Global", mult_glob if mult_glob is not None else "—")
    _write_pair(ws_resumen, 12, "Total Filas", kpis.get("filas", 0))
    
    # Distribucion por Veredicto
    ws_resumen.cell(row=14, column=1, value="Conteo por Veredicto").font = Font(bold=True)
    row_idx = 15
    for veredicto, count in kpis.get("por_veredicto", {}).items():
        c_label = ws_resumen.cell(row=row_idx, column=1, value=veredicto)
        c_val = ws_resumen.cell(row=row_idx, column=2, value=count)
        
        # Aplicar color al label si existe
        color_info = VEREDICTO_COLORS.get(veredicto)
        if color_info:
            c_label.fill = PatternFill("solid", fgColor=color_info[0])
            c_label.font = Font(color=color_info[1], bold=True)
            
        row_idx += 1
        
    # --------------------------------------------------------------------------
    # 2. Hoja Ecosistema
    # --------------------------------------------------------------------------
    ws_data = wb.create_sheet(title="Ecosistema")
    nivel = meta.get("nivel", "categoria")
    
    # Definir columnas base
    cols_def = [
        ("Departamento", "department", 20, None),
    ]
    if nivel in ("categoria", "subcategoria"):
        cols_def.append(("Categoría", "category", 25, None))
    if nivel == "subcategoria":
        cols_def.append(("Subcategoría", "subcategory", 25, None))
        
    # Agregar metricas
    cols_def.extend([
        ("Venta S/", "venta_pen", 15, "#,##0.00"),
        ("Compra S/", "compra_pen", 15, "#,##0.00"),
        ("Stock S/", "stock_pen", 15, "#,##0.00"),
        ("Uds Vendidas", "unds_vendidas", 12, "#,##0"),
        ("Uds Compradas", "unds_compradas", 12, "#,##0"),
        ("Uds Stock", "unds_stock", 12, "#,##0"),
        ("Mult.", "multiplicador", 10, "0.00"),
        ("Sellthrough %", "sellthrough_pct", 15, "0.00"),
        ("Cobertura (m)", "meses_cobertura", 15, "0.00"),
        ("Veredicto", "veredicto", 20, None),
    ])
    
    # Headers
    for c_idx, (header_label, _, width, _) in enumerate(cols_def, start=1):
        cell = ws_data.cell(row=1, column=c_idx, value=header_label)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN
        cell.border = BORDER_THIN
        ws_data.column_dimensions[get_column_letter(c_idx)].width = width
        
    # Datos
    for r_idx, item in enumerate(items, start=2):
        for c_idx, (_, key, _, num_format) in enumerate(cols_def, start=1):
            val = item.get(key)
            # Manejar nulos o Sin clasificar
            if val is None and key in ("department", "category", "subcategory"):
                val = "Sin clasificar"
                
            cell = ws_data.cell(row=r_idx, column=c_idx, value=val)
            cell.border = BORDER_THIN
            
            if num_format and isinstance(val, (int, float)):
                cell.number_format = num_format
                
            # Colorear Veredicto
            if key == "veredicto" and val:
                color_info = VEREDICTO_COLORS.get(str(val))
                if color_info:
                    cell.fill = PatternFill("solid", fgColor=color_info[0])
                    cell.font = Font(color=color_info[1], bold=True)
                    cell.alignment = Alignment(horizontal="center")

    # Totales
    total_row = len(items) + 2
    for c_idx, (header_label, key, _, num_format) in enumerate(cols_def, start=1):
        cell = ws_data.cell(row=total_row, column=c_idx)
        cell.font = Font(bold=True)
        cell.border = BORDER_THIN
        
        if c_idx == 1:
            cell.value = "TOTALES"
            continue
            
        if key == "venta_pen": cell.value = kpis.get("venta_total_pen")
        elif key == "compra_pen": cell.value = kpis.get("compra_total_pen")
        elif key == "stock_pen": cell.value = kpis.get("stock_total_pen")
        elif key == "unds_vendidas": cell.value = sum(i.get("unds_vendidas", 0) for i in items)
        elif key == "unds_compradas": cell.value = sum(i.get("unds_compradas", 0) for i in items)
        elif key == "unds_stock": cell.value = sum(i.get("unds_stock", 0) for i in items)
        elif key == "multiplicador": cell.value = kpis.get("multiplicador_global")
        elif key == "sellthrough_pct":
            uc = sum(i.get("unds_compradas", 0) for i in items)
            uv = sum(i.get("unds_vendidas", 0) for i in items)
            cell.value = round(100.0 * uv / uc, 1) if uc > 0 else None
        
        if num_format and isinstance(cell.value, (int, float)):
            cell.number_format = num_format

    # AutoFilter
    if items:
        ws_data.auto_filter.ref = f"A1:{get_column_letter(len(cols_def))}{len(items) + 1}"
    
    return wb

def _write_pair(ws, row: int, label: str, val: Any, is_currency: bool = False, bold_val: bool = False):
    c_label = ws.cell(row=row, column=1, value=label)
    c_label.font = Font(bold=True, color="595959")
    
    c_val = ws.cell(row=row, column=2, value=val)
    if is_currency and isinstance(val, (int, float)):
        c_val.number_format = '"S/" #,##0.00'
    if bold_val:
        c_val.font = Font(bold=True)
