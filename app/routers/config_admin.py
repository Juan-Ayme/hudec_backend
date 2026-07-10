"""
Configuración runtime editable (tabla `app_config`).

Gestiona las **exclusiones** de departamentos/categorías que se ocultan POR COMPLETO
de las matrices de clasificación. La DB es la fuente de verdad (editable en vivo desde
la UI); el `.env` ya NO controla esto.

★ Se guardan por NOMBRE (no por ID) porque los IDs de la taxonomía se reasignan cuando
  un sync re-siembra `departments`/`categories`. Guardar el nombre sobrevive a esos
  re-seeds; el ID actual se resuelve en cada consulta. Los nombres se almacenan como
  JSON (algunos nombres tienen comas, ej. "Vinos, Licores y Cervezas").

Solo las matrices usan estas exclusiones; analytics/documents filtran por sucursal.
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from pydantic import BaseModel

from app.auth import (
    CurrentCompany,
    CurrentUser,
    get_current_company,
    get_current_user,
    require_admin,
    require_operador_or_admin,
)
from app.database import get_db
from app.events import log_event
from app.config_defaults import (
    DEFAULT_THRESHOLDS,
    THRESHOLD_SECTIONS,
    DEFAULT_COMPANY,
    COMPANY_SECTIONS,
)
from app.kawii_matrix import cache as matrix_cache

# Toda la configuración es POR EMPRESA. Cada request necesita X-Company-Id
# (via get_current_company) para saber qué fila de app_config leer/escribir.
router = APIRouter(
    prefix="/config",
    tags=["config"],
    dependencies=[Depends(get_current_company)],
)


# ──────────────────────────────────────────────────────────────────────────────
# Historial / respaldos de app_config
#
# Cada PUT a /config/* guarda en `app_config_history` el valor ANTERIOR antes
# de pisar el actual. Permite restaurar a cualquier punto en el tiempo.
# Retención: últimos 50 snapshots automáticos por config_key + todos los
# manuales (is_manual = TRUE).
# ──────────────────────────────────────────────────────────────────────────────

# Keys versionables: lo que se respalda automáticamente y se exporta/importa.
BACKUP_KEYS: tuple[str, ...] = (
    "excluded_departments",
    "excluded_categories",
    "seasonal_departments",
    "thresholds",
    "company",
    "sales_goals",
)

# Cuántos snapshots automáticos retener por key antes de borrar los más viejos.
AUTO_RETENTION_PER_KEY = 50


async def _snapshot_before_change(
    db: AsyncSession,
    company_id: int,
    config_key: str,
    *,
    source: str,
    label: str | None = None,
    is_manual: bool = False,
) -> None:
    """Antes de pisar `app_config[company_id, key]`, copia el valor actual a
    `app_config_history`. Si no había fila previa, guarda con value=NULL para
    marcar el momento (útil para restaurar a 'estado inicial').

    Auto-cleanup: si hay más de AUTO_RETENTION_PER_KEY auto-snapshots para
    esa key EN ESA EMPRESA, borra los más viejos. Los manuales no se cuentan
    ni se borran."""
    current_value = await db.scalar(
        text("SELECT value FROM app_config WHERE company_id = :c AND key = :k"),
        {"c": company_id, "k": config_key},
    )
    await db.execute(
        text(
            """
            INSERT INTO app_config_history (company_id, config_key, value, label, source, is_manual)
            VALUES (:c, :k, :v, :l, :s, :m)
            """
        ),
        {
            "c": company_id,
            "k": config_key,
            "v": current_value,
            "l": label,
            "s": source,
            "m": is_manual,
        },
    )
    # Cleanup: dejar solo los últimos AUTO_RETENTION_PER_KEY auto-snapshots por key EN ESTA EMPRESA.
    if not is_manual:
        await db.execute(
            text(
                """
                DELETE FROM app_config_history
                WHERE id IN (
                    SELECT id FROM app_config_history
                    WHERE company_id = :c AND config_key = :k AND is_manual = FALSE
                    ORDER BY changed_at DESC
                    OFFSET :n
                )
                """
            ),
            {"c": company_id, "k": config_key, "n": AUTO_RETENTION_PER_KEY},
        )


def _parse_names(value: str | None) -> list[str]:
    """Parsea el valor JSON guardado a lista de nombres. Tolera vacío/legado/basura."""
    if not value:
        return []
    try:
        arr = json.loads(value)
        if isinstance(arr, list):
            return [str(x).strip() for x in arr if str(x).strip()]
    except (ValueError, TypeError):
        pass
    return []  # legado (ej. "21,12") o corrupto → sin exclusiones


async def resolve_department_ids(db: AsyncSession, company_id: int, names: list[str]) -> list[int]:
    """Convierte nombres de departamento a sus IDs ACTUALES (robusto a re-seeds)."""
    if not names:
        return []
    return list(
        (await db.execute(
            text("SELECT id FROM departments WHERE company_id = :c AND name = ANY(:n)"),
            {"c": company_id, "n": names},
        )).scalars().all()
    )


async def resolve_category_ids(db: AsyncSession, company_id: int, names: list[str]) -> list[int]:
    if not names:
        return []
    return list(
        (await db.execute(
            text("SELECT id FROM categories WHERE company_id = :c AND name = ANY(:n)"),
            {"c": company_id, "n": names},
        )).scalars().all()
    )


async def _read_cfg_names(db: AsyncSession, company_id: int, key: str) -> list[str]:
    val = await db.scalar(
        text("SELECT value FROM app_config WHERE company_id = :c AND key = :k"),
        {"c": company_id, "k": key},
    )
    return _parse_names(val)


async def get_exclusions(db: AsyncSession, company_id: int) -> dict:
    """Exclusiones vigentes resueltas a IDs ACTUALES (para parametrizar las matrices).

    Returns: {"departments": [int], "categories": [int], "department_names": [...], ...}
    """
    dep_names = await _read_cfg_names(db, company_id, "excluded_departments")
    cat_names = await _read_cfg_names(db, company_id, "excluded_categories")
    return {
        "departments": await resolve_department_ids(db, company_id, dep_names),
        "categories": await resolve_category_ids(db, company_id, cat_names),
        "department_names": dep_names,
        "category_names": cat_names,
    }


async def get_seasonal(db: AsyncSession, company_id: int) -> list[int]:
    """IDs ACTUALES de los departamentos estacionales (config en app_config, por nombre)."""
    names = await _read_cfg_names(db, company_id, "seasonal_departments")
    return await resolve_department_ids(db, company_id, names)


# ──────────────────────────────────────────────────────────────────────────────
# Metas de venta (KPI "Venta acumulada vs meta"). Manuales: gerencia las carga.
#
# Se guardan como JSON en app_config bajo la clave 'sales_goals', keyed por mes
# ("YYYY-MM") para conservar historia y permitir cargar el mes siguiente con
# anticipación. Montos en S/ (moneda de la venta). Estructura:
#   {"2026-06": {"global": 500000, "1": 300000, "3": 200000}, ...}
# Las claves numéricas son bsale_office_id; "global" es la meta de toda la empresa.
# A diferencia de las exclusiones (taxonomía), las oficinas tienen IDs estables,
# así que aquí guardamos por ID directamente (no por nombre).
# ──────────────────────────────────────────────────────────────────────────────

GOALS_KEY = "sales_goals"


async def get_goals(db: AsyncSession, company_id: int) -> dict:
    """Todas las metas configuradas: {"YYYY-MM": {"global": x, "<office_id>": y}}.

    Devuelve {} si no hay nada cargado o el valor está corrupto (degradación segura)."""
    val = await db.scalar(
        text("SELECT value FROM app_config WHERE company_id = :c AND key = :k"),
        {"c": company_id, "k": GOALS_KEY},
    )
    if not val:
        return {}
    try:
        data = json.loads(val)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def goal_for_month(goals: dict, month: str) -> tuple[dict, str]:
    """Meta vigente para `month` ("YYYY-MM"). Si ese mes no tiene meta cargada,
    hereda la del último mes configurado <= month (fallback).

    Returns: (dict_meta, fuente) donde fuente ∈ {"exacta", "heredada:<mes>", "no_configurada"}.
    """
    if month in goals and goals[month]:
        return goals[month], "exacta"
    past = sorted(m for m in goals if m <= month and goals[m])
    if past:
        return goals[past[-1]], f"heredada:{past[-1]}"
    return {}, "no_configurada"


async def set_goals_month(db: AsyncSession, company_id: int, month: str, month_goals: dict) -> dict:
    """Reemplaza (upsert) la meta de UN mes y persiste todo el JSON. Devuelve el dict completo."""
    goals = await get_goals(db, company_id)
    goals[month] = month_goals
    await _snapshot_before_change(db, company_id, GOALS_KEY, source=f"PUT goals month={month}")
    await db.execute(
        text(
            "INSERT INTO app_config (company_id, key, value, updated_at) "
            "VALUES (:c, :k, :v, NOW()) "
            "ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
        ),
        {"c": company_id, "k": GOALS_KEY, "v": json.dumps(goals, ensure_ascii=False)},
    )
    await db.commit()
    return goals


class ExclusionsBody(BaseModel):
    excluded_departments: list[int]  # IDs actuales (la UI trabaja con IDs)
    excluded_categories: list[int]
    seasonal_departments: list[int] = []  # departamentos de campaña (estacionales)


async def _dept_names(db: AsyncSession, company_id: int, ids: list[int]) -> list[str]:
    if not ids:
        return []
    return list(
        (await db.execute(
            text("SELECT name FROM departments WHERE company_id = :c AND id = ANY(:ids)"),
            {"c": company_id, "ids": ids},
        )).scalars().all()
    )


@router.get("/exclusions")
async def read_exclusions(
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Exclusiones + estacionales actuales (IDs resueltos) + todos los departamentos con sus flags."""
    cid = company.company_id
    excl = await get_exclusions(db, cid)
    seasonal_ids = await get_seasonal(db, cid)
    dep_excluded = set(excl["departments"])
    dep_seasonal = set(seasonal_ids)
    depts = (
        await db.execute(
            text("SELECT id, name FROM departments WHERE company_id = :c ORDER BY name"),
            {"c": cid},
        )
    ).mappings().all()
    return {
        "excluded_departments": excl["departments"],
        "excluded_categories": excl["categories"],
        "excluded_department_names": excl["department_names"],
        "seasonal_departments": seasonal_ids,
        "departments": [
            {
                "id": d["id"],
                "name": d["name"],
                "excluded": d["id"] in dep_excluded,
                "seasonal": d["id"] in dep_seasonal,
            }
            for d in depts
        ],
    }


@router.put("/exclusions")
async def write_exclusions(
    body: ExclusionsBody,
    company: CurrentCompany = Depends(require_admin),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Reemplaza exclusiones y estacionales. La UI envía IDs actuales; se convierten a
    NOMBRES para guardar (robusto a re-seeds). Efecto inmediato y global en las matrices."""
    cid = company.company_id
    excl_dep = await _dept_names(db, cid, body.excluded_departments)
    seasonal_dep = await _dept_names(db, cid, body.seasonal_departments)
    cat_names: list[str] = []
    if body.excluded_categories:
        cat_names = list(
            (await db.execute(
                text("SELECT name FROM categories WHERE company_id = :c AND id = ANY(:ids)"),
                {"c": cid, "ids": body.excluded_categories},
            )).scalars().all()
        )
    for key, names in [
        ("excluded_departments", excl_dep),
        ("excluded_categories", cat_names),
        ("seasonal_departments", seasonal_dep),
    ]:
        await _snapshot_before_change(db, cid, key, source="PUT /config/exclusions")
        await db.execute(
            text(
                "INSERT INTO app_config (company_id, key, value, updated_at) "
                "VALUES (:c, :k, :v, NOW()) "
                "ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
            ),
            {"c": cid, "k": key, "v": json.dumps(names, ensure_ascii=False)},
        )
    await log_event(
        db, company_id=cid, event_type="config.updated", actor_user_id=user.id,
        payload={
            "que": "exclusions",
            "excluded_departments": excl_dep,
            "excluded_categories": cat_names,
            "seasonal_departments": seasonal_dep,
        },
        commit=False,
    )
    await db.commit()
    matrix_cache.invalidate(company_id=cid)
    return {
        "ok": True,
        "operation": "set_config",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "excluded_department_names": excl_dep,
        "excluded_category_names": cat_names,
        "seasonal_department_names": seasonal_dep,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Umbrales de clasificación (ventanas, tendencia, XYZ, velocidad, cobertura, …)
#
# Mismo patrón que `sales_goals`: JSON en `app_config` bajo la key `thresholds`.
# Si la fila no existe (instalación nueva), `GET` devuelve los defaults de
# `config_defaults.DEFAULT_THRESHOLDS` sin escribir nada — la DB queda como
# fuente de verdad recién al primer `PUT`.
#
# IMPORTANTE: hoy estos valores se guardan y se devuelven, pero los SQL de las
# matrices todavía tienen los números hardcoded como literales. Editar desde la
# UI persiste el cambio pero NO afecta a los reportes hasta que se reemplacen
# los literales en los SQL por `:params` (cambio que requiere tests de regresión).
# ──────────────────────────────────────────────────────────────────────────────

THRESHOLDS_KEY = "thresholds"


async def get_thresholds(db: AsyncSession, company_id: int) -> dict:
    """Lee los umbrales de `app_config.thresholds` para una empresa.

    Devuelve los defaults Python si no existe la fila o está corrupta. Hace
    *merge* sobre los defaults para que si en el futuro se agrega un umbral
    nuevo en código, los despliegues viejos lo reciban automáticamente sin
    requerir migración. La DB manda sobre los defaults clave a clave.
    """
    val = await db.scalar(
        text("SELECT value FROM app_config WHERE company_id = :c AND key = :k"),
        {"c": company_id, "k": THRESHOLDS_KEY},
    )
    merged = dict(DEFAULT_THRESHOLDS)
    if val:
        try:
            data = json.loads(val)
            if isinstance(data, dict):
                for k, v in data.items():
                    if k in DEFAULT_THRESHOLDS and isinstance(v, (int, float)):
                        merged[k] = v
        except (ValueError, TypeError):
            pass
    return merged


class ThresholdsBody(BaseModel):
    """Acepta cualquier subset de keys; las omitidas mantienen su valor previo."""

    thresholds: dict[str, float]


@router.get("/thresholds")
async def read_thresholds(
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Devuelve los umbrales vigentes + la metadata de secciones para la UI."""
    thresholds = await get_thresholds(db, company.company_id)
    return {
        "thresholds": thresholds,
        "defaults": DEFAULT_THRESHOLDS,
        "sections": THRESHOLD_SECTIONS,
    }


@router.put("/thresholds")
async def write_thresholds(
    body: ThresholdsBody,
    company: CurrentCompany = Depends(require_admin),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Persiste umbrales (merge sobre los actuales).

    Solo se aceptan keys conocidas (las de `DEFAULT_THRESHOLDS`). Valores
    desconocidos se ignoran silenciosamente — degradación segura ante un
    frontend desactualizado.
    """
    cid = company.company_id
    current = await get_thresholds(db, cid)
    for k, v in body.thresholds.items():
        if k in DEFAULT_THRESHOLDS and isinstance(v, (int, float)):
            current[k] = v
    await _snapshot_before_change(db, cid, THRESHOLDS_KEY, source="PUT /config/thresholds")
    await db.execute(
        text(
            "INSERT INTO app_config (company_id, key, value, updated_at) "
            "VALUES (:c, :k, :v, NOW()) "
            "ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
        ),
        {"c": cid, "k": THRESHOLDS_KEY, "v": json.dumps(current)},
    )
    await log_event(
        db, company_id=cid, event_type="config.updated", actor_user_id=user.id,
        payload={"que": "thresholds", "changed": list(body.thresholds.keys())},
        commit=False,
    )
    await db.commit()
    return {
        "ok": True,
        "operation": "set_thresholds",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "thresholds": current,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Configuración por EMPRESA (marca + IDs operativos BSale).
#
# Mismo patrón que thresholds. `GET /config/company` devuelve, en una sola
# request: los valores actuales (DB con fallback a defaults Python), los
# defaults, la metadata de secciones para el form, y el CATÁLOGO de BSale
# (offices / document_types / users / categories) que la UI usa para mostrar
# multi-selects con nombres reales en vez de IDs crudos.
#
# IMPORTANTE: hoy `_get_query_params()` lee los IDs operativos del .env (via
# `harvester/config.py`). Editar desde la UI persiste en la DB pero los SQL
# todavía usan los valores del .env hasta que se conecte el runtime. Para una
# empresa nueva: sirve para CONFIGURAR (la próxima vez que se conecte va a usar
# los valores guardados), pero por ahora también hay que mantener el .env en
# sincronía con lo guardado en la UI.
# ──────────────────────────────────────────────────────────────────────────────

COMPANY_KEY = "company"


async def get_company(db: AsyncSession, company_id: int) -> dict:
    """Lee la configuración de empresa de `app_config.company` (por company_id).

    Devuelve los defaults Python si no existe la fila o está corrupta. Hace
    *merge* sobre los defaults clave-a-clave para que si en el futuro se
    agrega un campo nuevo en código, los despliegues viejos lo reciban
    automáticamente sin requerir migración."""
    val = await db.scalar(
        text("SELECT value FROM app_config WHERE company_id = :c AND key = :k"),
        {"c": company_id, "k": COMPANY_KEY},
    )
    merged = dict(DEFAULT_COMPANY)
    if val:
        try:
            data = json.loads(val)
            if isinstance(data, dict):
                for k in DEFAULT_COMPANY:
                    if k in data:
                        merged[k] = data[k]
        except (ValueError, TypeError):
            pass
    return merged


async def _load_catalogs(db: AsyncSession, company_id: int) -> dict:
    """Datos para los multi-selects de la UI: sucursales, tipos de doc, usuarios, categorías.
    Vienen del catálogo ya sincronizado desde BSale; el sync es prerequisito."""
    offices = (
        await db.execute(
            text(
                "SELECT bsale_office_id AS id, name, is_active "
                "FROM offices WHERE company_id = :c ORDER BY name"
            ),
            {"c": company_id},
        )
    ).mappings().all()
    document_types = (
        await db.execute(
            text(
                "SELECT bsale_document_type_id AS id, name, code, "
                "is_credit_note, is_sales_note "
                "FROM document_types WHERE company_id = :c AND is_active ORDER BY name"
            ),
            {"c": company_id},
        )
    ).mappings().all()
    users = (
        await db.execute(
            text(
                "SELECT bsale_user_id AS id, "
                "TRIM(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')) AS name, "
                "email, is_active "
                "FROM users WHERE company_id = :c ORDER BY first_name, last_name"
            ),
            {"c": company_id},
        )
    ).mappings().all()
    categories = (
        await db.execute(
            text(
                "SELECT c.id, c.name, d.name AS department_name "
                "FROM categories c "
                "JOIN departments d ON d.id = c.department_id AND d.company_id = c.company_id "
                "WHERE c.company_id = :c "
                "ORDER BY d.name, c.name"
            ),
            {"c": company_id},
        )
    ).mappings().all()
    return {
        "offices": [dict(r) for r in offices],
        "document_types": [dict(r) for r in document_types],
        "users": [dict(r) for r in users],
        "categories": [dict(r) for r in categories],
    }


class CompanyBody(BaseModel):
    """Acepta cualquier subset de keys; las omitidas mantienen su valor previo."""

    company: dict


@router.get("/company")
async def read_company(
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Devuelve config de empresa + defaults + metadata + catálogos BSale."""
    cid = company.company_id
    company_cfg = await get_company(db, cid)
    catalogs = await _load_catalogs(db, cid)
    return {
        "company": company_cfg,
        "defaults": DEFAULT_COMPANY,
        "sections": COMPANY_SECTIONS,
        "catalogs": catalogs,
    }


@router.put("/company")
async def write_company(
    body: CompanyBody,
    company: CurrentCompany = Depends(require_admin),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Persiste la configuración de empresa (merge sobre lo actual).

    Solo acepta keys conocidas (`DEFAULT_COMPANY`). Valida tipos básicos:
    strings para marca, listas de int para IDs, int|None para almacén central.
    Valores con tipo incorrecto se descartan silenciosamente (degradación segura)."""
    cid = company.company_id
    current = await get_company(db, cid)

    def _as_int_list(v):
        if not isinstance(v, list):
            return None
        out = []
        for x in v:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                return None
        return out

    for k, v in body.company.items():
        if k not in DEFAULT_COMPANY:
            continue
        if k in ("brand_name", "classification_label"):
            if isinstance(v, str):
                current[k] = v.strip()
        elif k == "office_almacen":
            if v is None:
                current[k] = None
            else:
                try:
                    current[k] = int(v)
                except (TypeError, ValueError):
                    pass
        else:  # listas de IDs
            parsed = _as_int_list(v)
            if parsed is not None:
                current[k] = parsed

    await _snapshot_before_change(db, cid, COMPANY_KEY, source="PUT /config/company")
    await db.execute(
        text(
            "INSERT INTO app_config (company_id, key, value, updated_at) "
            "VALUES (:c, :k, :v, NOW()) "
            "ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
        ),
        {"c": cid, "k": COMPANY_KEY, "v": json.dumps(current)},
    )
    await log_event(
        db, company_id=cid, event_type="config.updated", actor_user_id=user.id,
        payload={"que": "company", "changed": list(body.company.keys())},
        commit=False,
    )
    await db.commit()
    return {
        "ok": True,
        "operation": "set_company",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "company": current,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Recomendaciones: analiza la DB sincronizada para sugerir IDs operativos.
#
# Útil al replicar para una empresa nueva: en vez de que el usuario tipee IDs
# mirando BSale, el sistema infiere desde los datos ya sincronizados qué
# parece ser venta vs devolución vs traslado, qué oficina es la tienda vs el
# almacén, qué usuarios son almaceneros (por volumen de recepciones), etc.
#
# La UI muestra las sugerencias en el form pero NO las guarda automáticamente
# — el usuario las revisa, ajusta y aprieta "Guardar" cuando está conforme.
# ──────────────────────────────────────────────────────────────────────────────


async def _suggest_company(db: AsyncSession, company_id: int) -> dict:
    """Devuelve sugerencias para cada campo de DEFAULT_COMPANY basadas en los
    datos sincronizados desde BSale.

    Reglas:
    - Tipos VENTA: document_types activos no marcados como nota de crédito,
      cuyo nombre NO contiene "TRASLADO/DESPACHO/TRANSFER", y que tienen
      documentos reales en la base (≥ 50 docs).
    - Tipos DEVOLUCIÓN: document_types con `is_credit_note=true`.
    - Tipos TRASLADO: document_types cuyo nombre contiene "TRASLADO/DESPACHO".
    - Sucursales TIENDA: oficinas activas no-virtuales que tienen ≥ 100
      documentos asociados (sucursales que VENDEN al público).
    - Almacén CENTRAL: oficina activa cuyo nombre contiene "ALMAC/CENTRAL/
      BODEGA"; si no se encuentra, la oficina que tiene recepciones pero
      casi sin ventas.
    - Usuarios ALMACENEROS: usuarios con ≥ 30 recepciones (los que
      efectivamente reciben mercadería).
    - Categorías OBJETIVO: las 3 categorías con más unidades vendidas en
      los últimos 90 días."""

    rec: dict = {}

    # --- Marca: mantener defaults (no se infiere desde datos) ---
    rec["brand_name"] = DEFAULT_COMPANY["brand_name"]
    rec["classification_label"] = DEFAULT_COMPANY["classification_label"]

    # --- Tipos de documento (subquery correlacionada) ---
    doc_types = (
        await db.execute(
            text(
                """
                SELECT dt.bsale_document_type_id AS id,
                       dt.name,
                       dt.is_credit_note,
                       (SELECT COUNT(*) FROM documents
                        WHERE company_id = :cid
                          AND bsale_document_type_id = dt.bsale_document_type_id) AS doc_count
                FROM document_types dt
                WHERE dt.company_id = :cid
                  AND dt.is_active
                """
            ),
            {"cid": company_id},
        )
    ).mappings().all()

    venta_ids: list[int] = []
    devolucion_ids: list[int] = []
    traslado_ids: list[int] = []
    for dt in doc_types:
        name_upper = (dt["name"] or "").upper()
        is_transfer = any(k in name_upper for k in ("TRASLADO", "DESPACHO", "TRANSFER"))
        if is_transfer:
            traslado_ids.append(dt["id"])
        elif dt["is_credit_note"]:
            devolucion_ids.append(dt["id"])
        elif dt["doc_count"] and dt["doc_count"] >= 50:
            venta_ids.append(dt["id"])
    rec["tipos_venta"] = sorted(venta_ids)
    rec["tipos_devolucion"] = sorted(devolucion_ids)
    rec["tipos_traslado"] = sorted(traslado_ids)

    # --- Sucursales (con subqueries correlacionadas para evitar cross-product) ---
    offices = (
        await db.execute(
            text(
                """
                SELECT o.bsale_office_id AS id,
                       o.name,
                       o.is_virtual,
                       (SELECT COUNT(*) FROM documents
                        WHERE company_id = :cid
                          AND bsale_office_id = o.bsale_office_id) AS docs,
                       (SELECT COUNT(*) FROM receptions
                        WHERE company_id = :cid
                          AND bsale_office_id = o.bsale_office_id) AS receps
                FROM offices o
                WHERE o.company_id = :cid
                  AND o.is_active
                """
            ),
            {"cid": company_id},
        )
    ).mappings().all()

    tienda_ids: list[int] = []
    almacen_id: int | None = None
    # Pasada 1: oficinas con ≥ 100 documentos = tienda. Marca de "almacén" por
    # nombre tiene precedencia sobre el ranking de ventas.
    for o in offices:
        name_upper = (o["name"] or "").upper()
        is_almacen_name = any(k in name_upper for k in ("ALMAC", "CENTRAL", "BODEGA", "DEPOSITO"))
        if is_almacen_name and almacen_id is None:
            almacen_id = o["id"]
            continue
        if not o["is_virtual"] and (o["docs"] or 0) >= 100:
            tienda_ids.append(o["id"])
    # Pasada 2 (fallback): si no hubo "almacén" por nombre, la oficina con
    # más recepciones pero menos ventas (proporcionalmente).
    if almacen_id is None:
        candidates = [
            o for o in offices
            if o["id"] not in tienda_ids
            and (o["receps"] or 0) > 0
            and (o["docs"] or 0) < (o["receps"] or 0)
        ]
        candidates.sort(key=lambda o: -(o["receps"] or 0))
        if candidates:
            almacen_id = candidates[0]["id"]

    rec["offices_tienda"] = sorted(tienda_ids)
    rec["office_almacen"] = almacen_id

    # --- Usuarios almaceneros (subquery correlacionada, evita cross-product) ---
    warehouse_users = (
        await db.execute(
            text(
                """
                SELECT u.bsale_user_id AS id,
                       (SELECT COUNT(*) FROM receptions
                        WHERE company_id = :cid AND bsale_user_id = u.bsale_user_id) AS receps
                FROM users u
                WHERE u.company_id = :cid
                  AND u.is_active
                  AND (SELECT COUNT(*) FROM receptions
                       WHERE company_id = :cid AND bsale_user_id = u.bsale_user_id) >= 30
                ORDER BY 2 DESC
                """
            ),
            {"cid": company_id},
        )
    ).mappings().all()
    rec["bsale_warehouse_user_ids"] = sorted([u["id"] for u in warehouse_users])

    # --- Categorías objetivo: top 3 por unidades vendidas en los últimos
    # 365 días. Path de taxonomía: product → product_type → subcategory →
    # category (los productos no tienen subcategory_id directo). ---
    top_cats = (
        await db.execute(
            text(
                """
                SELECT c.id, SUM(dd.quantity) AS unidades
                FROM document_details dd
                JOIN documents doc     ON doc.bsale_document_id      = dd.bsale_document_id     AND doc.company_id = dd.company_id
                JOIN variants v        ON v.bsale_variant_id         = dd.bsale_variant_id      AND v.company_id   = dd.company_id
                JOIN products p        ON p.bsale_product_id         = v.bsale_product_id       AND p.company_id   = v.company_id
                JOIN product_types pt  ON pt.bsale_product_type_id   = p.bsale_product_type_id  AND pt.company_id  = p.company_id
                JOIN subcategories sc  ON sc.id                      = pt.subcategory_id        AND sc.company_id  = pt.company_id
                JOIN categories c      ON c.id                       = sc.category_id           AND c.company_id   = sc.company_id
                WHERE dd.company_id = :cid
                  AND doc.emission_date >= NOW() - INTERVAL '365 days'
                  AND doc.is_active
                GROUP BY c.id
                ORDER BY 2 DESC
                LIMIT 3
                """
            ),
            {"cid": company_id},
        )
    ).mappings().all()
    rec["target_categories"] = sorted([c["id"] for c in top_cats])

    return rec


@router.get("/company/recommendations")
async def read_company_recommendations(
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Sugiere los IDs operativos analizando la DB ya sincronizada.

    La UI los muestra como pre-selección — el usuario revisa, ajusta y
    guarda con el botón normal. Nada se persiste desde este endpoint."""
    rec = await _suggest_company(db, company.company_id)
    return {
        "recommendations": rec,
        "notes": [
            "Tipos de venta: docs activos no-NC con ≥ 50 documentos y nombre que no contiene TRASLADO/DESPACHO.",
            "Tipos de devolución: docs marcados is_credit_note=true.",
            "Tipos de traslado: docs cuyo nombre contiene TRASLADO/DESPACHO/TRANSFER.",
            "Sucursales tienda: oficinas activas no-virtuales con ≥ 100 documentos.",
            "Almacén central: oficina cuyo nombre contiene ALMAC/CENTRAL/BODEGA/DEPOSITO (fallback: oficina con muchas recepciones y pocas ventas).",
            "Almaceneros: usuarios con ≥ 30 recepciones.",
            "Categorías objetivo: top 3 por unidades vendidas en los últimos 90 días.",
        ],
    }


# ──────────────────────────────────────────────────────────────────────────────
# Respaldos: listado, snapshot manual, restore, export, import.
# Pensados como "deshacer" para cuando alguien manipula mal la configuración.
#
# Cada PUT a /config/* ya guarda un snapshot automático del valor ANTERIOR en
# `app_config_history` (via `_snapshot_before_change`). Estos endpoints
# permiten al usuario verlos, marcar puntos importantes con label manual, y
# revertir cualquier key a un punto previo. El export/import sirve como
# respaldo afuera de la DB (descarga JSON).
# ──────────────────────────────────────────────────────────────────────────────


class ManualSnapshotBody(BaseModel):
    """Snapshot manual con etiqueta legible (ej. 'antes de campaña Q4')."""

    label: str
    keys: list[str] | None = None  # None = todas las BACKUP_KEYS


class ImportBody(BaseModel):
    """JSON exportado previamente. `config` es {key: parsed_value, ...}."""

    config: dict[str, Any]
    label: str | None = None  # se aplica al snapshot pre-import


@router.get("/backups")
async def list_backups(
    config_key: str | None = Query(
        None, description="Filtrar por key. Si se omite, devuelve todas."
    ),
    limit: int = Query(200, ge=1, le=1000),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Lista los snapshots de configuración de la empresa activa, más recientes primero."""
    params: dict = {"lim": limit, "cid": company.company_id}
    where = "WHERE company_id = :cid"
    if config_key:
        where += " AND config_key = :k"
        params["k"] = config_key
    rows = (
        await db.execute(
            text(
                f"""
                SELECT id, config_key, value, changed_at, label, source, is_manual
                FROM app_config_history
                {where}
                ORDER BY changed_at DESC
                LIMIT :lim
                """
            ),
            params,
        )
    ).mappings().all()

    def _short_preview(v: str | None) -> str | None:
        if v is None:
            return None
        s = str(v)
        return s if len(s) <= 120 else s[:117] + "..."

    return {
        "total": len(rows),
        "backups": [
            {
                "id": r["id"],
                "config_key": r["config_key"],
                "value_preview": _short_preview(r["value"]),
                "has_value": r["value"] is not None,
                "changed_at": r["changed_at"].isoformat(),
                "label": r["label"],
                "source": r["source"],
                "is_manual": r["is_manual"],
            }
            for r in rows
        ],
    }


@router.post("/backups")
async def create_manual_snapshot(
    body: ManualSnapshotBody,
    company: CurrentCompany = Depends(require_admin),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Snapshot manual con etiqueta. Por defecto incluye TODAS las
    `BACKUP_KEYS`; opcionalmente solo las que se pasen en `keys`."""
    if not body.label.strip():
        raise HTTPException(status_code=400, detail="`label` no puede estar vacío")
    keys = body.keys or list(BACKUP_KEYS)
    invalid = [k for k in keys if k not in BACKUP_KEYS]
    if invalid:
        raise HTTPException(
            status_code=400, detail=f"Keys inválidas: {invalid}. Válidas: {list(BACKUP_KEYS)}"
        )
    cid = company.company_id
    for k in keys:
        await _snapshot_before_change(
            db, cid, k, source="manual snapshot", label=body.label.strip(), is_manual=True
        )
    await log_event(
        db, company_id=cid, event_type="config.updated", actor_user_id=user.id,
        payload={"que": "backup_manual", "label": body.label.strip(), "keys": keys},
        commit=False,
    )
    await db.commit()
    return {
        "ok": True,
        "operation": "manual_snapshot",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "label": body.label.strip(),
        "keys": keys,
    }


@router.post("/backups/{backup_id}/restore")
async def restore_backup(
    backup_id: int,
    company: CurrentCompany = Depends(require_admin),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Restaura `app_config[config_key]` al valor que tenía esa fila de
    historial. Antes de pisar, hace UN snapshot más (manual con label
    "antes de restore #N") para que el restore también sea reversible.

    El backup DEBE pertenecer a la empresa activa (evita cross-tenant restore)."""
    cid = company.company_id
    row = await db.execute(
        text(
            "SELECT config_key, value, changed_at, label "
            "FROM app_config_history WHERE id = :id AND company_id = :cid"
        ),
        {"id": backup_id, "cid": cid},
    )
    backup = row.mappings().first()
    if not backup:
        raise HTTPException(status_code=404, detail=f"Backup {backup_id} no existe")

    key = backup["config_key"]
    target_value = backup["value"]

    await _snapshot_before_change(
        db,
        cid,
        key,
        source=f"POST /config/backups/{backup_id}/restore",
        label=f"antes de restore #{backup_id}",
        is_manual=True,
    )

    if target_value is None:
        # El backup capturaba el momento ANTES de la primera vez que
        # apareció esa key: significa que la key no existía. Para
        # restaurar a "no existía", borramos la fila de app_config.
        await db.execute(
            text("DELETE FROM app_config WHERE company_id = :c AND key = :k"),
            {"c": cid, "k": key},
        )
    else:
        await db.execute(
            text(
                "INSERT INTO app_config (company_id, key, value, updated_at) "
                "VALUES (:c, :k, :v, NOW()) "
                "ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
            ),
            {"c": cid, "k": key, "v": target_value},
        )
    await log_event(
        db, company_id=cid, event_type="config.updated", actor_user_id=user.id,
        payload={"que": "restore", "backup_id": backup_id, "config_key": key},
        commit=False,
    )
    await db.commit()
    return {
        "ok": True,
        "operation": "restore",
        "backup_id": backup_id,
        "config_key": key,
        "restored_to": backup["changed_at"].isoformat(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.delete("/backups/{backup_id}")
async def delete_backup(
    backup_id: int,
    company: CurrentCompany = Depends(require_admin),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Borra un snapshot histórico específico de la empresa activa. Útil para
    limpiar manuales que ya no se necesitan."""
    result = await db.execute(
        text("DELETE FROM app_config_history WHERE id = :id AND company_id = :cid RETURNING id"),
        {"id": backup_id, "cid": company.company_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Backup {backup_id} no existe")
    await log_event(
        db, company_id=company.company_id, event_type="config.updated",
        actor_user_id=user.id,
        payload={"que": "delete_backup", "backup_id": row["id"]},
        commit=False,
    )
    await db.commit()
    return {"ok": True, "deleted_id": row["id"]}


@router.get("/export")
async def export_config(
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Descarga el estado actual de `app_config` para todas las
    `BACKUP_KEYS` de la empresa activa. El JSON resultante se puede subir
    vía `POST /config/import` en otro despliegue para clonar la configuración,
    o usarse como respaldo fuera de la DB."""
    rows = (
        await db.execute(
            text(
                "SELECT key, value FROM app_config WHERE company_id = :c AND key = ANY(:keys)"
            ),
            {"c": company.company_id, "keys": list(BACKUP_KEYS)},
        )
    ).mappings().all()
    config: dict[str, Any] = {}
    for r in rows:
        try:
            config[r["key"]] = json.loads(r["value"]) if r["value"] else None
        except (ValueError, TypeError):
            config[r["key"]] = None
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "keys": list(BACKUP_KEYS),
        "config": config,
    }


@router.post("/import")
async def import_config(
    body: ImportBody,
    company: CurrentCompany = Depends(require_admin),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Aplica un JSON exportado previamente sobre la empresa activa. Antes de
    pisar cada key se hace UN snapshot manual con label "antes de import" (o
    el `label` dado), para que la operación sea reversible. Keys desconocidas
    se ignoran."""
    snapshot_label = (body.label or "").strip() or "antes de import"
    applied: list[str] = []
    ignored: list[str] = []
    cid = company.company_id
    for key, parsed in body.config.items():
        if key not in BACKUP_KEYS:
            ignored.append(key)
            continue
        await _snapshot_before_change(
            db, cid, key, source="POST /config/import", label=snapshot_label, is_manual=True
        )
        if parsed is None:
            await db.execute(
                text("DELETE FROM app_config WHERE company_id = :c AND key = :k"),
                {"c": cid, "k": key},
            )
        else:
            await db.execute(
                text(
                    "INSERT INTO app_config (company_id, key, value, updated_at) "
                    "VALUES (:c, :k, :v, NOW()) "
                    "ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
                ),
                {"c": cid, "k": key, "v": json.dumps(parsed, ensure_ascii=False)},
            )
        applied.append(key)
    await log_event(
        db, company_id=cid, event_type="config.updated", actor_user_id=user.id,
        payload={"que": "import", "applied": applied, "ignored": ignored, "label": snapshot_label},
        commit=False,
    )
    await db.commit()
    return {
        "ok": True,
        "operation": "import",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "applied": applied,
        "ignored": ignored,
        "snapshot_label": snapshot_label,
    }
