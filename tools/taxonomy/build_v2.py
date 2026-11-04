"""
Genera Nueva_estructura/kawii-pluss-v2.json aplicando migracion_v1_v2.json
sobre kawii-pluss-v1.json (el export real de GET /taxonomy/export).

No toca la BD: es la vista previa en JSON de lo que hará POST /taxonomy/migrate.
Termina con código 1 si alguna operación fue rechazada u omitida, para que un
cambio en v1 que rompa el mapa se note.

Uso:
    python -m tools.taxonomy.build_v2
    python -m tools.taxonomy.build_v2 --v1 otro.json --out salida.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

root_path = Path(__file__).resolve().parent.parent.parent
if str(root_path) not in sys.path:
    sys.path.append(str(root_path))

from app.services.taxonomy_migration import apply_to_tree, sort_tree  # noqa: E402

BASE = root_path / "Nueva_estructura"


def _count(tree: dict) -> tuple[int, int, int]:
    cats = sum(len(c) for c in tree.values())
    subs = sum(len(s) for c in tree.values() for s in c.values())
    return len(tree), cats, subs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--v1", default=str(BASE / "kawii-pluss-v1.json"))
    ap.add_argument("--ops", default=str(BASE / "migracion_v1_v2.json"))
    ap.add_argument("--out", default=str(BASE / "kawii-pluss-v2.json"))
    args = ap.parse_args()

    v1 = json.loads(Path(args.v1).read_text(encoding="utf-8"))
    ops = json.loads(Path(args.ops).read_text(encoding="utf-8"))["ops"]
    v2, rep = apply_to_tree(v1, ops)
    v2 = sort_tree(v2)
    Path(args.out).write_text(json.dumps(v2, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("v1: %d departamentos / %d categorías / %d subcategorías" % _count(v1))
    print("v2: %d departamentos / %d categorías / %d subcategorías" % _count(v2))
    print(f"operaciones aplicadas: {rep.applied}/{len(ops)}")
    problems = [r for r in rep.results if r.status != "applied"]
    for r in problems:
        print(f"  #{r.index} {r.op} {' > '.join(r.path)}: {r.status} ({r.detail})")
    print(f"escrito: {args.out}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
