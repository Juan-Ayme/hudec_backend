"""
Cliente HTTP para la API de BSale.

Responsabilidades:
  - Rate limiting (max 9 req/s)
  - Reintentos con backoff exponencial
  - Paginacion automatica (paralela)
  - Logging de cada request

NO tiene logica de negocio ni de base de datos.

Nota de rendimiento (2026-11): la latencia real de BSale es ~1.5-2.5s por
request. Con paginación serial se aprovechaba solo ~20-40% del límite de
9 RPS. Ahora:
  - paginate()/fetch_subresource() piden el `count` en la primera página y
    bajan el resto en paralelo (el RateLimiter global sigue siendo el tope).
  - Cada hilo reutiliza una requests.Session (keep-alive) en vez de abrir
    una conexión TLS nueva por request.
"""

import time
import threading
import logging
import concurrent.futures
import requests
from requests.adapters import HTTPAdapter
from typing import Any

from harvester.config import (
    BSALE_BASE_URL,
    BSALE_PAGE_SIZE,
    BSALE_TIMEOUT,
    BSALE_MAX_RETRIES,
    BSALE_MAX_RPS,
    BSALE_MAX_WORKERS,
)
from harvester.tenant_context import current_token

logger = logging.getLogger("harvester.bsale_client")


def _build_headers() -> dict:
    """Headers para BSale. El token viene del tenant activo (contextvar),
    para que cada empresa use el suyo. Levanta si no hay tenant seteado."""
    return {"access_token": current_token(), "Accept": "application/json"}


# Sesión HTTP por hilo (keep-alive). Antes cada request abría una conexión
# TCP+TLS nueva contra api.bsale.io. requests.Session no es 100% thread-safe,
# por eso se mantiene una por hilo.
_thread_local = threading.local()


def _session() -> requests.Session:
    sess = getattr(_thread_local, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.mount("https://", HTTPAdapter(pool_connections=1, pool_maxsize=2))
        _thread_local.session = sess
    return sess


class RateLimiter:
    """Rate limiter thread-safe con espaciado uniforme entre requests.

    Reserva un "slot" cada 1/max_rps segundos. A diferencia de la ventana
    fija anterior, no permite ráfagas de hasta 2x el límite en el borde entre
    dos ventanas (relevante con ~20 hilos concurrentes) y no duerme mientras
    sostiene el lock.
    """

    def __init__(self, max_rps: int = BSALE_MAX_RPS):
        self._interval = 1.0 / max(1, max_rps)
        self._lock = threading.Lock()
        self._next_slot = time.monotonic()

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
        wait = slot - now
        if wait > 0:
            time.sleep(wait)


# Singleton del rate limiter
_limiter = RateLimiter()


def fetch(url: str, retries: int = BSALE_MAX_RETRIES) -> dict[str, Any]:
    """
    GET a la API de BSale con rate limiting y reintentos.
    Retorna el JSON parseado o {} si falla.
    """
    for attempt in range(retries):
        _limiter.acquire()
        try:
            resp = _session().get(url, headers=_build_headers(), timeout=BSALE_TIMEOUT)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                # Rate limited por BSale, backoff mas agresivo
                wait = 2.0 * (attempt + 1)
                logger.warning("429 Rate Limited en %s, esperando %.1fs", url, wait)
                time.sleep(wait)
                continue

            # Otros errores HTTP
            logger.warning(
                "HTTP %d en %s (intento %d/%d)",
                resp.status_code, url, attempt + 1, retries,
            )
            time.sleep(1.5 * (attempt + 1))

        except requests.exceptions.Timeout:
            logger.warning("Timeout en %s (intento %d/%d)", url, attempt + 1, retries)
            time.sleep(2.0 * (attempt + 1))

        except requests.exceptions.RequestException as exc:
            logger.error("Error de red en %s: %s", url, exc)
            if attempt == retries - 1:
                return {}
            time.sleep(2.0)

    logger.error("Agotados %d reintentos para %s", retries, url)
    return {}


def _write(method: str, url: str, payload: dict | None = None,
           retries: int = BSALE_MAX_RETRIES) -> dict[str, Any]:
    """
    Helper genérico para POST/PUT/DELETE a BSale.
    Aplica rate limiting + retries con backoff y devuelve JSON.

    Lanza RuntimeError si todos los intentos fallan, con el detalle
    del último error para que el endpoint API pueda devolverlo al cliente.
    """
    last_status: int | None = None
    last_body: str = ""
    for attempt in range(retries):
        _limiter.acquire()
        try:
            kwargs = {"headers": _build_headers(), "timeout": BSALE_TIMEOUT}
            if payload is not None:
                kwargs["json"] = payload

            sess = _session()
            if method == "POST":
                resp = sess.post(url, **kwargs)
            elif method == "PUT":
                resp = sess.put(url, **kwargs)
            elif method == "DELETE":
                resp = sess.delete(url, **kwargs)
            else:
                raise ValueError(f"Metodo no soportado: {method}")

            last_status = resp.status_code
            last_body = resp.text[:500] if resp.text else ""

            if resp.status_code in (200, 201, 204):
                return resp.json() if resp.text else {}

            if resp.status_code == 429:
                wait = 2.0 * (attempt + 1)
                logger.warning("429 Rate Limited en %s %s, esperando %.1fs",
                               method, url, wait)
                time.sleep(wait)
                continue

            # 4xx que NO sea 429 = error del cliente, no reintentar
            if 400 <= resp.status_code < 500:
                raise RuntimeError(
                    f"BSale {method} {url} -> HTTP {resp.status_code}: {last_body}"
                )

            # 5xx = transitorio, reintentar
            logger.warning("HTTP %d en %s %s (intento %d/%d)",
                           resp.status_code, method, url, attempt + 1, retries)
            time.sleep(1.5 * (attempt + 1))

        except requests.exceptions.Timeout:
            logger.warning("Timeout en %s %s (intento %d/%d)",
                           method, url, attempt + 1, retries)
            time.sleep(2.0 * (attempt + 1))
        except requests.exceptions.RequestException as exc:
            logger.error("Error de red en %s %s: %s", method, url, exc)
            if attempt == retries - 1:
                raise RuntimeError(f"Network error: {exc}") from exc
            time.sleep(2.0)

    raise RuntimeError(
        f"BSale {method} {url} fallo despues de {retries} intentos. "
        f"Ultimo status={last_status}, body={last_body}"
    )


def post(endpoint: str, payload: dict) -> dict[str, Any]:
    """POST a BSale. Endpoint sin slash inicial, ej: 'product_types.json'."""
    return _write("POST", f"{BSALE_BASE_URL}/{endpoint}", payload)


def put(endpoint: str, payload: dict) -> dict[str, Any]:
    """PUT a BSale, ej: 'product_types/123.json'."""
    return _write("PUT", f"{BSALE_BASE_URL}/{endpoint}", payload)


def delete(endpoint: str) -> dict[str, Any]:
    """DELETE a BSale, ej: 'product_types/123.json'."""
    return _write("DELETE", f"{BSALE_BASE_URL}/{endpoint}")


# ---------------------------------------------------------------------------
# PAGINACIÓN
# ---------------------------------------------------------------------------


def _paged_url(base_url: str, page_size: int, offset: int, extra: str = "") -> str:
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}limit={page_size}&offset={offset}{extra}"


def _fetch_offsets(base_url: str, page_size: int, offsets: list[int],
                   extra: str = "", max_workers: int = BSALE_MAX_WORKERS
                   ) -> list[tuple[int, dict]]:
    """Baja varias páginas en paralelo. Devuelve [(offset, data)] en orden."""
    if not offsets:
        return []
    urls = [_paged_url(base_url, page_size, o, extra) for o in offsets]
    workers = max(1, min(max_workers, len(urls)))
    if workers == 1:
        return [(o, fetch(u)) for o, u in zip(offsets, urls)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as exe:
        # exe.map preserva el orden de entrada
        return list(zip(offsets, exe.map(fetch, urls)))


def _paginate_url(base_url: str, page_size: int, extra: str = "",
                  max_workers: int = BSALE_MAX_WORKERS,
                  label: str | None = None) -> tuple[list[dict], int | None]:
    """Paginación genérica (paralela) sobre una URL completa.

    1. Pide offset=0 → obtiene items + `count`.
    2. Con el count pre-computa los offsets restantes y los baja en paralelo.
    3. Si la última página vino llena (aparecieron registros durante la
       descarga) o BSale no devolvió count, sigue en modo serial hasta
       encontrar una página incompleta.

    Returns: (items, count_reportado_por_bsale)
    """
    label = label or base_url
    first = fetch(_paged_url(base_url, page_size, 0, extra))
    if not first:
        raise RuntimeError(f"BSale no respondio en offset=0 para {label}")

    items: list[dict] = list(first.get("items") or [])
    total = first.get("count")
    if not items or len(items) < page_size:
        return items, total

    next_offset = page_size
    if isinstance(total, int) and total > page_size:
        offsets = list(range(page_size, total, page_size))
        pages_by_offset: dict[int, list[dict]] = {}
        failed_offsets: list[int] = []

        for off, data in _fetch_offsets(base_url, page_size, offsets, extra, max_workers):
            if not data:
                failed_offsets.append(off)
            else:
                pages_by_offset[off] = data.get("items") or []

        # Segunda pasada sobre páginas que agotaron reintentos por hipo transitorio
        if failed_offsets:
            logger.warning("%s: reintentando %d paginas fallidas tras pausa...",
                           label, len(failed_offsets))
            time.sleep(2.0)
            still_failed: list[int] = []
            for off, data in _fetch_offsets(base_url, page_size, failed_offsets, extra,
                                            min(4, max_workers)):
                if not data:
                    still_failed.append(off)
                else:
                    pages_by_offset[off] = data.get("items") or []
            if still_failed:
                raise RuntimeError(
                    f"Paginacion incompleta en {label}: fallaron {len(still_failed)} "
                    f"paginas (offsets={still_failed[:5]})"
                )

        last_len = page_size
        for off in offsets:
            page_items = pages_by_offset.get(off, [])
            items.extend(page_items)
            last_len = len(page_items)

        next_offset = offsets[-1] + page_size
        if last_len < page_size:
            return items, total

    # Fallback serial (sin count, o el total creció durante la descarga)
    while True:
        data = fetch(_paged_url(base_url, page_size, next_offset, extra))
        if not data:
            raise RuntimeError(
                f"Paginacion serial incompleta en {label} (offset={next_offset})"
            )
        page_items = data.get("items") or []
        if not page_items:
            break
        items.extend(page_items)
        if len(page_items) < page_size:
            break
        next_offset += page_size

    return items, total


def paginate(endpoint: str, extra_params: str = "",
             max_workers: int = BSALE_MAX_WORKERS) -> list[dict]:
    """
    Paginacion automatica (paralela) sobre un endpoint BSale.

    Args:
        endpoint: ruta relativa (ej: "/offices.json")
        extra_params: parametros adicionales (ej: "&state=0&expand=[product]")
        max_workers: hilos para bajar páginas en paralelo (el RateLimiter
            global sigue limitando a BSALE_MAX_RPS).

    Returns:
        Lista de todos los items combinados de todas las paginas.
    """
    items, total = _paginate_url(
        f"{BSALE_BASE_URL}{endpoint}", BSALE_PAGE_SIZE, extra_params,
        max_workers=max_workers, label=endpoint,
    )
    logger.info(
        "Paginacion %s completada: %d/%s items obtenidos",
        endpoint, len(items), total if total is not None else "?",
    )
    return items


def paginate_newer(endpoint: str, extra_params: str, known_count: int,
                   known_max_id: int, margin: int = 100,
                   max_workers: int = BSALE_MAX_WORKERS) -> list[dict] | None:
    """Descarga solo la "cola" de un listado BSale (los registros más nuevos).

    BSale v1 no ofrece filtro por rango de fechas en recepciones/consumos
    (solo fecha exacta), pero lista por id ascendente. Si localmente ya
    tenemos `known_count` registros (con id máximo `known_max_id`), los
    nuevos están al final: se baja desde offset = known_count - margin.

    AUTO-VERIFICACIÓN: el resultado solo se acepta si los ids vienen
    ascendentes y el primero <= known_max_id (es decir, la cola se solapa
    con lo que ya tenemos → no quedó ningún hueco). Si algo no cuadra
    devuelve None y el llamador debe hacer la paginación completa.
    """
    base_url = f"{BSALE_BASE_URL}{endpoint}"
    head = fetch(_paged_url(base_url, 1, 0, extra_params))
    total = head.get("count") if head else None
    if not isinstance(total, int):
        return None

    start = max(0, min(known_count, total) - margin)
    if start == 0:
        # Pocos registros: la cola es todo el listado.
        items, _ = _paginate_url(base_url, BSALE_PAGE_SIZE, extra_params,
                                 max_workers=max_workers, label=endpoint)
        return items

    offsets = list(range(start, total, BSALE_PAGE_SIZE))
    items: list[dict] = []
    last_len = 0
    for _off, data in _fetch_offsets(base_url, BSALE_PAGE_SIZE, offsets,
                                     extra_params, max_workers):
        if not data:
            logger.warning("paginate_newer %s: pagina fallida, se usara full", endpoint)
            return None
        page_items = data.get("items") or []
        items.extend(page_items)
        last_len = len(page_items)

    # Si entraron registros nuevos justo durante la descarga en un borde exacto de 50
    next_offset = (offsets[-1] + BSALE_PAGE_SIZE) if offsets else start
    while last_len == BSALE_PAGE_SIZE:
        data = fetch(_paged_url(base_url, BSALE_PAGE_SIZE, next_offset, extra_params))
        if not data:
            return None
        page_items = data.get("items") or []
        if not page_items:
            break
        items.extend(page_items)
        last_len = len(page_items)
        next_offset += BSALE_PAGE_SIZE

    ids = [int(it.get("id") or 0) for it in items]
    if not ids:
        return None
    ascending = all(a < b for a, b in zip(ids, ids[1:]))
    overlaps = ids[0] <= known_max_id
    if not (ascending and overlaps):
        logger.warning(
            "paginate_newer %s: no se pudo verificar orden/solape "
            "(ascendente=%s, solapa=%s) -> fallback a paginacion completa",
            endpoint, ascending, overlaps,
        )
        return None

    logger.info("Paginacion incremental %s: %d items (de %d totales, desde offset %d)",
                endpoint, len(items), total, start)
    return items


def fetch_subresource(url: str, page_size: int = 25,
                      max_workers: int = 4) -> list[dict]:
    """
    Pagina un sub-recurso (ej: details dentro de un documento).
    BSale pagina detalles con limit=25 por defecto.

    Si BSale devuelve `count`, las páginas restantes se bajan en paralelo
    (pocos hilos por defecto: esta función suele llamarse desde dentro de
    otros pools).

    Args:
        url: URL completa del sub-recurso (viene en el JSON)
        page_size: tamanio de pagina del sub-recurso
        max_workers: hilos para páginas restantes

    Returns:
        Lista de todos los items del sub-recurso.
    """
    items, _ = _paginate_url(url, page_size, max_workers=max_workers)
    return items
