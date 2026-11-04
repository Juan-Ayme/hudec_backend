"""Utilidades compartidas por los scripts de tools/taxonomy."""

from __future__ import annotations

import csv
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

root_path = Path(__file__).resolve().parent.parent.parent
if str(root_path) not in sys.path:
    sys.path.append(str(root_path))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(root_path / ".env")

from sqlalchemy import text  # noqa: E402

from app.database import async_session_maker  # noqa: E402

BASE = root_path / "Nueva_estructura"
REPORTS = BASE / "reportes"


@asynccontextmanager
async def company_session(company_id: int):
    """Sesión con el contexto RLS de la empresa (igual que get_current_company).
    El llamador decide commit; si no hace commit, todo se descarta al salir."""
    async with async_session_maker() as db:
        await db.execute(text(f"SET LOCAL app.current_company = '{int(company_id)}'"))
        try:
            yield db
        finally:
            await db.rollback()


def load_ops() -> list[dict]:
    return json.loads((BASE / "migracion_v1_v2.json").read_text(encoding="utf-8"))["ops"]


def load_v2_tree() -> dict:
    return json.loads((BASE / "kawii-pluss-v2.json").read_text(encoding="utf-8"))


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    """CSV con BOM y ';' para que Excel en español lo abra con tildes y columnas bien."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


PRODUCTS_SQL = """
    SELECT p.bsale_product_id, p.name, p.is_active,
           pt.name AS type_name,
           vpf.department, vpf.category, vpf.subcategory,
           v.display_code AS sku, v.bar_code,
           (SELECT count(*) FROM variants v2
             WHERE v2.company_id = p.company_id AND v2.bsale_product_id = p.bsale_product_id) AS n_variantes
    FROM products p
    JOIN v_products_full vpf ON vpf.company_id = p.company_id AND vpf.bsale_product_id = p.bsale_product_id
    LEFT JOIN product_types pt ON pt.company_id = p.company_id
                              AND pt.bsale_product_type_id = p.bsale_product_type_id
    LEFT JOIN LATERAL (
        SELECT display_code, bar_code FROM variants v1
        WHERE v1.company_id = p.company_id AND v1.bsale_product_id = p.bsale_product_id
        ORDER BY v1.is_active DESC, v1.bsale_variant_id
        LIMIT 1
    ) v ON true
    WHERE p.company_id = :c AND (p.is_active OR :incluir_inactivos)
    ORDER BY p.name
"""


async def fetch_products(db, company_id: int, include_inactive: bool = False) -> list[dict]:
    rows = (await db.execute(text(PRODUCTS_SQL), {"c": company_id, "incluir_inactivos": include_inactive})).mappings().all()
    return [dict(r) for r in rows]
