# KAWII — Documentación del proyecto

Sistema BI multi-tenant sobre datos de BSale. Un solo backend + un solo frontend sirven a N empresas, cada una con sus propios datos, credenciales y configuración.

## Índice de documentación

| Archivo | Contenido |
|---|---|
| [ARQUITECTURA.md](ARQUITECTURA.md) | Cómo funciona el sistema por dentro: schema DB, aislamiento por empresa, cifrado, roles |
| [OPERACIONES.md](OPERACIONES.md) | Guía del operador: crear empresa, usuario, cargar token BSale, correr sync |
| [DESARROLLO.md](DESARROLLO.md) | Setup local, migraciones, actualizar código, tests |
| [PRODUCCION.md](PRODUCCION.md) | Deploy a producción, backup, monitoreo, rollback |
| [migrations/](migrations/) | Migraciones SQL aplicadas cronológicamente (los 3 archivos actuales = schema completo) |

## Empezar acá

**Si sos operador** (dueño / admin del negocio):
1. Lee [OPERACIONES.md](OPERACIONES.md) → sección "Crear una empresa nueva"
2. Necesitás el token de BSale de esa empresa a mano
3. El proceso es: crear empresa → cargar token → cargar config operativa → primer sync

**Si sos desarrollador** (agregar features, corregir bugs):
1. Lee [DESARROLLO.md](DESARROLLO.md) → sección "Setup local desde cero"
2. Repositorio: `backend_hudec` (FastAPI + PostgreSQL) y `frontend_hudec` (Next.js 16)
3. Convención: SQL crudo con `text()`, no ORM. Cada query filtra por `company_id`

**Si sos DevOps / SysAdmin** (deploy, mantener el sistema):
1. Lee [PRODUCCION.md](PRODUCCION.md) → sección "Requisitos"
2. Sistema: PostgreSQL 17+ con `pgcrypto`, Python 3.14, Node 20+
3. Componentes: 1 backend web + 1 frontend + 1 harvester (cron)

## Stack técnico

```
Frontend        Next.js 16 (App Router) + React Query + Tailwind 4
Backend         FastAPI + SQLAlchemy (async) + psycopg2 (sync harvester)
Base de datos   PostgreSQL 17+ con pgcrypto + Row-Level Security
Cron            harvester/mt_sync.py — descarga BSale cada 24h
Auth            JWT en cookie httpOnly + membresías por empresa
```

## Multi-tenant en 30 segundos

**Una sola instalación sirve a muchas empresas:**

```
                          Frontend Next.js
                          (single deployment)
                                 │
                                 │ header: X-Company-Id: N
                                 │ cookie: kawii_session (JWT)
                                 ▼
                          Backend FastAPI
                          (single deployment)
                                 │
                                 │ SET LOCAL app.current_company = N
                                 ▼
                          PostgreSQL
                          (single DB)
                     ┌───────────┴───────────┐
                     │ RLS filtra por        │
                     │ company_id automátic. │
                     └───────────────────────┘
```

**Puntos clave:**
- Cada tabla tiene una columna `company_id` — filas de distintas empresas viven en la misma tabla, aisladas por Row-Level Security de Postgres.
- El JWT lleva sólo el `user_id`. El header `X-Company-Id` decide qué empresa se está viendo. La tabla `user_companies` valida la membresía (403 si no está autorizado).
- Los tokens de BSale de cada empresa viven cifrados en `companies.bsale_token` con `pgcrypto` (clave maestra en `.env` como `TOKEN_ENCRYPTION_KEY`).

Para el detalle técnico completo ver [ARQUITECTURA.md](ARQUITECTURA.md).

## Convenciones importantes

**Al agregar código nuevo:**
- Toda query SQL nueva DEBE llevar el filtro `company_id`. RLS es una segunda capa de seguridad, pero el filtro explícito es la primera.
- Endpoints nuevos declaran `company: CurrentCompany = Depends(get_current_company)` como parámetro. Esto activa RLS automáticamente.
- Los tokens y secretos NUNCA se logean ni se devuelven en JSON. `getpass` para input, `pgp_sym_encrypt` para guardar.

**Al modificar el schema:**
- Escribir una migration en `docs/migrations/YYYY-MM-DD_descripcion.sql`
- Nunca ejecutar `ALTER TABLE` directo en producción — siempre via migration versionada
- Si se agrega una tabla con `company_id`, añadir la policy RLS correspondiente

**Al hacer commit:**
- Nunca commitear `.env`, `.env.local`, o cualquier archivo con secretos
- El archivo `.env.example` sí se commitea (sin valores reales)
- Los archivos JSON de taxonomía por empresa (`Nueva_estructura/{slug}.json`) sí se commitean

## Contacto y soporte

- Repositorio: `kawii_analisis/`
- Docs internos: este archivo y los enlazados
- Logs: ver [OPERACIONES.md](OPERACIONES.md) → "Ver logs del sistema"
