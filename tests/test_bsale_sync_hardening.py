"""Tests unitarios de robustez e integridad del cliente y orquestador de Sync BSale."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

# Asegurar variables mínimas para importar harvester.config en entorno de test
os.environ.setdefault("DB_NAME", "test_db")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "5432")
os.environ.setdefault("DB_USER", "test_user")
os.environ.setdefault("DB_PASSWORD", "test_pass")

from harvester import bsale_client
from app.routers import sync as sync_router


def test_paginate_preserves_order_and_recovers_transient_page_failure():
    """Si una página intermedia falla en la 1ra pasada pero responde en la 2da,
    no se pierde ningún registro y se respeta el orden por offset."""
    calls: dict[str, int] = {}

    def fake_fetch(url: str, retries: int = 3):
        calls[url] = calls.get(url, 0) + 1
        if "offset=0" in url:
            return {"count": 120, "items": [{"id": i} for i in range(1, 51)]}
        if "offset=50" in url:
            # Falla la primera vez (devuelve {}), se recupera en la segunda pasada
            if calls[url] == 1:
                return {}
            return {"count": 120, "items": [{"id": i} for i in range(51, 101)]}
        if "offset=100" in url:
            return {"count": 120, "items": [{"id": i} for i in range(101, 121)]}
        return {"count": 120, "items": []}

    with patch.object(bsale_client, "fetch", side_effect=fake_fetch), \
         patch.object(bsale_client.time, "sleep"):
        items = bsale_client.paginate("/variants.json", max_workers=4)

    assert [it["id"] for it in items] == list(range(1, 121))


def test_paginate_raises_on_permanent_page_failure():
    """Nunca debe marcar SUCCESS silencioso si una página sigue fallando."""
    def fake_fetch(url: str, retries: int = 3):
        if "offset=0" in url:
            return {"count": 120, "items": [{"id": i} for i in range(1, 51)]}
        if "offset=50" in url:
            return {}  # Falla permanente
        return {"count": 120, "items": [{"id": i} for i in range(101, 121)]}

    with patch.object(bsale_client, "fetch", side_effect=fake_fetch), \
         patch.object(bsale_client.time, "sleep"):
        with pytest.raises(RuntimeError, match="Paginacion incompleta"):
            bsale_client.paginate("/stocks.json", max_workers=4)


def test_paginate_fetches_trailing_pages_when_count_grows_mid_sync():
    """Si entran registros nuevos en BSale mientras se pagina, la cola serial los captura."""
    def fake_fetch(url: str, retries: int = 3):
        if "offset=0" in url:
            return {"count": 100, "items": [{"id": i} for i in range(1, 51)]}
        if "offset=50" in url:
            # Venían 50 llenos y además se crearon 5 más en offset=100
            return {"count": 105, "items": [{"id": i} for i in range(51, 101)]}
        if "offset=100" in url:
            return {"count": 105, "items": [{"id": i} for i in range(101, 106)]}
        return {"count": 105, "items": []}

    with patch.object(bsale_client, "fetch", side_effect=fake_fetch):
        items = bsale_client.paginate("/stocks.json", max_workers=4)

    assert len(items) == 105
    assert items[-1]["id"] == 105


def test_paginate_newer_verifies_overlap_and_fetches_tail():
    """paginate_newer acepta solo si hay solape con known_max_id y orden ascendente."""
    def fake_fetch(url: str, retries: int = 3):
        if "limit=1&offset=0" in url:
            return {"count": 210, "items": [{"id": 1}]}
        if "offset=100" in url:
            return {"count": 210, "items": [{"id": i} for i in range(101, 151)]}
        if "offset=150" in url:
            return {"count": 210, "items": [{"id": i} for i in range(151, 201)]}
        if "offset=200" in url:
            return {"count": 210, "items": [{"id": i} for i in range(201, 211)]}
        return {"count": 210, "items": []}

    with patch.object(bsale_client, "fetch", side_effect=fake_fetch):
        # Tenemos 200 locales con max_id=200; en BSale hay 210 → start=100
        items = bsale_client.paginate_newer(
            "/stocks/receptions.json", "&officeid=1",
            known_count=200, known_max_id=200, margin=100, max_workers=4,
        )
        assert items is not None
        assert [it["id"] for it in items] == list(range(101, 211))

        # Si known_max_id fuera 50 (no solapa con ids[0]=101), debe devolver None (fallback a full)
        no_overlap = bsale_client.paginate_newer(
            "/stocks/receptions.json", "&officeid=1",
            known_count=200, known_max_id=50, margin=100, max_workers=4,
        )
        assert no_overlap is None


def test_sync_router_conflict_detection_and_pruning():
    """Evita procesos mt_sync duplicados para la misma empresa, permitiendo otra empresa."""
    saved = dict(sync_router._task_state)
    try:
        sync_router._task_state.clear()
        sync_router._task_state["t1"] = {
            "task_id": "t1", "company_id": 1, "status": "RUNNING",
            "params": {"mode": "quick"},
        }
        # Misma empresa → conflicto
        assert sync_router._find_conflicting_task(1) is not None
        # Todas las empresas (None) → conflicto con empresa 1 activa
        assert sync_router._find_conflicting_task(None) is not None
        # Otra empresa (2) → permitido
        assert sync_router._find_conflicting_task(2) is None

        # Pruning de tareas finalizadas
        for i in range(60):
            sync_router._task_state[f"done_{i}"] = {
                "task_id": f"done_{i}", "company_id": 1, "status": "SUCCESS",
            }
        sync_router._prune_finished_tasks()
        assert "t1" in sync_router._task_state
        finished_left = [
            k for k, v in sync_router._task_state.items() if v["status"] == "SUCCESS"
        ]
        assert len(finished_left) == sync_router._MAX_FINISHED_TASKS
    finally:
        sync_router._task_state.clear()
        sync_router._task_state.update(saved)


def test_subcategory_resolver_avoids_ambiguous_bare_names():
    """Si dos categorías distintas tienen una subcategoría con el mismo nombre
    (ej. 'Accesorios'), el nombre corto no debe asignarse a ciegas, pero el
    nombre completo 'Categoría / Subcategoría' sí debe resolverse siempre."""
    from contextlib import contextmanager
    from harvester import sync_masters
    from harvester.tenant_context import tenant_scope

    class FakeCursor:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return [
                (10, "Accesorios", "Mochilas"),
                (20, "Accesorios", "Papelería"),
                (30, "Cuadernos", "Papelería"),
            ]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    @contextmanager
    def fake_get_conn():
        yield FakeConn()

    with tenant_scope(1, "tok", "hudec"),          patch.object(sync_masters.db, "get_conn", fake_get_conn):
        res = sync_masters._load_subcategory_resolver()

    # Nombres compuestos siempre exactos
    assert res["mochilas / accesorios"] == 10
    assert res["papelería / accesorios"] == 20
    assert res["papelería / cuadernos"] == 30
    # Nombre corto único sí está
    assert res["cuadernos"] == 30
    # Nombre corto ambiguo ('accesorios') NO debe estar para evitar falsos mapeos
    assert "accesorios" not in res


def test_sync_product_types_preserves_manual_mapping():
    """sync_product_types debe respetar el subcategory_id manual existente en BD
    aunque el nombre en BSale no coincida con ninguna subcategoría por texto."""
    from contextlib import contextmanager
    from harvester import sync_masters
    from harvester.tenant_context import tenant_scope

    captured_rows = []
    executed_queries = []

    class FakeCursor:
        def __init__(self):
            self._last_sql = ""

        def execute(self, sql, params=None):
            self._last_sql = sql
            executed_queries.append((sql, params))

        def fetchall(self):
            if "FROM products" in self._last_sql:
                return [(5,)]
            if "FROM product_types" in self._last_sql:
                # ptid=5 ya tiene subcategory_id=99 asignado manualmente en nuestra BD
                return [(5, 99)]
            return []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    @contextmanager
    def fake_get_conn():
        yield FakeConn()

    bsale_items = [
        {"id": 5, "name": "NOMBRE LIBRE EN BSALE", "state": 0},
        {"id": 6, "name": "Papelería / Cuadernos", "state": 0},
    ]

    with tenant_scope(1, "tok", "hudec"),          patch.object(sync_masters, "paginate", return_value=bsale_items),          patch.object(sync_masters, "_load_subcategory_resolver", return_value={"papelería / cuadernos": 30}),          patch.object(sync_masters.db, "get_conn", fake_get_conn),          patch.object(sync_masters.db, "sync_start", return_value=1),          patch.object(sync_masters.db, "sync_finish"),          patch.object(sync_masters.db, "execute_batch", side_effect=lambda sql, rows: captured_rows.extend(rows) or len(rows)):
        stats = sync_masters.sync_product_types()

    assert stats["unmapped"] == 0
    # ptid=5 conserva subcategory_id=99 e is_mapped=True
    assert (1, 5, "NOMBRE LIBRE EN BSALE", 99, True, True) in captured_rows
    # ptid=6 se resuelve por nombre a subcategory_id=30
    assert (1, 6, "Papelería / Cuadernos", 30, True, True) in captured_rows


def test_bulk_change_product_type_only_updates_confirmed_bsale_products():
    """En bulk_change_product_type, si 1 producto falla en BSale y otro tiene éxito,
    la BD local solo debe actualizar el producto confirmado por BSale."""
    import asyncio
    from app.auth import CurrentCompany, CurrentUser
    from app.routers import products as products_router

    executed_updates: list[tuple[str, dict]] = []

    class FakeMappings:
        def __init__(self, rows):
            self._rows = rows

        def first(self):
            return self._rows[0] if self._rows else None

        def all(self):
            return self._rows

    class FakeResult:
        def __init__(self, rows=None):
            self._rows = rows or []

        def mappings(self):
            return FakeMappings(self._rows)

        def fetchall(self):
            return self._rows

    class FakeDB:
        async def execute(self, stmt, params=None):
            sql = str(stmt)
            if "FROM product_types pt" in sql:
                return FakeResult([{
                    "bsale_product_type_id": 50,
                    "name": "Papelería / Cuadernos",
                    "is_active": True,
                    "is_mapped": True,
                    "subcategory_id": 30,
                    "subcategory": "Cuadernos",
                    "category": "Papelería",
                    "department": "Escolar",
                }])
            if "SELECT slug, pgp_sym_decrypt" in sql:
                return FakeResult([{"slug": "hudec", "token": "fake_tok"}])
            if "UPDATE products" in sql:
                executed_updates.append((sql, dict(params or {})))
                return FakeResult([])
            return FakeResult([])

        async def commit(self):
            pass

    fake_matched = [
        {
            "bsale_product_id": 1001,
            "product_name": "CUADERNO A4",
            "is_active": True,
            "bsale_product_type_id": 10,
            "override_subcategory_id": 5,
            "current_product_type_name": "Antiguo",
            "current_department": "Otros",
            "current_category": "Otros",
            "current_subcategory": "Varios",
        },
        {
            "bsale_product_id": 1002,
            "product_name": "CUADERNO B5",
            "is_active": True,
            "bsale_product_type_id": 10,
            "override_subcategory_id": None,
            "current_product_type_name": "Antiguo",
            "current_department": "Otros",
            "current_category": "Otros",
            "current_subcategory": "Varios",
        },
    ]

    def fake_bsale_put(endpoint: str, payload: dict):
        if "1002" in endpoint:
            raise RuntimeError("BSale 500 error")
        return {"id": 1001, "product_type": {"id": "50"}}

    async def _run():
        with patch.object(products_router, "_resolve_products_from_selectors", return_value=(fake_matched, {"product_ids": [], "variant_ids": [], "skus": []})),              patch.object(products_router, "_token_key", return_value="key"),              patch.object(products_router.bsale_client, "put", side_effect=fake_bsale_put),              patch.object(products_router, "log_event", return_value=1),              patch.object(products_router.matrix_cache, "invalidate"):
            body = products_router.BulkChangeProductTypeIn(
                target_product_type_id=50,
                product_ids=[1001, 1002],
                clear_overrides=True,
                dry_run=False,
            )
            return await products_router.bulk_change_product_type(
                body=body,
                company=CurrentCompany(company_id=1, slug="hudec", name="Hudec", role="admin"),
                user=CurrentUser(id=1, username="admin", is_active=True),
                db=FakeDB(),
            )

    res = asyncio.run(_run())
    assert res["totals"]["moved"] == 1
    assert res["totals"]["failed"] == 1
    assert res["moved"][0]["bsale_product_id"] == 1001
    assert res["failed"][0]["bsale_product_id"] == 1002
    # Solo 1001 debe actualizarse en la BD local
    assert len(executed_updates) == 1
    assert executed_updates[0][1]["pids"] == [1001]
    assert executed_updates[0][1]["target_pt_id"] == 50

