"""
Endpoints de AUDITORIA del catalogo.

Detectan inconsistencias entre tu BD interna y BSale:

  - product_types con nombre que no cumple "Categoria / Subcategoria"
  - product_types huerfanos (sin subcategoria) que tienen productos
  - product_types inactivos pero con mapeo activo
  - subcategorias sin ningun product_type apuntando a ellas
  - categorias sin subcategorias
  - departamentos sin categorias
  - product_types con nombre duplicado (mismo name, distinto bsale_id)

Cada bloque devuelve la lista de filas afectadas + un conteo total,
asi el frontend puede mostrar tarjetas/contadores y permitir auto-fix.

Tambien expone POST /audits/fix-naming para renombrar en BSale + BD
todos los product_types mal nombrados (o un subset por ids).
"""

from datetime import datetime, timezone

from fastapi import Depends, APIRouter, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from pydantic import BaseModel, Field

from app.auth import CurrentCompany, get_current_company, require_admin
from app.database import get_db
from harvester import bsale_client
from harvester.tenant_context import set_current_tenant


# Auditorías: cualquier usuario logueado en una empresa puede ver inconsistencias.
# El auto-fix (POST /fix-naming) renombra en BSale y se restringe a admin.
router = APIRouter(
    prefix="/audits",
    tags=["audits"],
    dependencies=[Depends(get_current_company)],
)


# ---------------------------------------------------------------------------
# METADATA por tipo de issue
# ---------------------------------------------------------------------------
ISSUES_META: dict[str, dict] = {
    "naming_mismatches": {
        "source": "both",
        "where": "BSale (nombre actual) + BD local (nombre esperado)",
        "what": (
            "Hay product_types cuyo nombre en BSale NO sigue la convención "
            "'Categoría / Subcategoría' que define tu taxonomía local."
        ),
        "impact": (
            "Los reportes que parsean el nombre del PT pueden romper o caer en "
            "categorías equivocadas. La integración cross-sistema se desincroniza."
        ),
        "fix_hint": (
            "Usa el botón 'Corregir nombres' arriba: renombra en BSale por API "
            "y actualiza tu BD en una sola operación."
        ),
    },
    "orphan_product_types_with_products": {
        "source": "local_db",
        "where": "BD local (mapeo faltante en product_types.subcategory_id)",
        "what": "product_types con productos pero sin subcategoría asignada.",
        "impact": "Los reportes ignoran esos productos porque no tienen jerarquía.",
        "fix_hint": "Mapear cada PT desde /bsale/product-types.",
    },
    "inactive_but_mapped": {
        "source": "bsale",
        "where": "BSale (state=1) + BD local (is_mapped=true)",
        "what": "PT desactivado en BSale sigue con mapeo activo acá.",
        "impact": "Sincronía perdida.",
        "fix_hint": "Reactivar en BSale o desmapear en local.",
    },
    "subcategories_without_product_type": {
        "source": "local_db",
        "where": "BD local (subcategorías huérfanas)",
        "what": "Subcategorías sin ningún product_type apuntando a ellas.",
        "impact": "Nada llega ahí salvo con override manual.",
        "fix_hint": "Borrar la subcategoría o mapear un PT.",
    },
    "categories_without_subcategories": {
        "source": "local_db",
        "where": "BD local",
        "what": "Categorías sin subcategorías dentro.",
        "impact": "Nivel intermedio vacío en el árbol.",
        "fix_hint": "Crear subcategorías o borrar la categoría.",
    },
    "departments_without_categories": {
        "source": "local_db",
        "where": "BD local",
        "what": "Departamentos sin categorías.",
        "impact": "Rama vacía en el árbol.",
        "fix_hint": "Crear categorías o borrar el departamento.",
    },
    "duplicate_product_type_names": {
        "source": "bsale",
        "where": "BSale (mismos nombres, distintos ids)",
        "what": "PTs con el mismo nombre pero distinto bsale_id.",
        "impact": "Confusión en mapeo.",
        "fix_hint": "Unificar en BSale o renombrar.",
    },
    "products_without_classification": {
        "source": "local_db",
        "where": "BD local (v_products_full con department=NULL)",
        "what": "Productos sin departamento (ni por PT ni por override).",
        "impact": "No aparecen en reportes agregados.",
        "fix_hint": "Crear el PT mapeo o override individual del producto.",
    },
}


# ---------------------------------------------------------------------------
# DETECCION — cada helper recibe cid (company_id) y filtra por él.
# ---------------------------------------------------------------------------

async def _audit_naming_mismatches(db: AsyncSession, cid: int) -> list[dict]:
    res = await db.execute(text("""
        SELECT pt.bsale_product_type_id AS id,
               pt.name AS current_name,
               c.name || ' / ' || s.name AS expected_name,
               s.name AS subcategory, c.name AS category, d.name AS department,
               pt.subcategory_id,
               (SELECT COUNT(*) FROM products p
                 WHERE p.company_id = pt.company_id
                   AND p.bsale_product_type_id = pt.bsale_product_type_id) AS productos
        FROM product_types pt
        JOIN subcategories s ON s.id = pt.subcategory_id AND s.company_id = pt.company_id
        JOIN categories    c ON c.id = s.category_id     AND c.company_id = s.company_id
        JOIN departments   d ON d.id = c.department_id   AND d.company_id = c.company_id
        WHERE pt.company_id = :cid
          AND pt.is_mapped = TRUE
          AND pt.name <> c.name || ' / ' || s.name
        ORDER BY d.name, c.name, s.name
    """), {"cid": cid})
    return [dict(r) for r in res.mappings().all()]


async def _audit_orphan_pts_with_products(db: AsyncSession, cid: int) -> list[dict]:
    res = await db.execute(text("""
        SELECT pt.bsale_product_type_id AS id, pt.name,
               COUNT(p.bsale_product_id) AS productos
        FROM product_types pt
        LEFT JOIN products p ON p.bsale_product_type_id = pt.bsale_product_type_id
                            AND p.company_id = pt.company_id
        WHERE pt.company_id = :cid
          AND NOT pt.is_mapped
        GROUP BY 1, 2
        HAVING COUNT(p.bsale_product_id) > 0
        ORDER BY 3 DESC
    """), {"cid": cid})
    return [dict(r) for r in res.mappings().all()]


async def _audit_inactive_pts_mapped(db: AsyncSession, cid: int) -> list[dict]:
    res = await db.execute(text("""
        SELECT pt.bsale_product_type_id AS id, pt.name,
               s.name AS subcategory,
               c.name AS category, d.name AS department,
               (SELECT COUNT(*) FROM products p
                 WHERE p.company_id = pt.company_id
                   AND p.bsale_product_type_id = pt.bsale_product_type_id) AS productos,
               pt.synced_at AS ultimo_sync
        FROM product_types pt
        LEFT JOIN subcategories s ON s.id = pt.subcategory_id AND s.company_id = pt.company_id
        LEFT JOIN categories    c ON c.id = s.category_id     AND c.company_id = s.company_id
        LEFT JOIN departments   d ON d.id = c.department_id   AND d.company_id = c.company_id
        WHERE pt.company_id = :cid
          AND pt.is_active = FALSE
          AND pt.is_mapped = TRUE
        ORDER BY pt.name
    """), {"cid": cid})
    return [dict(r) for r in res.mappings().all()]


async def _audit_subs_without_pt(db: AsyncSession, cid: int) -> list[dict]:
    res = await db.execute(text("""
        SELECT s.id, s.name AS subcategory,
               c.name AS category, d.name AS department,
               (SELECT COUNT(*) FROM products p
                 WHERE p.company_id = s.company_id AND p.subcategory_id = s.id)
                   AS productos_override
        FROM subcategories s
        JOIN categories    c ON c.id = s.category_id   AND c.company_id = s.company_id
        JOIN departments   d ON d.id = c.department_id AND d.company_id = c.company_id
        LEFT JOIN product_types pt ON pt.subcategory_id = s.id AND pt.company_id = s.company_id
        WHERE s.company_id = :cid
          AND pt.bsale_product_type_id IS NULL
        ORDER BY d.name, c.name, s.name
    """), {"cid": cid})
    return [dict(r) for r in res.mappings().all()]


async def _audit_cats_without_subs(db: AsyncSession, cid: int) -> list[dict]:
    res = await db.execute(text("""
        SELECT c.id, c.name AS category, d.name AS department
        FROM categories c
        JOIN departments d ON d.id = c.department_id AND d.company_id = c.company_id
        LEFT JOIN subcategories s ON s.category_id = c.id AND s.company_id = c.company_id
        WHERE c.company_id = :cid
        GROUP BY c.id, c.name, d.name
        HAVING COUNT(s.id) = 0
        ORDER BY d.name, c.name
    """), {"cid": cid})
    return [dict(r) for r in res.mappings().all()]


async def _audit_depts_without_cats(db: AsyncSession, cid: int) -> list[dict]:
    res = await db.execute(text("""
        SELECT d.id, d.name AS department
        FROM departments d
        LEFT JOIN categories c ON c.department_id = d.id AND c.company_id = d.company_id
        WHERE d.company_id = :cid
        GROUP BY d.id, d.name
        HAVING COUNT(c.id) = 0
        ORDER BY d.name
    """), {"cid": cid})
    return [dict(r) for r in res.mappings().all()]


async def _audit_duplicate_pt_names(db: AsyncSession, cid: int) -> list[dict]:
    res = await db.execute(text("""
        SELECT name, COUNT(*) AS count,
               ARRAY_AGG(bsale_product_type_id ORDER BY bsale_product_type_id) AS ids
        FROM product_types
        WHERE company_id = :cid
        GROUP BY name
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC, name
    """), {"cid": cid})
    return [dict(r) for r in res.mappings().all()]


async def _audit_products_without_classification(db: AsyncSession, cid: int) -> int:
    return await db.scalar(text("""
        SELECT COUNT(*) FROM v_products_full
        WHERE company_id = :cid AND department IS NULL
    """), {"cid": cid}) or 0


async def _audit_products_without_classification_sample(
    db: AsyncSession, cid: int, limit: int = 50,
) -> list[dict]:
    res = await db.execute(text("""
        SELECT v.bsale_product_id AS id,
               v.product_name AS producto,
               v.bsale_product_type_id AS bsale_pt_id,
               pt.name AS bsale_pt_name,
               pt.is_active AS bsale_pt_active,
               pt.is_mapped AS local_mapped
          FROM v_products_full v
          LEFT JOIN product_types pt
            ON pt.bsale_product_type_id = v.bsale_product_type_id
           AND pt.company_id = v.company_id
         WHERE v.company_id = :cid AND v.department IS NULL
         ORDER BY pt.name NULLS LAST, v.product_name
         LIMIT :lim
    """), {"lim": limit, "cid": cid})
    return [dict(r) for r in res.mappings().all()]


# ---------------------------------------------------------------------------
# ENDPOINT PRINCIPAL
# ---------------------------------------------------------------------------

@router.get("")
async def run_audits(
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Ejecuta todas las auditorias y devuelve un resumen estructurado."""
    cid = company.company_id
    naming      = await _audit_naming_mismatches(db, cid)
    orphans     = await _audit_orphan_pts_with_products(db, cid)
    inactive    = await _audit_inactive_pts_mapped(db, cid)
    subs_empty  = await _audit_subs_without_pt(db, cid)
    cats_empty  = await _audit_cats_without_subs(db, cid)
    depts_empty = await _audit_depts_without_cats(db, cid)
    dup         = await _audit_duplicate_pt_names(db, cid)
    sin_clasif  = await _audit_products_without_classification(db, cid)
    sin_clasif_sample = (
        await _audit_products_without_classification_sample(db, cid)
        if sin_clasif > 0 else []
    )

    severity = "ok"
    if naming or orphans or inactive or dup or sin_clasif > 0:
        severity = "warning"
    if sin_clasif > 50 or len(orphans) > 10:
        severity = "critical"

    side_counts = {"bsale": 0, "local_db": 0, "both": 0}
    for key, count in {
        "naming_mismatches":                  len(naming),
        "orphan_product_types_with_products": len(orphans),
        "inactive_but_mapped":                len(inactive),
        "subcategories_without_product_type": len(subs_empty),
        "categories_without_subcategories":   len(cats_empty),
        "departments_without_categories":     len(depts_empty),
        "duplicate_product_type_names":       len(dup),
        "products_without_classification":    sin_clasif,
    }.items():
        if count <= 0:
            continue
        src = ISSUES_META.get(key, {}).get("source", "both")
        side_counts[src] = side_counts.get(src, 0) + count

    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "severity": severity,
        "summary": {
            "naming_mismatches":                   len(naming),
            "orphan_product_types_with_products":  len(orphans),
            "inactive_but_mapped":                 len(inactive),
            "subcategories_without_product_type":  len(subs_empty),
            "categories_without_subcategories":    len(cats_empty),
            "departments_without_categories":      len(depts_empty),
            "duplicate_product_type_names":        len(dup),
            "products_without_classification":     sin_clasif,
        },
        "side_counts": side_counts,
        "meta": ISSUES_META,
        "issues": {
            "naming_mismatches":                   naming,
            "orphan_product_types_with_products":  orphans,
            "inactive_but_mapped":                 inactive,
            "subcategories_without_product_type":  subs_empty,
            "categories_without_subcategories":    cats_empty,
            "departments_without_categories":      depts_empty,
            "duplicate_product_type_names":        dup,
            "products_without_classification":     sin_clasif_sample,
        },
    }


# ---------------------------------------------------------------------------
# AUTO-FIX: nombres
# ---------------------------------------------------------------------------

class FixNamingIn(BaseModel):
    ids: list[int] | None = Field(
        None,
        description="Lista de bsale_product_type_id a renombrar. "
                    "Si es null, renombra TODOS los detectados.",
    )
    dry_run: bool = Field(
        False,
        description="Si es True, solo simula y devuelve la lista de cambios "
                    "sin tocar BSale ni la BD.",
    )


@router.post("/fix-naming", dependencies=[Depends(require_admin)])
async def fix_naming(
    body: FixNamingIn | None = None,
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Renombra product_types en BSale + BD interna para que cumplan la convención."""
    cid = company.company_id
    payload    = body or FixNamingIn()
    candidates = await _audit_naming_mismatches(db, cid)

    if payload.ids is not None:
        wanted     = set(payload.ids)
        candidates = [c for c in candidates if c["id"] in wanted]

    # Para hablar con BSale necesitamos el token de la empresa activa.
    # El backend descifra el token y lo activa via tenant_context.
    # Cargar el token de la empresa
    tok_row = await db.execute(text("""
        SELECT slug, pgp_sym_decrypt(bsale_token, :key)::text AS token
        FROM companies WHERE id = :cid
    """), {"cid": cid, "key": _token_key()})
    tk = tok_row.mappings().first()
    if not tk or not tk["token"]:
        raise HTTPException(500, f"No hay token BSale cargado para company {cid}")
    set_current_tenant(cid, tk["token"], tk["slug"])

    fixed:   list[dict] = []
    failed:  list[dict] = []
    skipped: list[dict] = []

    for c in candidates:
        pt_id    = c["id"]
        new_name = c["expected_name"]
        old_name = c["current_name"]

        if payload.dry_run:
            skipped.append({"id": pt_id, "from": old_name, "to": new_name,
                            "reason": "dry_run"})
            continue

        try:
            bsale_client.put(f"product_types/{pt_id}.json", {"name": new_name})
        except Exception as exc:
            failed.append({"id": pt_id, "from": old_name, "to": new_name,
                           "step": "bsale", "error": str(exc)})
            continue

        try:
            await db.execute(text("""
                UPDATE product_types
                   SET name = :name, synced_at = NOW()
                 WHERE company_id = :cid AND bsale_product_type_id = :pt_id
            """), {"name": new_name, "pt_id": pt_id, "cid": cid})
            await db.commit()
        except Exception as exc:
            await db.rollback()
            failed.append({"id": pt_id, "from": old_name, "to": new_name,
                           "step": "internal_db", "error": str(exc)})
            continue

        fixed.append({"id": pt_id, "from": old_name, "to": new_name})

    return {
        "ok": True,
        "operation": "fix_naming",
        "dry_run": payload.dry_run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "candidates": len(candidates),
            "fixed":      len(fixed),
            "failed":     len(failed),
            "skipped":    len(skipped),
        },
        "fixed":   fixed,
        "failed":  failed,
        "skipped": skipped,
        "scope": "bsale+internal" if not payload.dry_run else "noop",
    }


def _token_key() -> str:
    import os
    k = os.environ.get("TOKEN_ENCRYPTION_KEY")
    if not k:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY no está en .env")
    return k


# ---------------------------------------------------------------------------
# AUTO-FIX: limpiar product_types huerfanos sin productos
# ---------------------------------------------------------------------------

@router.get("/orphans-without-products")
async def orphans_without_products(
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """product_types sin mapeo y sin productos: candidatos seguros a borrar."""
    cid = company.company_id
    res = await db.execute(text("""
        SELECT pt.bsale_product_type_id AS id, pt.name, pt.is_active
        FROM product_types pt
        LEFT JOIN products p ON p.bsale_product_type_id = pt.bsale_product_type_id
                            AND p.company_id = pt.company_id
        WHERE pt.company_id = :cid AND NOT pt.is_mapped
        GROUP BY pt.bsale_product_type_id, pt.name, pt.is_active
        HAVING COUNT(p.bsale_product_id) = 0
        ORDER BY pt.name
    """), {"cid": cid})
    return [dict(r) for r in res.mappings().all()]
