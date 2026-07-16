"""Test directo del SQL 09_costos_por_sucursal contra la DB."""
import asyncio
import sys
import os
from pathlib import Path

# Force UTF-8
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main():
    from app.database import engine
    from sqlalchemy import text

    sql_path = Path(__file__).resolve().parent.parent / "app" / "kawii_matrix" / "sql" / "09_costos_por_sucursal.sql"
    sql_text = sql_path.read_text(encoding="utf-8")

    params = {
        "days": 90,
        "sucursales_objetivo": [1, 3],
        "umbral_margen_bajo": 20.0,
        "umbral_margen_alto": 70.0,
        "umbral_outlier_pct": 50.0,
        "umbral_desactualizado_pct": 20.0,
        "umbral_ratio_max_min": 2.0,
    }

    async with engine.connect() as conn:
        # Set tenant context (company_id = 1 = Hudec)
        await conn.execute(text("SET LOCAL app.current_company = '1'"))

        try:
            result = await conn.execute(text(sql_text), params)
            rows = result.mappings().all()
        except Exception as e:
            err_msg = str(e)
            # Buscar la linea del error SQL real
            print(f"ERROR SQL: {type(e).__name__}")
            print(err_msg[:1000])
            await engine.dispose()
            return

    # Resumen
    total = len(rows)
    errores = sum(1 for r in rows if r["severidad"] == "ERROR")
    warnings = sum(1 for r in rows if r["severidad"] == "WARNING")
    ok_count = sum(1 for r in rows if r["severidad"] == "OK")

    print("=" * 60)
    print(f"RESULTADO: {total} filas")
    print(f"  OK:      {ok_count}")
    print(f"  WARNING: {warnings}")
    print(f"  ERROR:   {errores}")
    print("=" * 60)

    # Conteo de alertas
    from collections import Counter
    alertas_counter = Counter()
    for r in rows:
        if r["alertas"]:
            for a in r["alertas"]:
                alertas_counter[a] += 1

    if alertas_counter:
        print("\nAlertas por tipo:")
        for alerta, count in alertas_counter.most_common():
            print(f"  {alerta}: {count}")

    # Muestra de errores
    errores_sample = [r for r in rows if r["severidad"] == "ERROR"][:5]
    if errores_sample:
        print(f"\nMuestra de ERRORES (primeros {len(errores_sample)}):")
        for r in errores_sample:
            print(f"  {r['codigo_sku']} | {r['sucursal']} | costo={r['costo_efectivo']} ({r['tabla_costo']}) | precio={r['precio_venta']} ({r['tabla_precio']}) | alertas={list(r['alertas'])}")

    # Muestra de warnings
    warnings_sample = [r for r in rows if r["severidad"] == "WARNING"][:5]
    if warnings_sample:
        print(f"\nMuestra de WARNINGS (primeros {len(warnings_sample)}):")
        for r in warnings_sample:
            print(f"  {r['codigo_sku']} | {r['sucursal']} | costo={r['costo_efectivo']} ({r['tabla_costo']}) | precio={r['precio_venta']} ({r['tabla_precio']}) | margen={r['margen_pct']}% | alertas={list(r['alertas'])}")

    # Muestra de OK
    ok_sample = [r for r in rows if r["severidad"] == "OK"][:3]
    if ok_sample:
        print(f"\nMuestra de OK (primeros {len(ok_sample)}):")
        for r in ok_sample:
            print(f"  {r['codigo_sku']} | {r['sucursal']} | costo={r['costo_efectivo']} | precio={r['precio_venta']} | margen={r['margen_pct']}%")

    # Caso de prueba: variante 2819 (PL-01)
    pl01 = [r for r in rows if r.get("bsale_variant_id") == 2819]
    if pl01:
        print(f"\nCASO DE PRUEBA - Variante 2819 (PL-01):")
        for r in pl01:
            print(f"  Sucursal: {r['sucursal']} | costo={r['costo_efectivo']} | origen={r['costo_origen']} | precio={r['precio_venta']} | margen={r['margen_pct']}% | severidad={r['severidad']} | alertas={list(r['alertas'])}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
