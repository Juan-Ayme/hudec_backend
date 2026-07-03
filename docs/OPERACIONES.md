# Manual del operador

Guía paso a paso para las tareas frecuentes: crear empresa, usuario, cargar configuración, correr sync, ver logs.

## Índice

1. [Crear una empresa nueva](#1-crear-una-empresa-nueva)
2. [Crear un usuario para acceder al sistema](#2-crear-un-usuario-para-acceder-al-sistema)
3. [Cargar el token BSale de una empresa](#3-cargar-el-token-bsale-de-una-empresa)
4. [Cargar la configuración operativa](#4-cargar-la-configuración-operativa)
5. [Correr el primer sync](#5-correr-el-primer-sync)
6. [Sync recurrente (cron diario)](#6-sync-recurrente-cron-diario)
7. [Cambiar contraseña de un usuario](#7-cambiar-contraseña-de-un-usuario)
8. [Ver logs y estado del sistema](#8-ver-logs-y-estado-del-sistema)
9. [Rotar el token BSale de una empresa](#9-rotar-el-token-bsale-de-una-empresa)
10. [Backup y restore](#10-backup-y-restore)

## 1. Crear una empresa nueva

**Precondiciones:**
- Tenés el token BSale de la nueva empresa a mano.
- Conocés algunos IDs operativos de BSale (sucursales, tipos de doc). Opcional al inicio: se pueden completar después.
- Tenés acceso SSH / terminal al servidor donde corre el backend.

**Paso a paso:**

1. Abrir terminal en el directorio `backend_hudec/`.

2. Correr el script para crear la empresa y cargar el token:

    ```powershell
    cd backend_hudec
    .\.venv\Scripts\python.exe tools/set_company_token.py
    ```

    El script pregunta:
    - **Company ID**: un número entero único (ej. `3` si ya hay Hudec=1 y Coya=2).
    - **Nombre de la empresa**: cualquier texto (ej. "Distribuidora ABC").
    - **Slug**: identificador corto en minúsculas, sin espacios (ej. `abc`). Se usa para el nombre del archivo de taxonomía y URLs futuras.
    - **Token BSale**: se pega desde el portapapeles. NO se muestra al tipearlo. Se pide dos veces para confirmar.

3. Cargar la taxonomía de la empresa. Dos opciones:

   **A) Desde la UI (recomendado):**
   Loguearte con un admin de esa empresa, ir a `/taxonomia` → botón **"Importar"** → pegar el JSON → **"Importar"**.

   **B) Copiar el JSON al repo** (`Nueva_estructura/{slug}.json`):
   El sync inicial lo leerá si la DB está vacía. Después de la primera vez, la DB pasa a ser la fuente de verdad y el JSON del repo NO se vuelve a leer.

   La estructura del JSON es la misma en ambos casos:

    ```json
    {
      "Departamento 1": {
        "Categoría A": {
          "Subcategoría X": [],
          "Subcategoría Y": []
        }
      },
      "Departamento 2": { ... }
    }
    ```

4. Cargar la configuración operativa (ver sección [4](#4-cargar-la-configuración-operativa)).

5. Correr el primer sync (ver sección [5](#5-correr-el-primer-sync)).

### Sobre la taxonomía: la DB es la fuente de verdad

Desde la versión multi-tenant, **la taxonomía vive en la DB**, no en archivos del repo:

- **Primera vez:** si la DB está vacía para esa empresa, el sync la siembra desde `Nueva_estructura/{slug}.json` (fallback). También se puede sembrar desde la UI (`/taxonomia` → Importar).
- **Ediciones desde la UI:** persisten. Los syncs siguientes no las pisan.
- **Bootstrap idempotente:** volver a importar el mismo JSON no duplica ni pisa nada. Solo agrega lo que falta.
- **Exportar:** `/taxonomia` → botón **"Exportar"** descarga el árbol actual como JSON.

Endpoints backend:
- `POST /taxonomy/bootstrap` — importa un JSON (DO NOTHING sobre conflictos).
- `GET /taxonomy/export` — devuelve el árbol actual como JSON.

## 2. Crear un usuario para acceder al sistema

Hay tres formas:

### A) Bootstrap automático del primer admin

Al arrancar el backend por primera vez, si la tabla `app_users` está vacía, se crea un admin automáticamente con las credenciales de `.env`:

```
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin15-L
```

Este admin queda con membresía `admin` en la empresa `id=1` (Hudec).

**CAMBIAR EL PASSWORD** después del primer login desde la UI (Configuración → Usuarios).

### Flujo de acceso al sistema

Cuando un usuario hace login, el sistema decide qué mostrarle según cuántas empresas tenga acceso:

- **1 sola empresa** → entra directo al dashboard con esa empresa activa (auto-select).
- **2+ empresas** → aparece la página `/select-company` con cards para elegir explícitamente.
- **0 empresas** → mensaje "Sin empresas — pedí al admin que te agregue como miembro".

Una vez adentro, el user puede cambiar de empresa en cualquier momento:
- **Dropdown en el header** (siempre visible con 2+ empresas).
- **Botón "Cambiar empresa" en el UserMenu** (arriba a la derecha, sólo con 2+ empresas). Lleva de vuelta a `/select-company`.

### B) Crear usuario desde la UI (recomendado)

Un admin logueado va a **Configuración → Usuarios → Nuevo usuario**:

- Username (único global)
- Password (mínimo 6 chars)
- Rol EN ESTA EMPRESA: `admin`, `operador` o `viewer`

El user queda creado y con membresía en la empresa activa. Para darle acceso a otras empresas, un admin de esas empresas debe agregarlo desde SU UI.

### C) Crear usuario via SQL directo (avanzado)

Solo en casos donde la UI no está disponible o hay que hacer bootstrap manual:

```sql
-- 1) Crear el usuario global (password_hash con bcrypt cost 12)
INSERT INTO app_users (username, password_hash)
VALUES ('juana', '<hash_bcrypt>');

-- 2) Darle membresía en las empresas que corresponda
INSERT INTO user_companies (user_id, company_id, role)
VALUES
    (<user_id>, 1, 'admin'),
    (<user_id>, 2, 'viewer');
```

Para generar el hash bcrypt:

```powershell
cd backend_hudec
.\.venv\Scripts\python.exe -c "import bcrypt; print(bcrypt.hashpw(b'mi-password', bcrypt.gensalt()).decode())"
```

## 3. Cargar el token BSale de una empresa

Si la empresa ya existe pero necesitás actualizar su token (empresa nueva sin token, o rotación de credenciales):

```powershell
cd backend_hudec
.\.venv\Scripts\python.exe tools/set_company_token.py
```

- Company ID: el ID existente (ej. `2` para Coya).
- El script detecta que ya existe, dice "Se actualizará SU token."
- Pega el token nuevo dos veces.

**Al terminar:** verifica que el token se puede descifrar:

```powershell
$env:PGPASSWORD='<pw hudec_app>'
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U hudec_app -h localhost -d kawii_mt -c "SELECT id, name, octet_length(bsale_token) AS bytes FROM companies;"
```

Debería mostrar `bytes > 0` para las empresas con token cargado.

## 4. Cargar la configuración operativa

Cada empresa necesita configurar sus IDs de BSale (sucursales, tipos de documento, usuarios almaceneros). Hay dos formas:

### A) Desde la UI (recomendado)

Loguearse como admin de esa empresa, ir a **Configuración → Empresa**.

Si la empresa YA tiene datos syncados, apretar **"Sugerir configuración"**: el sistema analiza los datos y propone los IDs correctos automáticamente. El operador revisa, ajusta y guarda.

Si la empresa está vacía (sin sync), tipear los IDs manualmente en cada multi-select:
- Sucursales que venden (offices_tienda)
- Almacén central (office_almacen) — opcional
- Tipos de venta, devolución, traslado
- Usuarios almaceneros
- Categorías objetivo

### B) Desde terminal (para bootstrap)

Cuando la empresa aún no tiene sync (ej. Coya recién creada), la UI no puede "Sugerir". Usar el script:

```powershell
cd backend_hudec
.\.venv\Scripts\python.exe tools/set_company_config.py
```

El script pregunta interactivamente todos los IDs. Si es la empresa cuyos IDs viven en el `.env` (típicamente la primera / Hudec), se puede aceptar los defaults del `.env` apretando Enter en cada campo.

## 5. Correr el primer sync

**Precondiciones:**
- Empresa creada (paso [1](#1-crear-una-empresa-nueva)).
- Token BSale cargado (paso [3](#3-cargar-el-token-bsale-de-una-empresa)).
- Config operativa cargada (paso [4](#4-cargar-la-configuración-operativa)).
- JSON de taxonomía en `Nueva_estructura/{slug}.json`.

**Sync inicial completo (2 años de historia):**

```powershell
cd backend_hudec
.\.venv\Scripts\python.exe -m tools.maintenance.mt_sync --company-id 2 --days 730
```

- Toma entre 3 y 8 horas dependiendo del volumen de la empresa.
- BSale limita a 9 req/s — el sync respeta el rate limit.
- El log completo queda en `mt_sync.log`.

**Recomendación:** correrlo de noche o en background:

```powershell
Start-Job -ScriptBlock {
    Set-Location "C:\Users\juana\Documents\kawii_analisis\backend_hudec"
    .\.venv\Scripts\python.exe -m tools.maintenance.mt_sync --company-id 2 --days 730 2>&1 | Out-File mt_sync_output.log
}
```

**Sync rápido para validar el flujo (últimos 7 días):**

```powershell
.\.venv\Scripts\python.exe -m tools.maintenance.mt_sync --company-id 2 --days 7 --skip-stock-snapshot
```

Toma ~10-15 minutos y sirve para confirmar que el token es correcto, la config es correcta, y los INSERTs pasan sin error.

**Sync de TODAS las empresas activas (para cron diario):**

```powershell
.\.venv\Scripts\python.exe -m tools.maintenance.mt_sync --days 7
```

Sin `--company-id`, itera sobre todas las empresas con `is_active=true` y `bsale_token IS NOT NULL`.

## 6. Sync recurrente (cron diario)

En Windows (Task Scheduler):

1. Crear una tarea programada.
2. Trigger: diario a las 03:00 AM (o cuando prefieras).
3. Acción: ejecutar
    ```
    C:\Users\juana\Documents\kawii_analisis\backend_hudec\.venv\Scripts\python.exe
    ```
    con argumentos:
    ```
    -m tools.maintenance.mt_sync --days 7
    ```
    y directorio inicial:
    ```
    C:\Users\juana\Documents\kawii_analisis\backend_hudec
    ```
4. Guardar y probar con "Ejecutar" desde el Task Scheduler.

En Linux (cron):

```
# Editar crontab
crontab -e

# Añadir línea (sync todas las noches a las 3 AM)
0 3 * * * cd /opt/kawii/backend_hudec && ./.venv/bin/python -m tools.maintenance.mt_sync --days 7 >> /var/log/kawii/mt_sync.log 2>&1
```

## 7. Cambiar contraseña de un usuario

**Desde la UI** (usuario logueado): Configuración → Usuarios → click en el usuario → Reset password.

**Desde SQL** (bootstrap / recuperación):

```powershell
cd backend_hudec
$hash = .\.venv\Scripts\python.exe -c "import bcrypt; print(bcrypt.hashpw(b'nuevo-password', bcrypt.gensalt()).decode())"

$env:PGPASSWORD='<pw hudec_app>'
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U hudec_app -h localhost -d kawii_mt -c "UPDATE app_users SET password_hash = '$hash' WHERE username = 'admin';"
```

## 8. Ver logs y estado del sistema

**Backend web:**

Los logs van al stdout de uvicorn. Redirigir a archivo:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 2>&1 | Out-File backend.log
```

**Harvester (sync):**

Cada corrida escribe a `mt_sync.log` en la raíz del backend. También queda registrado en la tabla `sync_log`:

```sql
-- Ver últimas 20 corridas de sync
SELECT company_id, entity, status, started_at, finished_at,
       records_fetched, records_inserted, records_updated, records_skipped
FROM sync_log
ORDER BY started_at DESC
LIMIT 20;
```

Desde la UI: **Sync** en el menú lateral.

**Estado de la DB:**

```sql
-- Cuántas empresas activas
SELECT id, name, slug, is_active, octet_length(bsale_token) AS token_bytes
FROM companies;

-- Cuántos usuarios y sus membresías
SELECT u.username, uc.company_id, uc.role
FROM app_users u
LEFT JOIN user_companies uc ON uc.user_id = u.id
ORDER BY u.username, uc.company_id;

-- Volumen de datos por empresa
SELECT company_id,
       (SELECT count(*) FROM documents        WHERE company_id = c.id) AS docs,
       (SELECT count(*) FROM variants         WHERE company_id = c.id) AS variants,
       (SELECT count(*) FROM receptions       WHERE company_id = c.id) AS recepciones
FROM companies c;
```

## 9. Rotar el token BSale de una empresa

Si BSale invalidó el token viejo (rotación de seguridad, cambio de contraseña, etc.):

1. Obtener el token nuevo desde BSale.
2. Correr `set_company_token.py` con el mismo `company_id` — pisa el token viejo.
3. Ejecutar un sync manual para verificar que el token nuevo funciona:

    ```powershell
    .\.venv\Scripts\python.exe -m tools.maintenance.mt_sync --company-id <id> --days 1 --skip-stock-snapshot
    ```

Si sale error `401 Unauthorized`, el token nuevo también está mal.

## 10. Backup y restore

### Backup completo

```powershell
$env:PGPASSWORD='<pw postgres superuser>'
$fecha = Get-Date -Format 'yyyy-MM-dd_HH-mm'
& "C:\Program Files\PostgreSQL\18\bin\pg_dump.exe" -U postgres -h localhost -d kawii_mt --format=custom -f "backups/kawii_mt_$fecha.dump"
```

Almacenar en un disco distinto o en la nube (S3, Google Cloud Storage).

**Frecuencia recomendada:** diario, con retención de al menos 30 días.

### Restore

En una DB vacía:

```powershell
& "C:\Program Files\PostgreSQL\18\bin\createdb.exe" -U postgres -h localhost kawii_mt_restore
& "C:\Program Files\PostgreSQL\18\bin\pg_restore.exe" -U postgres -h localhost -d kawii_mt_restore backups/kawii_mt_2026-07-01.dump
```

**Importante:** después del restore, la clave maestra `TOKEN_ENCRYPTION_KEY` en `.env` DEBE ser la misma que cuando se hizo el backup. Sin ella, los tokens en `companies.bsale_token` no se pueden descifrar.

### Backup de la clave maestra

**Guardar `TOKEN_ENCRYPTION_KEY` en un password manager** (1Password, Bitwarden, KeePass). Sin ella, los tokens de BSale son irrecuperables.
