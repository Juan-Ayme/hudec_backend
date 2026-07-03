"""
Endpoints REST para el sistema KAWII Matrix.

Expone las 4 matrices analíticas como JSON, con filtros, paginación,
agregaciones (distribución, grupos de acción) y vistas especializadas
(transferencias, resumen ejecutivo).
"""

import io
from datetime import datetime

from fastapi import APIRouter, Depends, Query, HTTPException, Path
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentCompany, get_current_company, require_admin
from app.config import get_settings
from app.database import get_db
from app.kawii_matrix import cache as matrix_cache
from app.kawii_matrix import service
from app.kawii_matrix.schemas import (
    MatrixResponse,
    DistributionResponse,
    TransferResponse,
    ActionGroupsResponse,
    SummaryResponse,
)


router = APIRouter(
    prefix="/matrix",
    tags=["matrix"],
    dependencies=[Depends(get_current_company)],
)


# ─────────────────────────────────────────────────────────────────────────
# Endpoint principal: ejecutar una matriz por su ID con filtros
# ─────────────────────────────────────────────────────────────────────────

@router.get(
    "/{module_id}",
    response_model=MatrixResponse,
    summary="Ejecuta una matriz KAWII y devuelve los SKUs clasificados",
)
async def get_matrix(
    module_id: str = Path(..., pattern="^(04|04b|05|06|07|08)$", description="ID del módulo"),
    sucursal: str | None = Query(None, description="Filtro: Magdalena, Asamblea"),
    departamento: str | None = Query(None, description="Filtro por nombre de departamento"),
    categoria: str | None = Query(None, description="Filtro por categoría"),
    subcategoria: str | None = Query(None, description="Filtro por subcategoría"),
    sku: str | None = Query(None, description="Filtro exacto por código SKU"),
    clasificacion_contains: str | None = Query(
        None,
        description="Filtra por texto contenido en la clasificación (ej: 'ALTA ROTACIÓN', 'EXITOSO')",
    ),
    nivel: str | None = Query(
        None,
        description="Solo módulo 07: filtrar por nivel jerárquico (DEPARTAMENTO/CATEGORÍA/SUBCATEGORÍA/SKU)",
    ),
    limit: int | None = Query(None, ge=1, le=10000, description="Máximo de filas a retornar"),
    offset: int = Query(0, ge=0, description="Offset para paginación"),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
):
    """
    Módulos disponibles:
      - **04**: Matriz 90d (foto operativa — 90 días, vista por sucursal)
      - **04b**: Matriz 90d Jerárquica (+ totales en S/ por Subcat/Cat/Depto)
      - **05**: Matriz Operativa (90d + contexto lifetime + IC)
      - **06**: Histórico Productos (lifetime, autopsia de ciclo de vida)
      - **07**: Informe Consolidado (jerárquico DEPT→CAT→SUBCAT→SKU con ABC Pareto)
    """
    try:
        return await service.run_matrix(
            db,
            company.company_id,
            module_id,
            sucursal=sucursal,
            departamento=departamento,
            categoria=categoria,
            subcategoria=subcategoria,
            sku=sku,
            clasificacion_contains=clasificacion_contains,
            nivel=nivel,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────
# Distribución por categoría
# ─────────────────────────────────────────────────────────────────────────

# ============================================================================
# ENDPOINTS COMENTADOS (2026-06-20) — no consumidos por el frontend.
# getMatrixDistribution y getMatrixTransfers están en api.ts pero ninguna
# página los importa. Las queries reales viven en app/kawii_matrix/service.py
# (get_distribution / get_transfers); aquí solo va el handler HTTP.
# ============================================================================

# @router.get(
#     "/{module_id}/distribution",
#     response_model=DistributionResponse,
#     summary="Distribución de SKUs por etiqueta de clasificación",
# )
# async def get_distribution(
#     module_id: str = Path(..., pattern="^(04|04b|05|06|07|08)$"),
#     sucursal: str | None = Query(None, description="Filtro opcional por sucursal"),
#     db: AsyncSession = Depends(get_db),
# ):
#     """
#     Devuelve cuántos productos están en cada categoría.
#     Útil para gráficos de pie/donut en el dashboard.
#     """
#     try:
#         return await service.get_distribution(db, module_id, sucursal=sucursal)
#     except (ValueError, RuntimeError) as exc:
#         raise HTTPException(status_code=400, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────
# Transferencias inter-sucursal
# ─────────────────────────────────────────────────────────────────────────

# @router.get(
#     "/{module_id}/transfers",
#     response_model=TransferResponse,
#     summary="Sugerencias de transferencia inter-sucursal (solo módulos 04 y 05)",
# )
# async def get_transfers(
#     module_id: str = Path(..., pattern="^(04|04b|05)$"),
#     db: AsyncSession = Depends(get_db),
# ):
#     """
#     Detecta productos con EXCESO en una sucursal mientras la otra tiene DÉFICIT.
#     Sugiere cantidad a transferir manteniendo 1 mes de stock al donante.
#     """
#     try:
#         return await service.get_transfers(db, module_id)
#     except (ValueError, RuntimeError) as exc:
#         raise HTTPException(status_code=400, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────
# Grupos de acción (urgente_comprar, reponer, descatalogar, etc.)
# ─────────────────────────────────────────────────────────────────────────

@router.get(
    "/{module_id}/action-groups",
    response_model=ActionGroupsResponse,
    summary="Agrupa SKUs por acción de negocio (urgente/reponer/descatalogar/...)",
)
async def get_action_groups(
    module_id: str = Path(..., pattern="^(04|04b|05|06|07|08)$"),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
):
    """
    Agrupa las ~26 etiquetas en 7 grupos accionables:
      - **urgente_comprar**: QUIEBRE Alta Rotación, Lote vendido rápido
      - **reponer**: STOCK PREVIO, POTENCIAL ACTIVO, EXITOSO, LOTE AGOTADO RÁPIDO
      - **saludable**: ALTA/ACTIVA/SANA/MEDIA ROTACIÓN
      - **exceso**: EXCESO INVENTARIO
      - **liquidar**: BAJA ROT 45d, MUERTO con stock
      - **descatalogar**: MARGINAL, RESIDUO, HISTÓRICO
      - **evaluar**: NUEVO, EMERGENTE, ESCONDIDO, ALERTA VISUAL
    """
    try:
        return await service.get_action_groups(db, company.company_id, module_id)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────
# Resumen ejecutivo
# ─────────────────────────────────────────────────────────────────────────

# ENDPOINT COMENTADO (2026-06-20) — getMatrixSummary en api.ts no es importado
# por ninguna página. La query real vive en service.get_summary().

# @router.get(
#     "/_/summary",
#     response_model=SummaryResponse,
#     summary="Resumen ejecutivo de los 2 sucursales (vista operativa 90d)",
# )
# async def get_summary(db: AsyncSession = Depends(get_db)):
#     """
#     Devuelve KPIs operativos:
#       - Total SKUs activos por sucursal
#       - Cuántos urgentes, reponer, saludables, descatalogar, exceso
#       - Cantidad de transferencias inter-sucursal sugeridas
#       - Productos creciendo vs decayendo en últimos 45d
#     """
#     return await service.get_summary(db)


# ─────────────────────────────────────────────────────────────────────────
# Lista de módulos disponibles
# ─────────────────────────────────────────────────────────────────────────

@router.get("/", summary="Lista los módulos disponibles")
async def list_modules():
    """Devuelve los IDs y descripciones de cada matriz."""
    return {
        "modules": [
            {
                "id": "04",
                "name": "Matriz 90d",
                "description": "Foto operativa del 'ahora' (ventana 90 días, por sucursal)",
                "endpoint": "/matrix/04",
            },
            {
                "id": "04b",
                "name": "Matriz 90d Jerárquica",
                "description": "Matriz 90d + totales en S/ por Subcategoría, Categoría y Departamento",
                "endpoint": "/matrix/04b",
            },
            {
                "id": "05",
                "name": "Matriz Operativa Enriquecida",
                "description": "Matriz 90d + contexto lifetime (Mejor Mes, IC, Sell-Through Lifetime)",
                "endpoint": "/matrix/05",
            },
            {
                "id": "06",
                "name": "Histórico Productos",
                "description": "Autopsia lifetime: ciclo de vida completo del SKU",
                "endpoint": "/matrix/06",
            },
            {
                "id": "07",
                "name": "Informe Consolidado",
                "description": "Vista jerárquica DEPT→CAT→SUBCAT→SKU con ABC Pareto",
                "endpoint": "/matrix/07",
            },
        ],
        "endpoints_especiales": {
            "summary": "/matrix/_/summary",
            "distribution": "/matrix/{module_id}/distribution",
            "transfers": "/matrix/{module_id}/transfers",
            "action_groups": "/matrix/{module_id}/action-groups",
        },
    }



# ─────────────────────────────────────────────────────────────────────────
# Descarga de Excel del módulo (.xlsx)
# ─────────────────────────────────────────────────────────────────────────

_MODULE_TITLES = {
    "04":  ("Matriz 90d", "04_matriz_90d.sql"),
    "04b": ("Matriz 90d Jerárquica", "04b_matriz_90d_jerarquico.sql"),
    "05":  ("Matriz Operativa (90d + lifetime)", "05_matriz_operativa.sql"),
    "06":  ("Histórico Productos (lifetime)", "06_historico_productos.sql"),
    "07":  ("Informe Consolidado (ABC Pareto)", "07_informe_consolidado.sql"),
    "08":  ("Transferencias inter-sucursal", "08_transferencias.sql"),
}


@router.get(
    "/{module_id}/excel",
    summary="Descarga el reporte de la matriz como Excel (.xlsx)",
    response_class=StreamingResponse,
)
async def get_matrix_excel(
    module_id: str = Path(..., pattern="^(04|04b|05|06|07|08)$"),
    sucursal: str | None = Query(None),
    departamento: str | None = Query(None),
    categoria: str | None = Query(None),
    subcategoria: str | None = Query(None),
    sku: str | None = Query(None),
    clasificacion_contains: str | None = Query(None),
    nivel: str | None = Query(None),
    accion: str | None = Query(None, description="Filtro por bucket de acción (coma-separados)"),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
):
    """Genera un Excel maquetado del módulo solicitado.

    Para `04b` usa el layout ejecutivo (excel_executive) — es el único módulo
    cuyo SQL expone las columnas monetarias agregadas (Vendido SKU/Subcat/Cat/Depto S/)
    que necesita ese layout. Para los demás módulos cae al layout outline-colapsable
    (excel_builder) que sí trabaja con sus columnas en unidades.
    """
    from analytics.excel_builder import build_workbook
    from analytics.excel_executive import build_executive_workbook

    settings = get_settings()
    titulo, sql_file = _MODULE_TITLES.get(module_id, (f"Matriz {module_id}", f"{module_id}_*.sql"))

    started = datetime.now()
    try:
        result = await service.run_matrix(
            db, company.company_id, module_id,
            sucursal=sucursal, departamento=departamento, categoria=categoria,
            subcategoria=subcategoria, sku=sku,
            clasificacion_contains=clasificacion_contains, nivel=nivel,
            limit=None, offset=0,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    elapsed = (datetime.now() - started).total_seconds()

    # Filtro opcional por acción de negocio
    accion_label = None
    if accion:
        wanted = {a.strip() for a in accion.split(",") if a.strip()}
        invalid = wanted - set(service.ACTION_BUCKETS)
        if invalid:
            raise HTTPException(400, f"Acciones inválidas: {sorted(invalid)}")
        label_col = service.find_label_column(result["columns"])
        if not label_col:
            raise HTTPException(400, "Módulo sin columna de clasificación")
        result["rows"] = [
            r for r in result["rows"]
            if (not r.get("Nivel") or r["Nivel"] == "SKU")
            and service.classify_action(str(r.get(label_col) or "")) in wanted
        ]
        labels = {"urgente_comprar":"Compra urgente","reponer":"Reponer","saludable":"Saludable",
                  "exceso":"Exceso","liquidar":"Liquidar","descatalogar":"Descatalogar","evaluar":"Evaluar","otro":"Otro"}
        accion_label = " + ".join(labels[a] for a in service.ACTION_BUCKETS if a in wanted)
        titulo = f"{titulo} — {accion_label}"

    cols = result["columns"]
    rows_tuples = [tuple(row.get(c) for c in cols) for row in result["rows"]]

    if module_id == "04b":
        wb = build_executive_workbook(
            cols=cols, rows=rows_tuples, modulo_id=module_id,
            titulo=titulo, sql_file=sql_file, descripcion="",
            classification_col=settings.CLASSIFICATION_LABEL,
            elapsed_seconds=elapsed, brand_name=settings.BRAND_NAME,
            sucursal=sucursal, accion_label=accion_label, periodo_dias=90,
        )
    else:
        wb = build_workbook(
            cols=cols, rows=rows_tuples, modulo_id=module_id,
            titulo=titulo, sql_file=sql_file, descripcion="",
            classification_col=settings.CLASSIFICATION_LABEL,
            elapsed_seconds=elapsed, brand_name=settings.BRAND_NAME,
        )

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fecha = datetime.now().strftime("%Y-%m-%d")
    safe = titulo.replace("—","-").replace("/","_").replace(":","").replace(" ","_")
    filename = f"{module_id}_{safe}_{fecha}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "X-Total-Rows": str(len(rows_tuples))},
    )



# ─────────────────────────────────────────────────────────────────────────
# Cache de matrices (admin) — útil tras un sync grande / cambio de exclusiones
# ─────────────────────────────────────────────────────────────────────────

@router.get("/_cache/stats", summary="Estado del cache de matrices")
async def cache_stats() -> dict:
    return matrix_cache.stats()


@router.post(
    "/_cache/invalidate",
    dependencies=[Depends(require_admin)],
    summary="Invalida el cache de matrices (admin)",
)
async def cache_invalidate(
    module_id: str | None = None,
    company: CurrentCompany = Depends(get_current_company),
) -> dict:
    """Limpia el cache de la EMPRESA ACTIVA para el módulo dado (o todos)."""
    cleared = matrix_cache.invalidate(module_id, company.company_id)
    return {"cleared": cleared, "module_id": module_id, "company_id": company.company_id}
