"""
Genera la plantilla Excel del driver de compras con las columnas de atributos
y listas desplegables (TIPO BSALE sale de la taxonomía v2).

No toca la BD. La misma plantilla se descarga desde GET /attributes/driver-template
(ahí con la taxonomía vigente de la empresa).

Uso:
    python -m tools.taxonomy.build_driver_template
    python -m tools.taxonomy.build_driver_template --out mi_driver.xlsx
"""

from __future__ import annotations

import argparse
from pathlib import Path

from tools.taxonomy._common import BASE, load_v2_tree

from app.services.driver_template import build_driver_template, tipos_from_tree  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(BASE / "driver_compras_v2.xlsx"))
    args = ap.parse_args()
    content = build_driver_template(tipos_from_tree(load_v2_tree()))
    Path(args.out).write_bytes(content)
    print(f"escrito: {args.out}")


if __name__ == "__main__":
    main()
