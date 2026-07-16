"""Script para investigar cómo se almacena el costo de una variante específica en BSale."""
import asyncio
import os
import sys
from pathlib import Path

# Force UTF-8
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

async def main():
    from app.database import engine
    from sqlalchemy import text
    from harvester import bsale_client
    from harvester.tenant_context import set_current_tenant

    sku = "360229"

    async with engine.connect() as conn:
        # 1. Obtener la variante local y el token de la empresa
        print(f"Buscando variante {sku} en la DB local...")
        result = await conn.execute(
            text("SELECT bsale_variant_id, display_code FROM variants WHERE display_code = :sku LIMIT 1"),
            {"sku": sku}
        )
        variant = result.fetchone()
        
        if not variant:
            print(f"No se encontró la variante {sku} en la DB local.")
            return

        bsale_variant_id = variant.bsale_variant_id
        print(f"✅ Variante encontrada. Bsale ID: {bsale_variant_id}")

        # Obtener el token de Hudec (company_id = 1)
        from dotenv import load_dotenv
        load_dotenv()
        encryption_key = os.environ.get("TOKEN_ENCRYPTION_KEY")
        
        result = await conn.execute(
            text("SELECT pgp_sym_decrypt(bsale_token, :key)::text AS token FROM companies WHERE id = 1"),
            {"key": encryption_key}
        )
        company = result.fetchone()
        if not company or not company.token:
            print("No se encontró el token de BSale para la empresa 1.")
            return

        token = company.token
        
    # Establecer el token para el cliente de Bsale
    set_current_tenant(1, token, "hudec")

    # 2. Consultar a BSale el endpoint de variantes general
    url_variant = f"https://api.bsale.io/v1/variants/{bsale_variant_id}.json"
    print(f"\nConsultando Bsale (Variante general): {url_variant}")
    variant_data = bsale_client.fetch(url_variant)
    
    import json
    print("\n--- JSON de Variante desde BSale ---")
    print(json.dumps(variant_data, indent=2, ensure_ascii=False))

    # 3. Consultar a BSale el endpoint de costos si existe
    url_costs = f"https://api.bsale.io/v1/variants/{bsale_variant_id}/costs.json"
    print(f"\nConsultando Bsale (Costos de variante): {url_costs}")
    costs_data = bsale_client.fetch(url_costs)
    
    print("\n--- JSON de Costos desde BSale ---")
    print(json.dumps(costs_data, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    asyncio.run(main())
