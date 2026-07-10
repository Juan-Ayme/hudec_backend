"""
Configuración central de logging del backend KAWII.

Un solo punto de verdad para el formato de logs. Toggle por env `LOG_FORMAT`:
  - `text` (default, dev): línea legible con contexto de request al final.
  - `json` (prod / Render): un objeto JSON por línea, ingerible por un
    agregador de logs (Datadog / Loki / etc.).

Cada record incluye los campos de contexto `request_id`, `company_id` y
`user_id`, inyectados por `app.middleware.logging.RequestContextFilter` a
partir de los `contextvars` que setea el middleware de request. Si no hay
contexto (ej. un script CLI como `mt_sync`), los formatters usan `"-"`.

Jerarquía de loggers: todo el backend cuelga de `kawii.*`
(`kawii.api`, `kawii.auth`, `kawii.matrix.*`, `kawii.events`, `kawii.access`,
`kawii.db`, ...). El ETL usa `harvester.*`. Ambas jerarquías comparten el
mismo formatter (ver `build_formatter`, reusado por `mt_sync`).

No agrega dependencias: el `JsonFormatter` está escrito sobre stdlib `json`.
"""

from __future__ import annotations

import json
import logging
import logging.config
from datetime import datetime, timezone

# Formato de la línea de texto (el contexto de request se añade al final por
# TextFormatter, solo cuando existe, para no ensuciar logs de CLI).
TEXT_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

# Atributos estándar de un LogRecord: se excluyen al recolectar los "extra"
# arbitrarios para el JSON (los campos de contexto se emiten explícitamente).
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
        # Campos de contexto que ya emitimos explícitamente en el JSON.
        "request_id", "company_id", "user_id",
    }
)


class TextFormatter(logging.Formatter):
    """Formatter de texto que anexa el contexto de request al final de la línea,
    pero solo cuando hay contexto (evita `[rid=- co=- u=-]` en logs de CLI)."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        parts: list[str] = []
        rid = getattr(record, "request_id", "-")
        if rid and rid != "-":
            parts.append(f"rid={rid}")
        co = getattr(record, "company_id", "-")
        if co and co != "-":
            parts.append(f"co={co}")
        uid = getattr(record, "user_id", "-")
        if uid and uid != "-":
            parts.append(f"u={uid}")
        if parts:
            return f"{base} [{' '.join(parts)}]"
        return base


class JsonFormatter(logging.Formatter):
    """Formatter JSON estructurado, una línea por record. Sin dependencias.

    Incluye siempre los campos de contexto (`request_id`, `company_id`,
    `user_id`) y añade cualquier `extra=...` serializable que traiga el record
    (ej. `duration_ms`, `event_type`, `status_code`)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "company_id": getattr(record, "company_id", "-"),
            "user_id": getattr(record, "user_id", "-"),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        # Extras arbitrarios pasados via `logger.info(..., extra={...})`.
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key in payload:
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = str(value)

        return json.dumps(payload, ensure_ascii=False, default=str)


def build_formatter(log_format: str = "text") -> logging.Formatter:
    """Devuelve una instancia del formatter según `log_format` (`text`|`json`).

    Reusado por `tools/maintenance/mt_sync.py` para aplicar el MISMO formato a
    sus handlers (StreamHandler + FileHandler) sin pasar por `dictConfig`.
    """
    if (log_format or "text").strip().lower() == "json":
        return JsonFormatter()
    return TextFormatter(TEXT_FMT, DATEFMT)


def setup_logging(log_format: str = "text") -> None:
    """Configura el logging global del proceso vía `dictConfig`.

    Reemplaza cualquier `basicConfig` previo. Idempotente: llamarla más de una
    vez re-aplica la config (útil en tests). Adjunta el `RequestContextFilter`
    al handler de consola para que TODO record (propagado o no) reciba los
    campos de contexto de request.
    """
    fmt = (log_format or "text").strip().lower()
    formatter_name = "json" if fmt == "json" else "text"

    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "request_context": {
                "()": "app.middleware.logging.RequestContextFilter",
            },
        },
        "formatters": {
            "text": {
                "()": "app.logging_config.TextFormatter",
                "format": TEXT_FMT,
                "datefmt": DATEFMT,
            },
            "json": {
                "()": "app.logging_config.JsonFormatter",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": formatter_name,
                "filters": ["request_context"],
                "stream": "ext://sys.stdout",
            },
        },
        "root": {"level": "INFO", "handlers": ["console"]},
        "loggers": {
            # Jerarquías propias: heredan el handler de consola vía propagación.
            "kawii": {"level": "INFO"},
            "harvester": {"level": "INFO"},
            # Ruido de terceros: bajamos el nivel. Tenemos nuestra propia línea
            # de acceso (kawii.access) así que silenciamos la de uvicorn.
            "uvicorn.access": {"level": "WARNING"},
            "uvicorn.error": {"level": "INFO"},
            "sqlalchemy.engine": {"level": "WARNING"},
        },
    }
    logging.config.dictConfig(config)
