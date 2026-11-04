"""
Endpoints que CREAN / MODIFICAN / ELIMINAN cosas en BSale y reflejan
el cambio en la BD interna.

Hoy BSale solo tiene un nivel jerarquico: `product_types`.
Departments / categories / subcategories existen unicamente en TU BD,
por lo cual NO se replican a BSale (eso lo hace taxonomy_admin.py).

Cada operacion devuelve un mini-informe JSON estandarizado:

    {
        "ok": true,
        "operation": "create_product_type",
        "timestamp": "...",
        "entity": {...},
        "report": {
            "rows_affected": 1,
            "scope": "bsale+internal",
            "warnings": []
        }
    }
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import logging
import os

from fastapi import Depends, APIRouter, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from pydantic import BaseModel, Field

from app.auth import (
    CurrentCompany,
    CurrentUser,
    get_current_company,
    get_current_user,
    require_admin,
)
from app.database import get_db
from app.events import log_event
from app.kawii_matrix import cache as matrix_cache
from harvester import bsale_client
from harvester.tenant_context import tenant_scope

logger = logging.getLogger(__name__)


async def _company_token(db: AsyncSession, cid: int) -> tuple[str, str]:
    """Obtiene (token, slug) de la empresa activa descifrando bsale_token."""
    key = os.environ.get("TOKEN_ENCRYPTION_KEY")
    if not key:
        raise HTTPException(500, "TOKEN_ENCRYPTION_KEY no configurada en el servidor")
    res = await db.execute(text("""
        SELECT slug, pgp_sym_decrypt(bsale_token, :key)::text AS token
        FROM companies
        WHERE id = :cid AND is_active = TRUE AND bsale_token IS NOT NULL
    """), {"cid": cid, "key": key})
    row = res.mappings().first()
    if not row or not row["token"]:
        raise HTTPException(400, f"No hay token BSale activo para la empresa {cid}")
    return row["token"], row["slug"]


# Estos endpoints crean / editan / borran en la cuenta BSale del cliente.
# Solo admin: error humano acá tiene consecuencias contables.
# get_current_company activa RLS para aislar por empresa.
router = APIRouter(
    prefix="/bsale",
    tags=["bsale-admin"],
    dependencies=[Depends(get_current_company), Depends(require_admin)],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _report(operation: str, entity: dict | None, rows: int = 1,
            scope: str = "bsale+internal",
            warnings: list[str] | None = None) -> dict:
    return {
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


async def _full_pt(pt_id: int, db: AsyncSession, cid: int) -> dict | None:
    """Devuelve el product_type con su mapeo a la taxonomia local."""
    res = await db.execute(text("""
        SELECT pt.bsale_product_type_id, pt.name, pt.is_active, pt.is_mapped,
               pt.subcategory_id,
               s.name AS subcategory,
               c.name AS category, c.id AS category_id,
               d.name AS department, d.id AS department_id
        FROM product_types pt
        LEFT JOIN subcategories s ON s.id = pt.subcategory_id AND s.company_id = pt.company_id
        LEFT JOIN categories    c ON c.id = s.category_id    AND c.company_id = pt.company_id
        LEFT JOIN departments   d ON d.id = c.department_id  AND d.company_id = pt.company_id
        WHERE pt.company_id = :cid AND pt.bsale_product_type_id = :pt_id
    """), {"pt_id": pt_id, "cid": cid})
    row = res.mappings().first()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------

class ProductTypeIn(BaseModel):
    """
    Crea un product_type en BSale Y lo deja mapeado a una subcategoria
    de mi taxonomia (opcional).
    """
    name: str = Field(..., min_length=1, max_length=160)
    subcategory_id: int | None = Field(
        None,
        description="Subcategoria local a la cual mapear este product_type. "
                    "Si es null, queda como 'sin mapear' (huerfano).",
    )


class ProductTypeUpdateIn(BaseModel):
    """
    Modifica un product_type existente. Todos los campos son opcionales,
    solo se aplica lo que venga distinto de None.
    """
    name: str | None = Field(None, min_length=1, max_length=160)
    subcategory_id: int | None = Field(
        None,
        description="Cambia el mapeo a otra subcategoria local. "
                    "Para eliminar el mapeo usa unmap=true en el query string.",
    )


# ---------------------------------------------------------------------------
# LIST (READ-ONLY)
# ---------------------------------------------------------------------------

@router.get("/product-types")
async def list_product_types(
    q: str | None = Query(None, description="Busqueda por nombre"),
    only_unmapped: bool = False,
    only_inactive: bool = False,
    only_empty: bool = Query(False, description="Solo tipos con 0 productos asociados."),
    only_with_products: bool = Query(False, description="Solo tipos con al menos 1 producto asociado."),
    limit: int = Query(500, ge=1, le=5000),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Lista product_types locales con su mapeo a la taxonomia.

    Util para el panel de administracion: muestra cuales estan mapeados,
    a que subcategoria, cuantos productos tienen (activos e inactivos), y si su
    nombre cumple la convencion 'Categoria / Subcategoria'.
    """
    where = ["pt.company_id = :cid"]
    params: dict = {"cid": company.company_id}

    if q:
        where.append("pt.name ILIKE :q")
        params["q"] = f"%{q}%"
    if only_unmapped:
        where.append("pt.is_mapped = FALSE")
    if only_inactive:
        where.append("pt.is_active = FALSE")
    if only_empty:
        where.append("COALESCE(pc.cnt, 0) = 0")
    if only_with_products:
        where.append("COALESCE(pc.cnt, 0) > 0")

    where_sql = "WHERE " + " AND ".join(where)
    params["limit"] = limit

    res = await db.execute(text(f"""
        WITH prod_counts AS (
            SELECT bsale_product_type_id,
                   COUNT(*) AS cnt,
                   COUNT(*) FILTER (WHERE is_active = TRUE)  AS active_cnt,
                   COUNT(*) FILTER (WHERE is_active = FALSE) AS inactive_cnt
            FROM products
            WHERE company_id = :cid AND bsale_product_type_id IS NOT NULL
            GROUP BY bsale_product_type_id
        )
        SELECT pt.bsale_product_type_id, pt.name, pt.is_active, pt.is_mapped,
               pt.subcategory_id, pt.synced_at,
               s.name AS subcategory,
               c.name AS category, c.id AS category_id,
               d.name AS department, d.id AS department_id,
               COALESCE(pc.cnt, 0)          AS productos,
               COALESCE(pc.active_cnt, 0)   AS productos_activos,
               COALESCE(pc.inactive_cnt, 0) AS productos_inactivos,
               CASE
                 WHEN pt.is_mapped AND s.name IS NOT NULL AND c.name IS NOT NULL
                 THEN c.name || ' / ' || s.name
                 ELSE NULL
               END AS expected_name,
               CASE
                 WHEN pt.is_mapped AND s.name IS NOT NULL AND c.name IS NOT NULL
                      AND pt.name = c.name || ' / ' || s.name
                 THEN TRUE ELSE FALSE
               END AS naming_ok
        FROM product_types pt
        LEFT JOIN subcategories s  ON s.id = pt.subcategory_id AND s.company_id = pt.company_id
        LEFT JOIN categories    c  ON c.id = s.category_id     AND c.company_id = pt.company_id
        LEFT JOIN departments   d  ON d.id = c.department_id   AND d.company_id = pt.company_id
        LEFT JOIN prod_counts   pc ON pc.bsale_product_type_id = pt.bsale_product_type_id
        {where_sql}
        ORDER BY pt.name
        LIMIT :limit
    """), params)

    rows = [dict(r) for r in res.mappings().all()]
    return {"total": len(rows), "items": rows}


async def _build_product_types_workspace(db: AsyncSession, cid: int) -> dict:
    """Construye las bandejas de trabajo para organizar tipos y categorías."""
    res = await db.execute(text("""
        WITH prod_counts AS (
            SELECT bsale_product_type_id,
                   COUNT(*) AS cnt,
                   COUNT(*) FILTER (WHERE is_active = TRUE)  AS active_cnt,
                   COUNT(*) FILTER (WHERE is_active = FALSE) AS inactive_cnt
            FROM products
            WHERE company_id = :cid AND bsale_product_type_id IS NOT NULL
            GROUP BY bsale_product_type_id
        )
        SELECT pt.bsale_product_type_id, pt.name, pt.is_active, pt.is_mapped,
               pt.subcategory_id, pt.synced_at,
               s.name AS subcategory,
               c.name AS category, c.id AS category_id,
               d.name AS department, d.id AS department_id,
               COALESCE(pc.cnt, 0)          AS productos,
               COALESCE(pc.active_cnt, 0)   AS productos_activos,
               COALESCE(pc.inactive_cnt, 0) AS productos_inactivos,
               CASE
                 WHEN pt.is_mapped AND s.name IS NOT NULL AND c.name IS NOT NULL
                 THEN c.name || ' / ' || s.name
                 ELSE NULL
               END AS expected_name,
               CASE
                 WHEN pt.is_mapped AND s.name IS NOT NULL AND c.name IS NOT NULL
                      AND pt.name = c.name || ' / ' || s.name
                 THEN TRUE ELSE FALSE
               END AS naming_ok
        FROM product_types pt
        LEFT JOIN subcategories s  ON s.id = pt.subcategory_id AND s.company_id = pt.company_id
        LEFT JOIN categories    c  ON c.id = s.category_id     AND c.company_id = pt.company_id
        LEFT JOIN departments   d  ON d.id = c.department_id   AND d.company_id = pt.company_id
        LEFT JOIN prod_counts   pc ON pc.bsale_product_type_id = pt.bsale_product_type_id
        WHERE pt.company_id = :cid
        ORDER BY pt.name
    """), {"cid": cid})
    all_pts = [dict(r) for r in res.mappings().all()]

    extra_counts_res = await db.execute(text("""
        SELECT
            (SELECT COUNT(*) FROM products WHERE company_id = :cid AND subcategory_id IS NOT NULL) AS overrides_cnt,
            (SELECT COUNT(*) FROM v_products_full WHERE company_id = :cid AND department IS NULL)  AS unclassified_cnt
    """), {"cid": cid})
    extra = extra_counts_res.mappings().first() or {}

    unmapped_with_products = sorted(
        [pt for pt in all_pts if not pt["is_mapped"] and pt["productos"] > 0],
        key=lambda x: (-x["productos_activos"], -x["productos"], x["name"]),
    )
    unmapped_empty = [
        pt for pt in all_pts if not pt["is_mapped"] and pt["productos"] == 0
    ]
    mapped = [pt for pt in all_pts if pt["is_mapped"]]
    mapped_empty_count = sum(1 for pt in mapped if pt["productos"] == 0)
    naming_mismatches_count = sum(1 for pt in mapped if not pt["naming_ok"])

    return {
        "summary": {
            "total_product_types": len(all_pts),
            "active_product_types": sum(1 for pt in all_pts if pt["is_active"]),
            "inactive_product_types": sum(1 for pt in all_pts if not pt["is_active"]),
            "mapped_count": len(mapped),
            "unmapped_with_products_count": len(unmapped_with_products),
            "unmapped_empty_count": len(unmapped_empty),
            "mapped_empty_count": mapped_empty_count,
            "naming_mismatches_count": naming_mismatches_count,
            "products_with_override_count": int(extra.get("overrides_cnt") or 0),
            "products_without_classification_count": int(extra.get("unclassified_cnt") or 0),
        },
        "unmapped_with_products": unmapped_with_products,
        "unmapped_empty": unmapped_empty,
        "mapped": mapped,
    }


@router.get("/product-types/workspace")
async def get_product_types_workspace(
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Vista completa de bandeja de trabajo de tipos de producto para el frontend.

    Separa en una sola llamada:
      - `unmapped_with_products`: tipos en BSale sin categoría asignada pero con productos.
      - `unmapped_empty`: tipos en BSale sin categoría y con 0 productos (listos para borrar).
      - `mapped`: tipos ya vinculados al árbol Departamento -> Categoría -> Subcategoría.
      - `summary`: contadores globales para badges/tarjetas.
    """
    return await _build_product_types_workspace(db, company.company_id)


@router.post("/product-types/pull")
async def pull_product_types_from_bsale(
    company: CurrentCompany = Depends(get_current_company),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Jala en vivo todos los `product_types` desde BSale (~1-2 segundos) y actualiza la BD.

    - NO pisa ningún mapeo manual (`subcategory_id`) que ya exista en nuestra BD.
    - Auto-mapea únicamente los tipos nuevos/sin mapear cuyo nombre coincida con
      'Categoría / Subcategoría' (o una subcategoría única).
    - Inserta tipos nuevos creados en BSale (incluso si aún tienen 0 productos) para
      que aparezcan de inmediato en la bandeja del frontend.
    - Marca `is_active = FALSE` en los tipos locales que hayan sido borrados/desactivados en BSale.
    - Devuelve las estadísticas del pull + el `workspace` actualizado.
    """
    cid = company.company_id
    token, slug = await _company_token(db, cid)

    def _fetch_all_pts() -> list[dict]:
        with tenant_scope(cid, token, slug):
            return bsale_client.paginate("/product_types.json")

    try:
        items = await asyncio.to_thread(_fetch_all_pts)
    except Exception as exc:
        raise HTTPException(502, f"Error consultando product_types en BSale: {exc}")

    # Cargar subcategorías para auto-resolver tipos nuevos que sigan la convención
    sub_res = await db.execute(text("""
        SELECT s.id, s.name AS sub_name, c.name AS cat_name
        FROM subcategories s
        JOIN categories c ON c.id = s.category_id AND c.company_id = s.company_id
        WHERE s.company_id = :cid
    """), {"cid": cid})
    resolver: dict[str, int] = {}
    bare_counts: dict[str, int] = {}
    bare_first_id: dict[str, int] = {}
    for r in sub_res.mappings().all():
        s_clean = (r["sub_name"] or "").strip().lower()
        c_clean = (r["cat_name"] or "").strip().lower()
        resolver[f"{c_clean} / {s_clean}"] = r["id"]
        if s_clean:
            bare_counts[s_clean] = bare_counts.get(s_clean, 0) + 1
            bare_first_id.setdefault(s_clean, r["id"])
    for s_clean, cnt in bare_counts.items():
        if cnt == 1:
            resolver.setdefault(s_clean, bare_first_id[s_clean])

    # Estado actual en nuestra BD
    existing_res = await db.execute(text("""
        SELECT bsale_product_type_id, name, subcategory_id, is_active, is_mapped
        FROM product_types
        WHERE company_id = :cid
    """), {"cid": cid})
    existing_map = {r["bsale_product_type_id"]: dict(r) for r in existing_res.mappings().all()}

    hist_res = await db.execute(text("""
        SELECT DISTINCT bsale_product_type_id
        FROM products
        WHERE company_id = :cid AND bsale_product_type_id IS NOT NULL
    """), {"cid": cid})
    pt_with_history = {r[0] for r in hist_res.fetchall()}

    new_inserted = 0
    updated_count = 0
    auto_mapped = 0
    active_bsale_ids: list[int] = []
    batch_pt_ids: list[int] = []
    batch_names: list[str] = []
    batch_sub_ids: list[int | None] = []
    batch_actives: list[bool] = []
    batch_mappeds: list[bool] = []

    for item in items:
        try:
            ptid = int(item.get("id") or 0)
        except (ValueError, TypeError):
            continue
        if ptid <= 0:
            continue

        is_active = str(item.get("state", "0")).strip() == "0"
        if is_active:
            active_bsale_ids.append(ptid)

        # Ignorar tipos inactivos en BSale que nunca existieron acá ni tienen productos
        if not is_active and ptid not in existing_map and ptid not in pt_with_history:
            continue

        raw_name = str(item.get("name") or "").strip() or "SIN CATEGORIA"
        prev = existing_map.get(ptid)
        sub_id = prev["subcategory_id"] if prev else None

        if sub_id is None:
            norm_name = " / ".join(p.strip() for p in raw_name.split("/")).lower() if "/" in raw_name else raw_name.lower()
            sub_id = resolver.get(norm_name) or resolver.get(raw_name.lower())
            if sub_id is None and "/" in raw_name:
                _, sub_only = raw_name.rsplit("/", 1)
                sub_id = resolver.get(sub_only.strip().lower())
            if sub_id is not None:
                auto_mapped += 1

        is_mapped = sub_id is not None
        batch_pt_ids.append(ptid)
        batch_names.append(raw_name)
        batch_sub_ids.append(sub_id)
        batch_actives.append(is_active)
        batch_mappeds.append(is_mapped)

        if prev is None:
            new_inserted += 1
        elif prev["name"] != raw_name or prev["is_active"] != is_active or prev["subcategory_id"] != sub_id:
            updated_count += 1

    if batch_pt_ids:
        await db.execute(text("""
            INSERT INTO product_types
                (company_id, bsale_product_type_id, name, subcategory_id, is_active, is_mapped, synced_at)
            SELECT :cid, u.pt_id, u.name, u.sub_id, u.is_active, u.is_mapped, NOW()
            FROM UNNEST(
                CAST(:pt_ids AS integer[]),
                CAST(:names AS text[]),
                CAST(:sub_ids AS integer[]),
                CAST(:actives AS boolean[]),
                CAST(:mappeds AS boolean[])
            ) AS u(pt_id, name, sub_id, is_active, is_mapped)
            ON CONFLICT (company_id, bsale_product_type_id) DO UPDATE SET
                name           = EXCLUDED.name,
                subcategory_id = COALESCE(product_types.subcategory_id, EXCLUDED.subcategory_id),
                is_active      = EXCLUDED.is_active,
                is_mapped      = (COALESCE(product_types.subcategory_id, EXCLUDED.subcategory_id) IS NOT NULL),
                synced_at      = NOW()
            WHERE (product_types.name, product_types.subcategory_id, product_types.is_active, product_types.is_mapped)
                  IS DISTINCT FROM
                  (EXCLUDED.name, COALESCE(product_types.subcategory_id, EXCLUDED.subcategory_id),
                   EXCLUDED.is_active, (COALESCE(product_types.subcategory_id, EXCLUDED.subcategory_id) IS NOT NULL))
        """), {
            "cid": cid,
            "pt_ids": batch_pt_ids,
            "names": batch_names,
            "sub_ids": batch_sub_ids,
            "actives": batch_actives,
            "mappeds": batch_mappeds,
        })

    # Desactivar localmente los tipos que ya no vienen activos en BSale
    deactivated_locally = 0
    if items:
        if active_bsale_ids:
            deact_res = await db.execute(text("""
                UPDATE product_types
                   SET is_active = FALSE, synced_at = NOW()
                 WHERE company_id = :cid
                   AND is_active = TRUE
                   AND NOT (bsale_product_type_id = ANY(:active_ids))
            """), {"cid": cid, "active_ids": active_bsale_ids})
        else:
            deact_res = await db.execute(text("""
                UPDATE product_types
                   SET is_active = FALSE, synced_at = NOW()
                 WHERE company_id = :cid AND is_active = TRUE
            """), {"cid": cid})
        deactivated_locally = deact_res.rowcount or 0

    workspace = await _build_product_types_workspace(db, cid)

    await log_event(
        db,
        company_id=cid,
        event_type="bsale.product_types_pulled",
        actor_user_id=user.id,
        payload={
            "fetched_from_bsale": len(items),
            "new_inserted": new_inserted,
            "updated": updated_count,
            "auto_mapped": auto_mapped,
            "deactivated_locally": deactivated_locally,
        },
        commit=False,
    )
    await db.commit()
    matrix_cache.invalidate(company_id=cid)

    return {
        "ok": True,
        "operation": "pull_product_types",
        "timestamp": await _now(),
        "stats": {
            "fetched_from_bsale": len(items),
            "new_inserted": new_inserted,
            "updated": updated_count,
            "auto_mapped": auto_mapped,
            "deactivated_locally": deactivated_locally,
            "unmapped_with_products": workspace["summary"]["unmapped_with_products_count"],
            "unmapped_empty": workspace["summary"]["unmapped_empty_count"],
        },
        "workspace": workspace,
    }


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------

@router.post("/product-types", status_code=201)
async def create_product_type(
    body: ProductTypeIn,
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    1) Crea el product_type en BSale (POST /v1/product_types.json).
    2) Lo inserta en la tabla local product_types.
    3) Lo deja mapeado a la subcategoria indicada (si se pasa).

    scope = "bsale+internal"
    """
    cid = company.company_id
    # Validar subcategoria local si se entrega
    sub_local = None
    if body.subcategory_id is not None:
        res_sub = await db.execute(
            text("SELECT id, name FROM subcategories WHERE company_id = :cid AND id = :id"),
            {"id": body.subcategory_id, "cid": cid},
        )
        row = res_sub.mappings().first()
        sub_local = dict(row) if row else None
        if not sub_local:
            raise HTTPException(
                404,
                f"Subcategoria {body.subcategory_id} no existe en mi BD. "
                f"Crea primero la taxonomia con /taxonomy/subcategories.",
            )

    token, slug = await _company_token(db, cid)

    def _create_remote() -> dict:
        with tenant_scope(cid, token, slug):
            return bsale_client.post("product_types.json", {"name": body.name})

    # 1) BSale
    try:
        resp = await asyncio.to_thread(_create_remote)
    except RuntimeError as exc:
        raise HTTPException(502, f"Error creando en BSale: {exc}")

    pt_id = resp.get("id")
    if not pt_id:
        raise HTTPException(502, f"BSale no devolvio id. Respuesta: {resp}")

    # 2) BD local (UPSERT idempotente multi-tenant)
    is_mapped = sub_local is not None
    await db.execute(text("""
        INSERT INTO product_types
            (company_id, bsale_product_type_id, name, subcategory_id, is_active, is_mapped, synced_at)
        VALUES (:cid, :pt_id, :name, :sub_id, TRUE, :is_mapped, NOW())
        ON CONFLICT (company_id, bsale_product_type_id) DO UPDATE SET
            name           = EXCLUDED.name,
            subcategory_id = EXCLUDED.subcategory_id,
            is_active      = EXCLUDED.is_active,
            is_mapped      = EXCLUDED.is_mapped,
            synced_at      = NOW()
    """), {
        "cid": cid,
        "pt_id": pt_id,
        "name": body.name,
        "sub_id": body.subcategory_id,
        "is_mapped": is_mapped,
    })
    entity = await _full_pt(pt_id, db, cid)
    await db.commit()
    matrix_cache.invalidate(company_id=cid)

    warnings = []
    if not is_mapped:
        warnings.append(
            "product_type creado SIN mapeo. Usa "
            "PATCH /bsale/product-types/{id} con subcategory_id para mapearlo."
        )

    return await _report("create_product_type", entity, warnings=warnings)


# ---------------------------------------------------------------------------
# UPDATE (rename y/o re-map)
# ---------------------------------------------------------------------------

@router.patch("/product-types/{pt_id}")
async def update_product_type(
    pt_id: int,
    body: ProductTypeUpdateIn,
    unmap: bool = False,
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Actualiza un product_type. Tres operaciones independientes:

      - Renombrar  -> body.name           (afecta BSale + BD)
      - Re-mapear  -> body.subcategory_id (afecta solo BD interna)
      - Des-mapear -> ?unmap=true         (afecta solo BD interna)

    Se pueden combinar en un mismo request.
    """
    cid = company.company_id
    pt = await _full_pt(pt_id, db, cid)
    if not pt:
        raise HTTPException(404, f"product_type {pt_id} no existe en mi BD")

    warnings: list[str] = []
    scopes: set[str] = set()

    # --- 1) Rename en BSale (si cambia el nombre) ---
    new_name = pt["name"]
    if body.name and body.name != pt["name"]:
        token, slug = await _company_token(db, cid)

        def _rename_remote() -> dict:
            with tenant_scope(cid, token, slug):
                return bsale_client.put(f"product_types/{pt_id}.json", {"id": pt_id, "name": body.name})

        try:
            await asyncio.to_thread(_rename_remote)
        except RuntimeError as exc:
            raise HTTPException(502, f"Error renombrando en BSale: {exc}")
        new_name = body.name
        scopes.add("bsale")

    # --- 2) Re-mapeo en BD local ---
    new_sub_id: int | None = pt["subcategory_id"]
    new_is_mapped: bool = pt["is_mapped"]

    if unmap:
        new_sub_id = None
        new_is_mapped = False
        scopes.add("internal")
    elif body.subcategory_id is not None and body.subcategory_id != pt["subcategory_id"]:
        res_sub = await db.execute(
            text("SELECT id FROM subcategories WHERE company_id = :cid AND id = :id"),
            {"id": body.subcategory_id, "cid": cid},
        )
        if not res_sub.scalar():
            raise HTTPException(404, f"Subcategoria {body.subcategory_id} no existe")
        new_sub_id = body.subcategory_id
        new_is_mapped = True
        scopes.add("internal")

    if not scopes:
        return await _report(
            "update_product_type", pt, rows=0, scope="noop",
            warnings=["No se entrego ningun cambio"],
        )

    await db.execute(text("""
        UPDATE product_types
           SET name           = :name,
               subcategory_id = :sub_id,
               is_mapped      = :is_mapped,
               synced_at      = NOW()
         WHERE company_id = :cid AND bsale_product_type_id = :pt_id
    """), {
        "cid": cid,
        "name": new_name,
        "sub_id": new_sub_id,
        "is_mapped": new_is_mapped,
        "pt_id": pt_id,
    })
    entity = await _full_pt(pt_id, db, cid)
    await db.commit()
    matrix_cache.invalidate(company_id=cid)

    scope = "bsale+internal" if "bsale" in scopes and "internal" in scopes \
        else ("bsale" if "bsale" in scopes else "internal_db")

    return await _report("update_product_type", entity, scope=scope, warnings=warnings)


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------

@router.delete("/product-types/{pt_id}")
async def delete_product_type(
    pt_id: int,
    force: bool = False,
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Elimina un product_type en BSale + BD interna.

    Por defecto: rechaza si tiene productos (409). Con ?force=true
    igual se elimina, pero los productos quedaran apuntando a un
    product_type fantasma (BSale tipicamente no permite borrar tipos
    con productos vivos, asi que ese caso suele fallar en la API).
    """
    cid = company.company_id
    pt = await _full_pt(pt_id, db, cid)
    if not pt:
        raise HTTPException(404, f"product_type {pt_id} no existe en mi BD")

    res_count = await db.execute(
        text("SELECT COUNT(*) FROM products WHERE company_id = :cid AND bsale_product_type_id = :pt_id"),
        {"pt_id": pt_id, "cid": cid},
    )
    n_prods = res_count.scalar() or 0

    warnings: list[str] = []
    if n_prods > 0 and not force:
        raise HTTPException(
            409,
            f"product_type '{pt['name']}' tiene {n_prods} productos. "
            f"Usa ?force=true para forzar (BSale rechazara si hay productos vivos).",
        )
    if n_prods > 0:
        warnings.append(
            f"Se intentara borrar con {n_prods} productos asociados; "
            f"BSale puede devolver 4xx."
        )

    token, slug = await _company_token(db, cid)

    def _delete_remote() -> dict:
        with tenant_scope(cid, token, slug):
            return bsale_client.delete(f"product_types/{pt_id}.json")

    # 1) BSale
    try:
        await asyncio.to_thread(_delete_remote)
    except RuntimeError as exc:
        raise HTTPException(502, f"Error eliminando en BSale: {exc}")

    # 2) BD local
    await db.execute(
        text("DELETE FROM product_types WHERE company_id = :cid AND bsale_product_type_id = :pt_id"),
        {"pt_id": pt_id, "cid": cid},
    )
    await db.commit()
    matrix_cache.invalidate(company_id=cid)

    return await _report(
        "delete_product_type",
        {"bsale_product_type_id": pt_id, "name": pt["name"]},
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# UTIL: resync puntual de un product_type desde BSale
# ---------------------------------------------------------------------------

@router.post("/product-types/{pt_id}/resync")
async def resync_product_type(
    pt_id: int,
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Trae el product_type desde BSale y reescribe la fila local
    (sin tocar el subcategory_id local). Util si alguien edito en BSale
    fuera de la app y queremos reflejarlo sin ejecutar el sync completo.
    """
    from harvester.config import BSALE_BASE_URL

    cid = company.company_id
    token, slug = await _company_token(db, cid)

    def _fetch_remote() -> dict:
        with tenant_scope(cid, token, slug):
            return bsale_client.fetch(f"{BSALE_BASE_URL}/product_types/{pt_id}.json")

    data = await asyncio.to_thread(_fetch_remote)
    if not data or "id" not in data:
        raise HTTPException(404, f"BSale no devolvio product_type {pt_id}")

    is_active = str(data.get("state", "0")).strip() == "0"

    await db.execute(text("""
        INSERT INTO product_types
            (company_id, bsale_product_type_id, name, is_active, is_mapped, synced_at)
        VALUES (:cid, :pt_id, :name, :is_active, FALSE, NOW())
        ON CONFLICT (company_id, bsale_product_type_id) DO UPDATE SET
            name      = EXCLUDED.name,
            is_active = EXCLUDED.is_active,
            synced_at = NOW()
    """), {"cid": cid, "pt_id": pt_id, "name": data.get("name", ""), "is_active": is_active})
    entity = await _full_pt(pt_id, db, cid)
    await db.commit()
    matrix_cache.invalidate(company_id=cid)

    return await _report("resync_product_type", entity, scope="bsale->internal")


# ---------------------------------------------------------------------------
# BULK MAP: mapear / desmapear múltiples product_types de golpe
# ---------------------------------------------------------------------------

class BulkMapProductTypesIn(BaseModel):
    product_type_ids: list[int] = Field(
        ...,
        min_length=1,
        description="Lista de bsale_product_type_id a mapear o desmapear.",
    )
    subcategory_id: int | None = Field(
        None,
        description="ID de la subcategoría local destino (obligatorio salvo que unmap=True).",
    )
    unmap: bool = Field(
        False,
        description="Si es True, quita el mapeo (subcategory_id=NULL, is_mapped=FALSE) a los tipos indicados.",
    )
    rename_in_bsale: bool = Field(
        False,
        description="Si es True y se mapea a una subcategoría, también renombra en BSale a 'Categoría / Subcategoría'.",
    )
    dry_run: bool = Field(
        False,
        description="Si es True, simula la operación sin escribir en BSale ni en BD.",
    )


@router.post("/product-types/bulk-map")
async def bulk_map_product_types(
    body: BulkMapProductTypesIn,
    company: CurrentCompany = Depends(get_current_company),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Mapea (o desmapea) múltiples `product_types` hacia una subcategoría local de un solo golpe."""
    cid = company.company_id
    unique_ids = list(dict.fromkeys(int(x) for x in body.product_type_ids if x and int(x) > 0))
    if not unique_ids:
        raise HTTPException(400, "product_type_ids no contiene IDs válidos")

    if not body.unmap and body.subcategory_id is None:
        raise HTTPException(400, "Debes enviar subcategory_id o activar unmap=true")

    target_sub = None
    expected_bsale_name = None
    if not body.unmap:
        sub_res = await db.execute(text("""
            SELECT s.id AS subcategory_id, s.name AS subcategory,
                   c.id AS category_id,    c.name AS category,
                   d.id AS department_id,  d.name AS department
            FROM subcategories s
            JOIN categories  c ON c.id = s.category_id   AND c.company_id = s.company_id
            JOIN departments d ON d.id = c.department_id AND d.company_id = c.company_id
            WHERE s.company_id = :cid AND s.id = :sub_id
        """), {"cid": cid, "sub_id": body.subcategory_id})
        sub_row = sub_res.mappings().first()
        if not sub_row:
            raise HTTPException(404, f"Subcategoría {body.subcategory_id} no existe en esta empresa")
        target_sub = dict(sub_row)
        expected_bsale_name = f"{target_sub['category']} / {target_sub['subcategory']}"

    pts_res = await db.execute(text("""
        SELECT bsale_product_type_id, name, subcategory_id, is_active, is_mapped
        FROM product_types
        WHERE company_id = :cid AND bsale_product_type_id = ANY(:ids)
    """), {"cid": cid, "ids": unique_ids})
    found_pts = {r["bsale_product_type_id"]: dict(r) for r in pts_res.mappings().all()}
    not_found = [pid for pid in unique_ids if pid not in found_pts]

    candidates = [found_pts[pid] for pid in unique_ids if pid in found_pts]

    if body.dry_run:
        return {
            "ok": True,
            "operation": "bulk_map_product_types",
            "dry_run": True,
            "timestamp": await _now(),
            "target_subcategory": target_sub,
            "totals": {
                "received": len(unique_ids),
                "to_update": len(candidates),
                "not_found": len(not_found),
            },
            "to_update": [
                {
                    "bsale_product_type_id": pt["bsale_product_type_id"],
                    "current_name": pt["name"],
                    "new_name": expected_bsale_name if (body.rename_in_bsale and not body.unmap) else pt["name"],
                    "old_subcategory_id": pt["subcategory_id"],
                    "new_subcategory_id": None if body.unmap else body.subcategory_id,
                }
                for pt in candidates
            ],
            "not_found": not_found,
        }

    if not candidates:
        return {
            "ok": True,
            "operation": "bulk_map_product_types",
            "dry_run": False,
            "timestamp": await _now(),
            "target_subcategory": target_sub,
            "totals": {"received": len(unique_ids), "updated": 0, "rename_failed": 0, "not_found": len(not_found)},
            "updated": [],
            "rename_failed": [],
            "not_found": not_found,
        }

    rename_failed: list[dict] = []
    renamed_ids: set[int] = set()

    if body.rename_in_bsale and not body.unmap and expected_bsale_name:
        token, slug = await _company_token(db, cid)
        to_rename = [pt for pt in candidates if pt["name"] != expected_bsale_name]

        def _rename_batch(items_to_rename: list[dict]):
            ok_ids: set[int] = set()
            errs: list[dict] = []

            def _put_one(pt_item: dict):
                ptid = pt_item["bsale_product_type_id"]
                bsale_client.put(f"product_types/{ptid}.json", {"id": ptid, "name": expected_bsale_name})
                return ptid

            with tenant_scope(cid, token, slug):
                with ThreadPoolExecutor(max_workers=min(8, max(1, len(items_to_rename)))) as pool:
                    futs = {pool.submit(_put_one, it): it for it in items_to_rename}
                    for fut in as_completed(futs):
                        it = futs[fut]
                        try:
                            ok_ids.add(fut.result())
                        except Exception as exc:
                            errs.append({
                                "bsale_product_type_id": it["bsale_product_type_id"],
                                "current_name": it["name"],
                                "target_name": expected_bsale_name,
                                "error": str(exc),
                            })
            return ok_ids, errs

        if to_rename:
            renamed_ids, rename_failed = await asyncio.to_thread(_rename_batch, to_rename)

    found_ids = [pt["bsale_product_type_id"] for pt in candidates]
    new_sub_id = None if body.unmap else body.subcategory_id
    new_is_mapped = not body.unmap

    await db.execute(text("""
        UPDATE product_types
           SET subcategory_id = :sub_id,
               is_mapped      = :is_mapped,
               name           = CASE
                                    WHEN bsale_product_type_id = ANY(:renamed_ids) THEN :expected_name
                                    ELSE name
                                END,
               synced_at      = NOW()
         WHERE company_id = :cid
           AND bsale_product_type_id = ANY(:ids)
    """), {
        "cid": cid,
        "sub_id": new_sub_id,
        "is_mapped": new_is_mapped,
        "renamed_ids": list(renamed_ids),
        "expected_name": expected_bsale_name or "",
        "ids": found_ids,
    })
    await log_event(
        db,
        company_id=cid,
        event_type="bsale.product_types_bulk_mapped",
        actor_user_id=user.id,
        payload={
            "updated_count": len(found_ids),
            "subcategory_id": new_sub_id,
            "unmap": body.unmap,
            "renamed_in_bsale": len(renamed_ids),
            "rename_failed": len(rename_failed),
        },
        commit=False,
    )
    await db.commit()
    matrix_cache.invalidate(company_id=cid)

    return {
        "ok": True,
        "operation": "bulk_map_product_types",
        "dry_run": False,
        "timestamp": await _now(),
        "target_subcategory": target_sub,
        "totals": {
            "received": len(unique_ids),
            "updated": len(found_ids),
            "renamed_in_bsale": len(renamed_ids),
            "rename_failed": len(rename_failed),
            "not_found": len(not_found),
        },
        "updated_ids": found_ids,
        "rename_failed": rename_failed,
        "not_found": not_found,
    }


# ---------------------------------------------------------------------------
# BULK DELETE: eliminar múltiples product_types vacíos en BSale + BD
# ---------------------------------------------------------------------------

class BulkDeleteProductTypesIn(BaseModel):
    product_type_ids: list[int] | None = Field(
        None,
        description="Lista de bsale_product_type_id a eliminar.",
    )
    delete_all_empty_unmapped: bool = Field(
        False,
        description="Si es True, selecciona automáticamente todos los product_types sin mapear y con 0 productos.",
    )
    dry_run: bool = Field(
        False,
        description="Si es True, simula y devuelve qué tipos se eliminarían sin tocar BSale ni la BD.",
    )


@router.post("/product-types/bulk-delete")
async def bulk_delete_product_types(
    body: BulkDeleteProductTypesIn,
    company: CurrentCompany = Depends(get_current_company),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Elimina en lote `product_types` vacíos en BSale (`DELETE /v1/product_types/{id}.json`) y en la BD local.

    Protección automática:
      - Bloquea cualquier `product_type` que todavía tenga productos asociados en la BD
        (activos o inactivos), informando cuántos tiene y sugiriendo usar `/bsale/product-types/merge`.
    """
    cid = company.company_id
    explicit_ids = list(dict.fromkeys(int(x) for x in (body.product_type_ids or []) if x and int(x) > 0))

    if not explicit_ids and not body.delete_all_empty_unmapped:
        raise HTTPException(
            400,
            "Debes enviar product_type_ids o activar delete_all_empty_unmapped=true",
        )

    if body.delete_all_empty_unmapped:
        auto_res = await db.execute(text("""
            SELECT pt.bsale_product_type_id
            FROM product_types pt
            LEFT JOIN products p
                   ON p.bsale_product_type_id = pt.bsale_product_type_id
                  AND p.company_id = pt.company_id
            WHERE pt.company_id = :cid
              AND pt.is_mapped = FALSE
            GROUP BY pt.bsale_product_type_id
            HAVING COUNT(p.bsale_product_id) = 0
            ORDER BY pt.bsale_product_type_id
        """), {"cid": cid})
        auto_ids = [r[0] for r in auto_res.fetchall()]
        target_ids = list(dict.fromkeys(explicit_ids + auto_ids))
    else:
        target_ids = explicit_ids

    if not target_ids:
        return {
            "ok": True,
            "operation": "bulk_delete_product_types",
            "dry_run": body.dry_run,
            "timestamp": await _now(),
            "totals": {
                "requested": 0,
                "deleted": 0,
                "blocked_with_products": 0,
                "failed": 0,
                "not_found": 0,
            },
            "deleted": [],
            "blocked_with_products": [],
            "failed": [],
            "not_found": [],
        }

    res = await db.execute(text("""
        WITH prod_counts AS (
            SELECT bsale_product_type_id,
                   COUNT(*) AS cnt,
                   COUNT(*) FILTER (WHERE is_active = TRUE)  AS active_cnt,
                   COUNT(*) FILTER (WHERE is_active = FALSE) AS inactive_cnt
            FROM products
            WHERE company_id = :cid AND bsale_product_type_id = ANY(:ids)
            GROUP BY bsale_product_type_id
        )
        SELECT pt.bsale_product_type_id, pt.name, pt.is_active, pt.is_mapped,
               COALESCE(pc.cnt, 0)          AS productos,
               COALESCE(pc.active_cnt, 0)   AS productos_activos,
               COALESCE(pc.inactive_cnt, 0) AS productos_inactivos
        FROM product_types pt
        LEFT JOIN prod_counts pc ON pc.bsale_product_type_id = pt.bsale_product_type_id
        WHERE pt.company_id = :cid
          AND pt.bsale_product_type_id = ANY(:ids)
        ORDER BY pt.name
    """), {"cid": cid, "ids": target_ids})
    found_rows = [dict(r) for r in res.mappings().all()]
    found_ids_set = {r["bsale_product_type_id"] for r in found_rows}
    not_found = [pid for pid in target_ids if pid not in found_ids_set]

    to_delete: list[dict] = []
    blocked_with_products: list[dict] = []

    for row in found_rows:
        if row["productos"] > 0:
            blocked_with_products.append({
                "bsale_product_type_id": row["bsale_product_type_id"],
                "name": row["name"],
                "productos": row["productos"],
                "productos_activos": row["productos_activos"],
                "productos_inactivos": row["productos_inactivos"],
                "reason": (
                    f"Tiene {row['productos']} productos ({row['productos_activos']} activos, "
                    f"{row['productos_inactivos']} inactivos). Usa POST /bsale/product-types/merge "
                    f"para moverlos a otro tipo antes de eliminar."
                ),
            })
        else:
            to_delete.append(row)

    if body.dry_run:
        return {
            "ok": True,
            "operation": "bulk_delete_product_types",
            "dry_run": True,
            "timestamp": await _now(),
            "totals": {
                "requested": len(target_ids),
                "to_delete": len(to_delete),
                "blocked_with_products": len(blocked_with_products),
                "not_found": len(not_found),
            },
            "to_delete": to_delete,
            "blocked_with_products": blocked_with_products,
            "not_found": not_found,
        }

    if not to_delete:
        return {
            "ok": True,
            "operation": "bulk_delete_product_types",
            "dry_run": False,
            "timestamp": await _now(),
            "totals": {
                "requested": len(target_ids),
                "deleted": 0,
                "blocked_with_products": len(blocked_with_products),
                "failed": 0,
                "not_found": len(not_found),
            },
            "deleted": [],
            "blocked_with_products": blocked_with_products,
            "failed": [],
            "not_found": not_found,
        }

    token, slug = await _company_token(db, cid)

    def _delete_pts_in_bsale(items_to_del: list[dict]):
        deleted_items: list[dict] = []
        failed_items: list[dict] = []

        def _del_one(it: dict):
            ptid = it["bsale_product_type_id"]
            if it.get("is_active", True):
                bsale_client.delete(f"product_types/{ptid}.json")
            else:
                # Si ya figuraba inactivo localmente, intentamos borrar en BSale pero toleramos si ya estaba borrado
                try:
                    bsale_client.delete(f"product_types/{ptid}.json")
                except Exception:
                    pass
            return it

        with tenant_scope(cid, token, slug):
            with ThreadPoolExecutor(max_workers=min(8, max(1, len(items_to_del)))) as pool:
                futs = {pool.submit(_del_one, item): item for item in items_to_del}
                for fut in as_completed(futs):
                    item = futs[fut]
                    try:
                        fut.result()
                        deleted_items.append({
                            "bsale_product_type_id": item["bsale_product_type_id"],
                            "name": item["name"],
                        })
                    except Exception as exc:
                        failed_items.append({
                            "bsale_product_type_id": item["bsale_product_type_id"],
                            "name": item["name"],
                            "error": str(exc),
                        })
        return deleted_items, failed_items

    deleted, failed = await asyncio.to_thread(_delete_pts_in_bsale, to_delete)
    deleted_ids = [d["bsale_product_type_id"] for d in deleted]

    if deleted_ids:
        await db.execute(text("""
            DELETE FROM product_types
             WHERE company_id = :cid
               AND bsale_product_type_id = ANY(:ids)
        """), {"cid": cid, "ids": deleted_ids})
        await log_event(
            db,
            company_id=cid,
            event_type="bsale.product_types_bulk_deleted",
            actor_user_id=user.id,
            payload={
                "deleted_count": len(deleted_ids),
                "deleted_ids": deleted_ids,
                "blocked_count": len(blocked_with_products),
                "failed_count": len(failed),
            },
            commit=False,
        )
        await db.commit()
        matrix_cache.invalidate(company_id=cid)

    return {
        "ok": True,
        "operation": "bulk_delete_product_types",
        "dry_run": False,
        "timestamp": await _now(),
        "totals": {
            "requested": len(target_ids),
            "deleted": len(deleted),
            "blocked_with_products": len(blocked_with_products),
            "failed": len(failed),
            "not_found": len(not_found),
        },
        "deleted": deleted,
        "blocked_with_products": blocked_with_products,
        "failed": failed,
        "not_found": not_found,
    }


# ---------------------------------------------------------------------------
# MERGE: fusionar uno o varios product_types viejos dentro de uno destino
# ---------------------------------------------------------------------------

class MergeProductTypesIn(BaseModel):
    source_product_type_ids: list[int] = Field(
        ...,
        min_length=1,
        description="Lista de bsale_product_type_id origen cuyos productos se moverán al destino.",
    )
    target_product_type_id: int = Field(
        ...,
        description="bsale_product_type_id destino que recibirá todos los productos.",
    )
    delete_sources_after: bool = Field(
        True,
        description="Si es True (por defecto), elimina en BSale y en BD los tipos origen que queden con 0 productos.",
    )
    copy_mapping_if_target_unmapped: bool = Field(
        True,
        description="Si el destino no está mapeado y algún origen sí tenía subcategoría, hereda ese mapeo.",
    )
    clear_product_overrides: bool = Field(
        True,
        description="Si es True (por defecto), limpia products.subcategory_id de los productos movidos.",
    )
    dry_run: bool = Field(
        False,
        description="Si es True, simula toda la fusión sin modificar BSale ni la BD.",
    )


@router.post("/product-types/merge")
async def merge_product_types(
    body: MergeProductTypesIn,
    company: CurrentCompany = Depends(get_current_company),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Fusiona uno o varios `product_types` origen dentro de `target_product_type_id`.

    1. Mueve todos los productos (activos e inactivos) de los tipos origen al tipo destino
       en BSale (`PUT /v1/products/{id}.json` en paralelo) y en la BD local.
    2. Si `delete_sources_after=True`, elimina en BSale y en la BD local cada tipo origen
       que haya quedado con 0 productos (y 0 errores).
    """
    cid = company.company_id
    target_id = int(body.target_product_type_id)
    source_ids = [
        int(x) for x in dict.fromkeys(body.source_product_type_ids)
        if x and int(x) > 0 and int(x) != target_id
    ]
    if not source_ids:
        raise HTTPException(
            400,
            "source_product_type_ids debe contener al menos un ID distinto a target_product_type_id",
        )

    target_pt = await _full_pt(target_id, db, cid)
    if not target_pt:
        raise HTTPException(404, f"target_product_type_id {target_id} no existe en la BD")
    if not target_pt["is_active"]:
        raise HTTPException(400, f"El tipo destino '{target_pt['name']}' ({target_id}) está inactivo")

    sources_res = await db.execute(text("""
        SELECT bsale_product_type_id, name, subcategory_id, is_active, is_mapped
        FROM product_types
        WHERE company_id = :cid AND bsale_product_type_id = ANY(:ids)
    """), {"cid": cid, "ids": source_ids})
    found_sources = {r["bsale_product_type_id"]: dict(r) for r in sources_res.mappings().all()}
    sources_not_found = [sid for sid in source_ids if sid not in found_sources]
    if not found_sources:
        raise HTTPException(404, f"Ninguno de los source_product_type_ids existe: {sources_not_found}")

    valid_source_ids = list(found_sources.keys())

    # Buscar TODOS los productos (activos e inactivos) asociados a los tipos origen
    prods_res = await db.execute(text("""
        SELECT bsale_product_id, name, is_active, bsale_product_type_id, subcategory_id
        FROM products
        WHERE company_id = :cid
          AND bsale_product_type_id = ANY(:source_ids)
        ORDER BY bsale_product_id
    """), {"cid": cid, "source_ids": valid_source_ids})
    products_to_move = [dict(r) for r in prods_res.mappings().all()]

    if body.dry_run:
        return {
            "ok": True,
            "operation": "merge_product_types",
            "dry_run": True,
            "timestamp": await _now(),
            "target": target_pt,
            "sources": list(found_sources.values()),
            "sources_not_found": sources_not_found,
            "totals": {
                "sources_count": len(valid_source_ids),
                "products_to_move": len(products_to_move),
                "active_products_to_move": sum(1 for p in products_to_move if p["is_active"]),
                "inactive_products_to_move": sum(1 for p in products_to_move if not p["is_active"]),
                "will_delete_sources": body.delete_sources_after,
            },
            "sample_products": products_to_move[:25],
        }

    token, slug = await _company_token(db, cid)

    # 1. Mover productos en BSale en paralelo
    def _move_products_in_bsale(prods: list[dict]):
        moved: list[dict] = []
        failed: list[dict] = []

        def _put_prod(p: dict):
            pid = p["bsale_product_id"]
            resp = bsale_client.put(f"products/{pid}.json", {"id": pid, "productTypeId": target_id})
            if not resp or "id" not in resp:
                raise RuntimeError(f"BSale no confirmó actualización del producto {pid}")
            return p

        with tenant_scope(cid, token, slug):
            with ThreadPoolExecutor(max_workers=min(12, max(1, len(prods)))) as pool:
                futs = {pool.submit(_put_prod, prod): prod for prod in prods}
                for fut in as_completed(futs):
                    prod = futs[fut]
                    try:
                        fut.result()
                        moved.append({
                            "bsale_product_id": prod["bsale_product_id"],
                            "name": prod["name"],
                            "from_product_type_id": prod["bsale_product_type_id"],
                            "to_product_type_id": target_id,
                        })
                    except Exception as exc:
                        logger.error(
                            "Error moviendo producto %s a product_type %s en BSale: %s",
                            prod["bsale_product_id"], target_id, exc,
                        )
                        failed.append({
                            "bsale_product_id": prod["bsale_product_id"],
                            "name": prod["name"],
                            "from_product_type_id": prod["bsale_product_type_id"],
                            "error": str(exc),
                        })
        return moved, failed

    moved_products: list[dict] = []
    failed_products: list[dict] = []
    if products_to_move:
        moved_products, failed_products = await asyncio.to_thread(
            _move_products_in_bsale, products_to_move
        )

    moved_pids = [m["bsale_product_id"] for m in moved_products]
    if moved_pids:
        await db.execute(text("""
            UPDATE products
               SET bsale_product_type_id = :target_id,
                   subcategory_id = CASE WHEN :clear_ov THEN NULL ELSE subcategory_id END,
                   synced_at = NOW()
             WHERE company_id = :cid
               AND bsale_product_id = ANY(:pids)
        """), {
            "cid": cid,
            "target_id": target_id,
            "clear_ov": body.clear_product_overrides,
            "pids": moved_pids,
        })

    # Si el destino no estaba mapeado y algún origen tenía mapeo, heredarlo opcionalmente
    if body.copy_mapping_if_target_unmapped and not target_pt["is_mapped"]:
        donor_sub_id = next(
            (s["subcategory_id"] for s in found_sources.values() if s.get("subcategory_id") is not None),
            None,
        )
        if donor_sub_id is not None:
            await db.execute(text("""
                UPDATE product_types
                   SET subcategory_id = :sub_id, is_mapped = TRUE, synced_at = NOW()
                 WHERE company_id = :cid AND bsale_product_type_id = :target_id
            """), {"cid": cid, "sub_id": donor_sub_id, "target_id": target_id})

    # 2. Eliminar tipos origen que quedaron con 0 productos si delete_sources_after=True
    sources_deleted: list[dict] = []
    sources_kept: list[dict] = []

    if body.delete_sources_after:
        rem_res = await db.execute(text("""
            SELECT bsale_product_type_id, COUNT(*) AS rem
            FROM products
            WHERE company_id = :cid AND bsale_product_type_id = ANY(:source_ids)
            GROUP BY bsale_product_type_id
        """), {"cid": cid, "source_ids": valid_source_ids})
        remaining_map = {r["bsale_product_type_id"]: int(r["rem"]) for r in rem_res.mappings().all()}

        empty_sources = []
        for sid in valid_source_ids:
            rem = remaining_map.get(sid, 0)
            if rem > 0:
                sources_kept.append({
                    "bsale_product_type_id": sid,
                    "name": found_sources[sid]["name"],
                    "reason": f"Aún tiene {rem} productos que no pudieron moverse.",
                })
            else:
                empty_sources.append(found_sources[sid])

        if empty_sources:
            def _del_empty_sources(srcs: list[dict]):
                del_ok: list[dict] = []
                del_fail: list[dict] = []
                with tenant_scope(cid, token, slug):
                    for s in srcs:
                        sid = s["bsale_product_type_id"]
                        try:
                            if s.get("is_active", True):
                                bsale_client.delete(f"product_types/{sid}.json")
                            else:
                                try:
                                    bsale_client.delete(f"product_types/{sid}.json")
                                except Exception:
                                    pass
                            del_ok.append({"bsale_product_type_id": sid, "name": s["name"]})
                        except Exception as exc:
                            del_fail.append({
                                "bsale_product_type_id": sid,
                                "name": s["name"],
                                "reason": f"Productos movidos OK, pero BSale rechazó borrar el tipo vacío: {exc}",
                            })
                return del_ok, del_fail

            del_ok, del_fail = await asyncio.to_thread(_del_empty_sources, empty_sources)
            sources_deleted.extend(del_ok)
            sources_kept.extend(del_fail)

            if del_ok:
                del_ids = [d["bsale_product_type_id"] for d in del_ok]
                await db.execute(text("""
                    DELETE FROM product_types
                     WHERE company_id = :cid
                       AND bsale_product_type_id = ANY(:ids)
                """), {"cid": cid, "ids": del_ids})

    updated_target = await _full_pt(target_id, db, cid)

    await log_event(
        db,
        company_id=cid,
        event_type="bsale.product_types_merged",
        actor_user_id=user.id,
        payload={
            "source_product_type_ids": valid_source_ids,
            "target_product_type_id": target_id,
            "products_moved": len(moved_products),
            "products_failed": len(failed_products),
            "sources_deleted": [d["bsale_product_type_id"] for d in sources_deleted],
        },
        commit=False,
    )
    await db.commit()
    matrix_cache.invalidate(company_id=cid)

    return {
        "ok": True,
        "operation": "merge_product_types",
        "dry_run": False,
        "timestamp": await _now(),
        "target": updated_target,
        "totals": {
            "sources_requested": len(valid_source_ids),
            "products_found": len(products_to_move),
            "products_moved": len(moved_products),
            "products_failed": len(failed_products),
            "sources_deleted": len(sources_deleted),
            "sources_kept": len(sources_kept),
        },
        "moved_products": moved_products,
        "failed_products": failed_products,
        "sources_deleted": sources_deleted,
        "sources_kept": sources_kept,
        "sources_not_found": sources_not_found,
    }
