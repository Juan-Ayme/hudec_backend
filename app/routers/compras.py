"""Ecosistema de compras — cruza COMPRA ↔ VENTA ↔ STOCK por nivel de taxonomía.

Responde la pregunta que el backend hoy no contesta: *"¿cuánto compré de cada
categoría, cuánto vendí de eso, y qué me quedó?"*. De ahí salen tres lecturas
que el gerente puede accionar sin que nadie se las explique:

  - **multiplicador** = venta / compra. Soles vendidos por cada sol comprado.
    Bajo (< ~1.5) = se compró de más para lo que rota.
  - **sellthrough** = unidades vendidas / unidades compradas (%). Qué salió.
  - **meses_cobertura** = stock actual / (venta mensual en unidades). Cuánto
    dura el stock al ritmo de venta. Bajo = riesgo de quiebre; alto = capital
    dormido.

Asimetría deliberada de sucursales (ver `app/routers/_compras_filters.py`):
la COMPRA y el STOCK barren tiendas + almacén central (la mercadería entra y se
acumula ahí); la VENTA solo las tiendas que venden al público. Con `office_id`
las tres se acotan a esa sucursal.

El universo de "compra real" (sin traslados, sin ajustes, con importaciones,
con sanidad de cantidad/costo) lo define `build_compras_filter`, el mismo helper
que usará el resto del módulo de compras.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentCompany, get_current_company
from app.database import get_db
from app.routers._compras_filters import (
    build_compras_filter,
    scope_compras,
    scope_ventas,
)
from app.routers._office_scope import office_filter
from app.routers.config_admin import get_company
from app.services.attribute_catalog import enriched_view_sql
from harvester.config import TIPOS_VENTA as DEFAULT_TIPOS_VENTA

router = APIRouter(
    prefix="/compras",
    tags=["compras"],
    dependencies=[Depends(get_current_company)],
)

Nivel = Literal[
    "departamento", "categoria", "subcategoria",
    # Atributos (v_products_enriched): cruzan la taxonomía.
    "marca", "proveedor", "licencia", "origen", "publico", "temporada", "estilo", "material",
]

# Columnas de agrupación por nivel. `key` son las columnas de v_products_full
# (o v_products_enriched para los niveles de atributo) que entran al
# GROUP BY / SELECT; `label` es cómo se llama la fila en la respuesta.
_NIVEL_COLS: dict[str, list[str]] = {
    "departamento":  ["department"],
    "categoria":     ["department", "category"],
    "subcategoria":  ["department", "category", "subcategory"],
    "marca":         ["marca"],
    "proveedor":     ["proveedor"],
    "licencia":      ["licencia"],
    "origen":        ["origen"],
    "publico":       ["publico"],
    "temporada":     ["temporada"],
    "estilo":        ["estilo"],
    "material":      ["material"],
}
_NIVELES_TAXONOMIA = {"departamento", "categoria", "subcategoria"}


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _veredicto(mult: float | None, cobertura: float | None, compra: float, venta: float) -> str:
    """Etiqueta accionable a partir de las 3 métricas. El orden importa:
    primero los casos sin comparable, después riesgo de quiebre, después exceso."""
    if compra == 0 and venta > 0:
        return "SIN_REPOSICION"          # vende y no se repuso en la ventana
    if compra == 0 and venta == 0:
        return "SIN_MOVIMIENTO"
    if cobertura is not None and cobertura < 0.5:
        return "RIESGO_QUIEBRE"          # < ~2 semanas de stock
    if mult is not None and mult < 1.5:
        return "SOBRECOMPRA"             # se compró de más para lo que rota
    if cobertura is not None and cobertura > 2.5:
        return "EXCESO_STOCK"            # capital dormido
    return "EQUILIBRADO"


async def _ecosistema_data(
    db: AsyncSession,
    cid: int,
    nivel: Nivel,
    dias: int,
    office_id: int | None,
) -> dict:
    company_config = await get_company(db, cid)
    tipos_venta = company_config.get("tipos_venta") or DEFAULT_TIPOS_VENTA

    scope_venta = await scope_ventas(db, cid, office_id)     # solo tiendas
    scope_cs = await scope_compras(db, cid, office_id)       # tiendas + almacén
    cf = await build_compras_filter(db, cid)                 # compra real

    cols = _NIVEL_COLS[nivel]
    # Los niveles de atributo leen la vista enriquecida (con fallback en NULL si
    # la migración de atributos todavía no se aplicó).
    vista = "v_products_full" if nivel in _NIVELES_TAXONOMIA else await enriched_view_sql(db)
    sel_cols = ", ".join(f"vpf.{c}" for c in cols)
    sel_dims = ", ".join(cols)
    grp_cols = ", ".join(str(i + 1) for i in range(len(cols)))
    # Unión NULL-safe entre la tabla de dimensiones y cada CTE. Postgres no acepta
    # IS NOT DISTINCT FROM en FULL JOIN (no es hash/merge-joinable), pero sí en
    # LEFT JOIN — por eso se arma `dims` (UNION de las 3) y se hace LEFT JOIN.
    join_dv = " AND ".join(f"d.{c} IS NOT DISTINCT FROM v.{c}" for c in cols)
    join_dc = " AND ".join(f"d.{c} IS NOT DISTINCT FROM c.{c}" for c in cols)
    join_ds = " AND ".join(f"d.{c} IS NOT DISTINCT FROM s.{c}" for c in cols)
    meses = dias / 30.0

    params: dict[str, Any] = cf.merge({
        "cid": cid,
        "dias": dias,
        "tipos_venta": tipos_venta,
    })

    # Cada CTE agrega a su propio nivel; el FULL JOIN las une para no perder
    # categorías que solo compraron, solo vendieron o solo tienen stock.
    query = f"""
    WITH compra AS (
        SELECT {sel_cols},
               SUM(rd.quantity)           AS unds_compradas,
               SUM(rd.quantity * rd.cost) AS costo_comprado
          FROM reception_details rd
          JOIN receptions r ON r.company_id = rd.company_id
                           AND r.bsale_reception_id = rd.bsale_reception_id
          JOIN variants v ON v.company_id = rd.company_id
                         AND v.bsale_variant_id = rd.bsale_variant_id
          JOIN {vista} vpf ON vpf.company_id = v.company_id
                                  AND vpf.bsale_product_id = v.bsale_product_id
         WHERE rd.company_id = :cid
           AND r.admission_date >= CURRENT_DATE - make_interval(days => :dias)
           AND {office_filter(scope_cs, alias="r")}
           AND {cf.where}
         GROUP BY {grp_cols}
    ),
    venta AS (
        SELECT {sel_cols},
               SUM(dd.quantity)     AS unds_vendidas,
               SUM(dd.total_amount) AS venta_pen
          FROM document_details dd
          JOIN documents doc ON doc.company_id = dd.company_id
                            AND doc.bsale_document_id = dd.bsale_document_id
          JOIN variants v ON v.company_id = dd.company_id
                         AND v.bsale_variant_id = dd.bsale_variant_id
          JOIN {vista} vpf ON vpf.company_id = v.company_id
                                  AND vpf.bsale_product_id = v.bsale_product_id
         WHERE dd.company_id = :cid
           AND doc.emission_date >= CURRENT_DATE - make_interval(days => :dias)
           AND doc.is_active AND NOT doc.is_credit_note
           AND doc.bsale_document_type_id = ANY(:tipos_venta)
           AND {office_filter(scope_venta, alias="doc")}
         GROUP BY {grp_cols}
    ),
    stock AS (
        SELECT {sel_cols},
               SUM(sl.quantity)                                   AS unds_stock,
               SUM(sl.quantity * COALESCE(vc.effective_cost, 0))  AS stock_pen
          FROM stock_levels sl
          JOIN variants v ON v.company_id = sl.company_id
                         AND v.bsale_variant_id = sl.bsale_variant_id
          JOIN {vista} vpf ON vpf.company_id = v.company_id
                                  AND vpf.bsale_product_id = v.bsale_product_id
          LEFT JOIN variant_costs vc ON vc.company_id = sl.company_id
                                    AND vc.bsale_variant_id = sl.bsale_variant_id
         WHERE sl.company_id = :cid AND sl.quantity > 0
           AND {office_filter(scope_cs, alias="sl")}
         GROUP BY {grp_cols}
    ),
    dims AS (   -- todas las combinaciones que aparecen en cualquiera de las 3
        SELECT {sel_dims} FROM compra
        UNION SELECT {sel_dims} FROM venta
        UNION SELECT {sel_dims} FROM stock
    ),
    unido AS (
        SELECT {", ".join(f"d.{c}" for c in cols)},
               COALESCE(v.venta_pen, 0)        AS venta_pen,
               COALESCE(v.unds_vendidas, 0)    AS unds_vendidas,
               COALESCE(c.costo_comprado, 0)   AS compra_pen,
               COALESCE(c.unds_compradas, 0)   AS unds_compradas,
               COALESCE(s.stock_pen, 0)        AS stock_pen,
               COALESCE(s.unds_stock, 0)       AS unds_stock
          FROM dims d
          LEFT JOIN venta  v ON {join_dv}
          LEFT JOIN compra c ON {join_dc}
          LEFT JOIN stock  s ON {join_ds}
    )
    SELECT * FROM unido
     ORDER BY venta_pen DESC NULLS LAST
    """

    rows = (await db.execute(text(query), params)).mappings().all()

    items: list[dict] = []
    tot_venta = tot_compra = tot_stock = 0.0
    for r in rows:
        venta = _f(r["venta_pen"])
        compra = _f(r["compra_pen"])
        stock = _f(r["stock_pen"])
        unds_v = _f(r["unds_vendidas"])
        unds_c = _f(r["unds_compradas"])
        unds_s = _f(r["unds_stock"])
        tot_venta += venta
        tot_compra += compra
        tot_stock += stock

        mult = round(venta / compra, 2) if compra > 0 else None
        sellthrough = round(100.0 * unds_v / unds_c, 1) if unds_c > 0 else None
        venta_mensual_unds = unds_v / meses if meses else 0
        cobertura = round(unds_s / venta_mensual_unds, 1) if venta_mensual_unds > 0 else None

        item: dict[str, Any] = {c: r[c] for c in cols}
        item.update({
            "venta_pen":       round(venta, 2),
            "compra_pen":      round(compra, 2),
            "stock_pen":       round(stock, 2),
            "unds_vendidas":   round(unds_v),
            "unds_compradas":  round(unds_c),
            "unds_stock":      round(unds_s),
            "multiplicador":   mult,
            "sellthrough_pct": sellthrough,
            "meses_cobertura": cobertura,
            "veredicto":       _veredicto(mult, cobertura, compra, venta),
        })
        items.append(item)

    # KPIs de cabecera + conteo por veredicto (para chips en la UI).
    por_veredicto: dict[str, int] = {}
    for it in items:
        por_veredicto[it["veredicto"]] = por_veredicto.get(it["veredicto"], 0) + 1

    return {
        "meta": {
            "generado_at":  datetime.now(timezone.utc).isoformat(),
            "nivel":        nivel,
            "dias":         dias,
            "office_id":    office_id,
            "office_scope_compra": scope_cs,
            "office_scope_venta":  scope_venta,
            "nota": (
                "Compra y stock incluyen almacén central; venta solo tiendas. "
                "Compra real: sin traslados, sin ajustes, con importaciones, con "
                "sanidad de cantidad/costo."
            ),
        },
        "kpis": {
            "venta_total_pen":       round(tot_venta, 2),
            "compra_total_pen":      round(tot_compra, 2),
            "stock_total_pen":       round(tot_stock, 2),
            "multiplicador_global":  round(tot_venta / tot_compra, 2) if tot_compra > 0 else None,
            "filas":                 len(items),
            "por_veredicto":         por_veredicto,
        },
        "items": items,
    }


@router.get("/ecosistema")
async def ecosistema(
    nivel: Nivel = Query("categoria", description="Nivel de agregación."),
    dias: int = Query(365, ge=30, le=730, description="Ventana de compra y venta (días hacia atrás)."),
    office_id: int | None = Query(None, description="Filtra a una sucursal (vacío = scope de la empresa)."),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Compra ↔ venta ↔ stock por `nivel`, con multiplicador, sellthrough,
    cobertura y veredicto por fila. Ordenado por venta desc."""
    return await _ecosistema_data(db, company.company_id, nivel, dias, office_id)


@router.get("/ecosistema/excel", response_class=StreamingResponse)
async def ecosistema_excel(
    nivel: Nivel = Query("categoria", description="Nivel de agregación."),
    dias: int = Query(365, ge=30, le=730, description="Ventana de compra y venta (días hacia atrás)."),
    office_id: int | None = Query(None, description="Filtra a una sucursal (vacío = scope de la empresa)."),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Mismo contenido que el ecosistema JSON, como Excel descargable."""
    from analytics.excel_ecosistema import build_ecosistema_workbook

    cid = company.company_id
    data = await _ecosistema_data(db, cid, nivel, dias, office_id)
    
    sucursal = None
    if office_id is not None:
        sucursal = await db.scalar(
            text("SELECT name FROM offices WHERE company_id = :cid AND bsale_office_id = :oid"),
            {"cid": cid, "oid": office_id},
        )
        
    wb = build_ecosistema_workbook(
        items=data["items"],
        meta=data["meta"],
        kpis=data["kpis"],
        sucursal_nombre=sucursal
    )
    
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    
    fecha = datetime.now().strftime("%Y-%m-%d")
    filename = f"ecosistema_compras_{nivel}_{fecha}.xlsx"
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ══════════════════════════════════════════════════════════════════════════════
# CANASTA — qué se llevan juntos los clientes (market-basket por categoría)
#
# Responde "¿qué categorías se compran juntas?" para decidir exhibición cruzada,
# combos y reposición conjunta. La métrica que importa es el LIFT, no el conteo:
# una categoría grande (ej. Higiene Personal) aparece en muchos tickets solo por
# tamaño; el lift divide por la frecuencia base de cada una y deja arriba los
# pares que de verdad se buscan juntos.
#
#   soporte  = P(A y B)          → % de tickets que llevan ambas
#   confianza(A→B) = P(B|A)      → de los que llevan A, cuántos llevan B
#   lift     = P(A y B) / (P(A)·P(B))
#              > 1 se compran juntas más de lo esperado por azar (afinidad real)
#              ≈ 1 independientes · < 1 se evitan
#
# Rendimiento: la matriz de co-ocurrencia hace un self-join sobre las líneas del
# período. Los CTE van MATERIALIZED a propósito —`lineas` se referencia 3 veces y
# sin materializar Postgres la recomputaría cada vez (la vista de catálogo es
# cara). El endpoint fija un statement_timeout de guarda para que una ventana
# grande nunca deje una query colgada. Cachear en el frontend por
# (nivel, desde, hasta, office_id): los datos no cambian hasta el sync nocturno.
# ══════════════════════════════════════════════════════════════════════════════

# Guarda de tiempo (ms) para las queries pesadas de canasta. Si se excede,
# el endpoint responde 503 pidiendo achicar la ventana en vez de colgarse.
_CANASTA_TIMEOUT_MS = 30_000

# Máximo de categorías distintas por ticket para entrar al análisis de afinidad.
# Un ticket con más es casi seguro mayoreo / consumo interno / artefacto, no una
# compra real de cliente; excluirlo acota el cómputo de pares y limpia la señal.
_CANASTA_BASKET_CAP = 20

# Expresión de agrupación por nivel. Subcategoría se cualifica con su categoría
# para no fundir dos subcategorías homónimas de departamentos distintos.
_CANASTA_GRP: dict[str, str] = {
    "categoria":    "vpf.category",
    "subcategoria": "vpf.category || ' › ' || vpf.subcategory",
}
_CANASTA_NULLGUARD: dict[str, str] = {
    "categoria":    "vpf.category IS NOT NULL",
    "subcategoria": "vpf.category IS NOT NULL AND vpf.subcategory IS NOT NULL",
}

CanastaNivel = Literal["categoria", "subcategoria"]


def _interpretar_lift(lift: float) -> str:
    if lift >= 2.0:
        return "afinidad_muy_fuerte"
    if lift >= 1.5:
        return "afinidad_fuerte"
    return "afinidad_moderada"


def _resolver_ventana(
    fecha_desde: str | None, fecha_hasta: str | None, dias: int
) -> tuple[date, date]:
    """Devuelve (desde, hasta_exclusive). Si vienen ambas fechas, se usan tal
    cual (inclusivas). Si no, los últimos `dias` cerrando ayer (hoy es parcial)."""
    if fecha_desde and fecha_hasta:
        try:
            d = date.fromisoformat(fecha_desde)
            h = date.fromisoformat(fecha_hasta)
        except ValueError:
            raise HTTPException(status_code=400, detail="Fechas inválidas; usar YYYY-MM-DD.")
        if d > h:
            raise HTTPException(status_code=400, detail="fecha_desde no puede ser mayor que fecha_hasta.")
        return d, h + timedelta(days=1)
    if fecha_desde or fecha_hasta:
        raise HTTPException(
            status_code=400,
            detail="Pasá fecha_desde Y fecha_hasta juntas, o ninguna (se usa `dias`).",
        )
    hoy = date.today()
    return hoy - timedelta(days=dias), hoy  # cierra ayer (hoy exclusivo)


@router.get("/canasta/meses")
async def canasta_meses(
    office_id: int | None = Query(None),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Meses con datos de venta, para poblar el selector de rango del frontend.
    Ligero: solo agrega `documents`, sin tocar catálogo."""
    cid = company.company_id
    company_config = await get_company(db, cid)
    tipos_venta = company_config.get("tipos_venta") or DEFAULT_TIPOS_VENTA
    scope = await scope_ventas(db, cid, office_id)

    rows = (await db.execute(text(f"""
        SELECT to_char(emission_date, 'YYYY-MM')       AS mes,
               COUNT(*)                                 AS tickets,
               ROUND(SUM(total_amount)::numeric, 0)     AS ventas_pen
          FROM documents
         WHERE company_id = :cid AND is_active AND NOT is_credit_note
           AND bsale_document_type_id = ANY(:tipos_venta)
           AND {office_filter(scope)}
         GROUP BY 1 ORDER BY 1
    """), {"cid": cid, "tipos_venta": tipos_venta})).mappings().all()

    meses = [
        {"mes": r["mes"], "tickets": int(r["tickets"]), "ventas_pen": float(r["ventas_pen"] or 0)}
        for r in rows
    ]
    return {
        "office_id": office_id,
        "office_scope": scope,
        "meses": meses,
        "primer_mes": meses[0]["mes"] if meses else None,
        "ultimo_mes": meses[-1]["mes"] if meses else None,
    }


@router.get("/canasta")
async def canasta(
    nivel: CanastaNivel = Query("categoria", description="Nivel de afinidad."),
    dias: int = Query(90, ge=7, le=365, description="Ventana si no se pasan fechas."),
    fecha_desde: str | None = Query(None, description="Inicio YYYY-MM-DD (con fecha_hasta)."),
    fecha_hasta: str | None = Query(None, description="Fin YYYY-MM-DD, inclusivo."),
    office_id: int | None = Query(None, description="Filtra a una sucursal."),
    min_soporte: int = Query(15, ge=1, le=1000, description="Mínimo de tickets juntos para considerar un par."),
    lift_min: float = Query(1.2, ge=1.0, le=10.0, description="Lift mínimo para reportar afinidad."),
    top: int = Query(40, ge=1, le=300, description="Cuántos pares devolver."),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Afinidad entre categorías (o subcategorías) en la ventana elegida:
    pares con soporte/confianza/lift, perfil de canasta y mejor acompañante por
    categoría. Ver el bloque de comentarios arriba para la metodología."""
    cid = company.company_id
    company_config = await get_company(db, cid)
    tipos_venta = company_config.get("tipos_venta") or DEFAULT_TIPOS_VENTA
    scope = await scope_ventas(db, cid, office_id)
    desde, hasta = _resolver_ventana(fecha_desde, fecha_hasta, dias)

    grp = _CANASTA_GRP[nivel]
    nullguard = _CANASTA_NULLGUARD[nivel]
    of_doc = office_filter(scope, alias="doc")
    # Fechas embebidas como literales `::date`, NO como bind params. Con un bind
    # param, la matriz de afinidad recibía un plan genérico que para la
    # distribución de algunas empresas degeneraba en horas de cómputo; con el
    # literal el planner ve el valor real y elige un plan sano. `desde`/`hasta`
    # son `date` validados (nunca texto libre del usuario), así que interpolarlos
    # es seguro frente a inyección.
    desde_lit = f"'{desde.isoformat()}'::date"
    hasta_lit = f"'{hasta.isoformat()}'::date"
    params: dict[str, Any] = {
        "cid": cid, "tipos_venta": tipos_venta, "min_soporte": min_soporte,
    }

    # Guarda de tiempo: una ventana enorme aborta limpio en vez de colgar la
    # conexión. SET LOCAL vive solo esta transacción.
    await db.execute(text(f"SET LOCAL statement_timeout = {_CANASTA_TIMEOUT_MS}"))

    # ── Perfil de canasta: productos por ticket (sin tocar catálogo → barato) ──
    perfil_row = (await db.execute(text(f"""
        WITH tk AS (
            SELECT dd.bsale_document_id AS doc,
                   COUNT(*)             AS n_prod,
                   SUM(dd.total_amount) AS monto
              FROM documents doc
              JOIN document_details dd ON dd.company_id = doc.company_id
                                      AND dd.bsale_document_id = doc.bsale_document_id
             WHERE doc.company_id = :cid
               AND doc.emission_date >= {desde_lit} AND doc.emission_date < {hasta_lit}
               AND doc.is_active AND NOT doc.is_credit_note
               AND doc.bsale_document_type_id = ANY(:tipos_venta)
               AND {of_doc}
             GROUP BY dd.bsale_document_id
        )
        SELECT COUNT(*)                                            AS tickets,
               COUNT(*) FILTER (WHERE n_prod = 1)                  AS t1,
               COUNT(*) FILTER (WHERE n_prod = 2)                  AS t2,
               COUNT(*) FILTER (WHERE n_prod = 3)                  AS t3,
               COUNT(*) FILTER (WHERE n_prod >= 4)                 AS t4mas,
               ROUND(AVG(n_prod), 2)                               AS prod_promedio,
               ROUND(AVG(monto)::numeric, 2)                       AS ticket_promedio,
               ROUND(AVG(monto) FILTER (WHERE n_prod >= 2)::numeric, 2) AS ticket_promedio_multi
          FROM tk
    """), params)).mappings().one()

    # ── Matriz de afinidad (lift) ─────────────────────────────────────────────
    # Estructura pensada para no degenerar en ninguna empresa:
    #  · var_cat: mapa variante→grupo (~miles de filas) MATERIALIZED una vez, para
    #    no expandir la vista de catálogo (5 tablas) por cada línea de venta.
    #  · lineas MATERIALIZED: se referencia varias veces; sin materializar Postgres
    #    la recomputaría cada vez.
    #  · pares vía expansión LATERAL de un array de categorías por ticket, en vez
    #    de un self-join de `lineas`. El self-join elegía un nested-loop
    #    catastrófico en empresas con pocas filas (estimaciones malas sobre un CTE
    #    sin estadísticas); la expansión por array tiene costo acotado por el
    #    número real de pares.
    #  · cap de tamaño de canasta (`_CANASTA_BASKET_CAP`): un ticket con más de N
    #    categorías distintas casi nunca es una compra real de cliente (es mayoreo,
    #    consumo de personal o un artefacto). Excluirlo acota la expansión de pares
    #    Y mejora la señal de afinidad.
    try:
        pares_rows = (await db.execute(text(f"""
            WITH var_cat AS MATERIALIZED (
                SELECT v.bsale_variant_id AS vid, {grp} AS grp
                  FROM variants v
                  JOIN v_products_full vpf ON vpf.company_id = v.company_id
                                          AND vpf.bsale_product_id = v.bsale_product_id
                 WHERE v.company_id = :cid AND {nullguard}
            ),
            lineas AS MATERIALIZED (
                SELECT DISTINCT dd.bsale_document_id AS doc, vc.grp
                  FROM documents doc
                  JOIN document_details dd ON dd.company_id = doc.company_id
                                          AND dd.bsale_document_id = doc.bsale_document_id
                  JOIN var_cat vc ON vc.vid = dd.bsale_variant_id
                 WHERE doc.company_id = :cid
                   AND doc.emission_date >= {desde_lit} AND doc.emission_date < {hasta_lit}
                   AND doc.is_active AND NOT doc.is_credit_note
                   AND doc.bsale_document_type_id = ANY(:tipos_venta)
                   AND {of_doc}
            ),
            arrs AS (
                SELECT doc, array_agg(grp ORDER BY grp) AS g
                  FROM lineas
                 GROUP BY doc
                HAVING COUNT(*) BETWEEN 2 AND {_CANASTA_BASKET_CAP}
            ),
            total AS (SELECT COUNT(DISTINCT doc)::numeric AS n FROM lineas),
            solos AS (SELECT grp, COUNT(*)::numeric AS n FROM lineas GROUP BY grp),
            pares AS (
                SELECT g[i] AS ga, g[j] AS gb, COUNT(*)::numeric AS juntos
                  FROM arrs,
                       LATERAL generate_series(1, array_length(g, 1) - 1) AS i,
                       LATERAL generate_series(i + 1, array_length(g, 1)) AS j
                 GROUP BY g[i], g[j]
                HAVING COUNT(*) >= :min_soporte
            )
            SELECT p.ga, p.gb, p.juntos::int                        AS juntos,
                   ROUND(100 * p.juntos / t.n, 2)                   AS soporte_pct,
                   ROUND(100 * p.juntos / sa.n, 1)                  AS confianza_a_b,
                   ROUND(100 * p.juntos / sb.n, 1)                  AS confianza_b_a,
                   ROUND((p.juntos * t.n) / (sa.n * sb.n), 2)       AS lift,
                   sa.n::int                                        AS tickets_a,
                   sb.n::int                                        AS tickets_b
              FROM pares p
              CROSS JOIN total t
              JOIN solos sa ON sa.grp = p.ga
              JOIN solos sb ON sb.grp = p.gb
             WHERE (p.juntos * t.n) / (sa.n * sb.n) >= :lift_min
             ORDER BY lift DESC
        """), {**params, "lift_min": lift_min})).mappings().all()
    except Exception as exc:
        # statement_timeout u otro error de la query pesada → 503 accionable.
        if "statement timeout" in str(exc).lower() or "canceling statement" in str(exc).lower():
            raise HTTPException(
                status_code=503,
                detail=(
                    "La canasta tardó demasiado para esta ventana. Probá un rango "
                    "más corto (menos días o un mes puntual) o filtrá por sucursal."
                ),
            )
        raise

    # ── Ensamblado ────────────────────────────────────────────────────────────
    pares: list[dict] = []
    # mejor_acompanante[grp] = (partner, lift, confianza, tickets_juntos)
    mejor: dict[str, dict] = {}
    for r in pares_rows:
        ga, gb = r["ga"], r["gb"]
        lift = float(r["lift"])
        par = {
            "a": ga, "b": gb,
            "juntos": int(r["juntos"]),
            "soporte_pct": float(r["soporte_pct"]),
            "confianza_a_b": float(r["confianza_a_b"]),
            "confianza_b_a": float(r["confianza_b_a"]),
            "lift": lift,
            "interpretacion": _interpretar_lift(lift),
        }
        pares.append(par)
        # Mejor acompañante por categoría, en ambas direcciones (por lift).
        for x, y, conf in ((ga, gb, par["confianza_a_b"]), (gb, ga, par["confianza_b_a"])):
            cur = mejor.get(x)
            if cur is None or lift > cur["lift"]:
                mejor[x] = {"categoria": x, "acompanante": y, "lift": lift,
                            "confianza_pct": conf, "tickets_juntos": par["juntos"]}

    pares.sort(key=lambda p: p["lift"], reverse=True)
    por_categoria = sorted(mejor.values(), key=lambda m: m["lift"], reverse=True)

    tickets = int(perfil_row["tickets"] or 0)
    multi = tickets - int(perfil_row["t1"] or 0)
    n_dias = (hasta - desde).days

    return {
        "meta": {
            "generado_at":   datetime.now(timezone.utc).isoformat(),
            "nivel":         nivel,
            "desde":         desde.isoformat(),
            "hasta":         (hasta - timedelta(days=1)).isoformat(),  # inclusivo para mostrar
            "dias":          n_dias,
            "office_id":     office_id,
            "office_scope":  scope,
            "parametros":    {"min_soporte": min_soporte, "lift_min": lift_min, "top": top},
            "nota": (
                "El lift es la métrica de afinidad: >1 = se compran juntas más de "
                "lo esperado por azar. El conteo crudo favorece a las categorías "
                "grandes; por eso se ordena y filtra por lift, no por 'juntos'."
            ),
        },
        "perfil_canasta": {
            "tickets":               tickets,
            "tickets_multiproducto": multi,
            "pct_multiproducto":     round(100 * multi / tickets, 1) if tickets else 0.0,
            "productos_por_ticket":  float(perfil_row["prod_promedio"] or 0),
            "ticket_promedio_pen":   float(perfil_row["ticket_promedio"] or 0),
            "ticket_promedio_multi_pen": float(perfil_row["ticket_promedio_multi"] or 0),
            "distribucion": [
                {"productos": "1",   "tickets": int(perfil_row["t1"] or 0)},
                {"productos": "2",   "tickets": int(perfil_row["t2"] or 0)},
                {"productos": "3",   "tickets": int(perfil_row["t3"] or 0)},
                {"productos": "4+",  "tickets": int(perfil_row["t4mas"] or 0)},
            ],
        },
        "pares": pares[:top],
        "por_categoria": por_categoria,
        "resumen": _resumen_canasta(pares, por_categoria, tickets, multi, nivel),
    }


def _resumen_canasta(
    pares: list[dict], por_categoria: list[dict], tickets: int, multi: int, nivel: str
) -> list[str]:
    """Bullets en lenguaje llano para el encabezado de la vista."""
    out: list[str] = []
    if tickets:
        out.append(
            f"{multi:,} de {tickets:,} tickets ({round(100*multi/tickets):.0f}%) "
            f"llevan más de un producto — ahí vive la venta cruzada."
        )
    if pares:
        top = pares[0]
        out.append(
            f"La afinidad más fuerte es «{top['a']}» + «{top['b']}»: "
            f"aparecen juntas {top['juntos']:,} veces (lift {top['lift']}). "
            f"De quien lleva «{top['a']}», el {top['confianza_a_b']:.0f}% también lleva «{top['b']}»."
        )
        fuertes = [p for p in pares if p["lift"] >= 1.5]
        if fuertes:
            out.append(
                f"{len(fuertes)} par(es) con afinidad fuerte (lift ≥ 1.5) — "
                f"candidatos a exhibición cruzada o combo."
            )
    else:
        out.append(
            "No se detectaron pares con afinidad sobre el umbral en esta ventana. "
            "Probá bajar `min_soporte` o ampliar el rango."
        )
    return out


# ══════════════════════════════════════════════════════════════════════════════
# COMPARADOR TEMPORAL — dos ventanas de fecha arbitrarias, cara a cara
#
# Responde: "en tal fecha (un mes del año pasado, una semana buena, lo que sea)
# se vendía bien X; ¿cómo viene ahora y qué me queda en stock?". Cruza:
#   · Ventana A (referencia) : venta + rotación de un rango de fechas cualquiera.
#   · Ventana B (comparación): venta + rotación de otro rango (por defecto, el
#     tramo reciente del mismo largo, terminando ayer).
#   · Stock ACTUAL           : lo que hay hoy (stock_levels).
#   · Stock de la ventana A   : bonus, solo si A cae dentro de stock_history
#     (arranca 2026-07-02); para fechas más viejas no existe y va en null.
#
# De ahí sale la sugerencia de recompra del ejemplo del gerente: si en A se
# vendía fuerte y hoy queda poco stock, `sugerencia_compra > 0`; el `veredicto`
# avisa además si la demanda SIGUE (reponer) o CAYÓ (revisar antes de comprar).
#
# Reutiliza el patrón de `/analytics/compras-historico/excel` (venta de ventana
# × stock actual × sugerencia) y lo extiende con la segunda ventana y la rotación
# comparada. Todo por SKU, agrupado por jerarquía; descargable en Excel.
# ══════════════════════════════════════════════════════════════════════════════

_COMPARADOR_TIMEOUT_MS = 30_000

ComparadorPreset = Literal["semana_anterior", "mes_anterior", "mismo_mes_ano_pasado"]


def _preset_ventana_a(preset: str, hoy: date) -> tuple[date, date]:
    """Llena la ventana A (referencia) desde un preset. `hoy` se inyecta para
    no llamar date.today() acá dentro (testeable)."""
    if preset == "semana_anterior":
        inicio_semana = hoy - timedelta(days=hoy.weekday())   # lunes de esta semana
        fin = inicio_semana - timedelta(days=1)               # domingo pasado
        return fin - timedelta(days=6), fin
    if preset == "mes_anterior":
        primero_este = hoy.replace(day=1)
        fin = primero_este - timedelta(days=1)                # último día del mes pasado
        return fin.replace(day=1), fin
    if preset == "mismo_mes_ano_pasado":
        y, m = hoy.year - 1, hoy.month
        desde = date(y, m, 1)
        fin = date(y + (m // 12), (m % 12) + 1, 1) - timedelta(days=1)
        return desde, fin
    raise HTTPException(400, f"preset desconocido: {preset}")


def _parse_fecha(s: str, campo: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(400, f"{campo} inválida; usar YYYY-MM-DD.")


def _resolver_ventanas_comparador(
    a_desde: str | None, a_hasta: str | None,
    b_desde: str | None, b_hasta: str | None,
    preset: str | None, hoy: date,
) -> tuple[date, date, date, date]:
    """Devuelve (a0, a1, b0, b1) con extremos inclusivos.

    Ventana A: por `preset` o por (a_desde, a_hasta). Ventana B: explícita si
    vienen ambas; si no, el tramo reciente del mismo largo que A cerrando ayer.
    """
    if preset:
        a0, a1 = _preset_ventana_a(preset, hoy)
    else:
        if not (a_desde and a_hasta):
            raise HTTPException(400, "Pasá `preset`, o `a_desde` y `a_hasta` juntas.")
        a0, a1 = _parse_fecha(a_desde, "a_desde"), _parse_fecha(a_hasta, "a_hasta")
    if a1 < a0:
        raise HTTPException(400, "a_desde no puede ser mayor que a_hasta.")

    if b_desde and b_hasta:
        b0, b1 = _parse_fecha(b_desde, "b_desde"), _parse_fecha(b_hasta, "b_hasta")
        if b1 < b0:
            raise HTTPException(400, "b_desde no puede ser mayor que b_hasta.")
    elif b_desde or b_hasta:
        raise HTTPException(400, "Pasá `b_desde` y `b_hasta` juntas, o ninguna.")
    else:
        largo = (a1 - a0).days                 # mismo largo que A
        b1 = hoy - timedelta(days=1)           # cierra ayer (hoy es parcial)
        b0 = b1 - timedelta(days=largo)
    return a0, a1, b0, b1


def _pct(nuevo: float, viejo: float) -> float | None:
    return round((nuevo - viejo) / viejo * 100, 1) if viejo else None


def _veredicto_comparador(
    vel_a: float, vel_b: float, stock: float, sugerencia: float, cob: int
) -> str:
    """Decisión de recompra a partir de rotación de referencia (A), rotación
    actual (B) y stock. El orden codifica la prioridad."""
    if vel_a == 0 and vel_b == 0:
        return "SIN_MOVIMIENTO"
    if sugerencia > 0:
        # Falta stock para cubrir la demanda de referencia. ¿Sigue vendiéndose?
        if vel_b >= vel_a * 0.6:
            return "REPONER"            # vendía bien y sigue, queda poco
        return "REVISAR_DEMANDA"        # vendía bien pero cayó — ¿estacional/discontinuado?
    if vel_b > 0 and vel_a <= vel_b * 0.5:
        return "EN_ALZA"                # ahora rota más que en la referencia
    cobertura_actual = (stock / vel_b) if vel_b > 0 else None
    if cobertura_actual is not None and cobertura_actual > cob * 2.5:
        return "EXCESO_STOCK"           # mucho stock para lo que rota hoy
    return "OK"


def _enrich_sku_comparador(r: dict, dias_a: int, dias_b: int, cob: int) -> dict:
    unds_a = _f(r.get("unds_a")); soles_a = _f(r.get("soles_a"))
    unds_b = _f(r.get("unds_b")); soles_b = _f(r.get("soles_b"))
    stock = _f(r.get("stock_now_unds")); stock_val = _f(r.get("stock_now_valor"))
    stock_a = r.get("stock_a_unds")     # None si la ventana A no tiene histórico

    vel_a = unds_a / dias_a if dias_a else 0.0
    vel_b = unds_b / dias_b if dias_b else 0.0
    cobertura = round(stock / vel_b, 1) if vel_b > 0 else None
    sugerencia = max(0, round(vel_a * cob - stock))
    return {
        "departamento": r.get("departamento") or "Sin Dpto",
        "categoria":    r.get("categoria") or "Sin Cat",
        "subcategoria": r.get("subcategoria") or "Sin SubCat",
        "sku":          r.get("sku"),
        "producto":     r.get("producto"),
        "venta_a_pen":  round(soles_a, 2),
        "venta_b_pen":  round(soles_b, 2),
        "unds_a":       round(unds_a),
        "unds_b":       round(unds_b),
        "velocidad_a":  round(vel_a, 2),
        "velocidad_b":  round(vel_b, 2),
        "delta_venta_pct":      _pct(soles_b, soles_a),
        "delta_velocidad_pct":  _pct(vel_b, vel_a),
        "stock_actual_unds":    round(stock),
        "stock_actual_valor":   round(stock_val, 2),
        "stock_ventana_a_unds": round(_f(stock_a)) if stock_a is not None else None,
        "cobertura_dias":       cobertura,
        "sugerencia_compra":    sugerencia,
        "veredicto":            _veredicto_comparador(vel_a, vel_b, stock, sugerencia, cob),
    }


async def _comparador_rows(
    db: AsyncSession, cid: int, company_config: dict,
    a0: date, a1: date, b0: date, b1: date,
    office_id: int | None, cob: int,
) -> tuple[list[dict], list[str]]:
    """Corre la comparación a nivel SKU y devuelve (skus_enriquecidos, alertas).
    Compartido por el endpoint JSON y el Excel."""
    tipos_venta = company_config.get("tipos_venta") or DEFAULT_TIPOS_VENTA
    scope = await scope_ventas(db, cid, office_id)
    of_doc = office_filter(scope, alias="doc")
    of_sl = office_filter(scope, alias="sl")

    # Fechas como literales `::date` (seguras: son `date` validados). Los extremos
    # superiores van +1 día exclusivos → comparación sargable contra el índice.
    a0l = f"'{a0.isoformat()}'::date"
    a1l = f"'{a1.isoformat()}'::date"                                   # inclusivo, para el snapshot
    a1x = f"'{(a1 + timedelta(days=1)).isoformat()}'::date"
    b0l = f"'{b0.isoformat()}'::date"
    b1x = f"'{(b1 + timedelta(days=1)).isoformat()}'::date"

    # ¿La ventana A cae dentro del histórico de stock? Si no, stock_a va null.
    min_snap = await db.scalar(
        text("SELECT MIN(snapshot_date) FROM stock_history WHERE company_id = :cid"),
        {"cid": cid},
    )
    a_tiene_historia = min_snap is not None and a1 >= min_snap
    alertas: list[str] = []
    if not a_tiene_historia:
        alertas.append(
            "El stock de la ventana A no está disponible (el histórico de stock "
            f"arranca el {min_snap.isoformat() if min_snap else 'N/D'}); se compara "
            "la demanda de A contra el stock actual."
        )

    if a_tiene_historia:
        # Stock al cierre de A: por (variante, sucursal) el snapshot más reciente
        # <= a1, sumado por variante.
        stock_a_cte = f"""
        , stock_a AS (
            SELECT vid, SUM(qty) AS unds FROM (
                SELECT DISTINCT ON (sh.bsale_variant_id, sh.bsale_office_id)
                       sh.bsale_variant_id AS vid, sh.quantity_available AS qty
                  FROM stock_history sh
                 WHERE sh.company_id = :cid AND sh.bsale_office_id = ANY(:scope_ids)
                   AND sh.snapshot_date <= {a1l}
                 ORDER BY sh.bsale_variant_id, sh.bsale_office_id, sh.snapshot_date DESC
            ) z GROUP BY vid
        )"""
        stock_a_join = "LEFT JOIN stock_a sa ON sa.vid = vids.vid"
        stock_a_sel = "sa.unds AS stock_a_unds,"
    else:
        stock_a_cte = ""
        stock_a_join = ""
        stock_a_sel = "NULL::numeric AS stock_a_unds,"

    params: dict[str, Any] = {
        "cid": cid, "tipos_venta": tipos_venta, "scope_ids": scope,
    }
    query = f"""
    WITH va AS (
        SELECT dd.bsale_variant_id vid, SUM(dd.quantity) unds, SUM(dd.total_amount) soles
          FROM documents doc
          JOIN document_details dd ON dd.company_id = doc.company_id
                                  AND dd.bsale_document_id = doc.bsale_document_id
         WHERE doc.company_id = :cid AND doc.is_active AND NOT doc.is_credit_note
           AND doc.bsale_document_type_id = ANY(:tipos_venta)
           AND doc.emission_date >= {a0l} AND doc.emission_date < {a1x}
           AND {of_doc}
         GROUP BY 1
    ),
    vb AS (
        SELECT dd.bsale_variant_id vid, SUM(dd.quantity) unds, SUM(dd.total_amount) soles
          FROM documents doc
          JOIN document_details dd ON dd.company_id = doc.company_id
                                  AND dd.bsale_document_id = doc.bsale_document_id
         WHERE doc.company_id = :cid AND doc.is_active AND NOT doc.is_credit_note
           AND doc.bsale_document_type_id = ANY(:tipos_venta)
           AND doc.emission_date >= {b0l} AND doc.emission_date < {b1x}
           AND {of_doc}
         GROUP BY 1
    ),
    stock_now AS (
        SELECT sl.bsale_variant_id vid, SUM(sl.quantity_available) unds,
               SUM(sl.quantity_available * COALESCE(vc.effective_cost, 0)) valor
          FROM stock_levels sl
          LEFT JOIN variant_costs vc ON vc.company_id = sl.company_id
                                    AND vc.bsale_variant_id = sl.bsale_variant_id
         WHERE sl.company_id = :cid AND {of_sl}
         GROUP BY 1
    ),
    vids AS (SELECT vid FROM va UNION SELECT vid FROM vb){stock_a_cte}
    SELECT COALESCE(v.code, '')                                     AS sku,
           COALESCE(vpf.product_name, v.description, 'Sin nombre')  AS producto,
           vpf.department AS departamento, vpf.category AS categoria,
           vpf.subcategory AS subcategoria,
           va.unds AS unds_a, va.soles AS soles_a,
           vb.unds AS unds_b, vb.soles AS soles_b,
           sn.unds AS stock_now_unds, sn.valor AS stock_now_valor,
           {stock_a_sel}
           v.bsale_variant_id AS vid
      FROM vids
      JOIN variants v ON v.company_id = :cid AND v.bsale_variant_id = vids.vid
      LEFT JOIN v_products_full vpf ON vpf.company_id = :cid
                                   AND vpf.bsale_product_id = v.bsale_product_id
      LEFT JOIN va ON va.vid = vids.vid
      LEFT JOIN vb ON vb.vid = vids.vid
      LEFT JOIN stock_now sn ON sn.vid = vids.vid
      {stock_a_join}
    """

    dias_a = (a1 - a0).days + 1
    dias_b = (b1 - b0).days + 1
    await db.execute(text(f"SET LOCAL statement_timeout = {_COMPARADOR_TIMEOUT_MS}"))
    try:
        rows = (await db.execute(text(query), params)).mappings().all()
    except Exception as exc:
        if "statement timeout" in str(exc).lower() or "canceling statement" in str(exc).lower():
            raise HTTPException(
                503,
                "El comparador tardó demasiado. Probá ventanas más cortas o filtrá por sucursal.",
            )
        raise

    skus = [_enrich_sku_comparador(dict(r), dias_a, dias_b, cob) for r in rows]

    # Alerta de aperturas escalonadas: si A arranca antes de la primera venta del
    # negocio, parte del período no tiene datos y el total queda sesgado.
    primera_venta = await db.scalar(
        text("""SELECT MIN(emission_date)::date FROM documents
                 WHERE company_id = :cid AND is_active AND NOT is_credit_note"""),
        {"cid": cid},
    )
    if primera_venta and a0 < primera_venta:
        alertas.append(
            f"La ventana A empieza antes de la primera venta registrada "
            f"({primera_venta.isoformat()}); parte del período no tiene datos."
        )
    return skus, alertas


def _agrupar_comparador(skus: list[dict], keys: list[str]) -> list[dict]:
    """Agrega los SKUs por una jerarquía (`keys` = ['departamento'] o
    ['departamento','categoria'], etc.), sumando montos/unidades y stock."""
    grupos: dict[tuple, dict] = {}
    for s in skus:
        k = tuple(s[key] for key in keys)
        g = grupos.setdefault(k, {
            **{key: s[key] for key in keys},
            "skus": 0, "venta_a_pen": 0.0, "venta_b_pen": 0.0,
            "unds_a": 0, "unds_b": 0, "stock_actual_unds": 0,
            "stock_actual_valor": 0.0, "sugerencia_compra": 0,
        })
        g["skus"] += 1
        g["venta_a_pen"] += s["venta_a_pen"]
        g["venta_b_pen"] += s["venta_b_pen"]
        g["unds_a"] += s["unds_a"]
        g["unds_b"] += s["unds_b"]
        g["stock_actual_unds"] += s["stock_actual_unds"]
        g["stock_actual_valor"] += s["stock_actual_valor"]
        g["sugerencia_compra"] += s["sugerencia_compra"]
    out = []
    for g in grupos.values():
        g["venta_a_pen"] = round(g["venta_a_pen"], 2)
        g["venta_b_pen"] = round(g["venta_b_pen"], 2)
        g["stock_actual_valor"] = round(g["stock_actual_valor"], 2)
        g["delta_venta_pct"] = _pct(g["venta_b_pen"], g["venta_a_pen"])
        out.append(g)
    out.sort(key=lambda x: x["venta_a_pen"], reverse=True)
    return out


def _meta_comparador(
    a0: date, a1: date, b0: date, b1: date, office_id: int | None,
    scope: list[int], cob: int, alertas: list[str],
) -> dict:
    return {
        "generado_at": datetime.now(timezone.utc).isoformat(),
        "ventana_a": {"desde": a0.isoformat(), "hasta": a1.isoformat(),
                      "dias": (a1 - a0).days + 1},
        "ventana_b": {"desde": b0.isoformat(), "hasta": b1.isoformat(),
                      "dias": (b1 - b0).days + 1},
        "office_id": office_id,
        "office_scope": scope,
        "dias_cobertura_objetivo": cob,
        "alertas": alertas,
        "nota": (
            "Ventana A = referencia; Ventana B = comparación (por defecto, el tramo "
            "reciente del mismo largo). velocidad = uds/día. sugerencia_compra "
            "cubre los días de cobertura objetivo al ritmo de A, menos el stock "
            "actual. El veredicto avisa si la demanda sigue (REPONER) o cayó "
            "(REVISAR_DEMANDA)."
        ),
    }


@router.get("/comparador")
async def comparador(
    a_desde: str | None = Query(None, description="Ventana A (referencia) inicio YYYY-MM-DD."),
    a_hasta: str | None = Query(None, description="Ventana A fin, inclusivo."),
    b_desde: str | None = Query(None, description="Ventana B (comparación) inicio; default: tramo reciente."),
    b_hasta: str | None = Query(None, description="Ventana B fin, inclusivo."),
    preset: ComparadorPreset | None = Query(None, description="Llena la ventana A rápido."),
    office_id: int | None = Query(None, description="Filtra a una sucursal."),
    dias_cobertura: int = Query(30, ge=1, le=180, description="Días de stock a cubrir en la sugerencia."),
    top: int = Query(300, ge=1, le=3000, description="Máx SKUs en el JSON (el Excel trae todos)."),
    orden: Literal["sugerencia", "venta_a", "delta_venta"] = Query(
        "sugerencia", description="Cómo ordenar la lista de SKUs."
    ),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Compara dos ventanas de fecha arbitrarias a nivel SKU (agrupado por
    jerarquía) y las cruza con el stock actual: qué se vendía entonces, cómo va
    ahora, cuánto rota y qué reponer. Ver el bloque de comentarios arriba."""
    cid = company.company_id
    company_config = await get_company(db, cid)
    a0, a1, b0, b1 = _resolver_ventanas_comparador(
        a_desde, a_hasta, b_desde, b_hasta, preset, date.today()
    )
    scope = await scope_ventas(db, cid, office_id)
    skus, alertas = await _comparador_rows(
        db, cid, company_config, a0, a1, b0, b1, office_id, dias_cobertura
    )

    # KPIs de cabecera.
    venta_a = sum(s["venta_a_pen"] for s in skus)
    venta_b = sum(s["venta_b_pen"] for s in skus)
    por_veredicto: dict[str, int] = {}
    sugerencia_unds_total = 0
    for s in skus:
        por_veredicto[s["veredicto"]] = por_veredicto.get(s["veredicto"], 0) + 1
        sugerencia_unds_total += s["sugerencia_compra"]

    # Orden de la lista de SKUs para el JSON.
    if orden == "venta_a":
        key = lambda s: s["venta_a_pen"]
    elif orden == "delta_venta":
        key = lambda s: (s["delta_venta_pct"] if s["delta_venta_pct"] is not None else -1e9)
    else:
        key = lambda s: (s["sugerencia_compra"], s["venta_a_pen"])
    skus_ordenados = sorted(skus, key=key, reverse=True)

    return {
        "meta": _meta_comparador(a0, a1, b0, b1, office_id, scope, dias_cobertura, alertas),
        "kpis": {
            "skus": len(skus),
            "venta_a_pen": round(venta_a, 2),
            "venta_b_pen": round(venta_b, 2),
            "delta_venta_pct": _pct(venta_b, venta_a),
            "sugerencia_unds_total": sugerencia_unds_total,
            "por_veredicto": por_veredicto,
        },
        "por_departamento": _agrupar_comparador(skus, ["departamento"]),
        "por_categoria": _agrupar_comparador(skus, ["departamento", "categoria"]),
        "skus": skus_ordenados[:top],
        "skus_total": len(skus),
    }


@router.get("/comparador/excel", response_class=StreamingResponse)
async def comparador_excel(
    a_desde: str | None = Query(None),
    a_hasta: str | None = Query(None),
    b_desde: str | None = Query(None),
    b_hasta: str | None = Query(None),
    preset: ComparadorPreset | None = Query(None),
    office_id: int | None = Query(None),
    dias_cobertura: int = Query(30, ge=1, le=180),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Mismo comparador, TODOS los SKUs, como Excel jerárquico descargable."""
    from analytics.excel_comparador import build_comparador_workbook

    cid = company.company_id
    company_config = await get_company(db, cid)
    a0, a1, b0, b1 = _resolver_ventanas_comparador(
        a_desde, a_hasta, b_desde, b_hasta, preset, date.today()
    )
    skus, alertas = await _comparador_rows(
        db, cid, company_config, a0, a1, b0, b1, office_id, dias_cobertura
    )

    sucursal = None
    if office_id is not None:
        sucursal = await db.scalar(
            text("SELECT name FROM offices WHERE company_id = :cid AND bsale_office_id = :oid"),
            {"cid": cid, "oid": office_id},
        )
    brand = company_config.get("brand_name") or "KAWII"

    wb = build_comparador_workbook(
        skus=skus,
        ventana_a=(a0.isoformat(), a1.isoformat()),
        ventana_b=(b0.isoformat(), b1.isoformat()),
        dias_cobertura=dias_cobertura,
        sucursal=sucursal or "Todas las sucursales",
        brand_name=brand,
        alertas=alertas,
    )
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    filename = f"Comparador_{a0.isoformat()}_vs_{b0.isoformat()}.xlsx"
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

