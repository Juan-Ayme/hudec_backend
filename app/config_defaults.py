"""
Valores por defecto de los umbrales de clasificación.

★ Estos defaults son la SEMILLA inicial: cuando `app_config.thresholds` está vacía,
  los endpoints `GET /config/thresholds` devuelven este dict. Al guardar (PUT) por
  primera vez desde la UI, la DB queda como fuente de verdad y manda sobre estos
  valores. Igual patrón que las exclusiones (siembra → DB manda en vivo).

★ Coinciden con los defaults históricos del `.env` (`harvester/config.py`). Si en
  el futuro se conecta el SQL para usar estos valores en runtime (reemplazar
  literales por `:params`), entonces cambiar la DB cambiará el comportamiento real
  de las matrices. Por ahora la UI puede leer y editar pero los SQL siguen con
  literales — primero hay que hacer la sustitución con tests de regresión.
"""

from __future__ import annotations


DEFAULT_THRESHOLDS: dict[str, float | int] = {
    # Ventanas de análisis (días)
    "window_main_days": 90,
    "window_trend_split_days": 45,
    "window_recent_days": 30,
    "window_blind_spot_days": 180,
    "window_new_product_days": 15,
    "window_dead_days": 60,
    "piso_dias_lote": 7,
    "cobertura_objetivo_dias": 45,

    # Sell-through / pérdida
    "sellthrough_exito_ratio": 0.80,
    "loss_consumo_ratio": 0.50,
    "loss_venta_ratio": 0.20,

    # Tendencia
    "trend_grow_mult": 1.5,
    "trend_decay_mult": 0.7,

    # XYZ (estabilidad de demanda)
    "xyz_constante_pct": 20,
    "xyz_variable_pct": 8,

    # Velocidad / proyección mensual
    "proy_mes_alta": 30,
    "proy_mes_min_floor": 3,
    "proy_mes_min_cap": 10,
    "proy_mes_cat_ratio": 0.5,

    # Cobertura (días de stock)
    "cobertura_critica_dias": 15,
    "cobertura_baja_dias": 30,

    # Lote / lifetime
    "dias_absorcion_bestseller_max": 45,
    "dsv_quiebre_max_dias": 14,
    "lifetime_bestseller_min": 50,

    # Lote frenado (P15)
    "lote_frenado_stock_min": 5,
    "lote_frenado_proy_min": 10,
    "lote_frenado_proy30_max": 5,
    "lote_frenado_edad_min": 90,

    # Lento crónico (P18)
    "lento_cronico_dsv_min": 8,
    "lento_cronico_lifetime_max": 60,
    "lento_cronico_proy_max": 5,

    # Frescura del lote
    "recien_reabastecido_dias": 14,

    # Transferencias (módulo 08)
    "transfer_donor_stock_min": 5,

    # Recepciones (sanidad)
    "reception_qty_sanity_limit": 50000,
}


# Metadata para la UI: agrupa por sección y describe cada umbral.
# El frontend la usa para renderizar inputs con etiquetas legibles sin tener
# que duplicar la lista en TypeScript.
THRESHOLD_SECTIONS: list[dict] = [
    {
        "key": "ventanas",
        "title": "Ventanas de análisis (días)",
        "description": "Cuánto tiempo mira hacia atrás cada matriz para sus cálculos.",
        "fields": [
            {"key": "window_main_days", "label": "Ventana principal", "help": "Corte 'ahora vs antes' de las matrices. Cambiarla afecta TODOS los reportes."},
            {"key": "window_trend_split_days", "label": "Split de tendencia", "help": "Ventana recent vs anterior dentro de la principal."},
            {"key": "window_recent_days", "label": "Velocidad reciente", "help": "Últimos N días para reposición."},
            {"key": "window_blind_spot_days", "label": "Punto ciego", "help": "SKUs que vendieron entre N y la ventana principal."},
            {"key": "window_new_product_days", "label": "Producto nuevo (gracia)", "help": "Días para considerar un producto NUEVO."},
            {"key": "window_dead_days", "label": "Producto muerto", "help": "Sin venta en N días → 'muerto'."},
            {"key": "piso_dias_lote", "label": "Piso días de lote", "help": "Días mínimos de vida para calcular velocidad (anti-distorsión)."},
            {"key": "cobertura_objetivo_dias", "label": "Cobertura objetivo", "help": "Días de stock ideales (sugeridor de compra)."},
        ],
    },
    {
        "key": "sellthrough",
        "title": "Sell-through y pérdida",
        "description": "Qué porcentaje de lo recibido debe haber salido para considerar éxito o pérdida.",
        "fields": [
            {"key": "sellthrough_exito_ratio", "label": "Éxito (ratio)", "help": "≥ 80% de lo recibido salió = 'vendió todo'."},
            {"key": "loss_consumo_ratio", "label": "Consumo sospechoso", "help": "Consumos > 50% del recibido = sospecha de pérdida."},
            {"key": "loss_venta_ratio", "label": "Venta baja (pérdida)", "help": "Ventas < 20% del recibido + consumo alto = pérdida total."},
        ],
    },
    {
        "key": "tendencia",
        "title": "Tendencia",
        "description": "Cómo se compara la velocidad reciente vs la anterior.",
        "fields": [
            {"key": "trend_grow_mult", "label": "Multiplicador 'Creciendo'", "help": "v_recent > v_old × este valor → 📈"},
            {"key": "trend_decay_mult", "label": "Multiplicador 'Decayendo'", "help": "v_recent < v_old × este valor → 📉"},
        ],
    },
    {
        "key": "xyz",
        "title": "XYZ (estabilidad de demanda)",
        "description": "Frecuencia de venta para clasificar como constante, variable o errático.",
        "fields": [
            {"key": "xyz_constante_pct", "label": "Constante (%)", "help": "Frecuencia ≥ este % → constante."},
            {"key": "xyz_variable_pct", "label": "Variable (%)", "help": "Frecuencia ≥ este % → variable (resto = errático)."},
        ],
    },
    {
        "key": "velocidad",
        "title": "Velocidad / proyección mensual",
        "description": "Umbrales de rotación y adaptación por categoría.",
        "fields": [
            {"key": "proy_mes_alta", "label": "Alta rotación", "help": "≥ N unds/mes → alta rotación."},
            {"key": "proy_mes_min_floor", "label": "Piso mínimo (vendedor lento)", "help": "Floor del umbral adaptativo de 'vendedor lento'."},
            {"key": "proy_mes_min_cap", "label": "Cap máximo (vendedor lento)", "help": "Cap del umbral adaptativo y fallback sin baseline (= cap × ratio)."},
            {"key": "proy_mes_cat_ratio", "label": "Ratio sobre promedio categoría", "help": "Fracción de la velocidad típica de la categoría a exigir."},
        ],
    },
    {
        "key": "cobertura",
        "title": "Cobertura (días de stock)",
        "description": "Umbrales para clasificar inventario como crítico, bajo o sano.",
        "fields": [
            {"key": "cobertura_critica_dias", "label": "Cobertura crítica", "help": "≤ este número de días = borde de reposición urgente."},
            {"key": "cobertura_baja_dias", "label": "Cobertura baja", "help": "< este número de días = stock justo / crítico."},
        ],
    },
    {
        "key": "lote_lifetime",
        "title": "Lote y lifetime",
        "description": "Tiempo de absorción del lote y volumen vital.",
        "fields": [
            {"key": "dias_absorcion_bestseller_max", "label": "Días absorción bestseller", "help": "≤ N días en agotar el lote = bestseller real."},
            {"key": "dsv_quiebre_max_dias", "label": "Quiebre caliente (DSV)", "help": "Agotado pero vendió hace ≤ N días."},
            {"key": "lifetime_bestseller_min", "label": "Bestseller lifetime mínimo", "help": "≥ N unidades vendidas en toda su vida."},
        ],
    },
    {
        "key": "lote_frenado",
        "title": "Lote frenado (P15)",
        "description": "Saldo significativo que dejó de rotar.",
        "fields": [
            {"key": "lote_frenado_stock_min", "label": "Stock mínimo", "help": "Stock ≥ N = saldo significativo (no agonía)."},
            {"key": "lote_frenado_proy_min", "label": "Velocidad histórica mínima", "help": "Velocidad histórica del lote ≥ N."},
            {"key": "lote_frenado_proy30_max", "label": "Velocidad 30d máxima", "help": "Velocidad últimos 30d < N = ya no rota."},
            {"key": "lote_frenado_edad_min", "label": "Edad mínima (días)", "help": "SKU debe tener ≥ N días para considerarlo viejo."},
        ],
    },
    {
        "key": "lento_cronico",
        "title": "Lento crónico (P18)",
        "description": "Producto antiguo que nunca cuajó.",
        "fields": [
            {"key": "lento_cronico_dsv_min", "label": "Días sin venta mínimos", "help": "Sin venta ≥ N días (evita reglas de stock crítico)."},
            {"key": "lento_cronico_lifetime_max", "label": "Lifetime máximo", "help": "< N ventas en toda su vida."},
            {"key": "lento_cronico_proy_max", "label": "Proyección máxima", "help": "< N unds/mes de promedio lifetime."},
        ],
    },
    {
        "key": "frescura",
        "title": "Frescura del lote",
        "description": "Esperar a que un lote recién llegado madure.",
        "fields": [
            {"key": "recien_reabastecido_dias", "label": "Recién reabastecido", "help": "≤ N días desde última recepción = esperar a que madure."},
        ],
    },
    {
        "key": "transferencias",
        "title": "Transferencias (módulo 08)",
        "description": "Umbrales del sugeridor de transferencias inter-sucursal.",
        "fields": [
            {"key": "transfer_donor_stock_min", "label": "Stock mínimo del donante", "help": "Stock mínimo para considerar una sucursal como donante."},
        ],
    },
    {
        "key": "recepciones",
        "title": "Recepciones (sanidad)",
        "description": "Filtro anti-error de tipeo en cantidades de recepción.",
        "fields": [
            {"key": "reception_qty_sanity_limit", "label": "Cantidad máxima por línea", "help": "Cualquier línea > este valor se descarta (probable error de tipeo)."},
        ],
    },
]


# ──────────────────────────────────────────────────────────────────────────────
# Configuración por EMPRESA (marca + IDs operativos de BSale).
#
# Igual patrón que thresholds: app_config.company (JSON). Si la fila no existe,
# el endpoint GET devuelve estos defaults (que coinciden con lo que está
# corriendo en .env hoy). El primer PUT desde la UI siembra la DB.
#
# Nota: conectar esto al runtime de las matrices requiere cambiar
# `harvester/config.py` para leer de DB en vez de .env — está pendiente como
# fase 2 (mismo patrón que thresholds).
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_COMPANY: dict = {
    # Marca / white-label
    "brand_name": "",
    "classification_label": "Clasificación",
    # IDs operativos de BSale (vacíos por defecto: la empresa nueva los carga
    # desde la UI con multi-selects sobre el catálogo ya sincronizado).
    "offices_tienda": [],          # list[int]
    "office_almacen": None,        # int | None
    "tipos_venta": [],
    "tipos_devolucion": [],
    "tipos_traslado": [],
    "bsale_warehouse_user_ids": [],
    "target_categories": [],
}


# Metadata para la UI: agrupa por sección. Cada field declara su tipo de
# control para que el frontend renderice multi-select / single-select / text.
COMPANY_SECTIONS: list[dict] = [
    {
        "key": "marca",
        "title": "Marca (white-label)",
        "description": "Nombre interno y etiqueta de la columna de clasificación. Aparecen en la UI, en /docs y en los reportes.",
        "fields": [
            {"key": "brand_name", "label": "Nombre de la empresa", "kind": "text", "help": "Identificador interno (ej. 'hudec', 'kawii_bi')."},
            {"key": "classification_label", "label": "Etiqueta de Clasificación", "kind": "text", "help": "Cómo se llama la columna en los reportes (ej. 'Clasificación HUDEC')."},
        ],
    },
    {
        "key": "sucursales",
        "title": "Sucursales",
        "description": "Qué sucursales venden al público y cuál es el almacén central. Vienen del catálogo BSale tras el primer sync.",
        "fields": [
            {"key": "offices_tienda", "label": "Sucursales que venden", "kind": "multi_office", "help": "Sucursales públicas que generan ventas (excluye almacén central)."},
            {"key": "office_almacen", "label": "Almacén central", "kind": "single_office", "help": "Sucursal que solo recibe mercadería (no vende). Si no tenés almacén central, dejá en blanco."},
        ],
    },
    {
        "key": "documentos",
        "title": "Tipos de documento BSale",
        "description": "Qué tipos de documento de BSale cuentan como venta, devolución o traslado interno.",
        "fields": [
            {"key": "tipos_venta", "label": "Tipos de venta", "kind": "multi_document_type", "help": "Boletas, facturas, notas de venta, etc."},
            {"key": "tipos_devolucion", "label": "Tipos de devolución", "kind": "multi_document_type", "help": "Notas de crédito que descuentan ventas."},
            {"key": "tipos_traslado", "label": "Tipos de traslado interno", "kind": "multi_document_type", "help": "Movimientos entre sucursales (NO son ventas ni pérdidas)."},
        ],
    },
    {
        "key": "usuarios",
        "title": "Usuarios almaceneros",
        "description": "Solo las recepciones hechas por usuarios almaceneros cuentan como llegadas reales de mercadería. Las del resto (cajeros, admins) son ajustes contables.",
        "fields": [
            {"key": "bsale_warehouse_user_ids", "label": "Almaceneros", "kind": "multi_user", "help": "Los usuarios de BSale que hacen las recepciones de mercadería."},
        ],
    },
    {
        "key": "categorias",
        "title": "Categorías objetivo",
        "description": "Categorías a destacar en el reporte de salud del catálogo.",
        "fields": [
            {"key": "target_categories", "label": "Categorías destacadas", "kind": "multi_category", "help": "Las categorías que más le importan al negocio (top sellers, foco de campaña, etc.)."},
        ],
    },
]
