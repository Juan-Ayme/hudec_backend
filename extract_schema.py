import asyncio
import json
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import MetaData, text

async def get_schema():
    engine = create_async_engine(
        'postgresql+asyncpg://hudec_app:MD8N7foHgv3pm8SXzAHg8DFU497d2n6Xn6RxoC5F8apWH3oTu384DUErqF7phNfC@ep-winter-unit-ah31xvz6-pooler.c-3.us-east-1.aws.neon.tech:5432/hudec_bd', 
        connect_args={'ssl': True}
    )
    
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
