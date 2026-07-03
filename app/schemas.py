"""Modelos Pydantic (schemas de respuesta)."""

from datetime import datetime, date
from typing import Optional, Any

from pydantic import BaseModel, ConfigDict, Field


# ---- Taxonomia ----

class Department(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    slug: Optional[str] = None


class Category(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    department_id: int
    name: str
    slug: Optional[str] = None


class Subcategory(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    category_id: int
    name: str
    slug: Optional[str] = None


class TaxonomyTree(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    departments: list[dict]


# ---- Productos ----

class Product(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    bsale_product_id: int
    name: str
    bsale_product_type_id: Optional[int] = None
    product_type_name: Optional[str] = None
    subcategory: Optional[str] = None
    category: Optional[str] = None
    department: Optional[str] = None
    state: Optional[int] = None


class Variant(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    bsale_variant_id: int
    bsale_product_id: int
    code: Optional[str] = None
    description: Optional[str] = None
    cost: Optional[float] = None
    price_list: Optional[float] = None


# ---- Stock ----

class StockLevel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    bsale_variant_id: int
    bsale_office_id: int
    office_name: Optional[str] = None
    variant_code: Optional[str] = None
    quantity_available: float
    quantity_reserved: Optional[float] = None


class StockValuation(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    sucursal: str
    valor_soles: float
    unidades: float


# ---- Documentos / Ventas ----

class Document(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    bsale_document_id: int
    document_type_name: Optional[str] = None
    number: Optional[str] = None
    emission_date: Optional[datetime] = None
    total_amount: Optional[float] = None
    office_id: Optional[int] = None
    is_credit_note: Optional[bool] = None


class SalesByDepartment(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    departamento: str
    ventas: float
    tickets: int


class SalesByDay(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    dia: date
    ventas: float
    tickets: int


# ---- Analytics ----

class DashboardKPIs(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    ventas_30d: float
    tickets_30d: int
    ticket_promedio_30d: float
    productos_total: int
    productos_mapeados: int
    variantes_total: int
    stock_total_valorizado: float
    sucursales: int


# ---- Sync ----

class SyncLogEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    entity: str
    status: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    records_fetched: Optional[int] = None
    records_inserted: Optional[int] = None
    records_updated: Optional[int] = None
    records_skipped: Optional[int] = None
    duracion_s: Optional[int] = None


class SyncTriggerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    ok: bool
    message: str
    task_id: Optional[str] = None


class HealthResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    status: str
    db: str
    version: str
    app: str
    timestamp: datetime
