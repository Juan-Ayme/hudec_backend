"""
Decisiones de compra del catálogo (Compras & Catálogo → modal por SKU).

Persiste lo que el usuario decide cuando aprieta Ordenar / Comprar similar /
Posponer / Ignorar en el dashboard de compras. Cada decisión guarda un
SNAPSHOT de cómo estaba el SKU al momento (clasificación, stock, sugerencia),
para poder reconstruir "¿qué clasificación tenía cuando posponí?" sin
depender de que las matrices den el mismo resultado más adelante.

Tipos de decisión:
- `solicitado`: el encargado de tienda AVISA que este SKU hay que pedirlo.
  No decide la compra — solo la solicita. Es el único tipo que puede
  registrar un rol `viewer`; el admin/operador lo ve en su bandeja y
  resuelve con `ordenar`/`ignorar`. Sin cantidad obligatoria.
- `ordenar`: comprar este SKU exacto. Persiste la cantidad acordada.
- `comprar_similar`: comprar un producto IGUAL (mismo concepto, no
  necesariamente el SKU). Útil cuando el SKU exacto no está disponible
  pero se compra algo equivalente. Persiste cantidad.
- `posponer`: no comprar ahora, revisar después. Sin cantidad.
- `ignorar`: no comprar (descatalogar candidato). Sin cantidad.

Cada SKU+sucursal acumula HISTORIAL de decisiones (no se pisa). La
"decisión vigente" es la más reciente por (variant_id, office_id).

Tabla: `purchase_decisions`. DDL al final del archivo — correrlo una vez
en la DB antes del primer uso:

    CREATE TABLE IF NOT EXISTS purchase_decisions (
        id BIGSERIAL PRIMARY KEY,
        bsale_variant_id INTEGER NOT NULL REFERENCES variants(bsale_variant_id) ON DELETE CASCADE,
        bsale_office_id INTEGER NOT NULL REFERENCES offices(bsale_office_id) ON DELETE CASCADE,
        decision TEXT NOT NULL CHECK (decision IN ('ordenar','comprar_similar','posponer','ignorar')),
        quantity INTEGER,
        notes TEXT,
        classification_snapshot JSONB,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS purchase_decisions_variant_office_idx
        ON purchase_decisions (bsale_variant_id, bsale_office_id, created_at DESC);
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    CurrentCompany,
    CurrentUser,
    get_current_company,
    get_current_user,
    require_operador_or_admin,
)
from app.database import get_db

router = APIRouter(
    prefix="/purchases",
    tags=["purchases"],
    dependencies=[Depends(get_current_company)],
)


# ──────────────────────────────────────────────────────────────────────────────
# Modelos Pydantic (request/response)
# ──────────────────────────────────────────────────────────────────────────────

DecisionKind = Literal[
    "solicitado", "ordenar", "comprar_similar", "posponer", "ignorar"
]


class DecisionCreate(BaseModel):
    """Payload del POST /purchases/decisions.

    Identificación del SKU: se acepta `bsale_variant_id` (el FK real) O `sku`
    (display_code, lo que el usuario ve en pantalla). Al menos uno debe
    venir; si vienen ambos, manda `bsale_variant_id`.

    `quantity` es opcional: requerido para `ordenar` y `comprar_similar`,
    ignorado para `posponer` e `ignorar`.

    `classification_snapshot` es un dict libre con lo que la UI tenía a la
    vista cuando el usuario decidió: clasificación textual, sugerencia,
    stock, etc. Se guarda como JSONB para poder reconstruir contexto sin
    depender de la matriz actual."""

    bsale_variant_id: int | None = None
    sku: str | None = None
    bsale_office_id: int
    decision: DecisionKind
    quantity: int | None = None
    notes: str | None = None
    classification_snapshot: dict[str, Any] | None = None

    @field_validator("quantity")
    @classmethod
    def _quantity_consistent_with_decision(cls, v, info):
        decision = info.data.get("decision")
        if decision in ("ordenar", "comprar_similar"):
            if v is None or v <= 0:
                raise ValueError(
                    f"`quantity` debe ser > 0 cuando decision='{decision}'"
                )
        return v


class DecisionOut(BaseModel):
    """Una fila tal como vive en la DB."""

    id: int
    bsale_variant_id: int
    bsale_office_id: int
    decision: DecisionKind
    quantity: int | None
    notes: str | None
    classification_snapshot: dict[str, Any] | None
    created_at: datetime
    # Quién registró la decisión (para "Solicitado por Deisy"). `actor_user_id`
    # puede ser NULL en filas viejas anteriores a la migración 2026-07-11.
    actor_user_id: int | None = None
    actor_username: str | None = None
    # display_code del SKU (lo que el usuario ve). Presente en la lista de
    # decisiones vigentes para cruzar con la tabla de compras sin exponer el
    # variant_id interno. None en respuestas que no lo resuelven.
    sku: str | None = None


class DecisionHistory(BaseModel):
    """Decisión vigente + historial completo para un (SKU, sucursal)."""

    current: DecisionOut | None = Field(
        None, description="Última decisión registrada (la 'vigente')."
    )
    history: list[DecisionOut] = Field(
        default_factory=list,
        description="Todas las decisiones para este SKU+sucursal, más recientes primero.",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _row_to_decision(row: dict) -> DecisionOut:
    """Convierte una fila de DB en DecisionOut (deserializa JSON snapshot)."""
    snapshot = row.get("classification_snapshot")
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (ValueError, TypeError):
            snapshot = None
    return DecisionOut(
        id=row["id"],
        bsale_variant_id=row["bsale_variant_id"],
        bsale_office_id=row["bsale_office_id"],
        decision=row["decision"],
        quantity=row.get("quantity"),
        notes=row.get("notes"),
        classification_snapshot=snapshot,
        created_at=row["created_at"],
        actor_user_id=row.get("actor_user_id"),
        actor_username=row.get("actor_username"),
        sku=row.get("sku"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────────


async def _resolve_variant_id(
    db: AsyncSession, bsale_variant_id: int | None, sku: str | None
) -> int:
    """Devuelve el `bsale_variant_id` definitivo, resolviendo `sku` si hace falta."""
    if bsale_variant_id is not None:
        return bsale_variant_id
    if not sku:
        raise HTTPException(
            status_code=400, detail="Debe pasarse `bsale_variant_id` o `sku`"
        )
    vid = await db.scalar(
        text("SELECT bsale_variant_id FROM variants WHERE display_code = :s LIMIT 1"),
        {"s": sku},
    )
    if vid is None:
        raise HTTPException(status_code=404, detail=f"SKU '{sku}' no existe")
    return vid


@router.post("/decisions")
async def create_decision(
    body: DecisionCreate,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
    company: CurrentCompany = Depends(get_current_company),
) -> dict:
    """Registra una nueva decisión. NO actualiza filas existentes — se
    apila al historial. La 'decisión vigente' para ese (SKU, sucursal) pasa
    a ser esta.

    Permiso por tipo de decisión:
      - `solicitado` (avisar que falta): cualquier miembro de la empresa,
        incluido `viewer`. Es la acción del encargado de tienda.
      - resto (decidir la compra): solo `operador`/`admin`.
    """
    if body.decision != "solicitado" and company.role not in ("operador", "admin"):
        raise HTTPException(
            status_code=403,
            detail=(
                "Tu rol solo permite registrar decisiones de tipo 'solicitado'. "
                "Ordenar/ignorar/posponer requieren rol operador o admin."
            ),
        )

    variant_id = await _resolve_variant_id(db, body.bsale_variant_id, body.sku)
    result = await db.execute(
        text(
            """
            INSERT INTO purchase_decisions
                (company_id, bsale_variant_id, bsale_office_id, decision,
                 quantity, notes, classification_snapshot, actor_user_id)
            VALUES (:c, :v, :o, :d, :q, :n, CAST(:s AS jsonb), :actor)
            RETURNING id, created_at
            """
        ),
        {
            # company_id explícito: la columna es NOT NULL y el RLS exige
            # company_id = current_company_id(). Antes se omitía (bug latente).
            "c": company.company_id,
            "v": variant_id,
            "o": body.bsale_office_id,
            "d": body.decision,
            "q": body.quantity,
            "n": body.notes,
            "s": json.dumps(body.classification_snapshot)
            if body.classification_snapshot is not None
            else None,
            "actor": user.id,
        },
    )
    row = result.mappings().one()
    await db.commit()
    return {
        "ok": True,
        "id": row["id"],
        "bsale_variant_id": variant_id,
        "created_at": row["created_at"].isoformat(),
    }


@router.get("/decisions/by-sku/{sku:path}")
async def read_decisions_for_sku_by_code(
    sku: str,
    office_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> DecisionHistory:
    """Igual que `/decisions/{variant_id}` pero acepta el `display_code` del
    SKU (lo que el usuario ve en pantalla, ej. 'L9162')."""
    variant_id = await _resolve_variant_id(db, None, sku)
    return await read_decisions_for_sku(variant_id, office_id, db)


@router.get("/decisions/{bsale_variant_id}")
async def read_decisions_for_sku(
    bsale_variant_id: int,
    office_id: int | None = Query(
        None,
        description="Si se pasa, filtra al SKU+sucursal. Si no, devuelve el "
        "historial cross-sucursal del SKU.",
    ),
    db: AsyncSession = Depends(get_db),
) -> DecisionHistory:
    """Decisión vigente + historial completo para un SKU (opcionalmente
    filtrado por sucursal)."""
    params: dict[str, Any] = {"v": bsale_variant_id}
    where = "pd.bsale_variant_id = :v"
    if office_id is not None:
        where += " AND pd.bsale_office_id = :o"
        params["o"] = office_id
    rows = (
        await db.execute(
            text(
                f"""
                SELECT pd.id, pd.bsale_variant_id, pd.bsale_office_id, pd.decision,
                       pd.quantity, pd.notes, pd.classification_snapshot,
                       pd.created_at, pd.actor_user_id, u.username AS actor_username
                FROM purchase_decisions pd
                LEFT JOIN app_users u ON u.id = pd.actor_user_id
                WHERE {where}
                ORDER BY pd.created_at DESC
                """
            ),
            params,
        )
    ).mappings().all()
    history = [_row_to_decision(dict(r)) for r in rows]
    return DecisionHistory(
        current=history[0] if history else None,
        history=history,
    )


@router.get("/decisions")
async def list_active_decisions(
    office_id: int | None = Query(None),
    decision: DecisionKind | None = Query(
        None, description="Filtrar por tipo de decisión."
    ),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Lista la DECISIÓN VIGENTE (más reciente) por SKU+sucursal. Útil para
    el dashboard: 'qué SKUs ya tienen una decisión tomada y cuál'."""
    params: dict[str, Any] = {}
    filters = []
    if office_id is not None:
        filters.append("pd.bsale_office_id = :o")
        params["o"] = office_id
    if decision is not None:
        filters.append("pd.decision = :d")
        params["d"] = decision
    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    # DISTINCT ON para tomar la fila más reciente por (variant, office).
    rows = (
        await db.execute(
            text(
                f"""
                SELECT DISTINCT ON (pd.bsale_variant_id, pd.bsale_office_id)
                       pd.id, pd.bsale_variant_id, pd.bsale_office_id, pd.decision,
                       pd.quantity, pd.notes, pd.classification_snapshot,
                       pd.created_at, pd.actor_user_id, u.username AS actor_username,
                       v.display_code AS sku
                FROM purchase_decisions pd
                LEFT JOIN app_users u ON u.id = pd.actor_user_id
                LEFT JOIN variants v
                       ON v.company_id = pd.company_id
                      AND v.bsale_variant_id = pd.bsale_variant_id
                {where}
                ORDER BY pd.bsale_variant_id, pd.bsale_office_id, pd.created_at DESC
                """
            ),
            params,
        )
    ).mappings().all()
    return {
        "total": len(rows),
        "decisions": [_row_to_decision(dict(r)).model_dump(mode="json") for r in rows],
    }


@router.delete("/decisions/{decision_id}")
async def delete_decision(
    decision_id: int,
    db: AsyncSession = Depends(get_db),
    _user: CurrentUser = Depends(require_operador_or_admin),
) -> dict:
    """Borra una decisión histórica específica (no recomendado en general:
    preferí registrar una decisión NUEVA que invierta la anterior). Útil
    solo si el usuario apretó por error y quiere limpiar el rastro."""
    result = await db.execute(
        text("DELETE FROM purchase_decisions WHERE id = :id RETURNING id"),
        {"id": decision_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Decisión {decision_id} no existe")
    await db.commit()
    return {
        "ok": True,
        "deleted_id": row["id"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
