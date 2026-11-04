import asyncio
from sqlalchemy import text
from app.database import engine

async def query():
    async with engine.begin() as conn:
        sku = 'PL-01'
        
        # Check sales price by office
        print(f"--- SALES PRICE BY OFFICE FOR {sku} ---")
        res = await conn.execute(text(f"""
            SELECT doc.bsale_office_id, 
                   COUNT(dd.bsale_detail_id) as sales_count,
                   MIN(dd.total_amount/dd.quantity) as min_price,
                   MAX(dd.total_amount/dd.quantity) as max_price
            FROM document_details dd
            JOIN variants v ON v.bsale_variant_id = dd.bsale_variant_id
            JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id
            WHERE v.display_code = '{sku}' AND dd.quantity > 0 AND dd.is_gratuity = FALSE
            GROUP BY doc.bsale_office_id
        """))
        for row in res.fetchall():
            print(f"Office: {row[0]} | Times Sold: {row[1]} | Min Price: {float(row[2])} | Max Price: {float(row[3])}")
            
        # Check offices table schema
        res = await conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='offices'"))
        cols = [r[0] for r in res.fetchall()]
        print(f"\n--- OFFICES TABLE COLUMNS ---")
        print(cols)

if __name__ == "__main__":
    asyncio.run(query())
