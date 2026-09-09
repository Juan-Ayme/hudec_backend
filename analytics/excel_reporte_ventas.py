"""Builder Excel del **Reporte de Ventas** por departamento.

Layout del workbook:

    1) Portada    — cabecera del informe (tienda / periodo / sucursal) + KPIs
                    globales del rango.
    2) Resumen    — una fila por departamento + gráficos globales
                    (barras, torta, y la evolución diaria de la venta).
    3..N) Una hoja POR DEPARTAMENTO, cada una con:
            · tabla Categoría → Productos (filas de producto colapsables
              con los botones +/- de Excel),
            · el bloque horizontal "Desglose de los ingresos por categoría",
            · 3 gráficos por categoría (barras / torta / línea).

Los gráficos leen de una tabla-auxiliar vertical escrita en columnas ocultas
a la derecha: openpyxl necesita un rango contiguo para `Reference`, y el
bloque de desglose visible es horizontal. Ese bloque auxiliar no se toca a
mano — se regenera en cada descarga.

Consume el dict que arma `app.routers.reporte_ventas._build_data`.
Reutiliza los estilos compartidos de `analytics.excel_builder`.
"""

from __future__ import annotations

from typing import Any, Sequence

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from analytics.excel_builder import (
    BORDER_THIN,
    HEADER_ALIGN,
    HEADER_FILL,
    HEADER_FONT,
    ancho_columna,
)

# ── Formatos de celda ──────────────────────────────────────────────────────
MONEY_FMT = '"S/ "#,##0.00'
INT_FMT = "#,##0"
PCT_FMT = '0.0"%"'

# ── Paleta ────────────────────────────────────────────────────────────────
C_DARK = "1F2937"      # banner principal
C_ACCENT = "2E75B6"    # sub-banner
C_CAT_FILL = "1F4E78"  # fila de categoría
C_TOTAL_FILL = "203864"

TITLE_FONT = Font(size=16, bold=True, color="FFFFFF")
SUBTITLE_FONT = Font(size=11, bold=False, color="FFFFFF")
CAT_ROW_FONT = Font(size=11, bold=True, color="FFFFFF")
TOTAL_ROW_FONT = Font(size=11, bold=True, color="FFFFFF")
LABEL_FONT = Font(bold=True, color="1F2937")
NOTE_FONT = Font(italic=True, size=9, color="808080")

FILL_ZEBRA = PatternFill("solid", fgColor="FAFAFA")
FILL_CAMPO = PatternFill("solid", fgColor="E2EFDA")  # verde claro (celdas de la plantilla)

# Colores del bloque "Desglose de ingresos por categoría" — mismo criterio
# visual que la plantilla: una franja de color por categoría, reutilizada
# como color de serie en los gráficos.
CAT_PALETTE = [
    "1F3864", "375623", "808080", "BF8F00",
    "2E75B6", "70AD47", "C55A11", "3B3838",
    "7030A0", "C00000",
]

# Colores rotativos de las pestañas por departamento.
TAB_COLORS = [
    "1F4E78", "2E75B6", "548235", "BF8F00", "C00000",
    "7030A0", "385723", "C65911", "203864", "843C0C",
]

# Columnas de la tabla de detalle (hoja de departamento).
#
# Orden pensado para que la cuenta se lea de izquierda a derecha sin saltos:
# Ingresos − Costo = Ganancia. El IGV va después, como dato informativo: acá
# se queda en la empresa, así que NO se resta del ingreso (ver la nota de
# metodología en la Portada y el docstring de app/routers/reporte_ventas.py).
DEPT_COLS = [
    "Categoría / Producto",
    "SKU",
    "Unds vendidas",
    "Ingresos (S/)",
    "Costo (S/)",
    "Ganancia (S/)",
    "Margen %",
    "Markup %",
    "IGV incluido (S/)",
    "% del depto",
]
# Índice 0-based por tipo de formato.
_DEPT_INT_COLS = {2}
_DEPT_MONEY_COLS = {3, 4, 5, 8}
_DEPT_PCT_COLS = {6, 7, 9}

RESUMEN_COLS = [
    "Departamento",
    "Unds vendidas",
    "Ingresos (S/)",
    "Costo (S/)",
    "Ganancia (S/)",
    "Margen %",
    "Markup %",
    "IGV incluido (S/)",
    "% del total",
    "Tickets",
    "Ticket prom. (S/)",
]
_RES_INT_COLS = {1, 9}
_RES_MONEY_COLS = {2, 3, 4, 7, 10}
_RES_PCT_COLS = {5, 6, 8}

_INVALID_SHEET_CHARS = set('[]:*?/\\')


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _num(v: Any) -> float:
    """A float para escribir en la celda. Decimal/str/None → float."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _sheet_name(base: str, idx: int, used: set[str]) -> str:
    """Nombre de hoja: `NN Departamento`, ≤31 chars, sin caracteres prohibidos.

    El prefijo numérico conserva el orden del ranking (los departamentos
    llegan ordenados por ingresos) aunque Excel muestre las pestañas por
    posición, y evita colisiones entre nombres largos que se truncan igual.
    """
    prefix = f"{idx:02d} "
    cleaned = "".join(c for c in (base or "Sin nombre") if c not in _INVALID_SHEET_CHARS).strip()
    candidate = (prefix + cleaned)[:31].strip() or f"Hoja {idx}"
    if candidate not in used:
        used.add(candidate)
        return candidate
    for i in range(2, 100):
        suffix = f" ({i})"
        alt = candidate[: 31 - len(suffix)] + suffix
        if alt not in used:
            used.add(alt)
            return alt
    raise ValueError(f"No se pudo generar nombre único para '{base}'")


def _banner(ws: Worksheet, ncols: int, titulo: str, subtitulo: str) -> None:
    """Banner de 2 filas (título oscuro + subtítulo azul) al tope de la hoja."""
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
    c = ws.cell(row=1, column=1, value=titulo)
    c.font = TITLE_FONT
    c.fill = PatternFill("solid", fgColor=C_DARK)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncols)
    c = ws.cell(row=2, column=1, value=subtitulo)
    c.font = SUBTITLE_FONT
    c.fill = PatternFill("solid", fgColor=C_ACCENT)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 22


def _write_header_row(ws: Worksheet, row: int, cols: Sequence[str]) -> None:
    for c_idx, h in enumerate(cols, 1):
        cell = ws.cell(row=row, column=c_idx, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN
        cell.border = BORDER_THIN
    ws.row_dimensions[row].height = 30


def _fmt_cell(cell, col0: int, int_cols: set[int], money_cols: set[int], pct_cols: set[int]) -> None:
    if col0 in money_cols:
        cell.number_format = MONEY_FMT
        cell.alignment = Alignment(horizontal="right", vertical="center")
    elif col0 in pct_cols:
        cell.number_format = PCT_FMT
        cell.alignment = Alignment(horizontal="right", vertical="center")
    elif col0 in int_cols:
        cell.number_format = INT_FMT
        cell.alignment = Alignment(horizontal="right", vertical="center")
    else:
        cell.alignment = Alignment(horizontal="left", vertical="center")


def _autosize(ws: Worksheet, cols: Sequence[str], rows: list[Sequence[Any]]) -> None:
    for c_idx, h in enumerate(cols, 1):
        values = [r[c_idx - 1] for r in rows[:200] if len(r) >= c_idx]
        ws.column_dimensions[get_column_letter(c_idx)].width = ancho_columna(values, h)


def _helper_table(
    ws: Worksheet,
    *,
    col: int,
    labels: list[str],
    values: list[float],
    header: str,
) -> tuple[int, int]:
    """Escribe una tabla auxiliar vertical (label, valor) en columnas ocultas.

    openpyxl arma los gráficos con `Reference` sobre un rango contiguo de la
    hoja; el bloque de desglose visible es horizontal, así que los gráficos
    leen de acá. Devuelve (primera_fila_datos, ultima_fila_datos).
    """
    ws.cell(row=1, column=col, value="Categoría")
    ws.cell(row=1, column=col + 1, value=header)
    r = 2
    for label, val in zip(labels, values):
        ws.cell(row=r, column=col, value=label)
        ws.cell(row=r, column=col + 1, value=val)
        r += 1
    for c in (col, col + 1):
        ws.column_dimensions[get_column_letter(c)].hidden = True
    return 2, r - 1


# ──────────────────────────────────────────────────────────────────────────
# Hoja: Portada
# ──────────────────────────────────────────────────────────────────────────


def _hoja_portada(wb: Workbook, data: dict) -> None:
    meta = data["meta"]
    tot = data["totales"]

    ws = wb.create_sheet("Portada")
    ws.sheet_properties.tabColor = C_DARK
    ws.sheet_view.showGridLines = False

    _banner(
        ws,
        4,
        "REPORTE DE VENTAS",
        f"{meta['empresa']} · {meta['desde']} al {meta['hasta']} ({meta['dias']} días)",
    )

    # Cabecera tipo plantilla: tienda / sucursal / periodo en celdas resaltadas.
    campos = [
        ("NOMBRE DE LA TIENDA", meta["empresa"]),
        ("SUCURSAL", meta["sucursal"]),
        ("INICIO Y FINALIZACIÓN", f"{meta['desde']} – {meta['hasta']}"),
        ("GENERADO", meta["generado_en"]),
    ]
    r = 4
    for label, val in campos:
        c = ws.cell(row=r, column=1, value=label)
        c.font = LABEL_FONT
        c = ws.cell(row=r, column=2, value=val)
        c.fill = FILL_CAMPO
        c.border = BORDER_THIN
        c.alignment = Alignment(horizontal="left", vertical="center")
        r += 1

    # KPIs del rango.
    r += 1
    c = ws.cell(row=r, column=1, value="INDICADORES DEL PERIODO")
    c.font = Font(size=12, bold=True, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor=C_CAT_FILL)
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[r].height = 22
    r += 1

    # `destacado` = fila del puente principal (ingresos → costo → ganancia).
    # Las demás son contexto y van en gris para que no compitan.
    kpis: list[tuple[str, float, str, bool]] = [
        ("Ingresos (con IGV)", _num(tot["ingresos"]), MONEY_FMT, True),
        ("Costo de lo vendido", -_num(tot["costo"]), MONEY_FMT, True),
        ("GANANCIA", _num(tot["ganancia"]), MONEY_FMT, True),
        ("Margen % (ganancia / ingresos)", _num(tot["margen_pct"]), PCT_FMT, True),
        ("Markup % (ganancia / costo)", _num(tot["markup_pct"]), PCT_FMT, False),
        ("IGV incluido en los ingresos", _num(tot["igv"]), MONEY_FMT, False),
        ("Venta neta según BSale (sin IGV)", _num(tot["ingresos_netos"]), MONEY_FMT, False),
        ("Unidades vendidas", _num(tot["unidades"]), INT_FMT, False),
        ("Tickets", _num(tot["tickets"]), INT_FMT, False),
        ("Ticket promedio", _num(tot["ticket_promedio"]), MONEY_FMT, False),
        ("Departamentos con venta", float(len(data["departamentos"])), INT_FMT, False),
    ]
    for label, val, fmt, destacado in kpis:
        c = ws.cell(row=r, column=1, value=label)
        c.font = Font(bold=True, size=11, color="1F2937") if destacado else Font(size=10, color="595959")
        c.border = BORDER_THIN
        c = ws.cell(row=r, column=2, value=val)
        c.number_format = fmt
        c.border = BORDER_THIN
        c.alignment = Alignment(horizontal="right", vertical="center")
        c.font = Font(bold=True, size=12) if destacado else Font(size=10, color="595959")
        if label == "GANANCIA":
            for col in (1, 2):
                ws.cell(row=r, column=col).fill = PatternFill("solid", fgColor="D9EAD3")
        r += 1

    r += 1
    ws.cell(
        row=r,
        column=1,
        value=(
            "Metodología: se excluyen las notas de crédito y las líneas de regalo. "
            "El costo es cantidad × costo efectivo de la variante en la sucursal de "
            "la venta (con fallback al costo global). "
            "GANANCIA = ingresos CON IGV − costo: el IGV cobrado se queda en la "
            "empresa, así que cuenta como ingreso, y los proveedores no facturan "
            "IGV, así que el costo registrado es el que realmente se paga. Por eso "
            "este margen (≈34%) es más alto que el margen contable clásico sobre "
            "venta neta (≈23%) que usan las vistas de Rentabilidad y Diagnóstico. "
            "Los tickets se cuentan a nivel documento: un mismo ticket con productos "
            "de dos departamentos cuenta en ambos, por eso la columna Tickets del "
            "Resumen suma más que el total."
        ),
    ).font = NOTE_FONT
    ws.merge_cells(start_row=r, start_column=1, end_row=r + 2, end_column=4)
    ws.cell(row=r, column=1).alignment = Alignment(vertical="top", wrap_text=True)

    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 30
    ws.column_dimensions["C"].width = 18
    ws.column_dimensions["D"].width = 18


# ──────────────────────────────────────────────────────────────────────────
# Hoja: Resumen
# ──────────────────────────────────────────────────────────────────────────


def _hoja_resumen(wb: Workbook, data: dict) -> None:
    meta = data["meta"]
    tot = data["totales"]
    deps = data["departamentos"]

    ws = wb.create_sheet("Resumen")
    ws.sheet_properties.tabColor = C_ACCENT

    _banner(
        ws,
        len(RESUMEN_COLS),
        "RESUMEN POR DEPARTAMENTO",
        f"{meta['empresa']} · {meta['sucursal']} · {meta['desde']} al {meta['hasta']}",
    )

    head_row = 4
    _write_header_row(ws, head_row, RESUMEN_COLS)

    rows: list[Sequence[Any]] = []
    for d in deps:
        rows.append([
            d["departamento"],
            _num(d["unidades"]),
            _num(d["ingresos"]),
            _num(d["costo"]),
            _num(d["ganancia"]),
            _num(d["margen_pct"]),
            _num(d["markup_pct"]),
            _num(d["igv"]),
            _num(d["participacion_pct"]),
            _num(d["tickets"]),
            _num(d["ticket_promedio"]),
        ])

    r = head_row + 1
    first_data = r
    for i, row_vals in enumerate(rows):
        for c_idx, val in enumerate(row_vals, 1):
            cell = ws.cell(row=r, column=c_idx, value=val)
            cell.border = BORDER_THIN
            _fmt_cell(cell, c_idx - 1, _RES_INT_COLS, _RES_MONEY_COLS, _RES_PCT_COLS)
            if i % 2 == 1:
                cell.fill = FILL_ZEBRA
        r += 1
    last_data = r - 1

    # Fila TOTAL.
    total_vals = [
        "TOTAL",
        _num(tot["unidades"]),
        _num(tot["ingresos"]),
        _num(tot["costo"]),
        _num(tot["ganancia"]),
        _num(tot["margen_pct"]),
        _num(tot["markup_pct"]),
        _num(tot["igv"]),
        100.0 if deps else 0.0,
        _num(tot["tickets"]),
        _num(tot["ticket_promedio"]),
    ]
    for c_idx, val in enumerate(total_vals, 1):
        cell = ws.cell(row=r, column=c_idx, value=val)
        cell.font = TOTAL_ROW_FONT
        cell.fill = PatternFill("solid", fgColor=C_TOTAL_FILL)
        cell.border = BORDER_THIN
        _fmt_cell(cell, c_idx - 1, _RES_INT_COLS, _RES_MONEY_COLS, _RES_PCT_COLS)
    total_row = r

    _autosize(ws, RESUMEN_COLS, rows)
    ws.freeze_panes = f"A{head_row + 1}"
    if last_data >= first_data:
        ws.auto_filter.ref = (
            f"A{head_row}:{get_column_letter(len(RESUMEN_COLS))}{last_data}"
        )

    # ── Gráficos ──
    chart_row = total_row + 2
    if last_data >= first_data:
        # Barras: ingresos por departamento.
        bar = BarChart()
        bar.type = "col"
        bar.title = "Ingresos por departamento (S/)"
        bar.y_axis.title = "Ingresos (S/)"
        bar.height, bar.width = 10, 24
        bar.legend = None
        bar.add_data(
            Reference(ws, min_col=3, min_row=head_row, max_row=last_data),
            titles_from_data=True,
        )
        bar.set_categories(Reference(ws, min_col=1, min_row=first_data, max_row=last_data))
        ws.add_chart(bar, f"A{chart_row}")

        # Torta: participación de cada departamento en los ingresos.
        pie = PieChart()
        pie.title = "Desglose de ingresos por departamento"
        pie.height, pie.width = 10, 14
        pie.add_data(
            Reference(ws, min_col=3, min_row=head_row, max_row=last_data),
            titles_from_data=True,
        )
        pie.set_categories(Reference(ws, min_col=1, min_row=first_data, max_row=last_data))
        pie.dataLabels = DataLabelList()
        pie.dataLabels.showPercent = True
        ws.add_chart(pie, f"N{chart_row}")

    # ── Evolución diaria (tabla auxiliar + línea) ──
    serie = data.get("serie_diaria") or []
    if serie:
        s_head = chart_row + 21
        ws.cell(row=s_head, column=1, value="EVOLUCIÓN DIARIA DE LA VENTA").font = Font(
            size=12, bold=True, color="1F4E78"
        )
        s_head += 1
        _write_header_row(ws, s_head, ["Día", "Ingresos (S/)", "Tickets"])
        sr = s_head + 1
        for p in serie:
            ws.cell(row=sr, column=1, value=p["fecha"]).border = BORDER_THIN
            c = ws.cell(row=sr, column=2, value=_num(p["ingresos"]))
            c.number_format = MONEY_FMT
            c.border = BORDER_THIN
            c = ws.cell(row=sr, column=3, value=_num(p["tickets"]))
            c.number_format = INT_FMT
            c.border = BORDER_THIN
            sr += 1
        s_last = sr - 1

        line = LineChart()
        line.title = "Ingresos por día (S/)"
        line.y_axis.title = "Ingresos (S/)"
        line.x_axis.title = "Fecha"
        line.height, line.width = 10, 24
        line.add_data(
            Reference(ws, min_col=2, min_row=s_head, max_row=s_last),
            titles_from_data=True,
        )
        line.set_categories(Reference(ws, min_col=1, min_row=s_head + 1, max_row=s_last))
        ws.add_chart(line, f"E{s_head}")


# ──────────────────────────────────────────────────────────────────────────
# Hoja: un departamento
# ──────────────────────────────────────────────────────────────────────────


def _hoja_departamento(wb: Workbook, dep: dict, meta: dict, idx: int, used: set[str]) -> None:
    nombre = dep["departamento"]
    ws = wb.create_sheet(_sheet_name(nombre, idx, used))
    ws.sheet_properties.tabColor = TAB_COLORS[(idx - 1) % len(TAB_COLORS)]
    # Las filas de producto van DEBAJO de su categoría, así que el botón +/-
    # tiene que dibujarse en la fila de arriba (la categoría), no abajo.
    ws.sheet_properties.outlinePr.summaryBelow = False

    ncols = len(DEPT_COLS)
    _banner(
        ws,
        ncols,
        f"DEPARTAMENTO: {nombre.upper()}",
        f"{meta['empresa']} · {meta['sucursal']} · {meta['desde']} al {meta['hasta']} "
        f"({meta['dias']} días)",
    )

    head_row = 4
    _write_header_row(ws, head_row, DEPT_COLS)

    cats = dep.get("categorias") or []
    r = head_row + 1
    autosize_sample: list[Sequence[Any]] = []

    for cat in cats:
        # ── Fila de categoría ──
        cat_vals = [
            cat["categoria"],
            "",
            _num(cat["unidades"]),
            _num(cat["ingresos"]),
            _num(cat["costo"]),
            _num(cat["ganancia"]),
            _num(cat["margen_pct"]),
            _num(cat["markup_pct"]),
            _num(cat["igv"]),
            _num(cat["participacion_pct"]),
        ]
        for c_idx, val in enumerate(cat_vals, 1):
            cell = ws.cell(row=r, column=c_idx, value=val)
            cell.font = CAT_ROW_FONT
            cell.fill = PatternFill("solid", fgColor=C_CAT_FILL)
            cell.border = BORDER_THIN
            _fmt_cell(cell, c_idx - 1, _DEPT_INT_COLS, _DEPT_MONEY_COLS, _DEPT_PCT_COLS)
        ws.row_dimensions[r].height = 20
        autosize_sample.append(cat_vals)
        r += 1

        # ── Filas de producto (colapsables bajo la categoría) ──
        for i, p in enumerate(cat.get("productos") or []):
            vals = [
                p["producto"],
                p["sku"],
                _num(p["unidades"]),
                _num(p["ingresos"]),
                _num(p["costo"]),
                _num(p["ganancia"]),
                _num(p["margen_pct"]),
                _num(p["markup_pct"]),
                _num(p["igv"]),
                _num(p["participacion_pct"]),
            ]
            for c_idx, val in enumerate(vals, 1):
                cell = ws.cell(row=r, column=c_idx, value=val)
                cell.border = BORDER_THIN
                cell.font = Font(size=10, color="1F2937")
                if c_idx == 2:
                    cell.font = Font(name="Consolas", size=9, color="4B5563")
                _fmt_cell(cell, c_idx - 1, _DEPT_INT_COLS, _DEPT_MONEY_COLS, _DEPT_PCT_COLS)
                if i % 2 == 1:
                    cell.fill = FILL_ZEBRA
            ws.row_dimensions[r].outline_level = 1
            autosize_sample.append(vals)
            r += 1

    # ── Fila TOTAL del departamento ──
    total_vals = [
        f"TOTAL {nombre}",
        "",
        _num(dep["unidades"]),
        _num(dep["ingresos"]),
        _num(dep["costo"]),
        _num(dep["ganancia"]),
        _num(dep["margen_pct"]),
        _num(dep["markup_pct"]),
        _num(dep["igv"]),
        100.0 if cats else 0.0,
    ]
    for c_idx, val in enumerate(total_vals, 1):
        cell = ws.cell(row=r, column=c_idx, value=val)
        cell.font = TOTAL_ROW_FONT
        cell.fill = PatternFill("solid", fgColor=C_TOTAL_FILL)
        cell.border = BORDER_THIN
        _fmt_cell(cell, c_idx - 1, _DEPT_INT_COLS, _DEPT_MONEY_COLS, _DEPT_PCT_COLS)
    ws.row_dimensions[r].height = 20
    total_row = r

    _autosize(ws, DEPT_COLS, autosize_sample)
    ws.column_dimensions["A"].width = 46
    ws.freeze_panes = f"A{head_row + 1}"

    if not cats:
        ws.cell(
            row=total_row + 2,
            column=1,
            value="Sin ventas de este departamento en el rango seleccionado.",
        ).font = NOTE_FONT
        return

    # ── Bloque horizontal: desglose de ingresos por categoría ──
    labels = [c["categoria"] for c in cats]
    ingresos = [_num(c["ingresos"]) for c in cats]
    pcts = [_num(c["participacion_pct"]) for c in cats]

    d_row = total_row + 2
    ws.merge_cells(start_row=d_row, start_column=1, end_row=d_row, end_column=len(labels) + 2)
    c = ws.cell(row=d_row, column=1, value=f'DESGLOSE DE LOS INGRESOS POR CATEGORÍA — {nombre}')
    c.font = Font(size=12, bold=True, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor=C_DARK)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[d_row].height = 24

    # Fila de encabezados: una franja de color por categoría (+ columna TODOS).
    hdr = d_row + 1
    ws.cell(row=hdr, column=1, value="").fill = PatternFill("solid", fgColor=C_DARK)
    for j, label in enumerate(labels):
        cell = ws.cell(row=hdr, column=2 + j, value=label)
        cell.font = Font(bold=True, color="FFFFFF", size=10)
        cell.fill = PatternFill("solid", fgColor=CAT_PALETTE[j % len(CAT_PALETTE)])
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER_THIN
    cell = ws.cell(row=hdr, column=2 + len(labels), value="TODOS")
    cell.font = Font(bold=True, color="1F2937", size=10)
    cell.fill = PatternFill("solid", fgColor="D9D9D9")
    cell.alignment = Alignment(horizontal="center", vertical="center")
    cell.border = BORDER_THIN
    ws.row_dimensions[hdr].height = 28

    # Filas de valores: INGRESOS TOTALES y PORCENTAJE.
    for k, (titulo, valores, fmt, total_val) in enumerate((
        ("INGRESOS TOTALES", ingresos, MONEY_FMT, _num(dep["ingresos"])),
        ("PORCENTAJE", pcts, PCT_FMT, 100.0),
    )):
        rr = hdr + 1 + k
        cell = ws.cell(row=rr, column=1, value=titulo)
        cell.font = Font(bold=True, color="FFFFFF", size=10)
        cell.fill = PatternFill("solid", fgColor="404040")
        cell.alignment = Alignment(horizontal="left", vertical="center")
        cell.border = BORDER_THIN
        for j, val in enumerate(valores):
            cell = ws.cell(row=rr, column=2 + j, value=val)
            cell.number_format = fmt
            cell.alignment = Alignment(horizontal="right", vertical="center")
            cell.border = BORDER_THIN
        cell = ws.cell(row=rr, column=2 + len(labels), value=total_val)
        cell.number_format = fmt
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9D9D9")
        cell.alignment = Alignment(horizontal="right", vertical="center")
        cell.border = BORDER_THIN

    # ── Gráficos por categoría (leen de la tabla auxiliar oculta) ──
    helper_col = ncols + 2
    h_first, h_last = _helper_table(
        ws, col=helper_col, labels=labels, values=ingresos, header="Ingresos (S/)"
    )
    val_ref = Reference(ws, min_col=helper_col + 1, min_row=1, max_row=h_last)
    cat_ref = Reference(ws, min_col=helper_col, min_row=h_first, max_row=h_last)

    g_row = hdr + 4

    bar = BarChart()
    bar.type = "col"
    bar.title = f"Ingresos por categoría — {nombre}"
    bar.y_axis.title = "Ingresos (S/)"
    bar.height, bar.width = 10, 22
    bar.legend = None
    bar.add_data(val_ref, titles_from_data=True)
    bar.set_categories(cat_ref)
    ws.add_chart(bar, f"A{g_row}")

    pie = PieChart()
    pie.title = "Desglose de los ingresos"
    pie.height, pie.width = 10, 13
    pie.add_data(val_ref, titles_from_data=True)
    pie.set_categories(cat_ref)
    pie.dataLabels = DataLabelList()
    pie.dataLabels.showPercent = True
    ws.add_chart(pie, f"A{g_row + 21}")

    line = LineChart()
    line.title = "Ingresos totales por categoría"
    line.y_axis.title = "Ingresos (S/)"
    line.height, line.width = 10, 13
    line.legend = None
    line.add_data(val_ref, titles_from_data=True)
    line.set_categories(cat_ref)
    ws.add_chart(line, f"H{g_row + 21}")

    # Los datos viven en columnas ocultas: sin esto Excel aplica "graficar solo
    # celdas visibles" y los tres gráficos salen en blanco.
    for chart in (bar, pie, line):
        chart.visible_cells_only = False


# ──────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────


def build_reporte_ventas_workbook(data: dict) -> Workbook:
    """Arma el workbook completo a partir del dict del endpoint."""
    wb = Workbook()
    wb.remove(wb.active)

    _hoja_portada(wb, data)
    _hoja_resumen(wb, data)

    used: set[str] = set()
    for i, dep in enumerate(data["departamentos"], 1):
        _hoja_departamento(wb, dep, data["meta"], i, used)

    return wb
