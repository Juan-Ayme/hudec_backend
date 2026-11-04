import os
from dotenv import load_dotenv
import psycopg2

load_dotenv(r"C:\Users\juana\Documents\kawii_analisis\backend_hudec\.env")
cfg = dict(
    host=os.environ["DB_HOST"], 
    port=int(os.environ["DB_PORT"]),
    dbname=os.environ["DB_NAME"], 
    user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"]
)
if "require" in os.environ.get("DB_SSLMODE", ""):
    cfg["sslmode"] = "require"

conn = psycopg2.connect(**cfg)
conn.autocommit = True
cur = conn.cursor()
cur.execute("SET app.current_company='1';")

q = """
SELECT v.display_code AS sku,
       SUM(dd.total_amount) FILTER (WHERE NOT dd.is_gratuity) AS venta,
       SUM(dd.quantity * COALESCE(vco.effective_cost, vc.effective_cost))
         FILTER (WHERE NOT dd.is_gratuity AND COALESCE(vco.effective_cost, vc.effective_cost) IS NOT NULL) AS costo,
       SUM(dd.quantity) FILTER (WHERE NOT dd.is_gratuity) AS unds
FROM document_details dd
JOIN documents doc ON doc.bsale_document_id = dd.bsale_document_id AND doc.company_id = dd.company_id
JOIN variants v ON v.bsale_variant_id = dd.bsale_variant_id AND v.company_id = dd.company_id
LEFT JOIN variant_costs vc ON vc.bsale_variant_id = dd.bsale_variant_id AND vc.company_id = dd.company_id
LEFT JOIN variant_costs_by_office vco ON vco.bsale_variant_id = dd.bsale_variant_id AND vco.company_id = dd.company_id AND vco.bsale_office_id = doc.bsale_office_id
WHERE dd.company_id = 1
  AND v.display_code = 'PL-01'
  AND (%(oid)s::int IS NULL OR doc.bsale_office_id = %(oid)s::int)
GROUP BY v.display_code
"""
for oid in (1, 3, None):
    cur.execute(q, {"oid": oid})
    got = False
    for r in cur.fetchall():
        got = True
        sku, venta, costo, unds = r
        venta=float(venta or 0); costo=float(costo or 0); unds=float(unds or 0)
        margen = venta - costo
        margen_pct = (margen/venta*100) if venta else 0
        etq = f"oficina {oid}" if oid else "TODAS las oficinas"
        print(f"  [{etq}] venta={venta:.2f} costo={costo:.2f} unds={unds:.0f} "
              f"costo/u={costo/unds if unds else 0:.4f} margen={margen:.2f} ({margen_pct:.1f}%)")
    if not got:
        print(f"  [oficina {oid}] sin ventas de PL-01 en esa oficina")

cur.close()
conn.close()
print("DONE")
