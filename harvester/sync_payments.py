"""
Sincronizadores de medios de pago, pagos y devoluciones.

  - payment_types              (/payment_types.json, ~15 registros)
  - payments                   (/payments.json, ~200K registros)
  - returns + return_details   (/returns.json, decenas de registros)

Particularidades de /payments.json (verificadas contra la API, nov-2026):
  * Solo filtran `state`, `documentid` y `recorddate` (fecha EXACTA).
    `recorddaterange`, `createdatrange`, `officeid` y `paymenttypeid` se
    ignoran en silencio (devuelven el total).
  * El listado viene ordenado por recordDate (no por id), así que los pagos
    nuevos caen al final y no sirve paginate_newer (que exige ids ascendentes).
  * recordDate es la medianoche UTC del día calendario (igual que emissionDate).

Estrategia:
  * incremental → un `recorddate=<día>` por cada día de la ventana.
  * full        → listado completo + re-descarga por día de hoy/ayer, porque
                  dentro del bloque del día el orden no es estable mientras
                  entran pagos nuevos.
  Se piden pagos de cualquier estado: los anulados (state != 0) se guardan
  con is_active = false para que dejen de sumar.
"""

import concurrent.futures
import logging
import time
from datetime import datetime, timezone

from harvester.bsale_client import paginate, fetch, fetch_subresource
from harvester.config import BSALE_BASE_URL
from harvester.tenant_context import current_company_id
from harvester.sync_transactions import _safe_int, _safe_float, _unix_to_ts
from harvester import db

logger = logging.getLogger("harvester.sync_payments")

DAY_SECONDS = 86400

# Cada cuánto se re-descarga el listado COMPLETO de pagos (para capturar
# anulaciones de pagos viejos que ya salieron de la ventana incremental).
PAYMENTS_FULL_REFRESH_HOURS = 24 * 7

# Días (hacia atrás desde hoy) que el modo full vuelve a pedir por fecha.
FULL_TAIL_DAYS = 2

# Filas por transacción al hacer upsert de pagos.
PAYMENTS_FLUSH_SIZE = 10_000


def _flag(val) -> bool:
    return _safe_int(val) == 1


# ============================================================
# PAYMENT TYPES
# ============================================================

def sync_payment_types() -> dict:
    """Sincroniza los medios de pago (activos e inactivos)."""
    log_id = db.sync_start("payment_types")
    stats = {"fetched": 0, "inserted": 0, "skipped": 0}
    try:
        cid = current_company_id()
        items = paginate("/payment_types.json")
        stats["fetched"] = len(items)

        rows: list[tuple] = []
        for it in items:
            pt_id = _safe_int(it.get("id"))
            name = (it.get("name") or "").strip()
            if pt_id == 0 or not name:
                stats["skipped"] += 1
                continue
            rows.append((
                cid,
                pt_id,
                name[:120],
                _safe_int(it.get("state")) == 0,
                _flag(it.get("isCash")),
                _flag(it.get("isClientCredit")),
                _flag(it.get("isCreditMemo")),
                _flag(it.get("isCreditNote")),
            ))

        sql = """
            INSERT INTO payment_types (company_id, bsale_payment_type_id, name, is_active,
                                       is_cash, is_client_credit, is_credit_memo,
                                       is_credit_note, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (company_id, bsale_payment_type_id) DO UPDATE SET
                name = EXCLUDED.name,
                is_active = EXCLUDED.is_active,
                is_cash = EXCLUDED.is_cash,
                is_client_credit = EXCLUDED.is_client_credit,
                is_credit_memo = EXCLUDED.is_credit_memo,
                is_credit_note = EXCLUDED.is_credit_note,
                synced_at = NOW()
            WHERE (payment_types.name, payment_types.is_active, payment_types.is_cash,
                   payment_types.is_client_credit, payment_types.is_credit_memo,
                   payment_types.is_credit_note)
                  IS DISTINCT FROM
                  (EXCLUDED.name, EXCLUDED.is_active, EXCLUDED.is_cash,
                   EXCLUDED.is_client_credit, EXCLUDED.is_credit_memo,
                   EXCLUDED.is_credit_note)
        """
        db.execute_batch(sql, rows)
        stats["inserted"] = len(rows)
        db.sync_finish(log_id, fetched=stats["fetched"],
                       inserted=stats["inserted"], skipped=stats["skipped"])
    except Exception as exc:
        db.sync_finish(log_id, status="FAILED", error=str(exc),
                       fetched=stats["fetched"])
        raise
    return stats


# ============================================================
# PAYMENTS
# ============================================================

def payment_dict_to_row(p: dict, cid: int) -> tuple | None:
    """Convierte UN pago crudo de BSale en la fila de `payments`.

    Devuelve None si el pago no se puede guardar (sin id o fecha inválida).
    Los pagos anulados (state != 0) SÍ se devuelven, con is_active = False.
    """
    pay_id = _safe_int(p.get("id"))
    if pay_id == 0:
        return None
    record_ts = _unix_to_ts(_safe_int(p.get("recordDate")))
    if record_ts is None:
        db.log_quality_issue("payments", pay_id, "recordDate", "INVALID_TYPE",
                             "Fecha de pago invalida", str(p.get("recordDate")))
        return None

    def _ref(key: str) -> int | None:
        return _safe_int((p.get(key) or {}).get("id")) or None

    return (
        cid,
        pay_id,
        _ref("document"),
        _ref("payment_type"),
        _ref("office"),
        _ref("user"),
        round(_safe_float(p.get("amount")), 2),
        record_ts,
        _unix_to_ts(_safe_int(p.get("createdAt"))),
        _flag(p.get("isCreditPayment")),
        _safe_int(p.get("state")) == 0,
    )


def _resolve_payments_full(full_history: bool | None, since_unix: int | None) -> bool:
    """Sin ventana → full. None = automático: full si no hubo uno OK en 7 días."""
    if not since_unix:
        return True
    if full_history is not None:
        return full_history
    age = db.hours_since_last_success("payments", mode="full")
    return age is None or age >= PAYMENTS_FULL_REFRESH_HOURS


def _payment_days(since_unix: int, now_unix: int) -> list[int]:
    """Medianoches UTC desde `since_unix` hasta el día UTC actual inclusive.

    recordDate es la fecha local de Perú codificada como medianoche UTC, que
    nunca va por delante del día UTC actual: con incluir hoy alcanza.
    """
    start = (since_unix // DAY_SECONDS) * DAY_SECONDS
    end = (now_unix // DAY_SECONDS) * DAY_SECONDS
    return list(range(start, end + 1, DAY_SECONDS))


def _fetch_payments_by_day(days: list[int], max_workers: int = 4) -> list[dict]:
    """Baja los pagos (cualquier estado) de cada día con `recorddate=<día>`."""
    if not days:
        return []

    def _one(day: int) -> list[dict]:
        return paginate("/payments.json", f"&recorddate={day}", max_workers=4)

    items: list[dict] = []
    workers = max(1, min(max_workers, len(days)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as exe:
        for day_items in exe.map(_one, days):
            items.extend(day_items)
    return items


def _payments_count_in_bsale() -> int | None:
    meta = fetch(f"{BSALE_BASE_URL}/payments.json?limit=1")
    total = meta.get("count") if meta else None
    return total if isinstance(total, int) else None


def sync_payments(since_unix: int | None = None,
                  full_history: bool | None = None) -> dict:
    """Sincroniza los pagos de BSale.

    Args:
        since_unix: inicio de la ventana incremental (se recorre por día de
            recordDate hasta hoy). None → listado completo.
        full_history: True fuerza el listado completo, False fuerza el
            incremental, None = automático (completo si no hubo uno OK en
            los últimos 7 días, para capturar anulaciones de pagos viejos).
    """
    full = _resolve_payments_full(full_history, since_unix)
    mode = "full" if full else "incremental"
    log_id = db.sync_start("payments", {"mode": mode, "since_unix": since_unix})
    stats = {"fetched": 0, "inserted": 0, "skipped": 0, "inactive": 0,
             "duplicates": 0, "mode": mode, "bsale_count": None}

    try:
        cid = current_company_id()
        now_unix = int(time.time())

        if full:
            stats["bsale_count"] = _payments_count_in_bsale()
            items = paginate("/payments.json")
            tail_since = now_unix - FULL_TAIL_DAYS * DAY_SECONDS
            items.extend(_fetch_payments_by_day(_payment_days(tail_since, now_unix)))
        else:
            days = _payment_days(since_unix, now_unix)
            logger.info("Pagos: %d dias desde %s", len(days),
                        datetime.fromtimestamp(days[0], tz=timezone.utc).date())
            items = _fetch_payments_by_day(days)

        # Dedupe por id (la última aparición gana: la descarga por día es la
        # más reciente en el modo full).
        by_id: dict[int, dict] = {}
        for p in items:
            pid = _safe_int(p.get("id"))
            if pid:
                by_id[pid] = p
        stats["fetched"] = len(by_id)
        stats["duplicates"] = len(items) - len(by_id)

        rows: list[tuple] = []
        for p in by_id.values():
            row = payment_dict_to_row(p, cid)
            if row is None:
                stats["skipped"] += 1
                continue
            if not row[-1]:
                stats["inactive"] += 1
            rows.append(row)

        if full and stats["bsale_count"] is not None and len(by_id) < stats["bsale_count"]:
            logger.warning("Pagos: BSale reporta %d pero se descargaron %d unicos",
                           stats["bsale_count"], len(by_id))

        for i in range(0, len(rows), PAYMENTS_FLUSH_SIZE):
            chunk = rows[i:i + PAYMENTS_FLUSH_SIZE]
            _flush_payments(chunk)
            stats["inserted"] += len(chunk)

        logger.info("Pagos (%s): %d unicos, %d upsert, %d anulados",
                    mode, stats["fetched"], stats["inserted"], stats["inactive"])
        db.sync_finish(log_id, fetched=stats["fetched"],
                       inserted=stats["inserted"], skipped=stats["skipped"])
    except Exception as exc:
        db.sync_finish(log_id, status="FAILED", error=str(exc),
                       fetched=stats["fetched"])
        raise

    return stats


def _flush_payments(rows: list[tuple]) -> None:
    sql = """
        INSERT INTO payments (company_id, bsale_payment_id, bsale_document_id,
                              bsale_payment_type_id, bsale_office_id, bsale_user_id,
                              amount, record_date, bsale_created_at,
                              is_credit_payment, is_active, synced_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (company_id, bsale_payment_id) DO UPDATE SET
            bsale_document_id = EXCLUDED.bsale_document_id,
            bsale_payment_type_id = EXCLUDED.bsale_payment_type_id,
            bsale_office_id = EXCLUDED.bsale_office_id,
            bsale_user_id = EXCLUDED.bsale_user_id,
            amount = EXCLUDED.amount,
            record_date = EXCLUDED.record_date,
            bsale_created_at = EXCLUDED.bsale_created_at,
            is_credit_payment = EXCLUDED.is_credit_payment,
            is_active = EXCLUDED.is_active,
            synced_at = NOW()
        WHERE (payments.bsale_document_id, payments.bsale_payment_type_id,
               payments.bsale_office_id, payments.bsale_user_id, payments.amount,
               payments.record_date, payments.bsale_created_at,
               payments.is_credit_payment, payments.is_active)
              IS DISTINCT FROM
              (EXCLUDED.bsale_document_id, EXCLUDED.bsale_payment_type_id,
               EXCLUDED.bsale_office_id, EXCLUDED.bsale_user_id, EXCLUDED.amount,
               EXCLUDED.record_date, EXCLUDED.bsale_created_at,
               EXCLUDED.is_credit_payment, EXCLUDED.is_active)
    """
    db.execute_batch(sql, rows)


# ============================================================
# RETURNS + RETURN DETAILS
# ============================================================

def return_dict_to_rows(r: dict, cid: int) -> tuple[tuple | None, list[tuple]]:
    """Convierte UNA devolución cruda de BSale en (ret_row, [det_rows]).

    Si el detalle viene truncado (>= 25 inline o count mayor), baja el
    sub-recurso completo, igual que documentos y recepciones.
    """
    ret_id = _safe_int(r.get("id"))
    if ret_id == 0:
        return None, []
    return_ts = _unix_to_ts(_safe_int(r.get("returnDate")))
    if return_ts is None:
        db.log_quality_issue("returns", ret_id, "returnDate", "INVALID_TYPE",
                             "Fecha de devolucion invalida", str(r.get("returnDate")))
        return None, []

    def _ref(key: str) -> int | None:
        return _safe_int((r.get(key) or {}).get("id")) or None

    ret_row = (
        cid,
        ret_id,
        (r.get("code") or None),
        return_ts,
        ((r.get("motive") or "").strip()[:500] or None),
        _safe_int(r.get("type")) if r.get("type") is not None else None,
        _flag(r.get("priceAdjustment")),
        _flag(r.get("editTexts")),
        round(_safe_float(r.get("amount")), 2),
        _ref("office"),
        _ref("user"),
        _ref("reference_document"),
        _ref("credit_note"),
    )

    container = r.get("details") or {}
    detail_items = container.get("items") or []
    if len(detail_items) >= 25 or _safe_int(container.get("count")) > len(detail_items):
        detail_items = fetch_subresource(
            f"{BSALE_BASE_URL}/returns/{ret_id}/details.json", page_size=50)

    det_rows: list[tuple] = []
    for det in detail_items:
        det_id = _safe_int(det.get("id"))
        if det_id == 0:
            continue
        det_rows.append((
            cid,
            det_id,
            ret_id,
            _safe_int(det.get("documentDetailId")) or None,
            _safe_float(det.get("quantity")),
            _safe_float(det.get("quantityDevStock")),
            _safe_float(det.get("variantCost")),
        ))
    return ret_row, det_rows


def sync_returns() -> dict:
    """Sincroniza todas las devoluciones (son pocas: siempre listado completo)."""
    log_id = db.sync_start("returns")
    stats = {"fetched": 0, "inserted": 0, "skipped": 0, "details_inserted": 0,
             "details_without_variant": 0}
    try:
        cid = current_company_id()
        items = paginate(
            "/returns.json",
            "&expand=%5Bdetails%2Creference_document%2Ccredit_note%5D",
        )
        stats["fetched"] = len(items)

        ret_rows: list[tuple] = []
        det_rows: list[tuple] = []
        for r in items:
            ret_row, dets = return_dict_to_rows(r, cid)
            if ret_row is None:
                stats["skipped"] += 1
                continue
            ret_rows.append(ret_row)
            det_rows.extend(dets)

        sql_ret = """
            INSERT INTO returns (company_id, bsale_return_id, code, return_date, motive,
                                 return_type, price_adjustment, edit_texts, amount,
                                 bsale_office_id, bsale_user_id,
                                 bsale_reference_document_id, bsale_credit_note_id,
                                 synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (company_id, bsale_return_id) DO UPDATE SET
                code = EXCLUDED.code,
                return_date = EXCLUDED.return_date,
                motive = EXCLUDED.motive,
                return_type = EXCLUDED.return_type,
                price_adjustment = EXCLUDED.price_adjustment,
                edit_texts = EXCLUDED.edit_texts,
                amount = EXCLUDED.amount,
                bsale_office_id = EXCLUDED.bsale_office_id,
                bsale_user_id = EXCLUDED.bsale_user_id,
                bsale_reference_document_id = EXCLUDED.bsale_reference_document_id,
                bsale_credit_note_id = EXCLUDED.bsale_credit_note_id,
                synced_at = NOW()
            WHERE (returns.code, returns.return_date, returns.motive, returns.return_type,
                   returns.price_adjustment, returns.edit_texts, returns.amount,
                   returns.bsale_office_id, returns.bsale_user_id,
                   returns.bsale_reference_document_id, returns.bsale_credit_note_id)
                  IS DISTINCT FROM
                  (EXCLUDED.code, EXCLUDED.return_date, EXCLUDED.motive, EXCLUDED.return_type,
                   EXCLUDED.price_adjustment, EXCLUDED.edit_texts, EXCLUDED.amount,
                   EXCLUDED.bsale_office_id, EXCLUDED.bsale_user_id,
                   EXCLUDED.bsale_reference_document_id, EXCLUDED.bsale_credit_note_id)
        """
        db.execute_batch(sql_ret, ret_rows)
        stats["inserted"] = len(ret_rows)

        # bsale_variant_id no se toca en el upsert: lo completa el UPDATE de abajo.
        sql_det = """
            INSERT INTO return_details (company_id, bsale_return_detail_id, bsale_return_id,
                                        bsale_document_detail_id, quantity,
                                        quantity_dev_stock, variant_cost, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (company_id, bsale_return_detail_id) DO UPDATE SET
                bsale_return_id = EXCLUDED.bsale_return_id,
                bsale_document_detail_id = EXCLUDED.bsale_document_detail_id,
                quantity = EXCLUDED.quantity,
                quantity_dev_stock = EXCLUDED.quantity_dev_stock,
                variant_cost = EXCLUDED.variant_cost,
                synced_at = NOW()
            WHERE (return_details.bsale_return_id, return_details.bsale_document_detail_id,
                   return_details.quantity, return_details.quantity_dev_stock,
                   return_details.variant_cost)
                  IS DISTINCT FROM
                  (EXCLUDED.bsale_return_id, EXCLUDED.bsale_document_detail_id,
                   EXCLUDED.quantity, EXCLUDED.quantity_dev_stock, EXCLUDED.variant_cost)
        """
        db.execute_batch(sql_det, det_rows)
        stats["details_inserted"] = len(det_rows)

        stats["details_without_variant"] = _resolve_return_variants(cid)

        logger.info("Devoluciones: %d headers, %d detalles (%d sin variante)",
                    stats["inserted"], stats["details_inserted"],
                    stats["details_without_variant"])
        db.sync_finish(log_id, fetched=stats["fetched"],
                       inserted=stats["inserted"], skipped=stats["skipped"])
    except Exception as exc:
        db.sync_finish(log_id, status="FAILED", error=str(exc),
                       fetched=stats["fetched"])
        raise
    return stats


def _resolve_return_variants(cid: int) -> int:
    """Completa bsale_variant_id desde document_details. Devuelve cuántos quedan NULL."""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE return_details rd
                SET bsale_variant_id = dd.bsale_variant_id
                FROM document_details dd
                WHERE rd.company_id = %s
                  AND dd.company_id = rd.company_id
                  AND dd.bsale_detail_id = rd.bsale_document_detail_id
                  AND rd.bsale_variant_id IS DISTINCT FROM dd.bsale_variant_id
                """,
                (cid,),
            )
            cur.execute(
                "SELECT COUNT(*) FROM return_details "
                "WHERE company_id = %s AND bsale_variant_id IS NULL",
                (cid,),
            )
            return int(cur.fetchone()[0])
