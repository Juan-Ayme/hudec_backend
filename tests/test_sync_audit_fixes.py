"""Arreglos de la auditoría del sync BSale vs Neon (2026-11-04).

1. Variantes eliminadas en BSale (state=97) que el listado no trae.
2. Último costo = recepción más reciente (BSale entrega history ascendente).
3. Stock de filas que BSale ya no lista → 0.
4. Documentos anulados después de sincronizados → fuera de la BD.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import patch

os.environ.setdefault("DB_NAME", "test_db")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "5432")
os.environ.setdefault("DB_USER", "test_user")
os.environ.setdefault("DB_PASSWORD", "test_pass")

from harvester import sync_masters, sync_transactions  # noqa: E402


class FakeCursor:
    def __init__(self, results: list):
        self.results = list(results)   # lo que devuelven los SELECT, en orden
        self.executed: list[tuple[str, object]] = []
        self.rowcount = 0
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        if sql.lstrip().upper().startswith("SELECT") or sql.lstrip().upper().startswith("WITH"):
            self._last = self.results.pop(0) if self.results else []
        else:
            self.rowcount = self.results.pop(0) if self.results and isinstance(self.results[0], int) else 0

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if isinstance(self._last, list) else self._last

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cur):
        self.cur = cur
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


def _fake_get_conn(cur):
    conn = FakeConn(cur)

    @contextmanager
    def get_conn():
        yield conn
    return get_conn, conn


# ── 2. Último costo ──────────────────────────────────────────────────────────

def test_latest_history_cost_toma_la_recepcion_mas_reciente():
    history = [  # como lo entrega BSale: del más antiguo al más nuevo
        {"cost": 1.5, "admissionDate": 1747785600},   # mayo
        {"cost": 1.88, "admissionDate": 1758240000},  # septiembre
    ]
    assert sync_masters._latest_history_cost(history) == 1.88


def test_latest_history_cost_ignora_costos_cero_y_vacios():
    history = [
        {"cost": 5.1, "admissionDate": 1753660800},
        {"cost": 0.0, "admissionDate": 1758240000},   # recepción sin costo
        {"cost": None, "admissionDate": 1759000000},
    ]
    assert sync_masters._latest_history_cost(history) == 5.1
    assert sync_masters._latest_history_cost([]) == 0.0


# ── 1. Variantes fuera del listado ──────────────────────────────────────────

def test_unlisted_variants_se_piden_una_por_una_y_las_no_encontradas_se_registran():
    cur = FakeCursor([[(1516,), (299,), (9999,)]])
    get_conn, _ = _fake_get_conn(cur)
    payloads = {
        1516: {"id": 1516, "state": 97, "product": {"id": 1425, "name": "SET AFEITADOR", "state": 97}},
        299: {"id": 299, "state": 97, "product": {"id": 290, "name": "CHEQUEO", "state": 97}},
        9999: {},  # BSale no la devuelve
    }
    issues = []
    with patch.object(sync_masters, "current_company_id", return_value=1), \
         patch.object(sync_masters.db, "get_conn", get_conn), \
         patch.object(sync_masters.db, "log_quality_issue", side_effect=lambda *a, **k: issues.append(a)), \
         patch.object(sync_masters, "fetch", side_effect=lambda url: payloads[int(url.split("/variants/")[1].split(".")[0])]):
        got = sync_masters._fetch_unlisted_variants([{"id": 1}, {"id": 2}])
    assert sorted(v["id"] for v in got) == [299, 1516]
    assert all(v["state"] == 97 for v in got)
    assert issues and issues[0][1] == 9999 and issues[0][3] == "NOT_FOUND"
    # el listado se pasa a la consulta para no volver a pedir lo que ya vino
    assert cur.executed[0][1]["ids"] == [1, 2]


def test_unlisted_variants_no_reconcilia_si_faltan_demasiadas():
    cur = FakeCursor([[(i,) for i in range(1, sync_masters.UNLISTED_VARIANTS_LIMIT + 2)]])
    get_conn, _ = _fake_get_conn(cur)
    with patch.object(sync_masters, "current_company_id", return_value=1), \
         patch.object(sync_masters.db, "get_conn", get_conn), \
         patch.object(sync_masters, "fetch") as f:
        assert sync_masters._fetch_unlisted_variants([{"id": 1}]) == []
        f.assert_not_called()


# ── 3. Stock que BSale ya no lista ──────────────────────────────────────────

def test_stock_no_listado_queda_en_cero():
    # office 1: 4 filas a poner en 0 de 5000 → se hace; office 3: sin respuesta → no se toca
    cur = FakeCursor([[(4, 5000)], 4])
    get_conn, conn = _fake_get_conn(cur)
    with patch.object(sync_masters.db, "get_conn", get_conn):
        n = sync_masters._zero_unlisted_stock(1, {1: [{"id": 10}, {"id": 11}], 3: []})
    assert n == 4
    updates = [s for s, _ in cur.executed if s.startswith("UPDATE stock_levels")]
    assert len(updates) == 1 and "quantity = 0" in updates[0]
    assert conn.commits == 1


def test_stock_no_se_toca_si_la_descarga_parece_incompleta():
    cur = FakeCursor([[(3000, 5000)]])   # 60% de la sucursal "desapareció"
    get_conn, _ = _fake_get_conn(cur)
    with patch.object(sync_masters.db, "get_conn", get_conn):
        assert sync_masters._zero_unlisted_stock(1, {1: [{"id": 10}]}) == 0
    assert not [s for s, _ in cur.executed if s.startswith("UPDATE")]


# ── 4. Documentos anulados después del sync ─────────────────────────────────

def test_documentos_anulados_se_quitan_y_quedan_registrados():
    import datetime as dt
    emitted = dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc)
    cur = FakeCursor([[(555, 42.5, emitted)], 3, 1])   # SELECT, DELETE detalles, DELETE doc
    get_conn, conn = _fake_get_conn(cur)
    issues, urls = [], []

    def fake_paginate(endpoint, extra=""):
        urls.append(extra)
        return [{"id": 555, "state": 1}, {"id": 777, "state": 1}]   # 777 nunca entró a la BD

    with patch.object(sync_transactions, "current_company_id", return_value=1), \
         patch.object(sync_transactions.db, "get_conn", get_conn), \
         patch.object(sync_transactions, "paginate", side_effect=fake_paginate), \
         patch.object(sync_transactions.db, "log_quality_issue", side_effect=lambda *a, **k: issues.append(a)):
        n = sync_transactions._remove_annulled_documents(since_unix=1_790_000_000)
    assert n == 1
    assert "&state=1" in urls[0] and "emissiondaterange" in urls[0]
    deletes = [s for s, _ in cur.executed if s.startswith("DELETE")]
    assert deletes[0].startswith("DELETE FROM document_details") and deletes[1].startswith("DELETE FROM documents")
    assert issues == [("documents", 555, "state", "ANNULLED_IN_BSALE", issues[0][4], "42.5")]


def test_sin_anulados_no_toca_la_bd():
    with patch.object(sync_transactions, "paginate", return_value=[]), \
         patch.object(sync_transactions.db, "get_conn") as gc:
        assert sync_transactions._remove_annulled_documents(None) == 0
        gc.assert_not_called()


# ── 5. 429 no consume los reintentos de error ───────────────────────────────

def test_fetch_espera_ante_429_y_luego_responde():
    from harvester import bsale_client

    class R:
        def __init__(self, code):
            self.status_code = code

        def json(self):
            return {"ok": True}

    seq = iter([R(429), R(429), R(429), R(429), R(200)])   # 4 esperas > 3 reintentos de antes

    class S:
        def get(self, *a, **k):
            return next(seq)

    with patch.object(bsale_client, "_session", return_value=S()), \
         patch.object(bsale_client, "_build_headers", return_value={}), \
         patch.object(bsale_client._limiter, "acquire"), \
         patch.object(bsale_client.time, "sleep") as sl:
        assert bsale_client.fetch("https://x/variants/1/costs.json") == {"ok": True}
    assert [c.args[0] for c in sl.call_args_list] == [2.0, 4.0, 8.0, 16.0]


def test_fetch_se_rinde_tras_errores_reales():
    from harvester import bsale_client

    class R:
        status_code = 500

    class S:
        def get(self, *a, **k):
            return R()

    with patch.object(bsale_client, "_session", return_value=S()), \
         patch.object(bsale_client, "_build_headers", return_value={}), \
         patch.object(bsale_client._limiter, "acquire"), \
         patch.object(bsale_client.time, "sleep"):
        assert bsale_client.fetch("https://x/a.json", retries=3) == {}
