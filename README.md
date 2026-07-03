# KAWII Backend — Consorcio Hudec

Backend FastAPI + PostgreSQL para el sistema BI multi-tenant del consorcio Hudec.
Un solo backend sirve a todas las empresas del grupo (Kawii Pluss, Coya Cosmetics, futuras)
con aislamiento por Row-Level Security de Postgres.

**Stack:** Python 3.14 · FastAPI · PostgreSQL 17 con `pgcrypto` + RLS · BSale API v1.

## Estructura rápida

```
app/               Backend FastAPI (async con SQLAlchemy + text() para SQL crudo)
harvester/         Cliente BSale + escritores a Postgres (sync)
tools/             Scripts: crear empresa, cargar config, mt_sync (cron)
docs/              Documentación completa (empezar por README.md)
Nueva_estructura/  JSONs de taxonomía por empresa (bootstrap inicial)
```

## Empezar

- **Setup local:** [docs/DESARROLLO.md](docs/DESARROLLO.md)
- **Operar el sistema:** [docs/OPERACIONES.md](docs/OPERACIONES.md)
- **Deploy a producción:** [docs/PRODUCCION.md](docs/PRODUCCION.md)
- **Cómo funciona por dentro:** [docs/ARQUITECTURA.md](docs/ARQUITECTURA.md)

## Correr en local (resumen)

```powershell
# 1) Backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# 2) Sync manual multi-tenant (una vez, después queda en cron)
.\.venv\Scripts\python.exe -m tools.maintenance.mt_sync --days 7

# 3) Sync una empresa específica
.\.venv\Scripts\python.exe -m tools.maintenance.mt_sync --company-id 1 --days 30
```

## Cargar una empresa nueva

```powershell
# 1) Crear la empresa y guardar su token BSale (cifrado con pgcrypto)
.\.venv\Scripts\python.exe tools\set_company_token.py

# 2) Cargar la config operativa (IDs BSale de sucursales, tipos de doc, etc.)
.\.venv\Scripts\python.exe tools\set_company_config.py
#    O desde la UI: /configuracion → Empresa → Sugerir configuración

# 3) Cargar la taxonomía inicial
#    Opción A: desde la UI en /taxonomia → botón "Importar" → pegar JSON
#    Opción B: poner Nueva_estructura/{slug}.json en el repo (bootstrap automático)

# 4) Primer sync completo (2 años de historia)
.\.venv\Scripts\python.exe -m tools.maintenance.mt_sync --company-id N --days 730
```

Detalle paso a paso: [docs/OPERACIONES.md → Crear una empresa nueva](docs/OPERACIONES.md).

## Notas

- El backend se conecta a Postgres con el rol **`hudec_app`** (NOSUPERUSER, NOBYPASSRLS).
  Un superuser bypassaría Row-Level Security. Ver `docs/ARQUITECTURA.md`.
- Los tokens de BSale de cada empresa viven cifrados en `companies.bsale_token` con `pgcrypto`.
  La clave maestra está en `.env` como `TOKEN_ENCRYPTION_KEY` — **backup obligatorio**.
- Cada request al backend lleva un header `X-Company-Id` que el frontend inyecta según la
  empresa activa. El backend valida contra `user_companies` y activa RLS via
  `SET LOCAL app.current_company`.
