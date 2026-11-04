"""Migración de taxonomía por operaciones (rename / move / merge / create / drop_empty).

Las operaciones se expresan por RUTA DE NOMBRES, no por id, para que el mismo
archivo (Nueva_estructura/migracion_v1_v2.json) sirva para el árbol JSON y para
la BD de cualquier empresa:

    {"op": "rename",     "path": ["Hogar y Decoración", "Cocina"], "to": "Utensilios de Cocina"}
    {"op": "move",       "path": ["Hogar y Decoración", "Cocina"], "to": ["Cocina y Comedor"]}
    {"op": "merge",      "path": ["A", "B", "Galletas Asiáticos"], "into": ["X", "Y", "Galletas"]}
    {"op": "create",     "path": ["Cocina y Comedor", "Vajilla", "Platos Tendidos"]}
    {"op": "drop_empty", "path": ["Alimentos y Bebidas Asiáticas"]}

Semántica (idéntica en `apply_to_tree` y en el endpoint POST /taxonomy/migrate):
  - rename: si ya existe un hermano con el nombre destino, se FUSIONA en él.
  - move: cambia el padre (crea el padre si falta); si en el destino hay un
    nodo con el mismo nombre, se fusiona.
  - merge: mueve todo el contenido de `path` dentro de `into` (mismo nivel) y
    elimina `path`. Hijos con el mismo nombre se fusionan recursivamente.
  - create: crea los nodos que falten en la ruta.
  - drop_empty: elimina el nodo SOLO si no tiene hijos (en la BD además: sin
    productos, tipos ni metas). Si no está vacío, se informa y no se toca.
  - Una ruta origen que no existe se informa como `skipped` (la migración es
    re-ejecutable: lo ya migrado no existe más en el origen).

En la BD ninguna operación borra productos ni tipos de producto: las fusiones
reasignan `product_types.subcategory_id`, `products.subcategory_id` y
`category_targets` al destino antes de eliminar el nodo vacío.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

VALID_OPS = {"rename", "move", "merge", "create", "drop_empty"}


class MigrationError(ValueError):
    pass


@dataclass
class OpResult:
    index: int
    op: str
    path: list[str]
    status: str                    # 'applied' | 'skipped' | 'refused'
    detail: str = ""
    merged: bool = False


@dataclass
class TreeMigrationReport:
    results: list[OpResult] = field(default_factory=list)

    @property
    def applied(self) -> int:
        return sum(r.status == "applied" for r in self.results)

    def as_dict(self) -> dict:
        return {
            "applied": self.applied,
            "skipped": sum(r.status == "skipped" for r in self.results),
            "refused": sum(r.status == "refused" for r in self.results),
            "results": [r.__dict__ for r in self.results],
        }


def validate_ops(ops: list[dict]) -> None:
    """Valida la forma de las operaciones; levanta MigrationError con el índice."""
    if not isinstance(ops, list) or not ops:
        raise MigrationError("`ops` debe ser una lista no vacía")
    for i, o in enumerate(ops):
        if not isinstance(o, dict) or o.get("op") not in VALID_OPS:
            raise MigrationError(f"op #{i}: 'op' debe ser uno de {sorted(VALID_OPS)}")
        path = o.get("path")
        if not isinstance(path, list) or not 1 <= len(path) <= 3 or not all(isinstance(p, str) and p for p in path):
            raise MigrationError(f"op #{i}: 'path' debe ser una lista de 1 a 3 nombres")
        kind = o["op"]
        if kind == "rename" and not (isinstance(o.get("to"), str) and o["to"].strip()):
            raise MigrationError(f"op #{i}: rename requiere 'to' (nombre nuevo)")
        if kind == "move":
            to = o.get("to")
            if not isinstance(to, list) or len(to) != len(path) - 1:
                raise MigrationError(f"op #{i}: move requiere 'to' = ruta del nuevo padre ({len(path) - 1} niveles)")
        if kind == "merge":
            into = o.get("into")
            if not isinstance(into, list) or len(into) != len(path):
                raise MigrationError(f"op #{i}: merge requiere 'into' del mismo nivel que 'path'")


# ---------------------------------------------------------------------------
# Aplicación sobre el árbol JSON {"Depto": {"Cat": {"Sub": []}}}
# ---------------------------------------------------------------------------

def _children(tree: dict, parent_path: list[str]) -> dict | None:
    node = tree
    for name in parent_path:
        if not isinstance(node, dict) or name not in node:
            return None
        node = node[name]
    return node if isinstance(node, dict) else None


def _ensure(tree: dict, path: list[str]) -> dict:
    node = tree
    for depth, name in enumerate(path):
        if name not in node:
            node[name] = {} if depth < 2 else []
        node = node[name]
    return node


def _merge_nodes(dst: dict | list, src: dict | list) -> None:
    """Fusiona `src` dentro de `dst` (hijos con mismo nombre se fusionan)."""
    if isinstance(dst, list) or isinstance(src, list):
        return  # nivel subcategoría: no tiene hijos en el árbol
    for name, child in src.items():
        if name in dst:
            _merge_nodes(dst[name], child)
        else:
            dst[name] = child


def apply_to_tree(tree: dict, ops: list[dict]) -> tuple[dict, TreeMigrationReport]:
    """Aplica las operaciones a una copia del árbol y devuelve (árbol, informe)."""
    validate_ops(ops)
    t = copy.deepcopy(tree)
    rep = TreeMigrationReport()

    for i, o in enumerate(ops):
        kind, path = o["op"], list(o["path"])
        parent = _children(t, path[:-1])
        name = path[-1]
        exists = parent is not None and name in parent

        if kind == "create":
            if exists:
                rep.results.append(OpResult(i, kind, path, "skipped", "ya existe"))
            else:
                _ensure(t, path)
                rep.results.append(OpResult(i, kind, path, "applied"))
            continue

        if not exists:
            rep.results.append(OpResult(i, kind, path, "skipped", "la ruta origen no existe"))
            continue

        if kind == "rename":
            new = o["to"].strip()
            if new == name:
                rep.results.append(OpResult(i, kind, path, "skipped", "mismo nombre"))
                continue
            node = parent.pop(name)
            merged = new in parent
            if merged:
                _merge_nodes(parent[new], node)
            else:
                parent[new] = node
            rep.results.append(OpResult(i, kind, path, "applied", f"→ {new}", merged))

        elif kind == "move":
            target_parent = _ensure(t, list(o["to"])) if o["to"] else t
            if target_parent is parent:
                rep.results.append(OpResult(i, kind, path, "skipped", "ya está en ese padre"))
                continue
            node = parent.pop(name)
            merged = name in target_parent
            if merged:
                _merge_nodes(target_parent[name], node)
            else:
                target_parent[name] = node
            rep.results.append(OpResult(i, kind, path, "applied", f"→ {' > '.join(o['to'])}", merged))

        elif kind == "merge":
            into = list(o["into"])
            if into == path:
                rep.results.append(OpResult(i, kind, path, "skipped", "origen = destino"))
                continue
            target = _ensure(t, into)
            node = parent.pop(name)
            _merge_nodes(target, node)
            rep.results.append(OpResult(i, kind, path, "applied", f"→ {' > '.join(into)}", True))

        elif kind == "drop_empty":
            node = parent[name]
            if isinstance(node, dict) and node:
                rep.results.append(OpResult(i, kind, path, "refused", f"no está vacío ({len(node)} hijos)"))
            else:
                del parent[name]
                rep.results.append(OpResult(i, kind, path, "applied"))

    return t, rep


def translate_path(path: list[str] | tuple[str, ...], ops: list[dict]) -> tuple[str, ...]:
    """Dónde queda una ruta v1 después de aplicar `ops` (para saber la
    clasificación v2 de un producto antes de migrar la BD)."""
    cur = list(path)
    for o in ops:
        src = list(o["path"])
        n = len(src)
        if len(cur) < n or cur[:n] != src:
            continue
        if o["op"] == "rename":
            cur = src[:-1] + [o["to"].strip()] + cur[n:]
        elif o["op"] == "move":
            cur = list(o["to"]) + [src[-1]] + cur[n:]
        elif o["op"] == "merge":
            cur = list(o["into"]) + cur[n:]
    return tuple(cur)


def sort_tree(tree: dict) -> dict:
    """Orden alfabético estable (mismo orden que GET /taxonomy/export)."""
    return {
        d: {c: {s: [] for s in sorted(subs)} for c, subs in sorted(cats.items())}
        for d, cats in sorted(tree.items())
    }
