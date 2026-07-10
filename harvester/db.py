"""
Capa de base de datos para el Harvester.

Provee:
  - Pool de conexiones
  - Helper para executemany (batch upsert)
  - Sync log (inicio/fin de cada entidad)
  - Data quality issue logger
"""

import logging
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.pool
import psycopg2.extras

from harvester.config import DB_CONFIG
from harvester.tenant_context import current_company_id

logger = logging.getLogger("harvester.db")

# Pool de conexiones (min=2, max=12)
# Subido de max=4 a max=12 para soportar la paralelización inter-fase
# (orquestador en mt_sync.py corre 5 bloques de sync en paralelo, cada uno
# con su propio worker pool de hasta 8 → contención en la pool vieja).
_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def init_pool():
    """Inicializa el pool de conexiones. Llamar una vez al inicio."""
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2, maxconn=12, **DB_CONFIG
        )
        logger.info("Pool de conexiones inicializado (%s:%s/%s) [min=2, max=12]",
                     DB_CONFIG["host"], DB_CONFIG["port"], DB_CONFIG["dbname"])


def close_pool():
    """Cierra todas las conexiones del pool."""
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None
        logger.info("Pool de conexiones cerrado")


def _acquire_live_conn():
    """
    Obtiene una conexion del pool descartando las que esten cerradas.
    Neon (Postgres serverless) cierra conexiones idle silenciosamente;
    psycopg2 marca conn.closed=1 al detectarlas, pero el pool no las purga
    solo. Hacemos ping (SELECT 1) para garantizar que este viva.
    """
    for _ in range(10):  # Intentos suficientes para limpiar un pool lleno de zombies
        try:
            conn = _pool.getconn()
        except psycopg2.pool.PoolError:
            # Si el pool falla al dar conexion
            raise RuntimeError("No se pudo obtener una conexion viva del pool (PoolError)")
            
        if conn.closed == 0:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                return conn
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                # Conexion muerta (TCP drop silencioso), cerrarla y reintentar
                _pool.putconn(conn, close=True)
                continue
        else:
            # Conexion zombie detectada por psycopg2
            _pool.putconn(conn, close=True)

    logger.error(
        "No se pudo obtener una conexion viva del pool tras 10 intentos "
        "(todas cerradas/zombie). Pool %s:%s/%s",
        DB_CONFIG.get("host"), DB_CONFIG.get("port"), DB_CONFIG.get("dbname"),
    )
    raise RuntimeError("No se pudo obtener una conexion viva del pool tras 10 intentos")


@contextmanager
def get_conn():
    """Context manager que obtiene y devuelve una conexion al pool."""
    if _pool is None:
        raise RuntimeError("Pool no inicializado. Llama a init_pool() primero.")
    conn = _acquire_live_conn()
    broken = False
    try:
        # Activar contexto RLS para esta transacción: Postgres necesita
        # saber el tenant activo para que WITH CHECK valide los INSERTs.
        # SET LOCAL se resetea automáticamente en commit()/rollback().
        try:
            cid = current_company_id()
            with conn.cursor() as cur:
                cur.execute("SET LOCAL app.current_company = %s", (str(cid),))
        except Exception:
            pass  # Sin tenant activo — RLS USING permite NULL para lecturas
        yield conn
        conn.commit()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        # Conexion rota: no intentamos rollback (fallaria igual) y la
        # devolvemos al pool marcada para cierre.
        broken = True
        raise
    except Exception:
        try:
            conn.rollback()
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            broken = True
        raise
    finally:
        _pool.putconn(conn, close=broken)


def execute_batch(sql: str, rows: list[tuple], page_size: int = 100) -> int:
    """
    Ejecuta un batch de inserts/upserts eficientemente.

    Args:
        sql: Query SQL con %s placeholders
        rows: Lista de tuplas con los valores
        page_size: Tamanio del batch interno de psycopg2

    Returns:
        Cantidad de filas procesadas

    Reintenta 1 vez si la conexion del pool resulto muerta en la primera
    invocacion (Neon cierra conexiones idle tras ~5 min). get_conn() ya
    purga la conexion rota del pool, asi que el reintento toma una sana.
    """
    if not rows:
        return 0

    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_batch(cur, sql, rows, page_size=page_size)
            return len(rows)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            last_exc = exc
            if attempt == 0:
                logger.warning(
                    "execute_batch fallo por conexion rota, reintentando: %s", exc
                )
                continue
            raise
    # Inalcanzable: el raise del attempt==1 ya salio.
    raise last_exc  # type: ignore[misc]


# --- Sync Log ---

def sync_start(entity: str, params: dict | None = None) -> int:
    """Registra inicio de sync. Retorna el ID del log.
    Requiere tenant activo (harvester.tenant_context)."""
    cid = current_company_id()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO sync_log (company_id, entity, status, params)
                   VALUES (%s, %s, 'RUNNING', %s) RETURNING id""",
                (cid, entity, psycopg2.extras.Json(params)),
            )
            row = cur.fetchone()
            log_id = row[0]
    logger.info("Sync iniciada: %s (log_id=%d, company_id=%d)", entity, log_id, cid)
    return log_id


def sync_finish(
    log_id: int,
    *,
    status: str = "SUCCESS",
    fetched: int = 0,
    inserted: int = 0,
    updated: int = 0,
    skipped: int = 0,
    error: str | None = None,
):
    """Registra fin de sync con metricas."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE sync_log
                   SET finished_at = NOW(), status = %s,
                       records_fetched = %s, records_inserted = %s,
                       records_updated = %s, records_skipped = %s,
                       error_message = %s
                   WHERE id = %s""",
                (status, fetched, inserted, updated, skipped, error, log_id),
            )
    level = logging.INFO if status == "SUCCESS" else logging.ERROR
    logger.log(
        level,
        "Sync finalizada (log_id=%d): %s | fetched=%d inserted=%d updated=%d skipped=%d",
        log_id, status, fetched, inserted, updated, skipped,
    )


# --- Data Quality ---

def log_quality_issue(
    entity: str,
    bsale_id: int | None,
    field: str,
    issue_type: str,
    description: str,
    raw_value: str | None = None,
):
    """Registra un problema de calidad de datos sin detener el proceso.
    Requiere tenant activo."""
    try:
        cid = current_company_id()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO data_quality_issues
                       (company_id, entity, bsale_id, field, issue_type, description, raw_value)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (cid, entity, bsale_id, field, issue_type, description, raw_value),
                )
    except Exception as exc:
        logger.error("No se pudo registrar issue de calidad: %s", exc)
