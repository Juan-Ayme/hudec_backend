"""Atributos de producto/variante y proveedores (capa propia, no BSale).

  GET  /attributes/definitions              catálogo de atributos (los siembra si faltan)
  GET  /attributes/values?code=marca        valores distintos con conteo (para filtros)
  GET  /attributes/coverage                 % de productos activos con cada atributo
  GET  /attributes/products/{id}            atributos + variantes + proveedores de un producto
  PUT  /attributes/products/{id}            carga manual (valor null = borrar el manual)
  POST /attributes/bulk                     mismo valor manual para muchos productos
  GET  /attributes/driver-template          plantilla Excel del driver con listas desplegables
  POST /attributes/import-driver            importa el driver (dry_run por defecto)

Lectura: cualquier usuario de la empresa. Escritura: operador o admin.
Ningún endpoint toca productos, variantes ni BSale.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    CurrentCompany,
    CurrentUser,
    get_current_company,
    get_current_user,
    require_operador_or_admin,
)
from app.database import get_db
from app.kawii_matrix import cache as matrix_cache
from app.services.attribute_catalog import (
    ATTRIBUTE_DEFS,
    DEFS_BY_CODE,
    PRODUCT_CODES,
    ensure_definitions,
)
from app.services.attribute_inference import norm, parse_contenido
from app.services.attribute_store import delete_attr, upsert_attr
from app.services.driver_import import DriverError, import_driver, parse_driver
from app.services.driver_template import build_driver_template

router = APIRouter(
    prefix="/attributes",
    tags=["product-attributes"],
    dependencies=[Depends(get_current_company)],
)

_MAX_UPLOAD = 15 * 1024 * 1024


async def _require_tables(db: AsyncSession) -> None:
    ok = await db.scalar(text("SELECT to_regclass('product_attrs') IS NOT NULL"))
    if not ok:
        raise HTTPException(
            503,
            "Falta aplicar la migración docs/migrations/2026-11-02_product_attributes.sql",
        )


def _parse_value(code: str, raw) -> tuple[str | None, float | None, str | None]:
    """Valida un valor manual según el tipo del atributo → (texto, número, unidad)."""
    d = DEFS_BY_CODE.get(code)
    if d is None:
        raise HTTPException(400, f"Atributo desconocido: {code}")
    if code == "contenido":
        p = parse_contenido(str(raw))
        if not p:
            raise HTTPException(400, f"Contenido '{raw}' sin unidad (ml, g, L, kg)")
        return None, p.value_num, p.unit
    if d.data_type == "number":
        try:
            return None, float(raw), "und"
        except (TypeError, ValueError):
            raise HTTPException(400, f"{d.name} debe ser un número")
    s = " ".join(str(raw).split())
    if d.data_type == "enum":
        for allowed in d.allowed_values:
            if norm(allowed) == norm(s):
                return allowed, None, None
        raise HTTPException(400, f"'{s}' no es válido para {d.name}: {', '.join(d.allowed_values)}")
    return s, None, None


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------

@router.get("/definitions")
async def definitions(company: CurrentCompany = Depends(get_current_company),
                      db: AsyncSession = Depends(get_db)) -> dict:
    await _require_tables(db)
    if await ensure_definitions(db, company.company_id):
        await db.commit()
    rows = (await db.execute(text(
        "SELECT code, name, level, data_type, allowed_values, push_to_bsale "
        "FROM attribute_definitions WHERE company_id = :c ORDER BY sort_order, code"),
        {"c": company.company_id})).mappings().all()
    return {"definitions": [dict(r) for r in rows]}


@router.get("/values")
async def attribute_values(code: str = Query(..., description="Código del atributo, ej. marca"),
                           company: CurrentCompany = Depends(get_current_company),
                           db: AsyncSession = Depends(get_db)) -> dict:
    await _require_tables(db)
    if code == "proveedor":
        q = ("SELECT s.name AS valor, count(DISTINCT ps.bsale_product_id) AS productos "
             "FROM suppliers s LEFT JOIN product_suppliers ps "
             "  ON ps.company_id = s.company_id AND ps.supplier_id = s.id "
             "WHERE s.company_id = :c GROUP BY 1 ORDER BY 2 DESC, 1")
    elif code in DEFS_BY_CODE:
        table = "product_attrs" if DEFS_BY_CODE[code].level == "product" else "variant_attrs"
        q = (f"SELECT COALESCE(value_text, trim(to_char(value_num, 'FM999999990.##') || ' ' || COALESCE(unit, ''))) "
             f"AS valor, count(*) AS productos FROM {table} "
             f"WHERE company_id = :c AND code = :code GROUP BY 1 ORDER BY 2 DESC, 1")
    else:
        raise HTTPException(400, f"Atributo desconocido: {code}")
    rows = (await db.execute(text(q), {"c": company.company_id, "code": code})).mappings().all()
    return {"code": code, "values": [dict(r) for r in rows]}


@router.get("/coverage")
async def coverage(incluir_inactivos: bool = Query(False, description="Cuenta también productos inactivos"),
                   company: CurrentCompany = Depends(get_current_company),
                   db: AsyncSession = Depends(get_db)) -> dict:
    """Cuántos productos tienen cada atributo, y de qué fuente vino (activos, o todos)."""
    await _require_tables(db)
    cid = company.company_id
    params = {"c": cid, "inc": incluir_inactivos}
    total = await db.scalar(text("SELECT count(*) FROM products WHERE company_id = :c AND (is_active OR :inc)"),
                            params) or 0
    rows = (await db.execute(text("""
        SELECT a.code, a.source, count(*) AS n
        FROM product_attrs a
        JOIN products p ON p.company_id = a.company_id AND p.bsale_product_id = a.bsale_product_id
        WHERE a.company_id = :c AND (p.is_active OR :inc)
        GROUP BY 1, 2
    """), params)).mappings().all()
    by_code: dict[str, dict] = {code: {"con_valor": 0, "por_fuente": {}} for code in PRODUCT_CODES}
    for r in rows:
        b = by_code.setdefault(r["code"], {"con_valor": 0, "por_fuente": {}})
        b["con_valor"] += r["n"]
        b["por_fuente"][r["source"]] = r["n"]
    con_proveedor = await db.scalar(text("""
        SELECT count(DISTINCT ps.bsale_product_id) FROM product_suppliers ps
        JOIN products p ON p.company_id = ps.company_id AND p.bsale_product_id = ps.bsale_product_id
        WHERE ps.company_id = :c AND (p.is_active OR :inc)"""), params) or 0
    by_code["proveedor"] = {"con_valor": con_proveedor, "por_fuente": {}}
    for b in by_code.values():
        b["pct"] = round(100.0 * b["con_valor"] / total, 1) if total else 0.0
    return {"productos": total, "incluye_inactivos": incluir_inactivos, "atributos": by_code}


@router.get("/products/{product_id}")
async def product_attributes(product_id: int,
                             company: CurrentCompany = Depends(get_current_company),
                             db: AsyncSession = Depends(get_db)) -> dict:
    await _require_tables(db)
    cid = company.company_id
    prod = (await db.execute(text(
        "SELECT bsale_product_id, product_name, department, category, subcategory "
        "FROM v_products_full WHERE company_id = :c AND bsale_product_id = :p"),
        {"c": cid, "p": product_id})).mappings().first()
    if not prod:
        raise HTTPException(404, f"Producto {product_id} no existe")
    attrs = (await db.execute(text(
        "SELECT code, value_text, value_num, unit, source, confidence, updated_at "
        "FROM product_attrs WHERE company_id = :c AND bsale_product_id = :p ORDER BY code"),
        {"c": cid, "p": product_id})).mappings().all()
    variants = (await db.execute(text("""
        SELECT v.bsale_variant_id, v.display_code, v.bar_code, v.description,
               COALESCE(json_agg(json_build_object('code', va.code, 'value', va.value_text,
                                                   'source', va.source))
                        FILTER (WHERE va.code IS NOT NULL), '[]') AS atributos
        FROM variants v
        LEFT JOIN variant_attrs va ON va.company_id = v.company_id AND va.bsale_variant_id = v.bsale_variant_id
        WHERE v.company_id = :c AND v.bsale_product_id = :p
        GROUP BY 1, 2, 3, 4 ORDER BY 1"""), {"c": cid, "p": product_id})).mappings().all()
    suppliers = (await db.execute(text("""
        SELECT s.id, s.name, ps.last_unit_cost, ps.last_purchase_date, ps.is_primary
        FROM product_suppliers ps JOIN suppliers s ON s.company_id = ps.company_id AND s.id = ps.supplier_id
        WHERE ps.company_id = :c AND ps.bsale_product_id = :p
        ORDER BY ps.is_primary DESC, ps.last_purchase_date DESC NULLS LAST"""),
        {"c": cid, "p": product_id})).mappings().all()
    return {"producto": dict(prod), "atributos": [dict(a) for a in attrs],
            "variantes": [dict(v) for v in variants], "proveedores": [dict(s) for s in suppliers]}


# ---------------------------------------------------------------------------
# Escritura manual
# ---------------------------------------------------------------------------

class ManualAttrsIn(BaseModel):
    attrs: dict[str, object | None] = Field(
        ..., description="{código: valor}. null borra el valor manual (vuelve a quedar sin dato).")
    variant_id: int | None = Field(None, description="Para atributos de variante: a qué variante aplica.")


@router.put("/products/{product_id}", dependencies=[Depends(require_operador_or_admin)])
async def set_product_attributes(product_id: int, body: ManualAttrsIn,
                                 company: CurrentCompany = Depends(get_current_company),
                                 user: CurrentUser = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)) -> dict:
    await _require_tables(db)
    cid = company.company_id
    await ensure_definitions(db, cid)
    exists = await db.scalar(text("SELECT 1 FROM products WHERE company_id = :c AND bsale_product_id = :p"),
                             {"c": cid, "p": product_id})
    if not exists:
        raise HTTPException(404, f"Producto {product_id} no existe")
    if body.variant_id is not None:
        ok = await db.scalar(text("SELECT 1 FROM variants WHERE company_id = :c AND bsale_variant_id = :v "
                                  "AND bsale_product_id = :p"), {"c": cid, "v": body.variant_id, "p": product_id})
        if not ok:
            raise HTTPException(400, f"La variante {body.variant_id} no es del producto {product_id}")

    written, deleted = [], []
    for code, raw in body.attrs.items():
        d = DEFS_BY_CODE.get(code)
        if d is None:
            raise HTTPException(400, f"Atributo desconocido: {code}")
        if d.level == "variant" and body.variant_id is None:
            raise HTTPException(400, f"'{code}' es de variante: enviá variant_id")
        entity = product_id if d.level == "product" else body.variant_id
        if raw is None or str(raw).strip() == "":
            if await delete_attr(db, cid, d.level, entity, code):
                deleted.append(code)
            continue
        vt, vn, unit = _parse_value(code, raw)
        await upsert_attr(db, cid, d.level, entity, code, value_text=vt, value_num=vn, unit=unit,
                          source="manual", confidence=1.0, user_id=user.id)
        written.append(code)
    await db.commit()
    matrix_cache.invalidate(company_id=cid)
    return {"ok": True, "escritos": written, "borrados": deleted}


class BulkAttrIn(BaseModel):
    product_ids: list[int] = Field(..., min_length=1, max_length=5000)
    code: str
    value: object | None = Field(None, description="null borra el valor en todos")


@router.post("/bulk", dependencies=[Depends(require_operador_or_admin)])
async def bulk_set_attribute(body: BulkAttrIn,
                             company: CurrentCompany = Depends(get_current_company),
                             user: CurrentUser = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)) -> dict:
    """Mismo valor manual para muchos productos (solo atributos de producto)."""
    await _require_tables(db)
    cid = company.company_id
    d = DEFS_BY_CODE.get(body.code)
    if d is None or d.level != "product":
        raise HTTPException(400, f"'{body.code}' no es un atributo de producto")
    await ensure_definitions(db, cid)
    ids = (await db.execute(text(
        "SELECT bsale_product_id FROM products WHERE company_id = :c AND bsale_product_id = ANY(:ids)"),
        {"c": cid, "ids": list(set(body.product_ids))})).scalars().all()
    n = 0
    if body.value is None or str(body.value).strip() == "":
        for pid in ids:
            n += await delete_attr(db, cid, "product", pid, body.code)
    else:
        vt, vn, unit = _parse_value(body.code, body.value)
        for pid in ids:
            n += await upsert_attr(db, cid, "product", pid, body.code, value_text=vt, value_num=vn,
                                   unit=unit, source="manual", confidence=1.0, user_id=user.id)
    await db.commit()
    matrix_cache.invalidate(company_id=cid)
    return {"ok": True, "productos": len(ids), "filas_afectadas": n,
            "no_encontrados": sorted(set(body.product_ids) - set(ids))}


# ---------------------------------------------------------------------------
# Driver Excel
# ---------------------------------------------------------------------------

@router.get("/driver-template")
async def driver_template(company: CurrentCompany = Depends(get_current_company),
                          db: AsyncSession = Depends(get_db)) -> Response:
    """Plantilla del driver con listas desplegables (tipos de la taxonomía vigente)."""
    rows = (await db.execute(text("""
        SELECT c.name || ' / ' || s.name FROM subcategories s
        JOIN categories c ON c.company_id = s.company_id AND c.id = s.category_id
        WHERE s.company_id = :c"""), {"c": company.company_id})).scalars().all()
    content = build_driver_template(list(rows))
    fecha = datetime.now().strftime("%Y-%m-%d")
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="driver_compras_{fecha}.xlsx"'},
    )


@router.post("/import-driver", dependencies=[Depends(require_operador_or_admin)])
async def import_driver_endpoint(
    file: UploadFile = File(..., description="Driver de compras (.xlsx)"),
    dry_run: bool = Query(True, description="True = solo informa qué haría, no escribe"),
    sheet: str | None = Query(None, description="Hoja a leer (default: la primera)"),
    company: CurrentCompany = Depends(get_current_company),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Importa proveedor, último costo y atributos desde el driver. Ver
    app/services/driver_import.py para el detalle de columnas."""
    await _require_tables(db)
    if not (file.filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "El driver debe ser .xlsx")
    content = await file.read()
    if len(content) > _MAX_UPLOAD:
        raise HTTPException(413, "Archivo demasiado grande (máx. 15 MB)")
    try:
        rows, columnas = parse_driver(content, sheet)
    except DriverError as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(400, f"La hoja '{sheet}' no existe")

    cid = company.company_id
    await ensure_definitions(db, cid)
    result = await import_driver(db, cid, rows, dry_run=dry_run, user_id=user.id)
    if dry_run:
        await db.rollback()
    else:
        await db.commit()
        matrix_cache.invalidate(company_id=cid)
    attr_cols = [d.code for d in ATTRIBUTE_DEFS if d.code in columnas]
    return {"ok": True, "dry_run": dry_run, "archivo": file.filename,
            "columnas_reconocidas": columnas, "columnas_de_atributos": attr_cols, **result}
