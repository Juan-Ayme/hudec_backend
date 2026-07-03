# Arquitectura del sistema

Sistema BI multi-tenant sobre datos sincronizados desde BSale. Este documento explica **cómo funciona por dentro**.

## Componentes

```
┌──────────────────────┐        ┌──────────────────────┐
│  Frontend Next.js    │───────▶│  Backend FastAPI     │
│  (browser)           │        │  (uvicorn)           │
└──────────────────────┘        └──────────┬───────────┘
                                           │
                                           ▼
                                ┌──────────────────────┐
                                │  PostgreSQL          │
                                │  (base kawii_mt)     │
                                └──────────────────────┘
                                           ▲
                                           │
┌──────────────────────┐                   │
│  BSale API           │                   │
│  api.bsale.io        │◀──────────────────┤
└──────────────────────┘                   │
                                ┌──────────┴───────────┐
                                │  Harvester (mt_sync) │
                                │  (cron diario)       │
                                └──────────────────────┘
```

**Cuatro procesos independientes:**
1. **Frontend** — Next.js sirve la UI, se conecta al backend en `NEXT_PUBLIC_API_BASE_URL`.
2. **Backend web** — FastAPI expuesto en `:8000`. Sirve todos los endpoints REST.
3. **Postgres** — la única base de datos. Todos los datos multi-tenant viven acá.
4. **Harvester** — script Python que corre en un cron diario. Baja datos de BSale de cada empresa y los guarda en Postgres. NO comparte proceso con el backend web.

## Modelo multi-tenant

**Estrategia:** shared database + shared schema + `company_id` en cada tabla.

- Una sola base de datos.
- Todas las empresas comparten las mismas tablas.
- Cada fila tiene una columna `company_id` que indica a qué empresa pertenece.
- Row-Level Security (RLS) de Postgres filtra automáticamente las filas visibles según la variable de sesión `app.current_company`.

### Tablas principales

**Tablas nuevas de multi-tenant:**

```
companies                 Empresas registradas
  ├── id (PK)
  ├── name, slug
  ├── bsale_token         Token cifrado con pgp_sym_encrypt (bytea)
  ├── brand_name, classification_label
  └── is_active

user_companies            Pivote N-a-N: usuario ↔ empresa
  ├── user_id
  ├── company_id
  └── role                admin | operador | viewer

app_users                 Usuarios GLOBALES del sistema
  ├── id (PK)
  ├── username (UNIQUE)
  ├── password_hash       bcrypt
  └── is_active
```

**Tablas espejo de BSale (17 tablas):**

Cada una tiene PK compuesta `(company_id, bsale_X_id)`:

```
offices, document_types, users (BSale users), product_types, products,
variants, variant_attribute_values, variant_costs, documents, document_details,
receptions, reception_details, consumptions, consumption_details,
stock_levels, stock_history, product_type_attributes
```

Ejemplo:

```sql
CREATE TABLE documents (
    company_id      integer NOT NULL REFERENCES companies(id),
    bsale_document_id integer NOT NULL,
    ...
    PRIMARY KEY (company_id, bsale_document_id)
);
```

**Por qué PK compuesta:** BSale asigna IDs por cuenta. Si Hudec tiene `bsale_office_id=1` y Coya también tiene un `bsale_office_id=1`, son sucursales completamente distintas. Sin la PK compuesta chocarían.

**Tablas locales (departments, categories, subcategories):**

Usan `id SERIAL` como PK simple más UNIQUE `(company_id, id)`:

```sql
CREATE TABLE departments (
    id serial PRIMARY KEY,
    company_id integer NOT NULL REFERENCES companies(id),
    name varchar(150) NOT NULL,
    slug varchar(180) NOT NULL,
    CONSTRAINT departments_company_name_key UNIQUE (company_id, name),
    CONSTRAINT departments_company_id_key   UNIQUE (company_id, id)  -- para FKs compuestas
);
```

**Tablas de config (aisladas por company_id + key):**

```
app_config                key-value por empresa
  PRIMARY KEY (company_id, key)
  ├── 'excluded_departments' → ["Temporada", ...]
  ├── 'thresholds'           → {window_main_days: 90, ...}
  ├── 'company'              → {offices_tienda: [1,3], tipos_venta: [10], ...}
  └── 'sales_goals'          → {"2026-01": {global: 30000, ...}}

app_config_history        Audit log de cambios de config

sync_log                  Historial de ejecuciones del harvester
data_quality_issues       Warnings del harvester (filas malformadas)
webhook_events            Eventos push de BSale (aún no activos)
purchase_decisions        Decisiones de compra registradas por el operador
category_targets          Metas por categoría × sucursal
```

## Autenticación y aislamiento

**Flujo de un request típico:**

```
1. Browser → POST /auth/login {username, password}
                ↓
   Backend valida contra app_users (bcrypt)
                ↓
   Genera JWT con {user_id, username}
                ↓
   Setea cookie kawii_session (httpOnly, SameSite=lax)
                ↓
   Devuelve {user, companies: [...]}  ← lista de empresas del user

2. Browser → GET /analytics/kpis?days=30
             Headers:
               Cookie: kawii_session=<JWT>
               X-Company-Id: 1
                ↓
   Backend get_current_user()      ← decodifica JWT, obtiene user_id
                ↓
   Backend get_current_company()   ← lee X-Company-Id, valida en user_companies
                ↓
   SET LOCAL app.current_company = '1'   ← activa el filtro RLS
                ↓
   Query ejecuta SELECT ... FROM documents WHERE ...
                ↓
   RLS filtra automáticamente: sólo filas con company_id = 1
                ↓
   Devuelve JSON
```

**El JWT NO contiene `company_id`.** Se cambia de empresa cambiando el header, sin re-emitir el token.

**Membresía requerida:** si el user manda `X-Company-Id: 2` pero no tiene fila en `user_companies (user_id, 2)`, el backend devuelve 403.

## Row-Level Security (RLS)

**Cómo funciona:**

Cada tabla con `company_id` tiene una policy que filtra las filas visibles según la variable de sesión:

```sql
CREATE POLICY tenant_isolation ON <tabla>
  USING (current_company_id() IS NULL OR company_id = current_company_id())
  WITH CHECK (current_company_id() IS NULL OR company_id = current_company_id());
```

Donde `current_company_id()` es una función que lee `current_setting('app.current_company')`.

**Rol dedicado `hudec_app`:**

Postgres `SUPERUSER` bypassa RLS siempre. Por eso el backend NO se conecta como `postgres`, se conecta como un rol dedicado sin superuser:

```sql
CREATE ROLE hudec_app LOGIN PASSWORD '<pw>' NOSUPERUSER NOBYPASSRLS;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO hudec_app;
```

En `.env`:
```
DB_USER=hudec_app
DB_PASSWORD=<pw del rol>
```

**Por qué `WITH CHECK` permite NULL:**

El harvester (mt_sync) NO setea `app.current_company` a nivel Postgres — inyecta el `company_id` directamente en cada INSERT. La policy es permisiva cuando `current_company_id()` es NULL, así los INSERTs masivos del harvester pasan.

## Cifrado de tokens BSale

Cada empresa tiene su propio token de BSale. Viven cifrados en `companies.bsale_token` con `pgcrypto`.

```sql
-- Al escribir:
UPDATE companies
SET bsale_token = pgp_sym_encrypt(:token, :master_key)
WHERE id = :cid;

-- Al leer (harvester):
SELECT pgp_sym_decrypt(bsale_token, :master_key)::text AS token
FROM companies WHERE id = :cid;
```

**La clave maestra** vive en `.env` como `TOKEN_ENCRYPTION_KEY`. Sin ella, los tokens en DB son basura ilegible.

**Regla de oro:** si se pierde `TOKEN_ENCRYPTION_KEY`, los tokens quedan irrecuperables y hay que reintroducir cada uno desde BSale. **Backupear en password manager**.

## Harvester (sync desde BSale)

`tools/maintenance/mt_sync.py` es el orquestador multi-tenant.

**Flujo:**
1. Lee `companies` activas con token cargado.
2. Por cada empresa:
   - Descifra el token con `TOKEN_ENCRYPTION_KEY`.
   - Setea el tenant activo en el contexto Python (`harvester/tenant_context.py`).
   - Corre las fases del sync: taxonomy → offices → users → product_types → products+variants → costs → stock → recepciones → consumos → documentos.
   - Cada INSERT incluye `company_id = current_company_id()`.
3. Al final imprime un resumen por empresa.

**Uso:**
```powershell
# Sync de todas las empresas activas, últimos 7 días de docs:
python -m tools.maintenance.mt_sync --days 7

# Sync de una sola empresa, últimos 30 días:
python -m tools.maintenance.mt_sync --company-id 2 --days 30

# Sync inicial completo de 2 años (Hudec):
python -m tools.maintenance.mt_sync --company-id 1 --days 730
```

**Cron recomendado:** una vez al día. Se puede correr manualmente cuando se necesite (ej. después de cargar una empresa nueva).

## Frontend

**Estructura clave (`frontend_hudec/src/`):**

```
components/
  auth-context.tsx       AuthProvider — sesión + lista de empresas
  company-context.tsx    CompanyProvider — empresa activa + selector
  sucursal-context.tsx   SucursalProvider — filtro por sucursal dentro de empresa
  app-shell.tsx          Layout con header (Company + Sucursal + UserMenu)

lib/
  api.ts                 Cliente HTTP — inyecta X-Company-Id automáticamente

app/                     Rutas Next.js App Router
  page.tsx               Dashboard principal
  configuracion/         Config editable por empresa
  reportes/              Tableros gerenciales
  ...
```

**Cómo se inyecta el `X-Company-Id`:**

`api.ts::readActiveCompanyId()` lee `localStorage.getItem('kawii.company')` en cada request. Si el user cambia de empresa (via el `CompanySelector`), se actualiza el localStorage y se recarga la página para que todas las queries de React Query se rearmen.

## Deuda técnica reconocida

**Cosas que no son bugs pero son mejoras conocidas:**

- Webhooks push de BSale NO están implementados en el modelo multi-tenant. Cuando se necesite: agregar `webhook_events.company_id` y un receiver que resuelva la empresa por el token o por un slug en la URL.
- Los IDs operativos (`OFFICES_TIENDA`, `TIPOS_VENTA`, etc.) viven en `app_config[cid, 'company']` por empresa. NO están en el `.env`. Se cargan desde la UI (`/configuracion → Empresa → Sugerir configuración`) o via script `tools/set_company_config.py`.

## Diagramas de referencia rápida

**Membresías N-a-N:**

```
app_users              user_companies            companies
┌────┬──────────┐      ┌─────┬─────┬───────┐    ┌────┬───────┐
│ id │ username │      │ uid │ cid │ role  │    │ id │ name  │
├────┼──────────┤      ├─────┼─────┼───────┤    ├────┼───────┤
│ 1  │ juana    │      │  1  │  1  │ admin │    │ 1  │ Hudec │
│ 2  │ pedro    │      │  1  │  2  │ admin │    │ 2  │ Coya  │
└────┴──────────┘      │  2  │  1  │ viewer│    └────┴───────┘
                        └─────┴─────┴───────┘

juana (id=1) → admin en Hudec + admin en Coya
pedro (id=2) → viewer en Hudec (no ve Coya)
```

**Aislamiento de datos por RLS:**

```
Query: SELECT * FROM documents

Sin SET LOCAL app.current_company:
  → RLS bloquea (current_company_id() = NULL)
  → 0 filas (para usuarios normales; hudec_app sí, es NOBYPASSRLS)

Con SET LOCAL app.current_company = '1':
  → RLS filtra: WHERE company_id = 1
  → Solo docs de Hudec

Con SET LOCAL app.current_company = '2':
  → RLS filtra: WHERE company_id = 2
  → Solo docs de Coya
```
