import asyncio
import os
import sys

# Agregar el directorio actual al path para importar módulos de la app
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import text
from app.database import async_session_maker

async def run():
    async with async_session_maker() as session:
        query = text("""
            SELECT 
                vpf.department,
                dd.total_amount,
                dd.quantity,
                COALESCE(vco.effective_cost, vc.effective_cost) as unit_cost,
                (dd.quantity * COALESCE(vco.effective_cost, vc.effective_cost)) as total_cost,
                (dd.total_amount - dd.quantity * COALESCE(vco.effective_cost, vc.effective_cost)) as margin
            FROM document_details dd
            JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
            JOIN variants v ON v.bsale_variant_id = dd.bsale_variant_id AND v.company_id = dd.company_id
            JOIN v_products_full vpf ON vpf.bsale_product_id = v.bsale_product_id AND vpf.company_id = v.company_id
            LEFT JOIN variant_costs vc ON vc.bsale_variant_id = dd.bsale_variant_id AND vc.company_id = dd.company_id
            LEFT JOIN variant_costs_by_office vco ON vco.bsale_variant_id = dd.bsale_variant_id
                AND vco.company_id = dd.company_id AND vco.bsale_office_id = doc.bsale_office_id
            WHERE dd.total_amount > 0 AND dd.quantity > 0
              AND COALESCE(vco.effective_cost, vc.effective_cost) IS NOT NULL
              AND dd.total_amount - dd.quantity * COALESCE(vco.effective_cost, vc.effective_cost) > 0
            ORDER BY doc.emission_date DESC
            LIMIT 1
        """)
        res = await session.execute(query)
        row = res.fetchone()
        
        if row:
            print("--- PRODUCTO DE EJEMPLO ---")
            print(f"Departamento: {row.department}")
            print(f"Venta Neta (Total): {float(row.total_amount):.2f}")
            print(f"Cantidad Vendida: {row.quantity}")
            print(f"Costo Unitario Efectivo: {float(row.unit_cost):.2f}")
            print(f"Costo Total: {float(row.total_cost):.2f}")
            print(f"Margen Generado: {float(row.margin):.2f}")
            print(f"Margen Porcentual: {round((float(row.margin) / float(row.total_amount))*100, 2)}%")
        else:
            print("No se encontraron productos con costos para ejemplificar.")

if __name__ == "__main__":
    asyncio.run(run())
