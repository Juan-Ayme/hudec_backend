# 📦 Reporte de Salud de Inventario HUDEC — v2
**Generado:** 2026-05-26 11:04  |  **Ventana de análisis:** 90 días  |  **Horizonte de reposición:** 30 días

> **Metodología:** Semáforo biaxial — combina *velocidad de venta real* (últimos 90 días) con *valor económico al precio de venta*. No usa costos de producto.

### 📖 Guía de Diagnósticos
| Diagnóstico | Significado |
|---|---|
| 🔥 QUIEBRE_INMINENTE | Se agota en < 30 días Y vende rápido. Reponer YA. |
| ⚠️ QUIEBRE_MODERADO | Se agota en < 30 días, venta media. Evaluar reposición. |
| ☠️ STOCK_MUERTO | Tiene unidades físicas pero NO vendió nada en 90 días. |
| 🧟 ESTANCADO | Stock para > 4 meses y lleva > 30 días sin venderse. |
| 🐢 LENTO | Vende poco y tiene stock para > 3 meses. Candidato a liquidar. |
| 🟡 FALSO_QUIEBRE | Se acaba pronto pero vende casi nada. No comprar urgente. |
| 📦 SOBRE_STOCK_ACTIVO | Vende bien pero tiene exceso de stock (> 3 meses). Redistribuir. |
| 🟢 SANO | Inventario equilibrado (30–90 días de stock). Ideal. |
| ✨ NUEVO | Ingresó hace < 90 días. Sin penalización de rotación aún. |
| ⚪ SIN_STOCK | Agotado en tiendas y almacén. |
| ❓ INDETERMINADO | No se pudo clasificar con la información disponible. |

---
## 1. Resumen Global de Salud del Inventario

| Diagnóstico | SKUs | Uds en Inventario | Valor en Stock (S/) | % Catálogo |
|---|---|---|---|---|
| **TOTAL** | **0** | | **—** | 100% |

---
## 2. 🔥 Alertas de Quiebre — Productos que se Agotarán en < 30 Días

*0 SKUs en riesgo — Ingreso proyectado en riesgo: **—***

| SKU | Producto | Cat. | Precio Vta | Stock T | Stock A | Vta/mes | Días Stock | Ingreso en Riesgo | Estado |
|---|---|---|---|---|---|---|---|---|---|

---
## 3. 💸 Capital Atrapado — Prioridad de Liquidación

*0 SKUs con capital inmovilizado — Total estimado: **—***

| SKU | Producto | Cat. | Estado | Precio Vta | Stock Total | Valor (S/) | Días sin Vender | Antigüedad |
|---|---|---|---|---|---|---|---|---|

---
## 5. Auditoría Completa de SKUs

| SKU | Producto | Cat | Precio Vta | Stock T | Stock A | Ingr. Diario | Días Stock | Días Sin Vta | Diagnóstico |
|---|---|---|---|---|---|---|---|---|---|

---
*HUDEC Analytics — 2026-05-26 11:04*