"""
Aplica los overrides de runtime desde `app_config` sobre los `:params` de las matrices.

Conecta los endpoints `/config/thresholds` y `/config/company` al runtime real
de las matrices. Antes de este módulo, lo que se guardaba en esos endpoints se
persistía pero no afectaba a las queries — las matrices seguían leyendo todo
del `.env` al boot.

Semántica:
- Lo que esté en `app_config` MANDA sobre el valor que venía del `.env`.
- Si una key NO está en `app_config` (o el valor es vacío/inválido), se
  conserva el del `.env`. Esto evita que una empresa recién instalada (sin
  config en DB) pierda los IDs del `.env` y quede con matrices vacías.

A diferencia de `get_thresholds()` / `get_company()` de `config_admin.py`, las
lecturas acá NO hacen merge con los defaults Python: solo lo realmente
persistido cuenta como override. Es la diferencia clave entre "leer para la UI"
y "leer para el SQL".

Degradación segura: si la DB falla, la excepción se propaga al `try/except`
del caller en `service.py`, que la silencia. La sesión queda en el mismo
estado en que estaba antes del override.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


# ──────────────────────────────────────────────────────────────────────────────
# Mapa: clave en app_config.company → nombre del :param en el SQL de la matriz.
# Solo incluye los que se usan en los SQL hoy (`sucursales_objetivo`, `tipos_*`
# y `warehouse_user_ids`). `office_almacen` y `target_categories` no entran
# aquí porque las matrices no los toman como :param.
# ──────────────────────────────────────────────────────────────────────────────
_COMPANY_TO_PARAMS: dict[str, str] = {
    "offices_tienda": "sucursales_objetivo",
    "tipos_venta": "tipos_venta",
    "tipos_devolucion": "tipos_devolucion",
    "tipos_traslado": "tipos_traslado",
    "bsale_warehouse_user_ids": "warehouse_user_ids",
}


async def _read_raw(db: AsyncSession, company_id: int, key: str) -> dict[str, Any]:
    """Devuelve el JSON guardado en `app_config[company_id, key]` o `{}` si no hay fila.

    NO hace merge con defaults: solo refleja lo realmente persistido."""
    val = await db.scalar(
        text("SELECT value FROM app_config WHERE company_id = :c AND key = :k"),
        {"c": company_id, "k": key},
    )
    if not val:
        return {}
    try:
        data = json.loads(val)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


async def apply_db_overrides(db: AsyncSession, company_id: int, params: dict) -> None:
    """Aplica in-place a `params` los overrides desde `app_config` de UNA empresa.

    Args:
        db: sesión async (la misma que la matriz va a usar para la query).
        company_id: empresa cuya config leer.
        params: dict devuelto por `_get_query_params()` con los valores del `.env`.

    Reglas:
    - `app_config[cid, thresholds]`: cada key numérica que coincida con un
      :param de `params` lo reemplaza. Si no, se ignora.
    - `app_config[cid, company]`: cada lista de IDs no vacía se traduce al
      nombre del :param vía `_COMPANY_TO_PARAMS` y reemplaza.

    Si la fila no existe (o el valor es vacío/inválido), el `.env` se mantiene."""
    # --- Thresholds: numéricos, 1:1 con el nombre de :param en el SQL ---
    thresholds = await _read_raw(db, company_id, "thresholds")
    for k, v in thresholds.items():
        if k not in params:
            continue
        if isinstance(v, bool):  # bool es subclase de int — descartar explícito
            continue
        if isinstance(v, (int, float)):
            params[k] = v

    # --- Company: IDs operativos, traducidos al nombre de :param ---
    company = await _read_raw(db, company_id, "company")
    for company_key, param_key in _COMPANY_TO_PARAMS.items():
        v = company.get(company_key)
        if not isinstance(v, list) or not v:
            continue  # vacío o ausente: conservar el del .env
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in v):
            continue  # tipos inesperados: conservar el del .env
        params[param_key] = v

    # --- Precisión: convertir floats a Decimal antes de pasar al SQL ---
    # asyncpg pasa floats Python como float8 (double precision), perdiendo
    # precisión IEEE-754: 0.2 → 0.20000000000000001. Cuando el SQL hace
    # CAST(:x AS numeric), arrastra esa imprecisión y dispara diferencias
    # en SKUs cuyo cómputo cae JUSTO en el borde del threshold.
    # Decimal(str(0.2)) = Decimal('0.2') exacto; asyncpg lo pasa como NUMERIC.
    for k, v in list(params.items()):
        if isinstance(v, float) and not isinstance(v, bool):
            params[k] = Decimal(str(v))
