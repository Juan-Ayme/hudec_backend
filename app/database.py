import logging
import os
from collections.abc import AsyncGenerator

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("kawii.db")

load_dotenv()

DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASS = os.environ.get("DB_PASS") or os.environ.get("DB_PASSWORD", "postgres")
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "kawii_db")

DATABASE_URL = f"postgresql+asyncpg://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# SSL: Neon (y cualquier Postgres managed en cloud) rechaza conexiones sin TLS.
# asyncpg NO lo pide por default. Lo activamos cuando:
#   - DB_SSLMODE está como "require" / "verify-ca" / "verify-full" en el entorno, o
#   - el host es .neon.tech (autodetect, evita que se rompa al deployar)
# En local con Postgres sin SSL, dejar DB_SSLMODE sin setear y usar localhost.
_sslmode = os.environ.get("DB_SSLMODE", "").lower()
_connect_args: dict = {}
if _sslmode in ("require", "verify-ca", "verify-full") or ".neon.tech" in DB_HOST:
    _connect_args["ssl"] = True

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
    connect_args=_connect_args,
)

async_session_maker = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependencia para inyectar la sesión asíncrona en los endpoints.
    """
    async with async_session_maker() as session:
        try:
            yield session
        except StarletteHTTPException:
            # 401/403/404/... son control de flujo HTTP normal, no fallos de DB:
            # se re-levantan sin ruido de traceback.
            raise
        except Exception:
            # Errores reales durante el request (SQL, conexión rota, bug):
            # dejan traceback correlacionado por el filtro de contexto.
            logger.exception("Error no controlado durante la sesión de base de datos")
            raise
        finally:
            await session.close()
