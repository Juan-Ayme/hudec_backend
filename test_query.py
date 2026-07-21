import asyncio
import json
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

async def run_query():
    engine = create_async_engine(
        'postgresql+asyncpg://hudec_app:MD8N7foHgv3pm8SXzAHg8DFU497d2n6Xn6RxoC5F8apWH3oTu384DUErqF7phNfC@ep-winter-unit-ah31xvz6-pooler.c-3.us-east-1.aws.neon.tech:5432/hudec_bd', 
        connect_args={'ssl': True}
    )
    
    query = """
    WITH sales AS (
        SELECT 
            dd.bsale_variant_id,
            SUM(dd.quantity) as unidades_vendidas,
            SUM(dd.total_amount) as total_ventas
        FROM document_details dd
        JOIN documents d ON dd.bsale_document_id = d.bsale_document_id
        WHERE d.is_active = true 
          AND d.bsale_document_type_id IN (1, 2, 3) -- Assuming these are sales types
          AND d.is_credit_note = false
        GROUP BY dd.bsale_variant_id
    )
    SELECT 
        p.department,
        p.category,
        SUM(s.unidades_vendidas) as total_unidades_vendidas,
        SUM(s.total_ventas) as ingresos_totales,
        SUM(s.unidades_vendidas * COALESCE(vc.effective_cost, 0)) as costo_total,
        SUM(s.total_ventas) - SUM(s.unidades_vendidas * COALESCE(vc.effective_cost, 0)) as margen_absoluto,
        CASE 
            WHEN SUM(s.total_ventas) > 0 
            THEN (SUM(s.total_ventas) - SUM(s.unidades_vendidas * COALESCE(vc.effective_cost, 0))) / SUM(s.total_ventas) * 100 
            ELSE 0 
        END as margen_porcentaje
    FROM sales s
    JOIN variants v ON s.bsale_variant_id = v.bsale_variant_id
    JOIN v_products_full p ON v.bsale_product_id = p.bsale_product_id
    LEFT JOIN variant_costs vc ON s.bsale_variant_id = vc.bsale_variant_id
    GROUP BY p.department, p.category
    ORDER BY margen_absoluto DESC
    LIMIT 20;
    """
    
    async with engine.begin() as conn:
        result = await conn.execute(text(query))
        rows = [dict(zip(result.keys(), row)) for row in result]
        
        # Convert Decimals to float for JSON serializability
        def default(o):
            import decimal
            if isinstance(o, decimal.Decimal):
                return float(o)
            raise TypeError

        print(json.dumps(rows, default=default, indent=2))
        
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(run_query())
