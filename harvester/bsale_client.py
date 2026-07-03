"""
Cliente HTTP para la API de BSale.

Responsabilidades:
  - Rate limiting (max 9 req/s)
  - Reintentos con backoff exponencial
  - Paginacion automatica
  - Logging de cada request

NO tiene logica de negocio ni de base de datos.
"""

import time
import threading
import logging
import requests
from typing import Any

from harvester.config import (
    BSALE_BASE_URL,
    BSALE_PAGE_SIZE,
    BSALE_TIMEOUT,
    BSALE_MAX_RETRIES,
    BSALE_MAX_RPS,
)
from harvester.tenant_context import current_token

logger = logging.getLogger("harvester.bsale_client")


def _build_headers() -> dict:
    """Headers para BSale. El token viene del tenant activo (contextvar),
    para que cada empresa use el suyo. Levanta si no hay tenant seteado."""
    return {"access_token": current_token(), "Accept": "application/json"}


class RateLimiter:
    """Token-bucket rate limiter thread-safe."""

    def __init__(self, max_rps: int = BSALE_MAX_RPS):
        self._max = max_rps
        self._lock = threading.Lock()
        self._count = 0
        self._window_start = time.monotonic()

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._window_start

            # Reset window cada segundo
            if elapsed >= 1.0:
                self._window_start = now
                self._count = 0

            # Si ya alcanzamos el limite, dormimos lo que falta
            if self._count >= self._max:
                sleep_for = 1.0 - elapsed
                if sleep_for > 0:
                    time.sleep(sleep_for)
                self._window_start = time.monotonic()
                self._count = 0

            self._count += 1


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
            resp = requests.get(url, headers=_build_headers(), timeout=BSALE_TIMEOUT)

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

            if method == "POST":
                resp = requests.post(url, **kwargs)
            elif method == "PUT":
                resp = requests.put(url, **kwargs)
            elif method == "DELETE":
                resp = requests.delete(url, **kwargs)
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


def paginate(endpoint: str, extra_params: str = "") -> list[dict]:
    """
    Paginacion automatica sobre un endpoint BSale.

    Args:
        endpoint: ruta relativa (ej: "/offices.json")
        extra_params: parametros adicionales (ej: "&state=0&expand=[product]")

    Returns:
        Lista de todos los items combinados de todas las paginas.
    """
    all_items: list[dict] = []
    offset = 0
    total_expected = None

    while True:
        url = f"{BSALE_BASE_URL}{endpoint}?limit={BSALE_PAGE_SIZE}&offset={offset}{extra_params}"
        data = fetch(url)

        if not data:
            logger.warning("Respuesta vacia en offset=%d para %s", offset, endpoint)
            break

        items = data.get("items", [])
        if not items:
            break

        # Registrar el total esperado la primera vez
        if total_expected is None:
            total_expected = data.get("count", 0)
            logger.info(
                "Paginando %s: %d registros esperados",
                endpoint, total_expected,
            )

        all_items.extend(items)

        # Si la pagina vino incompleta, ya terminamos
        if len(items) < BSALE_PAGE_SIZE:
            break

        offset += BSALE_PAGE_SIZE

    logger.info(
        "Paginacion %s completada: %d/%s items obtenidos",
        endpoint, len(all_items), total_expected or "?",
    )
    return all_items


def fetch_subresource(url: str, page_size: int = 25) -> list[dict]:
    """
    Pagina un sub-recurso (ej: details dentro de un documento).
    BSale pagina detalles con limit=25 por defecto.

    Args:
        url: URL completa del sub-recurso (viene en el JSON)
        page_size: tamanio de pagina del sub-recurso

    Returns:
        Lista de todos los items del sub-recurso.
    """
    all_items: list[dict] = []
    offset = 0

    while True:
        separator = "&" if "?" in url else "?"
        paged_url = f"{url}{separator}limit={page_size}&offset={offset}"
        data = fetch(paged_url)

        if not data:
            break

        items = data.get("items", [])
        if not items:
            break

        all_items.extend(items)

        if len(items) < page_size:
            break

        offset += page_size

    return all_items
