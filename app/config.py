"""
Settings del backend (FastAPI).

Carga la configuracion desde el archivo .env usando pydantic-settings.
Si una variable no esta en .env, se usa el valor por defecto definido aqui.

NO importar este modulo directamente en codigo de harvester;
los settings de harvester estan en harvester/config.py.

Uso:
    from app.config import get_settings
    settings = get_settings()  # singleton con cache, seguro llamarlo multiples veces
"""

import logging
import os
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

# Reutilizamos la config de conexion del harvester (misma DB, diferente pool)
from harvester.config import DB_CONFIG

logger = logging.getLogger("kawii.config")


class Settings(BaseSettings):
    """Configuracion de la API FastAPI. Cada campo puede sobreescribirse via .env."""

    # Lee automaticamente variables desde produccion/.env
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Identificacion de la app ---
    APP_NAME: str = "HUDEC Inventory BI"   # Aparece en /docs (Swagger) y en /health
    APP_VERSION: str = "1.0.0"    # Version semantica
    DEBUG: bool = False           # True = logs mas detallados (NO usar en produccion)

    # --- Logging ---
    # Formato de salida de los logs (ver app/logging_config.py):
    #   "text" (default): línea legible, ideal para desarrollo local.
    #   "json": un objeto JSON por línea, para producción / Render (ingerible
    #           por un agregador de logs). Cambiar via env LOG_FORMAT=json.
    LOG_FORMAT: str = "text"

    # --- Configuracion de marca (White-label) ---
    BRAND_NAME: str = "hudec"
    CLASSIFICATION_LABEL: str = "Clasificación HUDEC"
    TIMEZONE: str = "America/Lima"


    # --- CORS (Cross-Origin Resource Sharing) ---
    # Lista de origenes permitidos para hacer requests a la API desde el navegador.
    # "*" = cualquier origen (OK en desarrollo; restringir en produccion con el dominio real).
    CORS_ORIGINS: list[str] = ["*"]

    # --- Paginacion de respuestas ---
    DEFAULT_PAGE_SIZE: int = 50   # Registros por pagina si el cliente no especifica
    MAX_PAGE_SIZE: int = 500      # Maximo que el cliente puede pedir en un request

    # --- Pool de conexiones Postgres ---
    DB_POOL_MIN: int = 1   # Conexiones minimas que se mantienen abiertas
    DB_POOL_MAX: int = 10  # Maximo de conexiones simultaneas al Postgres

    # --- Cache de matrices ---
    # TTL en segundos del resultado base de cada matriz (cache in-process).
    # Reduce el egress de la DB managed: los hits dentro del TTL no vuelven
    # a correr el SQL pesado.
    #
    # Default 1800s (30 min): hoy la data solo cambia con el cron diario
    # (run_daily_sync.py, 08:00 UTC) o con un /sync manual. Cuando los
    # webhooks de BSale estén activos en prod, bajar a ~120s para limitar
    # el lag de stock/documents recibidos por push.
    #
    # 0 desactiva el cache. Si Render escala a >1 worker, mover a Redis.
    MATRIX_CACHE_TTL_SECONDS: int = 1800

    # --- Webhooks (BSale push notifications) ---
    # Secreto compartido que BSale debe enviar en el header
    # X-Kawii-Webhook-Secret al hacer POST /webhooks/bsale. Si está vacío
    # el endpoint responde 503 (no acepta tráfico anónimo).
    # Generar con: python -c "import secrets; print(secrets.token_urlsafe(32))"
    WEBHOOK_SECRET: str = ""


def _warn_unsafe_for_production(settings: "Settings") -> None:
    """Loguea warnings si los settings combinan DEBUG=false con defaults
    inseguros que SOLO deberían usarse en desarrollo. Si estás en producción
    revisá cada warning y arreglálo en el `.env` de la plataforma."""
    if settings.DEBUG:
        return  # En dev permitimos cualquier combinación.

    if settings.CORS_ORIGINS == ["*"]:
        logger.warning(
            "CORS_ORIGINS=['*'] con DEBUG=false. En producción restringilo "
            "al dominio del frontend (ej: CORS_ORIGINS=['https://app.empresa.com'])."
        )

    if not os.environ.get("JWT_SECRET"):
        logger.warning(
            "JWT_SECRET no está definido en el entorno con DEBUG=false. "
            "El proceso usa un secreto aleatorio que cambia en cada reinicio "
            "y todas las sesiones se invalidan. Generalo con "
            "`python -c \"import secrets; print(secrets.token_urlsafe(64))\"` "
            "y cargalo como variable de entorno."
        )

    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    if admin_password in ("", "admin", "CAMBIAR_INMEDIATAMENTE"):
        logger.warning(
            "ADMIN_PASSWORD ausente o con valor de plantilla. El bootstrap "
            "creará un admin con password 'admin'. Cambialo antes de exponer "
            "la API."
        )


@lru_cache  # Se instancia una sola vez y se reutiliza (patron singleton)
def get_settings() -> Settings:
    """Retorna la instancia singleton de Settings. Cachea el resultado."""
    settings = Settings()
    _warn_unsafe_for_production(settings)
    return settings


def get_db_config() -> dict:
    """Devuelve el diccionario de conexion a Postgres (mismo que usa el harvester)."""
    return DB_CONFIG
