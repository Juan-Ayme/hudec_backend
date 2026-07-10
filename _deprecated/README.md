# _deprecated — código archivado (no vivo)

Código que **funciona pero ya nadie invoca**, movido aquí en la limpieza de 2026-07-10
para sacarlo del código vivo sin perderlo. No se importa desde la app ni desde el cron.
Nada aquí se ejecuta en producción. Si algo vuelve a hacer falta, restaurarlo con git.

- `run_daily_sync.py` — wrapper redundante de `python -m tools.maintenance.mt_sync`.
- `analytics/generar_reporte_avanzado.py` — apuntaba a otro proyecto/DB (rutas hardcodeadas).
- `analytics/inventory_report.py` (+ `reporte_inventario.md`) — CLI de inventario sin caller.
