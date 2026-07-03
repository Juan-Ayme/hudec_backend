# Manual del desarrollador

Setup local, cómo actualizar código, cómo hacer migraciones, tests.

## Índice

1. [Setup local desde cero](#1-setup-local-desde-cero)
2. [Estructura del código](#2-estructura-del-código)
3. [Correr el backend](#3-correr-el-backend)
4. [Correr el frontend](#4-correr-el-frontend)
5. [Hacer una migración SQL](#5-hacer-una-migración-sql)
6. [Agregar un endpoint nuevo](#6-agregar-un-endpoint-nuevo)
7. [Agregar una tabla nueva](#7-agregar-una-tabla-nueva)
8. [Actualizar dependencias](#8-actualizar-dependencias)
9. [Debug de RLS](#9-debug-de-rls)

## 1. Setup local desde cero

### Requisitos

- Windows 10/11 o Linux (probado en ambos)
- Git
- Python 3.14+
- Node.js 20+
- PostgreSQL 17+ con extensión `pgcrypto` habilitada

### Paso a paso

**1. Clonar el repositorio:**

```powershell
git clone <url-del-repo> kawii_analisis
cd kawii_analisis
```

**2. Instalar Postgres:**

Descargar desde postgresql.org/download/. Durante la instalación, anotar la password del usuario `postgres`.

**3. Crear la base de datos:**

```powershell
$env:PGPASSWORD='<pw postgres>'
& "C:\Program Files\PostgreSQL\18\bin\createdb.exe" -U postgres -h localhost kawii_mt
```

**4. Aplicar el schema multi-tenant:**

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h localhost -d kawii_mt -f backend_hudec\docs\migrations\2026-06-30_multitenant_schema.sql
```

Verificar: `SELECT count(*) FROM information_schema.tables WHERE table_schema='public'` debe dar 30.

**5. Aplicar la migración de cifrado de tokens:**

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h localhost -d kawii_mt -f backend_hudec\docs\migrations\2026-07-01_encrypt_bsale_token.sql
```

**6. Aplicar la migración de Row-Level Security:**

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h localhost -d kawii_mt -f backend_hudec\docs\migrations\2026-07-01_row_level_security.sql
```

**7. Crear el rol `hudec_app`:**

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h localhost -d kawii_mt -c "CREATE ROLE hudec_app LOGIN PASSWORD 'cambiar_este_password' NOSUPERUSER NOBYPASSRLS; GRANT USAGE ON SCHEMA public TO hudec_app; GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO hudec_app; GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO hudec_app; GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO hudec_app;"
```

**8. Configurar el `.env` del backend:**

Copiar `backend_hudec/.env.example` a `backend_hudec/.env` y editar:

- `DB_USER=hudec_app`, `DB_PASSWORD=<pw del paso 7>`
- `DB_NAME=kawii_mt`
- `JWT_SECRET=<generar con `python -c "import secrets; print(secrets.token_urlsafe(64))"`>`
- `TOKEN_ENCRYPTION_KEY=<generar igual>`
- `ADMIN_USERNAME=admin`, `ADMIN_PASSWORD=<pw fuerte>`

**9. Instalar dependencias del backend:**

```powershell
cd backend_hudec
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

**10. Instalar dependencias del frontend:**

```powershell
cd ..\frontend_hudec
npm install
```

**11. Configurar el `.env.local` del frontend:**

```powershell
copy .env.example .env.local
```

Editar `.env.local`:

```
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

**12. Arrancar backend y frontend (dos terminales):**

Terminal 1:
```powershell
cd backend_hudec
.\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Terminal 2:
```powershell
cd frontend_hudec
npm run dev
```

**13. Abrir en el navegador:**

http://localhost:3000/login

Loguearse con las credenciales de `.env` (`admin` / la password que pusiste).

**14. Cargar la primera empresa (Hudec):**

Ver [OPERACIONES.md → sección 1](OPERACIONES.md#1-crear-una-empresa-nueva).

## 2. Estructura del código

### Backend (`backend_hudec/`)

```
app/                        FastAPI web app
  main.py                   Entry point de uvicorn
  auth.py                   JWT + get_current_user + get_current_company
  config.py                 Settings de la app (lee .env)
  config_defaults.py        DEFAULT_THRESHOLDS, DEFAULT_COMPANY, etc.
  database.py               get_db() — session async de SQLAlchemy
  schemas.py                Pydantic schemas
  routers/                  Todos los endpoints organizados por dominio
    analytics.py              /analytics/* — KPIs, ventas por dimensión
    auth.py                   /auth/* — login, users
    bsale_admin.py            /bsale/* — mapeo product_types
    catalog_health.py         /catalog-health — vista 3 del dashboard
    category_targets.py       /config/category-targets — metas por categoría
    config_admin.py           /config/* — exclusions, thresholds, company, backups, goals
    costs_audit.py            /config/variant-costs/* — auditoría de costos
    diagnosis.py              /diagnosis — vista 2 del dashboard
    plan.py                   /plan — vista 4 del dashboard
    products.py               /products/*
    pulse.py                  /pulse — vista 1 del dashboard
    purchases.py              /purchases/decisions/*
    stock.py                  /stock/valuation
    sync.py                   /sync/* — disparar sync, ver logs
    taxonomy.py               /taxonomy/* — lectura del árbol
    taxonomy_admin.py         /taxonomy/* — CRUD taxonomía
    audits.py                 /audits/* — auditorías de datos
  kawii_matrix/             Módulo de matrices de clasificación KAWII
    router.py                 /matrix/* — endpoints REST
    service.py                Lógica de ejecución + cache invalidation
    cache.py                  In-memory cache keyed por (module_id, company_id)
    runtime_config.py         Lee overrides desde app_config
    schemas.py                Pydantic responses
    sql/                      Las 11 queries SQL de las matrices
      _matriz_90d_base.sql      CTE base compartida (1200+ líneas)
      04_matriz_90d.sql
      04b_matriz_90d_jerarquico.sql
      04h_rotacion_historica.sql
      05_matriz_operativa.sql
      06_historico_productos.sql
      07_informe_consolidado.sql
      08_transferencias.sql
      sku_detail.sql

harvester/                  Sync desde BSale (psycopg2 sync, no async)
  bsale_client.py             fetch(), paginate() — HTTP client con rate limit
  config.py                   Constantes de BSale + DB pool config
  db.py                       Pool de conexiones psycopg2 + execute_batch()
  sync_masters.py             Sync de entidades maestras (taxonomy, offices, products, ...)
  sync_transactions.py        Sync de documentos, recepciones, consumos
  tenant_context.py           Estado global del tenant activo (variable de módulo)

tools/                      Scripts standalone
  set_company_token.py        Crear empresa + guardar token cifrado
  set_company_config.py       Cargar config operativa de una empresa
  maintenance/
    mt_sync.py                  Orquestador multi-tenant del sync

Nueva_estructura/           JSONs de taxonomía por empresa (bootstrap inicial)
  kawii-pluss.json            Taxonomía inicial de Kawii Pluss
  coya-cosmetics.json         Taxonomía inicial de Coya Cosmetics

docs/                       Documentación (este directorio)
  README.md
  ARQUITECTURA.md
  OPERACIONES.md
  DESARROLLO.md
  PRODUCCION.md
  migrations/                 Migraciones SQL cronológicas (schema real)
```

### Frontend (`frontend_hudec/`)

```
src/
  app/                      Next.js App Router — cada carpeta es una ruta
    page.tsx                  /  — dashboard principal
    login/page.tsx            /login
    configuracion/page.tsx    /configuracion — tabs de config
    reportes/                 /reportes/*
    productos/page.tsx
    ...
    layout.tsx                Root layout — envuelve todo en Providers

  components/
    providers.tsx             QueryClient + AuthProvider + CompanyProvider + SucursalProvider
    auth-context.tsx          useAuth() — sesión + companies del user
    company-context.tsx       useCompany() + <CompanySelector />
    sucursal-context.tsx      useSucursal() + <SucursalSelector />
    app-shell.tsx             Sidebar + header + main
    nav.ts                    Configuración estática del menú
    ui/                       Componentes shadcn-like (button, card, input, ...)
    charts/                   D3/Recharts wrappers

  features/                 Lógica específica por feature
    compras-catalogo/
    ventas-jerarquicas/

  lib/
    api.ts                    Cliente HTTP tipado — inyecta X-Company-Id
    types.ts                  Interfaces TS de todas las respuestas del API
    format.ts, utils.ts, ...
```

## 3. Correr el backend

**En dev con reload automático:**

```powershell
cd backend_hudec
.\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

**Sin reload (más parecido a producción):**

```powershell
.\.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level info
```

Docs de la API interactivas: http://localhost:8000/docs

## 4. Correr el frontend

```powershell
cd frontend_hudec
npm run dev
```

Abre en http://localhost:3000. Los cambios en `.tsx` se recargan automáticamente.

**Build de producción:**

```powershell
npm run build
npm run start
```

## 5. Hacer una migración SQL

**Regla:** todo cambio de schema va en un archivo SQL versionado, nunca directo en producción.

**Formato:**

```
docs/migrations/YYYY-MM-DD_descripcion_corta.sql
```

**Contenido:**

```sql
-- =====================================================================
-- KAWII — <título de la migración>
-- =====================================================================
-- Contexto: por qué se hace este cambio.
-- Aplicar:
--   psql -U postgres -d kawii_mt -f docs/migrations/YYYY-MM-DD_...
-- =====================================================================

BEGIN;

-- ALTER TABLE / CREATE INDEX / etc.

COMMIT;
```

**Aplicar en local:**

```powershell
$env:PGPASSWORD='<pw postgres>'
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h localhost -d kawii_mt -f docs\migrations\<archivo>.sql
```

**En producción:** hacer `pg_dump` antes de aplicar. Ver [PRODUCCION.md → Backup](PRODUCCION.md).

## 6. Agregar un endpoint nuevo

**Ejemplo:** `GET /reportes/mi-nuevo-reporte` que devuelve algo por empresa.

**1. Elegir router.** Va en un router existente que tenga un prefijo lógico, o en uno nuevo.

**2. Escribir el endpoint:**

```python
# app/routers/mi_router.py
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.auth import CurrentCompany, get_current_company
from app.database import get_db

router = APIRouter(
    prefix="/reportes",
    tags=["reportes"],
    dependencies=[Depends(get_current_company)],  # activa RLS
)

@router.get("/mi-nuevo-reporte")
async def mi_nuevo_reporte(
    days: int = Query(30, ge=1, le=365),
    company: CurrentCompany = Depends(get_current_company),
    db: AsyncSession = Depends(get_db),
) -> dict:
    cid = company.company_id
    # SIEMPRE filtrar por company_id explícito (RLS es segunda capa)
    res = await db.execute(
        text("""
            SELECT COUNT(*) AS total, SUM(total_amount) AS ventas
            FROM documents
            WHERE company_id = :cid
              AND emission_date >= NOW() - :days * INTERVAL '1 day'
        """),
        {"cid": cid, "days": days},
    )
    row = res.mappings().one()
    return {"total": row["total"], "ventas": float(row["ventas"] or 0)}
```

**3. Registrar el router** en `app/main.py`:

```python
from app.routers import mi_router
app.include_router(mi_router.router)
```

**4. Probar:**

```powershell
curl.exe -X POST http://localhost:8000/auth/login -H "Content-Type: application/json" -d '{"username":"admin","password":"<pw>"}' -c cookies.txt
curl.exe -b cookies.txt -H "X-Company-Id: 1" "http://localhost:8000/reportes/mi-nuevo-reporte?days=30"
```

**5. Verificar aislamiento:**

Repetir con `-H "X-Company-Id: 2"` — debe devolver 0 (o los valores de la otra empresa, no los de Hudec).

## 7. Agregar una tabla nueva

**Ejemplo:** una tabla `mi_tabla` con datos por empresa.

**1. Escribir migration SQL:**

```sql
-- docs/migrations/2026-XX-XX_add_mi_tabla.sql
BEGIN;

CREATE TABLE mi_tabla (
    company_id integer NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    id serial NOT NULL,
    campo1 text NOT NULL,
    campo2 int,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, id)
);
CREATE INDEX idx_mi_tabla_company ON mi_tabla (company_id);

-- Habilitar Row-Level Security
ALTER TABLE mi_tabla ENABLE ROW LEVEL SECURITY;
ALTER TABLE mi_tabla FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON mi_tabla
  USING (current_company_id() IS NULL OR company_id = current_company_id())
  WITH CHECK (current_company_id() IS NULL OR company_id = current_company_id());

-- Grants para el rol hudec_app
GRANT SELECT, INSERT, UPDATE, DELETE ON mi_tabla TO hudec_app;
GRANT USAGE, SELECT ON SEQUENCE mi_tabla_id_seq TO hudec_app;

COMMIT;
```

**2. Aplicar la migración** (ver sección [5](#5-hacer-una-migración-sql)).

**3. Todas las queries** a la tabla incluyen `WHERE company_id = :cid`. Si te olvidás, RLS igual filtra (segunda capa de seguridad).

## 8. Actualizar dependencias

### Backend (Python)

```powershell
cd backend_hudec
.\.venv\Scripts\pip install --upgrade -r requirements.txt
```

Si se agrega una nueva dependencia:

1. Instalar: `.\.venv\Scripts\pip install nueva-dependencia`
2. Congelar: `.\.venv\Scripts\pip freeze > requirements.txt`
3. Commit del `requirements.txt`.

### Frontend (Node)

```powershell
cd frontend_hudec
npm outdated       # ver qué está atrasado
npm update         # actualiza patch/minor según ranges
npm audit          # ver vulnerabilidades
npm audit fix      # arreglar automáticamente
```

Si se agrega una dependencia nueva:

```powershell
npm install nueva-libreria
```

`package.json` y `package-lock.json` se actualizan automáticamente. Commitear ambos.

## 9. Debug de RLS

**Síntoma común:** una query devuelve 0 filas cuando debería devolver datos.

**Causa típica:** el endpoint no depende de `get_current_company`, entonces `SET LOCAL app.current_company` nunca se ejecutó → RLS bloquea porque `current_company_id()` es NULL para un rol NOBYPASSRLS.

**Cómo verificar:**

```sql
-- Con psql conectado como hudec_app
BEGIN;
SET LOCAL app.current_company = '1';
SELECT current_company_id();  -- debe dar 1
SELECT count(*) FROM documents;  -- debe dar los docs de company 1
ROLLBACK;
```

Si `current_company_id()` da NULL, verificar:
1. `SHOW app.current_company;` — ver si la variable está seteada.
2. `SELECT current_role;` — verificar que estás como `hudec_app` (no `postgres`).

**Cómo arreglar en el código:**

Agregar `Depends(get_current_company)` al router:

```python
router = APIRouter(
    prefix="/mi-endpoint",
    dependencies=[Depends(get_current_company)],  # ← esta línea
)
```

O en cada endpoint individualmente:

```python
async def mi_endpoint(
    company: CurrentCompany = Depends(get_current_company),
    ...
):
```

## Convenciones de estilo

- Python: PEP 8 con líneas hasta 100 chars. `black` como formatter (opcional).
- Comentarios en SQL: `-- ★ nota importante` para marcar decisiones no obvias.
- Nombres de tablas: plural en snake_case (`documents`, `stock_levels`).
- Nombres de columnas: snake_case. Los IDs de BSale se llaman `bsale_X_id` para distinguir del PK local.
- Docstrings de endpoints: explican QUÉ hace y QUÉ devuelve. Los detalles de "cómo" van en comentarios inline.
