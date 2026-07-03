# 04 — Matriz 60d (snapshot puro del "ahora")

## Propósito

Foto **pura de los últimos 60 días** sin mezclar con datos históricos. Responde la pregunta: **"¿qué está pasando AHORA con mi catálogo activo?"**

A diferencia del informe consolidado (que mezcla histórico), esta consulta filtra **fantasmas** (SKUs sin actividad reciente) para que solo veas lo operacionalmente relevante.

## Diferencia con los otros reportes

| Reporte | Ventana | Filtra fantasmas | Histórico |
|---|---|---|---|
| **04_matriz_60d** | 60 días | ✅ Sí | ❌ No |
| 05_matriz_operativa | 60d + lifetime | Parcial | ✅ Sí (columnas extra) |
| 06_historico_productos | Lifetime | ❌ No | ✅ Sí |
| 07_informe_consolidado | Lifetime | Parcial | ✅ Sí |

## Cuándo usar este reporte

- **Lunes a la mañana** para ver "qué pasó la semana pasada"
- Reuniones operativas semanales con encargados
- Cuando querés ignorar el ruido del histórico
- Para auditar el catálogo ACTIVO (no el muerto)

## Cascada de clasificación

Con la **regla de los 45 días** aplicada, la cascada es:

1. 🌱 **NUEVO** (edad ≤ 15d)
2. 🚨 **LOTE VENDIDO RÁPIDO** (stock=0, lote vendió ≥80% en ≤30d)
3. 💀 **MUERTO 60D** (stock>0, sin ventas en 60d)
4. ❓ **RECIBIDO Y NO VENDIDO** (recibió en 60d, no vendió, stock=0)
5. 👀 **ALERTA VISUAL** (stock 1-2, sin venta 16-59d)
6. 🚨 **QUIEBRE STOCK X/Y/Z** (stock=0, venta últimos 14d, proy ≥10)
7. 👻 **AGOTADO HACE TIEMPO** (stock=0, sin venta 15-59d)
8. 🐢 **BAJA ROTACIÓN (lote sin rotar >45d)** ← regla de permanencia
9. 👻 **FALSO AGOTADO** (stock=0, proy <10)
10. ⚠️ **STOCK CRÍTICO + BAJA ROT**
11. 🐢 **BAJA ROTACIÓN**
12. 🔥 **ALTA ROTACIÓN X/Y/Z**
13. ⚡ **MEDIA ROTACIÓN**
14. 🟢 **INVENTARIO SANO**
15. 🧊 **EXCESO DE INVENTARIO**

## Cómo ejecutar

```powershell
$env:PGPASSWORD="root"
psql -h localhost -U postgres -d database_kawii_pluss -f matriz_kawii_60d.sql
```

Tiempo: ~0.5s, devuelve ~1,589 filas.

## Filtros típicos

```sql
-- Solo lo urgente
SELECT * FROM (...) WHERE "Clasificación KAWII" LIKE '🚨%';

-- Por sucursal
SELECT * FROM (...) WHERE "Sucursal" = 'KAWII MAGDALENA';

-- Productos con lote sin rotar (regla 45d)
SELECT * FROM (...) WHERE "Clasificación KAWII" LIKE '%lote sin rotar%';
```
