"""
Contexto de request + middleware de logging de acceso.

Piezas:
  - `contextvars` para `request_id`, `company_id`, `user_id`. Viven por request
    (Starlette corre cada request en su propia copia de contexto, así que no se
    filtran entre requests).
  - `RequestContextFilter`: un `logging.Filter` que copia esos contextvars a
    cada `LogRecord` (con default `"-"`). Se adjunta al handler de consola en
    `app.logging_config.setup_logging`, así TODO log emitido durante un request
    queda correlacionado.
  - `RequestContextMiddleware`: genera un `request_id` corto, resuelve de forma
    barata `user_id` (del JWT en la cookie) y `company_id` (del header
    `X-Company-Id`), mide la duración con `time.perf_counter()` y emite UNA
    línea de acceso a nivel INFO al terminar.

Nota de diseño — por qué se resuelve el contexto acá y no solo en las
dependencias de auth: `BaseHTTPMiddleware` corre el endpoint en una task hija
con una COPIA del contexto, por lo que los contextvars que setean
`get_current_user`/`get_current_company` NO vuelven a ser visibles en el
middleware al momento de loguear la línea de acceso. Resolverlos acá (barato,
sin tocar la DB) garantiza que la línea de acceso lleve el contexto y que
además se propague hacia abajo a todos los logs del request. Es best-effort:
`company_id` es el que el cliente declara en el header (la membresía la valida
`get_current_company`); para la línea de acceso alcanza.
"""

from __future__ import annotations

import contextvars
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("kawii.access")

# ── Contextvars de request (default "-" => sin contexto) ──
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)
company_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "company_id", default="-"
)
user_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "user_id", default="-"
)


class RequestContextFilter(logging.Filter):
    """Inyecta request_id / company_id / user_id en cada LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.company_id = company_id_var.get()
        record.user_id = user_id_var.get()
        return True


# ── Setters/getters usados por auth y por el helper de eventos ──

def set_request_id(value: str) -> None:
    request_id_var.set(value or "-")


def set_company_id(value) -> None:
    company_id_var.set(str(value) if value not in (None, "") else "-")


def set_user_id(value) -> None:
    user_id_var.set(str(value) if value not in (None, "") else "-")


def get_request_id() -> str | None:
    rid = request_id_var.get()
    return None if rid == "-" else rid


def _resolve_user_id(request) -> str:
    """Decodifica el JWT de la cookie para obtener el user_id. Best-effort: sin
    cookie / token inválido devuelve '-'. No toca la DB. Import diferido para
    evitar un ciclo de import a nivel de módulo con `app.auth`."""
    try:
        from app.auth import COOKIE_NAME, decode_access_token

        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return "-"
        payload = decode_access_token(token)
        if payload and payload.get("sub"):
            return str(payload["sub"])
    except Exception:
        pass
    return "-"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Setea el contexto del request y loguea una línea de acceso por request."""

    async def dispatch(self, request, call_next):
        request_id = uuid.uuid4().hex[:8]
        request_id_var.set(request_id)
        user_id_var.set(_resolve_user_id(request))
        company_id_var.set(str(request.headers.get("x-company-id") or "-"))

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            # El exception_handler global de main.py va a loguear el traceback;
            # acá dejamos la línea de acceso con el 500 para no perderla.
            logger.info(
                "%s %s -> 500 (%.1fms)",
                request.method,
                request.url.path,
                duration_ms,
                extra={
                    "http_method": request.method,
                    "path": request.url.path,
                    "status_code": 500,
                    "duration_ms": duration_ms,
                },
            )
            raise

        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        logger.info(
            "%s %s -> %d (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            extra={
                "http_method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        # Correlación cliente <-> servidor: el front puede loguear este id.
        response.headers["X-Request-ID"] = request_id
        return response
