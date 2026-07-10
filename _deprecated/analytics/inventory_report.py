"""
KAWII | Análisis de Inventario y Rotación de Stock — v2
=======================================================
Ejecutar desde la raíz del proyecto (produccion/):
    python -m analytics.inventory_report

Genera: analytics/reporte_inventario.md

MEJORAS v2:
  - Precio de venta real promedio (AVG de ventas 90d, sin costo)
  - Valor económico del stock (capital inmovilizado en S/)
  - Ingreso en riesgo para quiebres (S/ que se dejarán de ganar)
  - Semáforo biaxial: velocidad × valor económico
  - Umbrales de rotación relativos al precio (no hardcodeados)
  - SQL unificado y filtrado por categorías objetivo
"""

import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harvester.config import (
    DB_CONFIG,
    OFFICES_TIENDA,
    OFFICE_ALMACEN,
    TARGET_CATEGORIES,
)
from app.config import get_settings

settings = get_settings()

# ── Constantes de negocio ─────────────────────────────────────────────────────
TIENDAS   = tuple(OFFICES_TIENDA)
ALMACEN   = OFFICE_ALMACEN
CATS      = tuple(TARGET_CATEGORIES)
VENTANA   = 90                # Días para calcular venta diaria real
HORIZONTE = 30                # Días de horizonte de reposición

OUTPUT = Path(__file__).parent / "reporte_inventario.md"

# ── Query principal ───────────────────────────────────────────────────────────
# Usa total_unit_value (precio con impuesto) de document_details como precio
# real de venta. No toca variant_costs en ningún momento.
SQL = """
WITH

-- 1. Stock actual en tiendas físicas
stock_tiendas AS (
    SELECT bsale_variant_id,
           SUM(quantity_available) AS stock_tiendas
    FROM   stock_levels
    WHERE  bsale_office_id IN %(tiendas)s
    GROUP  BY bsale_variant_id
),

-- 2. Stock en almacén (solo abastece, no genera ventas)
stock_almacen AS (
    SELECT bsale_variant_id,
           SUM(quantity_available) AS stock_almacen
    FROM   stock_levels
    WHERE  bsale_office_id = %(almacen)s
    GROUP  BY bsale_variant_id
),

-- 3. Métricas de venta: unidades + precio de venta REAL promedio (90 días)
--    total_unit_value = precio final con impuesto pagado por el cliente
--    NO se usa ningún campo de costo.
ventas_90d AS (
    SELECT
        dd.bsale_variant_id,
        SUM(dd.quantity)                             AS unidades_90d,
        -- Precio promedio real de venta (ponderado por cantidad)
        ROUND(
            SUM(dd.total_unit_value * dd.quantity)
            / NULLIF(SUM(dd.quantity), 0),
        2)                                           AS precio_venta_prom
    FROM   document_details dd
    JOIN   documents        doc ON doc.bsale_document_id = dd.bsale_document_id
    WHERE  doc.bsale_office_id IN %(tiendas)s
      AND  doc.is_credit_note = FALSE
      AND  doc.is_active      = TRUE
      AND  dd.is_gratuity     = FALSE
      AND  dd.total_unit_value > 0
      AND  (doc.emission_date AT TIME ZONE 'America/Lima')::date >= CURRENT_DATE - INTERVAL '90 days'
    GROUP  BY dd.bsale_variant_id
),

-- 4. Fecha de primera recepción (antigüedad del producto)
fecha_recepcion AS (
    SELECT rd.bsale_variant_id,
           MIN(r.admission_date) AS primera_recepcion
    FROM   reception_details rd
    JOIN   receptions r ON r.bsale_reception_id = rd.bsale_reception_id
    GROUP  BY rd.bsale_variant_id
),

-- 5. Última venta registrada (en tiendas activas)
ultima_venta AS (
    SELECT dd.bsale_variant_id,
           MAX(doc.emission_date) AS ultima_venta
    FROM   document_details dd
    JOIN   documents doc ON doc.bsale_document_id = dd.bsale_document_id
    WHERE  doc.is_active      = TRUE
      AND  doc.is_credit_note = FALSE
      AND  doc.bsale_office_id IN %(tiendas)s
      AND  dd.is_gratuity     = FALSE
    GROUP  BY dd.bsale_variant_id
)

SELECT
    v.bsale_variant_id                                                      AS variant_id,
    v.display_code                                                          AS sku,
    v.description                                                           AS variante,
    p.name                                                                  AS producto,
    dep.name                                                                AS departamento,
    cat.name                                                                AS categoria,
    sc.name                                                                 AS subcategoria,

    -- Temporalidad
    fr.primera_recepcion,
    EXTRACT(DAY FROM (CURRENT_DATE - fr.primera_recepcion))                 AS antiguedad_dias,
    uv.ultima_venta,
    EXTRACT(DAY FROM (CURRENT_DATE - uv.ultima_venta))                      AS dias_sin_vender,

    -- Stock
    COALESCE(st.stock_tiendas, 0)                                           AS stock_tiendas,
    COALESCE(sa.stock_almacen, 0)                                           AS stock_almacen,
    COALESCE(st.stock_tiendas, 0) + COALESCE(sa.stock_almacen, 0)          AS stock_total,

    -- Velocidad de venta
    COALESCE(vt.unidades_90d, 0)                                            AS unidades_90d,
    ROUND(COALESCE(vt.unidades_90d, 0) / %(ventana)s::NUMERIC, 4)          AS venta_diaria,
    ROUND((COALESCE(vt.unidades_90d, 0) / %(ventana)s::NUMERIC) * %(horizonte)s, 2)
                                                                            AS meta_30d,

    -- Precio de venta real promedio (del mercado, no del catálogo)
    COALESCE(vt.precio_venta_prom, 0)                                       AS precio_venta_prom,

    -- Valor económico del stock = capital inmovilizado en S/ (sin costo)
    ROUND(
        (COALESCE(st.stock_tiendas, 0) + COALESCE(sa.stock_almacen, 0))
        * COALESCE(vt.precio_venta_prom, 0),
    2)                                                                      AS valor_stock_soles,

    -- Ingreso diario proyectado (ritmo de generación de ingresos)
    ROUND(
        (COALESCE(vt.unidades_90d, 0) / %(ventana)s::NUMERIC)
        * COALESCE(vt.precio_venta_prom, 0),
    2)                                                                      AS ingreso_diario,

    -- Días de stock restante (stock total / venta diaria)
    CASE
        WHEN COALESCE(vt.unidades_90d, 0) = 0 THEN NULL
        ELSE ROUND(
            (COALESCE(st.stock_tiendas, 0) + COALESCE(sa.stock_almacen, 0))
            / (COALESCE(vt.unidades_90d, 0) / %(ventana)s::NUMERIC),
        1)
    END                                                                     AS dias_stock,

    -- Ingreso en riesgo: lo que se deja de ganar si el producto se agota
    -- (solo aplica cuando dias_stock < horizonte de reposición)
    CASE
        WHEN COALESCE(vt.unidades_90d, 0) = 0 THEN 0
        WHEN ROUND(
                (COALESCE(st.stock_tiendas, 0) + COALESCE(sa.stock_almacen, 0))
                / (COALESCE(vt.unidades_90d, 0) / %(ventana)s::NUMERIC), 1
             ) >= %(horizonte)s THEN 0
        ELSE ROUND(
            GREATEST(0,
                %(horizonte)s
                - (COALESCE(st.stock_tiendas, 0) + COALESCE(sa.stock_almacen, 0))
                  / (COALESCE(vt.unidades_90d, 0) / %(ventana)s::NUMERIC)
            )
            * (COALESCE(vt.unidades_90d, 0) / %(ventana)s::NUMERIC)
            * COALESCE(vt.precio_venta_prom, 0),
        2)
    END                                                                     AS ingreso_en_riesgo

FROM variants v
JOIN products         p   ON p.bsale_product_id   = v.bsale_product_id
LEFT JOIN subcategories sc  ON sc.id               = p.subcategory_id
LEFT JOIN categories   cat ON cat.id               = sc.category_id
LEFT JOIN departments  dep ON dep.id               = cat.department_id
LEFT JOIN stock_tiendas st  ON st.bsale_variant_id = v.bsale_variant_id
LEFT JOIN stock_almacen sa  ON sa.bsale_variant_id = v.bsale_variant_id
LEFT JOIN ventas_90d    vt  ON vt.bsale_variant_id = v.bsale_variant_id
LEFT JOIN fecha_recepcion fr ON fr.bsale_variant_id = v.bsale_variant_id
LEFT JOIN ultima_venta  uv  ON uv.bsale_variant_id  = v.bsale_variant_id
WHERE
    v.is_active    = TRUE
    AND p.is_active = TRUE
    AND cat.id IN %(cats)s
    AND (
        COALESCE(st.stock_tiendas, 0) > 0
        OR COALESCE(sa.stock_almacen, 0) > 0
        OR COALESCE(vt.unidades_90d, 0) > 0
    )
ORDER BY cat.name, sc.name, p.name, v.display_code;
"""


# ─────────────────────────────────────────────────────────────────────────────
# SEMÁFORO BIAXIAL: velocidad de venta × valor económico
# ─────────────────────────────────────────────────────────────────────────────
# Umbrales de velocidad:
#   ALTA  : vende >= 10 unidades/mes  (0.333/día)
#   MEDIA : vende entre 3 y 10 ud/mes (0.100–0.333/día)
#   BAJA  : vende < 3 unidades/mes    (<0.100/día)
#
# El umbral de 10 ud/mes es dinámico: si el precio es > S/500, baja a 3 ud/mes
# porque el valor económico ya justifica la urgencia.
# ─────────────────────────────────────────────────────────────────────────────

def _umbral_alta(precio: float) -> float:
    """Umbral diario para considerar ALTA rotación según precio de venta."""
    if precio >= 500:
        return 3.0 / 30.0    # 3 uds/mes si precio alto
    return 10.0 / 30.0       # 10 uds/mes si precio normal


def semaforo(row: dict) -> str:
    stock       = float(row["stock_total"] or 0)
    vd          = float(row["venta_diaria"] or 0)
    ds          = float(row["dias_stock"]) if row["dias_stock"] is not None else None
    precio      = float(row["precio_venta_prom"] or 0)
    antiguedad  = float(row["antiguedad_dias"]) if row["antiguedad_dias"] is not None else 9999
    dsv         = float(row["dias_sin_vender"]) if row["dias_sin_vender"] is not None else 9999

    umbral_alta  = _umbral_alta(precio)
    umbral_media = 3.0 / 30.0   # 3 uds/mes como mínimo de movimiento

    # ── 0. Sin stock físico ───────────────────────────────────────────────────
    if stock <= 0:
        return "⚪ SIN_STOCK"

    # ── 1. Producto nuevo: menos de 90 días desde primera recepción ───────────
    if antiguedad < VENTANA:
        return "✨ NUEVO"

    # ── 2. Stock muerto: tiene unidades pero NO ha vendido nada en 90 días ───
    if vd == 0 or dsv >= VENTANA:
        return "☠️ STOCK_MUERTO"

    # ── 3. Estancado: stock para > 120 días Y no vende en los últimos 30 días
    #    (vendió antes pero perdió tracción)
    if ds is not None and ds > 120 and dsv > 30:
        return "🧟 ESTANCADO"

    # ── 4. Quiebre inminente: dias_stock < horizonte de reposición ───────────
    if ds is not None and ds < HORIZONTE:
        if vd >= umbral_alta:
            return "🔥 QUIEBRE_INMINENTE"   # Vende rápido, se acaba pronto → urgente
        elif vd >= umbral_media:
            return "⚠️ QUIEBRE_MODERADO"    # Vende algo, pero tampoco es urgentísimo
        else:
            return "🟡 FALSO_QUIEBRE"       # Se acaba pronto pero vende casi nada

    # ── 5. Sobre-stock: stock para más de 90 días ────────────────────────────
    if ds is not None and ds > 90:
        if vd >= umbral_alta:
            return "📦 SOBRE_STOCK_ACTIVO"  # Vende bien pero tiene demasiado stock
        else:
            return "🐢 LENTO"               # Vende poco y tiene stock de sobra

    # ── 6. Zona sana: stock entre 30 y 90 días ───────────────────────────────
    if ds is not None and HORIZONTE <= ds <= 90:
        return "🟢 SANO"

    return "❓ INDETERMINADO"


# ── Helpers de formato ────────────────────────────────────────────────────────
def fmt(v, dec=0):
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{dec}f}"
    except Exception:
        return str(v)


def soles(v):
    """Formatea como moneda S/ con separador de miles."""
    if v is None or float(v) == 0:
        return "—"
    return f"S/ {float(v):,.0f}"


# ─────────────────────────────────────────────────────────────────────────────
# CONSTRUCCIÓN DEL REPORTE
# ─────────────────────────────────────────────────────────────────────────────

ORDEN_SEMAFORO = [
    "🔥 QUIEBRE_INMINENTE",
    "⚠️ QUIEBRE_MODERADO",
    "☠️ STOCK_MUERTO",
    "🧟 ESTANCADO",
    "🐢 LENTO",
    "🟡 FALSO_QUIEBRE",
    "📦 SOBRE_STOCK_ACTIVO",
    "🟢 SANO",
    "✨ NUEVO",
    "⚪ SIN_STOCK",
    "❓ INDETERMINADO",
]

GUIA = {
    "🔥 QUIEBRE_INMINENTE"  : "Se agota en < 30 días Y vende rápido. Reponer YA.",
    "⚠️ QUIEBRE_MODERADO"   : "Se agota en < 30 días, venta media. Evaluar reposición.",
    "☠️ STOCK_MUERTO"       : "Tiene unidades físicas pero NO vendió nada en 90 días.",
    "🧟 ESTANCADO"          : "Stock para > 4 meses y lleva > 30 días sin venderse.",
    "🐢 LENTO"              : "Vende poco y tiene stock para > 3 meses. Candidato a liquidar.",
    "🟡 FALSO_QUIEBRE"      : "Se acaba pronto pero vende casi nada. No comprar urgente.",
    "📦 SOBRE_STOCK_ACTIVO" : "Vende bien pero tiene exceso de stock (> 3 meses). Redistribuir.",
    "🟢 SANO"               : "Inventario equilibrado (30–90 días de stock). Ideal.",
    "✨ NUEVO"              : "Ingresó hace < 90 días. Sin penalización de rotación aún.",
    "⚪ SIN_STOCK"          : "Agotado en tiendas y almacén.",
    "❓ INDETERMINADO"      : "No se pudo clasificar con la información disponible.",
}


def build_report(rows: list[dict]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Calcular semáforo de cada fila
    for row in rows:
        row["_semaforo"] = semaforo(row)

    lines = []

    # ── Encabezado ────────────────────────────────────────────────────────────
    lines += [
        f"# 📦 Reporte de Salud de Inventario {settings.BRAND_NAME.upper()} — v2",
        f"**Generado:** {now}  |  **Ventana de análisis:** {VENTANA} días  |  "
        f"**Horizonte de reposición:** {HORIZONTE} días",
        "",
        "> **Metodología:** Semáforo biaxial — combina *velocidad de venta real* "
        "(últimos 90 días) con *valor económico al precio de venta*. "
        "No usa costos de producto.",
        "",
        "### 📖 Guía de Diagnósticos",
        "| Diagnóstico | Significado |",
        "|---|---|",
    ]
    for k, v in GUIA.items():
        lines.append(f"| {k} | {v} |")
    lines.append("")

    # ── 1. Resumen global ─────────────────────────────────────────────────────
    resumen: dict[str, dict] = {s: {"skus": 0, "stock": 0, "valor": 0.0} for s in ORDEN_SEMAFORO}
    for row in rows:
        s = row["_semaforo"]
        resumen[s]["skus"]  += 1
        resumen[s]["stock"] += float(row["stock_total"] or 0)
        resumen[s]["valor"] += float(row["valor_stock_soles"] or 0)

    total_skus  = len(rows)
    total_valor = sum(r["valor"] for r in resumen.values())

    lines += [
        "---",
        "## 1. Resumen Global de Salud del Inventario",
        "",
        "| Diagnóstico | SKUs | Uds en Inventario | Valor en Stock (S/) | % Catálogo |",
        "|---|---|---|---|---|",
    ]
    for s in ORDEN_SEMAFORO:
        r = resumen[s]
        if r["skus"] == 0:
            continue
        pct = (r["skus"] / total_skus) * 100
        lines.append(
            f"| {s} | {r['skus']} | {fmt(r['stock'])} ud | "
            f"{soles(r['valor'])} | {fmt(pct, 1)}% |"
        )
    lines += [
        f"| **TOTAL** | **{total_skus}** | | **{soles(total_valor)}** | 100% |",
        "",
    ]

    # ── 2. Alertas: Quiebre inminente ────────────────────────────────────────
    quiebres = sorted(
        [r for r in rows if r["_semaforo"] in ("🔥 QUIEBRE_INMINENTE", "⚠️ QUIEBRE_MODERADO")],
        key=lambda r: float(r["dias_stock"] or 9999),
    )
    total_riesgo = sum(float(r["ingreso_en_riesgo"] or 0) for r in quiebres)

    lines += [
        "---",
        "## 2. 🔥 Alertas de Quiebre — Productos que se Agotarán en < 30 Días",
        "",
        f"*{len(quiebres)} SKUs en riesgo — Ingreso proyectado en riesgo: **{soles(total_riesgo)}***",
        "",
        "| SKU | Producto | Cat. | Precio Vta | Stock T | Stock A | Vta/mes | Días Stock | Ingreso en Riesgo | Estado |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in quiebres[:100]:
        lines.append(
            f"| {r['sku']} | {r['producto']} | {r['categoria']} | "
            f"{soles(r['precio_venta_prom'])} | {fmt(r['stock_tiendas'])} | "
            f"{fmt(r['stock_almacen'])} | {fmt(r['meta_30d'], 1)} | "
            f"**{fmt(r['dias_stock'], 1)}** | {soles(r['ingreso_en_riesgo'])} | {r['_semaforo']} |"
        )
    if len(quiebres) > 100:
        lines.append(f"*(Se omitieron {len(quiebres)-100} filas...)*")
    lines.append("")

    # ── 3. Capital atrapado (Muerto + Estancado + Lento) ─────────────────────
    capital_frio = sorted(
        [r for r in rows if r["_semaforo"] in ("☠️ STOCK_MUERTO", "🧟 ESTANCADO", "🐢 LENTO")],
        key=lambda r: -float(r["valor_stock_soles"] or 0),
    )
    total_capital = sum(float(r["valor_stock_soles"] or 0) for r in capital_frio)

    lines += [
        "---",
        "## 3. 💸 Capital Atrapado — Prioridad de Liquidación",
        "",
        f"*{len(capital_frio)} SKUs con capital inmovilizado — "
        f"Total estimado: **{soles(total_capital)}***",
        "",
        "| SKU | Producto | Cat. | Estado | Precio Vta | Stock Total | Valor (S/) | Días sin Vender | Antigüedad |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in capital_frio[:150]:
        lines.append(
            f"| {r['sku']} | {r['producto']} | {r['categoria']} | {r['_semaforo']} | "
            f"{soles(r['precio_venta_prom'])} | **{fmt(r['stock_total'])}** | "
            f"**{soles(r['valor_stock_soles'])}** | "
            f"{fmt(r['dias_sin_vender'])} | {fmt(r['antiguedad_dias'])} |"
        )
    if len(capital_frio) > 150:
        lines.append(f"*(Se omitieron {len(capital_frio)-150} filas...)*")
    lines.append("")

    # ── 4. Sobre-stock activo (redistribuir, no liquidar) ────────────────────
    sobre = sorted(
        [r for r in rows if r["_semaforo"] == "📦 SOBRE_STOCK_ACTIVO"],
        key=lambda r: -float(r["valor_stock_soles"] or 0),
    )
    if sobre:
        lines += [
            "---",
            "## 4. 📦 Sobre-Stock Activo — Venden Bien Pero Tienen Exceso",
            "",
            f"*{len(sobre)} SKUs — considerar redistribución entre tiendas*",
            "",
            "| SKU | Producto | Precio Vta | Stock Total | Valor (S/) | Vta/mes | Días Stock |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in sobre[:80]:
            lines.append(
                f"| {r['sku']} | {r['producto']} | {soles(r['precio_venta_prom'])} | "
                f"{fmt(r['stock_total'])} | {soles(r['valor_stock_soles'])} | "
                f"{fmt(r['meta_30d'], 1)} | {fmt(r['dias_stock'], 1)} |"
            )
        lines.append("")

    # ── 5. Auditoría completa ─────────────────────────────────────────────────
    lines += [
        "---",
        "## 5. Auditoría Completa de SKUs",
        "",
        "| SKU | Producto | Cat | Precio Vta | Stock T | Stock A | Ingr. Diario | Días Stock | Días Sin Vta | Diagnóstico |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    rows_ord = sorted(rows, key=lambda r: (ORDEN_SEMAFORO.index(r["_semaforo"]), r["sku"]))
    for r in rows_ord:
        lines.append(
            f"| {r['sku']} | {r['producto']} | {r['categoria']} | "
            f"{soles(r['precio_venta_prom'])} | {fmt(r['stock_tiendas'])} | "
            f"{fmt(r['stock_almacen'])} | {soles(r['ingreso_diario'])} | "
            f"{fmt(r['dias_stock'], 1)} | {fmt(r['dias_sin_vender'])} | {r['_semaforo']} |"
        )

    lines += ["", "---", f"*{settings.BRAND_NAME.upper()} Analytics — {now}*"]
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("🔌 Conectando a PostgreSQL...")
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            print("⚙️  Ejecutando análisis v2 (precio de venta real + valor económico)...")
            cur.execute(SQL, {
                "tiendas"   : TIENDAS,
                "almacen"   : ALMACEN,
                "cats"      : CATS,
                "ventana"   : VENTANA,
                "horizonte" : HORIZONTE,
            })
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    print(f"✅ {len(rows)} SKUs analizados.")
    report = build_report(rows)
    OUTPUT.write_text(report, encoding="utf-8")
    print(f"📄 Reporte guardado en: {OUTPUT}")


if __name__ == "__main__":
    main()
