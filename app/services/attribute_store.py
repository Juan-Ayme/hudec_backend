"""Escritura de atributos y proveedores respetando la prioridad de fuentes.

Prioridad (attribute_catalog.SOURCE_PRIORITY): manual > driver > bsale > inferred.
El UPSERT solo pisa un valor existente si la fuente nueva tiene prioridad igual
o mayor; así una corrida de inferencia nunca borra lo que alguien cargó a mano.
"""

from __future__ import annotations

from sqlalchemy import text

from app.services.attribute_catalog import SOURCE_PRIORITY

_PRIO_SQL = "CASE {col} " + " ".join(
    f"WHEN '{s}' THEN {p}" for s, p in SOURCE_PRIORITY.items()
) + " ELSE 0 END"

_TABLES = {"product": ("product_attrs", "bsale_product_id"), "variant": ("variant_attrs", "bsale_variant_id")}


async def upsert_attr(
    db,
    company_id: int,
    level: str,
    entity_id: int,
    code: str,
    *,
    value_text: str | None = None,
    value_num: float | None = None,
    unit: str | None = None,
    source: str,
    confidence: float | None = None,
    user_id: int | None = None,
) -> bool:
    """Inserta o actualiza un valor. Devuelve True si escribió, False si un
    valor de mayor prioridad lo impidió."""
    table, id_col = _TABLES[level]
    prio_existing = _PRIO_SQL.format(col=f"{table}.source")
    prio_new = _PRIO_SQL.format(col="EXCLUDED.source")
    res = await db.execute(
        text(f"""
            INSERT INTO {table}
                (company_id, {id_col}, code, value_text, value_num, unit, source, confidence, updated_by, updated_at)
            VALUES (:c, :e, :code, :vt, :vn, :u, :source, :conf, :uid, now())
            ON CONFLICT (company_id, {id_col}, code) DO UPDATE SET
                value_text = EXCLUDED.value_text,
                value_num  = EXCLUDED.value_num,
                unit       = EXCLUDED.unit,
                source     = EXCLUDED.source,
                confidence = EXCLUDED.confidence,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            WHERE {prio_existing} <= {prio_new}
        """),
        {"c": company_id, "e": entity_id, "code": code, "vt": value_text, "vn": value_num,
         "u": unit, "source": source, "conf": confidence, "uid": user_id},
    )
    return bool(res.rowcount)


async def delete_attr(db, company_id: int, level: str, entity_id: int, code: str) -> bool:
    table, id_col = _TABLES[level]
    res = await db.execute(
        text(f"DELETE FROM {table} WHERE company_id = :c AND {id_col} = :e AND code = :code"),
        {"c": company_id, "e": entity_id, "code": code},
    )
    return bool(res.rowcount)


async def get_or_create_supplier(db, company_id: int, name: str) -> tuple[int, bool]:
    """(id, creado). El nombre se guarda tal cual, recortado; la búsqueda ignora mayúsculas."""
    clean = " ".join(name.split())
    sid = await db.scalar(
        text("SELECT id FROM suppliers WHERE company_id = :c AND lower(name) = lower(:n)"),
        {"c": company_id, "n": clean},
    )
    if sid is not None:
        return sid, False
    sid = await db.scalar(
        text("INSERT INTO suppliers (company_id, name) VALUES (:c, :n) RETURNING id"),
        {"c": company_id, "n": clean},
    )
    return sid, True


async def upsert_product_supplier(
    db, company_id: int, product_id: int, supplier_id: int,
    *, unit_cost: float | None, purchase_date, source: str = "driver",
) -> None:
    """Registra la relación producto–proveedor quedándose con la compra más reciente."""
    await db.execute(
        text("""
            INSERT INTO product_suppliers
                (company_id, bsale_product_id, supplier_id, last_unit_cost, last_purchase_date, source, updated_at)
            VALUES (:c, :p, :s, :cost, :d, :src, now())
            ON CONFLICT (company_id, bsale_product_id, supplier_id) DO UPDATE SET
                last_unit_cost     = CASE WHEN EXCLUDED.last_purchase_date IS NULL
                                            OR product_suppliers.last_purchase_date IS NULL
                                            OR EXCLUDED.last_purchase_date >= product_suppliers.last_purchase_date
                                          THEN COALESCE(EXCLUDED.last_unit_cost, product_suppliers.last_unit_cost)
                                          ELSE product_suppliers.last_unit_cost END,
                last_purchase_date = GREATEST(EXCLUDED.last_purchase_date, product_suppliers.last_purchase_date),
                updated_at         = now()
        """),
        {"c": company_id, "p": product_id, "s": supplier_id, "cost": unit_cost, "d": purchase_date, "src": source},
    )


async def recompute_primary_suppliers(db, company_id: int, product_ids: list[int]) -> None:
    """El proveedor principal de cada producto es el de la compra más reciente."""
    if not product_ids:
        return
    await db.execute(
        text("""
            WITH ranked AS (
                SELECT bsale_product_id, supplier_id,
                       row_number() OVER (PARTITION BY bsale_product_id
                                          ORDER BY last_purchase_date DESC NULLS LAST, updated_at DESC) AS rn
                FROM product_suppliers
                WHERE company_id = :c AND bsale_product_id = ANY(:ids)
            )
            UPDATE product_suppliers ps SET is_primary = (r.rn = 1)
            FROM ranked r
            WHERE ps.company_id = :c AND ps.bsale_product_id = r.bsale_product_id
              AND ps.supplier_id = r.supplier_id
        """),
        {"c": company_id, "ids": product_ids},
    )
