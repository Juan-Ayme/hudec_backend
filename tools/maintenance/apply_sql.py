"""
Ejecuta un archivo .sql contra la BD del .env (alternativa a psql cuando no
está instalado). El archivo se manda entero: si trae BEGIN/COMMIT, la
transacción la maneja el propio SQL.

Uso:
    python -m tools.maintenance.apply_sql docs/migrations/2026-11-02_product_attributes.sql
    python -m tools.maintenance.apply_sql --check v_products_enriched product_attrs
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

root_path = Path(__file__).resolve().parent.parent.parent
if str(root_path) not in sys.path:
    sys.path.append(str(root_path))

from dotenv import load_dotenv  # noqa: E402
import psycopg2  # noqa: E402


def _conn():
    load_dotenv(root_path / ".env")
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ.get("DB_PASSWORD") or os.environ.get("DB_PASS"),
        sslmode=os.environ.get("DB_SSLMODE") or "require",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sql_file", nargs="?", help="Archivo .sql a ejecutar")
    ap.add_argument("--check", nargs="+", metavar="OBJETO", help="Solo verifica que existan estas tablas/vistas")
    args = ap.parse_args()
    if not args.sql_file and not args.check:
        ap.error("indicá un archivo .sql o --check")

    conn = _conn()
    conn.autocommit = True  # el BEGIN/COMMIT del archivo controla la transacción
    try:
        with conn.cursor() as cur:
            if args.sql_file:
                sql = Path(args.sql_file).read_text(encoding="utf-8")
                cur.execute(sql)
                print(f"ejecutado: {args.sql_file}")
            for obj in args.check or []:
                cur.execute("SELECT to_regclass(%s) IS NOT NULL", (obj,))
                print(f"{obj}: {'OK' if cur.fetchone()[0] else 'NO EXISTE'}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
