"""Endpoints de documentos (ventas / boletas / facturas / notas).

Por defecto solo lista documentos de las sucursales activas
(OFFICE_IDS en analytics.core.config). Si el frontend pasa
un office_id explicito (ej. para una vista de auditoria),
se respeta ese filtro especifico.

FIX (2026-05-06): Los filtros date_from / date_to ahora comparan contra
  (emission_date AT TIME ZONE 'America/Lima')::date para evitar que ventas
  nocturnas (> 19:00 Lima) queden asignadas al dia siguiente en UTC.
"""

from datetime import date, datetime

from fastapi import Depends, APIRouter, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from analytics.core.config import OFFICE_IDS
from app.database import get_db

router = APIRouter(prefix="/documents", tags=["documents"])

_OFFICE_IDS_SQL = ", ".join(str(i) for i in OFFICE_IDS)

# Zona horaria del negocio (Peru / Lima = UTC-5)
_TZ = "America/Lima"


# ============================================================================
# ENDPOINTS COMENTADOS (2026-06-20) — no consumidos por el frontend.
# Razón: ninguna página de src/app/ importa getDocuments, getDocument o
# getDocumentsSummary desde lib/api.ts. Se preservan las queries SQL para
# poder reactivar rápidamente si vuelven a necesitarse.
# ============================================================================

# @router.get("")
# async def list_documents(
#     date_from: date | None = None,
#     date_to: date | None = None,
#     document_type_id: int | None = None,
#     office_id: int | None = None,
#     limit: int = Query(100, ge=1, le=1000),
#     offset: int = Query(0, ge=0),
#     db: AsyncSession = Depends(get_db)
# ) -> dict:
#     where = []
#     params = {}
#
#     if date_from:
#         where.append(f"(doc.emission_date AT TIME ZONE '{_TZ}')::date >= :date_from")
#         params["date_from"] = date_from
#     if date_to:
#         where.append(f"(doc.emission_date AT TIME ZONE '{_TZ}')::date < :date_to")
#         params["date_to"] = date_to
#     if document_type_id:
#         where.append("doc.bsale_document_type_id = :dt_id")
#         params["dt_id"] = document_type_id
#     if office_id:
#         # filtro explicito (override) — respetar lo que pase el frontend
#         where.append("doc.bsale_office_id = :office_id")
#         params["office_id"] = office_id
#     else:
#         # filtro por defecto — solo sucursales activas
#         where.append(f"doc.bsale_office_id IN ({_OFFICE_IDS_SQL})")
#
#     where_sql = ("WHERE " + " AND ".join(where)) if where else ""
#
#     total_res = await db.execute(text(f"SELECT COUNT(*) FROM documents doc {where_sql}"), params)
#     total = total_res.scalar() or 0
#
#     params["limit"] = limit
#     params["offset"] = offset
#
#     rows_res = await db.execute(text(f"""
#         SELECT doc.bsale_document_id, doc.serial_number, doc.doc_number, doc.emission_date,
#                doc.total_amount, doc.bsale_office_id, o.name AS office_name,
#                dt.name AS document_type_name, doc.is_credit_note
#         FROM documents doc
#         LEFT JOIN offices o          ON o.bsale_office_id = doc.bsale_office_id
#         LEFT JOIN document_types dt  ON dt.bsale_document_type_id = doc.bsale_document_type_id
#         {where_sql}
#         ORDER BY doc.emission_date DESC
#         LIMIT :limit OFFSET :offset
#     """), params)
#
#     rows = [dict(r) for r in rows_res.mappings().all()]
#
#     return {"total": total, "limit": limit, "offset": offset, "items": rows}


# @router.get("/{doc_id}")
# async def get_document(doc_id: int, db: AsyncSession = Depends(get_db)) -> dict:
#     doc_res = await db.execute(text("""
#         SELECT doc.*, dt.name AS document_type_name, o.name AS office_name
#         FROM documents doc
#         LEFT JOIN document_types dt ON dt.bsale_document_type_id = doc.bsale_document_type_id
#         LEFT JOIN offices o         ON o.bsale_office_id = doc.bsale_office_id
#         WHERE doc.bsale_document_id = :doc_id
#     """), {"doc_id": doc_id})
#     doc_row = doc_res.mappings().first()
#
#     if not doc_row:
#         raise HTTPException(404, "Documento no encontrado")
#
#     doc = dict(doc_row)
#
#     detalles_res = await db.execute(text("""
#         SELECT dd.bsale_variant_id, v.code, p.name AS producto,
#                dd.quantity, dd.net_unit_value, dd.total_amount
#         FROM document_details dd
#         LEFT JOIN variants v  ON v.bsale_variant_id = dd.bsale_variant_id
#         LEFT JOIN products p  ON p.bsale_product_id = v.bsale_product_id
#         WHERE dd.bsale_document_id = :doc_id
#     """), {"doc_id": doc_id})
#
#     doc["detalles"] = [dict(r) for r in detalles_res.mappings().all()]
#     return doc


# @router.get("/stats/summary")
# async def documents_summary(db: AsyncSession = Depends(get_db)) -> dict:
#     total_docs = await db.execute(text("SELECT COUNT(*) FROM documents"))
#     total_dets = await db.execute(text("SELECT COUNT(*) FROM document_details"))
#     mas_reciente = await db.execute(text("SELECT MAX(emission_date) FROM documents"))
#     mas_antiguo = await db.execute(text("SELECT MIN(emission_date) FROM documents"))
#
#     por_tipo_res = await db.execute(text("""
#         SELECT dt.name AS tipo, COUNT(*) AS cantidad
#         FROM documents doc
#         LEFT JOIN document_types dt ON dt.bsale_document_type_id = doc.bsale_document_type_id
#         GROUP BY dt.name
#         ORDER BY cantidad DESC
#     """))
#
#     return {
#         "total_documentos": total_docs.scalar() or 0,
#         "total_detalles":   total_dets.scalar() or 0,
#         "mas_reciente":     mas_reciente.scalar(),
#         "mas_antiguo":      mas_antiguo.scalar(),
#         "por_tipo":         [dict(r) for r in por_tipo_res.mappings().all()],
#     }
