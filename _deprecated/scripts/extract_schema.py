import asyncio
import json
from sqlalchemy import text
from app.database import engine

async def get_schema():
    
    async with engine.begin() as conn:
        # Get all tables
        result = await conn.execute(text("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public'
        """))
        tables = [row[0] for row in result]
        
        schema = {}
        for table in tables:
            col_result = await conn.execute(text(f"""
                SELECT column_name, data_type 
                FROM information_schema.columns 
                WHERE table_schema = 'public' AND table_name = '{table}'
            """))
            schema[table] = {row[0]: row[1] for row in col_result}
            
        print(json.dumps(schema, indent=2))
        
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(get_schema())
