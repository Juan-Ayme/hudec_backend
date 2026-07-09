# Guía Frontend — Evolución en el tiempo 📈

Guía práctica para construir **gráficos de evolución** (cómo cambió cada
departamento, categoría, subcategoría o sucursal mes a mes) a partir de dos
endpoints nuevos del backend.

- **`GET /analytics/evolucion`** → la serie mensual (para líneas, áreas, mix 100%).
- **`GET /analytics/evolucion/resumen`** → una tarjeta de análisis por dimensión (tendencia, crecimiento, aporte).

> Los dos endpoints están también resumidos en [`FRONTEND_API.md`](FRONTEND_API.md).
> Este documento es la versión "paso a paso" para armar las visualizaciones.

---

## 1. Autenticación (igual que el resto de la API)

```http
GET /analytics/evolucion?dimension=departamento&meses=12
Authorization: Bearer <access_token>
X-Company-Id: 1
```

Todos los endpoints requieren el JWT y el header `X-Company-Id`. Nada especial
aquí respecto a las 4 vistas principales.

---

## 2. `GET /analytics/evolucion` — la serie mensual

### Parámetros

| Param | Tipo | Default | Para qué sirve |
|---|---|---|---|
| `dimension` | `departamento` \| `categoria` \| `subcategoria` \| `sucursal` | `departamento` | Qué eje analizar en el tiempo |
| `meses` | entero 3–36 | `12` | Cuántos meses mostrar |
| `office_id` | entero | (todas) | Filtra a una sucursal. **Se ignora** si `dimension=sucursal` |
| `top` | entero 0–50 | `0` | `0` = todas; `N` = deja las N de mayor venta y agrupa el resto como `"Otros"` |

> 💡 **Recomendación:** para gráficos de líneas, usar `top=6` u `8`. Con `top=0`
> categorías/subcategorías devuelven decenas o cientos de series (ilegible en un chart).

### Respuesta

Un arreglo plano (**long-format**): **una fila por mes × valor de dimensión**, sin
huecos (los meses sin venta vienen en `0`). Ya viene ordenado por `periodo` y luego
por `ventas` desc.

```json
[
  {
    "periodo": "2026-06",
    "dim": "Hogar y Decoración",
    "ventas": 30320.91,
    "unidades": 7527,
    "tickets": 812,
    "ticket_promedio": 37.34,
    "margen_soles": 7368.60,
    "margen_pct": 24.3,
    "participacion_pct": 22.4,
    "crecimiento_mom_pct": 21.5,
    "crecimiento_yoy_pct": 8.1,
    "skus_activos": 143,
    "cobertura_costos_pct": 100.0,
    "parcial": false
  },
  {
    "periodo": "2026-07",
    "dim": "Hogar y Decoración",
    "ventas": 1100.39,
    "unidades": 240,
    "…": "…",
    "parcial": true
  }
]
```

### Qué significa cada campo

| Campo | Significado |
|---|---|
| `periodo` | Mes en formato `YYYY-MM` |
| `dim` | Valor de la dimensión (o `"Otros"` si usaste `top`) |
| `ventas` | Soles vendidos ese mes (S/) |
| `unidades` | Unidades vendidas |
| `tickets` | Documentos que incluyeron esa dimensión (ver ⚠ nota) |
| `ticket_promedio` | `ventas / tickets` |
| `margen_soles` | Margen en soles (`net − unidades×costo`) |
| `margen_pct` | Margen sobre venta neta (%) |
| `participacion_pct` | **% del total del mes** (mix). Suma ~100 entre todas las `dim` de un mismo `periodo` |
| `crecimiento_mom_pct` | Variación vs **mes anterior**. `null` en el primer mes |
| `crecimiento_yoy_pct` | Variación vs **mismo mes del año pasado**. `null` si no hay historia |
| `skus_activos` | Cuántas variantes (SKU) distintas se vendieron |
| `cobertura_costos_pct` | % de unidades con costo cargado. Si es bajo, el margen no es confiable |
| `parcial` | `true` = **mes en curso, incompleto** → dibujar distinto (ver §4) |

> ⚠ **`tickets` y `skus_activos` son conteos por dimensión**: un ticket que compra
> de 2 categorías cuenta en las dos. En el bucket `"Otros"` estos dos se suman y
> pueden sobre-contar. `ventas`, `unidades` y `margen_soles` sí son exactos siempre.

---

## 3. Recetas de gráficos

El backend entrega long-format porque es lo más flexible. En el frontend solo hay
que **pivotear** `dim` sobre `periodo`.

### 3.1 Líneas de evolución (una línea por dimensión)

```
eje X = periodo (orden cronológico)
una serie por cada `dim`, valor = ventas
```

Pivot rápido en JS:

```js
const data = await fetch('/analytics/evolucion?dimension=departamento&meses=12&top=6', { headers })
  .then(r => r.json());

const periodos = [...new Set(data.map(d => d.periodo))].sort();
const dims     = [...new Set(data.map(d => d.dim))];

// { "Hogar y Decoración": {"2025-08": 30320.91, ...}, ... }
const byDim = {};
for (const row of data) {
  (byDim[row.dim] ??= {})[row.periodo] = row.ventas;
}

// Series para Recharts / Chart.js / ECharts:
const series = dims.map(dim => ({
  name: dim,
  data: periodos.map(p => byDim[dim][p] ?? 0),
}));
```

### 3.2 Área apilada 100% (cómo cambia la mezcla)

Usar **`participacion_pct`** en vez de `ventas`, con áreas apiladas al 100%.
Responde "¿qué peso tiene cada categoría y cómo se movió ese peso?".

```js
const series = dims.map(dim => ({
  name: dim,
  data: periodos.map(p =>
    data.find(d => d.dim === dim && d.periodo === p)?.participacion_pct ?? 0),
}));
```

### 3.3 Barras de crecimiento (MoM o YoY)

Usar `crecimiento_mom_pct` o `crecimiento_yoy_pct`. Colorear verde si > 0, rojo si
< 0. Ignorar los `null` (primer mes / sin base del año anterior).

### 3.4 Métricas alternativas

El mismo gráfico sirve para `unidades`, `margen_soles`, `margen_pct` o
`ticket_promedio` — solo cambia el campo que se grafica. Un selector de métrica en
la UI no requiere volver a llamar al backend.

---

## 4. El mes en curso (`parcial: true`) — ¡importante!

La **última fila de cada serie** suele ser el mes actual, que está **incompleto**
(solo lleva los días transcurridos). Si lo grafican igual que los demás, **toda
línea termina en un desplome falso**.

Reglas:

- Dibujar el tramo del mes `parcial` con **línea punteada** o marcarlo "en curso".
- No mostrar `crecimiento_mom_pct` del mes parcial como una caída real.
- Para "el último dato cerrado", tomar el último mes con `parcial === false`.

```js
const cerrados = data.filter(d => !d.parcial);
const enCurso  = data.filter(d =>  d.parcial);
```

---

## 5. `GET /analytics/evolucion/resumen` — tarjetas de análisis

Mismos parámetros que `/evolucion`. Devuelve **una tarjeta por dimensión**, ya
ordenadas por venta total desc. Los cálculos de tendencia/crecimiento usan solo
**meses completos** (excluyen el mes en curso), así que son estables.

```json
[
  {
    "dim": "Librería y Oficina",
    "venta_total": 520415.42,
    "tendencia": "CRECIENDO",
    "crecimiento_periodo_pct": 144.5,
    "participacion_actual_pct": 3.7,
    "aporte_al_crecimiento_pct": 8.3,
    "mejor_mes": { "periodo": "2026-03", "ventas": 356603.53 },
    "peor_mes":  { "periodo": "2025-10", "ventas": 1532.70 },
    "margen_pct_actual": 31.2,
    "periodo_actual": "2026-06"
  }
]
```

| Campo | Significado / uso en UI |
|---|---|
| `dim` | Nombre de la dimensión |
| `venta_total` | Venta acumulada de la ventana (incluye el mes parcial) |
| `tendencia` | `CRECIENDO` / `ESTABLE` / `DECAYENDO` → chip ▲ / ▬ / ▼ |
| `crecimiento_periodo_pct` | Cambio del primer al último mes completo |
| `participacion_actual_pct` | Peso en el último mes completo |
| `aporte_al_crecimiento_pct` | **Cuánto del crecimiento total del negocio explica** esta dimensión (suma ~100). Ordenar por aquí = "quién mueve la aguja" |
| `mejor_mes` / `peor_mes` | Mes pico y valle (solo meses completos) |
| `margen_pct_actual` | Margen % del último mes completo |
| `periodo_actual` | Qué mes se usó como referencia |

**Colores sugeridos para `tendencia`:** `CRECIENDO` 🟢 · `ESTABLE` ⚪ · `DECAYENDO` 🔴.

Uso típico: mostrar las tarjetas al lado del gráfico de líneas, y permitir ordenar
por `aporte_al_crecimiento_pct` para responder "¿qué está impulsando (o hundiendo)
la venta total?".

---

## 6. Rendimiento y caché

La consulta escanea hasta **24 meses reales** de ventas (necesita el año anterior
para el YoY). Con el volumen actual responde en **~3–10 s** según la dimensión
(sucursal es la más rápida; categoría/departamento las más pesadas).

- Cachear en el frontend por la llave `(dimension, meses, office_id, top)`.
- Los datos cambian una vez al día (sync nocturno), así que un caché de varias
  horas es seguro.
- Mostrar un skeleton/spinner mientras carga; no bloquear la vista.

---

## 7. Ejemplos de llamadas

```http
# Evolución de departamentos (12 meses), top 6 líneas + "Otros"
GET /analytics/evolucion?dimension=departamento&meses=12&top=6

# Mix de categorías de una sola sucursal, últimos 6 meses
GET /analytics/evolucion?dimension=categoria&meses=6&office_id=1&top=8

# Evolución comparando sucursales (ignora office_id)
GET /analytics/evolucion?dimension=sucursal&meses=12

# Tarjetas de análisis por categoría
GET /analytics/evolucion/resumen?dimension=categoria&meses=12&top=10
```
