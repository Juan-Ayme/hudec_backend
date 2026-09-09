# API para Frontend — Kawii Backend

Documentación de los endpoints del módulo de **business intelligence** que consume el
frontend. Cubre las 4 vistas principales del dashboard + los endpoints admin de
configuración.

**Base URL** (ejemplo): `https://api.kawii.com.pe`

---

## Índice

- [Conceptos clave](#conceptos-clave)
- [Autenticación](#autenticación)
- [Vista 1 — Pulso (`GET /pulse`)](#vista-1--pulso-get-pulse)
- [Vista 2 — Diagnóstico (`GET /diagnosis`)](#vista-2--diagnóstico-get-diagnosis)
- [Vista 3 — Salud del catálogo (`GET /catalog-health`)](#vista-3--salud-del-catálogo-get-catalog-health)
- [Vista 4 — Plan del mes (`GET /plan`)](#vista-4--plan-del-mes-get-plan)
- [Compras & Catálogo (`GET /analytics/compras-catalogo`)](#compras--catálogo-get-analyticscompras-catalogo)
- [Reporte de Ventas (`GET /analytics/reporte-ventas`)](#reporte-de-ventas-get-analyticsreporte-ventas)
- [Evolución en el tiempo (`GET /analytics/evolucion`)](#evolución-en-el-tiempo-get-analyticsevolucion)
- [Admin — Metas del mes (`/analytics/goals`)](#admin--metas-del-mes-analyticsgoals)
- [Admin — Category Targets (`/config/category-targets`)](#admin--category-targets-configcategory-targets)
- [Admin — Variant Costs (`/config/variant-costs`)](#admin--variant-costs-configvariant-costs)
- [Flujo de onboarding para empresa nueva](#flujo-de-onboarding-para-empresa-nueva)
- [Glosario](#glosario)

---

## Conceptos clave

### El patrón Opción B — Total vs Recurrente

En **todas** las vistas, cuando hay categorías marcadas como estacionales (config en
`app_config.excluded_departments` / `excluded_categories`), los números vienen en
**dos versiones**:

- **`total`** = la venta real, completa (incluye estacional). Es lo que el dueño cobró.
- **`recurrente`** = la venta de la **base operativa**, excluye categorías estacionales.

#### ¿Cuál usar en la UI?

| Para... | Usar |
|---|---|
| Mostrar venta del mes / del día | `total` |
| Mostrar avance vs meta del dueño | `total` (la meta se carga en venta total) |
| Mostrar drivers / "¿qué cayó?" | `recurrente` |
| Veredicto / alertas | `recurrente` (evita falsos positivos por fechas que pasaron) |

**Regla rápida**: el dueño cobra **total**, pero **opera en recurrente**. Mostrar los
dos en el header del dashboard, con el recurrente como sub-número.

---

### Cobertura de costos

Cada vista trae en `meta.cobertura_costos`:

```json
{
  "pct_actual": 99.87,
  "estado":     "OK",          // OK ≥90  |  ADVERTENCIA 70-89  |  CRITICA <70
  "warning":    null
}
```

Si baja del 90%, el campo `warning` viene con una frase accionable que linkea al
backfill. **Mostrar siempre el warning si no es null** — el margen reportado puede
estar distorsionado.

---

### Estados estandarizados

Los endpoints usan estos códigos en `estado`, `veredicto.codigo`, `severidad`, etc.
El frontend puede mapearlos a colores/iconos:

#### Estados de meta (en `mes_en_curso` y por categoría)

| Código | Significado | Color sugerido |
|---|---|---|
| `META_CUMPLIDA` | venta ≥ meta | 🟢 verde |
| `ADELANTADO` | ritmo > 105% | 🔵 azul |
| `EN_RITMO` | ritmo 95-105% | 🟢 verde |
| `ATRASADO_LEVE` | ritmo 80-94% | 🟡 amarillo |
| `RIESGO_NO_LLEGAR` | ritmo < 80% | 🔴 rojo |
| `SIN_META` | sin meta cargada | ⚪ gris |

#### Veredictos (en `/diagnosis`, `/pulse`)

| Código | Lectura |
|---|---|
| `CRECIENDO_FUERTE` | YoY ↑ y vs 4 semanas ↑ — momentum confirmado |
| `BAJON_ESTACIONAL_NORMAL` | YoY ↑ pero vs 4 semanas ↓ — caída es estacional, OK |
| `PROBLEMA_REAL` | YoY ↓ y vs 4 semanas ↓ — accionar |
| `ESTANCAMIENTO` | YoY ↓ pero vs 4 semanas ↑ — recuperación parcial |

#### Severidad de alertas (en `/pulse.alertas`)

| Severidad | Cuándo |
|---|---|
| `CRITICA` | Quiebres con demanda activa importante |
| `ALTA` | Categorías cayendo >30%, devoluciones spike |
| `MEDIA` | Día anómalo |

---

## Autenticación

Todos los endpoints requieren **JWT en el header**:

```http
Authorization: Bearer <token>
```

- Login: `POST /auth/login` con `{username, password}` → devuelve `{access_token}`.
- Endpoints admin (PUT/POST/DELETE) requieren rol **operador** o **admin**.

---

# Las 4 Vistas Principales

---

## Vista 1 — Pulso (`GET /pulse`)

**Pregunta que responde**: *¿Cómo voy hoy?*  
**Tiempo de lectura**: 10 segundos. Vistazo rápido al iniciar el día.

### Parámetros

| Param | Tipo | Default | Comentario |
|---|---|---|---|
| `office_id` | int? | `null` | ID de sucursal. Sin valor = todas las activas. |

### Ejemplo de uso

```http
GET /pulse?office_id=1
```

### Shape de respuesta

```jsonc
{
  "meta": {
    "fecha": "2026-06-30",
    "office_id": 1,
    "office_scope": [1],
    "hoy_excluido": true,
    "ultimo_dia_cerrado": "2026-06-29",
    "datos_sync_hasta": "2026-06-30T08:42:00Z",
    "generado_at": "2026-06-30T10:15:00Z",
    "exclusiones": {
      "departamentos": ["Temporada y Celebraciones", "Juguetes y Juegos", "Librería y Oficina"],
      "categorias": [],
      "nota": "Las alertas/drivers operan sobre venta RECURRENTE. Las metas y avance % se calculan sobre venta TOTAL."
    },
    "cobertura_costos": { "pct_actual": 99.87, "estado": "OK", "warning": null }
  },

  // ── Estado del mes en curso vs meta ──
  "mes_en_curso": {
    "mes": "2026-06",
    "meta_source": "exacta",
    "dias_transcurridos": 29,
    "dias_del_mes": 30,
    "dias_restantes": 1,
    "ultimo_dia_cerrado": "2026-06-29",
    "global": {
      "office_id": 1,
      "sucursal": "KAWII MAGDALENA",
      "venta_acumulada": 105363.05,             // ← usar este para mostrar "ya vendí X"
      "venta_acumulada_recurrente": 92800.40,
      "meta": 125000.0,
      "meta_prorrateada": 116666.67,
      "gap_a_meta": -19636.95,                  // ← venta − meta: negativo = falta para la meta
      "avance_pct": 84.3,                       // ← "vas al 84% de la meta"
      "cumplimiento_vs_ritmo_pct": 90.3,        // ← "vs ritmo necesario, vas al 90%"
      "proyeccion_cierre_mes": 112888.98,
      "venta_diaria_necesaria": 9818.47,
      "estado": "ATRASADO_LEVE"                 // ← determina el color del card
    },
    "por_sucursal": [/* mismas filas por cada sucursal del scope */]
  },

  // ── Veredicto del momento (sobre venta recurrente) ──
  "veredicto": {
    "codigo": "BAJON_ESTACIONAL_NORMAL",
    "titulo": "Caída estacional, no problema",
    "explicacion": "Estás arriba del año pasado pero abajo del último mes...",
    "delta_yoy_pct": 140.25,
    "delta_mes_anterior_pct": -6.62,
    "base_calculo": "venta_recurrente",
    "ventana": { /* fechas y montos del comparable */ }
  },

  // ── Último día cerrado (ayer) ──
  "ultimo_dia_cerrado": {
    "fecha": "2026-06-29",
    "dia_semana": "Lun",
    "ventas": 5248.28,                          // total
    "ventas_recurrente": 4973.10,
    "tickets": 324,
    "ticket_promedio": 16.2,
    "ventas_promedio_mismo_dow": 6246.26,       // promedio últ. 8 lunes
    "delta_vs_promedio_dow_pct": -15.98,
    "z_score": -0.66,
    "anomalo": false,                           // ← si true, mostrar badge "día anómalo"
    "n_dias_comparacion": 8,
    "base_calculo": "venta_recurrente"
  },

  // ── Semana en curso día a día ──
  "semana_en_curso": [
    {
      "fecha": "2026-06-29",
      "dia": "Lun",
      "ventas": 5248.28,
      "ventas_recurrente": 4973.10,
      "tickets": 324,
      "ticket_promedio": 16.2
    }
    // ... mar, mié, ... hasta ayer
  ],

  // ── Resumen últ. 7 días (link a /diagnosis para detalle) ──
  "ultimos_7_dias": {
    "from": "2026-06-23",
    "to": "2026-06-29",
    "ventas": 36479.42,
    "ventas_recurrente": 34454.20,
    "tickets": 2395,
    "ticket_promedio": 15.23,
    "delta_vs_semana_anterior_pct_total": -25.39,
    "delta_vs_semana_anterior_pct_recurrente": -10.73,
    "delta_vs_ano_anterior_pct_total": 88.72,
    "delta_vs_ano_anterior_pct_recurrente": 94.48
  },

  // ── Alertas top 3-5 ordenadas por severidad + impacto ──
  "alertas": [
    {
      "severidad": "CRITICA",
      "tipo": "quiebres_alta_rotacion",
      "titulo": "25 SKUs en quiebre con demanda activa",
      "detalle": "Venta perdida estimada últimos 7 días: S/ 1,504.",
      "impacto_pen": -1504.11,
      "accion_sugerida": "Revisar reposición urgente — listado en /diagnosis.factores_adicionales",
      "skus": ["GF-3589", "ZMPS-1", "FD34-P54"]
    },
    {
      "severidad": "ALTA",
      "tipo": "categoria_cayendo",
      "titulo": "Empaque y Regalo cayó 85%",
      "detalle": "S/ 158 vs S/ 1,083 la semana anterior (Hogar y Decoración).",
      "impacto_pen": -924.2,
      "accion_sugerida": "Revisar stock, precio o cambio de demanda en /diagnosis"
    }
  ]
}
```

### Cómo renderizarlo

1. **Header** con `mes_en_curso.global.venta_acumulada` y avance %, color según `estado`.
2. **Card de veredicto** con el `titulo` y la `explicacion`. Color según `codigo`.
3. **Lista de alertas** ordenadas como vienen (ya están priorizadas).
4. **Mini-gráfico de barras** con `semana_en_curso` (día por día).
5. Botón **"Ver por qué"** → navega a `/diagnosis`.

---

## Vista 2 — Diagnóstico (`GET /diagnosis`)

**Pregunta que responde**: *¿Por qué vendo menos hoy?*  
**Foco**: drivers, descomposición de la caída, comparativas YoY.

### Parámetros

| Param | Tipo | Default | Comentario |
|---|---|---|---|
| `days` | int (1-90) | 7 | Tamaño de la ventana actual (excluye HOY). Recomendado múltiplo de 7. |
| `office_id` | int? | `null` | Filtra por sucursal. |
| `top_n` | int (1-50) | 10 | Cuántos ítems por lista (categorías, vendedores, SKUs). |

### Ejemplo de uso

```http
GET /diagnosis?days=7&top_n=10
```

### Shape de respuesta (resumido)

```jsonc
{
  "meta": {
    "current": {"from": "2026-06-23", "to": "2026-06-29", "dias": 7},
    "semana":  {"from": "2026-06-16", "to": "2026-06-22", "dias": 7, "shift_dias": 7},
    "base_4w": {"from": "2026-05-26", "to": "2026-06-22", "dias": 28, "normalizado_a_dias": 7},
    "yoy":     {"from": "2025-06-24", "to": "2025-06-30", "dias": 7},
    "office_id": null,
    "office_scope": [1, 3],
    "exclusiones": { "departamentos": [...], "categorias": [...], "nota": "..." },
    "cobertura_costos": { "pct_actual": 99.87, "estado": "OK", "warning": null },
    "alertas": [
      {
        "tipo": "feriados_desbalanceados_yoy",
        "mensaje": "Distinta cantidad de feriados vs YoY — el delta puede estar sesgado."
      }
    ]
  },

  // ── KPIs vs 3 ventanas (cada una con total + recurrente) ──
  "kpis": {
    "actual": {
      "from": "2026-06-23", "to": "2026-06-29",
      "ventas_total": 36479.42,
      "ventas_recurrente": 34454.20,
      "tickets": 2395,
      "unds": 7471.0,
      "ticket_promedio": 15.23,
      "margen_pct_total": 31.65,
      "margen_pct_recurrente": 31.80,
      "descuento_pct": 1.07
    },
    "vs_semana_anterior": {
      "label": "semana_anterior_ajustada",
      "from": "2026-06-16", "to": "2026-06-22",
      "ventas_total": 48895.72,
      "ventas_recurrente": 38600.50,
      "tickets": 2790,
      "delta_abs_total":      -12416.30,
      "delta_pct_total":      -25.39,
      "delta_abs_recurrente":  -4146.30,
      "delta_pct_recurrente": -10.73     // ← este es el accionable
    },
    "vs_promedio_4_semanas": { /* mismo shape, contra promedio 28d normalizado */ },
    "vs_ano_anterior":       { /* mismo shape, contra mismo período 364 días atrás */ }
  },

  "veredicto": {
    "codigo": "BAJON_ESTACIONAL_NORMAL",
    "titulo": "Caída estacional, no problema",
    "explicacion": "..."
  },

  // ── Anatomía: ¿tráfico, canasta o precio? ──
  "anatomia": {
    "delta_pct_total": -10.73,
    "contribucion_log_pct": {
      "tickets":         -11.1,    // ← el dominante (menos tráfico)
      "unds_per_ticket":  -1.2,
      "monto_per_und":    +1.5,
      "total":           -10.8
    },
    "comparacion_base": "semana_anterior_ajustada",
    "base_calculo": "venta_recurrente",
    "lectura": "El cambio es dominado por tráfico — bajó 11.1% (escala log)."
  },

  // ── Descomposiciones (suman al delta RECURRENTE) ──
  "descomposicion": {
    "comparacion_base": "semana_anterior_ajustada",
    "por_sucursal": [
      {
        "office_id": 1, "sucursal": "KAWII MAGDALENA",
        "ventas_actual": 23091.09, "ventas_prev": 29529.87,
        "delta_abs": -6438.78, "delta_pct": -21.8,
        "share_pct": 51.9
      },
      // ...
    ],
    "por_categoria":      [/* top N categorías que más movieron */],
    "por_dia_semana":     [/* L M X J V S D */],
    "por_franja_horaria": [/* 08-12h, 12-15h, 15-17h, 17-19h, 19-22h */],
    "por_vendedor":       [/* top N vendedores con |Δ| > 100 */]
  },

  // ── Factores que ayudan a explicar (no suman al delta — son lentes) ──
  "factores_adicionales": {
    "venta_perdida_por_quiebre": {
      "monto_estimado_pen": 1504.11,
      "skus_con_perdida": 25,
      "metodo": "dias_quiebre × tdpv_30d × precio_unit_30d",
      "top_skus": [/* top 10 SKUs con detalle */]
    },
    "cambio_descuentos": { "pct_actual": 1.07, "pct_prev": 2.29, "delta_pp": -1.22, ... },
    "devoluciones":      { "monto_actual": 62.5, "monto_prev": 0.0, "delta_pct": null },
    "gratuidades":       { "lineas_actual": 0, ... }
  },

  // ── SKUs top que subieron/cayeron + nuevos + enfriados ──
  "ganadores_y_perdedores": {
    "top_subieron":             [/* top N SKUs con ↑ */],
    "top_cayeron":              [/* top N SKUs con ↓ */],
    "skus_nuevos_con_traccion": [/* SKUs con primera venta en la ventana actual */],
    "skus_que_se_enfriaron":    [/* SKUs que vendían y dejaron de vender */]
  },

  // ── Huecos YoY: sub-categorías que cayeron >50% vs hace 12 meses ──
  "huecos_yoy": [
    {
      "departamento": "Calzados",
      "categoria": "Zapatillas",
      "subcategoria": "Zapatillas Urbanas",
      "venta_actual": 2398.05,
      "venta_yoy":    8306.90,
      "hueco_pen":    5908.85,
      "delta_pct":   -71.1,
      "diagnostico": "cambio_de_demanda"      // o "discontinuado_sin_reemplazo"
    }
  ],

  "resumen": [
    "Las ventas recurrentes cayeron 10.73% (S/ 4,143) vs los 7 días previos — el total bruto cambió -25.4% (incluye estacionales).",
    "Estás 94.48% arriba respecto al mismo período del año pasado.",
    "Veredicto: Caída estacional, no problema. ...",
    "El factor dominante es tráfico (menos tickets) (-11.1% en escala log).",
    "KAWII MAGDALENA explica el 51.9% del cambio (S/ -6,439)."
  ]
}
```

### Cómo renderizarlo

1. **Top de la página**: card de Veredicto (color según código) + KPIs con `total` y
   `recurrente` lado a lado.
2. **Sección "Por qué cayó":** mostrar `anatomia.lectura` + barras horizontales con
   las 3 contribuciones.
3. **Descomposición**: 5 tabs (Sucursal, Categoría, Día, Hora, Vendedor). Cada uno
   con tabla ordenada por `|delta_abs|`.
4. **Factores adicionales** como 4 cards.
5. **Ganadores y perdedores** como tabla con tab selector.
6. **Huecos YoY** como cards en lista vertical.

---

## Vista 3 — Salud del catálogo (`GET /catalog-health`)

**Pregunta que responde**: *¿Qué tengo que comprar / liquidar / reponer?*  
**Foco**: estado del 80/20, huecos, capital atrapado, candidatos a descuento.

### Parámetros

| Param | Tipo | Default | Comentario |
|---|---|---|---|
| `office_id` | int? | `null` | Filtra por sucursal. |
| `top_n` | int (1-100) | 15 | Cuántos ítems por lista. |

### Ejemplo de uso

```http
GET /catalog-health?office_id=1&top_n=20
```

### Shape de respuesta (resumido)

```jsonc
{
  "meta": {
    "fecha": "2026-06-30",
    "office_id": 1,
    "office_scope": [1],
    "exclusiones": {...},
    "cobertura_costos": {...},
    "nota": "Bloque estable 80/20 activo — metas/roles cargados en category_targets."
    // O: "Tabla category_targets vacía — correr POST /config/category-targets/bootstrap"
  },

  // ── BLOQUE 80/20 — solo aparece si hay category_targets cargados ──
  "bloque_estable_80_20": {
    "mes": "2026-06",
    "dias_transcurridos": 29,
    "dias_del_mes": 30,
    "ultimo_dia_cerrado": "2026-06-29",
    "total_categorias": 24,
    "por_sucursal": [
      {
        "office_id": 1, "sucursal": "KAWII MAGDALENA",
        "meta_total": 58053.0,
        "venta_acumulada_total": 59046.0,
        "meta_prorrateada_total": 56118.0,
        "avance_pct": 101.7,
        "ritmo_vs_meta_pct": 105.2,
        "categorias": 15,
        "cumplen": 11, "en_ritmo": 1, "atrasado_leve": 1, "riesgo": 2
      }
    ],
    "categorias": [
      {
        "category_id": 12,
        "categoria": "Menaje de Cocina",
        "departamento": "Hogar y Decoración",
        "office_id": 1,
        "sucursal": "KAWII MAGDALENA",
        "rol": "motor_1",                       // motor_1..4, fijo, complemento, upsell
        "meta_mensual_pen": 8649.0,
        "meta_prorrateada": 8360.0,
        "venta_acumulada_mes": 9124.0,
        "gap_a_meta": 475.0,
        "avance_pct": 105.5,
        "ritmo_vs_meta_pct": 109.1,
        "proyeccion_cierre": 9438.0,
        "estado": "META_CUMPLIDA",
        "skus_con_stock": 56,
        "skus_min": 105, "skus_max": 157,
        "skus_estado": "FALTAN_SKUS",           // OK | FALTAN_SKUS | EXCESO_SKUS
        "pvp_min": 1.9, "pvp_max": 10.1,
        "margen_objetivo_pct": 61.4
      }
      // ...
    ]
  },

  // ── Top categorías por venta últ. 30 días (siempre, con o sin targets) ──
  "categorias": {
    "ventana": "30d_vs_yoy",
    "total_actual": 182140,
    "total_yoy": 69400,
    "delta_yoy_pct": 162.5,
    "top_categorias": [
      {
        "departamento": "Hogar y Decoración",
        "categoria": "Menaje de Cocina",
        "ventas_30d": 15815, "ventas_30d_yoy": 13900,
        "delta_yoy_pct": 13.8,
        "skus_con_venta": 76,
        "tendencia": "estable"                  // subiendo | estable | bajando | hueco | nuevo
      }
    ]
  },

  // ── Sub-categorías que cayeron >50% vs hace 12 meses ──
  "huecos_yoy": {
    "ventana": "30d_vs_yoy",
    "total_hueco_pen": 16037,
    "subcategorias_count": 14,
    "top_huecos": [/* sub-cats con hueco, ordenadas DESC */]
  },

  // ── Capital atrapado (SKUs recibidos hace ≤90d con sellthrough <20%) ──
  "capital_atrapado": {
    "criterio": "SKU recibido en últ. 90 días con sellthrough < 20% y stock > 0",
    "ventana_recepcion": "90d",
    "skus_count_total": 259,
    "monto_total_pen": 40558,
    "top_skus": [
      {
        "sku": "LA-G-185",
        "producto": "SET COPA X 12 SET",
        "sucursal": "KAWII MAGDALENA",
        "office_id": 1,
        "fecha_recepcion": "2026-05-17",
        "unds_recibidas": 240,
        "unds_vendidas": 3,
        "stock_actual": 237,
        "sellthrough_pct": 1.2,
        "costo_unit": 12.3,
        "capital_atrapado_pen": 2923
      }
    ]
  },

  // ── Stock sin movimiento (candidatos a descuento 30%) ──
  "candidatos_descuento": {
    "criterio": "SKU con stock > 0 sin ventas en últimos 60 días",
    "skus_count_total": 64,
    "valor_inventario_pen": 4798,
    "top_skus": [/* SKUs ordenados por valor de inventario */]
  },

  "quiebres_demanda": {
    "skus_count": 25,
    "monto_estimado_pen": 1504.11,
    "ventana_dias": 7,
    "top_skus": [/* top 5 */],
    "ver_detalle_en": "/diagnosis (factores_adicionales.venta_perdida_por_quiebre)"
  },

  // ── Composición por edad del catálogo ──
  "composicion_catalogo": {
    "ventana_venta": "30d",
    "total_venta_pen": 182140,
    "total_skus": 1035,
    "por_edad": [
      { "bucket": "nuevo",    "etiqueta": "Primera venta ≤ 90 días",   "skus": 518, "venta_pen": 118196, "pct_venta": 64.9, "pct_unds": 60.1 },
      { "bucket": "reciente", "etiqueta": "Primera venta 91-365 días", "skus": 460, "venta_pen": 53119,  "pct_venta": 29.2, "pct_unds": 32.0 },
      { "bucket": "clasico",  "etiqueta": "Primera venta > 365 días",  "skus": 57,  "venta_pen": 10825,  "pct_venta": 5.9,  "pct_unds": 7.9  }
    ],
    "lectura": "El 64.9% de tu venta viene de SKUs nuevos (≤90 días). Buena renovación de catálogo."
  },

  "resumen": [
    "KAWII MAGDALENA — bloque 80/20: avance 101.7% (S/ 59,046 / S/ 58,053). 11 categorías cumplieron, 2 en riesgo.",
    "Motor más rezagado: Maquillaje y Cosméticos (motor_4, MAGDALENA) — S/ 3,082 de meta S/ 4,314 (71.4%).",
    "S/ 40,558 de capital atrapado en 259 SKUs recibidos hace ≤90 días con sellthrough < 20%.",
    // ...
  ]
}
```

### Cómo renderizarlo

1. **Si `bloque_estable_80_20` viene null** → mostrar banner azul:
   *"Para activar la sección 80/20, correr el bootstrap"* con botón que llama
   `POST /config/category-targets/bootstrap`.
2. **Si viene poblado**:
   - Card por sucursal con `avance_pct`, ritmo y conteo de estados.
   - Tabla de categorías con sort por estado / venta / meta.
   - Color de fila por `estado`.
3. **Capital atrapado** como tabla con valor en S/ y % sellthrough.
4. **Candidatos a descuento** como otra tabla con botón "Marcar para promo 30%".
5. **Composición por edad** como gráfico de dona o barras apiladas.

---

## Vista 4 — Plan del mes (`GET /plan`)

**Pregunta que responde**: *¿Llegaré a la meta y cómo planifico el próximo?*  
**Foco**: proyección, sugerencia de meta del próximo mes, calendario, presupuesto.

### Parámetros

| Param | Tipo | Default | Comentario |
|---|---|---|---|
| `office_id` | int? | `null` | Filtra por sucursal. |
| `meses_calendario` | int (1-12) | 6 | Cuántos meses adelante incluir en el calendario. |

### Ejemplo de uso

```http
GET /plan?office_id=&meses_calendario=6
```

### Shape de respuesta (resumido)

```jsonc
{
  "meta": {
    "fecha": "2026-06-30",
    "mes_actual": "2026-06",
    "mes_objetivo": "2026-07",
    "office_id": null,
    "office_scope": [1, 3],
    "exclusiones": {...},
    "cobertura_costos": {...}
  },

  // ── Proyección del mes en curso vs meta ──
  "mes_en_curso": {
    "mes": "2026-06",
    "dias_transcurridos": 29,
    "dias_del_mes": 30,
    "dias_restantes": 1,
    "ultimo_dia_cerrado": "2026-06-29",
    "venta_acumulada": 177581,
    "venta_acumulada_recurrente": 156168,
    "venta_diaria_promedio": 6124,
    "meta": 215000,
    "meta_source": "exacta",
    "gap_a_meta": -37419,                       // ← venta − meta: negativo = falta para la meta
    "proyeccion_lineal": 183705,
    "estado": "ATRASADO_LEVE",
    "venta_diaria_necesaria": 37419,
    "ritmo_necesario_multiplo": 6.11
  },

  // ── Meta sugerida del próximo mes (3 niveles) ──
  "sugerencia_proximo_mes": {
    "mes_objetivo": "2026-07",
    "metodo": "yoy_mas_crecimiento_3m_promedio",
    "venta_yoy_mismo_mes": 104398,
    "crecimiento_yoy_3m_pct": 5.2,
    "muestras_crecimiento": 3,
    "mejor_mes_historico": 137427,
    "meta_conservadora": 109618,
    "meta_realista":     115000,                  // ← recomendada (default)
    "meta_agresiva":     125000,
    "recomendacion": "realista"
  },

  // ── Pacing semanal del próximo mes (distribuye según YoY) ──
  "pacing_semanal": {
    "mes": "2026-07",
    "meta_total": 115000,
    "metodo": "dist_yoy_misma_semana",
    "venta_yoy_total": 104398,
    "semanas": [
      { "sem": 1, "from": "2026-07-01", "to": "2026-07-05", "dias": 5, "pct_mes": 18.5, "meta": 21275, "yoy_venta": 19300 },
      { "sem": 2, "from": "2026-07-06", "to": "2026-07-12", "dias": 7, "pct_mes": 18.8, "meta": 21620, "yoy_venta": 19620 }
      // ...
    ]
  },

  // ── Calendario de campañas próximos N meses ──
  "calendario_campanas": [
    {
      "mes": "2026-07",
      "mes_nombre": "Julio",
      "campana_principal": "Back to school + Invierno",
      "venta_yoy": 104398,
      "meta_conservadora": 104398,
      "meta_realista": 115000,
      "meta_agresiva": 125000,
      "categoria_protagonista": {
        "departamento": "Hogar y Decoración",
        "categoria": "Menaje de Cocina",
        "venta_yoy": 9350
      }
    }
    // ... 5 meses más
  ],

  // ── Presupuesto sugerido (con desglose si hay category_targets) ──
  "presupuesto_compra": {
    "mes_objetivo": "2026-07",
    "meta_venta": 115000,
    "margen_promedio_pct": 34.8,
    "muestras_margen": 3,
    "costo_estimado_pen": 74980,
    "presupuesto_compra_pen": 74980,
    "desglose_por_categoria": [
      {
        "category_id": 12,
        "categoria": "Menaje de Cocina",
        "departamento": "Hogar y Decoración",
        "office_id": 1,
        "sucursal": "KAWII MAGDALENA",
        "rol": "motor_1",
        "meta_mensual_categoria": 8649,
        "share_del_total_pct": 9.76,
        "cuota_meta_venta_pen": 11224,
        "margen_objetivo_pct": 61.4,
        "costo_estimado_pen": 4332,
        "presupuesto_compra_pen": 4332
      }
      // ... una entrada por cada categoría con target cargado
    ],
    "nota": "Desglose por categoría motor activo — usa category_targets."
  },

  "resumen": [
    "⚠️ Atrasado leve en 2026-06: proyección S/ 183,705 vs meta S/ 215,000.",
    "Para 2026-07 sugerimos meta realista de S/ 115,000 (YoY: S/ 104,398).",
    "Pico proyectado del semestre: Diciembre con S/ 398,780 (Navidad + fin de año).",
    "Presupuesto sugerido para 2026-07: S/ 74,980 (con margen promedio 34.8%)."
  ]
}
```

### Cómo renderizarlo

1. **Card de proyección del mes** con estado, gap, ritmo necesario.
2. **3 cards de meta sugerida** (conservadora/realista/agresiva), con la realista
   resaltada. Botón "Guardar como meta" llama `PUT /analytics/goals`.
3. **Pacing semanal** como tabla o barras horizontales.
4. **Calendario** como timeline horizontal scrolleable con foto/icono de categoría
   protagonista por mes.
5. **Presupuesto** con desglose como tabla expandible por sucursal/categoría.

---

## Compras & Catálogo (`GET /analytics/compras-catalogo`)

Dashboard de Compras Inteligente: SKUs en **quiebre real** (severidades 🔴 Crítico
y 🟠 Alta de la clasificación KAWII sobre la matriz 04b de 90 días) con métricas
para decidir compras. La página `/compras-catalogo` del frontend consume este
endpoint; el Excel descargable usa exactamente el mismo universo de SKUs.

### `GET /analytics/compras-catalogo`

Query params (todos opcionales):

| Param | Tipo | Descripción |
|---|---|---|
| `office_id` | int | Filtra por sucursal (vacío = todas) |
| `fecha_desde` | date `YYYY-MM-DD` | Solo SKUs con Últ. Recepción **≥** esta fecha |
| `fecha_hasta` | date `YYYY-MM-DD` | Solo SKUs con Últ. Recepción **≤** esta fecha |

El rango de fechas es **inclusivo** y filtra por fecha de ingreso (última
recepción del SKU en la sucursal). SKUs sin recepción registrada quedan fuera
cuando el filtro está activo. `fecha_desde > fecha_hasta` responde HTTP 400.

```jsonc
// Response (resumido)
{
  "generado_at": "2026-07-06T15:00:00",
  "office_id": null,
  "sucursal": null,
  "cobertura_objetivo_dias": 30,        // días de stock a los que repone la sugerencia
  "filtros": { "office_id": null, "fecha_desde": "2026-06-01", "fecha_hasta": "2026-06-30" },
  "kpis": {
    // SKUs críticos, venta 90d en riesgo, unidades a reponer, etc.
    "skus_con_similar": 4              // SKUs con ≥1 producto similar con stock en tienda
  },
  "por_departamento": [ /* agregados por departamento (sidebar) */ ],
  "por_accion":       [ /* breakdown por acción: REPONER YA, PROMOCIONAR, ... */ ],
  "skus": [
    {
      "sku": "ABC-123",
      "producto": "…",
      "sucursal": "…",
      "clasificacion": "…",             // etiqueta KAWII original
      "severidad": "🔴 Crítico",
      "accion": "REPONER YA",
      "stock_disponible": 2,
      "velocidad_30d": 0.8,             // uds/día últimos 30d (solo días con stock)
      "velocidad_90d": 0.5,
      "cantidad_sugerida": 22,          // max(0, round(vel_reciente × 30) − stock)
      "ultima_venta": "2026-07-04",
      "similares": {                    // null si no hay similares con stock
        "vigilar": 1, "lentos": 0, "liquidar": 0,   // conteo por estado
        "items": [
          {
            "sku": "190006",
            "producto": "HISOPO POTE PEQUEÑO",
            "estado": "vigilar",        // "vigilar" (saludable) | "lentos" | "liquidar"
            "stock": 169,
            "cobertura": "72 días",
            "unds_vend_90d": 71,
            "sucursal": "TIENDA 1"
          }
        ]
      },
      // + departamento, categoria, subcategoria, cobertura_dias, tendencia, etc.
    }
  ]
}
```

`cantidad_sugerida` usa la velocidad de los últimos 30 días (fallback: velocidad
90d) y repone hasta `cobertura_objetivo_dias` (30) días de stock.

**`similares`** — productos de nombre parecido (misma subcategoría, misma
sucursal) que ya tienen stock en tienda, para que el gerente no compre un
duplicado (ej. "BYWIN HISOPOS" agotado cuando "HISOPO POTE PEQUEÑO" está sano).
Es una **advertencia, nunca exclusión**: el SKU sigue en la lista y la decisión
es del gerente (encaja con la acción `comprar_similar` de purchase-decisions).
El matching es el mismo del chip "N en saludable" de `/ventas-jerarquicas`
(tokens del nombre + singular/plural), calculado en backend
(`analytics/similares.py`). Solo se consideran similares **con stock > 0**
(un similar agotado no sustituye nada). Los items vienen ordenados: primero
`vigilar` (saludable), luego `lentos`, luego `liquidar`; a igual estado, más
stock primero.

### `GET /analytics/compras-catalogo/excel`

Mismos filtros (`office_id`, `fecha_desde`, `fecha_hasta`) + `days` (default 30,
ventana de la pestaña "Venta por categoría"). Devuelve un `.xlsx` de 2 pestañas
con el **mismo universo de SKUs** que el JSON.

El Excel incluye la advertencia de similares: columna **"⚠ Similar en tienda"**
(última, en ámbar) en las pestañas por departamento con el mejor sustituto
(`PRODUCTO (SKU) · stock und · cob. X días · +N más`), y una línea resumen en el
🎯 Memo Ejecutivo. Header de respuesta: `X-Skus-Con-Similar`.

---

## Reporte de Ventas (`GET /analytics/reporte-ventas`)

Informe de ventas del rango elegido, agregado **departamento → categoría →
producto**, con costo, ganancia y margen. Pensado para el selector de fechas
que ya usa el dashboard (`start_date` / `end_date` inclusivos, así que
`start_date == end_date` da el reporte de un solo día).

Dos endpoints sobre **el mismo cálculo**, para que la pantalla y el Excel nunca
muestren números distintos.

### `GET /analytics/reporte-ventas`

**Query params:**

| Param | Tipo | Default | Notas |
|---|---|---|---|
| `start_date` | `YYYY-MM-DD` | — | **requerido**, inclusive |
| `end_date` | `YYYY-MM-DD` | — | **requerido**, inclusive |
| `office_id` | int | `null` | Sucursal puntual. Sin esto, todas las de `offices_tienda` |
| `incluir_productos` | bool | `true` | `false` corta en el nivel categoría — el payload pasa de ~223 KB a ~19 KB |

Errores: `400` si el formato de fecha es inválido, si `start_date > end_date`,
o si el rango supera **366 días**.

**Shape de respuesta:**

```jsonc
{
  "meta": {
    "empresa": "tiendas kawii",
    "desde": "2026-07-26", "hasta": "2026-08-24", "dias": 30,
    "sucursal": "Todas las sucursales",   // o "KAWII MAGDALENA"
    "office_ids": [1, 3],
    "generado_en": "2026-08-24T13:30:02+00:00"
  },
  "totales": {
    "unidades": 38072.0,
    "ingresos": 196688.59,        // CON IGV — es la base del margen acá
    "ingresos_netos": 166681.67,  // sin IGV, informativo
    "igv": 30006.92,              // siempre ingresos - ingresos_netos
    "costo": 129110.09,
    "ganancia": 67578.50,         // ingresos - costo
    "margen_pct": 34.36,          // ganancia / ingresos
    "markup_pct": 52.34,          // ganancia / costo
    "tickets": 11417,
    "ticket_promedio": 17.23
  },
  "serie_diaria": [
    { "fecha": "2026-07-26", "ingresos": 6890.20, "tickets": 401 }
  ],
  "departamentos": [
    {
      "departamento": "Hogar y Decoración",
      "unidades": 9312.0, "ingresos": 40027.42, "ingresos_netos": 33922.12,
      "igv": 6105.30, "costo": 26204.76, "ganancia": 13822.66,
      "margen_pct": 34.53, "markup_pct": 52.75,
      "participacion_pct": 20.35,          // sobre los ingresos totales
      "tickets": 3184, "ticket_promedio": 12.57,
      "categorias": [
        {
          "categoria": "Cocina",
          "unidades": 4021.0, "ingresos": 17447.93, /* … */
          "participacion_pct": 43.59,      // sobre los ingresos del DEPARTAMENTO
          "productos": [                   // ausente si incluir_productos=false
            {
              "sku": "80158", "producto": "TERMO 1LT",
              "unidades": 61.0, "ingresos": 895.17, "ingresos_netos": 758.51,
              "igv": 136.66, "costo": 621.25, "ganancia": 273.92,
              "margen_pct": 30.6, "markup_pct": 44.09,
              "participacion_pct": 5.13    // sobre los ingresos de la CATEGORÍA
            }
          ]
        }
      ]
    }
  ]
}
```

Todos los niveles (producto, categoría, departamento, totales) traen el mismo
set de métricas, así que un componente que renderice una fila sirve para los
cuatro.

Departamentos, categorías y productos vienen **ordenados por ingresos
descendente**. Los decimales son números JSON, no strings.

### `GET /analytics/reporte-ventas/excel`

Mismos params (sin `incluir_productos`: el Excel siempre trae el detalle).
Devuelve un `.xlsx` con:

- **Portada** — cabecera del informe + los KPIs del periodo + la nota
  metodológica.
- **Resumen** — una fila por departamento, con gráfico de barras, torta de
  participación y la línea de evolución diaria.
- **Una pestaña por departamento** — tabla de categorías con sus productos en
  filas **colapsables** (botones +/- de Excel), el bloque horizontal *Desglose
  de los ingresos por categoría*, y 3 gráficos (barras / torta / línea).

### Criterios de cálculo

- Se excluyen **notas de crédito** y **líneas de regalo** (`is_gratuity`),
  mismo criterio que `catalog-health` / `diagnosis`.
- **Costo** = cantidad × costo efectivo de la variante **en la sucursal de la
  venta** (`variant_costs_by_office`), con fallback al costo global
  (`variant_costs`).
- **Tickets**: se cuentan a nivel documento. Un ticket con productos de dos
  departamentos cuenta en ambos, así que la columna `tickets` de los
  departamentos **suma más** que `totales.tickets`. El total sale de contar
  documentos únicos.

#### ⚠️ La base del margen acá NO es la misma que en el resto del backend

```
ganancia   = ingresos CON IGV − costo neto
margen_pct = ganancia / ingresos CON IGV
```

`/analytics/rentabilidad`, `/diagnosis` y `/catalog-health` usan el margen
contable clásico **sobre venta neta**. Este reporte no. La diferencia es
deliberada y se apoya en dos hechos de esta empresa, confirmados con el dueño
el 2026-08-24:

1. El **IGV cobrado en las ventas se queda en la empresa** (no se remite) → el
   ingreso económico real es el bruto.
2. Los **proveedores no facturan IGV** en las compras → el costo neto guardado
   es exactamente lo que se paga.

Con esas dos condiciones, bruto-contra-neto da ~**34%**; el margen contable
daría ~**23%**. **Si alguna de las dos deja de ser cierta, este cálculo queda
mal**: subir un solo lado por 1,18 es justo lo que produce un margen inflado
(algebraicamente, "bruto vs costo neto" y "neto vs costo bruto" dan el mismo
número, así que el error es invisible si no se revisan las dos puntas juntas).

Por eso el endpoint expone `igv` en todos los niveles: permite auditar el
puente `ingresos − igv = ingresos_netos` y rehacer el margen contable sin
pedir otra query. `markup_pct` (ganancia sobre costo) va como segunda lente
para fijar precios.

**Al comparar contra otra vista del dashboard, verificá primero cuál base usa
cada una** — si no, parece que los números no cuadran.

---

## Evolución en el tiempo (`GET /analytics/evolucion`)

Serie **mensual** de ventas por dimensión, pensada para **gráficos de líneas /
áreas apiladas / mix 100%** y tarjetas de tendencia. Responde "¿cómo evolucionó
cada departamento / categoría / subcategoría / sucursal mes a mes?".

### `GET /analytics/evolucion`

**Query params:**

| Param | Tipo | Default | Notas |
|---|---|---|---|
| `dimension` | enum | `departamento` | `departamento` \| `categoria` \| `subcategoria` \| `sucursal` |
| `meses` | int 3–36 | `12` | Cuántos meses hacia atrás (ventana visible) |
| `office_id` | int | — | Filtra a una sucursal. Se **ignora** si `dimension=sucursal` |
| `top` | int 0–50 | `0` | `0` = todas; `N` = deja las N de mayor venta y agrupa el resto como `"Otros"` |

Devuelve un arreglo **long-format**: una fila por `(periodo, dim)`, sin huecos
(los meses sin venta salen en 0). Ordenado por `periodo`, luego `ventas` desc.

```json
[
  {
    "periodo": "2026-06",          // YYYY-MM
    "dim": "Hogar y Decoración",   // valor de la dimensión (o "Otros")
    "ventas": 30320.91,            // S/ (SUM total_amount)
    "unidades": 7527,
    "tickets": 812,                // documentos que incluyeron esta dim (ver nota)
    "ticket_promedio": 37.34,
    "margen_soles": 7368.6,        // net_amount − unidades×effective_cost
    "margen_pct": 24.3,
    "participacion_pct": 22.4,     // % del total del mes (mix); suma ~100 por periodo
    "crecimiento_mom_pct": 21.5,   // vs mes anterior; null en el 1er mes
    "crecimiento_yoy_pct": 8.1,    // vs mismo mes año pasado; null si no hay base
    "skus_activos": 143,           // variantes distintas vendidas
    "cobertura_costos_pct": 100.0, // % de unidades con costo (margen confiable si ~100)
    "parcial": false               // true = mes EN CURSO (incompleto) → dibujar punteado
  }
]
```

**Cómo graficar:**
- **Líneas / áreas apiladas por evolución**: pivotear `dim` sobre el eje `periodo`, valor `ventas` (o `unidades`).
- **Mix 100% (cómo cambia la mezcla)**: usar `participacion_pct` en área apilada al 100%.
- **Mes en curso**: la última fila trae `parcial: true` — dibujarla con línea punteada o rotularla "en curso"; **nunca** leerla como caída (solo lleva los días transcurridos del mes).
- **Margen**: si `cobertura_costos_pct` es bajo (<90), advertir que el margen puede estar distorsionado (mismo criterio que `meta.cobertura_costos`).

> **Nota `tickets` / `skus_activos`**: son conteos **por dimensión** (un ticket que
> toca 2 categorías cuenta en ambas). En el bucket `"Otros"` (cuando `top>0`) estos
> dos campos se suman, así que pueden sobre-contar tickets compartidos; `ventas`,
> `unidades` y `margen_soles` sí son exactos.

### `GET /analytics/evolucion/resumen`

Mismos params. Devuelve **una tarjeta de análisis por dimensión** (ordenadas por
venta total desc). Los cálculos de tendencia/crecimiento usan solo meses
**completos** (excluyen el mes en curso).

```json
[
  {
    "dim": "Librería y Oficina",
    "venta_total": 520415.42,          // incluye el mes parcial (venta real acumulada)
    "tendencia": "CRECIENDO",          // CRECIENDO | ESTABLE | DECAYENDO (1ª vs 2ª mitad)
    "crecimiento_periodo_pct": 144.5,  // primer vs último mes COMPLETO
    "participacion_actual_pct": 3.7,   // % del último mes completo
    "aporte_al_crecimiento_pct": 8.3,  // cuánto del crecimiento total del negocio explica; suma ~100
    "mejor_mes": {"periodo": "2026-03", "ventas": 356603.53},
    "peor_mes":  {"periodo": "2025-10", "ventas": 1532.7},
    "margen_pct_actual": 31.2,
    "periodo_actual": "2026-06"        // último mes completo usado como referencia
  }
]
```

Ideal para: chips de "▲ creciendo / ▼ cayendo" junto a cada línea del gráfico, y
para ordenar el foco por `aporte_al_crecimiento_pct` (quién mueve la aguja).

> **Rendimiento**: la consulta escanea toda la ventana (hasta 24 meses reales para
> poblar el YoY). Con el volumen actual responde en ~3–10 s según la dimensión;
> conviene cachear en el frontend por `(dimension, meses, office_id)`.

---

## Ecosistema de compras (`GET /compras/ecosistema`)

Cruza **compra ↔ venta ↔ stock** por nivel de taxonomía en una sola fila.
Responde *"¿cuánto compré de cada categoría, cuánto vendí de eso, y qué me
quedó?"* — el análisis que antes no existía. La compra sale de `receptions`
filtrando "compra real" (sin traslados, sin ajustes de inventario, con las
importaciones masivas, con sanidad de cantidad/costo).

**Asimetría de sucursales (importante para leer los números):** la **compra** y
el **stock** incluyen el almacén central (la mercadería entra y se acumula ahí);
la **venta** solo las tiendas que venden al público. Con `office_id` las tres se
acotan a esa sucursal. El `meta` devuelve ambos scopes usados.

### Parámetros

| Param | Tipo | Default | Comentario |
|---|---|---|---|
| `nivel` | enum | `categoria` | `departamento` \| `categoria` \| `subcategoria` |
| `dias` | int 30–730 | `365` | Ventana de compra y venta hacia atrás |
| `office_id` | int? | `null` | Filtra a una sucursal (vacío = scope de la empresa) |

```http
GET /compras/ecosistema?nivel=categoria&dias=365
```

### Shape de respuesta

```jsonc
{
  "meta": {
    "generado_at": "2026-08-17T19:12:04Z",
    "nivel": "categoria",
    "dias": 365,
    "office_id": null,
    "office_scope_compra": [1, 3, 4],   // tiendas + almacén
    "office_scope_venta":  [1, 3],      // solo tiendas
    "nota": "Compra y stock incluyen almacén central; venta solo tiendas. ..."
  },
  "kpis": {
    "venta_total_pen":      2695275.64,
    "compra_total_pen":     1948743.02,
    "stock_total_pen":      156042.20,
    "multiplicador_global": 1.38,       // venta / compra (flujo, NO margen)
    "filas": 81,
    "por_veredicto": {                  // conteo por estado → chips de resumen
      "SOBRECOMPRA": 32, "EQUILIBRADO": 16, "RIESGO_QUIEBRE": 29,
      "EXCESO_STOCK": 3, "SIN_MOVIMIENTO": 1
    }
  },
  "items": [
    {
      "department": "Juguetes y Entretenimiento",
      "category":   "Juguetes",          // + "subcategory" si nivel=subcategoria
      "venta_pen":       258114.48,      // total_amount (retail, con IGV)
      "compra_pen":      192652.00,      // quantity × cost (a costo)
      "stock_pen":         7340.63,      // stock actual valorizado a costo
      "unds_vendidas":   45233,
      "unds_compradas":  57021,
      "unds_stock":       2938,
      "multiplicador":    1.34,          // venta_pen / compra_pen; null si compra=0
      "sellthrough_pct":  79.3,          // unds_vendidas / unds_compradas; null si compra=0
      "meses_cobertura":  0.8,           // unds_stock / (unds_vendidas por mes); null si venta=0
      "veredicto":        "SOBRECOMPRA"
    }
    // ... ordenados por venta_pen desc
  ]
}
```

### Los tres números que el gerente lee

| Métrica | Qué dice | Bandera |
|---|---|---|
| **multiplicador** | Soles de venta (retail) por cada sol comprado (a costo). Es un ratio de flujo, **no** el margen. | Bajo = se compró de más para lo que rota |
| **sellthrough_pct** | Qué % de lo comprado ya salió | Bajo = capital atrapado |
| **meses_cobertura** | Cuánto dura el stock al ritmo de venta | < 0.5 riesgo de quiebre · > 2.5 stock dormido |

### Veredictos

| Código | Cuándo | Lectura |
|---|---|---|
| `SIN_REPOSICION` | vendió y no se compró en la ventana | revisar si falta reponer |
| `RIESGO_QUIEBRE` | cobertura < 0.5 meses | comprar ya |
| `SOBRECOMPRA` | multiplicador < 1.5 | se compró de más |
| `EXCESO_STOCK` | cobertura > 2.5 meses | capital dormido, promocionar |
| `EQUILIBRADO` | ninguna bandera | ok |
| `SIN_MOVIMIENTO` | ni compra ni venta | catálogo inactivo |

> **Nota sobre el multiplicador**: como venta va con IGV y a precio de lista, y
> compra a costo, el punto de equilibrio "vendí todo lo que compré" no es 1.0
> sino ~1.8 (según margen). Los umbrales del veredicto (1.5 / 0.5 / 2.5) son un
> punto de partida — conviene calibrarlos con el gerente una vez que los vea.

---

## Canasta — qué se llevan juntos los clientes (`GET /compras/canasta`)

Análisis de **market-basket**: qué categorías (o subcategorías) se compran juntas
en un mismo ticket. Sirve para decidir **exhibición cruzada, combos y reposición
conjunta**. Responde el pedido *"conocer mejor qué compran las personas"*.

La métrica que manda es el **lift**, no el conteo crudo: una categoría grande
(ej. Higiene Personal) aparece en muchos tickets solo por tamaño; el lift divide
por la frecuencia base de cada una y deja arriba los pares que de verdad se buscan
juntos.

### Selector de rango — `GET /compras/canasta/meses`

Antes de mostrar la canasta, poblá el selector de meses con este endpoint ligero:

```jsonc
// GET /compras/canasta/meses?office_id=
{
  "office_id": null,
  "office_scope": [1, 3],
  "meses": [
    { "mes": "2025-06", "tickets": 4055, "ventas_pen": 77243 },
    // ... un objeto por mes con datos
    { "mes": "2026-08", "tickets": 5450, "ventas_pen": 197112 }
  ],
  "primer_mes": "2025-06",
  "ultimo_mes": "2026-08"
}
```

### `GET /compras/canasta`

| Param | Tipo | Default | Comentario |
|---|---|---|---|
| `nivel` | enum | `categoria` | `categoria` \| `subcategoria` |
| `dias` | int 7–365 | `90` | Ventana si **no** se pasan fechas |
| `fecha_desde` | date? | — | Inicio `YYYY-MM-DD` (junto con `fecha_hasta`) |
| `fecha_hasta` | date? | — | Fin `YYYY-MM-DD`, **inclusivo** |
| `office_id` | int? | `null` | Filtra a una sucursal |
| `min_soporte` | int 1–1000 | `15` | Mínimo de tickets juntos para considerar un par |
| `lift_min` | float 1–10 | `1.2` | Lift mínimo para reportar afinidad |
| `top` | int 1–300 | `40` | Cuántos pares devolver |

Pasá `fecha_desde` **y** `fecha_hasta` juntas (para un mes puntual del selector),
o ninguna (usa `dias`). Pasar solo una → HTTP 400. `fecha_desde > fecha_hasta` → 400.

```http
GET /compras/canasta?nivel=categoria&fecha_desde=2026-05-01&fecha_hasta=2026-05-31
```

### Shape de respuesta

```jsonc
{
  "meta": {
    "generado_at": "2026-08-17T20:00:00Z",
    "nivel": "categoria",
    "desde": "2026-05-01", "hasta": "2026-05-31",   // inclusivos, para mostrar
    "dias": 31,
    "office_id": null,
    "office_scope": [1, 3],
    "parametros": { "min_soporte": 15, "lift_min": 1.2, "top": 40 },
    "nota": "El lift es la métrica de afinidad: >1 = se compran juntas más de lo esperado..."
  },

  // ── Perfil de canasta: ¿cuántos productos lleva la gente? ──
  "perfil_canasta": {
    "tickets": 11951,
    "tickets_multiproducto": 6405,
    "pct_multiproducto": 53.6,               // ← ahí vive la venta cruzada
    "productos_por_ticket": 2.33,
    "ticket_promedio_pen": 16.20,
    "ticket_promedio_multi_pen": 24.80,      // el ticket multi vale más
    "distribucion": [
      { "productos": "1",  "tickets": 5546 },
      { "productos": "2",  "tickets": 3820 },
      { "productos": "3",  "tickets": 1580 },
      { "productos": "4+", "tickets": 1005 }
    ]
  },

  // ── Pares ordenados por lift desc (los accionables) ──
  "pares": [
    {
      "a": "Cumpleaños y Fiestas",
      "b": "Piñatas",
      "juntos": 158,               // tickets que llevaron ambas
      "soporte_pct": 1.32,         // % de todos los tickets
      "confianza_a_b": 18.6,       // de los que llevan A, % que lleva B
      "confianza_b_a": 62.0,       // de los que llevan B, % que lleva A
      "lift": 8.84,                // > 1 = afinidad real
      "interpretacion": "afinidad_muy_fuerte"
    }
    // ...
  ],

  // ── Mejor acompañante por categoría (para "quien lleva X también lleva Y") ──
  "por_categoria": [
    {
      "categoria": "Piñatas",
      "acompanante": "Cumpleaños y Fiestas",
      "lift": 8.84,
      "confianza_pct": 62.0,       // % de tickets de "Piñatas" que llevan el acompañante
      "tickets_juntos": 158
    }
    // ... una fila por categoría con al menos una afinidad
  ],

  "resumen": [
    "6,405 de 11,951 tickets (54%) llevan más de un producto — ahí vive la venta cruzada.",
    "La afinidad más fuerte es «Cumpleaños y Fiestas» + «Piñatas»: aparecen juntas 158 veces (lift 8.84)...",
    "12 par(es) con afinidad fuerte (lift ≥ 1.5) — candidatos a exhibición cruzada o combo."
  ]
}
```

### Cómo renderizarlo

1. **Selector de rango** arriba (meses de `/canasta/meses`, o un date-range). El
   default sin fechas es los últimos 90 días.
2. **Perfil de canasta** como tarjetas / dona: % multiproducto y distribución por
   nº de productos. El % multiproducto es el titular ("dónde está la venta cruzada").
3. **Tabla de pares** ordenada por lift, con chip de color por `interpretacion`
   (muy_fuerte / fuerte / moderada). Mostrar `confianza_a_b` como "de quien lleva
   A, el X% lleva B".
4. **Por categoría** como buscador: elegís una categoría y ves su mejor acompañante.

### Métricas (glosario rápido)

| Métrica | Definición |
|---|---|
| **soporte** | % de tickets que llevan ambas categorías |
| **confianza(A→B)** | de los que llevan A, qué % también lleva B |
| **lift** | soporte / (frecuencia A × frecuencia B). >1 afinidad, ≈1 azar, <1 se evitan |

> **Interpretación del lift**: `≥ 2.0` muy fuerte · `≥ 1.5` fuerte · `≥ 1.2`
> moderada. Los pares con `lift < lift_min` no se devuelven.

> **Cobertura y límites**:
> - Solo cuenta ventas de las **tiendas** (excluye almacén central).
> - Un ticket con más de 20 categorías distintas se excluye (mayoreo / consumo
>   interno / artefacto, no una compra real de cliente).
> - Con ~2.5 productos por ticket la señal es sólida a nivel **categoría**; a nivel
>   **subcategoría** es más fina pero más dispersa — bajá `min_soporte` si hace falta.
> - **Rendimiento**: la matriz de afinidad es pesada (~2–20 s según empresa y
>   ventana). Es **cacheable** por `(nivel, desde, hasta, office_id)` hasta el
>   próximo sync nocturno. Si una ventana muy grande excede el límite interno,
>   responde **HTTP 503** pidiendo achicar el rango — mostrar ese mensaje y sugerir
>   un mes puntual.

---

## Comparador temporal (`GET /compras/comparador`)

Compara **dos ventanas de fecha arbitrarias** (cualquier mes/semana de cualquier
año) cara a cara, a nivel **SKU agrupado por jerarquía**, y las cruza con el
**stock actual**. Responde el pedido del gerente: *"en tal fecha se vendía bien
X; ¿cómo viene ahora y qué me queda para reponer?"*.

Reutiliza el patrón de `/analytics/compras-historico/excel` (venta de una ventana
× stock actual × sugerencia) y lo extiende con la **segunda ventana** y la
**rotación comparada**.

- **Ventana A** = referencia (la fecha buena que querés replicar).
- **Ventana B** = comparación. Si no la pasás, es el tramo reciente del **mismo
  largo** que A, cerrando ayer.
- **velocidad** = uds/día (la rotación). `delta_velocidad_pct` es la comparación
  de rotación A→B.
- **sugerencia_compra** cubre `dias_cobertura` días al ritmo de A, menos el stock
  actual. El **veredicto** avisa si la demanda sigue (`REPONER`) o cayó
  (`REVISAR_DEMANDA`), para no comprar a ciegas algo estacional.

### Parámetros

| Param | Tipo | Default | Comentario |
|---|---|---|---|
| `a_desde`, `a_hasta` | date | — | Ventana A `YYYY-MM-DD` (inclusivas). O usar `preset`. |
| `b_desde`, `b_hasta` | date? | — | Ventana B. Si se omiten → tramo reciente del mismo largo. |
| `preset` | enum? | — | `semana_anterior` \| `mes_anterior` \| `mismo_mes_ano_pasado` — llena A. |
| `office_id` | int? | `null` | Filtra a una sucursal. |
| `dias_cobertura` | int 1–180 | `30` | Días de stock a cubrir en la sugerencia. |
| `top` | int 1–3000 | `300` | Máx SKUs en el JSON (el Excel trae **todos**). |
| `orden` | enum | `sugerencia` | `sugerencia` \| `venta_a` \| `delta_venta`. |

Pasá `preset` **o** (`a_desde` + `a_hasta`). `b_*` van las dos juntas o ninguna.
Fechas inválidas o invertidas → HTTP 400.

```http
GET /compras/comparador?a_desde=2025-08-01&a_hasta=2025-08-31&orden=sugerencia
GET /compras/comparador?preset=mismo_mes_ano_pasado
```

### Shape de respuesta

```jsonc
{
  "meta": {
    "generado_at": "2026-08-19T14:00:00Z",
    "ventana_a": { "desde": "2025-08-01", "hasta": "2025-08-31", "dias": 31 },
    "ventana_b": { "desde": "2026-07-19", "hasta": "2026-08-18", "dias": 31 },
    "office_id": null,
    "office_scope": [1, 3],
    "dias_cobertura_objetivo": 30,
    "alertas": [
      "El stock de la ventana A no está disponible (el histórico de stock arranca el 2026-07-02)..."
    ],
    "nota": "Ventana A = referencia; Ventana B = comparación..."
  },
  "kpis": {
    "skus": 1850,
    "venta_a_pen": 135179, "venta_b_pen": 185387,
    "delta_venta_pct": 37.1,
    "sugerencia_unds_total": 25592,
    "por_veredicto": { "REPONER": 15, "REVISAR_DEMANDA": 808, "EN_ALZA": 1002, "EXCESO_STOCK": 10, "OK": 15 }
  },
  "por_departamento": [ { "departamento": "...", "skus": 120, "venta_a_pen": ..., "venta_b_pen": ..., "delta_venta_pct": ..., "sugerencia_compra": ... } ],
  "por_categoria":    [ { "departamento": "...", "categoria": "...", /* idem */ } ],
  "skus": [
    {
      "departamento": "Cuidado Personal y Salud",
      "categoria": "Higiene Personal", "subcategoria": "Toallitas Húmedas",
      "sku": "60301", "producto": "TOALLITA HUMEDA X100 BYWIN",
      "venta_a_pen": 3131, "venta_b_pen": 2332,
      "unds_a": 930, "unds_b": 685,
      "velocidad_a": 30.0, "velocidad_b": 22.1,     // rotación A vs B
      "delta_venta_pct": -25.5, "delta_velocidad_pct": -26.3,
      "stock_actual_unds": 1, "stock_actual_valor": 2.4,
      "stock_ventana_a_unds": null,                 // solo si A cae en el histórico de stock
      "cobertura_dias": 0.0,                        // stock / velocidad_b
      "sugerencia_compra": 898,                     // cubrir 30d al ritmo de A − stock
      "veredicto": "REPONER"                        // vendía bien y sigue, queda 1
    }
    // ... ordenados según `orden`
  ],
  "skus_total": 1850
}
```

### `GET /compras/comparador/excel`

Mismos parámetros (sin `top`/`orden`). Devuelve un `.xlsx` jerárquico
dpto→cat→subcat→SKU con **todos** los SKUs, subtotales por nivel, y las columnas
de comparación. Colores: 🟢 `REPONER`, 🟡 `REVISAR_DEMANDA`, 🔴 `EXCESO_STOCK`; la
columna de sugerencia se resalta en verde cuando es > 0.

### Veredictos

| Código | Cuándo | Acción |
|---|---|---|
| `REPONER` | vendía bien en A, sigue vendiéndose y queda poco stock | comprar |
| `REVISAR_DEMANDA` | vendía bien en A pero la rotación cayó | mirar antes de comprar (¿estacional? ¿discontinuado?) |
| `EN_ALZA` | rota más ahora que en A | vigilar / oportunidad |
| `EXCESO_STOCK` | mucho stock para lo que rota hoy | promocionar |
| `OK` / `SIN_MOVIMIENTO` | sin acción | — |

> **Límite de datos — stock histórico**: `stock_ventana_a_unds` (el stock que
> había al cierre de A) solo existe si A cae **después del 2026-07-02** (inicio de
> `stock_history`). Para fechas más viejas viene `null` y `meta.alertas` lo avisa;
> la comparación sigue siendo válida: **demanda de A vs stock actual**, que es la
> decisión de recompra real.

> **Aperturas escalonadas**: comparar contra fechas donde una sucursal aún no
> existía sesga los totales — si aplica, aparece en `meta.alertas`. Filtrá por
> `office_id` para comparar la misma tienda.

---

# Endpoints Admin

---

## Admin — Metas del mes (`/analytics/goals`)

Manejo de metas mensuales por sucursal. Se guardan en `app_config.sales_goals`.

### `GET /analytics/goals`

Devuelve todas las metas configuradas:

```jsonc
{
  "goals": {
    "2026-06": { "1": 125000.0, "3": 90000.0 },
    "2026-07": { "global": 250000.0 }
  }
}
```

### `PUT /analytics/goals` (operador/admin)

Carga/actualiza la meta de un mes (reemplaza la del mes completo):

```jsonc
// Request
{
  "month": "2026-07",
  "meta_global": 250000,                  // opcional
  "offices": { "1": 130000, "3": 95000 }  // por sucursal (claves = bsale_office_id)
}

// Response
{ "ok": true, "month": "2026-07", "saved": {...}, "goals": {...} }
```

---

## Admin — Category Targets (`/config/category-targets`)

Manejo de metas/roles por categoría (modelo 80/20).

### `GET /config/category-targets`

Lista todas las filas:

```jsonc
{
  "total": 24,
  "items": [
    {
      "category_id": 12,
      "categoria": "Menaje de Cocina",
      "departamento": "Hogar y Decoración",
      "bsale_office_id": 1,
      "sucursal": "KAWII MAGDALENA",
      "rol": "motor_1",
      "meta_mensual_pen": 8649,
      "pvp_min": 1.9, "pvp_max": 10.1,
      "margen_objetivo_pct": 61.4,
      "skus_min": 105, "skus_max": 157,
      "nota": "Generado automáticamente por /bootstrap"
    }
  ]
}
```

Filtros: `?office_id=1`

### `GET /config/category-targets/preview`

**No muta nada**. Muestra qué generaría el bootstrap:

```jsonc
{
  "total_sugerencias": 24,
  "exclusiones_aplicadas": { "departamentos": [...], "categorias": [] },
  "criterios": {
    "dias_min_con_venta": 75,
    "venta_min_90d_pen": 5000,
    "ticket_upsell_pen": 30,
    "dias_alta_frec": 85,
    "crecimiento_meta": "5%"
  },
  "items": [/* mismas filas que se insertarían */]
}
```

### `POST /config/category-targets/bootstrap?force=false` (operador/admin)

Carga inicial automática. **Idempotente**:

- Si tabla vacía → inserta sugerencias automáticas detectadas con datos reales.
- Si ya hay filas y `force=false` → **409 Conflict**.
- Si `force=true` → BORRA todo y recarga.

```jsonc
// Response 200
{
  "ok": true,
  "filas_insertadas": 24,
  "filas_borradas": 0,
  "force": false,
  "items": [/* las filas insertadas */]
}

// Response 409 (cuando ya hay datos)
{
  "detail": "Ya hay 24 filas en category_targets. Para reemplazar todo, llamar con ?force=true..."
}
```

### `PUT /config/category-targets/{category_id}/{office_id}` (operador/admin)

Edita campos individuales. Solo los campos enviados se modifican:

```jsonc
// Request
{
  "meta_mensual_pen": 10000,
  "margen_objetivo_pct": 65.0
}

// Response 200
{ "ok": true, "actualizado": { /* fila completa con los nuevos valores */ } }

// Response 404 si la fila no existe
```

### `DELETE /config/category-targets/{category_id}/{office_id}` (operador/admin)

Elimina la fila.

---

## Admin — Variant Costs (`/config/variant-costs`)

Auditoría y recuperación de costos de productos.

### `GET /config/variant-costs/audit`

Reporta cobertura de costos en variantes y venta:

```jsonc
{
  "variantes": {
    "total_activas": 3993,
    "con_costo": 3898,
    "sin_costo": 95,
    "recuperables": 0,
    "irrecuperables": 95,
    "pct_cobertura": 97.6
  },
  "ventas_ultimos_90d": {
    "venta_total": 584236,
    "venta_con_costo": 571813,
    "venta_sin_costo": 12423,
    "venta_sin_costo_pct": 2.1,
    "venta_recuperable_pct": 0.0,
    "cobertura_costos_pct": 97.9
  },
  "diagnostico": "OK — cobertura alta"
}
```

### `POST /config/variant-costs/backfill-from-receptions?dry_run=true` (operador/admin)

Recupera costos faltantes desde `reception_details`. **Idempotente**.

```jsonc
// Response (dry_run=true, no escribe)
{
  "dry_run": true,
  "candidatos_total": 3168,
  "actualizados": 3073,
  "saltados_sin_recep": 95,
  "sample": [/* primeros 10 cambios que se harían */],
  "nota": "Dry run — no se modificó nada."
}

// Response (dry_run=false, escribe)
{
  "dry_run": false,
  "candidatos_total": 0,
  "actualizados": 0,
  "saltados_sin_recep": 0,
  "nota": "Nada que actualizar."   // (porque el sync ya lo hace automático ahora)
}
```

> **Nota**: desde la tarea #24 el sync nocturno (`sync_variant_costs`) ya hace este
> fallback automáticamente. El endpoint queda como herramienta manual de auditoría.

---

## Flujo de onboarding para empresa nueva

Cuando una empresa nueva entra al sistema (DB propia), el flujo recomendado desde
el frontend es:

```
1. Login (POST /auth/login)
        ↓
2. Verificar sync de datos (mirar /pulse — si hay 0 ventas, esperar al primer sync)
        ↓
3. Cargar metas del mes en curso (PUT /analytics/goals)
        ↓
4. Bootstrap category_targets:
   a) Mostrar GET /config/category-targets/preview
   b) Usuario confirma → POST /config/category-targets/bootstrap
        ↓
5. Auditar cobertura de costos (GET /config/variant-costs/audit)
   - Si < 90% y hay recuperables → ofrecer botón
     "Recuperar costos desde recepciones" → POST /config/variant-costs/backfill...
        ↓
6. Dashboard arranca: GET /pulse, /diagnosis, /catalog-health, /plan
```

### Estado del sistema según cobertura

| `meta.cobertura_costos.estado` | Acción del frontend |
|---|---|
| `OK` (≥90%) | Sin acción. Mostrar margen tal cual. |
| `ADVERTENCIA` (70-89%) | Mostrar banner amarillo con `warning`. Botón "Auditar costos". |
| `CRITICA` (<70%) | Mostrar banner rojo con `warning`. Forzar al usuario a auditar antes de tomar decisiones de inversión. |

---

## Glosario

| Término | Definición |
|---|---|
| **Total** | Venta completa, incluye categorías estacionales. Es lo que el dueño cobró. |
| **Recurrente** | Venta excluyendo estacionales. La "base operativa". |
| **Estacional** | Categoría/departamento marcado como estacional en `app_config.excluded_departments`. Ejemplo en Kawii: "Temporada y Celebraciones" (Día del Padre, Navidad), "Juguetes y Juegos", "Librería y Oficina". |
| **Motor** | Categoría que más vende (motor_1 a motor_4 por sucursal). Asignado por el bootstrap. |
| **Fijo** | Categoría con alta frecuencia (vende casi todos los días) pero menor volumen que un motor. |
| **Complemento** | Categoría con volumen medio, no es motor ni fijo. |
| **Upsell** | Categoría con ticket promedio > S/30 (audio, calzado). |
| **Cobertura de costos** | % de la venta que tiene `variant_costs.effective_cost > 0`. Si baja, el margen está distorsionado. |
| **YoY** | "Year over Year" — vs mismo período 364 días atrás (52 semanas exactas para DoW-alignment). |
| **TDPV** | "Total Daily Per Variant" — velocidad de venta diaria de un SKU (unds/día). |
| **Sellthrough** | % de las unidades recibidas que ya se vendieron. Si baja del 20% → capital atrapado. |
| **Veredicto** | Lectura semafórica del momento del negocio (PROBLEMA_REAL, BAJON_ESTACIONAL, etc.). |
| **Pacing** | Distribución semanal de la meta del mes (suma exacta al monto total). |

---

## Notas finales

- **Las fechas viajan en ISO 8601** (`YYYY-MM-DD` para fechas calendario, ISO completo
  para timestamps).
- **Los montos en S/** (PEN). El frontend formatea con `Intl.NumberFormat('es-PE')`.
- **Día parcial**: HOY siempre se excluye (`hoy_excluido: true` en el meta). Todas
  las comparaciones cierran ayer.
- **Caché**: los endpoints son seguros para cachear ~5 minutos (los datos no cambian
  hasta el siguiente sync nocturno).
- **Errores**: el backend devuelve `{detail: "..."}` con código HTTP estándar (400,
  404, 409, 500).

### Versión del documento

Generado: 2026-06-30. Si agregás endpoints nuevos, actualizá esta doc.
