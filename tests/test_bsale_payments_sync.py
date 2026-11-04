"""Tests con fakes del sync de medios de pago, pagos y devoluciones (harvester/sync_payments.py)."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DB_NAME", "test_db")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "5432")
os.environ.setdefault("DB_USER", "test_user")
os.environ.setdefault("DB_PASSWORD", "test_pass")

from harvester import sync_payments as sp
from harvester.tenant_context import tenant_scope

DAY = 86400
TODAY = 1793664000            # 2026-11-03 00:00 UTC
NOW = TODAY + 15 * 3600       # 15:00 UTC


def _payment(pid, day, amount=10.0, state=0, ptype="1", doc="100", credit=0):
    return {
        "id": pid, "recordDate": day, "amount": amount, "createdAt": day + 3600,
        "isCreditPayment": credit, "state": state,
        "payment_type": {"id": ptype}, "document": {"id": doc},
        "office": {"id": "1"}, "user": {"id": "2"},
    }


class _Captured:
    def __init__(self):
        self.batches: list[tuple[str, list[tuple]]] = []
        self.finish_calls: list[dict] = []

    def execute_batch(self, sql, rows, page_size=1000):
        self.batches.append((sql, list(rows)))
        return len(rows)

    def sync_finish(self, log_id, **kw):
        self.finish_calls.append(kw)

    def rows_for(self, table: str) -> list[tuple]:
        out: list[tuple] = []
        for sql, rows in self.batches:
            if f"INSERT INTO {table} " in sql:
                out.extend(rows)
        return out


def _patch_db(cap: _Captured, last_full_hours=None):
    return [
        patch.object(sp.db, "sync_start", return_value=1),
        patch.object(sp.db, "sync_finish", side_effect=cap.sync_finish),
        patch.object(sp.db, "execute_batch", side_effect=cap.execute_batch),
        patch.object(sp.db, "log_quality_issue"),
        patch.object(sp.db, "hours_since_last_success", return_value=last_full_hours),
        patch.object(sp.time, "time", return_value=NOW),
    ]


def _run(patches, fn, *args, **kwargs):
    for p in patches:
        p.start()
    try:
        with tenant_scope(1, "tok", "kawii"):
            return fn(*args, **kwargs)
    finally:
        for p in reversed(patches):
            p.stop()


# ---------------------------------------------------------------------------
# Conversión de filas
# ---------------------------------------------------------------------------

def test_payment_row_keeps_annulled_payment_as_inactive():
    active = sp.payment_dict_to_row(_payment(5, TODAY, 13.456, ptype="13", doc="77"), cid=1)
    annulled = sp.payment_dict_to_row(_payment(6, TODAY, 9.5, state=1), cid=1)

    assert active[:6] == (1, 5, 77, 13, 1, 2)    # ids de string → int
    assert active[6] == 13.46                     # monto redondeado a 2 decimales
    assert active[7].timestamp() == TODAY
    assert active[-1] is True
    assert annulled[-1] is False                  # anulado: se guarda, pero inactivo


def test_payment_row_skips_missing_id_and_invalid_date():
    with patch.object(sp.db, "log_quality_issue") as log:
        assert sp.payment_dict_to_row({"id": 0, "recordDate": TODAY}, cid=1) is None
        assert sp.payment_dict_to_row(_payment(7, 0), cid=1) is None
    assert log.call_count == 1   # solo la fecha inválida se registra


def test_payment_days_covers_window_through_today():
    days = sp._payment_days(TODAY - 3 * DAY + 500, NOW)
    assert days == [TODAY - 3 * DAY, TODAY - 2 * DAY, TODAY - DAY, TODAY]


# ---------------------------------------------------------------------------
# sync_payments
# ---------------------------------------------------------------------------

def test_sync_payments_incremental_queries_each_day_and_marks_annulled():
    by_day = {
        TODAY - DAY: [_payment(1, TODAY - DAY), _payment(2, TODAY - DAY, state=1)],
        TODAY: [_payment(3, TODAY, 20.0, ptype="13")],
    }
    calls: list[tuple[str, str]] = []

    def fake_paginate(endpoint, extra="", max_workers=4):
        calls.append((endpoint, extra))
        assert endpoint == "/payments.json"
        assert "state=" not in extra          # nunca filtrar por estado
        day = int(extra.split("recorddate=")[1])
        return list(by_day.get(day, []))

    cap = _Captured()
    patches = _patch_db(cap, last_full_hours=1.0) + [
        patch.object(sp, "paginate", side_effect=fake_paginate),
    ]
    stats = _run(patches, sp.sync_payments, since_unix=TODAY - DAY)

    assert stats["mode"] == "incremental"
    assert sorted(extra for _, extra in calls) == [
        f"&recorddate={TODAY - DAY}", f"&recorddate={TODAY}",
    ]
    rows = cap.rows_for("payments")
    assert {r[1]: r[-1] for r in rows} == {1: True, 2: False, 3: True}
    assert stats["fetched"] == 3 and stats["inserted"] == 3 and stats["inactive"] == 1
    assert cap.finish_calls[-1].get("status", "SUCCESS") == "SUCCESS"


def test_sync_payments_auto_full_when_no_recent_full_and_dedupes_tail():
    full_list = [_payment(i, TODAY - DAY) for i in range(1, 5)] + [_payment(5, TODAY, 7.0)]
    # Por día vuelve el pago 5 con monto corregido y un pago 6 que el listado
    # completo se perdió por el reordenamiento del bloque del día.
    by_day = {TODAY: [_payment(5, TODAY, 8.0), _payment(6, TODAY)]}

    def fake_paginate(endpoint, extra="", max_workers=4):
        if not extra:
            return list(full_list)
        return list(by_day.get(int(extra.split("recorddate=")[1]), []))

    cap = _Captured()
    patches = _patch_db(cap, last_full_hours=None) + [
        patch.object(sp, "paginate", side_effect=fake_paginate),
        patch.object(sp, "fetch", return_value={"count": 6, "items": [{}]}),
    ]
    stats = _run(patches, sp.sync_payments, since_unix=TODAY - DAY)

    assert stats["mode"] == "full"
    assert stats["bsale_count"] == 6
    rows = {r[1]: r for r in cap.rows_for("payments")}
    assert sorted(rows) == [1, 2, 3, 4, 5, 6]
    assert rows[5][6] == 8.0          # gana la descarga por día (más reciente)
    assert stats["duplicates"] == 1


def test_sync_payments_explicit_flags():
    cap = _Captured()
    seen: list[str] = []

    def fake_paginate(endpoint, extra="", max_workers=4):
        seen.append(extra)
        return []

    patches = _patch_db(cap, last_full_hours=None) + [
        patch.object(sp, "paginate", side_effect=fake_paginate),
        patch.object(sp, "fetch", return_value={"count": 0}),
    ]
    # full_history=False gana sobre el automático aunque nunca hubo un full
    assert _run(patches, sp.sync_payments, since_unix=TODAY, full_history=False)["mode"] == "incremental"
    assert "" not in seen
    # sin ventana siempre es full
    assert _run(patches, sp.sync_payments, since_unix=None, full_history=False)["mode"] == "full"
    assert "" in seen


def test_sync_payments_marks_failed_and_reraises():
    cap = _Captured()
    patches = _patch_db(cap, last_full_hours=1.0) + [
        patch.object(sp, "paginate", side_effect=RuntimeError("Paginacion incompleta")),
    ]
    with pytest.raises(RuntimeError, match="Paginacion incompleta"):
        _run(patches, sp.sync_payments, since_unix=TODAY)
    assert cap.finish_calls[-1]["status"] == "FAILED"
    assert cap.rows_for("payments") == []


def test_sync_payments_flushes_in_chunks():
    items = [_payment(i, TODAY) for i in range(1, 8)]
    cap = _Captured()
    patches = _patch_db(cap, last_full_hours=1.0) + [
        patch.object(sp, "paginate", return_value=items),
        patch.object(sp, "PAYMENTS_FLUSH_SIZE", 3),
    ]
    stats = _run(patches, sp.sync_payments, since_unix=TODAY)
    sizes = [len(rows) for sql, rows in cap.batches if "INSERT INTO payments " in sql]
    assert sizes == [3, 3, 1]
    assert stats["inserted"] == 7


# ---------------------------------------------------------------------------
# sync_payment_types
# ---------------------------------------------------------------------------

def test_sync_payment_types_maps_flags_and_inactive_types():
    items = [
        {"id": 1, "name": "EFECTIVO", "state": 0, "isCash": 1},
        {"id": 4, "name": "CREDITO", "state": 0, "isClientCredit": 1},
        {"id": 7, "name": "ABONO DE CLIENTE", "state": 0, "isCreditMemo": 1},
        {"id": 3, "name": "NOTA CREDITO DEVOLUCION", "state": 1, "isCreditNote": 1},
        {"id": 0, "name": "roto"},
    ]
    cap = _Captured()
    patches = _patch_db(cap) + [patch.object(sp, "paginate", return_value=items)]
    stats = _run(patches, sp.sync_payment_types)

    rows = {r[1]: r for r in cap.rows_for("payment_types")}
    assert rows[1] == (1, 1, "EFECTIVO", True, True, False, False, False)
    assert rows[4][5] is True and rows[7][6] is True
    assert rows[3][3] is False and rows[3][7] is True   # inactivo + nota de crédito
    assert stats == {"fetched": 5, "inserted": 4, "skipped": 1}


# ---------------------------------------------------------------------------
# sync_returns
# ---------------------------------------------------------------------------

def test_sync_returns_rows_deep_fetch_and_variant_resolution():
    items = [
        {
            "id": 11, "code": "176633401791", "returnDate": TODAY, "motive": " Devolucion ",
            "type": 0, "priceAdjustment": 1, "editTexts": 0, "amount": 16.8,
            "office": {"id": "1"}, "user": {"id": "9"},
            "reference_document": {"id": 55057}, "credit_note": {"id": 55075},
            "details": {"count": 2, "items": [
                {"id": 17, "quantity": 1.0, "quantityDevStock": 1.0,
                 "variantCost": 3.5, "documentDetailId": 147608},
                {"id": 18, "quantity": 2.0, "quantityDevStock": 0.0,
                 "variantCost": 0.0, "documentDetailId": 147609},
            ]},
        },
        {
            # Anular venta: sin nota de crédito y con detalle truncado
            "id": 1, "returnDate": TODAY - DAY, "motive": "Anular venta", "type": -2,
            "amount": 23.4, "office": {"id": "1"}, "user": {"id": "2"},
            "reference_document": {"id": 1}, "credit_note": None,
            "details": {"count": 30, "items": [{"id": 1, "documentDetailId": 1}]},
        },
    ]
    deep = [{"id": 100 + i, "quantity": 1.0, "documentDetailId": 500 + i} for i in range(30)]
    cap = _Captured()
    patches = _patch_db(cap) + [
        patch.object(sp, "paginate", return_value=items),
        patch.object(sp, "fetch_subresource", return_value=deep),
        patch.object(sp, "_resolve_return_variants", return_value=3),
    ]
    stats = _run(patches, sp.sync_returns)

    rets = {r[1]: r for r in cap.rows_for("returns")}
    assert rets[11] == (1, 11, "176633401791", rets[11][3], "Devolucion", 0, True, False,
                        16.8, 1, 9, 55057, 55075)
    assert rets[1][5] == -2 and rets[1][12] is None
    dets = cap.rows_for("return_details")
    assert (1, 17, 11, 147608, 1.0, 1.0, 3.5) in dets
    assert len([d for d in dets if d[2] == 1]) == 30      # se usó el detalle profundo
    assert stats["inserted"] == 2 and stats["details_inserted"] == 32
    assert stats["details_without_variant"] == 3


def test_resolve_return_variants_updates_from_document_details():
    from contextlib import contextmanager

    executed: list[tuple[str, tuple]] = []

    class FakeCursor:
        def execute(self, sql, params=None):
            executed.append((sql, params))

        def fetchone(self):
            return (2,)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    @contextmanager
    def fake_get_conn():
        yield FakeConn()

    with patch.object(sp.db, "get_conn", fake_get_conn):
        assert sp._resolve_return_variants(1) == 2
    assert "UPDATE return_details" in executed[0][0]
    assert "FROM document_details" in executed[0][0]
    assert executed[0][1] == (1,)


# ---------------------------------------------------------------------------
# Registro en el orquestador
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode,full_refresh,expected_hist", [
    ("quick", False, False),
    ("full", False, None),
    ("full", True, True),
])
def test_mt_sync_registers_payment_phases_after_documents(mode, full_refresh, expected_hist):
    from tools.maintenance import mt_sync

    order: list[str] = []
    pay_kwargs: dict = {}

    def recorder(name):
        def _f(*a, **kw):
            order.append(name)
            if name == "payments":
                pay_kwargs.update(kw)
            return {}
        return _f

    names = [
        "sync_taxonomy", "sync_offices", "sync_users", "sync_product_types",
        "sync_document_types", "sync_variants", "sync_variant_costs",
        "sync_variant_costs_by_office", "sync_price_lists", "sync_stock_levels",
        "snapshot_stock_history", "sync_product_type_attributes",
        "sync_variant_attribute_values", "sync_receptions", "sync_consumptions",
    ]
    patches = [patch.object(mt_sync, n, MagicMock(return_value={})) for n in names] + [
        patch.object(mt_sync, "sync_documents", side_effect=recorder("documents")),
        patch.object(mt_sync, "sync_payment_types", side_effect=recorder("payment_types")),
        patch.object(mt_sync, "sync_payments", side_effect=recorder("payments")),
        patch.object(mt_sync, "sync_returns", side_effect=recorder("returns")),
        patch.object(mt_sync, "set_current_tenant"),
        patch.object(mt_sync, "clear_current_tenant"),
    ]
    for p in patches:
        p.start()
    try:
        res = mt_sync.run_pipeline_for_company(
            {"id": 1, "name": "Kawii", "slug": "kawii", "token": "t"},
            since_unix=TODAY, mode=mode, full_refresh=full_refresh,
        )
        assert order == ["documents", "payment_types", "payments", "returns"]
        assert pay_kwargs == {"since_unix": TODAY, "full_history": expected_hist}
        assert {"17.MediosPago", "18.Pagos", "19.Devoluciones"} <= set(res)

        order.clear()
        mt_sync.run_pipeline_for_company(
            {"id": 1, "name": "Kawii", "slug": "kawii", "token": "t"},
            since_unix=TODAY, mode=mode, skip_documents=True,
        )
        assert order == []   # --skip-documents también salta pagos y devoluciones
    finally:
        for p in reversed(patches):
            p.stop()
