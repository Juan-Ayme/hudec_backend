"""Aplicación de la migración de taxonomía sobre la BD de una empresa.

Misma semántica que `taxonomy_migration.apply_to_tree` (ver su docstring),
pero sobre departments / categories / subcategories. Corre dentro de la
transacción del llamador y NO hace commit: el endpoint decide commit (aplicar)
o rollback (dry_run, o si fallan las verificaciones).

Garantías:
  - Nunca toca `products`, `variants` ni ventas salvo para REASIGNAR
    `products.subcategory_id` (override) al nodo destino de una fusión.
  - `product_types.subcategory_id` y `category_targets` se reasignan antes de
    borrar el nodo origen, que ya quedó vacío.
  - Si dos categorías fusionadas tienen meta para la misma sucursal, aborta
    (o conserva la del destino si se pide explícitamente).
"""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import text

from app.services.taxonomy_migration import (
    MigrationError,
    OpResult,
    TreeMigrationReport,
    validate_ops,
)

_TABLE = {1: "departments", 2: "categories", 3: "subcategories"}
_PARENT_COL = {2: "department_id", 3: "category_id"}

TARGETS_ABORT = "abort"
TARGETS_KEEP_DESTINATION = "keep_destination"


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "sin-nombre"


class DbMigrator:
    """Aplica operaciones de migración sobre la taxonomía de UNA empresa."""

    def __init__(self, db, company_id: int, targets_on_conflict: str = TARGETS_ABORT):
        if targets_on_conflict not in (TARGETS_ABORT, TARGETS_KEEP_DESTINATION):
            raise MigrationError("targets_on_conflict debe ser 'abort' o 'keep_destination'")
        self.db = db
        self.cid = company_id
        self.targets_on_conflict = targets_on_conflict
        self.report = TreeMigrationReport()
        self.targets_dropped: list[dict] = []

    # ── lookups ──────────────────────────────────────────────────────────
    async def _find(self, path: list[str]) -> int | None:
        parent_id: int | None = None
        node_id: int | None = None
        for depth, name in enumerate(path, start=1):
            if depth == 1:
                q = "SELECT id FROM departments WHERE company_id = :c AND name = :n"
                p = {"c": self.cid, "n": name}
            else:
                q = (f"SELECT id FROM {_TABLE[depth]} WHERE company_id = :c "
                     f"AND {_PARENT_COL[depth]} = :p AND name = :n")
                p = {"c": self.cid, "p": parent_id, "n": name}
            node_id = await self.db.scalar(text(q), p)
            if node_id is None:
                return None
            parent_id = node_id
        return node_id

    async def _path_names(self, depth: int, node_id: int) -> list[str]:
        if depth == 1:
            name = await self.db.scalar(
                text("SELECT name FROM departments WHERE company_id = :c AND id = :i"),
                {"c": self.cid, "i": node_id},
            )
            return [name]
        row = (await self.db.execute(
            text(f"SELECT name, {_PARENT_COL[depth]} AS parent FROM {_TABLE[depth]} "
                 f"WHERE company_id = :c AND id = :i"),
            {"c": self.cid, "i": node_id},
        )).mappings().first()
        return (await self._path_names(depth - 1, row["parent"])) + [row["name"]]

    async def _ensure(self, path: list[str]) -> int:
        """Devuelve el id del nodo en `path`, creando los que falten."""
        parent_id: int | None = None
        for depth in range(1, len(path) + 1):
            existing = await self._find(path[:depth])
            if existing is not None:
                parent_id = existing
                continue
            slug = _slug("-".join(path[:depth]))
            if depth == 1:
                parent_id = await self.db.scalar(
                    text("INSERT INTO departments (company_id, name, slug) VALUES (:c, :n, :s) RETURNING id"),
                    {"c": self.cid, "n": path[0], "s": slug},
                )
            else:
                parent_id = await self.db.scalar(
                    text(f"INSERT INTO {_TABLE[depth]} (company_id, {_PARENT_COL[depth]}, name, slug) "
                         f"VALUES (:c, :p, :n, :s) RETURNING id"),
                    {"c": self.cid, "p": parent_id, "n": path[depth - 1], "s": slug},
                )
        return parent_id  # type: ignore[return-value]

    async def _children_of(self, depth: int, node_id: int) -> list[tuple[int, str]]:
        child = depth + 1
        rows = (await self.db.execute(
            text(f"SELECT id, name FROM {_TABLE[child]} WHERE company_id = :c AND {_PARENT_COL[child]} = :p"),
            {"c": self.cid, "p": node_id},
        )).all()
        return [(r[0], r[1]) for r in rows]

    async def _sub_refs(self, sub_id: int) -> int:
        return (await self.db.scalar(
            text("SELECT (SELECT count(*) FROM product_types WHERE company_id = :c AND subcategory_id = :s)"
                 "     + (SELECT count(*) FROM products      WHERE company_id = :c AND subcategory_id = :s)"),
            {"c": self.cid, "s": sub_id},
        )) or 0

    async def _cat_targets(self, cat_id: int) -> int:
        return (await self.db.scalar(
            text("SELECT count(*) FROM category_targets WHERE company_id = :c AND category_id = :i"),
            {"c": self.cid, "i": cat_id},
        )) or 0

    # ── primitivas ───────────────────────────────────────────────────────
    async def _merge_node(self, depth: int, src: int, dst: int) -> None:
        """Mueve TODO el contenido de `src` a `dst` (mismo nivel) y borra `src` vacío."""
        if depth == 3:
            for tbl in ("product_types", "products"):
                await self.db.execute(
                    text(f"UPDATE {tbl} SET subcategory_id = :d WHERE company_id = :c AND subcategory_id = :s"),
                    {"c": self.cid, "s": src, "d": dst},
                )
            if await self._sub_refs(src):
                raise MigrationError(f"subcategoría {src}: quedaron referencias tras reasignar")
        else:
            dst_children = {name: cid for cid, name in await self._children_of(depth, dst)}
            for child_id, child_name in await self._children_of(depth, src):
                if child_name in dst_children:
                    await self._merge_node(depth + 1, child_id, dst_children[child_name])
                else:
                    await self.db.execute(
                        text(f"UPDATE {_TABLE[depth + 1]} SET {_PARENT_COL[depth + 1]} = :d "
                             f"WHERE company_id = :c AND id = :i"),
                        {"c": self.cid, "d": dst, "i": child_id},
                    )
            if depth == 2:
                await self._move_targets(src, dst)
        await self.db.execute(
            text(f"DELETE FROM {_TABLE[depth]} WHERE company_id = :c AND id = :i"),
            {"c": self.cid, "i": src},
        )

    async def _move_targets(self, src_cat: int, dst_cat: int) -> None:
        """Pasa las metas de `src_cat` a `dst_cat`. Si ambas tienen meta para la
        misma sucursal: aborta, o conserva la del destino según la política."""
        conflicts = (await self.db.execute(
            text("SELECT s.bsale_office_id, s.meta_mensual_pen, s.rol FROM category_targets s "
                 "JOIN category_targets d ON d.company_id = s.company_id "
                 " AND d.bsale_office_id = s.bsale_office_id AND d.category_id = :d "
                 "WHERE s.company_id = :c AND s.category_id = :s"),
            {"c": self.cid, "s": src_cat, "d": dst_cat},
        )).mappings().all()
        if conflicts:
            src_name = " > ".join(await self._path_names(2, src_cat))
            dst_name = " > ".join(await self._path_names(2, dst_cat))
            if self.targets_on_conflict == TARGETS_ABORT:
                raise MigrationError(
                    f"'{src_name}' y '{dst_name}' tienen metas para las mismas sucursales "
                    f"({', '.join(str(r['bsale_office_id']) for r in conflicts)}). Revisá las metas o "
                    f"repetí con targets_on_conflict='keep_destination'."
                )
            for r in conflicts:
                self.targets_dropped.append({"categoria": src_name, "se_mantiene": dst_name, **dict(r)})
            await self.db.execute(
                text("DELETE FROM category_targets WHERE company_id = :c AND category_id = :s "
                     "AND bsale_office_id = ANY(:o)"),
                {"c": self.cid, "s": src_cat, "o": [r["bsale_office_id"] for r in conflicts]},
            )
        await self.db.execute(
            text("UPDATE category_targets SET category_id = :d WHERE company_id = :c AND category_id = :s"),
            {"c": self.cid, "s": src_cat, "d": dst_cat},
        )

    async def _rename_or_merge(self, depth: int, node_id: int, parent_path: list[str], new_name: str) -> bool:
        sibling = await self._find(parent_path + [new_name])
        if sibling is not None and sibling != node_id:
            await self._merge_node(depth, node_id, sibling)
            return True
        await self.db.execute(
            text(f"UPDATE {_TABLE[depth]} SET name = :n, slug = :s WHERE company_id = :c AND id = :i"),
            {"c": self.cid, "i": node_id, "n": new_name, "s": _slug("-".join(parent_path + [new_name]))},
        )
        return False

    async def _not_empty_reason(self, depth: int, node: int) -> str | None:
        if depth < 3:
            children = await self._children_of(depth, node)
            if children:
                return f"tiene {len(children)} hijos ({', '.join(n for _, n in children[:5])})"
        if depth == 2 and await self._cat_targets(node):
            return "tiene metas en category_targets"
        if depth == 3 and await self._sub_refs(node):
            return "tiene productos o tipos de producto asignados"
        return None

    # ── operaciones ──────────────────────────────────────────────────────
    async def apply(self, ops: list[dict]) -> TreeMigrationReport:
        validate_ops(ops)
        for i, o in enumerate(ops):
            kind, path = o["op"], list(o["path"])
            depth = len(path)
            node = await self._find(path)
            res = self.report.results

            if kind == "create":
                if node is not None:
                    res.append(OpResult(i, kind, path, "skipped", "ya existe"))
                else:
                    await self._ensure(path)
                    res.append(OpResult(i, kind, path, "applied"))
                continue

            if node is None:
                res.append(OpResult(i, kind, path, "skipped", "la ruta origen no existe"))
                continue

            if kind == "rename":
                new = o["to"].strip()
                if new == path[-1]:
                    res.append(OpResult(i, kind, path, "skipped", "mismo nombre"))
                    continue
                merged = await self._rename_or_merge(depth, node, path[:-1], new)
                res.append(OpResult(i, kind, path, "applied", f"→ {new}", merged))

            elif kind == "move":
                to = list(o["to"])
                if to == path[:-1]:
                    res.append(OpResult(i, kind, path, "skipped", "ya está en ese padre"))
                    continue
                new_parent = await self._ensure(to)
                existing = await self._find(to + [path[-1]])
                if existing is not None:
                    await self._merge_node(depth, node, existing)
                    merged = True
                else:
                    await self.db.execute(
                        text(f"UPDATE {_TABLE[depth]} SET {_PARENT_COL[depth]} = :p, slug = :s "
                             f"WHERE company_id = :c AND id = :i"),
                        {"c": self.cid, "i": node, "p": new_parent, "s": _slug("-".join(to + [path[-1]]))},
                    )
                    merged = False
                res.append(OpResult(i, kind, path, "applied", f"→ {' > '.join(to)}", merged))

            elif kind == "merge":
                into = list(o["into"])
                if into == path:
                    res.append(OpResult(i, kind, path, "skipped", "origen = destino"))
                    continue
                dst = await self._ensure(into)
                await self._merge_node(depth, node, dst)
                res.append(OpResult(i, kind, path, "applied", f"→ {' > '.join(into)}", True))

            elif kind == "drop_empty":
                reason = await self._not_empty_reason(depth, node)
                if reason:
                    res.append(OpResult(i, kind, path, "refused", reason))
                else:
                    await self.db.execute(
                        text(f"DELETE FROM {_TABLE[depth]} WHERE company_id = :c AND id = :i"),
                        {"c": self.cid, "i": node},
                    )
                    res.append(OpResult(i, kind, path, "applied"))
        return self.report


async def taxonomy_snapshot(db, company_id: int) -> dict[str, int]:
    """Conteos que una migración NO debe cambiar (salvo metas descartadas a propósito)."""
    row = (await db.execute(text("""
        SELECT
          (SELECT count(*) FROM products WHERE company_id = :c)                AS productos,
          (SELECT count(*) FROM products p
             LEFT JOIN product_types pt ON pt.company_id = p.company_id
                                      AND pt.bsale_product_type_id = p.bsale_product_type_id
            WHERE p.company_id = :c
              AND COALESCE(p.subcategory_id, pt.subcategory_id) IS NOT NULL)  AS productos_clasificados,
          (SELECT count(*) FROM product_types WHERE company_id = :c)           AS tipos,
          (SELECT count(*) FROM product_types WHERE company_id = :c
              AND subcategory_id IS NOT NULL)                                 AS tipos_mapeados,
          (SELECT count(*) FROM category_targets WHERE company_id = :c)        AS metas,
          (SELECT count(*) FROM departments WHERE company_id = :c)             AS departamentos,
          (SELECT count(*) FROM categories WHERE company_id = :c)              AS categorias,
          (SELECT count(*) FROM subcategories WHERE company_id = :c)           AS subcategorias
    """), {"c": company_id})).mappings().first()
    return {k: int(v or 0) for k, v in dict(row).items()}


INVARIANTS = ("productos", "productos_clasificados", "tipos", "tipos_mapeados")


def check_invariants(before: dict[str, int], after: dict[str, int], targets_dropped: int) -> list[str]:
    """Errores si la migración perdió productos, clasificaciones o metas."""
    errors = [
        f"'{k}' cambió de {before[k]} a {after[k]}"
        for k in INVARIANTS if before[k] != after[k]
    ]
    if after["metas"] != before["metas"] - targets_dropped:
        errors.append(
            f"metas: había {before['metas']}, se descartaron {targets_dropped} a propósito, "
            f"pero quedaron {after['metas']}"
        )
    return errors


async def bsale_type_renames(db, company_id: int) -> list[dict]:
    """Tipos de BSale cuyo nombre ya no coincide con 'Categoría / Subcategoría'
    de su mapeo. Es la lista para POST /bsale/product-types/bulk-map con
    rename_in_bsale=true (no se renombra nada automáticamente). Si varios
    tipos comparten subcategoría, `tipos_en_la_subcategoria` > 1: renombrarlos
    todos igual deja nombres duplicados en BSale, conviene revisarlos a mano."""
    rows = (await db.execute(text("""
        SELECT pt.bsale_product_type_id, pt.name AS nombre_actual,
               c.name || ' / ' || s.name AS nombre_sugerido,
               s.id AS subcategory_id,
               count(*) OVER (PARTITION BY s.id) AS tipos_en_la_subcategoria
        FROM product_types pt
        JOIN subcategories s ON s.company_id = pt.company_id AND s.id = pt.subcategory_id
        JOIN categories c    ON c.company_id = s.company_id  AND c.id = s.category_id
        WHERE pt.company_id = :c AND pt.is_active
        ORDER BY 3, 2
    """), {"c": company_id})).mappings().all()
    return [dict(r) for r in rows if r["nombre_actual"] != r["nombre_sugerido"]]
