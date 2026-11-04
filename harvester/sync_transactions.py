"""
Sincronizadores de entidades transaccionales (alto volumen).

  - documents + document_details  (102K+ registros)
  - receptions + reception_details
"""

import logging
import threading
import time
import concurrent.futures
from datetime import datetime, timezone
from typing import Any

from harvester.bsale_client import paginate, paginate_newer, fetch, fetch_subresource
from harvester.config import (
    BSALE_BASE_URL, BSALE_PAGE_SIZE, BSALE_MAX_WORKERS,
)
from harvester.tenant_context import current_company_id
from harvester import db

logger = logging.getLogger("harvester.sync_transactions")


# Recepciones/consumos: cada cuánto se re-descarga el historial COMPLETO
# (para capturar ediciones de registros viejos). El resto de las corridas
# solo baja la "cola" de registros nuevos (ver bsale_client.paginate_newer).
FULL_HISTORY_REFRESH_HOURS = 24 * 7


def _resolve_full_history(entity: str, full_history: bool | None) -> bool:
    """None = automático: historial completo si no hubo uno OK en 7 días."""
    if full_history is not None:
        return full_history
    age = db.hours_since_last_success(entity, mode="full")
    return age is None or age >= FULL_HISTORY_REFRESH_HOURS


def _local_tail_stats(table: str, id_col: str, cid: int) -> dict[int, tuple[int, int]]:
    """{office_id: (count, max_id)} de lo que ya tenemos localmente."""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT bsale_office_id, COUNT(*), MAX({id_col}) "
                f"FROM {table} WHERE company_id = %s GROUP BY bsale_office_id",
                (cid,),
            )
            return {r[0]: (int(r[1]), int(r[2] or 0)) for r in cur.fetchall()}


def _paginate_office(endpoint: str, extra: str, oid: int, full_history: bool,
                     local: dict[int, tuple[int, int]]) -> tuple[list[dict], str]:
    """Pagina un listado por sucursal: cola incremental si se puede, si no full."""
    if not full_history and oid in local:
        cnt, max_id = local[oid]
        items = paginate_newer(endpoint, extra, known_count=cnt, known_max_id=max_id)
        if items is not None:
            return items, "incremental"
    return paginate(endpoint, extra), "full"


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _unix_to_ts(unix_ts: int) -> datetime | None:
    """Convierte unix timestamp BSale a datetime UTC. Valida rango razonable."""
    if not unix_ts or unix_ts < 946684800:  # antes de 2000-01-01
        return None
    try:
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    except (OSError, ValueError):
        return None


# ============================================================
# DOCUMENTS (Boletas, Facturas, Notas de Credito)
# ============================================================

def doc_dict_to_rows(doc: dict) -> tuple[tuple | None, list[tuple]]:
    """
    Convierte UN documento crudo de BSale en (doc_row, [det_rows]).

    Retorna (None, []) cuando el documento se debe saltear (estado != 0,
    sin tipo, fecha invalida, etc.) — los motivos quedan registrados en
    data_quality_issues vía db.log_quality_issue, igual que en el flujo batch.

    Si el documento trae >=25 detalles inline (limite de BSale) o el
    contador indica que hay mas, este helper hace fetch_subresource del
    listado completo de detalles antes de devolver los rows. Por eso es
    seguro llamarlo tanto desde el batch (paginas TURBO) como desde el
    webhook receiver (un solo documento recien fetchado).
    """
    doc_id = _safe_int(doc.get("id"))
    if doc_id == 0:
        return None, []

    if doc.get("state") != 0:
        return None, []

    dt = doc.get("document_type") or {}
    dt_id = _safe_int(dt.get("id"))
    if dt_id == 0:
        db.log_quality_issue("documents", doc_id, "document_type.id",
                             "NULL_REQUIRED", "Documento sin tipo")
        return None, []

    is_credit_note = dt.get("isCreditNote") == 1

    emission_ts = _unix_to_ts(_safe_int(doc.get("emissionDate")))
    if emission_ts is None:
        db.log_quality_issue("documents", doc_id, "emissionDate",
                             "INVALID_TYPE", "Fecha emision invalida",
                             str(doc.get("emissionDate")))
        return None, []

    generation_ts = _unix_to_ts(_safe_int(doc.get("generationDate")))
    office_id = _safe_int((doc.get("office") or {}).get("id"))
    user_id = _safe_int((doc.get("user") or {}).get("id")) or None

    cid = current_company_id()
    doc_row = (
        cid,
        doc_id,
        dt_id,
        office_id,
        emission_ts,
        generation_ts,
        doc.get("serialNumber") or None,
        _safe_int(doc.get("number")) or None,
        _safe_float(doc.get("totalAmount")),
        _safe_float(doc.get("netAmount")),
        _safe_float(doc.get("taxAmount")),
        _safe_float(doc.get("exemptAmount")),
        is_credit_note,
        True,  # is_active (ya filtramos state=0)
        user_id,
        doc.get("token") or None,
    )

    # --- Detalles (Motor de Excavación) ---
    details_container = doc.get("details") or {}
    detail_items = details_container.get("items") or []
    detail_count = _safe_int(details_container.get("count"))

    # Trampa de los 25: BSale inline limit. Si >= 25, paginar directo.
    needs_deep_fetch = (
        len(detail_items) >= 25
        or (detail_count > len(detail_items))
    )
    if needs_deep_fetch:
        deep_url = f"{BSALE_BASE_URL}/documents/{doc_id}/details.json"
        detail_items = fetch_subresource(deep_url, page_size=50)

    det_rows: list[tuple] = []
    for det in detail_items:
        det_id = _safe_int(det.get("id"))
        if det_id == 0:
            continue

        variant_info = det.get("variant") or {}
        variant_id = _safe_int(variant_info.get("id"))
        if variant_id == 0:
            db.log_quality_issue("document_details", det_id, "variant.id",
                                 "NULL_REQUIRED",
                                 f"Detalle sin variante (doc={doc_id})")
            continue

        det_rows.append((
            cid,
            det_id,
            doc_id,
            variant_id,
            _safe_float(det.get("quantity")),
            _safe_float(det.get("netUnitValue")),
            _safe_float(det.get("netUnitValueRaw")),
            _safe_float(det.get("totalUnitValue")),
            _safe_float(det.get("netAmount")),
            _safe_float(det.get("taxAmount")),
            _safe_float(det.get("totalAmount")),
            _safe_float(det.get("discountPercentage")),
            _safe_float(det.get("netDiscount")),
            det.get("gratuity") == 1,
        ))

    return doc_row, det_rows


def _process_doc_page(url: str) -> dict:
    """
    Procesa UNA pagina de documentos (TURBO worker).
    Retorna dict con doc_rows, det_rows, fetched, skipped.
    Se ejecuta en un hilo del ThreadPoolExecutor.
    """
    result = {"doc_rows": [], "det_rows": [], "fetched": 0, "skipped": 0}

    data = fetch(url)
    if not data:
        # Segunda oportunidad antes de abortar para no perder 50 boletas silenciosamente
        time.sleep(2.0)
        data = fetch(url)
        if not data:
            raise RuntimeError(f"Fallo descarga de pagina de documentos: {url}")
    if "items" not in data:
        return result

    items = data["items"]
    if not items:
        return result

    result["fetched"] = len(items)

    for doc in items:
        doc_row, det_rows = doc_dict_to_rows(doc)
        if doc_row is None:
            result["skipped"] += 1
            continue
        result["doc_rows"].append(doc_row)
        result["det_rows"].extend(det_rows)

    return result


def sync_documents(since_unix: int | None = None) -> dict:
    """
    Sincroniza documentos de venta en modo TURBO (paralelo).

    Usa ThreadPoolExecutor para procesar múltiples páginas de la API
    simultáneamente, reduciendo drásticamente el tiempo de sync.

    Args:
        since_unix: Si se provee, solo sincroniza documentos desde esa fecha
                    (para sync incremental). Si None, sincroniza todo.
    """
    params_info = {"since_unix": since_unix}
    log_id = db.sync_start("documents", params_info)
    stats = {"fetched": 0, "inserted": 0, "skipped": 0, "details_inserted": 0}

    try:
        extra = "&state=0&expand=%5Bdetails%2Cdocument_type%5D"
        if since_unix:
            import time as _time
            # +86400s evita perder boletas emitidas hoy por desfase UTC vs medianoche BSale
            now_unix = int(_time.time()) + 86400
            extra += f"&emissiondaterange=[{since_unix},{now_unix}]"

        # Primero contar el total para pre-computar URLs
        count_url = f"{BSALE_BASE_URL}/documents.json?limit=1{extra}"
        meta = fetch(count_url)
        if not meta:
            raise RuntimeError("BSale no respondio al consultar conteo de documentos")
        total_expected = meta.get("count", 0)
        logger.info("Documentos a sincronizar: %d", total_expected)

        if total_expected == 0:
            db.sync_finish(log_id, fetched=0, inserted=0)
            return stats

        # === TURBO: Pre-computar TODAS las URLs de paginas ===
        urls = [
            f"{BSALE_BASE_URL}/documents.json"
            f"?limit={BSALE_PAGE_SIZE}&offset={o}{extra}"
            for o in range(0, total_expected, BSALE_PAGE_SIZE)
        ]
        total_pages = len(urls)
        logger.info("TURBO: %d paginas pre-computadas, %d workers",
                     total_pages, BSALE_MAX_WORKERS)

        # === TURBO: Procesar todas las paginas en paralelo ===
        batch_doc_rows = []
        batch_det_rows = []
        pages_done = 0

        last_page_fetched = 0
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=BSALE_MAX_WORKERS
        ) as exe:
            for page_result in exe.map(_process_doc_page, urls):
                batch_doc_rows.extend(page_result["doc_rows"])
                batch_det_rows.extend(page_result["det_rows"])
                stats["fetched"] += page_result["fetched"]
                stats["skipped"] += page_result["skipped"]
                last_page_fetched = page_result["fetched"]
                pages_done += 1

                # Flush batch cada 2000 docs para no acumular demasiado en RAM
                if len(batch_doc_rows) >= 2000:
                    _flush_documents(batch_doc_rows, batch_det_rows)
                    stats["inserted"] += len(batch_doc_rows)
                    stats["details_inserted"] += len(batch_det_rows)
                    batch_doc_rows = []
                    batch_det_rows = []

                # Progreso cada 20 paginas
                if pages_done % 20 == 0:
                    logger.info("TURBO: %d/%d paginas | %d/%d docs procesados",
                                pages_done, total_pages,
                                stats["fetched"], total_expected)

        # Si se emitieron ventas durante la descarga y superaron total_expected
        next_offset = len(urls) * BSALE_PAGE_SIZE
        while last_page_fetched == BSALE_PAGE_SIZE:
            tail_url = (
                f"{BSALE_BASE_URL}/documents.json"
                f"?limit={BSALE_PAGE_SIZE}&offset={next_offset}{extra}"
            )
            page_result = _process_doc_page(tail_url)
            last_page_fetched = page_result["fetched"]
            if not last_page_fetched:
                break
            batch_doc_rows.extend(page_result["doc_rows"])
            batch_det_rows.extend(page_result["det_rows"])
            stats["fetched"] += page_result["fetched"]
            stats["skipped"] += page_result["skipped"]
            next_offset += BSALE_PAGE_SIZE

        # Flush final
        if batch_doc_rows:
            _flush_documents(batch_doc_rows, batch_det_rows)
            stats["inserted"] += len(batch_doc_rows)
            stats["details_inserted"] += len(batch_det_rows)

        # Documentos que se anularon en BSale DESPUÉS de haberse sincronizado.
        stats["annulled_removed"] = _remove_annulled_documents(since_unix)

        logger.info("TURBO completado: %d headers, %d detalles insertados",
                     stats["inserted"], stats["details_inserted"])

        db.sync_finish(log_id, fetched=stats["fetched"],
                        inserted=stats["inserted"], skipped=stats["skipped"])
    except Exception as exc:
        db.sync_finish(log_id, status="FAILED", error=str(exc),
                        fetched=stats["fetched"])
        raise

    return stats


# Ventana mínima para buscar anulaciones: una boleta puede anularse días o
# semanas después de emitida, aunque el sync incremental mire solo 7 días.
ANNULLED_LOOKBACK_DAYS = 120


def _remove_annulled_documents(since_unix: int | None) -> int:
    """Quita de la BD los documentos que en BSale quedaron anulados (state != 0).

    El sync solo guarda documentos activos (state=0) y la mayoría de los
    reportes cuentan todo lo que hay en `documents`. Si un documento se anula
    después de sincronizado, el filtro state=0 ya no lo trae y quedaba contado
    como venta para siempre. Se borra igual que si nunca hubiera entrado, y
    cada uno queda registrado en data_quality_issues (ANNULLED_IN_BSALE) con su
    monto.
    """
    import time as _time
    now_unix = int(_time.time()) + 86400
    floor = now_unix - ANNULLED_LOOKBACK_DAYS * 86400
    start = min(since_unix, floor) if since_unix else None
    extra = "&state=1" + (f"&emissiondaterange=[{start},{now_unix}]" if start else "")
    items = paginate("/documents.json", extra)
    ids = [_safe_int(d.get("id")) for d in items if _safe_int(d.get("id")) and d.get("state") != 0]
    if not ids:
        return 0
    cid = current_company_id()
    removed = 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT bsale_document_id, total_amount, emission_date FROM documents "
                "WHERE company_id = %s AND bsale_document_id = ANY(%s)",
                (cid, ids),
            )
            found = cur.fetchall()
            if found:
                found_ids = [r[0] for r in found]
                cur.execute("DELETE FROM document_details WHERE company_id = %s AND bsale_document_id = ANY(%s)",
                            (cid, found_ids))
                cur.execute("DELETE FROM documents WHERE company_id = %s AND bsale_document_id = ANY(%s)",
                            (cid, found_ids))
                removed = cur.rowcount or 0
        conn.commit()
    for doc_id, total, emission in found:
        db.log_quality_issue("documents", doc_id, "state", "ANNULLED_IN_BSALE",
                             f"Documento anulado en BSale después de sincronizado; se quitó de la BD "
                             f"(emisión {emission:%Y-%m-%d})", str(total))
    if removed:
        logger.warning("Documentos anulados en BSale quitados de la BD: %d", removed)
    return removed


def _flush_documents(doc_rows: list[tuple], det_rows: list[tuple]):
    """Inserta batch de documentos y sus detalles."""
    sql_doc = """
        INSERT INTO documents (company_id, bsale_document_id, bsale_document_type_id,
                               bsale_office_id, emission_date, generation_date,
                               serial_number, doc_number, total_amount, net_amount,
                               tax_amount, exempt_amount, is_credit_note,
                               is_active, bsale_user_id, token, synced_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (company_id, bsale_document_id) DO UPDATE SET
            total_amount = EXCLUDED.total_amount,
            net_amount = EXCLUDED.net_amount,
            tax_amount = EXCLUDED.tax_amount,
            is_active = EXCLUDED.is_active,
            synced_at = NOW()
        WHERE (documents.total_amount, documents.net_amount, documents.tax_amount, documents.is_active)
              IS DISTINCT FROM
              (EXCLUDED.total_amount, EXCLUDED.net_amount, EXCLUDED.tax_amount, EXCLUDED.is_active)
    """
    db.execute_batch(sql_doc, doc_rows)

    sql_det = """
        INSERT INTO document_details (company_id, bsale_detail_id, bsale_document_id,
                                      bsale_variant_id, quantity, net_unit_value,
                                      net_unit_value_raw, total_unit_value,
                                      net_amount, tax_amount, total_amount,
                                      discount_percentage, net_discount,
                                      is_gratuity, synced_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (company_id, bsale_detail_id) DO UPDATE SET
            quantity = EXCLUDED.quantity,
            net_unit_value = EXCLUDED.net_unit_value,
            total_amount = EXCLUDED.total_amount,
            discount_percentage = EXCLUDED.discount_percentage,
            synced_at = NOW()
        WHERE (document_details.quantity, document_details.net_unit_value,
               document_details.total_amount, document_details.discount_percentage)
              IS DISTINCT FROM
              (EXCLUDED.quantity, EXCLUDED.net_unit_value,
               EXCLUDED.total_amount, EXCLUDED.discount_percentage)
    """
    db.execute_batch(sql_det, det_rows)


# ============================================================
# RECEPTIONS + RECEPTION DETAILS
# ============================================================

def sync_receptions(full_history: bool | None = None) -> dict:
    """Sincroniza recepciones de stock de todas las sucursales.

    Fetch + processing en paralelo por sucursal. Cada worker construye sus
    rec_rows/det_rows locales y al final se hace UN solo batch insert global
    (menos roundtrips DB que el flush por-sucursal del modelo anterior).

    full_history:
        True  → baja todo el historial (comportamiento previo).
        False → solo la cola de recepciones nuevas por sucursal.
        None  → automático: full si no hubo uno OK en los últimos 7 días.
    """
    full_history = _resolve_full_history("receptions", full_history)
    mode = "full" if full_history else "incremental"
    log_id = db.sync_start("receptions", {"mode": mode})
    stats = {"fetched": 0, "inserted": 0, "skipped": 0, "details_inserted": 0,
             "mode": mode}

    try:
        cid = current_company_id()
        local = {} if full_history else _local_tail_stats(
            "receptions", "bsale_reception_id", cid)
        # Obtener offices para ESTA empresa
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT bsale_office_id FROM offices "
                    "WHERE is_active = TRUE AND company_id = %s",
                    (cid,),
                )
                office_ids = [row[0] for row in cur.fetchall()]

        # Thread-safe locks para el log_quality_issue (un solo llamador a la vez
        # para no congestionar el pool de DB durante los workers).
        quality_lock = threading.Lock()

        def _fetch_and_process(oid: int) -> tuple[int, list, list, dict]:
            """Devuelve (oid, rec_rows, det_rows, local_stats)."""
            local_stats = {"fetched": 0, "skipped": 0}
            items, how = _paginate_office(
                "/stocks/receptions.json",
                f"&officeid={oid}&expand=%5Bdetails%2Cdocument%5D",
                oid, full_history, local,
            )
            logger.info("Recepciones office %d: %d registros (%s)", oid, len(items), how)
            local_stats["fetched"] = len(items)

            rec_rows: list[tuple] = []
            det_rows: list[tuple] = []

            for rec in items:
                rec_id = _safe_int(rec.get("id"))
                if rec_id == 0:
                    local_stats["skipped"] += 1
                    continue

                admission_unix = _safe_int(
                    rec.get("documentDate") or rec.get("admissionDate") or 0
                )
                admission_ts = _unix_to_ts(admission_unix)
                if admission_ts is None:
                    with quality_lock:
                        db.log_quality_issue("receptions", rec_id, "admissionDate",
                                             "INVALID_TYPE", "Fecha invalida",
                                             str(rec.get("admissionDate")))
                    local_stats["skipped"] += 1
                    continue

                note = rec.get("note") or ""
                is_dispatch = _safe_int(rec.get("internalDispatchId")) > 0
                is_transfer = is_dispatch or "TRASLADO" in note.upper()

                raw_date = rec.get("rawAdmissionDate") or None
                office_id = _safe_int((rec.get("office") or {}).get("id"))
                user_id = _safe_int((rec.get("user") or {}).get("id")) or None

                rec_rows.append((
                    cid,
                    rec_id,
                    office_id,
                    admission_ts,
                    raw_date,
                    rec.get("document") if rec.get("document") != "Sin Documento" else None,
                    rec.get("documentNumber") or None,
                    note or None,
                    is_dispatch,
                    is_transfer,
                    user_id,
                ))

                # Detalles — "Motor de Excavación" (idem versión serial)
                details_container = rec.get("details") or {}
                detail_items = details_container.get("items") or []
                detail_count = _safe_int(details_container.get("count"))

                needs_deep_fetch = (
                    len(detail_items) >= 25
                    or (detail_count > len(detail_items))
                )
                if needs_deep_fetch:
                    deep_url = f"{BSALE_BASE_URL}/stocks/receptions/{rec_id}/details.json"
                    detail_items = fetch_subresource(deep_url, page_size=50)

                for det in detail_items:
                    det_id = _safe_int(det.get("id"))
                    variant_id = _safe_int((det.get("variant") or {}).get("id"))
                    if det_id == 0 or variant_id == 0:
                        continue

                    det_rows.append((
                        cid,
                        det_id,
                        rec_id,
                        variant_id,
                        _safe_float(det.get("quantity")),
                        _safe_float(det.get("cost")),
                    ))

            return oid, rec_rows, det_rows, local_stats

        # ── Paralelo: 1 worker por sucursal ──
        max_workers = min(len(office_ids), 4) or 1
        all_rec_rows: list[tuple] = []
        all_det_rows: list[tuple] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as exe:
            futures = [exe.submit(_fetch_and_process, oid) for oid in office_ids]
            for fut in concurrent.futures.as_completed(futures):
                _oid, rec_rows, det_rows, local_stats = fut.result()
                all_rec_rows.extend(rec_rows)
                all_det_rows.extend(det_rows)
                stats["fetched"] += local_stats["fetched"]
                stats["skipped"] += local_stats["skipped"]

        # ── Batch inserts globales (una sola roundtrip por tabla) ──
        sql_rec = """
            INSERT INTO receptions (company_id, bsale_reception_id, bsale_office_id,
                                    admission_date, admission_date_raw,
                                    document_ref, document_number, note,
                                    is_internal_dispatch, is_transfer,
                                    bsale_user_id, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (company_id, bsale_reception_id) DO UPDATE SET
                admission_date = EXCLUDED.admission_date,
                document_ref = EXCLUDED.document_ref,
                document_number = EXCLUDED.document_number,
                note = EXCLUDED.note,
                is_transfer = EXCLUDED.is_transfer,
                bsale_user_id = EXCLUDED.bsale_user_id,
                synced_at = NOW()
            WHERE (receptions.admission_date, receptions.document_ref, receptions.document_number,
                   receptions.note, receptions.is_transfer, receptions.bsale_user_id)
                  IS DISTINCT FROM
                  (EXCLUDED.admission_date, EXCLUDED.document_ref, EXCLUDED.document_number,
                   EXCLUDED.note, EXCLUDED.is_transfer, EXCLUDED.bsale_user_id)
        """
        db.execute_batch(sql_rec, all_rec_rows)
        stats["inserted"] = len(all_rec_rows)

        # CRÍTICO: WHERE IS DISTINCT FROM hace que synced_at solo cambie en filas
        # nuevas o cuyo costo/cantidad fue editado; sync_variant_costs usa
        # rd.synced_at >= vc.synced_at para detectar incluso facturas con fecha atrasada.
        sql_det = """
            INSERT INTO reception_details (company_id, bsale_reception_detail_id,
                                           bsale_reception_id, bsale_variant_id,
                                           quantity, cost, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (company_id, bsale_reception_detail_id) DO UPDATE SET
                quantity = EXCLUDED.quantity,
                cost = EXCLUDED.cost,
                synced_at = NOW()
            WHERE (reception_details.quantity, reception_details.cost)
                  IS DISTINCT FROM
                  (EXCLUDED.quantity, EXCLUDED.cost)
        """
        db.execute_batch(sql_det, all_det_rows)
        stats["details_inserted"] = len(all_det_rows)

        logger.info("Recepciones: %d headers, %d detalles",
                     stats["inserted"], stats["details_inserted"])

        db.sync_finish(log_id, fetched=stats["fetched"],
                        inserted=stats["inserted"], skipped=stats["skipped"])
    except Exception as exc:
        db.sync_finish(log_id, status="FAILED", error=str(exc),
                        fetched=stats["fetched"])
        raise

    return stats


def sync_consumptions(full_history: bool | None = None) -> dict:
    """Sincroniza consumos de stock (mermas, uso interno) de todas las sucursales.

    Fetch + processing paralelo por sucursal (mismo patrón que sync_receptions).
    `full_history` igual que en sync_receptions.
    """
    full_history = _resolve_full_history("consumptions", full_history)
    mode = "full" if full_history else "incremental"
    log_id = db.sync_start("consumptions", {"mode": mode})
    stats = {"fetched": 0, "inserted": 0, "skipped": 0, "details_inserted": 0,
             "mode": mode}

    try:
        cid = current_company_id()
        local = {} if full_history else _local_tail_stats(
            "consumptions", "bsale_consumption_id", cid)
        # Obtener offices para ESTA empresa
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT bsale_office_id FROM offices "
                    "WHERE is_active = TRUE AND company_id = %s",
                    (cid,),
                )
                office_ids = [row[0] for row in cur.fetchall()]

        def _fetch_and_process(oid: int) -> tuple[int, list, list, dict]:
            local_stats = {"fetched": 0, "skipped": 0}
            items, how = _paginate_office(
                "/stocks/consumptions.json",
                f"&officeid={oid}&expand=%5Bdetails%5D",
                oid, full_history, local,
            )
            logger.info("Consumos office %d: %d registros (%s)", oid, len(items), how)
            local_stats["fetched"] = len(items)

            cons_rows: list[tuple] = []
            det_rows: list[tuple] = []

            for cons in items:
                cons_id = _safe_int(cons.get("id"))
                if cons_id == 0:
                    local_stats["skipped"] += 1
                    continue

                consumption_unix = _safe_int(cons.get("consumptionDate") or 0)
                consumption_ts = _unix_to_ts(consumption_unix)
                if consumption_ts is None:
                    local_stats["skipped"] += 1
                    continue

                note = cons.get("note") or ""
                office_id = _safe_int((cons.get("office") or {}).get("id"))

                cons_rows.append((
                    cid,
                    cons_id,
                    office_id,
                    consumption_ts,
                    note or None,
                ))

                details_container = cons.get("details") or {}
                detail_items = details_container.get("items") or []
                detail_count = _safe_int(details_container.get("count"))

                needs_deep_fetch = (
                    len(detail_items) >= 25
                    or (detail_count > len(detail_items))
                )
                if needs_deep_fetch:
                    deep_url = f"{BSALE_BASE_URL}/stocks/consumptions/{cons_id}/details.json"
                    detail_items = fetch_subresource(deep_url, page_size=50)

                for det in detail_items:
                    det_id = _safe_int(det.get("id"))
                    variant_id = _safe_int((det.get("variant") or {}).get("id"))
                    if det_id == 0 or variant_id == 0:
                        continue

                    det_rows.append((
                        cid,
                        det_id,
                        cons_id,
                        variant_id,
                        _safe_float(det.get("quantity")),
                    ))

            return oid, cons_rows, det_rows, local_stats

        # ── Paralelo: 1 worker por sucursal ──
        max_workers = min(len(office_ids), 4) or 1
        all_cons_rows: list[tuple] = []
        all_det_rows: list[tuple] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as exe:
            futures = [exe.submit(_fetch_and_process, oid) for oid in office_ids]
            for fut in concurrent.futures.as_completed(futures):
                _oid, cons_rows, det_rows, local_stats = fut.result()
                all_cons_rows.extend(cons_rows)
                all_det_rows.extend(det_rows)
                stats["fetched"] += local_stats["fetched"]
                stats["skipped"] += local_stats["skipped"]

        # ── Batch inserts globales ──
        sql_cons = """
            INSERT INTO consumptions (company_id, bsale_consumption_id, bsale_office_id,
                                      consumption_date, note)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (company_id, bsale_consumption_id) DO UPDATE SET
                consumption_date = EXCLUDED.consumption_date,
                note = EXCLUDED.note
            WHERE (consumptions.consumption_date, consumptions.note)
                  IS DISTINCT FROM
                  (EXCLUDED.consumption_date, EXCLUDED.note)
        """
        db.execute_batch(sql_cons, all_cons_rows)
        stats["inserted"] = len(all_cons_rows)

        sql_det = """
            INSERT INTO consumption_details (company_id, bsale_consumption_detail_id,
                                             bsale_consumption_id, bsale_variant_id, quantity)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (company_id, bsale_consumption_detail_id) DO UPDATE SET
                quantity = EXCLUDED.quantity
            WHERE consumption_details.quantity IS DISTINCT FROM EXCLUDED.quantity
        """
        db.execute_batch(sql_det, all_det_rows)
        stats["details_inserted"] = len(all_det_rows)

        logger.info("Consumos: %d headers, %d detalles",
                     stats["inserted"], stats["details_inserted"])

        db.sync_finish(log_id, fetched=stats["fetched"],
                        inserted=stats["inserted"], skipped=stats["skipped"])
        return stats
    except Exception as exc:
        db.sync_finish(log_id, status="FAILED", error=str(exc),
                        fetched=stats["fetched"])
        raise
