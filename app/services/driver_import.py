"""Importación del "driver" de compras (Excel) a atributos y proveedores.

El driver es la planilla donde se registra cada ingreso de mercadería:

    FECHA | SKU | CATEGORIA | NOMBRE PRODUCTO | PROVEDOR | OBSERVACION |
    CODIGO DE BARRA | UNIDAD MEDIDA | CANTIDAD | CAJAS | C/U | COSTO TOTAL |
    REAL STOCK | COYA | ASAMBLEA | MAGDALENA | OPINIÓN | ALMACÉN | ENVÍO

más las columnas nuevas de la plantilla v2 (ver driver_template.py): TIPO BSALE,
MARCA, ORIGEN, PÚBLICO, LICENCIA, ESTILO, TEMPORADA, MATERIAL, CONTENIDO,
UNID X PACK, UNID X CAJA, COLOR, DISEÑO, TALLA, SABOR / AROMA.

Qué se importa:
  - PROVEDOR + C/U + FECHA → suppliers / product_suppliers (último costo y fecha).
  - Columnas de atributos → product_attrs / variant_attrs con source='driver'
    (pisa lo inferido, nunca lo cargado a mano).
  - CANTIDAD / CAJAS → unidades_caja, si no vino UNID X CAJA y la división es exacta.
  - TIPO BSALE → NO se aplica: se informa si difiere del tipo actual en BSale.
Las columnas de reparto (COYA, ASAMBLEA, MAGDALENA, ALMACÉN, ENVÍO) y de stock
no se importan.

Cada fila se empareja con una variante por SKU (variants.code / display_code)
y, si no, por código de barras.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import text

from app.services.attribute_catalog import DEFS_BY_CODE
from app.services.attribute_inference import norm, parse_contenido
from app.services.attribute_store import (
    get_or_create_supplier,
    recompute_primary_suppliers,
    upsert_attr,
    upsert_product_supplier,
)

# Encabezado normalizado → campo interno. Se aceptan variantes de escritura.
HEADER_ALIASES: dict[str, str] = {
    "FECHA": "fecha",
    "SKU": "sku", "CODIGO": "sku", "CODIGO SKU": "sku",
    "CATEGORIA": "categoria",
    "NOMBRE PRODUCTO": "nombre", "NOMBRE DEL PRODUCTO": "nombre", "PRODUCTO": "nombre", "NOMBRE": "nombre",
    "PROVEDOR": "proveedor", "PROVEEDOR": "proveedor",
    "OBSERVACION": "observacion", "OBSERVACIONES": "observacion",
    "CODIGO DE BARRA": "bar_code", "CODIGO DE BARRAS": "bar_code", "COD BARRA": "bar_code",
    "UNIDAD MEDIDA": "unidad", "UNIDAD DE MEDIDA": "unidad",
    "CANTIDAD": "cantidad", "CAJAS": "cajas", "C/U": "costo_unit", "COSTO UNITARIO": "costo_unit",
    "COSTO TOTAL": "costo_total",
    "TIPO BSALE": "tipo_bsale", "TIPO DE PRODUCTO": "tipo_bsale",
    "MARCA": "marca", "ORIGEN": "origen", "PUBLICO": "publico", "LICENCIA": "licencia",
    "LICENCIA / PERSONAJE": "licencia", "PERSONAJE": "licencia", "ESTILO": "estilo",
    "TEMPORADA": "temporada", "MATERIAL": "material", "CONTENIDO": "contenido",
    "UNID X PACK": "unidades_pack", "UNIDADES POR PACK": "unidades_pack",
    "UNID X CAJA": "unidades_caja", "UNIDADES POR CAJA": "unidades_caja",
    "COLOR": "color", "DISENO": "diseno", "DISENO / MODELO": "diseno", "MODELO": "diseno",
    "TALLA": "talla", "SABOR / AROMA": "sabor_aroma", "SABOR": "sabor_aroma", "AROMA": "sabor_aroma",
}
ATTR_FIELDS = ["marca", "origen", "publico", "licencia", "estilo", "temporada", "material",
               "contenido", "unidades_pack", "unidades_caja", "color", "diseno", "talla", "sabor_aroma"]


class DriverError(ValueError):
    pass


@dataclass
class DriverRow:
    row_number: int
    values: dict[str, object] = field(default_factory=dict)


def _clean_code(v: object) -> str | None:
    """SKU/barra como texto. Excel guarda códigos largos como float: 7750082136342.0 → '7750082136342'.
    La notación científica ('2.10014E+12') perdió dígitos y no se puede recuperar."""
    if v is None:
        return None
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return repr(v)
    if isinstance(v, int):
        return str(v)
    s = str(v).strip()
    return s or None


def _to_num(v: object) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("S/", "").replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _to_date(v: object) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
            try:
                return datetime.strptime(v.strip(), fmt).date()
            except ValueError:
                continue
    return None


def parse_driver(content: bytes, sheet: str | None = None) -> tuple[list[DriverRow], list[str]]:
    """Lee el Excel y devuelve (filas, columnas reconocidas). Busca la fila de
    encabezado en las primeras 15 filas (la que tenga SKU o CODIGO DE BARRA)."""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)

    header_map: dict[int, str] = {}
    header_row_n = 0
    for n, row in enumerate(rows, start=1):
        cand = {i: HEADER_ALIASES.get(norm(str(c))) for i, c in enumerate(row) if c is not None}
        cand = {i: f for i, f in cand.items() if f}
        if "sku" in cand.values() or "bar_code" in cand.values():
            header_map, header_row_n = cand, n
            break
        if n >= 15:
            break
    if not header_map:
        raise DriverError("No encontré la fila de encabezados (debe tener SKU o CODIGO DE BARRA).")

    out: list[DriverRow] = []
    for n, row in enumerate(rows, start=header_row_n + 1):
        vals = {f: row[i] for i, f in header_map.items() if i < len(row) and row[i] not in (None, "")}
        if not vals.get("sku") and not vals.get("bar_code"):
            continue
        out.append(DriverRow(n, vals))
    wb.close()
    return out, sorted(set(header_map.values()))


def _canonical(code: str, raw: object) -> tuple[str | None, str | None]:
    """(valor canónico, advertencia). Para enums acepta el valor sin importar
    mayúsculas ni tildes; si no está en la lista, devuelve advertencia."""
    s = " ".join(str(raw).split())
    d = DEFS_BY_CODE[code]
    if d.data_type != "enum":
        return s, None
    for allowed in d.allowed_values:
        if norm(allowed) == norm(s):
            return allowed, None
    return None, f"'{s}' no es un valor válido de {d.name} ({', '.join(d.allowed_values)})"


async def _variant_lookup(db, cid: int, rows: list[DriverRow]) -> dict[str, dict]:
    """Mapa código → {variant, product, type_name} para SKU y barras del archivo."""
    codes = set()
    for r in rows:
        for k in ("sku", "bar_code"):
            c = _clean_code(r.values.get(k))
            if c:
                codes.add(c.upper())
    if not codes:
        return {}
    res = await db.execute(
        text("""
            SELECT v.bsale_variant_id, v.bsale_product_id, upper(v.code) AS code,
                   upper(v.display_code) AS display_code, upper(v.bar_code) AS bar_code,
                   pt.name AS type_name, p.name AS product_name
            FROM variants v
            LEFT JOIN products p ON p.company_id = v.company_id AND p.bsale_product_id = v.bsale_product_id
            LEFT JOIN product_types pt ON pt.company_id = p.company_id
                                      AND pt.bsale_product_type_id = p.bsale_product_type_id
            WHERE v.company_id = :c
              AND (upper(v.code) = ANY(:codes) OR upper(v.display_code) = ANY(:codes)
                   OR upper(v.bar_code) = ANY(:codes))
            ORDER BY v.is_active DESC
        """),
        {"c": cid, "codes": list(codes)},
    )
    by_sku: dict[str, dict] = {}
    by_bar: dict[str, dict] = {}
    for r in res.mappings().all():
        info = dict(r)
        for k in ("code", "display_code"):
            if r[k]:
                by_sku.setdefault(r[k], info)
        if r["bar_code"]:
            by_bar.setdefault(r["bar_code"], info)
    return {"sku": by_sku, "bar": by_bar}


async def import_driver(db, cid: int, rows: list[DriverRow], *, dry_run: bool, user_id: int | None) -> dict:
    """Aplica las filas del driver. Con dry_run no escribe (el llamador hace rollback igual)."""
    lookup = await _variant_lookup(db, cid, rows)
    stats = {"filas": len(rows), "emparejadas": 0, "proveedores_nuevos": 0, "relaciones_proveedor": 0,
             "atributos_escritos": 0, "atributos_bloqueados_por_manual": 0}
    por_atributo: dict[str, int] = {}
    sin_match: list[dict] = []
    advertencias: list[dict] = []
    tipos_distintos: list[dict] = []
    nuevos_proveedores: set[str] = set()
    touched_products: set[int] = set()

    for r in rows:
        sku = _clean_code(r.values.get("sku"))
        bar = _clean_code(r.values.get("bar_code"))
        info = (lookup.get("sku", {}).get(sku.upper()) if sku else None) or \
               (lookup.get("bar", {}).get(bar.upper()) if bar else None)
        if not info:
            reason = "código de barras en notación científica (Excel perdió dígitos)" if bar and "E+" in bar.upper() \
                else "SKU/código de barras no existe en BSale"
            sin_match.append({"fila": r.row_number, "sku": sku, "codigo_barra": bar,
                              "nombre": r.values.get("nombre"), "motivo": reason})
            continue
        stats["emparejadas"] += 1
        pid, vid = info["bsale_product_id"], info["bsale_variant_id"]
        if pid is None:
            advertencias.append({"fila": r.row_number, "detalle": f"la variante {vid} no tiene producto"})
            continue
        touched_products.add(pid)

        tipo = r.values.get("tipo_bsale")
        if tipo and norm(str(tipo)) != norm(info["type_name"]):
            tipos_distintos.append({"fila": r.row_number, "sku": sku, "producto": info["product_name"],
                                    "tipo_actual": info["type_name"], "tipo_driver": str(tipo).strip()})

        prov = r.values.get("proveedor")
        if prov and str(prov).strip():
            name = " ".join(str(prov).split())
            if dry_run:
                exists = await db.scalar(
                    text("SELECT 1 FROM suppliers WHERE company_id = :c AND lower(name) = lower(:n)"),
                    {"c": cid, "n": name})
                if not exists:
                    nuevos_proveedores.add(name)
            else:
                sid, created = await get_or_create_supplier(db, cid, name)
                if created:
                    nuevos_proveedores.add(name)
                await upsert_product_supplier(
                    db, cid, pid, sid,
                    unit_cost=_to_num(r.values.get("costo_unit")),
                    purchase_date=_to_date(r.values.get("fecha")),
                )
            stats["relaciones_proveedor"] += 1

        values: dict[str, tuple] = {}
        for code in ATTR_FIELDS:
            raw = r.values.get(code)
            if raw is None or str(raw).strip() == "":
                continue
            if code == "contenido":
                parsed = parse_contenido(str(raw))
                if not parsed:
                    advertencias.append({"fila": r.row_number, "detalle": f"CONTENIDO '{raw}' sin unidad (ml, g, L, kg)"})
                    continue
                values[code] = (None, parsed.value_num, parsed.unit)
            elif code in ("unidades_pack", "unidades_caja"):
                num = _to_num(raw)
                if num and num > 0:
                    values[code] = (None, num, "und")
            else:
                val, warn = _canonical(code, raw)
                if warn:
                    advertencias.append({"fila": r.row_number, "detalle": warn})
                    continue
                values[code] = (val, None, None)

        cant, cajas = _to_num(r.values.get("cantidad")), _to_num(r.values.get("cajas"))
        if "unidades_caja" not in values and cant and cajas and cajas > 0 and cant / cajas >= 2 \
                and float(cant / cajas).is_integer():
            values["unidades_caja"] = (None, cant / cajas, "und")

        for code, (vt, vn, unit) in values.items():
            level = DEFS_BY_CODE[code].level
            entity = pid if level == "product" else vid
            por_atributo[code] = por_atributo.get(code, 0) + 1
            if dry_run:
                continue
            wrote = await upsert_attr(db, cid, level, entity, code, value_text=vt, value_num=vn, unit=unit,
                                      source="driver", confidence=1.0, user_id=user_id)
            stats["atributos_escritos" if wrote else "atributos_bloqueados_por_manual"] += 1

    if not dry_run:
        await recompute_primary_suppliers(db, cid, sorted(touched_products))
    stats["proveedores_nuevos"] = len(nuevos_proveedores)
    return {
        "stats": stats,
        "atributos_por_codigo": por_atributo,
        "proveedores_nuevos": sorted(nuevos_proveedores),
        "sin_match": sin_match,
        "tipo_bsale_distinto": tipos_distintos,
        "advertencias": advertencias,
    }
