"""
Orquestador MULTI-TENANT del sync completo.

Orquestador del ETL multi-empresa:
  1. Carga todas las empresas activas con su token BSale (descifrado con pgcrypto).
  2. Por cada una:
       - `set_current_tenant(id, token, slug)` activa el contexto.
       - Corre el pipeline completo (taxonomy → masters → transactions).
       - Loguea stats por empresa.
  3. Al final imprime un resumen consolidado.

Uso:
    python tools/maintenance/mt_sync.py                 # 7 días de docs, todas las empresas
    python tools/maintenance/mt_sync.py --days 30       # último mes
    python tools/maintenance/mt_sync.py --days 730      # 2 años (sync completo inicial)
    python tools/maintenance/mt_sync.py --company-id 1  # solo una empresa
    python tools/maintenance/mt_sync.py --skip-documents

Cada empresa corre INDEPENDIENTE: si Hudec falla, Coya continúa. Los errores
por empresa se logean y aparecen en el resumen final.

Requiere en .env:
  - DB_HOST, DB_NAME, DB_USER, DB_PASSWORD, DB_PORT
  - TOKEN_ENCRYPTION_KEY (clave para descifrar companies.bsale_token)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Bootstrap path
root_path = Path(__file__).resolve().parent.parent.parent
if str(root_path) not in sys.path:
    sys.path.append(str(root_path))

from dotenv import load_dotenv
import psycopg2

from harvester import db
from harvester.tenant_context import set_current_tenant, clear_current_tenant
from harvester.sync_masters import (
    sync_taxonomy,
    sync_offices,
    sync_users,
    sync_product_types,
    sync_document_types,
    sync_variants,
    sync_variant_costs,
    sync_variant_costs_by_office,
    sync_price_lists,
    sync_stock_levels,
    snapshot_stock_history,
    sync_product_type_attributes,
    sync_variant_attribute_values,
)
from harvester.sync_transactions import sync_documents, sync_receptions, sync_consumptions


LOG_FMT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format=LOG_FMT,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("mt_sync.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# CARGA DE EMPRESAS (con descifrado de token)
# ---------------------------------------------------------------------------


def load_active_companies(
    filter_id: int | None = None,
) -> list[dict]:
    """Carga las empresas activas con su token BSale descifrado.

    Salta empresas sin token cargado o sin descifrar correctamente.
    """
    load_dotenv(root_path / ".env")
    master_key = os.environ.get("TOKEN_ENCRYPTION_KEY")
    if not master_key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY no está en .env")

    dsn = {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "dbname": os.environ["DB_NAME"],
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
    }

    where = "is_active = TRUE AND bsale_token IS NOT NULL"
    params: tuple = ()
    if filter_id is not None:
        where += " AND id = %s"
        params = (filter_id,)

    with psycopg2.connect(**dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, name, slug,
                       pgp_sym_decrypt(bsale_token, %s)::text AS token
                FROM companies
                WHERE {where}
                ORDER BY id
                """,
                (master_key, *params),
            )
            rows = cur.fetchall()

    return [
        {"id": r[0], "name": r[1], "slug": r[2], "token": r[3]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# PIPELINE POR EMPRESA
# ---------------------------------------------------------------------------


def run_pipeline_for_company(
    company: dict,
    since_unix: int,
    skip_documents: bool = False,
    skip_stock_snapshot: bool = False,
    mode: str = "full",
    full_refresh: bool = False,
) -> dict:
    """Corre el sync para UNA empresa. Devuelve stats por fase.

    Modos:
      - 'quick': refresco operativo rápido (~1-2 min). Actualiza stock,
        recepciones/consumos recientes, costos de variantes con recepción
        nueva y documentos del rango pedido. Ideal para botón manual en el día.
      - 'full': pipeline completo (catálogo, listas de precio, stock,
        recepciones/consumos, costos incrementales, documentos). Si además
        `full_refresh=True`, fuerza re-descarga completa de atributos,
        historial completo de recepciones/consumos y costos de todas las variantes.

    NOTA DE ORDEN: Recepciones corre ANTES de Costos y CostosSucursal para que
    ambos vean las recepciones recién ingresadas en la misma corrida.
    """
    logger = logging.getLogger("mt_sync")
    logger.info("=" * 72)
    logger.info(
        "  Empresa: %s (id=%d, slug=%s) | modo=%s | full_refresh=%s",
        company["name"], company["id"], company["slug"], mode, full_refresh,
    )
    logger.info("=" * 72)

    set_current_tenant(company["id"], company["token"], company["slug"])

    phase_results: dict = {}
    if mode == "quick":
        # Incluye maestros ligeros + variantes (~15s con paginación paralela)
        # para que productos/categorías/usuarios recién creados en BSale entren
        # al instante y NUNCA rompan FKs en stock_levels, recepciones o docs.
        fases = [
            ("1.Sucursales",        sync_offices),
            ("2.Usuarios",          sync_users),
            ("3.Categorias_BSale",  sync_product_types),
            ("4.Tipos_Documento",   sync_document_types),
            ("5.Productos_Variant", sync_variants),
            ("6.Stock",             sync_stock_levels),
            ("7.Recepciones",       lambda: sync_receptions(full_history=False)),
            ("8.Consumos",          lambda: sync_consumptions(full_history=False)),
            ("9.Costos",            lambda: sync_variant_costs(full=False)),
            ("10.CostosSucursal",   sync_variant_costs_by_office),
        ]
    else:
        hist_flag = True if full_refresh else None
        fases = [
            ("1.Taxonomia",         sync_taxonomy),
            ("2.Sucursales",        sync_offices),
            ("3.Usuarios",          sync_users),
            ("4.Categorias_BSale",  sync_product_types),
            ("5.Tipos_Documento",   sync_document_types),
            ("6.Productos_Variant", sync_variants),
            ("7.Attrs_Categoria",   lambda: sync_product_type_attributes(force=full_refresh)),
            ("8.Attrs_Variantes",   lambda: sync_variant_attribute_values(force=full_refresh)),
            ("9.Stock",             sync_stock_levels),
            ("10.Recepciones",      lambda: sync_receptions(full_history=hist_flag)),
            ("11.Consumos",         lambda: sync_consumptions(full_history=hist_flag)),
            ("12.Costos",           lambda: sync_variant_costs(full=full_refresh)),
            ("13.CostosSucursal",   sync_variant_costs_by_office),
            ("14.ListasPrecio",     sync_price_lists),
        ]
        if not skip_stock_snapshot:
            fases.append(("15.StockHistory", snapshot_stock_history))

    if not skip_documents:
        fases.append(("16.Documentos", lambda: sync_documents(since_unix=since_unix)))

    for nombre, func in fases:
        t0 = time.time()
        try:
            r = func()
            dur = time.time() - t0
            phase_results[nombre] = {"resultado": str(r), "duracion_s": round(dur, 1)}
            logger.info("  %s: %.1fs | %s", nombre, dur, r)
        except Exception as exc:
            phase_results[nombre] = {"ERROR": str(exc)}
            logger.exception("  %s FALLO: %s", nombre, exc)

    clear_current_tenant()
    return phase_results


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="KAWII multi-tenant — sync completo por empresa"
    )
    parser.add_argument("--days", type=int, default=7,
                        help="Cuántos días atrás sincronizar documentos (default: 7)")
    parser.add_argument("--company-id", type=int, default=None,
                        help="Sincronizar solo la empresa con este id (default: todas activas)")
    parser.add_argument("--skip-documents", action="store_true",
                        help="Saltar la fase de documentos (útil si solo cambió catálogo)")
    parser.add_argument("--skip-stock-snapshot", action="store_true",
                        help="Saltar el snapshot diario de stock_history")
    parser.add_argument("--mode", choices=["full", "quick"], default="full",
                        help="'quick' refresca solo stock, recepciones/consumos recientes, costos nuevos y docs; 'full' corre todo el pipeline")
    parser.add_argument("--full-refresh", action="store_true",
                        help="Forzar descarga completa de costos, atributos e historial de recepciones/consumos")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)
    logger = logging.getLogger("mt_sync")

    # BSale codifica emissionDate como medianoche UTC del día calendario.
    _now_utc = datetime.now(timezone.utc)
    since_dt = (_now_utc - timedelta(days=args.days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    since_unix = int(since_dt.timestamp())

    companies = load_active_companies(filter_id=args.company_id)
    if not companies:
        logger.error(
            "No hay empresas activas con token cargado. "
            "Usá tools/set_company_token.py para cargar tokens."
        )
        return 2

    logger.info("=" * 72)
    logger.info("  KAWII MT_SYNC — %d empresas | %d días de documentos",
                len(companies), args.days)
    logger.info("  Desde: %s (unix=%d)", since_dt.strftime("%Y-%m-%d %H:%M"), since_unix)
    logger.info("=" * 72)

    db.init_pool()
    t_total = time.time()
    all_results: dict = {}

    try:
        for company in companies:
            t0 = time.time()
            try:
                results = run_pipeline_for_company(
                    company,
                    since_unix=since_unix,
                    skip_documents=args.skip_documents,
                    skip_stock_snapshot=args.skip_stock_snapshot,
                    mode=args.mode,
                    full_refresh=args.full_refresh,
                )
                all_results[company["name"]] = {
                    "id": company["id"],
                    "duracion_s": round(time.time() - t0, 1),
                    "fases": results,
                }
            except Exception as exc:
                logger.exception("  Empresa %s CRASHEO: %s", company["name"], exc)
                all_results[company["name"]] = {"ERROR": str(exc)}

        elapsed = time.time() - t_total

        # Resumen consolidado
        logger.info("=" * 72)
        logger.info("  RESUMEN MULTI-TENANT — %.1f min totales", elapsed / 60)
        logger.info("=" * 72)
        total_errores = 0
        for name, r in all_results.items():
            if "ERROR" in r:
                total_errores += 1
                logger.info("  %-20s FAIL: %s", name, r["ERROR"])
            else:
                errores = sum(
                    1 for f in r["fases"].values() if isinstance(f, dict) and "ERROR" in f
                )
                total_errores += errores
                estado = "OK " if errores == 0 else "WARN"
                logger.info(
                    "  %-20s %s | duración=%ss | errores=%d fase(s)",
                    name, estado, r["duracion_s"], errores,
                )
        return 1 if total_errores > 0 else 0

    except KeyboardInterrupt:
        logger.warning("Interrumpido por el usuario")
        return 130
    finally:
        db.close_pool()


if __name__ == "__main__":
    sys.exit(main())
