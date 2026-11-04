"""Endpoints de productos y variantes.

Usa la vista `v_products_full` que aplica override por producto:
- Si products.subcategory_id está seteado, usa ese
- Si no, hereda del product_type (BSale)
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import logging
import os
from pydantic import BaseModel, Field

from fastapi import Depends, APIRouter, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.auth import (
    CurrentCompany,
    CurrentUser,
    get_current_company,
    get_current_user,
    require_admin,
    require_operador_or_admin,
)
from app.database import get_db
from app.events import log_event
from app.kawii_matrix import cache as matrix_cache
from harvester import bsale_client
from harvester.tenant_context import tenant_scope

logger = logging.getLogger(__name__)

# Productos / variantes. Lectura para cualquier logueado (con empresa activa);
# el override de subcategoría (PATCH /{id}/subcategory) cambia clasificación
# de un SKU y se restringe a operador/admin en la decoración del endpoint.
router = APIRouter(
    prefix="/products",
    tags=["products"],
    dependencies=[Depends(get_current_company)],
)


@router.get("")
async def list_products(
    q: str | None = Query(
        None,
        description="Busqueda por nombre, bsale_product_id (numerico exacto) "
                    "o SKU/code de cualquier variante (ILIKE).",
    ),
    department: str | None = None,
    category: str | None = None,
    subcategory: str | None = None,
    mapped_only: bool = False,
    override_only: bool = Query(
        False, description="Solo productos con override individual de subcategoría."
    ),
    unmapped_only: bool = Query(
        False, description="Solo productos cuya clasificación final está sin asignar."
    ),
    product_type_id: int | None = Query(
        None, description="Filtra por bsale_product_type_id (útil para drill-down desde huérfanos)."
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    cid = company.company_id
    where = ["v.company_id = :cid"]
    params: dict = {"cid": cid}

    if q:
        # Busqueda inteligente: nombre, ID exacto si es numerico, SKU de variante.
        clauses = ["v.product_name ILIKE :q"]
        params["q"] = f"%{q}%"
        if q.strip().isdigit():
            clauses.append("v.bsale_product_id = :qid")
            params["qid"] = int(q.strip())
        clauses.append("""EXISTS (
            SELECT 1 FROM variants va
             WHERE va.bsale_product_id = v.bsale_product_id
               AND va.company_id = v.company_id
               AND va.code ILIKE :q
        )""")
        where.append("(" + " OR ".join(clauses) + ")")

    if department:
        where.append("v.department = :department")
        params["department"] = department
    if category:
        where.append("v.category = :category")
        params["category"] = category
    if subcategory:
        where.append("v.subcategory = :subcategory")
        params["subcategory"] = subcategory
    if mapped_only:
        where.append("v.is_mapped = TRUE")
    if override_only:
        where.append("v.has_override = TRUE")
    if unmapped_only:
        where.append("v.subcategory IS NULL")
    if product_type_id is not None:
        where.append("v.bsale_product_type_id = :product_type_id")
        params["product_type_id"] = product_type_id

    where_sql = "WHERE " + " AND ".join(where)

    sql_count = f"""
        SELECT COUNT(*)
        FROM v_products_full v
        {where_sql}
    """
    res_total = await db.execute(text(sql_count), params)
    total = res_total.scalar() or 0

    params["limit"] = limit
    params["offset"] = offset
    sql = f"""
        SELECT v.bsale_product_id, v.product_name AS name, v.is_active,
               v.bsale_product_type_id, v.product_type_name,
               v.subcategory, v.category, v.department,
               v.has_override,
               (SELECT STRING_AGG(va.code, ', ' ORDER BY va.code)
                  FROM (
                       SELECT code FROM variants
                        WHERE bsale_product_id = v.bsale_product_id
                          AND company_id = v.company_id
                          AND code IS NOT NULL AND code <> ''
                        ORDER BY code
                        LIMIT 3
    ) va
               ) AS skus,
               (SELECT COUNT(*) FROM variants
                 WHERE bsale_product_id = v.bsale_product_id
                   AND company_id = v.company_id) AS variantes_count
        FROM v_products_full v
        {where_sql}
        ORDER BY v.product_name
        LIMIT :limit OFFSET :offset
    """
    res = await db.execute(text(sql), params)
    rows = [dict(r) for r in res.mappings().all()]

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": rows,
    }


@router.get("/{product_id}")
async def get_product(
    product_id: int,
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    cid = company.company_id
    res_prod = await db.execute(text("""
        SELECT v.bsale_product_id, v.product_name AS name, v.is_active,
               v.bsale_product_type_id, v.product_type_name,
               v.subcategory, v.category, v.department, v.has_override
        FROM v_products_full v
        WHERE v.company_id = :cid AND v.bsale_product_id = :product_id
    """), {"product_id": product_id, "cid": cid})
    prod_row = res_prod.mappings().first()

    if not prod_row:
        raise HTTPException(404, "Producto no encontrado")

    prod = dict(prod_row)

    res_var = await db.execute(text("""
        SELECT va.bsale_variant_id, va.code, va.description,
               vc.effective_cost, vc.average_cost, vc.latest_cost, vc.cost_source
        FROM variants va
        LEFT JOIN variant_costs vc
               ON vc.bsale_variant_id = va.bsale_variant_id AND vc.company_id = va.company_id
        WHERE va.company_id = :cid AND va.bsale_product_id = :product_id
        ORDER BY va.bsale_variant_id
    """), {"product_id": product_id, "cid": cid})
    prod["variantes"] = [dict(r) for r in res_var.mappings().all()]

    res_stock = await db.execute(text("""
        SELECT o.name AS sucursal, SUM(sl.quantity_available) AS unidades
        FROM stock_levels sl
        JOIN offices o  ON o.bsale_office_id = sl.bsale_office_id AND o.company_id = sl.company_id
        JOIN variants v ON v.bsale_variant_id = sl.bsale_variant_id AND v.company_id = sl.company_id
        WHERE sl.company_id = :cid AND v.bsale_product_id = :product_id
        GROUP BY o.name
        ORDER BY o.name
    """), {"product_id": product_id, "cid": cid})
    prod["stock_por_sucursal"] = [dict(r) for r in res_stock.mappings().all()]

    return prod


@router.patch("/{product_id}/subcategory", dependencies=[Depends(require_operador_or_admin)])
async def set_product_subcategory(
    product_id: int,
    subcategory_id: int | None = None,
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Override individual: asigna una subcategoría específica a UN producto.
    Si subcategory_id es null, se elimina el override y vuelve a heredar del product_type.
    """
    cid = company.company_id
    # Validar que el producto exista EN ESTA EMPRESA
    res_exists = await db.execute(text(
        "SELECT 1 FROM products WHERE company_id = :cid AND bsale_product_id = :product_id"
    ), {"product_id": product_id, "cid": cid})
    if not res_exists.scalar():
        raise HTTPException(404, f"Producto {product_id} no existe en esta empresa")

    # Validar subcategoría si se provee (debe pertenecer a la misma empresa)
    if subcategory_id is not None:
        res_subcat = await db.execute(text(
            "SELECT id, name FROM subcategories WHERE company_id = :cid AND id = :subcategory_id"
        ), {"subcategory_id": subcategory_id, "cid": cid})
        subcat = res_subcat.mappings().first()
        if not subcat:
            raise HTTPException(404, f"Subcategoria {subcategory_id} no existe en esta empresa")

    await db.execute(text(
        "UPDATE products SET subcategory_id = :subcategory_id "
        "WHERE company_id = :cid AND bsale_product_id = :product_id"
    ), {"subcategory_id": subcategory_id, "product_id": product_id, "cid": cid})
    await db.commit()
    matrix_cache.invalidate(company_id=cid)

    res_entity = await db.execute(text("""
        SELECT bsale_product_id, product_name AS name,
               department, category, subcategory, has_override
        FROM v_products_full
        WHERE company_id = :cid AND bsale_product_id = :product_id
    """), {"product_id": product_id, "cid": cid})
    return dict(res_entity.mappings().first())


@router.get("/stats/summary")
async def products_summary(
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Resumen global de la empresa activa."""
    cid = company.company_id
    p = {"cid": cid}
    counts_res = await db.execute(text("""
        SELECT
            (SELECT COUNT(*) FROM products WHERE company_id = :cid) AS tot_prod,
            (SELECT COUNT(*) FROM variants WHERE company_id = :cid) AS tot_var,
            (SELECT COUNT(*) FROM product_types WHERE company_id = :cid) AS pt_tot,
            (SELECT COUNT(*) FROM product_types WHERE company_id = :cid AND is_mapped = TRUE) AS pt_map,
            (SELECT COUNT(*) FROM product_types WHERE company_id = :cid AND is_mapped = FALSE) AS pt_unmap,
            (SELECT COUNT(*) FROM products WHERE company_id = :cid AND subcategory_id IS NOT NULL) AS prod_over
    """), p)
    counts = counts_res.mappings().first() or {}

    res_huerfanos = await db.execute(text("""
            SELECT pt.bsale_product_type_id AS id, pt.name,
                   COUNT(p.bsale_product_id) AS productos
            FROM product_types pt
            LEFT JOIN products p ON p.bsale_product_type_id = pt.bsale_product_type_id
                                AND p.company_id = pt.company_id
            WHERE pt.company_id = :cid
              AND NOT pt.is_mapped
              AND p.subcategory_id IS NULL
            GROUP BY 1, 2
            HAVING COUNT(p.bsale_product_id) > 0
            ORDER BY 3 DESC
        """), p)

    return {
        "total_productos":             int(counts.get("tot_prod") or 0),
        "total_variantes":             int(counts.get("tot_var") or 0),
        "product_types_total":         int(counts.get("pt_tot") or 0),
        "product_types_mapeados":      int(counts.get("pt_map") or 0),
        "product_types_sin_mapear":    int(counts.get("pt_unmap") or 0),
        "productos_con_override":      int(counts.get("prod_over") or 0),
        "productos_huerfanos":         [dict(r) for r in res_huerfanos.mappings().all()]
    }


# ---------------------------------------------------------------------------
# DESACTIVACIÓN MASIVA DE PRODUCTOS / VARIANTES (BSale + BD Interna)
# ---------------------------------------------------------------------------

class BulkDeactivateIn(BaseModel):
    skus: list[str] = Field(
        ...,
        description="Lista de SKUs, códigos de barra o códigos de visualización a desactivar.",
        min_length=1,
    )
    dry_run: bool = Field(
        False,
        description="Si es True, solo analiza y simula sin modificar BSale ni la base de datos.",
    )
    forzar_con_stock: bool = Field(
        False,
        description="Si es False (por defecto), bloquea variantes con stock > 0. Si es True, desactiva aun con stock disponible.",
    )


def _token_key() -> str:
    k = os.environ.get("TOKEN_ENCRYPTION_KEY")
    if not k:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY no está configurado en .env")
    return k


@router.post("/bulk-deactivate", dependencies=[Depends(require_admin)])
async def bulk_deactivate_products(
    body: BulkDeactivateIn,
    company: CurrentCompany = Depends(get_current_company),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Desactiva masivamente variantes en BSale (DELETE /v1/variants/{id}.json)
    y en la base de datos interna.

    - Protege automáticamente contra desactivar productos con stock físico en sucursales
      a menos que se envíe `forzar_con_stock: true`.
    - Si todas las variantes de un producto padre quedan inactivas, el producto
      también se desactiva automáticamente.
    - Soporta `dry_run: true` para previsualizar el impacto antes de aplicar.
    - Exige rol 'admin'.
    """
    cid = company.company_id
    clean_skus = [s.strip() for s in body.skus if s and s.strip()]
    if not clean_skus:
        raise HTTPException(400, "La lista de SKUs no contiene valores válidos")

    # Preservar orden y eliminar duplicados en la lista enviada
    unique_skus = list(dict.fromkeys(clean_skus))

    # 1. Buscar en BD local las variantes que coincidan por code, bar_code o display_code
    sql_variants = """
        SELECT 
            v.bsale_variant_id,
            v.bsale_product_id,
            v.code,
            v.bar_code,
            v.display_code,
            v.description,
            v.is_active,
            p.name AS product_name,
            COALESCE(SUM(sl.quantity_available), 0) AS total_stock
        FROM variants v
        JOIN products p 
          ON p.bsale_product_id = v.bsale_product_id AND p.company_id = v.company_id
        LEFT JOIN stock_levels sl 
          ON sl.bsale_variant_id = v.bsale_variant_id AND sl.company_id = v.company_id
        WHERE v.company_id = :cid
          AND (
              v.code = ANY(:skus)
              OR v.bar_code = ANY(:skus)
              OR v.display_code = ANY(:skus)
          )
        GROUP BY 
            v.bsale_variant_id,
            v.bsale_product_id,
            v.code,
            v.bar_code,
            v.display_code,
            v.description,
            v.is_active,
            p.name
    """
    res = await db.execute(text(sql_variants), {"cid": cid, "skus": unique_skus})
    matched_rows = [dict(r) for r in res.mappings().all()]

    found_skus = set()
    to_deactivate: list[dict] = []
    blocked_by_stock: list[dict] = []
    already_inactive: list[dict] = []

    for var in matched_rows:
        # Registrar cuáles SKUs de la lista hicieron match con esta variante
        for s in unique_skus:
            if (
                (var.get("code") and var["code"].strip() == s)
                or (var.get("bar_code") and var["bar_code"].strip() == s)
                or (var.get("display_code") and var["display_code"].strip() == s)
            ):
                found_skus.add(s)

        stock = float(var["total_stock"] or 0)
        item_info = {
            "bsale_variant_id": var["bsale_variant_id"],
            "bsale_product_id": var["bsale_product_id"],
            "code": var["code"],
            "bar_code": var["bar_code"],
            "display_code": var["display_code"],
            "product_name": var["product_name"],
            "stock": stock,
        }

        if not var["is_active"]:
            item_info["reason"] = "Ya se encuentra inactivo en la base de datos"
            already_inactive.append(item_info)
        elif stock > 0 and not body.forzar_con_stock:
            item_info["reason"] = f"Tiene {stock:g} unidades de stock disponible. Usa forzar_con_stock=true para desactivar."
            blocked_by_stock.append(item_info)
        else:
            to_deactivate.append(item_info)

    not_found = [s for s in unique_skus if s not in found_skus]

    # 2. Si es simulación (dry_run), responder de inmediato sin tocar nada
    if body.dry_run:
        parents_to_deactivate_count = 0
        if to_deactivate:
            to_deact_pids = list({x["bsale_product_id"] for x in to_deactivate})
            to_deact_vids = [x["bsale_variant_id"] for x in to_deactivate]
            res_sim_parents = await db.execute(text("""
                SELECT p.bsale_product_id
                FROM products p
                WHERE p.company_id = :cid
                  AND p.bsale_product_id = ANY(:pids)
                  AND NOT EXISTS (
                      SELECT 1 FROM variants v
                       WHERE v.company_id = p.company_id
                         AND v.bsale_product_id = p.bsale_product_id
                         AND v.is_active = TRUE
                         AND v.bsale_variant_id != ALL(:vids)
                  )
            """), {"cid": cid, "pids": to_deact_pids, "vids": to_deact_vids})
            parents_to_deactivate_count = len(res_sim_parents.fetchall())

        return {
            "ok": True,
            "dry_run": True,
            "forzar_con_stock": body.forzar_con_stock,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "totals": {
                "received": len(unique_skus),
                "to_deactivate": len(to_deactivate),
                "parent_products_to_deactivate": parents_to_deactivate_count,
                "blocked_by_stock": len(blocked_by_stock),
                "already_inactive": len(already_inactive),
                "not_found": len(not_found),
            },
            "to_deactivate": to_deactivate,
            "blocked_by_stock": blocked_by_stock,
            "already_inactive": already_inactive,
            "not_found": not_found,
        }

    # 3. Si no hay nada que desactivar (todos eran no encontrados, ya inactivos o bloqueados por stock)
    if not to_deactivate:
        return {
            "ok": True,
            "dry_run": False,
            "forzar_con_stock": body.forzar_con_stock,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "totals": {
                "received": len(unique_skus),
                "deactivated": 0,
                "failed": 0,
                "blocked_by_stock": len(blocked_by_stock),
                "already_inactive": len(already_inactive),
                "not_found": len(not_found),
            },
            "deactivated": [],
            "failed": [],
            "blocked_by_stock": blocked_by_stock,
            "already_inactive": already_inactive,
            "not_found": not_found,
        }

    # 4. Obtener y descifrar el token de BSale de la empresa activa
    tok_row = await db.execute(text("""
        SELECT slug, pgp_sym_decrypt(bsale_token, :key)::text AS token
        FROM companies WHERE id = :cid
    """), {"cid": cid, "key": _token_key()})
    tk = tok_row.mappings().first()
    if not tk or not tk["token"]:
        raise HTTPException(500, f"No hay token BSale configurado para la empresa {cid}")

    # 5. Ejecutar desactivaciones en BSale concurrentemente fuera del event loop
    def _delete_variants_in_bsale(items: list[dict]):
        deact: list[dict] = []
        fail: list[dict] = []
        vids: list[int] = []
        pids: set[int] = set()

        def _del_one(it: dict):
            vid = it["bsale_variant_id"]
            bsale_client.delete(f"variants/{vid}.json")
            return it

        with tenant_scope(cid, tk["token"], tk["slug"]):
            with ThreadPoolExecutor(max_workers=min(8, max(1, len(items)))) as pool:
                futs = {pool.submit(_del_one, item): item for item in items}
                for fut in as_completed(futs):
                    item = futs[fut]
                    vid = item["bsale_variant_id"]
                    pid = item["bsale_product_id"]
                    try:
                        fut.result()
                        deact.append({
                            "bsale_variant_id": vid,
                            "bsale_product_id": pid,
                            "code": item["code"],
                            "product_name": item["product_name"],
                            "stock": item["stock"],
                        })
                        vids.append(vid)
                        pids.add(pid)
                    except Exception as exc:
                        logger.error("Error al desactivar variante %s en BSale: %s", vid, exc)
                        fail.append({
                            "bsale_variant_id": vid,
                            "code": item["code"],
                            "product_name": item["product_name"],
                            "error": str(exc),
                        })
        return deact, fail, vids, pids

    deactivated, failed, deactivated_vids, affected_pids = await asyncio.to_thread(
        _delete_variants_in_bsale, to_deactivate
    )

    # 6. Actualizar BD local para las variantes desactivadas con éxito
    parent_products_deactivated = 0
    if deactivated_vids:
        await db.execute(text("""
            UPDATE variants
               SET is_active = FALSE, synced_at = NOW()
             WHERE company_id = :cid
               AND bsale_variant_id = ANY(:vids)
        """), {"cid": cid, "vids": deactivated_vids})

        # Desactivar producto padre si TODAS sus variantes quedaron inactivas
        if affected_pids:
            res_parents = await db.execute(text("""
                UPDATE products p
                   SET is_active = FALSE, synced_at = NOW()
                 WHERE p.company_id = :cid
                   AND p.bsale_product_id = ANY(:pids)
                   AND NOT EXISTS (
                       SELECT 1 FROM variants v
                        WHERE v.company_id = p.company_id
                          AND v.bsale_product_id = p.bsale_product_id
                          AND v.is_active = TRUE
                   )
                RETURNING p.bsale_product_id
            """), {"cid": cid, "pids": list(affected_pids)})
            deactivated_pids = [r[0] for r in res_parents.fetchall()]
            parent_products_deactivated = len(deactivated_pids)

            # Desactivar también en BSale los productos padre que ya no tienen variantes activas
            if deactivated_pids:
                def _delete_parents_in_bsale(pids_to_del: list[int]) -> None:
                    with tenant_scope(cid, tk["token"], tk["slug"]):
                        for pid in pids_to_del:
                            try:
                                bsale_client.delete(f"products/{pid}.json")
                                logger.info("Producto padre %s desactivado exitosamente en BSale", pid)
                            except Exception as exc:
                                logger.warning("No se pudo desactivar producto padre %s en BSale: %s", pid, exc)

                await asyncio.to_thread(_delete_parents_in_bsale, deactivated_pids)

        await db.commit()
        matrix_cache.invalidate(company_id=cid)

        # Registrar evento de auditoría
        await log_event(
            db,
            company_id=cid,
            event_type="products.bulk_deactivated",
            actor_user_id=user.id,
            payload={
                "total_received": len(unique_skus),
                "deactivated_variants": len(deactivated),
                "deactivated_parents": parent_products_deactivated,
                "failed_count": len(failed),
                "forzar_con_stock": body.forzar_con_stock,
            },
            commit=True,
        )

    return {
        "ok": True,
        "dry_run": False,
        "forzar_con_stock": body.forzar_con_stock,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "received": len(unique_skus),
            "deactivated": len(deactivated),
            "failed": len(failed),
            "parent_products_deactivated": parent_products_deactivated,
            "blocked_by_stock": len(blocked_by_stock),
            "already_inactive": len(already_inactive),
            "not_found": len(not_found),
        },
        "deactivated": deactivated,
        "failed": failed,
        "blocked_by_stock": blocked_by_stock,
        "already_inactive": already_inactive,
        "not_found": not_found,
    }


# ---------------------------------------------------------------------------
# RECLASIFICACIÓN MASIVA DE PRODUCTOS (Cambiar product_type en BSale + BD)
# ---------------------------------------------------------------------------

class BulkChangeProductTypeIn(BaseModel):
    target_product_type_id: int = Field(
        ...,
        description="ID del product_type de BSale (`bsale_product_type_id`) al que se moverán los productos.",
    )
    product_ids: list[int] | None = Field(
        None,
        description="Lista de bsale_product_id a mover al nuevo tipo.",
    )
    variant_ids: list[int] | None = Field(
        None,
        description="Lista de bsale_variant_id cuyos productos padre se moverán al nuevo tipo.",
    )
    skus: list[str] | None = Field(
        None,
        description="Lista de SKUs (code, bar_code o display_code) cuyos productos padre se moverán al nuevo tipo.",
    )
    clear_overrides: bool = Field(
        True,
        description="Si es True (por defecto), limpia products.subcategory_id para que el producto herede la categoría del nuevo product_type.",
    )
    dry_run: bool = Field(
        False,
        description="Si es True, solo simula y devuelve el resumen sin tocar BSale ni la BD.",
    )


async def _resolve_products_from_selectors(
    db: AsyncSession,
    cid: int,
    product_ids: list[int] | None,
    variant_ids: list[int] | None,
    skus: list[str] | None,
) -> tuple[list[dict], dict[str, list]]:
    """Resuelve una combinación de product_ids, variant_ids y/o skus a productos únicos de la empresa."""
    clean_pids = list(dict.fromkeys(int(x) for x in (product_ids or []) if x and int(x) > 0))
    clean_vids = list(dict.fromkeys(int(x) for x in (variant_ids or []) if x and int(x) > 0))
    clean_skus = list(dict.fromkeys(s.strip() for s in (skus or []) if s and s.strip()))

    if not clean_pids and not clean_vids and not clean_skus:
        raise HTTPException(
            400,
            "Debes enviar al menos uno de: product_ids, variant_ids o skus",
        )

    not_found_pids: list[int] = []
    not_found_vids: list[int] = []
    not_found_skus: list[str] = []
    resolved_pids: set[int] = set()

    if clean_pids:
        r_p = await db.execute(text("""
            SELECT bsale_product_id FROM products
            WHERE company_id = :cid AND bsale_product_id = ANY(:pids)
        """), {"cid": cid, "pids": clean_pids})
        found_p = {row[0] for row in r_p.fetchall()}
        resolved_pids.update(found_p)
        not_found_pids = [pid for pid in clean_pids if pid not in found_p]

    if clean_vids:
        r_v = await db.execute(text("""
            SELECT bsale_variant_id, bsale_product_id FROM variants
            WHERE company_id = :cid AND bsale_variant_id = ANY(:vids)
        """), {"cid": cid, "vids": clean_vids})
        found_v = {}
        for vid, pid in r_v.fetchall():
            found_v[vid] = pid
            if pid:
                resolved_pids.add(pid)
        not_found_vids = [vid for vid in clean_vids if vid not in found_v]

    if clean_skus:
        r_s = await db.execute(text("""
            SELECT code, bar_code, display_code, bsale_product_id
            FROM variants
            WHERE company_id = :cid
              AND (
                  code = ANY(:skus)
                  OR bar_code = ANY(:skus)
                  OR display_code = ANY(:skus)
              )
        """), {"cid": cid, "skus": clean_skus})
        matched_skus: set[str] = set()
        for row in r_s.mappings().all():
            if row["bsale_product_id"]:
                resolved_pids.add(row["bsale_product_id"])
            for s in clean_skus:
                if (
                    (row.get("code") and row["code"].strip() == s)
                    or (row.get("bar_code") and row["bar_code"].strip() == s)
                    or (row.get("display_code") and row["display_code"].strip() == s)
                ):
                    matched_skus.add(s)
        not_found_skus = [s for s in clean_skus if s not in matched_skus]

    products_list: list[dict] = []
    if resolved_pids:
        res_prods = await db.execute(text("""
            SELECT p.bsale_product_id,
                   p.name AS product_name,
                   p.is_active,
                   p.bsale_product_type_id,
                   p.subcategory_id AS override_subcategory_id,
                   vpf.product_type_name AS current_product_type_name,
                   vpf.department AS current_department,
                   vpf.category AS current_category,
                   vpf.subcategory AS current_subcategory
            FROM products p
            LEFT JOIN v_products_full vpf
                   ON vpf.company_id = p.company_id
                  AND vpf.bsale_product_id = p.bsale_product_id
            WHERE p.company_id = :cid
              AND p.bsale_product_id = ANY(:pids)
            ORDER BY p.bsale_product_id
        """), {"cid": cid, "pids": list(resolved_pids)})
        products_list = [dict(r) for r in res_prods.mappings().all()]

    not_found = {
        "product_ids": not_found_pids,
        "variant_ids": not_found_vids,
        "skus": not_found_skus,
    }
    return products_list, not_found


@router.post("/bulk-change-type", dependencies=[Depends(require_operador_or_admin)])
async def bulk_change_product_type(
    body: BulkChangeProductTypeIn,
    company: CurrentCompany = Depends(get_current_company),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Cambia masivamente el `product_type` de muchos productos en BSale y en la BD interna.

    - Acepta `product_ids`, `variant_ids` y/o `skus` (código, código de barras o display_code).
    - Actualiza BSale en paralelo (`PUT /v1/products/{id}.json` con `productTypeId`) respetando el rate-limit.
    - Actualiza en la BD local únicamente los productos confirmados por BSale.
    - Si `clear_overrides=True` (por defecto), limpia `products.subcategory_id` para que el producto
      herede limpiamente la categoría de su nuevo `product_type`.
    """
    cid = company.company_id
    target_pt_id = int(body.target_product_type_id)

    # Validar que el product_type destino exista en la empresa activa
    pt_res = await db.execute(text("""
        SELECT pt.bsale_product_type_id, pt.name, pt.is_active, pt.is_mapped,
               pt.subcategory_id,
               s.name AS subcategory,
               c.name AS category,
               d.name AS department
        FROM product_types pt
        LEFT JOIN subcategories s ON s.id = pt.subcategory_id AND s.company_id = pt.company_id
        LEFT JOIN categories    c ON c.id = s.category_id     AND c.company_id = pt.company_id
        LEFT JOIN departments   d ON d.id = c.department_id   AND d.company_id = pt.company_id
        WHERE pt.company_id = :cid AND pt.bsale_product_type_id = :pt_id
    """), {"cid": cid, "pt_id": target_pt_id})
    target_pt_row = pt_res.mappings().first()
    if not target_pt_row:
        raise HTTPException(
            404,
            f"El product_type destino {target_pt_id} no existe en la BD. "
            f"Si lo acabas de crear en BSale, ejecuta primero POST /bsale/product-types/pull.",
        )
    target_pt = dict(target_pt_row)
    if not target_pt["is_active"]:
        raise HTTPException(400, f"El product_type destino '{target_pt['name']}' ({target_pt_id}) está inactivo")

    matched_products, not_found = await _resolve_products_from_selectors(
        db, cid, body.product_ids, body.variant_ids, body.skus
    )

    to_move: list[dict] = []
    already_in_target: list[dict] = []
    for p in matched_products:
        if p["bsale_product_type_id"] == target_pt_id:
            already_in_target.append(p)
        else:
            to_move.append(p)

    if body.dry_run:
        return {
            "ok": True,
            "operation": "bulk_change_product_type",
            "dry_run": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "target_product_type": target_pt,
            "totals": {
                "matched_products": len(matched_products),
                "to_move": len(to_move),
                "already_in_target": len(already_in_target),
                "not_found_count": sum(len(v) for v in not_found.values()),
            },
            "to_move": to_move,
            "already_in_target": already_in_target,
            "not_found": not_found,
        }

    moved: list[dict] = []
    failed: list[dict] = []

    if to_move:
        tok_row = await db.execute(text("""
            SELECT slug, pgp_sym_decrypt(bsale_token, :key)::text AS token
            FROM companies WHERE id = :cid
        """), {"cid": cid, "key": _token_key()})
        tk = tok_row.mappings().first()
        if not tk or not tk["token"]:
            raise HTTPException(500, f"No hay token BSale configurado para la empresa {cid}")

        def _update_products_in_bsale(items: list[dict]):
            ok_list: list[dict] = []
            err_list: list[dict] = []

            def _put_one(it: dict):
                pid = it["bsale_product_id"]
                resp = bsale_client.put(
                    f"products/{pid}.json",
                    {"id": pid, "productTypeId": target_pt_id},
                )
                if not resp or "id" not in resp:
                    raise RuntimeError(f"BSale no confirmó actualización de producto {pid}")
                return it

            with tenant_scope(cid, tk["token"], tk["slug"]):
                with ThreadPoolExecutor(max_workers=min(12, max(1, len(items)))) as pool:
                    futs = {pool.submit(_put_one, item): item for item in items}
                    for fut in as_completed(futs):
                        item = futs[fut]
                        try:
                            fut.result()
                            ok_list.append({
                                "bsale_product_id": item["bsale_product_id"],
                                "product_name": item["product_name"],
                                "old_product_type_id": item["bsale_product_type_id"],
                                "old_product_type_name": item["current_product_type_name"],
                                "new_product_type_id": target_pt_id,
                                "new_product_type_name": target_pt["name"],
                            })
                        except Exception as exc:
                            logger.error(
                                "Error cambiando product_type de producto %s a %s en BSale: %s",
                                item["bsale_product_id"], target_pt_id, exc,
                            )
                            err_list.append({
                                "bsale_product_id": item["bsale_product_id"],
                                "product_name": item["product_name"],
                                "old_product_type_id": item["bsale_product_type_id"],
                                "error": str(exc),
                            })
            return ok_list, err_list

        moved, failed = await asyncio.to_thread(_update_products_in_bsale, to_move)

    moved_pids = [m["bsale_product_id"] for m in moved]
    already_pids_with_override = [
        p["bsale_product_id"]
        for p in already_in_target
        if body.clear_overrides and p.get("override_subcategory_id") is not None
    ]

    if moved_pids:
        await db.execute(text("""
            UPDATE products
               SET bsale_product_type_id = :target_pt_id,
                   subcategory_id = CASE WHEN :clear_ov THEN NULL ELSE subcategory_id END,
                   synced_at = NOW()
             WHERE company_id = :cid
               AND bsale_product_id = ANY(:pids)
        """), {
            "cid": cid,
            "target_pt_id": target_pt_id,
            "clear_ov": body.clear_overrides,
            "pids": moved_pids,
        })

    if already_pids_with_override:
        await db.execute(text("""
            UPDATE products
               SET subcategory_id = NULL, synced_at = NOW()
             WHERE company_id = :cid
               AND bsale_product_id = ANY(:pids)
        """), {"cid": cid, "pids": already_pids_with_override})

    if moved_pids or already_pids_with_override:
        await log_event(
            db,
            company_id=cid,
            event_type="products.bulk_type_changed",
            actor_user_id=user.id,
            payload={
                "target_product_type_id": target_pt_id,
                "target_product_type_name": target_pt["name"],
                "moved_count": len(moved_pids),
                "failed_count": len(failed),
                "overrides_cleared_on_existing": len(already_pids_with_override),
            },
            commit=False,
        )
        await db.commit()
        matrix_cache.invalidate(company_id=cid)

    return {
        "ok": True,
        "operation": "bulk_change_product_type",
        "dry_run": False,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "target_product_type": target_pt,
        "totals": {
            "matched_products": len(matched_products),
            "moved": len(moved),
            "failed": len(failed),
            "already_in_target": len(already_in_target),
            "overrides_cleared_on_existing": len(already_pids_with_override),
            "not_found_count": sum(len(v) for v in not_found.values()),
        },
        "moved": moved,
        "failed": failed,
        "already_in_target": already_in_target,
        "not_found": not_found,
    }


class BulkProductSubcategoryIn(BaseModel):
    subcategory_id: int | None = Field(
        None,
        description="ID de la subcategoría local a asignar como override. Si es null, elimina el override de los productos indicados.",
    )
    product_ids: list[int] | None = None
    variant_ids: list[int] | None = None
    skus: list[str] | None = None
    clear_all_company_overrides: bool = Field(
        False,
        description="Si es True y subcategory_id es null, limpia TODOS los overrides individuales de productos de la empresa.",
    )


@router.post("/bulk-subcategory", dependencies=[Depends(require_operador_or_admin)])
async def bulk_set_product_subcategory(
    body: BulkProductSubcategoryIn,
    company: CurrentCompany = Depends(get_current_company),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Asigna o limpia masivamente el override `products.subcategory_id` en la BD interna."""
    cid = company.company_id

    if body.subcategory_id is not None:
        res_sub = await db.execute(text("""
            SELECT id, name FROM subcategories WHERE company_id = :cid AND id = :sub_id
        """), {"cid": cid, "sub_id": body.subcategory_id})
        if not res_sub.mappings().first():
            raise HTTPException(404, f"Subcategoría {body.subcategory_id} no existe en esta empresa")

    if body.clear_all_company_overrides and body.subcategory_id is None:
        res_upd = await db.execute(text("""
            UPDATE products
               SET subcategory_id = NULL
             WHERE company_id = :cid AND subcategory_id IS NOT NULL
            RETURNING bsale_product_id
        """), {"cid": cid})
        cleared_ids = [r[0] for r in res_upd.fetchall()]
        await log_event(
            db,
            company_id=cid,
            event_type="products.bulk_overrides_cleared",
            actor_user_id=user.id,
            payload={"cleared_count": len(cleared_ids), "scope": "all_company"},
            commit=False,
        )
        await db.commit()
        matrix_cache.invalidate(company_id=cid)
        return {
            "ok": True,
            "operation": "bulk_clear_all_overrides",
            "updated_count": len(cleared_ids),
            "updated_product_ids": cleared_ids,
        }

    matched_products, not_found = await _resolve_products_from_selectors(
        db, cid, body.product_ids, body.variant_ids, body.skus
    )
    pids = [p["bsale_product_id"] for p in matched_products]
    if pids:
        await db.execute(text("""
            UPDATE products
               SET subcategory_id = :sub_id
             WHERE company_id = :cid AND bsale_product_id = ANY(:pids)
        """), {"cid": cid, "sub_id": body.subcategory_id, "pids": pids})
        await log_event(
            db,
            company_id=cid,
            event_type="products.bulk_subcategory_updated",
            actor_user_id=user.id,
            payload={"updated_count": len(pids), "subcategory_id": body.subcategory_id},
            commit=False,
        )
        await db.commit()
        matrix_cache.invalidate(company_id=cid)

    return {
        "ok": True,
        "operation": "bulk_set_product_subcategory",
        "subcategory_id": body.subcategory_id,
        "updated_count": len(pids),
        "updated_product_ids": pids,
        "not_found": not_found,
    }

