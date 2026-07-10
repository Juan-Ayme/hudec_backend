"""
KAWII Backend API - entrypoint FastAPI.

Levantar en dev:
    cd produccion
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

Docs interactivas:
    http://localhost:8000/docs       (Swagger UI)
    http://localhost:8000/redoc      (ReDoc)

Nota: el frontend/dashboard es un proyecto separado que consume esta API.
Esta API NO sirve archivos estaticos.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import engine
from app.logging_config import setup_logging
from app.middleware.logging import RequestContextMiddleware
from sqlalchemy import text
from app.routers import (
    analytics,
    audits,
    auth,
    bsale_admin,
    catalog_health,
    category_targets,
    config_admin,
    costs_audit,
    diagnosis,
    documents,
    matrix_simulator,
    plan,
    products,
    pulse,
    purchases,
    stock,
    sync,
    taxonomy,
    taxonomy_admin,
)
from app.kawii_matrix.router import router as matrix_router

# Config central de logging (text en dev, json en prod via LOG_FORMAT).
# Reemplaza el viejo logging.basicConfig — un solo punto de verdad para el
# formato, con contexto de request (request_id/company_id/user_id) inyectado.
setup_logging(get_settings().LOG_FORMAT)
logger = logging.getLogger("kawii.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: bootstrap del admin inicial. Shutdown: cierra el engine."""
    logger.info("Iniciando KAWII API...")
    # Sembrar el primer admin si la tabla app_users está vacía.
    try:
        from app.auth import bootstrap_first_admin
        from app.database import async_session_maker
        async with async_session_maker() as session:
            await bootstrap_first_admin(session)
    except Exception as exc:
        # No bloquear el arranque si la tabla aún no existe (primer deploy)
        logger.warning("No se pudo correr bootstrap_first_admin: %s", exc)
    yield
    logger.info("Apagando KAWII API...")
    await engine.dispose()


settings = get_settings()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "API REST sobre la base de datos KAWII (PostgreSQL + ETL BSale). "
        "Expone taxonomia, productos, stock, documentos, analytics y "
        "permite disparar sincronizaciones."
    ),
    lifespan=lifespan,
)

# RequestContextMiddleware se agrega ANTES que CORS → en este proyecto el
# primero agregado queda más AFUERA (ver nota de orden más abajo), así envuelve
# todo el request: genera request_id, resuelve company_id/user_id, mide duración
# y emite la línea de acceso con el status_code final.
app.add_middleware(RequestContextMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# GZip va DESPUÉS de CORS en add_middleware → Starlette los ejecuta en orden
# inverso, así CORS queda más afuera (maneja preflight sin tocar compresión) y
# GZip queda más adentro (comprime el body de la respuesta). minimum_size=1024
# evita comprimir payloads chicos donde el overhead no compensa.
app.add_middleware(GZipMiddleware, minimum_size=1024)


# ---- Routers ----
app.include_router(taxonomy.router)
app.include_router(taxonomy_admin.router)   # CRUD interno (departments/categories/subcategories)
app.include_router(bsale_admin.router)      # CRUD que escribe a BSale (product_types)
app.include_router(products.router)
app.include_router(stock.router)
app.include_router(documents.router)
app.include_router(analytics.router)
app.include_router(diagnosis.router)        # /diagnosis — Vista 2 (¿por qué vendo menos hoy?)
app.include_router(pulse.router)            # /pulse — Vista 1 (¿cómo voy hoy?)
app.include_router(catalog_health.router)   # /catalog-health — Vista 3 (¿qué comprar/liquidar/reponer?)
app.include_router(plan.router)             # /plan — Vista 4 (¿llegaré a la meta y cómo planifico el próximo?)
app.include_router(category_targets.router) # /config/category-targets — metas/roles por categoría motor (bootstrap automático)
app.include_router(costs_audit.router)      # /config/variant-costs — auditoría y backfill de costos desde recepciones
app.include_router(sync.router)
app.include_router(audits.router)
app.include_router(auth.router)             # /auth/* — login, logout, gestión de usuarios
app.include_router(config_admin.router)     # /config/* — configuración runtime (exclusiones)
app.include_router(purchases.router)        # /purchases/* — decisiones de compra del catálogo
app.include_router(matrix_router)  # /matrix/* — matrices de clasificación inteligente
app.include_router(matrix_simulator.router)  # /matrix-sim/* — simulador por SKU


# ---- Root / health ----

@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "health": "/health",
    }


from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db

@app.get("/health", tags=["meta"])
async def health(db: AsyncSession = Depends(get_db)) -> dict:
    """Healthcheck con ping a la BD."""
    try:
        db_version = await db.scalar(text("SELECT version()"))
        db_ok = "ok"
        productos = await db.scalar(text("SELECT COUNT(*) FROM products"))
    except Exception as exc:
        logger.exception("Health check DB fallo: %s", exc)
        db_ok = f"error: {exc}"
        db_version = None
        productos = None

    return {
        "status": "ok" if db_ok == "ok" else "degraded",
        "db": db_ok,
        "db_version": db_version.split(",")[0] if db_version else None,
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "productos_en_bd": productos,
        "timestamp": datetime.utcnow().isoformat(),
    }


# ---- Error handler global ----

@app.exception_handler(Exception)
async def unhandled_exception(request, exc):
    """
    Captura cualquier excepcion no manejada y devuelve 500 JSON.

    Importante: los exception handlers de FastAPI NO pasan por el CORSMiddleware,
    asi que tenemos que agregar los headers CORS a mano. Sin esto el browser
    bloquea la respuesta con CORS error y el frontend ve un generico
    'Failed to fetch' en vez del 500 con detalle.
    """
    logger.exception("Error no manejado en %s %s: %s", request.method, request.url, exc)

    # Echo del Origin del request (o '*') en Access-Control-Allow-Origin para
    # respetar la lista CORS_ORIGINS configurada.
    origin = request.headers.get("origin", "*")
    settings = get_settings()
    allowed = settings.CORS_ORIGINS
    if "*" in allowed or origin in allowed:
        allow_origin = origin
    else:
        allow_origin = allowed[0] if allowed else "*"

    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": str(exc)},
        headers={
            "Access-Control-Allow-Origin": allow_origin,
            "Access-Control-Allow-Credentials": "true",
            "Vary": "Origin",
        },
    )


