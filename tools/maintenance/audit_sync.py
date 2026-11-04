"""
Verificación post-sync: ¿lo que hay en Neon cuadra con BSale?

El resumen de mt_sync solo dice si cada fase CORRIÓ. Esto revisa si los datos
CUADRAN — la auditoría de 2026-11-04 encontró huecos que no daban ningún
error (ventas sin producto, costos que no se actualizaban, productos
eliminados que seguían activos). Es solo lectura: no escribe nada.

Chequeos (cada uno OK / AVISO / FALLA):
  1. Conteos por entidad, BSale vs Neon (sucursales, usuarios, tipos,
     productos y variantes activos, stock, recepciones, consumos, documentos,
     pagos, medios de pago, devoluciones).
  2. Documentos por mes (últimos N meses).
  3. Integridad en Neon: líneas de venta sin variante, ventas sin categoría,
     documentos sin detalle, cabecera que no suma sus líneas.
  4. Stock fantasma: cantidades en variantes inactivas.
  5. Costos: muestra contra BSale y variantes activas sin refrescar.
  6. Salud del sync: fases sin éxito reciente o colgadas en RUNNING.

Sale con código 1 si hay alguna FALLA (sirve para cron / CI).

Uso:
    python -m tools.maintenance.audit_sync --company-id 1
    python -m tools.maintenance.audit_sync --company-id 1 --meses 6 --muestra-costos 80 --json reporte.json
    python -m tools.maintenance.mt_sync --company-id 1 --days 7 --verify   # al final del sync
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

root_path = Path(__file__).resolve().parent.parent.parent
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from harvester import db  # noqa: E402
from harvester.bsale_client import fetch  # noqa: E402
from harvester.config import BSALE_BASE_URL  # noqa: E402
from harvester.tenant_context import current_company_id  # noqa: E402

logger = logging.getLogger("audit_sync")

OK, AVISO, FALLA = "OK", "AVISO", "FALLA"
_RANK = {OK: 0, AVISO: 1, FALLA: 2}

COST_TOLERANCE = 0.01
COST_MISMATCH_WARN = 0.10     # >10% de la muestra distinta → AVISO
COSTS_STALE_DAYS = 8          # el sync refresca cada 7 días
SYNC_STALE_HOURS = 36
SYNC_STUCK_HOURS = 3


@dataclass
class Check:
    grupo: str
    nombre: str
    estado: str
    bsale: object = None
    neon: object = None
    detalle: str = ""
    ejemplos: list = field(default_factory=list)


# ── reglas puras (testeables) ────────────────────────────────────────────────

def compare_counts(grupo: str, nombre: str, bsale, neon, *, soft: bool = False, detalle: str = "") -> Check:
    """Conteo igual → OK. Distinto → FALLA (AVISO si `soft`). Sin respuesta de BSale → AVISO."""
    if not isinstance(bsale, int):
        return Check(grupo, nombre, AVISO, bsale, neon, "BSale no respondió el conteo")
    if bsale == neon:
        return Check(grupo, nombre, OK, bsale, neon, detalle)
    dif = neon - bsale
    return Check(grupo, nombre, AVISO if soft else FALLA, bsale, neon,
                 f"diferencia {dif:+d}" + (f" · {detalle}" if detalle else ""))


def threshold(grupo: str, nombre: str, valor, *, falla_si_mayor: float | None = None,
              aviso_si_mayor: float | None = 0, detalle: str = "", ejemplos=None) -> Check:
    """Un número que debería ser 0 (o bajo)."""
    estado = OK
    if falla_si_mayor is not None and valor > falla_si_mayor:
        estado = FALLA
    elif aviso_si_mayor is not None and valor > aviso_si_mayor:
        estado = AVISO
    return Check(grupo, nombre, estado, None, valor, detalle, list(ejemplos or []))


def cost_matches(row: dict, bsale: dict | None) -> bool:
    """¿El costo guardado es coherente con lo que BSale informa hoy?
    AVERAGE: mismo promedio. RECEPTION_*/NONE/HISTORY: BSale no da promedio."""
    if not bsale:
        return True  # sin respuesta: no se acusa
    b_avg = float(bsale.get("averageCost") or 0)
    src = row["cost_source"]
    if src == "AVERAGE":
        return abs(float(row["average_cost"] or 0) - b_avg) <= COST_TOLERANCE
    return b_avg == 0


def overall(checks: list[Check]) -> str:
    return max((c.estado for c in checks), key=_RANK.__getitem__, default=OK)


# ── consultas ────────────────────────────────────────────────────────────────

def _q(sql: str, params: dict | None = None) -> list[tuple]:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            return cur.fetchall() if cur.description else []


def _one(sql: str, params: dict | None = None):
    rows = _q(sql, params)
    return rows[0][0] if rows else None


def _count(endpoint: str) -> int | str:
    sep = "&" if "?" in endpoint else "?"
    data = fetch(f"{BSALE_BASE_URL}/{endpoint}{sep}limit=1")
    c = data.get("count") if data else None
    return int(c) if c is not None else "sin respuesta"


def _table_exists(name: str) -> bool:
    return bool(_one("SELECT to_regclass(%(t)s) IS NOT NULL", {"t": name}))


# (grupo, nombre, endpoint BSale, SQL Neon, soft)
_COUNTS = [
    ("Maestros", "Sucursales", "offices.json", "SELECT count(*) FROM offices WHERE company_id=%(c)s", False),
    ("Maestros", "Usuarios", "users.json", "SELECT count(*) FROM users WHERE company_id=%(c)s", False),
    ("Maestros", "Tipos de documento", "document_types.json",
     "SELECT count(*) FROM document_types WHERE company_id=%(c)s", False),
    ("Maestros", "Tipos de producto activos", "product_types.json?state=0",
     "SELECT count(*) FROM product_types WHERE company_id=%(c)s AND is_active", False),
    ("Maestros", "Listas de precio", "price_lists.json",
     "SELECT count(*) FROM price_lists WHERE company_id=%(c)s", False),
    ("Catálogo", "Productos activos", "products.json?state=0",
     "SELECT count(*) FROM products WHERE company_id=%(c)s AND is_active", False),
    ("Catálogo", "Variantes activas", "variants.json?state=0",
     "SELECT count(*) FROM variants WHERE company_id=%(c)s AND is_active", False),
    ("Stock", "Filas de stock (variantes activas)", "stocks.json",
     """SELECT count(*) FROM stock_levels s JOIN variants v ON v.company_id=s.company_id
          AND v.bsale_variant_id=s.bsale_variant_id
          JOIN offices o ON o.company_id=s.company_id AND o.bsale_office_id=s.bsale_office_id AND o.is_active
         WHERE s.company_id=%(c)s AND v.is_active""", False),
    ("Movimientos", "Recepciones", "stocks/receptions.json",
     "SELECT count(*) FROM receptions WHERE company_id=%(c)s", False),
    ("Movimientos", "Consumos", "stocks/consumptions.json",
     "SELECT count(*) FROM consumptions WHERE company_id=%(c)s", False),
    ("Ventas", "Documentos activos", "documents.json?state=0",
     "SELECT count(*) FROM documents WHERE company_id=%(c)s AND is_active", False),
]
_COUNTS_OPTIONAL = [  # tablas que pueden no existir todavía
    ("Pagos", "Medios de pago", "payment_types.json", "payment_types",
     "SELECT count(*) FROM payment_types WHERE company_id=%(c)s"),
    ("Pagos", "Pagos", "payments.json", "payments", "SELECT count(*) FROM payments WHERE company_id=%(c)s"),
    ("Pagos", "Devoluciones", "returns.json", "returns", "SELECT count(*) FROM returns WHERE company_id=%(c)s"),
]


def _months(n: int) -> list[tuple[str, int, int, bool]]:
    today = dt.datetime.now(dt.timezone.utc)
    y, m = today.year, today.month
    out = []
    for i in range(n):
        start = dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end = dt.datetime(ny, nm, 1, tzinfo=dt.timezone.utc)
        out.append((f"{y}-{m:02d}", int(start.timestamp()), int(end.timestamp()) - 1, i == 0))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def run_audit(meses: int = 3, muestra_costos: int = 40, workers: int = 6) -> list[Check]:
    """Corre todos los chequeos para la empresa del tenant activo."""
    cid = current_company_id()
    p = {"c": cid}
    checks: list[Check] = []

    # 1. Conteos
    counts = list(_COUNTS)
    for g, n, ep, table, sql in _COUNTS_OPTIONAL:
        if _table_exists(table):
            counts.append((g, n, ep, sql, False))
        else:
            checks.append(Check(g, n, AVISO, None, None, f"tabla {table} no existe en Neon (falta la migración)"))
    months = _months(meses)
    with ThreadPoolExecutor(workers) as pool:
        remote = list(pool.map(_count, [c[2] for c in counts]))
        remote_m = list(pool.map(
            lambda m: _count(f"documents.json?state=0&emissiondaterange=[{m[1]},{m[2]}]"), months))
    for (g, n, _, sql, soft), r in zip(counts, remote):
        checks.append(compare_counts(g, n, r, _one(sql, p), soft=soft))

    # 2. Documentos por mes (el mes en curso puede tener ventas posteriores al sync)
    for (label, a, b, current), r in zip(months, remote_m):
        neon = _one("""SELECT count(*) FROM documents WHERE company_id=%(c)s AND is_active
                        AND extract(epoch FROM emission_date) BETWEEN %(a)s AND %(b)s""", {**p, "a": a, "b": b})
        checks.append(compare_counts("Ventas", f"Documentos {label}", r, neon, soft=current,
                                     detalle="mes en curso" if current else ""))

    # 3. Integridad de ventas en Neon
    huerf = _q("""SELECT count(*), coalesce(round(sum(x.total_amount)), 0) FROM document_details x
                   WHERE x.company_id=%(c)s AND NOT EXISTS (SELECT 1 FROM variants v
                         WHERE v.company_id=x.company_id AND v.bsale_variant_id=x.bsale_variant_id)""", p)[0]
    checks.append(threshold("Integridad", "Líneas de venta sin variante", huerf[0], falla_si_mayor=0,
                            detalle=f"S/ {huerf[1]:,} sin producto" if huerf[0] else ""))
    sin_cat = _q("""SELECT count(DISTINCT v.bsale_product_id), coalesce(round(sum(x.total_amount)), 0)
                      FROM document_details x
                      JOIN documents d ON d.company_id=x.company_id AND d.bsale_document_id=x.bsale_document_id
                      JOIN variants v ON v.company_id=x.company_id AND v.bsale_variant_id=x.bsale_variant_id
                      LEFT JOIN v_products_full f ON f.company_id=v.company_id AND f.bsale_product_id=v.bsale_product_id
                     WHERE x.company_id=%(c)s AND d.emission_date >= now() - interval '365 days'
                       AND f.subcategory IS NULL AND x.total_amount <> 0""", p)[0]
    checks.append(threshold("Integridad", "Productos vendidos (12m) sin categoría", sin_cat[0],
                            detalle=f"S/ {sin_cat[1]:,} en ventas" if sin_cat[0] else ""))
    sin_det = _one("""SELECT count(*) FROM documents d WHERE d.company_id=%(c)s AND d.is_active
                       AND d.emission_date >= now() - interval '90 days'
                       AND NOT EXISTS (SELECT 1 FROM document_details x WHERE x.company_id=d.company_id
                                       AND x.bsale_document_id=d.bsale_document_id)""", p)
    checks.append(threshold("Integridad", "Documentos (90d) sin detalle", sin_det, falla_si_mayor=0))
    descuadre = _q("""SELECT d.bsale_document_id, d.total_amount, s.t FROM documents d
                        JOIN (SELECT bsale_document_id, sum(total_amount) t FROM document_details
                               WHERE company_id=%(c)s GROUP BY 1) s ON s.bsale_document_id=d.bsale_document_id
                       WHERE d.company_id=%(c)s AND d.emission_date >= now() - interval '90 days'
                         AND abs(d.total_amount - s.t) > 1""", p)
    checks.append(threshold("Integridad", "Documentos (90d) cuyo total no suma sus líneas", len(descuadre),
                            falla_si_mayor=0, ejemplos=[r[0] for r in descuadre[:5]]))

    # 4. Stock fantasma
    fant = _q("""SELECT count(*), coalesce(sum(s.quantity), 0) FROM stock_levels s
                  JOIN variants v ON v.company_id=s.company_id AND v.bsale_variant_id=s.bsale_variant_id
                 WHERE s.company_id=%(c)s AND NOT v.is_active AND s.quantity <> 0""", p)[0]
    checks.append(threshold("Stock", "Stock en variantes inactivas", fant[0],
                            detalle=f"{float(fant[1]):,.0f} unidades" if fant[0] else ""))

    # 5. Costos
    stale = _one("""SELECT count(*) FROM variants v LEFT JOIN variant_costs vc
                      ON vc.company_id=v.company_id AND vc.bsale_variant_id=v.bsale_variant_id
                     WHERE v.company_id=%(c)s AND v.is_active
                       AND (vc.synced_at IS NULL OR vc.synced_at < now() - make_interval(days => %(d)s))""",
                 {**p, "d": COSTS_STALE_DAYS})
    checks.append(threshold("Costos", f"Variantes activas sin refrescar costo (> {COSTS_STALE_DAYS} días)", stale))
    sin_costo = _one("""SELECT count(*) FROM variants v JOIN variant_costs vc
                          ON vc.company_id=v.company_id AND vc.bsale_variant_id=v.bsale_variant_id
                         WHERE v.company_id=%(c)s AND v.is_active AND coalesce(vc.effective_cost, 0) = 0""", p)
    checks.append(threshold("Costos", "Variantes activas sin costo (ni BSale ni recepciones)", sin_costo,
                            aviso_si_mayor=50, detalle="informativo: se corrige cargando el costo en BSale"))
    if muestra_costos > 0:
        cols = ("bsale_variant_id", "average_cost", "cost_source")
        rows = [dict(zip(cols, r)) for r in _q(
            """SELECT vc.bsale_variant_id, vc.average_cost, vc.cost_source FROM variant_costs vc
                 JOIN variants v ON v.company_id=vc.company_id AND v.bsale_variant_id=vc.bsale_variant_id
                WHERE vc.company_id=%(c)s AND v.is_active""", p)]
        sample = random.sample(rows, min(muestra_costos, len(rows)))
        with ThreadPoolExecutor(workers) as pool:
            got = list(pool.map(lambda r: fetch(f"{BSALE_BASE_URL}/variants/{r['bsale_variant_id']}/costs.json"),
                                sample))
        bad = [r["bsale_variant_id"] for r, b in zip(sample, got) if not cost_matches(r, b)]
        rate = len(bad) / len(sample) if sample else 0
        checks.append(Check("Costos", f"Muestra de costos vs BSale ({len(sample)})",
                            AVISO if rate > COST_MISMATCH_WARN else OK, len(sample), len(sample) - len(bad),
                            f"{len(bad)} distintos ({rate:.0%}); BSale recalcula el promedio y el sync "
                            f"lo refresca cada 7 días" if bad else "", bad[:5]))

    # 6. Salud del sync
    last = _q("""SELECT entity, max(started_at) FILTER (WHERE status='SUCCESS'),
                        max(started_at) FILTER (WHERE status='RUNNING')
                   FROM sync_log WHERE company_id=%(c)s GROUP BY entity""", p)
    now = dt.datetime.now(dt.timezone.utc)
    viejas = [f"{e} ({(now - s).total_seconds() / 3600:.0f} h)" for e, s, _ in last
              if s and (now - s).total_seconds() > SYNC_STALE_HOURS * 3600
              and e in {"documents", "variants", "stock_levels", "receptions", "variant_costs", "payments"}]
    checks.append(threshold("Sync", f"Fases clave sin éxito en {SYNC_STALE_HOURS} h", len(viejas), ejemplos=viejas))
    # Corridas en RUNNING hace horas SIN un éxito posterior de la misma fase
    # (las interrumpidas viejas que ya se recuperaron no importan).
    colgadas = _q("""SELECT r.entity, r.started_at FROM sync_log r
                      WHERE r.company_id=%(c)s AND r.status='RUNNING'
                        AND r.started_at > now() - interval '7 days'
                        AND r.started_at < now() - make_interval(hours => %(h)s)
                        AND NOT EXISTS (SELECT 1 FROM sync_log s WHERE s.company_id=r.company_id
                                        AND s.entity=r.entity AND s.status='SUCCESS'
                                        AND s.started_at > r.started_at)""", {**p, "h": SYNC_STUCK_HOURS})
    checks.append(threshold("Sync", "Fases trabadas en RUNNING sin éxito posterior", len(colgadas),
                            ejemplos=[f"{e} desde {s:%Y-%m-%d %H:%M}" for e, s in colgadas[:5]]))
    return checks


def print_report(company_name: str, checks: list[Check]) -> None:
    icon = {OK: "OK   ", AVISO: "AVISO", FALLA: "FALLA"}
    print("=" * 96)
    print(f"  VERIFICACIÓN BSale vs Neon — {company_name} — {dt.datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 96)
    grupo = None
    for c in checks:
        if c.grupo != grupo:
            grupo = c.grupo
            print(f"\n  {grupo}")
        cifras = ""
        if c.bsale is not None or c.neon is not None:
            cifras = (f"BSale {c.bsale!s:>8} · Neon {c.neon!s:>8}" if c.bsale is not None
                      else f"{c.neon!s:>8}")
        extra = f"  {c.detalle}" if c.detalle else ""
        if c.ejemplos:
            extra += f"  ej: {', '.join(map(str, c.ejemplos))}"
        print(f"   [{icon[c.estado]}] {c.nombre:<52} {cifras}{extra}")
    res = overall(checks)
    n = {k: sum(c.estado == k for c in checks) for k in (OK, AVISO, FALLA)}
    print(f"\n  RESULTADO: {res}  ({n[OK]} OK · {n[AVISO]} avisos · {n[FALLA]} fallas)")
    print("=" * 96)


def verify_company(company: dict, meses: int = 3, muestra_costos: int = 40) -> list[Check]:
    """Corre la verificación para una empresa (abre y cierra su tenant)."""
    from harvester.tenant_context import tenant_scope
    with tenant_scope(company["id"], company["token"], company["slug"]):
        checks = run_audit(meses=meses, muestra_costos=muestra_costos)
    print_report(company["name"], checks)
    return checks


def main() -> int:
    from tools.maintenance.mt_sync import load_active_companies
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--company-id", type=int, default=None, help="default: todas las activas")
    ap.add_argument("--meses", type=int, default=3, help="meses de documentos a comparar (default 3)")
    ap.add_argument("--muestra-costos", type=int, default=40, help="variantes a comparar contra BSale (0 = no)")
    ap.add_argument("--json", help="guardar el reporte en este archivo")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    companies = load_active_companies(filter_id=a.company_id)
    if not companies:
        print("No hay empresas activas con token.")
        return 2
    db.init_pool()
    out, worst = {}, OK
    try:
        for comp in companies:
            checks = verify_company(comp, a.meses, a.muestra_costos)
            out[comp["name"]] = [asdict(c) for c in checks]
            worst = max(worst, overall(checks), key=_RANK.__getitem__)
    finally:
        db.close_pool()
    if a.json:
        Path(a.json).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"reporte: {a.json}")
    return 1 if worst == FALLA else 0


if __name__ == "__main__":
    sys.exit(main())
