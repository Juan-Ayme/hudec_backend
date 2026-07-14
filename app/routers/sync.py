"""Endpoints de administracion de sincronizacion."""

import logging
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, APIRouter, BackgroundTasks, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text
from pydantic import BaseModel, Field

from app.auth import CurrentCompany, CurrentUser, get_current_company, get_current_user, require_operador_or_admin
from app.database import get_db
from app.events import log_event
from app.kawii_matrix import cache as matrix_cache


class SyncRunRequest(BaseModel):
    days: int = Field(7, ge=1, le=3650)
    skip_documents: bool = False
    skip_stock_snapshot: bool = False


class StopTaskResult(BaseModel):
    ok: bool
    task_id: str
    status: str
    detail: str | None = None

# Toda la sincronización requiere operador o admin: dispara descargas largas
# de BSale (~90 min) y reescribe la BD interna.
router = APIRouter(
    prefix="/sync",
    tags=["sync"],
    dependencies=[Depends(require_operador_or_admin), Depends(get_current_company)],
)
logger = logging.getLogger(__name__)

# Estado in-memory de tareas disparadas desde la API
_task_state: dict[str, dict[str, Any]] = {}


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # produccion/


def _run_mt_sync(task_id: str, days: int,
                 skip_documents: bool, skip_stock_snapshot: bool) -> None:
    """Ejecuta mt_sync.py (orquestador multi-tenant) como subproceso.

    El handle del proceso queda en _task_state[task_id]["process"] para que
    POST /sync/{task_id}/stop pueda llamar process.terminate() desde otro
    request. El handle se remueve al final (no es JSON-serializable).
    """
    _task_state[task_id]["status"] = "RUNNING"
    _task_state[task_id]["started_at"] = datetime.utcnow().isoformat()

    cmd = [
        sys.executable, "-m", "tools.maintenance.mt_sync",
        "--days", str(days),
    ]
    if skip_documents:
        cmd.append("--skip-documents")
    if skip_stock_snapshot:
        cmd.append("--skip-stock-snapshot")

    try:
        proc = subprocess.Popen(
            cmd, cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        # Guardar el handle ANTES de bloquear en communicate(): el endpoint
        # /stop necesita acceder a él desde otro thread.
        _task_state[task_id]["process"] = proc
        try:
            stdout, stderr = proc.communicate(timeout=5400)  # 90 min
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            timed_out = True

        _task_state[task_id]["returncode"] = proc.returncode
        _task_state[task_id]["stdout_tail"] = stdout[-4000:] if stdout else ""
        _task_state[task_id]["stderr_tail"] = stderr[-2000:] if stderr else ""

        # Resolución del estado final:
        #   - Si /stop marcó "CANCELLING", el subprocess murió por terminate():
        #     queda como CANCELLED.
        #   - Si timeout: FAILED con detalle.
        #   - Sino, según returncode.
        if _task_state[task_id].get("status") == "CANCELLING":
            _task_state[task_id]["status"] = "CANCELLED"
        elif timed_out:
            _task_state[task_id]["status"] = "FAILED"
            _task_state[task_id]["error"] = "Timeout: el sync excedió 90 min"
        else:
            _task_state[task_id]["status"] = "SUCCESS" if proc.returncode == 0 else "FAILED"

        # Si el sync terminó OK, el contenido de la matriz cambió → tirar cache.
        # El cron diario corre en otro servicio de Render (no este proceso) y
        # ese caso NO lo cubrimos acá: a las 03:00 Lima nadie está en el
        # dashboard, y el TTL ya expiró cuando llegan a la mañana.
        if _task_state[task_id]["status"] == "SUCCESS":
            matrix_cache.invalidate()
    except Exception as exc:
        logger.exception("Error ejecutando mt_sync: %s", exc)
        _task_state[task_id]["status"] = "FAILED"
        _task_state[task_id]["error"] = str(exc)
    finally:
        _task_state[task_id]["finished_at"] = datetime.utcnow().isoformat()
        # Remover el handle: no es JSON-serializable y ya no sirve.
        _task_state[task_id].pop("process", None)


@router.post("/incremental")
async def trigger_incremental(
    company: CurrentCompany = Depends(get_current_company),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Sync RAPIDA (en proceso, NO subprocess) que solo refresca catalogo:
        - product_types
        - products + variants
        - subcategory_resolver

    NO descarga documentos, NO snapshotea stock, NO recalcula costos.
    Pensada para correr DESPUES de crear/editar product_types o cuando
    aparece un producto nuevo en BSale, asi se ve reflejado al instante.

    Devuelve un mini-informe JSON con stats por entidad.
    """
    started = datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "ok": True,
        "operation": "sync_incremental",
        "started_at": started.isoformat(),
        "stats": {},
        "warnings": [],
    }

    try:
        # Import diferido para no penalizar el arranque del API.
        from harvester import db as h_db, sync_masters

        # El harvester tiene su propio pool (separado del de la API).
        # init_pool() es idempotente: si ya esta inicializado, no hace nada.
        h_db.init_pool()

        # 1) Taxonomy NO se toca: vive solo en BD interna.
        # 2) product_types: trae cualquier categoria nueva creada en BSale.
        report["stats"]["product_types"] = sync_masters.sync_product_types()

        # 3) products + variants (sync_variants() crea ambos por FK).
        report["stats"]["variants"] = sync_masters.sync_variants()

    except Exception as exc:
        logger.exception("Error en sync incremental: %s", exc)
        report["ok"] = False
        report["error"] = str(exc)
        finished = datetime.now(timezone.utc)
        report["finished_at"] = finished.isoformat()
        report["duration_s"] = (finished - started).total_seconds()
        raise HTTPException(500, detail=report)

    finished = datetime.now(timezone.utc)
    report["finished_at"] = finished.isoformat()
    report["duration_s"] = (finished - started).total_seconds()

    # Productos huerfanos restantes (sin mapeo via product_type ni override)
    huerfanos_res = await db.execute(text("""
        SELECT COUNT(*) FROM v_products_full
        WHERE company_id = :cid AND department IS NULL
    """), {"cid": company.company_id})
    huerfanos = huerfanos_res.scalar() or 0
    report["productos_huerfanos"] = huerfanos
    
    if huerfanos > 0:
        report["warnings"].append(
            f"Quedan {huerfanos} productos sin mapear. "
            f"Crea product_types o usa PATCH /products/{{id}}/subcategory."
        )

    # El sync acaba de tocar products/variants → cualquier matriz cacheada
    # quedó vieja. Invalidamos para que el próximo hit refleje los datos nuevos.
    matrix_cache.invalidate()

    logger.info(
        "Sync incremental disparado por user_id=%s en company=%s (ok=%s)",
        user.id, company.company_id, report["ok"],
    )
    await log_event(
        db, company_id=company.company_id, event_type="sync.triggered",
        actor_user_id=user.id,
        payload={
            "mode": "incremental",
            "ok": report["ok"],
            "stats": report.get("stats", {}),
            "productos_huerfanos": report.get("productos_huerfanos"),
        },
        commit=True,
    )

    return report


@router.post("/run")
async def trigger_update(
    payload: SyncRunRequest | None = None,
    company: CurrentCompany = Depends(get_current_company),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Dispara mt_sync.py en background (sync multi-tenant).

    Acepta body JSON (recomendado) o query params:
        POST /sync/run
        {"days": 7, "skip_documents": false, "skip_stock_snapshot": false}

    Retorna un task_id para consultar estado via /sync/tasks/{task_id}.
    """
    params = payload or SyncRunRequest()
    task_id = uuid.uuid4().hex[:10]
    _task_state[task_id] = {
        "task_id": task_id,
        "status": "QUEUED",
        "params": params.model_dump(),
        "created_at": datetime.utcnow().isoformat(),
    }
    # No bloqueamos el request - corre en thread aparte
    thread = threading.Thread(
        target=_run_mt_sync,
        args=(task_id, params.days, params.skip_documents,
              params.skip_stock_snapshot),
        daemon=True,
    )
    thread.start()
    logger.info(
        "Sync full encolada task_id=%s por user_id=%s en company=%s (%d dias)",
        task_id, user.id, company.company_id, params.days,
    )
    await log_event(
        db, company_id=company.company_id, event_type="sync.triggered",
        actor_user_id=user.id,
        payload={"mode": "full", "task_id": task_id, **params.model_dump()},
        commit=True,
    )
    return {
        "ok": True,
        "message": f"Sync encolada ({params.days} dias)",
        "task_id": task_id,
    }


@router.get("/tasks")
async def list_tasks() -> list[dict]:
    """Tareas disparadas desde la API en esta instancia.

    Se excluye la clave 'process' (handle Popen, no JSON-serializable).
    """
    return [
        {k: v for k, v in t.items() if k != "process"}
        for t in _task_state.values()
    ]


@router.post("/{task_id}/stop", response_model=StopTaskResult)
async def stop_task(
    task_id: str,
    # La guardia de rol (operador/admin) ya la aplica el router vía
    # `dependencies=[Depends(require_operador_or_admin), ...]`. Acá inyectamos
    # get_current_user solo para obtener el username real: require_operador_or_admin
    # devuelve un CurrentCompany (sin .username), no un CurrentUser.
    user: CurrentUser = Depends(get_current_user),
) -> StopTaskResult:
    """Cancela una sync en curso enviando terminate() al subproceso.

    Códigos:
      - 404 si el task_id no existe en este worker.
      - 409 si la tarea ya terminó (SUCCESS / FAILED / CANCELLED).
      - 200 con status='CANCELLING' si el kill se envió. El estado pasa a
        'CANCELLED' en cuanto el thread del sync detecta la salida del
        subprocess (segundos). Confirmar con GET /sync/tasks.

    Notas:
      - El estado vive en memoria del worker; tras reiniciar Render el
        task_id deja de existir. Es esperable para el piloto: el subprocess
        hijo muere con el container, no queda zombie.
      - Los datos parciales que el sync alcanzó a escribir quedan en la DB:
        no se rollbackean (las fases del ETL son idempotentes con UPSERT,
        el próximo sync los completa).
    """
    task = _task_state.get(task_id)
    if not task:
        raise HTTPException(404, f"Task {task_id} no encontrada en este worker")

    current_status = task.get("status")
    if current_status in ("SUCCESS", "FAILED", "CANCELLED"):
        raise HTTPException(
            409, f"Task {task_id} ya terminó (status={current_status})"
        )

    now_iso = datetime.utcnow().isoformat()
    task["cancelled_by"] = user.username
    task["cancelled_at"] = now_iso

    proc = task.get("process")
    if proc is None:
        # Estaba QUEUED: aún no había arrancado el subprocess. Si el thread
        # llega a arrancar después, va a ver el status CANCELLED y debería
        # respetarlo, pero por las dudas marcamos también aquí.
        task["status"] = "CANCELLED"
        task["finished_at"] = now_iso
        return StopTaskResult(
            ok=True, task_id=task_id, status="CANCELLED",
            detail="Tarea estaba en cola (sin subprocess aún); marcada como CANCELLED.",
        )

    task["status"] = "CANCELLING"
    try:
        proc.terminate()  # SIGTERM en Unix, TerminateProcess en Windows.
    except Exception as exc:
        # No es fatal: el thread sigue corriendo, el subprocess sigue. Solo
        # logueamos para que el ops sepa.
        logger.warning("proc.terminate() falló para task %s: %s", task_id, exc)
        return StopTaskResult(
            ok=False, task_id=task_id, status="CANCELLING",
            detail=f"terminate() falló: {exc}. El sync puede continuar igualmente.",
        )

    logger.info("Sync %s cancelada por %s", task_id, user.username)
    return StopTaskResult(
        ok=True, task_id=task_id, status="CANCELLING",
        detail="Stop enviado al subprocess. Status final 'CANCELLED' en segundos.",
    )


# ENDPOINT COMENTADO (2026-06-20) — getSyncTask en api.ts pero ninguna página
# lo importa. El endpoint /sync/tasks (lista completa) sí se sigue usando.

# @router.get("/tasks/{task_id}")
# async def get_task(task_id: str) -> dict:
#     task = _task_state.get(task_id)
#     if not task:
#         raise HTTPException(404, f"Task {task_id} no encontrada")
#     return task


@router.get("/log")
async def sync_log(
    limit: int = Query(30, ge=1, le=500),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Historico de syncs de la empresa activa."""
    res = await db.execute(text("""
        SELECT id, entity, status, started_at, finished_at,
               records_fetched, records_inserted,
               records_updated, records_skipped, error_message,
               EXTRACT(EPOCH FROM (finished_at - started_at))::int AS duracion_s
        FROM sync_log
        WHERE company_id = :cid
        ORDER BY started_at DESC
        LIMIT :limit
    """), {"limit": limit, "cid": company.company_id})
    return [dict(r) for r in res.mappings().all()]


# GET /sync/log/{entity} eliminado — no consumido por el frontend



@router.get("/data-quality")
async def data_quality(
    limit: int = Query(100, ge=1, le=1000),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    res = await db.execute(text("""
        SELECT id, entity, bsale_id, field, issue_type, description, created_at
        FROM data_quality_issues
        WHERE company_id = :cid
        ORDER BY created_at DESC
        LIMIT :limit
    """), {"limit": limit, "cid": company.company_id})
    return [dict(r) for r in res.mappings().all()]
