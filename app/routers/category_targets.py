"""Configuración de metas/roles por categoría (`category_targets`).

Conecta el **modelo 80/20** del manual del dueño con datos reales: por cada
categoría motor, una fila con (categoría × sucursal) → meta mensual, rol,
rango de PVP, margen objetivo y rango de SKUs.

Filosofía: **bootstrap automático + editable**.
  - Al primer uso (tabla vacía) el dueño dispara `POST /bootstrap` desde la UI.
  - El sistema detecta motores estables con datos reales (mismo criterio que
    validamos: días con venta ≥ 75/90 + venta ≥ S/5,000 en los últ. 90 días).
  - Para cada motor genera **una fila por sucursal** con valores sugeridos.
  - Después el dueño edita lo que quiera vía `PUT /config/category-targets/...`.

Endpoints:
  GET    /config/category-targets                          → listado completo
  GET    /config/category-targets/preview                  → sugerencia sin guardar
  POST   /config/category-targets/bootstrap                → carga inicial (idempotente)
  PUT    /config/category-targets/{category_id}/{office_id}→ editar una fila
  DELETE /config/category-targets/{category_id}/{office_id}→ borrar una fila

Reglas del bootstrap (heurística):
  - Detecta motores estables (criterio fijo).
  - Asigna rol según ranking de venta + ticket promedio:
      * Top 4 de venta total          → motor_1, motor_2, motor_3, motor_4
      * Ticket promedio > S/30         → upsell
      * Días con venta ≥ 85 (alta frec)→ fijo
      * Resto                         → complemento
  - meta_mensual_pen = (venta_90d / 3) × 1.05  (mensual histórico + 5%)
  - pvp_min/pvp_max  = percentil 10 / 90 del precio histórico
  - margen_objetivo_pct = margen real promedio (recurrente)
  - skus_min/skus_max = SKUs actuales redondeados a rango

  Idempotencia: por defecto NO sobrescribe filas existentes. `?force=true`
  reemplaza todo.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from analytics.core.config import OFFICE_IDS
from app.auth import CurrentCompany, CurrentUser, get_current_company, get_current_user, require_operador_or_admin
from app.database import get_db
from app.events import log_event
from app.routers.config_admin import get_company
from app.routers.diagnosis import _load_exclusions, _TZ_DATE
from harvester.config import TIPOS_VENTA as DEFAULT_TIPOS_VENTA


router = APIRouter(
    prefix="/config/category-targets",
    tags=["config"],
    dependencies=[Depends(get_current_company)],
)


# ════════════════════════════════════════════════════════════════════════════
# Heurística de bootstrap
# ════════════════════════════════════════════════════════════════════════════

# Criterio de "motor estable" en los últimos 90 días:
DAYS_MIN_CON_VENTA = 75          # días con venta de 90
VENTA_MIN_PEN      = 5_000.0     # venta mínima 90d
TICKET_UPSELL_PEN  = 30.0        # ticket promedio para clasificar como "upsell"
DAYS_ALTA_FREC     = 85          # frecuencia mínima para "fijo"
CRECIMIENTO_META   = 1.05        # +5% sobre histórico al sugerir meta


async def _detectar_motores(
    db: AsyncSession, tipos_venta: list[int], excl: dict,
) -> list[dict]:
    """Devuelve la lista de categorías que califican como motor estable, junto
    con todas las métricas necesarias para sugerir meta/rol/PVP/etc.

    Una entrada por (categoría × sucursal), porque las metas son por tienda.
    """
    excl_terms: list[str] = []
    params: dict[str, Any] = {
        "tipos_venta":   tipos_venta,
        "office_ids":    list(OFFICE_IDS),
        "days_min":      DAYS_MIN_CON_VENTA,
        "venta_min":     VENTA_MIN_PEN,
    }
    if excl.get("depts"):
        params["_excl_depts"] = excl["depts"]
        excl_terms.append("AND COALESCE(vpf.department, '') <> ALL(:_excl_depts)")
    if excl.get("cats"):
        params["_excl_cats"] = excl["cats"]
        excl_terms.append("AND COALESCE(vpf.category, '') <> ALL(:_excl_cats)")
    excl_clause = "\n              ".join(excl_terms)

    # Métricas por (categoría × sucursal):
    #   - venta_90d, dias_con_venta, skus_activos
    #   - ticket_promedio aproximado (venta / count distinct documents)
    #   - margen recurrente
    #   - percentil 10/90 del precio unitario
    q = f"""
        WITH base AS (
            SELECT vpf.category, vpf.department, c.id AS category_id,
                   doc.bsale_office_id,
                   dd.bsale_variant_id,
                   doc.bsale_document_id,
                   dd.total_amount,
                   dd.quantity,
                   dd.net_amount,
                   vc.effective_cost,
                   (dd.net_amount / NULLIF(dd.quantity, 0)) AS precio_unit,
                   (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE AS dia
            FROM document_details dd
            JOIN documents doc       ON doc.bsale_document_id = dd.bsale_document_id
            JOIN variants v          ON v.bsale_variant_id    = dd.bsale_variant_id
            JOIN v_products_full vpf ON vpf.bsale_product_id  = v.bsale_product_id
            JOIN categories c        ON c.name = vpf.category
            LEFT JOIN variant_costs vc ON vc.bsale_variant_id = dd.bsale_variant_id
            WHERE (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE >= CURRENT_DATE - 90
              AND (doc.emission_date AT TIME ZONE '{_TZ_DATE}')::DATE <  CURRENT_DATE
              AND COALESCE(doc.is_credit_note, FALSE) = FALSE
              AND doc.bsale_document_type_id = ANY(:tipos_venta)
              AND NOT dd.is_gratuity
              AND doc.bsale_office_id = ANY(:office_ids)
              AND vpf.category IS NOT NULL
              {excl_clause}
        )
        SELECT category_id,
               category                                           AS categoria,
               department                                         AS departamento,
               bsale_office_id,
               COALESCE(SUM(total_amount), 0)                     AS venta_90d,
               COUNT(DISTINCT dia)                                AS dias_con_venta,
               COUNT(DISTINCT bsale_variant_id)                   AS skus_activos,
               COUNT(DISTINCT bsale_document_id)                  AS tickets_90d,
               CASE WHEN SUM(total_amount) > 0 AND COALESCE(SUM(quantity * effective_cost) FILTER (WHERE effective_cost IS NOT NULL), 0) > 0
                    THEN (SUM(total_amount) - SUM(quantity * effective_cost) FILTER (WHERE effective_cost IS NOT NULL))
                         / SUM(total_amount) * 100
                    ELSE NULL
               END                                                AS margen_pct,
               percentile_cont(0.10) WITHIN GROUP (ORDER BY precio_unit)  AS pvp_p10,
               percentile_cont(0.90) WITHIN GROUP (ORDER BY precio_unit)  AS pvp_p90
        FROM base
        GROUP BY category_id, category, department, bsale_office_id
        HAVING COUNT(DISTINCT dia) >= :days_min
           AND SUM(total_amount)    >= :venta_min
        ORDER BY bsale_office_id, venta_90d DESC
    """
    rows = (await db.execute(text(q), params)).mappings().all()
    return [dict(r) for r in rows]


def _asignar_rol_y_meta(rows: list[dict]) -> list[dict]:
    """Toma las filas detectadas y agrega los campos sugeridos:
       rol, meta_mensual_pen, pvp_min/max, margen_objetivo_pct, skus_min/max, nota.

    Procesa por sucursal porque el ranking es por sucursal (los motores #1-4
    de Magdalena pueden no ser los mismos que los de Asamblea).
    """
    out: list[dict] = []
    por_sucursal: dict[int, list[dict]] = {}
    for r in rows:
        por_sucursal.setdefault(int(r["bsale_office_id"]), []).append(r)

    for office_id, items in por_sucursal.items():
        # Items ya vienen ordenados por venta_90d DESC dentro de la sucursal.
        for idx, r in enumerate(items):
            venta_90d        = float(r["venta_90d"])
            tickets_90d      = int(r["tickets_90d"])
            dias_con_venta   = int(r["dias_con_venta"])
            skus_activos     = int(r["skus_activos"])
            margen_pct       = float(r["margen_pct"]) if r["margen_pct"] is not None else None
            pvp_p10          = float(r["pvp_p10"]) if r["pvp_p10"] is not None else None
            pvp_p90          = float(r["pvp_p90"]) if r["pvp_p90"] is not None else None
            ticket_promedio  = (venta_90d / tickets_90d) if tickets_90d else 0.0

            # Asignar rol
            if idx < 4:
                rol = f"motor_{idx + 1}"
            elif ticket_promedio > TICKET_UPSELL_PEN:
                rol = "upsell"
            elif dias_con_venta >= DAYS_ALTA_FREC:
                rol = "fijo"
            else:
                rol = "complemento"

            # Meta = mensual histórico × crecimiento
            meta_mensual = round((venta_90d / 3.0) * CRECIMIENTO_META, 2)
            # SKUs: rango entre actual y 1.5× actual (mínimo 5 SKUs en el rango)
            skus_min = max(5, skus_activos)
            skus_max = max(skus_min + 2, int(skus_activos * 1.5))

            out.append({
                "category_id":          int(r["category_id"]),
                "bsale_office_id":      office_id,
                "categoria":            r["categoria"],
                "departamento":         r["departamento"],
                "rol":                  rol,
                "meta_mensual_pen":     meta_mensual,
                "pvp_min":              round(pvp_p10, 2) if pvp_p10 is not None else None,
                "pvp_max":              round(pvp_p90, 2) if pvp_p90 is not None else None,
                "margen_objetivo_pct":  round(margen_pct, 1) if margen_pct is not None else None,
                "skus_min":             skus_min,
                "skus_max":             skus_max,
                "nota":                 "Generado automáticamente por /bootstrap",
                # Metadatos de la detección — útiles para mostrar en la UI
                "_metricas": {
                    "venta_90d":       round(venta_90d, 2),
                    "dias_con_venta":  dias_con_venta,
                    "ticket_promedio": round(ticket_promedio, 2),
                    "skus_activos":    skus_activos,
                },
            })
    return out


# ════════════════════════════════════════════════════════════════════════════
# Schemas Pydantic
# ════════════════════════════════════════════════════════════════════════════

class CategoryTargetUpdate(BaseModel):
    rol:                  Optional[str]    = None
    meta_mensual_pen:     Optional[float]  = None
    pvp_min:              Optional[float]  = None
    pvp_max:              Optional[float]  = None
    margen_objetivo_pct:  Optional[float]  = None
    skus_min:             Optional[int]    = None
    skus_max:             Optional[int]    = None
    nota:                 Optional[str]    = None


# ════════════════════════════════════════════════════════════════════════════
# Endpoints
# ════════════════════════════════════════════════════════════════════════════

@router.get("")
async def list_targets(
    office_id: Optional[int] = Query(None, description="Filtrar por sucursal."),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Lista todas las configuraciones cargadas con nombres legibles."""
    where = "WHERE 1=1"
    params: dict[str, Any] = {}
    if office_id is not None:
        where += " AND ct.bsale_office_id = :office_id"
        params["office_id"] = office_id

    q = f"""
        SELECT ct.category_id, c.name AS categoria,
               d.name AS departamento,
               ct.bsale_office_id, o.name AS sucursal,
               ct.rol, ct.meta_mensual_pen,
               ct.pvp_min, ct.pvp_max,
               ct.margen_objetivo_pct, ct.skus_min, ct.skus_max, ct.nota
        FROM category_targets ct
        JOIN categories c   ON c.id = ct.category_id
        LEFT JOIN departments d ON d.id = c.department_id
        LEFT JOIN offices o ON o.bsale_office_id = ct.bsale_office_id
        {where}
        ORDER BY ct.bsale_office_id, ct.meta_mensual_pen DESC NULLS LAST
    """
    rows = (await db.execute(text(q), params)).mappings().all()
    return {
        "total":  len(rows),
        "items": [
            {
                "category_id":         int(r["category_id"]),
                "categoria":           r["categoria"],
                "departamento":        r["departamento"],
                "bsale_office_id":     int(r["bsale_office_id"]),
                "sucursal":            r["sucursal"],
                "rol":                 r["rol"],
                "meta_mensual_pen":    float(r["meta_mensual_pen"]) if r["meta_mensual_pen"] is not None else None,
                "pvp_min":             float(r["pvp_min"]) if r["pvp_min"] is not None else None,
                "pvp_max":             float(r["pvp_max"]) if r["pvp_max"] is not None else None,
                "margen_objetivo_pct": float(r["margen_objetivo_pct"]) if r["margen_objetivo_pct"] is not None else None,
                "skus_min":            r["skus_min"],
                "skus_max":            r["skus_max"],
                "nota":                r["nota"],
            }
            for r in rows
        ],
    }


@router.get("/preview")
async def preview_bootstrap(
    company_ctx: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Devuelve la sugerencia que generaría el bootstrap, SIN guardarla.
    Útil para que la UI muestre 'esto se va a cargar — ¿confirmas?'.
    """
    cid = company_ctx.company_id
    company = await get_company(db, cid)
    tipos_venta = company.get("tipos_venta") or DEFAULT_TIPOS_VENTA
    excl = await _load_exclusions(db)

    rows = await _detectar_motores(db, tipos_venta, excl)
    sugeridos = _asignar_rol_y_meta(rows)
    return {
        "total_sugerencias": len(sugeridos),
        "exclusiones_aplicadas": {
            "departamentos": excl.get("depts", []),
            "categorias":    excl.get("cats", []),
        },
        "criterios": {
            "dias_min_con_venta": DAYS_MIN_CON_VENTA,
            "venta_min_90d_pen":  VENTA_MIN_PEN,
            "ticket_upsell_pen":  TICKET_UPSELL_PEN,
            "dias_alta_frec":     DAYS_ALTA_FREC,
            "crecimiento_meta":   f"{(CRECIMIENTO_META - 1) * 100:.0f}%",
        },
        "items": sugeridos,
    }


@router.post("/bootstrap", dependencies=[Depends(require_operador_or_admin)])
async def bootstrap(
    force: bool = Query(False, description="Si true, BORRA todo lo existente y carga de cero."),
    company_ctx: CurrentCompany = Depends(get_current_company),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Carga inicial automática. Por defecto solo corre si la tabla está vacía.

    Si la tabla ya tiene filas y `force=false` → 409 (Conflict). Para reemplazar
    todo, pasar `force=true` (BORRA filas existentes — irreversible).
    """
    existentes = await db.scalar(text("SELECT COUNT(*) FROM category_targets")) or 0
    if existentes > 0 and not force:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Ya hay {existentes} filas en category_targets. "
                "Para reemplazar todo, llamar con ?force=true. "
                "Para editar filas individuales, usar PUT /config/category-targets/{category_id}/{office_id}."
            ),
        )

    cid = company_ctx.company_id
    company = await get_company(db, cid)
    tipos_venta = company.get("tipos_venta") or DEFAULT_TIPOS_VENTA
    excl = await _load_exclusions(db)

    rows = await _detectar_motores(db, tipos_venta, excl)
    sugeridos = _asignar_rol_y_meta(rows)

    if force and existentes > 0:
        await db.execute(text("DELETE FROM category_targets"))

    for item in sugeridos:
        await db.execute(
            text("""
                INSERT INTO category_targets
                    (category_id, bsale_office_id, rol, meta_mensual_pen,
                     pvp_min, pvp_max, margen_objetivo_pct,
                     skus_min, skus_max, nota)
                VALUES
                    (:category_id, :bsale_office_id, :rol, :meta_mensual_pen,
                     :pvp_min, :pvp_max, :margen_objetivo_pct,
                     :skus_min, :skus_max, :nota)
                ON CONFLICT (category_id, bsale_office_id) DO NOTHING
            """),
            {
                "category_id":         item["category_id"],
                "bsale_office_id":     item["bsale_office_id"],
                "rol":                 item["rol"],
                "meta_mensual_pen":    item["meta_mensual_pen"],
                "pvp_min":             item["pvp_min"],
                "pvp_max":             item["pvp_max"],
                "margen_objetivo_pct": item["margen_objetivo_pct"],
                "skus_min":            item["skus_min"],
                "skus_max":            item["skus_max"],
                "nota":                item["nota"],
            },
        )
    await log_event(
        db, company_id=cid, event_type="config.updated", actor_user_id=user.id,
        payload={
            "que": "category_targets_bootstrap",
            "force": force,
            "filas_insertadas": len(sugeridos),
            "filas_borradas": existentes if force else 0,
        },
        commit=False,
    )
    await db.commit()

    return {
        "ok":              True,
        "filas_insertadas": len(sugeridos),
        "filas_borradas":  existentes if force else 0,
        "force":           force,
        "items":           sugeridos,
    }


@router.put("/{category_id}/{office_id}", dependencies=[Depends(require_operador_or_admin)])
async def update_target(
    category_id: int, office_id: int, body: CategoryTargetUpdate,
    company: CurrentCompany = Depends(get_current_company),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Actualiza una fila existente. Solo los campos presentes en el body se modifican.
    Si la fila no existe, devuelve 404 (usar bootstrap o crear vía SQL primero)."""
    existente = (await db.execute(
        text("SELECT 1 FROM category_targets WHERE category_id = :c AND bsale_office_id = :o"),
        {"c": category_id, "o": office_id},
    )).first()
    if not existente:
        raise HTTPException(
            status_code=404,
            detail=f"No existe fila para (category_id={category_id}, office_id={office_id}).",
        )

    fields_to_update = body.model_dump(exclude_unset=True)
    if not fields_to_update:
        raise HTTPException(status_code=400, detail="No hay campos para actualizar.")

    set_clause = ", ".join(f"{k} = :{k}" for k in fields_to_update)
    params: dict[str, Any] = {**fields_to_update, "c": category_id, "o": office_id}
    await db.execute(
        text(f"UPDATE category_targets SET {set_clause} WHERE category_id = :c AND bsale_office_id = :o"),
        params,
    )
    await log_event(
        db, company_id=company.company_id, event_type="config.updated",
        actor_user_id=user.id,
        payload={
            "que": "category_target_update",
            "category_id": category_id,
            "office_id": office_id,
            "campos": list(fields_to_update.keys()),
        },
        commit=False,
    )
    await db.commit()

    # Devolver fila actualizada
    r = (await db.execute(
        text("SELECT * FROM category_targets WHERE category_id = :c AND bsale_office_id = :o"),
        {"c": category_id, "o": office_id},
    )).mappings().one()
    return {"ok": True, "actualizado": dict(r)}


@router.delete("/{category_id}/{office_id}", dependencies=[Depends(require_operador_or_admin)])
async def delete_target(
    category_id: int, office_id: int,
    company: CurrentCompany = Depends(get_current_company),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Elimina una fila."""
    res = await db.execute(
        text("DELETE FROM category_targets WHERE category_id = :c AND bsale_office_id = :o"),
        {"c": category_id, "o": office_id},
    )
    if res.rowcount == 0:
        await db.commit()
        raise HTTPException(status_code=404, detail="Fila no encontrada.")
    await log_event(
        db, company_id=company.company_id, event_type="config.updated",
        actor_user_id=user.id,
        payload={
            "que": "category_target_delete",
            "category_id": category_id,
            "office_id": office_id,
        },
        commit=False,
    )
    await db.commit()
    return {"ok": True, "category_id": category_id, "office_id": office_id}
