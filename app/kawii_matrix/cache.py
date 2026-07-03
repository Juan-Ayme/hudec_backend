"""
Cache TTL en memoria para el resultado base de cada matriz.

Por qué existe
--------------
Cada hit a `/matrix/{module_id}` corre un SQL pesado (~15 CTEs, ~4s con el
hint nestloop=off) que descarga miles de filas desde la DB managed de Render.
En producción el egress de la DB es el costo dominante; los filtros de la API
son post-query en Python (ver service.run_matrix), así que el limit no recorta
lo que se descarga — solo lo que se serializa al browser.

Estrategia
----------
Cachear el resultado de la query base por `module_id` (no por combinación de
filtros: la cardinalidad sería inviable). TTL configurable vía
`MATRIX_CACHE_TTL_SECONDS` (default 120s). Los filtros siguen aplicándose
sobre el cache hit.

Coherencia
----------
- Render corre 1 worker uvicorn → cache in-process es coherente para todos
  los requests del servicio. Si se escala a N workers, mover a Redis.
- Los webhooks de BSale mutan documents/stock_levels entre syncs. El TTL
  corto absorbe el lag; para forzar refresh inmediato usar el endpoint admin
  `POST /matrix/cache/invalidate`.
- Cambios de exclusiones/thresholds desde la UI (config_admin) tardan hasta
  TTL en propagarse. Si molesta, llamar invalidate() desde esos endpoints.

Concurrencia
------------
Un Lock por módulo evita el thundering herd: si 5 requests llegan a la vez
con cache miss/expirado, solo uno corre el SQL y los otros 4 esperan y
reusan el resultado.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

logger = logging.getLogger("kawii.matrix.cache")

# Cache key = (module_id, company_id) para aislamiento multi-tenant.
# {(module_id, company_id): (expires_at_monotonic, columns, rows)}
_CacheKey = tuple[str, int]
_store: dict[_CacheKey, tuple[float, list[str], list[dict]]] = {}
_locks: dict[_CacheKey, asyncio.Lock] = {}


def _get_lock(key: _CacheKey) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


async def get_or_compute(
    module_id: str,
    company_id: int,
    ttl_seconds: int,
    compute: Callable[[], Awaitable[tuple[list[str], list[dict]]]],
) -> tuple[list[str], list[dict]]:
    """
    Devuelve (columns, rows) del cache si está fresco; si no, corre `compute`
    bajo lock y guarda el resultado. La cache está aislada por (module, company).

    ttl_seconds <= 0 desactiva el cache (bypass total — útil para debug).
    """
    if ttl_seconds <= 0:
        return await compute()

    key: _CacheKey = (module_id, company_id)
    now = time.monotonic()
    entry = _store.get(key)
    if entry is not None and entry[0] > now:
        return entry[1], entry[2]

    lock = _get_lock(key)
    async with lock:
        # Re-check: otra corrutina pudo haber poblado el cache mientras
        # esperábamos el lock.
        now = time.monotonic()
        entry = _store.get(key)
        if entry is not None and entry[0] > now:
            return entry[1], entry[2]

        columns, rows = await compute()
        _store[key] = (now + ttl_seconds, columns, rows)
        logger.info(
            "Matrix cache MISS módulo=%s company=%s → guardado (%d filas, TTL=%ds)",
            module_id, company_id, len(rows), ttl_seconds,
        )
        return columns, rows


def invalidate(module_id: str | None = None, company_id: int | None = None) -> int:
    """
    Invalida el cache. Sin argumentos limpia todo. Con `module_id` solo esa
    matriz (para todas las empresas). Con `company_id` solo esa empresa (para
    todas las matrices). Con ambos, la combinación específica.

    Devuelve cuántas entradas se borraron.
    """
    if module_id is None and company_id is None:
        n = len(_store)
        _store.clear()
        logger.info("Matrix cache INVALIDATE total: %d entradas limpiadas", n)
        return n
    to_remove = [
        k for k in _store
        if (module_id is None or k[0] == module_id)
        and (company_id is None or k[1] == company_id)
    ]
    for k in to_remove:
        del _store[k]
    if to_remove:
        logger.info(
            "Matrix cache INVALIDATE module=%s company=%s → %d entradas",
            module_id, company_id, len(to_remove),
        )
    return len(to_remove)


def stats() -> dict:
    """Snapshot del estado del cache (para debug / endpoint admin)."""
    now = time.monotonic()
    return {
        "modules_cached": len(_store),
        "entries": [
            {
                "module_id": mid,
                "company_id": cid,
                "rows": len(rows),
                "ttl_remaining_seconds": max(0, int(expires - now)),
                "fresh": expires > now,
            }
            for (mid, cid), (expires, _cols, rows) in _store.items()
        ],
    }
