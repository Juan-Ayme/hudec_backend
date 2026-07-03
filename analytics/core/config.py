"""
Constantes globales para todos los modulos de analisis.
Ajusta aqui si cambian los parametros del negocio.
"""

# ── Parametros de inventario ──────────────────────────────────────────────────

# Nivel de servicio 95%  →  z = 1.65
#   Si quieres 98% usa z = 2.05; 99% -> z = 2.33
SERVICE_LEVEL_Z: float = 1.65

# Lead time en dias (tiempo desde que ordenas hasta que recibes)
LEAD_TIME_DAYS: int = 7

# Costo fijo de pedido en CLP (transporte + admin)
ORDERING_COST_CLP: float = 5_000.0

# Tasa de costo de mantener inventario (anual, sobre el costo del producto)
# 20% incluye: financiamiento + almacenaje + merma + obsolescencia
HOLDING_RATE: float = 0.20

# ── Parametros ABC ────────────────────────────────────────────────────────────
# Clasifica segun % acumulado de ingresos (Pareto)
ABC_A_THRESHOLD: float = 0.80   # productos que generan el 80% del revenue → A
ABC_B_THRESHOLD: float = 0.95   # hasta el 95% → B  |  el resto → C

# ── Parametros XYZ (Coeficiente de Variacion) ────────────────────────────────
XYZ_X_MAX_CV: float = 0.50   # CV < 0.50  → X (demanda estable)
XYZ_Y_MAX_CV: float = 1.00   # 0.50 ≤ CV < 1.00 → Y (variable)
                              # CV ≥ 1.00  → Z (erratica)

# ── Ventanas de tiempo ────────────────────────────────────────────────────────
WEEKS_WMA: int = 13       # semanas para la WMA
WEEKS_TREND: int = 13     # semanas para la pendiente de tendencia
DAYS_SHORT: int = 30      # rotacion corta (rot_30d)
DAYS_LONG: int = 90       # rotacion larga (rot_90d)

# ── Umbrales de tendencia ─────────────────────────────────────────────────────
# Si la pendiente de la regresion lineal (unidades/semana) supera esto:
TREND_SLOPE_THRESHOLD: float = 0.10

# ── Sucursales activas ────────────────────────────────────────────────────────
# Solo se incluyen las dos tiendas operativas en TODOS los analisis.
# ID 1 = KAWII MAGDALENA  |  ID 3 = KAWII ASAMBLEA
# Si se abre una sucursal nueva, agrega su bsale_office_id aqui.
OFFICE_IDS: tuple[int, ...] = (1, 3)

# ── Display ───────────────────────────────────────────────────────────────────
CONSOLE_MAX_ROWS: int = 50   # filas a mostrar en tablas por consola
