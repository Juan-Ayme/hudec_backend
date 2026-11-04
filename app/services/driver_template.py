"""Plantilla Excel del driver de compras con las columnas de atributos.

Mantiene las columnas que ya se usan (FECHA … ENVÍO) y agrega las de
atributos, con listas desplegables para que los valores lleguen limpios:
  - TIPO BSALE: "Categoría / Subcategoría" de la taxonomía vigente.
  - ORIGEN, PÚBLICO, ESTILO, TEMPORADA, MATERIAL: valores del catálogo.
Las listas viven en una hoja oculta "Listas" (Excel no acepta listas inline
de más de 255 caracteres).
"""

from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from app.services.attribute_catalog import DEFS_BY_CODE

COLUMNAS_ACTUALES = [
    "FECHA", "SKU", "CATEGORIA", "NOMBRE PRODUCTO", "PROVEDOR", "OBSERVACION",
    "CODIGO DE BARRA", "UNIDAD MEDIDA", "CANTIDAD", "CAJAS", "C/U", "COSTO TOTAL",
    "REAL STOCK", "COYA", "ASAMBLEA", "MAGDALENA", "OPINIÓN", "ALMACÉN", "ENVÍO",
]
# (encabezado, código de atributo o None, ayuda)
COLUMNAS_NUEVAS: list[tuple[str, str | None, str]] = [
    ("TIPO BSALE", None, "Categoría / Subcategoría tal como debe quedar en BSale"),
    ("MARCA", "marca", "Ej. Artesco, Samantha, Eficaz"),
    ("ORIGEN", "origen", "Nacional / Asiático / Importado"),
    ("PÚBLICO", "publico", "Para quién es"),
    ("LICENCIA", "licencia", "Personaje o licencia: Hello Kitty, Capibara, Marvel…"),
    ("ESTILO", "estilo", "Kawaii / Básico / Premium"),
    ("TEMPORADA", "temporada", "Cuándo se vende más"),
    ("MATERIAL", "material", "Solo hogar, cocina y accesorios"),
    ("CONTENIDO", "contenido", "Con unidad: 250 ml, 1 L, 90 g, 4 kg"),
    ("UNID X PACK", "unidades_pack", "Unidades que se venden juntas (set x12 → 12)"),
    ("UNID X CAJA", "unidades_caja", "Unidades por caja del proveedor"),
    ("COLOR", "color", "Va como atributo de la variante en BSale"),
    ("DISEÑO", "diseno", "Modelo o diseño de la variante"),
    ("TALLA", "talla", "Talla de la variante"),
    ("SABOR / AROMA", "sabor_aroma", "Sabor o aroma de la variante"),
]

_FILL_ACTUAL = PatternFill("solid", fgColor="D9D9D9")
_FILL_NUEVA = PatternFill("solid", fgColor="FCE4D6")


def build_driver_template(tipos_bsale: list[str]) -> bytes:
    """Devuelve el .xlsx como bytes. `tipos_bsale` = ["Cat / Sub", ...]."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Driver"
    listas = wb.create_sheet("Listas")
    listas.sheet_state = "hidden"

    headers = COLUMNAS_ACTUALES + [h for h, _, _ in COLUMNAS_NUEVAS]
    for i, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=i, value=h)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.fill = _FILL_ACTUAL if i <= len(COLUMNAS_ACTUALES) else _FILL_NUEVA
        ws.column_dimensions[get_column_letter(i)].width = max(12, min(len(h) + 4, 40))
    ws.column_dimensions[get_column_letter(headers.index("NOMBRE PRODUCTO") + 1)].width = 45
    ws.column_dimensions[get_column_letter(headers.index("TIPO BSALE") + 1)].width = 45
    ws.freeze_panes = "C2"

    # Fila 2: ayuda en gris (el importador la ignora: no tiene SKU).
    for h, _, help_text in COLUMNAS_NUEVAS:
        c = ws.cell(row=2, column=headers.index(h) + 1, value=help_text)
        c.font = Font(italic=True, color="808080", size=9)
        c.alignment = Alignment(wrap_text=True, vertical="top")

    lists: dict[str, list[str]] = {"TIPO BSALE": sorted(tipos_bsale)}
    for h, code, _ in COLUMNAS_NUEVAS:
        if code and DEFS_BY_CODE[code].data_type == "enum":
            lists[h] = DEFS_BY_CODE[code].allowed_values

    for col_idx, (h, values) in enumerate(lists.items(), start=1):
        letter = get_column_letter(col_idx)
        listas.cell(row=1, column=col_idx, value=h)
        for r, v in enumerate(values, start=2):
            listas.cell(row=r, column=col_idx, value=v)
        dv = DataValidation(
            type="list",
            formula1=f"=Listas!${letter}$2:${letter}${len(values) + 1}",
            allow_blank=True,
            showErrorMessage=True,
            errorTitle=h,
            error=f"Elegí un valor de la lista de {h}.",
        )
        target = get_column_letter(headers.index(h) + 1)
        dv.add(f"{target}3:{target}5000")
        ws.add_data_validation(dv)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def tipos_from_tree(tree: dict) -> list[str]:
    """["Categoría / Subcategoría"] a partir del árbol {"Depto": {"Cat": {"Sub": []}}}."""
    return sorted({f"{c} / {s}" for cats in tree.values() for c, subs in cats.items() for s in subs})
