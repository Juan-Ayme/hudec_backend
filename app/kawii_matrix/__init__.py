"""
KAWII Matrix — Sistema de clasificación inteligente de SKUs.

Expone 4 vistas analíticas:
  - 04 Matriz 90d:           foto operativa del "ahora" (90 días)
  - 05 Matriz Operativa:     matriz enriquecida con contexto lifetime
  - 06 Histórico Productos:  análisis de ciclo de vida completo (lifetime)
  - 07 Informe Consolidado:  vista jerárquica DEPT→CAT→SUBCAT→SKU + ABC

Cada SKU se clasifica con una de ~26 etiquetas accionables:
  💎 EXITOSO AGOTADO / 🔥 ALTA ROTACIÓN / 🐢 BAJA ROTACIÓN /
  🚨 QUIEBRE STOCK / 🪦 AGOTADO MARGINAL / 🧊 EXCESO INVENTARIO ...

Incluye sugerencias automáticas de transferencia inter-sucursal
y análisis de tendencia (45d vs 45d previos).
"""
