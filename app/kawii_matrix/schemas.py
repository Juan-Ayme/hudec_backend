"""Pydantic schemas para respuestas de KAWII Matrix API."""

from typing import Any
from pydantic import BaseModel, Field


class MatrixRow(BaseModel):
    """Una fila de matriz (esquema flexible — columnas varían por módulo)."""
    model_config = {"extra": "allow"}


class MatrixResponse(BaseModel):
    module: str = Field(..., description="ID del módulo: 04, 05, 06 o 07")
    total: int = Field(..., description="Total de filas tras aplicar filtros")
    limit: int | None = Field(None, description="Limite aplicado (paginación)")
    offset: int = Field(0, description="Offset aplicado")
    columns: list[str] = Field(..., description="Nombres de columnas")
    rows: list[dict[str, Any]] = Field(..., description="Filas con datos")


class CategoryCount(BaseModel):
    label: str
    count: int


class DistributionResponse(BaseModel):
    module: str
    label_column: str
    sucursal_filter: str | None
    total_skus: int
    categories: list[CategoryCount]


class TransferResponse(BaseModel):
    module: str
    total: int
    transfers: list[dict[str, Any]]


class ActionGroupsSummary(BaseModel):
    urgente_comprar: int
    reponer: int
    saludable: int
    exceso: int
    liquidar: int
    descatalogar: int
    evaluar: int
    otro: int


class ActionGroupsResponse(BaseModel):
    module: str
    label_column: str
    summary: ActionGroupsSummary
    groups: dict[str, list[dict[str, Any]]]


class BranchStats(BaseModel):
    total_skus: int
    urgente: int
    reponer: int
    saludable: int
    descatalogar: int
    exceso: int


class SummaryResponse(BaseModel):
    total_skus: int
    by_branch: dict[str, BranchStats]
    transfers_sugeridas: int
    tendencia_creciendo: int
    tendencia_decayendo: int
