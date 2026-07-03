"""
Configuracion centralizada del Harvester.

Este archivo es el UNICO lugar donde se definen las credenciales y constantes
de conexion. Todo el resto del codigo las importa desde aqui.

Fuente de datos: archivo .env en la raiz del proyecto (produccion/.env).
NO editar credenciales directamente aqui; editarlas en .env.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Cargar el archivo .env que esta en produccion/ (un nivel arriba de este archivo)
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)


# =============================================================================
# CONFIGURACION DE LA API DE BSALE
# =============================================================================
# MULTI-TENANT: BSALE_TOKEN y BSALE_HEADERS ya NO son globales. El token
# vive por empresa en `companies.bsale_token` (cifrado) y se activa vía
# harvester/tenant_context.set_current_tenant(...) antes de cada sync.
# bsale_client._build_headers() lo lee del contexto en cada request.

# URL base de la API v1 de BSale (no cambia)
BSALE_BASE_URL: str = "https://api.bsale.io/v1"

# --- Limites y comportamiento del cliente HTTP ---
BSALE_MAX_RPS: int = 9      # Max requests por segundo (limite oficial de BSale)
BSALE_PAGE_SIZE: int = 50   # Registros por pagina (maximo que acepta BSale)
BSALE_TIMEOUT: int = 12     # Segundos antes de considerar un request como fallido.
                            # Antes era 25 — la P99 real es <2s; un timeout más bajo
                            # corta más rápido los stalls y deja al retry handler trabajar.
BSALE_MAX_RETRIES: int = 3  # Reintentos automaticos ante errores de red o 5xx
BSALE_MAX_WORKERS: int = 8  # Hilos paralelos para llamadas que se pueden paralelizar.
                            # El rate limit duro es 9 RPS (no 9 conexiones), por eso
                            # 8 workers tienen margen sin overflow. Subido de 6 → 8.
                             # (ej: costos de variantes, atributos de categorias)


# =============================================================================
# CONFIGURACION DE POSTGRESQL
# =============================================================================

# Todas las variables vienen del .env. Si no existen, os.environ lanza KeyError.
DB_NAME: str = os.environ["DB_NAME"]        # Nombre de la base de datos
DB_HOST: str = os.environ["DB_HOST"]        # Host del servidor Postgres (ej: localhost)
DB_PORT: str = os.environ["DB_PORT"]        # Puerto (normalmente 5432)
DB_USER: str = os.environ["DB_USER"]        # Usuario de Postgres
DB_PASSWORD: str = os.environ["DB_PASSWORD"]  # Contrasena de Postgres

# Diccionario listo para pasar a psycopg2.connect(**DB_CONFIG)
# keepalives_* previenen que Neon (Postgres serverless) mate conexiones idle
# durante syncs largos (costos, recepciones) que tardan >5 min entre queries.
DB_CONFIG: dict = {
    "host": os.getenv("DB_HOST", DB_HOST),
    "port": int(os.getenv("DB_PORT", DB_PORT)),
    "dbname": os.getenv("DB_NAME", DB_NAME),
    "user": os.getenv("DB_USER", DB_USER),
    "password": os.getenv("DB_PASSWORD", DB_PASSWORD),
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 5,
}


# =============================================================================
# SUCURSALES CONOCIDAS Y PARÁMETROS OPERATIVOS
# =============================================================================

# IDs de BSale para las sucursales de tienda (donde se hacen ventas al publico).
# Se usan en los reportes de ventas para EXCLUIR el almacen.
OFFICES_TIENDA: list[int] = [
    int(x.strip()) for x in os.environ.get("OFFICES_TIENDA", "1,3").split(",") if x.strip()
]

# ID del Almacen Central. Solo recibe recepciones, no hace ventas directas.
OFFICE_ALMACEN: int = int(os.environ.get("OFFICE_ALMACEN", "4"))

# IDs de documentos de venta y devoluciones de BSale
TIPOS_VENTA: list[int] = [
    int(x.strip()) for x in os.environ.get("TIPOS_VENTA", "1,10,50,51,52,53").split(",") if x.strip()
]
TIPOS_DEVOLUCION: list[int] = [
    int(x.strip()) for x in os.environ.get("TIPOS_DEVOLUCION", "9,40,43").split(",") if x.strip()
]

# IDs de documentos de TRASLADO INTERNO entre sucursales.
# En COYA (sistema actual) el id es 53. Si en otro deployment es 37, ajustar en .env.
# Se usa en los SQL de las matrices para no contar traslados como pérdidas
# (sell-through real = ventas + consumos + traslados).
TIPOS_TRASLADO: list[int] = [
    int(x.strip()) for x in os.environ.get("TIPOS_TRASLADO", "53").split(",") if x.strip()
]

# ─── Restricciones por NOMBRE (no por ID) ───────────────────────────────────
# CLAVE: los IDs de la taxonomía local (departments/categories) se REASIGNAN en cada
# re-seed del sync, así que referenciar por ID es frágil. Se configuran por NOMBRE,
# separados por "|" (pipe, NO coma: hay nombres con coma como "Vinos, Licores y
# Cervezas"). El ID actual se resuelve en cada consulta de matriz.
#
# Para replicar el sistema a otra empresa: basta con poner aquí los nombres de SUS
# departamentos. Las exclusiones se SIEMBRAN en la DB (app_config) al primer arranque
# y luego se pueden ajustar en vivo desde la pantalla Configuración (override).
def _names(env_key: str) -> list[str]:
    return [x.strip() for x in os.environ.get(env_key, "").split("|") if x.strip()]

EXCLUDED_DEPARTMENT_NAMES: list[str] = _names("EXCLUDED_DEPARTMENTS")
EXCLUDED_CATEGORY_NAMES: list[str] = _names("EXCLUDED_CATEGORIES")

# Departamentos ESTACIONALES (campaña): NO se excluyen; se clasifican con lógica de
# temporada (fuera de campaña → "TEMPORADA CERRADA" / "SOBRANTE" en vez de "MUERTO").
# También por NOMBRE; se resuelve a ID en cada consulta.
SEASONAL_DEPARTMENT_NAMES: list[str] = _names("SEASONAL_DEPARTMENTS")

# IDs de categorías objetivo a analizar (para el reporte de salud)
TARGET_CATEGORIES: list[int] = [
    int(x.strip()) for x in os.environ.get("TARGET_CATEGORIES", "228,221,145").split(",") if x.strip()
]


# =============================================================================
# IDs INTERNOS DE BSALE (POR EMPRESA — CRÍTICO)
# =============================================================================
# IDs de usuarios BSale considerados ALMACENEROS. Solo sus recepciones se
# toman como llegadas reales de mercadería; las demás (admins/cajeros) son
# ajustes contables y NO definen "última recepción" ni "lote actual".
# Antes era literal en SQL (IN (2, 4, 5, 14, 16)). Ahora se pasa como param.
BSALE_WAREHOUSE_USER_IDS: list[int] = [
    int(x.strip()) for x in os.environ.get("BSALE_WAREHOUSE_USER_IDS", "2,4,5,14,16").split(",") if x.strip()
]

# Tope de sanidad para la cantidad de una línea de recepción.
# Filtra códigos de barras tipeados como cantidad (qty=7501234567890).
RECEPTION_QTY_SANITY_LIMIT: int = int(os.environ.get("RECEPTION_QTY_SANITY_LIMIT", "50000"))


# =============================================================================
# VENTANAS DE ANÁLISIS (DÍAS) — POLÍTICA DEL NEGOCIO
# =============================================================================
# Cambiar estos valores afecta la SEMÁNTICA de las matrices. Los nombres de
# columnas ("Unds Vend (90d)") están fijos en SQL y no se renombran solos.
WINDOW_MAIN_DAYS:        int = int(os.environ.get("WINDOW_MAIN_DAYS",         "90"))
WINDOW_TREND_SPLIT_DAYS: int = int(os.environ.get("WINDOW_TREND_SPLIT_DAYS",  "45"))
WINDOW_RECENT_DAYS:      int = int(os.environ.get("WINDOW_RECENT_DAYS",       "30"))
WINDOW_BLIND_SPOT_DAYS:  int = int(os.environ.get("WINDOW_BLIND_SPOT_DAYS",  "180"))
WINDOW_NEW_PRODUCT_DAYS: int = int(os.environ.get("WINDOW_NEW_PRODUCT_DAYS",  "15"))
WINDOW_DEAD_DAYS:        int = int(os.environ.get("WINDOW_DEAD_DAYS",         "60"))
PISO_DIAS_LOTE:          int = int(os.environ.get("PISO_DIAS_LOTE",            "7"))
COBERTURA_OBJETIVO_DIAS: int = int(os.environ.get("COBERTURA_OBJETIVO_DIAS",  "45"))


# =============================================================================
# UMBRALES DE CLASIFICACIÓN (POLÍTICA COMERCIAL)
# =============================================================================
# Sell-through / pérdida
SELLTHROUGH_EXITO_RATIO: float = float(os.environ.get("SELLTHROUGH_EXITO_RATIO", "0.80"))
LOSS_CONSUMO_RATIO:      float = float(os.environ.get("LOSS_CONSUMO_RATIO",      "0.50"))
LOSS_VENTA_RATIO:        float = float(os.environ.get("LOSS_VENTA_RATIO",        "0.20"))

# Tendencia
TREND_GROW_MULT:  float = float(os.environ.get("TREND_GROW_MULT",  "1.5"))
TREND_DECAY_MULT: float = float(os.environ.get("TREND_DECAY_MULT", "0.7"))

# XYZ
XYZ_CONSTANTE_PCT: int = int(os.environ.get("XYZ_CONSTANTE_PCT", "20"))
XYZ_VARIABLE_PCT:  int = int(os.environ.get("XYZ_VARIABLE_PCT",   "8"))

# Velocidad / proyección
PROY_MES_ALTA:        int   = int  (os.environ.get("PROY_MES_ALTA",        "30"))
PROY_MES_MIN_FLOOR:   int   = int  (os.environ.get("PROY_MES_MIN_FLOOR",    "3"))
PROY_MES_MIN_CAP:     int   = int  (os.environ.get("PROY_MES_MIN_CAP",     "10"))
PROY_MES_CAT_RATIO:   float = float(os.environ.get("PROY_MES_CAT_RATIO",  "0.5"))

# Cobertura
COBERTURA_CRITICA_DIAS: int = int(os.environ.get("COBERTURA_CRITICA_DIAS", "15"))
COBERTURA_BAJA_DIAS:    int = int(os.environ.get("COBERTURA_BAJA_DIAS",    "30"))

# Lote / lifetime
DIAS_ABSORCION_BESTSELLER_MAX: int = int(os.environ.get("DIAS_ABSORCION_BESTSELLER_MAX", "45"))
DSV_QUIEBRE_MAX_DIAS:          int = int(os.environ.get("DSV_QUIEBRE_MAX_DIAS",          "14"))
LIFETIME_BESTSELLER_MIN:       int = int(os.environ.get("LIFETIME_BESTSELLER_MIN",       "50"))

# Lote frenado (P15)
LOTE_FRENADO_STOCK_MIN:  int = int(os.environ.get("LOTE_FRENADO_STOCK_MIN",   "5"))
LOTE_FRENADO_PROY_MIN:   int = int(os.environ.get("LOTE_FRENADO_PROY_MIN",   "10"))
LOTE_FRENADO_PROY30_MAX: int = int(os.environ.get("LOTE_FRENADO_PROY30_MAX",  "5"))
LOTE_FRENADO_EDAD_MIN:   int = int(os.environ.get("LOTE_FRENADO_EDAD_MIN",   "90"))

# Lento crónico (P18)
LENTO_CRONICO_DSV_MIN:      int = int(os.environ.get("LENTO_CRONICO_DSV_MIN",       "8"))
LENTO_CRONICO_LIFETIME_MAX: int = int(os.environ.get("LENTO_CRONICO_LIFETIME_MAX", "60"))
LENTO_CRONICO_PROY_MAX:     int = int(os.environ.get("LENTO_CRONICO_PROY_MAX",      "5"))

# Frescura del lote
RECIEN_REABASTECIDO_DIAS: int = int(os.environ.get("RECIEN_REABASTECIDO_DIAS", "14"))

# Transferencias (umbrales propios del módulo 08, distintos de los del clasificador)
TRANSFER_DONOR_STOCK_MIN: int = int(os.environ.get("TRANSFER_DONOR_STOCK_MIN", "5"))


