"""
Servicio que ejecuta los SQL de las matrices de clasificación y aplica filtros.

Los SQL se cargan al iniciar y se cachean en memoria.
Las consultas SQL se ejecutan con parámetros de entorno cargados dinámicamente
y los nombres de las columnas se mapean según la configuración de marca.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import get_settings
from app.kawii_matrix import cache as matrix_cache

# Ruta absoluta a la carpeta sql/
_SQL_DIR = Path(__file__).parent / "sql"

MATRIX_MAP = {
    "04":  "04_matriz_90d.sql",
    "04b": "04b_matriz_90d_jerarquico.sql",  # Matriz 90d + totales jerárquicos en S/
    "05":  "05_matriz_operativa.sql",
    "06":  "06_historico_productos.sql",
    "07":  "07_informe_consolidado.sql",
    "08":  "08_transferencias.sql",          # Sugerencias de transferencia inter-sucursal
}

# ★ Módulos COMPUESTOS: comparten las CTEs base + cascada de clasificación
#   (_matriz_90d_base.sql, termina en la CTE `matriz`) y su archivo propio es
#   solo el SELECT final (proyección de columnas + filtro fantasmas + ventanas
#   jerárquicas). Así un fix de lógica se aplica UNA vez en la base y las tres
#   matrices no pueden divergir.
_SHARED_BASE_FILE = "_matriz_90d_base.sql"
_COMPOSED_MODULES = {"04", "04b", "05"}


@lru_cache(maxsize=8)
def _load_sql(module_id: str) -> str:
    """Carga el SQL del módulo (cacheado en memoria).
    Para los módulos compuestos concatena base compartida + SELECT del módulo."""
    if module_id not in MATRIX_MAP:
        raise ValueError(f"Módulo desconocido: {module_id}. Opciones: {list(MATRIX_MAP)}")
    path = _SQL_DIR / MATRIX_MAP[module_id]
    if not path.exists():
        raise FileNotFoundError(f"SQL no encontrado: {path}")
    sql = path.read_text(encoding="utf-8")
    if module_id in _COMPOSED_MODULES:
        base = (_SQL_DIR / _SHARED_BASE_FILE).read_text(encoding="utf-8")
        sql = base + "\n" + sql
    return sql


def _get_query_params() -> dict:
    """Parámetros operacionales estáticos (de BSale, estables por empresa).
    Las exclusiones (DB) y el estacional (.env por nombre) se resuelven a IDs en
    `_execute_query_to_dicts` (porque los IDs de taxonomía cambian con los re-seeds)."""
    from harvester.config import (
        OFFICES_TIENDA,
        TIPOS_VENTA,
        TIPOS_DEVOLUCION,
        TIPOS_TRASLADO,
        BSALE_WAREHOUSE_USER_IDS,
        RECEPTION_QTY_SANITY_LIMIT,
        WINDOW_MAIN_DAYS,
        WINDOW_TREND_SPLIT_DAYS,
        WINDOW_RECENT_DAYS,
        WINDOW_BLIND_SPOT_DAYS,
        WINDOW_NEW_PRODUCT_DAYS,
        WINDOW_DEAD_DAYS,
        PISO_DIAS_LOTE,
        COBERTURA_OBJETIVO_DIAS,
        # Umbrales de clasificación (política comercial)
        SELLTHROUGH_EXITO_RATIO,
        LOSS_CONSUMO_RATIO,
        LOSS_VENTA_RATIO,
        TREND_GROW_MULT,
        TREND_DECAY_MULT,
        XYZ_CONSTANTE_PCT,
        XYZ_VARIABLE_PCT,
        PROY_MES_ALTA,
        PROY_MES_MIN_FLOOR,
        PROY_MES_MIN_CAP,
        PROY_MES_CAT_RATIO,
        COBERTURA_CRITICA_DIAS,
        COBERTURA_BAJA_DIAS,
        DIAS_ABSORCION_BESTSELLER_MAX,
        DSV_QUIEBRE_MAX_DIAS,
        LIFETIME_BESTSELLER_MIN,
        LOTE_FRENADO_STOCK_MIN,
        LOTE_FRENADO_PROY_MIN,
        LOTE_FRENADO_PROY30_MAX,
        LOTE_FRENADO_EDAD_MIN,
        LENTO_CRONICO_DSV_MIN,
        LENTO_CRONICO_LIFETIME_MAX,
        LENTO_CRONICO_PROY_MAX,
        RECIEN_REABASTECIDO_DIAS,
        TRANSFER_DONOR_STOCK_MIN,
    )
    settings = get_settings()
    return {
        "sucursales_objetivo": OFFICES_TIENDA,
        "tipos_venta": TIPOS_VENTA,
        "tipos_devolucion": TIPOS_DEVOLUCION,
        "tipos_traslado": TIPOS_TRASLADO,
        # Placeholders: se sobrescriben con IDs resueltos por nombre antes de la query.
        "excluded_departments": [],
        "excluded_categories": [],
        "seasonal_departments": [],
        "timezone": settings.TIMEZONE,
        # ★ Configuración por empresa (antes hardcodeada en SQL).
        "warehouse_user_ids": BSALE_WAREHOUSE_USER_IDS,
        "qty_sanity_limit": RECEPTION_QTY_SANITY_LIMIT,
        "ventana_main_dias": WINDOW_MAIN_DAYS,
        "ventana_trend_split_dias": WINDOW_TREND_SPLIT_DAYS,
        "ventana_recent_dias": WINDOW_RECENT_DAYS,
        "ventana_blind_spot_dias": WINDOW_BLIND_SPOT_DAYS,
        "ventana_new_product_dias": WINDOW_NEW_PRODUCT_DAYS,
        "ventana_dead_dias": WINDOW_DEAD_DAYS,
        "piso_dias_lote": PISO_DIAS_LOTE,
        "cobertura_objetivo_dias": COBERTURA_OBJETIVO_DIAS,
        # Umbrales de clasificación
        "sellthrough_exito_ratio": SELLTHROUGH_EXITO_RATIO,
        "loss_consumo_ratio": LOSS_CONSUMO_RATIO,
        "loss_venta_ratio": LOSS_VENTA_RATIO,
        "trend_grow_mult": TREND_GROW_MULT,
        "trend_decay_mult": TREND_DECAY_MULT,
        "xyz_constante_pct": XYZ_CONSTANTE_PCT,
        "xyz_variable_pct": XYZ_VARIABLE_PCT,
        "proy_mes_alta": PROY_MES_ALTA,
        "proy_mes_min_floor": PROY_MES_MIN_FLOOR,
        "proy_mes_min_cap": PROY_MES_MIN_CAP,
        "proy_mes_cat_ratio": PROY_MES_CAT_RATIO,
        "cobertura_critica_dias": COBERTURA_CRITICA_DIAS,
        "cobertura_baja_dias": COBERTURA_BAJA_DIAS,
        "dias_absorcion_bestseller_max": DIAS_ABSORCION_BESTSELLER_MAX,
        "dsv_quiebre_max_dias": DSV_QUIEBRE_MAX_DIAS,
        "lifetime_bestseller_min": LIFETIME_BESTSELLER_MIN,
        "lote_frenado_stock_min": LOTE_FRENADO_STOCK_MIN,
        "lote_frenado_proy_min": LOTE_FRENADO_PROY_MIN,
        "lote_frenado_proy30_max": LOTE_FRENADO_PROY30_MAX,
        "lote_frenado_edad_min": LOTE_FRENADO_EDAD_MIN,
        "lento_cronico_dsv_min": LENTO_CRONICO_DSV_MIN,
        "lento_cronico_lifetime_max": LENTO_CRONICO_LIFETIME_MAX,
        "lento_cronico_proy_max": LENTO_CRONICO_PROY_MAX,
        "recien_reabastecido_dias": RECIEN_REABASTECIDO_DIAS,
        "transfer_donor_stock_min": TRANSFER_DONOR_STOCK_MIN,
    }


async def _execute_query_to_dicts(db: AsyncSession, company_id: int, sql: str) -> tuple[list[str], list[dict]]:
    """Ejecuta una consulta SQL parametrizada y mapea sus columnas de clasificación dinámicamente.

    Multi-tenant: el parámetro `:company_id` se inyecta en todas las CTEs base.
    """
    settings = get_settings()
    params = _get_query_params()
    params["company_id"] = company_id

    # ★ Restricciones resueltas a IDs ACTUALES por NOMBRE (robusto a re-seeds):
    #   - Exclusiones: desde la DB (app_config, editable en la UI / sembrada de .env).
    #   - Estacional: desde .env (SEASONAL_DEPARTMENT_NAMES), por nombre.
    # Efecto en vivo en TODAS las matrices sin reiniciar. Si algo falla, quedan los
    # placeholders vacíos (no excluye / no estacional) — degradación segura.
    try:
        from app.routers.config_admin import get_exclusions, get_seasonal
        from app.kawii_matrix.runtime_config import apply_db_overrides
        excl = await get_exclusions(db, company_id)
        params["excluded_departments"] = excl["departments"]
        params["excluded_categories"] = excl["categories"]
        params["seasonal_departments"] = await get_seasonal(db, company_id)
        # ★ Override desde app_config (thresholds + IDs operativos). Lo que esté
        #   guardado en la UI manda sobre los valores del .env. Si no hay nada
        #   en DB, los valores del .env se conservan (degradación segura).
        await apply_db_overrides(db, company_id, params)
    except Exception:
        pass

    # ★ MULTI-TENANT (RLS): activa el filtro por empresa para esta transacción.
    # RLS habilitado en TODAS las tablas con company_id filtra automáticamente
    # las filas visibles. Sin este SET, ninguna fila se ve (el policy exige
    # que current_company_id() no sea NULL).
    await db.execute(text(f"SET LOCAL app.current_company = '{int(company_id)}'"))

    # ★ PERFORMANCE: las matrices tienen ~15 CTEs correlacionados. El planner de
    # Postgres subestima la cardinalidad (estima rows=137 cuando son ~3400) y elige
    # nested loops que re-escanean CTEs materializados millones de veces → ~205s.
    # Forzar hash/merge joins baja el tiempo a ~4s (50× más rápido). Verificado.
    # SET LOCAL = solo afecta esta transacción (no contamina el pool ni otros endpoints).
    await db.execute(text("SET LOCAL enable_nestloop = off"))

    # ★ FECHAS: BSale guarda emissionDate/admissionDate a MEDIANOCHE UTC. Las matrices
    # extraen la fecha con DATE(...) / ::date, que usan el TZ de sesión. Forzamos UTC
    # para obtener la fecha real del documento; con el TZ por defecto (Lima) toda
    # fecha (última venta, días sin venta, agrupación diaria) se corría un día atrás.
    await db.execute(text("SET LOCAL timezone = 'UTC'"))

    result = await db.execute(text(sql), params)
    
    raw_columns = list(result.keys())
    columns = [settings.CLASSIFICATION_LABEL if col == "Clasificación" else col for col in raw_columns]
    raw_rows = result.fetchall()
    
    rows: list[dict] = []
    for row in raw_rows:
        row_dict = {}
        for col_name, val in zip(raw_columns, row):
            target_col = settings.CLASSIFICATION_LABEL if col_name == "Clasificación" else col_name
            row_dict[target_col] = val
        rows.append(row_dict)
        
    return columns, rows


async def run_matrix(
    db: AsyncSession,
    company_id: int,
    module_id: str,
    *,
    sucursal: str | None = None,
    departamento: str | None = None,
    categoria: str | None = None,
    subcategoria: str | None = None,
    sku: str | None = None,
    clasificacion_contains: str | None = None,
    nivel: str | None = None,         # Solo aplica al 07 (DEPARTAMENTO/CATEGORÍA/SUBCATEGORÍA/SKU)
    solo_actividad_90d: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """
    Ejecuta una matriz y aplica filtros post-query.

    `solo_actividad_90d`: oculta la "cola del punto ciego" — SKUs sin stock hoy
    y sin ventas/recepciones en 90d (están en el reporte porque vendieron hace
    90-180d: OPORTUNIDAD PERDIDA, DEMANDA EXTINTA, etc.). Útil para analizar
    solo lo que está vivo HOY en tienda. Solo aplica a módulos con las
    columnas 90d (04/04b/05); en el resto es no-op.

    Returns:
        {
            "module": "04",
            "total": int,       # filas tras filtros
            "limit": int | None,
            "offset": int,
            "columns": list[str],
            "rows": list[dict],
        }
    """
    sql = _load_sql(module_id)
    settings = get_settings()
    columns, all_rows = await matrix_cache.get_or_compute(
        module_id,
        company_id,
        settings.MATRIX_CACHE_TTL_SECONDS,
        lambda: _execute_query_to_dicts(db, company_id, sql),
    )

    # ---- Filtros (case-insensitive containment para texto) ----
    def _match(row: dict, col: str, value: str | None, exact: bool = False) -> bool:
        if value is None:
            return True
        cell = row.get(col)
        if cell is None:
            return False
        cell_s = str(cell).strip()
        val_s = value.strip()
        if exact:
            return cell_s.casefold() == val_s.casefold()
        return val_s.casefold() in cell_s.casefold()

    # ── Filtro "solo actividad 90d" ──
    # Vivo hoy = vendió en 90d, O tiene stock en tienda, O recibió en 90d.
    # Se decide por columnas presentes (no por module_id) para que sea no-op
    # en módulos sin ventana 90d (06/07/08).
    def _num(v: Any) -> float:
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    aplica_actividad = solo_actividad_90d and "Unds Vend (90d)" in columns

    filtered = []
    for row in all_rows:
        if aplica_actividad and not (
            _num(row.get("Unds Vend (90d)")) > 0
            or _num(row.get("Stock Disp")) > 0
            or _num(row.get("Unds Recib (90d)")) > 0
        ):
            continue
        if not _match(row, "Sucursal", sucursal):
            continue
        if not _match(row, "Departamento", departamento):
            continue
        if not _match(row, "Categoría", categoria):
            continue
        if not _match(row, "Subcategoría", subcategoria):
            continue
        if sku is not None and not _match(row, "Código SKU", sku, exact=True):
            continue
        # Clasificación: puede llamarse mediante CLASSIFICATION_LABEL, "Prioridad / Recomendación",
        # "Diagnóstico Ciclo Vida" o "Diagnóstico" según el módulo. Buscamos en cualquiera.
        if clasificacion_contains is not None:
            label_cols = [
                settings.CLASSIFICATION_LABEL,
                "Prioridad / Recomendación",
                "Diagnóstico",
                "Diagnóstico Ciclo Vida",
            ]
            found = False
            for lc in label_cols:
                if lc in row and row[lc] and clasificacion_contains.casefold() in str(row[lc]).casefold():
                    found = True
                    break
            if not found:
                continue
        if nivel is not None and "Nivel" in row:
            if not _match(row, "Nivel", nivel, exact=True):
                continue
        filtered.append(row)

    total = len(filtered)

    if limit is not None:
        filtered = filtered[offset : offset + limit]
    elif offset > 0:
        filtered = filtered[offset:]

    return {
        "module": module_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "columns": columns,
        "rows": filtered,
    }


async def get_distribution(
    db: AsyncSession,
    company_id: int,
    module_id: str,
    *,
    sucursal: str | None = None,
) -> dict[str, Any]:
    """
    Devuelve la distribución de clasificaciones de un módulo.
    Útil para dashboards: cuántos productos en cada categoría.
    """
    sql = _load_sql(module_id)
    columns, rows = await _execute_query_to_dicts(db, company_id, sql)
    settings = get_settings()

    # Detectar columna de clasificación (varía por módulo)
    label_col = None
    for candidate in [
        settings.CLASSIFICATION_LABEL,
        "Prioridad / Recomendación",
        "Diagnóstico Ciclo Vida",
    ]:
        if candidate in columns:
            label_col = candidate
            break
    if not label_col:
        raise RuntimeError(f"No se encontró columna de clasificación en módulo {module_id}")

    if sucursal:
        rows = [r for r in rows if r.get("Sucursal") and sucursal.casefold() in str(r["Sucursal"]).casefold()]

    counts: dict[str, int] = {}
    for r in rows:
        label = str(r.get(label_col) or "(sin)")
        counts[label] = counts.get(label, 0) + 1

    items = [{"label": k, "count": v} for k, v in counts.items()]
    items.sort(key=lambda x: -x["count"])
    return {
        "module": module_id,
        "label_column": label_col,
        "sucursal_filter": sucursal,
        "total_skus": sum(c["count"] for c in items),
        "categories": items,
    }


async def get_transfers(db: AsyncSession, company_id: int, module_id: str = "04") -> dict[str, Any]:
    """
    Devuelve solo los productos con sugerencia de transferencia inter-sucursal.
    Solo aplica a módulos 04 y 05 que tienen esta columna.
    """
    if module_id not in ("04", "04b", "05"):
        raise ValueError("Sugerencia Transferencia solo disponible en módulos 04 y 05")

    sql = _load_sql(module_id)
    columns, rows = await _execute_query_to_dicts(db, company_id, sql)
    if "Sugerencia Transferencia" not in columns:
        raise RuntimeError(f"Módulo {module_id} no tiene columna 'Sugerencia Transferencia'")

    transfers = [
        r for r in rows
        if r.get("Sugerencia Transferencia") and "Transferir" in str(r["Sugerencia Transferencia"])
    ]
    return {
        "module": module_id,
        "total": len(transfers),
        "transfers": transfers,
    }


ACTION_BUCKETS = (
    "urgente_comprar", "reponer", "saludable", "exceso",
    "liquidar", "descatalogar", "evaluar", "otro",
)


def classify_action(label: str) -> str:
    """Mapea una clasificación de SKU a su bucket de ACCIÓN de negocio.
    Reutilizado por get_action_groups y por el filtro de Excel por acción.

    Estrategia: buscar el VERBO de acción en la etiqueta (es la parte
    distintiva tras el rename de 2026-06-06). Mantiene compat con keywords
    de los nombres viejos (EXITOSO, MUERTO, etc.).

    Orden importa: las reglas más específicas van primero.
    """
    L = (label or "").upper()

    # 1) URGENTE: sin stock + alta rotación (perdiendo venta diaria).
    #    Solo "COMPRAR YA" — más restrictivo que "REPONER YA".
    if "COMPRAR YA" in L:
        return "urgente_comprar"
    if "QUIEBRE" in L and ("BESTSELLER" in L or "ALTA" in L):
        return "urgente_comprar"

    # 2) EXCESO: capital atrapado / promocionar. Va antes que "REPONER"
    #    porque "EXCESO + DEMANDA CAYENDO — PROMOCIONAR YA" podría ambiguar.
    if "EXCESO" in L or "STOCK EXCESIVO" in L:
        return "exceso"

    # 3) LIQUIDAR: stock parado / lote frenado / baja rotación.
    if any(k in L for k in (
        "STOCK PARADO", "LOTE FRENADO", "BAJA ROT",
        "MUERTO", "SALDO QUEMADO",  # compat
    )):
        return "liquidar"

    # 4) DESCATALOGAR: muertos, marginales, sin futuro.
    if any(k in L for k in (
        "DESCATALOGAR", "PRODUCTO MUERTO", "BAJO VOLUMEN AGOTADO",
        "EX-BESTSELLER ENFRIADO", "DEMANDA EXTINTA",
        "MARGINAL", "RESIDUO", "HISTÓRICO", "FRACASO", "RECIBIDO",  # compat
    )):
        return "descatalogar"

    # 5) REPONER: bestsellers o agotados con demanda. Acción imperativa
    #    "REPONER YA" / "REPONER POCO" / "REPONER" en la etiqueta.
    if "REPONER YA" in L or "REPONER POCO" in L or "AGOTADO CON DEMANDA" in L:
        return "reponer"
    # ★ P21 (2026-06-10): "BESTSELLER AGOTADO 1-2 MESES — REPONER" (antes
    #   "BESTSELLER EN PAUSA — EVALUAR"). Vendió todo y nadie repuso; los
    #   estacionales ya se filtraron antes (TEMPORADA CERRADA) → reponer.
    if "BESTSELLER AGOTADO" in L:
        return "reponer"
    if any(k in L for k in (
        "EXITOSO ACTIVO", "EXITOSO OLVIDADO",  # compat (nombres viejos)
        "POTENCIAL ACTIVO", "STOCK PREVIO", "LOTE AGOTADO RÁPIDO",
    )):
        return "reponer"

    # 6) SALUDABLE: rotando bien, ritmo normal.
    if any(k in L for k in (
        "PRIORIDAD DE COMPRA",     # ALTA ROTACIÓN
        "MANTENER FLUJO",          # ROTACIÓN ACTIVA
        "RITMO NORMAL",            # INVENTARIO SANO
        "ALTA ROTACIÓN", "ROTACIÓN ACTIVA", "INVENTARIO SANO",  # compat
        "LOTE NUEVO VENDIENDO",    # ★ FIX 2026-06-10: el SQL lo define como sano (antes caía en "otro")
    )):
        return "saludable"

    # 7) EVALUAR: requiere mirada manual antes de decidir.
    if any(k in L for k in (
        "EVALUAR", "VIGILAR", "ESPERAR", "VERIFICAR", "INVESTIGAR",
        "PRODUCTO NUEVO", "EMERGENTE", "STOCK BAJO QUIETO",
        "RITMO PERDIDO", "VENDIENDO MÁS QUE ANTES", "BESTSELLER EN PAUSA",
        "POCO STOCK CON DEMANDA",
        "NUEVO", "ESCONDIDO", "ALERTA VISUAL",  # compat
    )):
        return "evaluar"

    return "otro"


def find_label_column(columns: list[str]) -> str | None:
    settings = get_settings()
    for c in [settings.CLASSIFICATION_LABEL, "Prioridad / Recomendación", "Diagnóstico Ciclo Vida"]:
        if c in columns:
            return c
    return None


async def get_action_groups(db: AsyncSession, company_id: int, module_id: str = "04") -> dict[str, Any]:
    """
    Agrupa SKUs por ACCIÓN de negocio (no por etiqueta exacta).
    Útil para el dashboard ejecutivo.
    """
    sql = _load_sql(module_id)
    columns, rows = await _execute_query_to_dicts(db, company_id, sql)

    label_col = find_label_column(columns)
    if not label_col:
        raise RuntimeError(f"No se encontró columna de clasificación en módulo {module_id}")

    groups: dict[str, list] = {k: [] for k in ACTION_BUCKETS}
    for r in rows:
        # Solo SKUs (no rollups del módulo 07)
        if r.get("Nivel") and r["Nivel"] != "SKU":
            continue
        groups[classify_action(str(r.get(label_col) or ""))].append(r)

    return {
        "module": module_id,
        "label_column": label_col,
        "summary": {k: len(v) for k, v in groups.items()},
        "groups": groups,
    }


async def get_summary(db: AsyncSession, company_id: int) -> dict[str, Any]:
    """
    Resumen ejecutivo combinando los 3 módulos operativos (04, 05, 07).
    Útil para mostrar en una tarjeta del dashboard.
    """
    sql = _load_sql("04")
    columns, rows = await _execute_query_to_dicts(db, company_id, sql)
    settings = get_settings()

    by_branch: dict[str, dict] = {}
    transfers_count = 0
    growing = 0
    declining = 0

    for r in rows:
        suc = str(r.get("Sucursal") or "—")
        if suc not in by_branch:
            by_branch[suc] = {
                "total_skus": 0,
                "urgente": 0,
                "reponer": 0,
                "saludable": 0,
                "descatalogar": 0,
                "exceso": 0,
            }
        b = by_branch[suc]
        b["total_skus"] += 1

        # Reutilizamos classify_action para evitar drift entre dos
        # implementaciones de la misma lógica de buckets.
        action = classify_action(str(r.get(settings.CLASSIFICATION_LABEL) or ""))
        if action == "urgente_comprar":
            b["urgente"] += 1
        elif action == "reponer":
            b["reponer"] += 1
        elif action == "saludable":
            b["saludable"] += 1
        elif action == "exceso":
            b["exceso"] += 1
        elif action == "descatalogar":
            b["descatalogar"] += 1

        # Transferencias y tendencia
        if r.get("Sugerencia Transferencia") and "Transferir" in str(r["Sugerencia Transferencia"]):
            transfers_count += 1
        tend = str(r.get("Tendencia") or "")
        if "Creciendo" in tend:
            growing += 1
        elif "Decayendo" in tend:
            declining += 1

    return {
        "total_skus": len(rows),
        "by_branch": by_branch,
        "transfers_sugeridas": transfers_count,
        "tendencia_creciendo": growing,
        "tendencia_decayendo": declining,
    }
