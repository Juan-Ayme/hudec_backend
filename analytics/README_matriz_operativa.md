# 05 — Matriz Operativa (60d + contexto histórico)

## Propósito

Vista **operativa principal por SKU** que combina:
- Métricas de los últimos 60 días (lo "ahora")
- Contexto histórico (lifetime, tendencia, mejor mes)

Es el reporte "todo terreno" — el que más uso recomendado para análisis individual de SKUs.

## Diferencia con los otros reportes

| Característica | 04_matriz_60d | **05_matriz_operativa** | 06_historico | 07_informe |
|---|---|---|---|---|
| Ventana operativa | 60d | 60d | — | 60d+lifetime |
| Contexto histórico | ❌ | ✅ (5 columnas) | ✅ (todo) | ✅ |
| Granularidad | SKU | **SKU** | SKU | SKU + agregados |
| Filtra fantasmas | ✅ | Sí (180d) | ❌ | Parcial |

## Columnas clave que tiene

### Operativo 60d
- Unds Vend (60d), Stock, Velocidad, Proyección 30d, % Frecuencia, XYZ, Cobertura, Sell-Through

### Contexto histórico
- **Unds Lifetime** — total vendido en toda su historia
- **Recibidas Lifetime** — total comprado al proveedor
- **% Sell-Through Lifetime** — cuánto se vendió de todo lo recibido (KPI clave)
- **Mejor Mes / Uds Mejor Mes** — cuándo fue su pico
- **Tendencia (90d vs prev) + Variación %** — está subiendo o bajando

### Días desde última recepción
- Para aplicar la regla de los 45 días (lote debe rotar)

## Cuándo usar este reporte

- Análisis individual de productos (revisión detallada)
- Decisiones de reposición con contexto histórico
- Detectar "joyas ocultas" o "parásitos" sin necesidad de hacer joins
- Cuando necesitás ver "el ahora + de dónde viene" en una sola fila

## Cascada de clasificación

La misma 15 reglas del módulo 04 (con la regla 45d aplicada), pero las clasificaciones también consideran el **lifetime sell-through** para distinguir:

- 🔄 **CICLO CERRADO** (stock=0, ST lifetime ≥80%, sin venta ≥60d) — vendió todo
- ⚰️ **MUERTO HISTÓRICO** (stock=0, ST lifetime <80%, sin venta ≥60d) — vendió poco

## Cómo ejecutar

```powershell
$env:PGPASSWORD="root"
psql -h localhost -U postgres -d database_kawii_pluss -f matriz_kawii_v2.sql
```

Tiempo: ~1.2s, devuelve ~3,032 filas.

## Filtros típicos

```sql
-- Productos con baja venta reciente pero buena historia (oportunidades)
SELECT * FROM (...)
WHERE "Unds Lifetime" > 100 AND "Unds Vend (60d)" < 5
ORDER BY "Unds Lifetime" DESC;

-- Tendencia: subiendo
SELECT * FROM (...) WHERE "Tendencia (90d vs prev)" = '📈 Subiendo';

-- Solo Magdalena, ALTA ROTACIÓN
SELECT * FROM (...)
WHERE "Sucursal" = 'KAWII MAGDALENA'
  AND "Clasificación KAWII" LIKE '🔥%';
```
