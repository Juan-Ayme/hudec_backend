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

from datetime import datetime, timezone

from fastapi import Depends, APIRouter, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from pydantic import BaseModel, Field

from app.auth import get_current_company, require_admin
from app.database import get_db
from harvester import bsale_client


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


async def _full_pt(pt_id: int, db: AsyncSession) -> dict | None:
    """Devuelve el product_type con su mapeo a la taxonomia local."""
    res = await db.execute(text("""
        SELECT pt.bsale_product_type_id, pt.name, pt.is_active, pt.is_mapped,
               pt.subcategory_id,
               s.name AS subcategory,
               c.name AS category, c.id AS category_id,
               d.name AS department, d.id AS department_id
        FROM product_types pt
        LEFT JOIN subcategories s ON s.id = pt.subcategory_id
        LEFT JOIN categories    c ON c.id = s.category_id
        LEFT JOIN departments   d ON d.id = c.department_id
        WHERE pt.bsale_product_type_id = :pt_id
    """), {"pt_id": pt_id})
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
    limit: int = Query(500, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Lista product_types locales con su mapeo a la taxonomia.

    Util para el panel de administracion: muestra cuales estan mapeados,
    a que subcategoria, cuantos productos tienen, y si su nombre cumple
    la convencion 'Categoria / Subcategoria'.
    """
    where = []
    params: dict = {}

    if q:
        where.append("pt.name ILIKE :q")
        params["q"] = f"%{q}%"
    if only_unmapped:
        where.append("pt.is_mapped = FALSE")
    if only_inactive:
        where.append("pt.is_active = FALSE")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params["limit"] = limit

    res = await db.execute(text(f"""
        SELECT pt.bsale_product_type_id, pt.name, pt.is_active, pt.is_mapped,
               pt.subcategory_id, pt.synced_at,
               s.name AS subcategory,
               c.name AS category, c.id AS category_id,
               d.name AS department, d.id AS department_id,
               (SELECT COUNT(*) FROM products p
                 WHERE p.bsale_product_type_id = pt.bsale_product_type_id) AS productos,
               CASE
                 WHEN pt.is_mapped AND s.name IS NOT NULL AND c.name IS NOT NULL
                      AND pt.name = c.name || ' / ' || s.name
                 THEN TRUE ELSE FALSE
               END AS naming_ok
        FROM product_types pt
        LEFT JOIN subcategories s ON s.id = pt.subcategory_id
        LEFT JOIN categories    c ON c.id = s.category_id
        LEFT JOIN departments   d ON d.id = c.department_id
        {where_sql}
        ORDER BY pt.name
        LIMIT :limit
    """), params)

    rows = [dict(r) for r in res.mappings().all()]
    return {"total": len(rows), "items": rows}


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------

@router.post("/product-types", status_code=201)
async def create_product_type(
    body: ProductTypeIn,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    1) Crea el product_type en BSale (POST /v1/product_types.json).
    2) Lo inserta en la tabla local product_types.
    3) Lo deja mapeado a la subcategoria indicada (si se pasa).

    scope = "bsale+internal"
    """
    # Validar subcategoria local si se entrega
    sub_local = None
    if body.subcategory_id is not None:
        res_sub = await db.execute(
            text("SELECT id, name FROM subcategories WHERE id = :id"),
            {"id": body.subcategory_id},
        )
        row = res_sub.mappings().first()
        sub_local = dict(row) if row else None
        if not sub_local:
            raise HTTPException(
                404,
                f"Subcategoria {body.subcategory_id} no existe en mi BD. "
                f"Crea primero la taxonomia con /taxonomy/subcategories.",
            )

    # 1) BSale
    try:
        resp = bsale_client.post("product_types.json", {"name": body.name})
    except RuntimeError as exc:
        raise HTTPException(502, f"Error creando en BSale: {exc}")

    pt_id = resp.get("id")
    if not pt_id:
        raise HTTPException(502, f"BSale no devolvio id. Respuesta: {resp}")

    # 2) BD local (UPSERT idempotente)
    is_mapped = sub_local is not None
    await db.execute(text("""
        INSERT INTO product_types
            (bsale_product_type_id, name, subcategory_id, is_active, is_mapped, synced_at)
        VALUES (:pt_id, :name, :sub_id, TRUE, :is_mapped, NOW())
        ON CONFLICT (bsale_product_type_id) DO UPDATE SET
            name           = EXCLUDED.name,
            subcategory_id = EXCLUDED.subcategory_id,
            is_active      = EXCLUDED.is_active,
            is_mapped      = EXCLUDED.is_mapped,
            synced_at      = NOW()
    """), {
        "pt_id": pt_id,
        "name": body.name,
        "sub_id": body.subcategory_id,
        "is_mapped": is_mapped,
    })
    await db.commit()

    warnings = []
    if not is_mapped:
        warnings.append(
            "product_type creado SIN mapeo. Usa "
            "PATCH /bsale/product-types/{id} con subcategory_id para mapearlo."
        )

    entity = await _full_pt(pt_id, db)
    return await _report("create_product_type", entity, warnings=warnings)


# ---------------------------------------------------------------------------
# UPDATE (rename y/o re-map)
# ---------------------------------------------------------------------------

@router.patch("/product-types/{pt_id}")
async def update_product_type(
    pt_id: int,
    body: ProductTypeUpdateIn,
    unmap: bool = False,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Actualiza un product_type. Tres operaciones independientes:

      - Renombrar  -> body.name           (afecta BSale + BD)
      - Re-mapear  -> body.subcategory_id (afecta solo BD interna)
      - Des-mapear -> ?unmap=true         (afecta solo BD interna)

    Se pueden combinar en un mismo request.
    """
    pt = await _full_pt(pt_id, db)
    if not pt:
        raise HTTPException(404, f"product_type {pt_id} no existe en mi BD")

    warnings: list[str] = []
    scopes: set[str] = set()

    # --- 1) Rename en BSale (si cambia el nombre) ---
    new_name = pt["name"]
    if body.name and body.name != pt["name"]:
        try:
            bsale_client.put(f"product_types/{pt_id}.json", {"name": body.name})
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
            text("SELECT id FROM subcategories WHERE id = :id"),
            {"id": body.subcategory_id},
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
         WHERE bsale_product_type_id = :pt_id
    """), {
        "name": new_name,
        "sub_id": new_sub_id,
        "is_mapped": new_is_mapped,
        "pt_id": pt_id,
    })
    await db.commit()

    scope = "bsale+internal" if "bsale" in scopes and "internal" in scopes \
        else ("bsale" if "bsale" in scopes else "internal_db")

    entity = await _full_pt(pt_id, db)
    return await _report("update_product_type", entity, scope=scope, warnings=warnings)


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------

@router.delete("/product-types/{pt_id}")
async def delete_product_type(
    pt_id: int,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Elimina un product_type en BSale + BD interna.

    Por defecto: rechaza si tiene productos (409). Con ?force=true
    igual se elimina, pero los productos quedaran apuntando a un
    product_type fantasma (BSale tipicamente no permite borrar tipos
    con productos vivos, asi que ese caso suele fallar en la API).
    """
    pt = await _full_pt(pt_id, db)
    if not pt:
        raise HTTPException(404, f"product_type {pt_id} no existe en mi BD")

    res_count = await db.execute(
        text("SELECT COUNT(*) FROM products WHERE bsale_product_type_id = :pt_id"),
        {"pt_id": pt_id},
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

    # 1) BSale
    try:
        bsale_client.delete(f"product_types/{pt_id}.json")
    except RuntimeError as exc:
        raise HTTPException(502, f"Error eliminando en BSale: {exc}")

    # 2) BD local
    await db.execute(
        text("DELETE FROM product_types WHERE bsale_product_type_id = :pt_id"),
        {"pt_id": pt_id},
    )
    await db.commit()

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
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Trae el product_type desde BSale y reescribe la fila local
    (sin tocar el subcategory_id local). Util si alguien edito en BSale
    fuera de la app y queremos reflejarlo sin ejecutar el sync completo.
    """
    from harvester.config import BSALE_BASE_URL

    data = bsale_client.fetch(f"{BSALE_BASE_URL}/product_types/{pt_id}.json")
    if not data or "id" not in data:
        raise HTTPException(404, f"BSale no devolvio product_type {pt_id}")

    is_active = str(data.get("state", "0")) not in ("1", 1, True, "true")

    await db.execute(text("""
        INSERT INTO product_types
            (bsale_product_type_id, name, is_active, is_mapped, synced_at)
        VALUES (:pt_id, :name, :is_active, FALSE, NOW())
        ON CONFLICT (bsale_product_type_id) DO UPDATE SET
            name      = EXCLUDED.name,
            is_active = EXCLUDED.is_active,
            synced_at = NOW()
    """), {"pt_id": pt_id, "name": data.get("name", ""), "is_active": is_active})
    await db.commit()

    entity = await _full_pt(pt_id, db)
    return await _report("resync_product_type", entity, scope="bsale->internal")
