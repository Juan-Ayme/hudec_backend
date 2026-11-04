"""Catálogo de atributos de producto y variante.

Es la fuente de verdad de QUÉ atributos existen. `ensure_definitions` los
siembra en `attribute_definitions` para una empresa (idempotente, no pisa
cambios hechos a mano en `allowed_values`).

Nivel producto: describen el artículo entero (todas sus variantes comparten
marca, origen, público...). Nivel variante: lo que cambia entre variantes del
mismo producto (color, diseño, talla, sabor). Solo los de variante se empujan a
BSale (`push_to_bsale`), porque en BSale los atributos se definen por tipo de
producto y se cargan por variante.

El proveedor NO es un atributo: vive en `suppliers` / `product_suppliers`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import text

# Prioridad de las fuentes: un valor solo se reemplaza por otro de prioridad
# igual o mayor. La inferencia (0) nunca pisa lo cargado por driver o a mano.
SOURCE_PRIORITY: dict[str, int] = {"inferred": 0, "bsale": 1, "driver": 2, "manual": 3}


@dataclass(frozen=True)
class AttributeDef:
    code: str
    name: str
    level: str                      # 'product' | 'variant'
    data_type: str = "text"         # 'text' | 'enum' | 'number'
    allowed_values: list[str] = field(default_factory=list)
    push_to_bsale: bool = False


ORIGENES = ["Nacional", "Asiático", "Importado"]
PUBLICOS = ["Dama", "Caballero", "Niña", "Niño", "Infantil", "Bebé", "Unisex", "Adulto", "Mascota"]
ESTILOS = ["Kawaii", "Básico", "Premium"]
TEMPORADAS = [
    "Todo el año", "Escolar", "Navidad", "Halloween", "Día de la Madre",
    "Día del Padre", "San Valentín", "Invierno", "Verano",
]
MATERIALES = [
    "Vidrio", "Plástico", "Cerámica", "Loza", "Acero", "Aluminio", "Metal",
    "Madera", "Bambú", "Silicona", "Tela", "Peluche", "Cuero", "Papel",
]

ATTRIBUTE_DEFS: list[AttributeDef] = [
    # ── Nivel producto ──
    AttributeDef("marca", "Marca", "product"),
    AttributeDef("origen", "Origen", "product", "enum", ORIGENES),
    AttributeDef("publico", "Público", "product", "enum", PUBLICOS),
    AttributeDef("licencia", "Licencia / Personaje", "product"),
    AttributeDef("estilo", "Estilo", "product", "enum", ESTILOS),
    AttributeDef("temporada", "Temporada / Ocasión", "product", "enum", TEMPORADAS),
    AttributeDef("material", "Material", "product", "enum", MATERIALES),
    AttributeDef("contenido", "Contenido (ml, g, L, kg)", "product", "number"),
    AttributeDef("unidades_pack", "Unidades por pack (lo que se vende junto)", "product", "number"),
    AttributeDef("unidades_caja", "Unidades por caja del proveedor", "product", "number"),
    AttributeDef("grupo", "Grupo (mismo artículo con varios SKU)", "product"),
    # ── Nivel variante (se empujan a BSale) ──
    AttributeDef("color", "Color", "variant", push_to_bsale=True),
    AttributeDef("diseno", "Diseño / Modelo", "variant", push_to_bsale=True),
    AttributeDef("talla", "Talla", "variant", push_to_bsale=True),
    AttributeDef("sabor_aroma", "Sabor / Aroma", "variant", push_to_bsale=True),
]

DEFS_BY_CODE: dict[str, AttributeDef] = {d.code: d for d in ATTRIBUTE_DEFS}
PRODUCT_CODES: list[str] = [d.code for d in ATTRIBUTE_DEFS if d.level == "product"]
VARIANT_CODES: list[str] = [d.code for d in ATTRIBUTE_DEFS if d.level == "variant"]

# Columnas que expone v_products_enriched (y que los reportes pueden agrupar).
ENRICHED_COLUMNS: list[str] = [
    "marca", "origen", "publico", "licencia", "estilo", "temporada",
    "material", "contenido", "unidades_pack", "unidades_caja", "grupo", "proveedor",
]
_NUMERIC_ENRICHED = {"unidades_pack", "unidades_caja"}


def can_overwrite(existing_source: str | None, new_source: str) -> bool:
    """True si un valor de `new_source` puede reemplazar a uno de `existing_source`."""
    if existing_source is None:
        return True
    return SOURCE_PRIORITY.get(new_source, 0) >= SOURCE_PRIORITY.get(existing_source, 0)


async def ensure_definitions(db, company_id: int) -> int:
    """Inserta en `attribute_definitions` los atributos que falten. Devuelve
    cuántos insertó. No modifica definiciones existentes."""
    inserted = 0
    for order, d in enumerate(ATTRIBUTE_DEFS):
        res = await db.execute(
            text(
                "INSERT INTO attribute_definitions "
                "(company_id, code, name, level, data_type, allowed_values, push_to_bsale, sort_order) "
                "VALUES (:c, :code, :name, :level, :dt, CAST(:av AS jsonb), :push, :ord) "
                "ON CONFLICT (company_id, code) DO NOTHING"
            ),
            {
                "c": company_id, "code": d.code, "name": d.name, "level": d.level,
                "dt": d.data_type, "av": json.dumps(d.allowed_values, ensure_ascii=False),
                "push": d.push_to_bsale, "ord": order,
            },
        )
        inserted += res.rowcount or 0
    return inserted


# Fallback SQL cuando la migración 2026-11-02 todavía no se aplicó: mismas
# columnas que v_products_enriched, en NULL, para que los reportes no se caigan.
_FALLBACK_COLS = ", ".join(
    f"NULL::{'numeric' if c in _NUMERIC_ENRICHED else 'text'} AS {c}" for c in ENRICHED_COLUMNS
)
FALLBACK_ENRICHED_VIEW = f"(SELECT vpf.*, {_FALLBACK_COLS} FROM v_products_full vpf)"

_view_exists_cache: dict[str, bool] = {}


async def enriched_view_sql(db) -> str:
    """Devuelve el nombre de `v_products_enriched` si existe, o una subconsulta
    equivalente con los atributos en NULL. Cachea solo el resultado positivo
    (si la vista aparece después de un deploy, se detecta en la próxima request)."""
    if _view_exists_cache.get("v_products_enriched"):
        return "v_products_enriched"
    exists = await db.scalar(text("SELECT to_regclass('v_products_enriched') IS NOT NULL"))
    if exists:
        _view_exists_cache["v_products_enriched"] = True
        return "v_products_enriched"
    return FALLBACK_ENRICHED_VIEW
