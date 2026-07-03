"""
Endpoints de ADMINISTRACIÓN de taxonomía interna.

Cada operación devuelve un mini-informe JSON estandarizado:
{
    "ok": bool,
    "operation": "create_subcategory",
    "timestamp": "2026-04-25T01:30:00Z",
    "entity": {...},
    "report": {
        "rows_affected": int,
        "scope": "internal_db" | "bsale+internal",
        "warnings": []
    }
}

IMPORTANTE: Department / Category / Subcategory existen sólo en TU BD.
            BSale no tiene esos conceptos. Por eso estas operaciones
            NO tocan BSale (scope=internal_db).
"""

from datetime import datetime, timezone
import re
import unicodedata

from fastapi import Depends, APIRouter, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text
from pydantic import BaseModel, Field

from app.auth import CurrentCompany, get_current_company, require_admin
from app.database import get_db

# Modifica la taxonomía local (departments / categories / subcategories).
# Afecta cómo se agrupan TODOS los reportes — solo admin.
# get_current_company activa RLS para aislar por empresa.
router = APIRouter(
    prefix="/taxonomy",
    tags=["taxonomy-admin"],
    dependencies=[Depends(get_current_company), Depends(require_admin)],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _slugify(text: str) -> str:
    """Convierte 'Hogar y Decoración' -> 'hogar-y-decoracion'."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9\s-]", "", text).strip().lower()
    return re.sub(r"[\s-]+", "-", text) or "sin-nombre"


async def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _report(operation: str, entity: dict | None, rows: int = 1,
            scope: str = "internal_db", warnings: list[str] | None = None,
            extra: dict | None = None) -> dict:
    """Construye un informe JSON estándar. `extra` se mergea en el nivel raíz."""
    base = {
        "ok": True,
        "operation": operation,
        "timestamp": await _now(),
        "entity": entity,
        "report": {
            "rows_affected": rows,
            "scope": scope,
            "warnings": warnings or [],
        },
    }
    if extra:
        base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------

class DepartmentIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


class CategoryIn(BaseModel):
    department_id: int
    name: str = Field(..., min_length=1, max_length=120)


class SubcategoryIn(BaseModel):
    category_id: int
    name: str = Field(..., min_length=1, max_length=120)


class RenameIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


# ---------------------------------------------------------------------------
# DEPARTMENTS
# ---------------------------------------------------------------------------

@router.post("/departments", status_code=201)
async def create_department(body: DepartmentIn, db: AsyncSession = Depends(get_db)) -> dict:
    res = await db.execute(text("SELECT id FROM departments WHERE name = :name"), {"name": body.name})
    existing = res.scalar()
    if existing:
        raise HTTPException(409, f"Ya existe un departamento con nombre '{body.name}'")

    slug = await _slugify(body.name)
    res = await db.execute(
        text("INSERT INTO departments (name, slug) VALUES (:name, :slug) RETURNING id"),
        {"name": body.name, "slug": slug}
    )
    await db.commit()
    new_id = res.scalar()

    entity_res = await db.execute(text("SELECT id, name, slug FROM departments WHERE id = :id"), {"id": new_id})
    entity = dict(entity_res.mappings().first())
    return await _report("create_department", entity)


@router.patch("/departments/{dep_id}")
async def rename_department(dep_id: int, body: RenameIn, db: AsyncSession = Depends(get_db)) -> dict:
    exists_res = await db.execute(text("SELECT 1 FROM departments WHERE id = :id"), {"id": dep_id})
    if not exists_res.scalar():
        raise HTTPException(404, f"Departamento {dep_id} no existe")

    slug = await _slugify(body.name)
    await db.execute(
        text("UPDATE departments SET name = :name, slug = :slug WHERE id = :id"),
        {"name": body.name, "slug": slug, "id": dep_id}
    )
    await db.commit()

    entity_res = await db.execute(text("SELECT id, name, slug FROM departments WHERE id = :id"), {"id": dep_id})
    entity = dict(entity_res.mappings().first())
    return await _report("rename_department", entity)


@router.delete("/departments/{dep_id}")
async def delete_department(dep_id: int, force: bool = False, db: AsyncSession = Depends(get_db)) -> dict:
    dept_res = await db.execute(text("SELECT id, name FROM departments WHERE id = :id"), {"id": dep_id})
    dept_row = dept_res.mappings().first()
    if not dept_row:
        raise HTTPException(404, f"Departamento {dep_id} no existe")
    dept = dict(dept_row)

    n_cats_res = await db.execute(text("SELECT COUNT(*) FROM categories WHERE department_id = :id"), {"id": dep_id})
    n_cats = n_cats_res.scalar() or 0
    warnings = []
    if n_cats > 0 and not force:
        raise HTTPException(
            409,
            f"Departamento '{dept['name']}' tiene {n_cats} categorias. "
            f"Use ?force=true para eliminar en cascada (los productos quedaran sin clasificar)."
        )
    if n_cats > 0:
        warnings.append(f"Cascada: se eliminaron {n_cats} categorias y sus subcategorias")

    await db.execute(text("DELETE FROM departments WHERE id = :id"), {"id": dep_id})
    await db.commit()

    return await _report("delete_department", dept, warnings=warnings)


# ---------------------------------------------------------------------------
# CATEGORIES
# ---------------------------------------------------------------------------

@router.post("/categories", status_code=201)
async def create_category(body: CategoryIn, db: AsyncSession = Depends(get_db)) -> dict:
    dept_res = await db.execute(text("SELECT id, name FROM departments WHERE id = :id"), {"id": body.department_id})
    dept_row = dept_res.mappings().first()
    if not dept_row:
        raise HTTPException(404, f"Departamento {body.department_id} no existe")
    dept = dict(dept_row)

    existing_res = await db.execute(
        text("SELECT id FROM categories WHERE department_id = :did AND name = :name"),
        {"did": body.department_id, "name": body.name}
    )
    if existing_res.scalar():
        raise HTTPException(
            409,
            f"Ya existe una categoría '{body.name}' dentro del departamento '{dept['name']}'",
        )

    slug = await _slugify(f"{dept['name']}-{body.name}")
    res = await db.execute(
        text("INSERT INTO categories (department_id, name, slug) VALUES (:did, :name, :slug) RETURNING id"),
        {"did": body.department_id, "name": body.name, "slug": slug}
    )
    await db.commit()
    new_id = res.scalar()

    entity_res = await db.execute(text("""
        SELECT c.id, c.name, c.slug, c.department_id, d.name AS department_name
        FROM categories c JOIN departments d ON d.id = c.department_id
        WHERE c.id = :id
    """), {"id": new_id})
    entity = dict(entity_res.mappings().first())
    return await _report("create_category", entity)


@router.patch("/categories/{cat_id}")
async def rename_category(cat_id: int, body: RenameIn, db: AsyncSession = Depends(get_db)) -> dict:
    cat_res = await db.execute(text("""
        SELECT c.id, c.department_id, d.name AS department_name
        FROM categories c JOIN departments d ON d.id = c.department_id
        WHERE c.id = :id
    """), {"id": cat_id})
    cat_row = cat_res.mappings().first()
    if not cat_row:
        raise HTTPException(404, f"Categoria {cat_id} no existe")
    cat = dict(cat_row)

    slug = await _slugify(f"{cat['department_name']}-{body.name}")
    await db.execute(
        text("UPDATE categories SET name = :name, slug = :slug WHERE id = :id"),
        {"name": body.name, "slug": slug, "id": cat_id}
    )
    await db.commit()

    entity_res = await db.execute(text("""
        SELECT c.id, c.name, c.slug, c.department_id, d.name AS department_name
        FROM categories c JOIN departments d ON d.id = c.department_id
        WHERE c.id = :id
    """), {"id": cat_id})
    entity = dict(entity_res.mappings().first())
    return await _report("rename_category", entity)


@router.delete("/categories/{cat_id}")
async def delete_category(cat_id: int, force: bool = False, db: AsyncSession = Depends(get_db)) -> dict:
    cat_res = await db.execute(text("SELECT id, name FROM categories WHERE id = :id"), {"id": cat_id})
    cat_row = cat_res.mappings().first()
    if not cat_row:
        raise HTTPException(404, f"Categoria {cat_id} no existe")
    cat = dict(cat_row)

    n_subs_res = await db.execute(text("SELECT COUNT(*) FROM subcategories WHERE category_id = :id"), {"id": cat_id})
    n_subs = n_subs_res.scalar() or 0
    warnings = []
    if n_subs > 0 and not force:
        raise HTTPException(
            409,
            f"Categoria '{cat['name']}' tiene {n_subs} subcategorias. "
            f"Use ?force=true para eliminar en cascada."
        )
    if n_subs > 0:
        warnings.append(f"Cascada: se eliminaron {n_subs} subcategorias")

    await db.execute(text("DELETE FROM categories WHERE id = :id"), {"id": cat_id})
    await db.commit()

    return await _report("delete_category", cat, warnings=warnings)


# ---------------------------------------------------------------------------
# CLEANUP: categorias vacias (sin productos ni historial)
# ---------------------------------------------------------------------------

_SQL_AUDIT_EMPTY_CATEGORIES = """
WITH conteos AS (
  SELECT
    c.id   AS cat_id,
    c.name AS categoria,
    d.name AS departamento,
    (SELECT COUNT(*) FROM subcategories s
       WHERE s.category_id = c.id)                            AS n_subcats,
    (SELECT COUNT(*) FROM product_types pt
       JOIN subcategories s ON s.id = pt.subcategory_id
       WHERE s.category_id = c.id)                            AS n_product_types,
    (SELECT COUNT(*) FROM products p
       JOIN subcategories s ON s.id = p.subcategory_id
       WHERE s.category_id = c.id)                            AS n_prod_override,
    (SELECT COUNT(DISTINCT p.bsale_product_id)
       FROM products p
       JOIN product_types pt ON pt.bsale_product_type_id = p.bsale_product_type_id
       JOIN subcategories s  ON s.id = pt.subcategory_id
       WHERE s.category_id = c.id)                            AS n_prod_via_pt,
    (SELECT COUNT(*) FROM document_details dd
       JOIN variants v       ON v.bsale_variant_id = dd.bsale_variant_id
       JOIN products p       ON p.bsale_product_id = v.bsale_product_id
       LEFT JOIN product_types pt
              ON pt.bsale_product_type_id = p.bsale_product_type_id
       LEFT JOIN subcategories s_pt ON s_pt.id = pt.subcategory_id
       LEFT JOIN subcategories s_ov ON s_ov.id = p.subcategory_id
       WHERE s_pt.category_id = c.id OR s_ov.category_id = c.id)
                                                              AS n_ventas_total
  FROM categories c
  JOIN departments d ON d.id = c.department_id
)
SELECT *,
  CASE
    WHEN n_product_types = 0
     AND n_prod_override = 0
     AND n_prod_via_pt   = 0
     AND n_ventas_total  = 0
    THEN TRUE ELSE FALSE
  END AS puede_borrarse
FROM conteos
ORDER BY departamento, categoria
"""


# ============================================================================
# ENDPOINTS COMENTADOS (2026-06-20) — auditEmptyCategories está en api.ts
# pero ninguna página lo importa. La constante _SQL_AUDIT_EMPTY_CATEGORIES
# arriba sigue definida (es código muerto pero la query SQL queda preservada
# como referencia).
# ============================================================================

# @router.get("/categories/empty/audit")
# async def audit_empty_categories(db: AsyncSession = Depends(get_db)) -> dict:
#     """
#     Lista las categorias que NO tienen productos ni ventas historicas.
#     """
#     res = await db.execute(text(_SQL_AUDIT_EMPTY_CATEGORIES))
#     rows = [dict(r) for r in res.mappings().all()]
#     candidatos = [r for r in rows if r.get("puede_borrarse")]
#     no_candidatos = [r for r in rows if not r.get("puede_borrarse")]
#
#     return {
#         "ok": True,
#         "operation": "audit_empty_categories",
#         "timestamp": await _now(),
#         "report": {
#             "total_categorias":       len(rows),
#             "candidatas_a_borrar":    len(candidatos),
#             "categorias_con_datos":   len(no_candidatos),
#             "scope":                  "internal_db",
#         },
#         "candidatas":      candidatos,
#         "todas":           rows,
#     }


# @router.delete("/categories/empty")
# async def delete_empty_categories(
#     confirm: bool = False,
#     dry_run: bool = True,
#     db: AsyncSession = Depends(get_db)
#     ) -> dict:
#     """
#     Elimina TODAS las categorias 100% vacias (sin productos ni ventas).
#     """
#     res = await db.execute(text(_SQL_AUDIT_EMPTY_CATEGORIES))
#     rows = [dict(r) for r in res.mappings().all()]
#     candidatas = [r for r in rows if r.get("puede_borrarse")]
#
#     if not candidatas:
#         return {
#             "ok":        True,
#             "operation": "delete_empty_categories",
#             "timestamp": await _now(),
#             "report":    {"eliminadas": 0, "scope": "internal_db",
#                           "message": "No hay categorias 100% vacias"},
#             "candidatas": [],
#         }
#
#     if dry_run or not confirm:
#         return {
#             "ok":        True,
#             "operation": "delete_empty_categories_DRY_RUN",
#             "timestamp": await _now(),
#             "report": {
#                 "eliminadas":   0,
#                 "candidatas":   len(candidatas),
#                 "scope":        "internal_db",
#                 "message":      "DRY RUN — pasa ?dry_run=false&confirm=true para borrar",
#             },
#             "candidatas": candidatas,
#         }
#
#     ids = [r["cat_id"] for r in candidatas]
#     await db.execute(text("DELETE FROM categories WHERE id = ANY(:ids)"), {"ids": ids})
#     await db.commit()
#
#     # Can't easily get rowcount here without dialect-specific handling with SQLAlchemy in all cases,
#     # but length of candidatas is the affected rowcount.
#     rows_affected = len(ids)
#
#     return {
#         "ok":        True,
#         "operation": "delete_empty_categories",
#         "timestamp": await _now(),
#         "report": {
#             "eliminadas":      rows_affected,
#             "scope":           "internal_db",
#             "subcats_cascada": sum(int(r.get("n_subcats") or 0) for r in candidatas),
#         },
#         "eliminadas": candidatas,
#     }


# ---------------------------------------------------------------------------
# SUBCATEGORIES
# ---------------------------------------------------------------------------

@router.post("/subcategories", status_code=201)
async def create_subcategory(body: SubcategoryIn, db: AsyncSession = Depends(get_db)) -> dict:
    cat_res = await db.execute(text("""
        SELECT c.id, c.name AS cat_name, d.name AS dept_name
        FROM categories c JOIN departments d ON d.id = c.department_id
        WHERE c.id = :id
    """), {"id": body.category_id})
    cat_row = cat_res.mappings().first()
    if not cat_row:
        raise HTTPException(404, f"Categoria {body.category_id} no existe")
    cat = dict(cat_row)

    existing_res = await db.execute(
        text("SELECT id FROM subcategories WHERE category_id = :cid AND name = :name"),
        {"cid": body.category_id, "name": body.name}
    )
    if existing_res.scalar():
        raise HTTPException(
            409,
            f"Ya existe la subcategoria '{body.name}' dentro de '{cat['cat_name']}'",
        )

    slug = await _slugify(f"{cat['dept_name']}-{cat['cat_name']}-{body.name}")
    res = await db.execute(
        text("INSERT INTO subcategories (category_id, name, slug) VALUES (:cid, :name, :slug) RETURNING id"),
        {"cid": body.category_id, "name": body.name, "slug": slug}
    )
    await db.commit()
    new_id = res.scalar()

    entity_res = await db.execute(text("""
        SELECT s.id, s.name, s.slug, s.category_id,
               c.name AS category_name, d.name AS department_name
        FROM subcategories s
        JOIN categories c   ON c.id = s.category_id
        JOIN departments d  ON d.id = c.department_id
        WHERE s.id = :id
    """), {"id": new_id})
    entity = dict(entity_res.mappings().first())
    return await _report("create_subcategory", entity)


@router.patch("/subcategories/{sub_id}")
async def rename_subcategory(sub_id: int, body: RenameIn, db: AsyncSession = Depends(get_db)) -> dict:
    sub_res = await db.execute(text("""
        SELECT s.id, c.name AS cat_name, d.name AS dept_name
        FROM subcategories s
        JOIN categories c  ON c.id = s.category_id
        JOIN departments d ON d.id = c.department_id
        WHERE s.id = :id
    """), {"id": sub_id})
    sub_row = sub_res.mappings().first()
    if not sub_row:
        raise HTTPException(404, f"Subcategoria {sub_id} no existe")
    sub = dict(sub_row)

    slug = await _slugify(f"{sub['dept_name']}-{sub['cat_name']}-{body.name}")
    await db.execute(
        text("UPDATE subcategories SET name = :name, slug = :slug WHERE id = :id"),
        {"name": body.name, "slug": slug, "id": sub_id}
    )
    await db.commit()

    entity_res = await db.execute(text("""
        SELECT s.id, s.name, s.slug, s.category_id,
               c.name AS category_name, d.name AS department_name
        FROM subcategories s
        JOIN categories c  ON c.id = s.category_id
        JOIN departments d ON d.id = c.department_id
        WHERE s.id = :id
    """), {"id": sub_id})
    entity = dict(entity_res.mappings().first())
    return await _report("rename_subcategory", entity)


@router.delete("/subcategories/{sub_id}")
async def delete_subcategory(sub_id: int, force: bool = False, db: AsyncSession = Depends(get_db)) -> dict:
    sub_res = await db.execute(text("""
        SELECT s.id, s.name, c.name AS cat_name, d.name AS dept_name
        FROM subcategories s
        JOIN categories c  ON c.id = s.category_id
        JOIN departments d ON d.id = c.department_id
        WHERE s.id = :id
    """), {"id": sub_id})
    sub_row = sub_res.mappings().first()
    if not sub_row:
        raise HTTPException(404, f"Subcategoria {sub_id} no existe")
    sub = dict(sub_row)

    n_pt_res = await db.execute(text("SELECT COUNT(*) FROM product_types WHERE subcategory_id = :id"), {"id": sub_id})
    n_pt = n_pt_res.scalar() or 0
    n_prods_res = await db.execute(text("SELECT COUNT(*) FROM products WHERE subcategory_id = :id"), {"id": sub_id})
    n_prods = n_prods_res.scalar() or 0
    
    refs = n_pt + n_prods
    warnings = []
    if refs > 0 and not force:
        raise HTTPException(
            409,
            f"Subcategoria '{sub['name']}' está referenciada por {n_pt} product_types "
            f"y {n_prods} productos (override). Use ?force=true (la FK pondrá NULL "
            f"en el lado override; los product_types quedaran sin mapear)."
        )
    if refs > 0:
        warnings.append(
            f"FK ON DELETE SET NULL: {n_prods} productos perderán su override "
            f"y {n_pt} product_types quedarán sin mapear"
        )

    await db.execute(text("DELETE FROM subcategories WHERE id = :id"), {"id": sub_id})
    await db.commit()

    return await _report("delete_subcategory", sub, warnings=warnings)


# ============================================================================
# BOOTSTRAP / EXPORT — importar y exportar toda la taxonomía como JSON
# ============================================================================


class BootstrapBody(BaseModel):
    """Estructura anidada: {'Depto': {'Cat': {'Sub': []}}}."""
    taxonomy: dict


def _slugify_sync(text: str) -> str:
    """Slug síncrono (idéntico a harvester._slugify).
    Copiado para no importar del harvester y crear ciclos."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "sin-nombre"


@router.post("/bootstrap")
async def bootstrap_taxonomy(
    body: BootstrapBody,
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Importa un árbol de taxonomía JSON. Idempotente y conservador:

      - INSERTA lo que falta (no pisa lo que ya existe).
      - Los cambios manuales previos desde la UI se PRESERVAN.
      - Se puede correr múltiples veces con el mismo JSON — no duplica.

    Estructura del JSON:
        {
          "Departamento 1": {
            "Categoría A": { "Subcat X": [], "Subcat Y": [] },
            "Categoría B": { ... }
          },
          "Departamento 2": { ... }
        }

    Los valores dentro de subcategoría son `[]` (histórico — antes eran
    los product_types, hoy vienen de BSale al sync). La UI puede mandarlos
    vacíos.
    """
    taxonomy = body.taxonomy
    if not isinstance(taxonomy, dict) or not taxonomy:
        raise HTTPException(400, "Body 'taxonomy' debe ser un objeto no vacío.")

    cid = company.company_id
    stats = {
        "departments_total": 0, "categories_total": 0, "subcategories_total": 0,
        "departments_inserted": 0, "categories_inserted": 0, "subcategories_inserted": 0,
    }

    for depto, cats in taxonomy.items():
        if not isinstance(cats, dict):
            raise HTTPException(400, f"'{depto}' debe ser un objeto (categorías).")

        # INSERT department; si no hubo insert, SELECT
        res = await db.execute(
            text(
                "INSERT INTO departments (company_id, name, slug) VALUES (:c, :n, :s) "
                "ON CONFLICT (company_id, name) DO NOTHING RETURNING id"
            ),
            {"c": cid, "n": depto, "s": _slugify_sync(depto)},
        )
        row = res.first()
        if row:
            depto_id = row[0]
            stats["departments_inserted"] += 1
        else:
            res = await db.execute(
                text("SELECT id FROM departments WHERE company_id = :c AND name = :n"),
                {"c": cid, "n": depto},
            )
            depto_id = res.scalar_one()
        stats["departments_total"] += 1

        for cat, subs in cats.items():
            if not isinstance(subs, dict):
                raise HTTPException(400, f"'{depto} > {cat}' debe ser un objeto (subcategorías).")

            res = await db.execute(
                text(
                    "INSERT INTO categories (company_id, department_id, name, slug) "
                    "VALUES (:c, :d, :n, :s) "
                    "ON CONFLICT (company_id, department_id, name) DO NOTHING RETURNING id"
                ),
                {"c": cid, "d": depto_id, "n": cat, "s": _slugify_sync(f"{depto}-{cat}")},
            )
            row = res.first()
            if row:
                cat_id = row[0]
                stats["categories_inserted"] += 1
            else:
                res = await db.execute(
                    text(
                        "SELECT id FROM categories WHERE company_id = :c AND department_id = :d AND name = :n"
                    ),
                    {"c": cid, "d": depto_id, "n": cat},
                )
                cat_id = res.scalar_one()
            stats["categories_total"] += 1

            for sub in subs.keys():
                res = await db.execute(
                    text(
                        "INSERT INTO subcategories (company_id, category_id, name, slug) "
                        "VALUES (:c, :ca, :n, :s) "
                        "ON CONFLICT (company_id, category_id, name) DO NOTHING"
                    ),
                    {"c": cid, "ca": cat_id, "n": sub, "s": _slugify_sync(f"{depto}-{cat}-{sub}")},
                )
                if res.rowcount and res.rowcount > 0:
                    stats["subcategories_inserted"] += 1
                stats["subcategories_total"] += 1

    await db.commit()
    return await _report("bootstrap_taxonomy", entity=None, rows=stats["departments_inserted"]
                          + stats["categories_inserted"]
                          + stats["subcategories_inserted"],
                          warnings=[], extra={"stats": stats})


@router.get("/export")
async def export_taxonomy(
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Devuelve la taxonomía actual de la empresa como JSON anidado —
    mismo formato que acepta POST /taxonomy/bootstrap.

    Útil para:
      - Descargar el estado actual y versionar en el repo.
      - Copiar la taxonomía de una empresa a otra (pegar en la UI de otra).
    """
    cid = company.company_id
    res = await db.execute(
        text(
            """
            SELECT d.name AS dep, c.name AS cat, s.name AS sub
            FROM departments d
            LEFT JOIN categories    c ON c.department_id = d.id AND c.company_id = d.company_id
            LEFT JOIN subcategories s ON s.category_id   = c.id AND s.company_id = c.company_id
            WHERE d.company_id = :c
            ORDER BY d.name, c.name, s.name
            """
        ),
        {"c": cid},
    )
    tree: dict = {}
    for r in res.mappings().all():
        d = tree.setdefault(r["dep"], {})
        if r["cat"] is None:
            continue
        c = d.setdefault(r["cat"], {})
        if r["sub"] is not None:
            c[r["sub"]] = []
    return {"taxonomy": tree, "company_id": cid}
