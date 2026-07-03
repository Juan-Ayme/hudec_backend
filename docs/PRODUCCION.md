# Deploy a producción

Cómo subir el sistema a un servidor de producción, mantenerlo actualizado, hacer backup, y rollback si algo sale mal.

## Índice

1. [Requisitos del servidor](#1-requisitos-del-servidor)
2. [Deploy inicial](#2-deploy-inicial)
3. [Configuración de Postgres para prod](#3-configuración-de-postgres-para-prod)
4. [Configuración del backend](#4-configuración-del-backend)
5. [Configuración del frontend](#5-configuración-del-frontend)
6. [Configurar el cron del harvester](#6-configurar-el-cron-del-harvester)
7. [Backup automático diario](#7-backup-automático-diario)
8. [Actualizar código (deploy de una versión nueva)](#8-actualizar-código-deploy-de-una-versión-nueva)
9. [Rollback](#9-rollback)
10. [Monitoreo y alertas](#10-monitoreo-y-alertas)
11. [Checklist pre-producción](#11-checklist-pre-producción)

## 1. Requisitos del servidor

**Mínimo:**
- Linux (Ubuntu 22.04+ recomendado) o Windows Server 2019+
- 4 GB RAM, 2 vCPUs, 50 GB disco (más 20 GB por cada empresa con 2+ años de historia)
- Python 3.14+
- Node.js 20+
- PostgreSQL 17+ (o servicio managed: Neon, RDS, Cloud SQL)
- Reverse proxy: nginx o Caddy (para HTTPS)

**Recomendado:**
- 8 GB RAM, 4 vCPUs, SSD
- Postgres managed con backups automáticos y point-in-time recovery
- Firewall que solo abra 80/443 al público. Los puertos de backend (8000) y Postgres (5432) NO deben ser accesibles desde internet.

## 2. Deploy inicial

**En el servidor:**

```bash
# 1. Instalar dependencias del sistema
sudo apt update
sudo apt install -y python3.14 python3.14-venv nodejs npm postgresql-17 postgresql-contrib-17 nginx

# 2. Clonar el repo
sudo mkdir -p /opt/kawii
cd /opt/kawii
sudo git clone <url-del-repo> .

# 3. Crear el usuario del sistema
sudo useradd -r -s /bin/false kawii
sudo chown -R kawii:kawii /opt/kawii
```

**Setup Postgres:**

```bash
# Crear DB y rol dedicado
sudo -u postgres psql <<EOF
CREATE DATABASE kawii_mt;
CREATE ROLE hudec_app LOGIN PASSWORD '<pw fuerte>' NOSUPERUSER NOBYPASSRLS;
\c kawii_mt
CREATE EXTENSION IF NOT EXISTS pgcrypto;
GRANT USAGE ON SCHEMA public TO hudec_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO hudec_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO hudec_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO hudec_app;
EOF

# Aplicar migraciones en orden (ver docs/migrations/)
sudo -u postgres psql -d kawii_mt -f /opt/kawii/backend_hudec/docs/migrations/2026-06-30_multitenant_schema.sql
sudo -u postgres psql -d kawii_mt -f /opt/kawii/backend_hudec/docs/migrations/2026-07-01_encrypt_bsale_token.sql
sudo -u postgres psql -d kawii_mt -f /opt/kawii/backend_hudec/docs/migrations/2026-07-01_row_level_security.sql
```

**Setup del backend:**

```bash
cd /opt/kawii/backend_hudec

# Crear venv e instalar deps
sudo -u kawii python3.14 -m venv .venv
sudo -u kawii ./.venv/bin/pip install -r requirements.txt

# Copiar y editar .env
sudo -u kawii cp .env.example .env
sudo -u kawii nano .env  # editar valores según sección 4
```

**Setup del frontend:**

```bash
cd /opt/kawii/frontend_hudec

sudo -u kawii npm install
sudo -u kawii cp .env.example .env.local
# Editar NEXT_PUBLIC_API_BASE_URL con la URL pública del backend
sudo -u kawii npm run build
```

## 3. Configuración de Postgres para prod

Editar `/etc/postgresql/17/main/postgresql.conf`:

```
# Conexiones
max_connections = 100
shared_buffers = 2GB           # 25% de RAM disponible
effective_cache_size = 6GB     # 75% de RAM
work_mem = 32MB
maintenance_work_mem = 256MB

# WAL / durability
wal_level = replica
max_wal_size = 4GB
checkpoint_completion_target = 0.9

# Logging
log_min_duration_statement = 1000  # loguear queries que tarden > 1s
log_line_prefix = '%t [%p]: [%l-1] user=%u,db=%d,app=%a,client=%h '

# Rendimiento del planner (importante para las matrices KAWII)
random_page_cost = 1.1          # SSD
effective_io_concurrency = 200  # SSD
```

Editar `/etc/postgresql/17/main/pg_hba.conf` para que `hudec_app` NO acepte conexiones desde internet:

```
# Solo permitir hudec_app desde localhost
host    kawii_mt   hudec_app   127.0.0.1/32   scram-sha-256
```

Reiniciar Postgres:

```bash
sudo systemctl restart postgresql
```

## 4. Configuración del backend

Editar `/opt/kawii/backend_hudec/.env`:

```
# Identidad
APP_NAME="KAWII API"
APP_VERSION=1.0.0
DEBUG=false                    # IMPORTANTE: false en prod
BRAND_NAME=""                  # se lee de app_config por empresa
TIMEZONE=America/Lima

# Postgres (rol dedicado, NOSUPERUSER)
DB_HOST=localhost
DB_PORT=5432
DB_NAME=kawii_mt
DB_USER=hudec_app
DB_PASSWORD=<pw del rol hudec_app>

# Auth
JWT_SECRET=<generar con `python -c "import secrets; print(secrets.token_urlsafe(64))"`>
JWT_EXPIRES_DAYS=7
COOKIE_SECURE=true             # HTTPS obligatorio
COOKIE_SAMESITE=lax

# Cifrado de tokens BSale (CRITICAL — guardar en password manager)
TOKEN_ENCRYPTION_KEY=<generar con secrets.token_urlsafe(64)>

# Admin inicial (solo se usa la primera vez que arranca)
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<pw fuerte, se cambia después del primer login>

# CORS — restringir al dominio real
CORS_ORIGINS=["https://kawii.tu-dominio.com"]
```

**Servicio systemd** en `/etc/systemd/system/kawii-backend.service`:

```ini
[Unit]
Description=KAWII Backend (FastAPI + uvicorn)
After=network.target postgresql.service

[Service]
Type=simple
User=kawii
Group=kawii
WorkingDirectory=/opt/kawii/backend_hudec
Environment=PATH=/opt/kawii/backend_hudec/.venv/bin
ExecStart=/opt/kawii/backend_hudec/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=on-failure
RestartSec=5s
StandardOutput=append:/var/log/kawii/backend.log
StandardError=append:/var/log/kawii/backend.log

[Install]
WantedBy=multi-user.target
```

Activar:

```bash
sudo mkdir -p /var/log/kawii
sudo chown kawii:kawii /var/log/kawii
sudo systemctl daemon-reload
sudo systemctl enable kawii-backend
sudo systemctl start kawii-backend
sudo systemctl status kawii-backend
```

## 5. Configuración del frontend

Editar `/opt/kawii/frontend_hudec/.env.local`:

```
NEXT_PUBLIC_API_BASE_URL=https://api.kawii.tu-dominio.com
```

Buildear con este `.env`:

```bash
cd /opt/kawii/frontend_hudec
sudo -u kawii npm run build
```

**Servicio systemd** en `/etc/systemd/system/kawii-frontend.service`:

```ini
[Unit]
Description=KAWII Frontend (Next.js)
After=network.target

[Service]
Type=simple
User=kawii
Group=kawii
WorkingDirectory=/opt/kawii/frontend_hudec
ExecStart=/usr/bin/npm run start -- --port 3000
Restart=on-failure
RestartSec=5s
StandardOutput=append:/var/log/kawii/frontend.log
StandardError=append:/var/log/kawii/frontend.log

[Install]
WantedBy=multi-user.target
```

**Nginx** como reverse proxy en `/etc/nginx/sites-available/kawii`:

```nginx
# Frontend
server {
    listen 443 ssl http2;
    server_name kawii.tu-dominio.com;
    ssl_certificate     /etc/letsencrypt/live/kawii.tu-dominio.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/kawii.tu-dominio.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
    }
}

# Backend API
server {
    listen 443 ssl http2;
    server_name api.kawii.tu-dominio.com;
    ssl_certificate     /etc/letsencrypt/live/api.kawii.tu-dominio.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.kawii.tu-dominio.com/privkey.pem;

    client_max_body_size 20M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;   # matrices grandes pueden tardar
    }
}

# Redirigir HTTP → HTTPS
server {
    listen 80;
    server_name kawii.tu-dominio.com api.kawii.tu-dominio.com;
    return 301 https://$host$request_uri;
}
```

Activar sitios y certificados SSL:

```bash
sudo ln -s /etc/nginx/sites-available/kawii /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

# Certbot para SSL Let's Encrypt
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d kawii.tu-dominio.com -d api.kawii.tu-dominio.com
```

## 6. Configurar el cron del harvester

Correr el sync todas las noches a las 3 AM:

```bash
sudo -u kawii crontab -e
```

Añadir:

```
0 3 * * * cd /opt/kawii/backend_hudec && ./.venv/bin/python -m tools.maintenance.mt_sync --days 3 >> /var/log/kawii/mt_sync.log 2>&1
```

**Notas:**
- `--days 3` = descarga los últimos 3 días de docs (por si hubo algún cambio retroactivo). El sync es idempotente (UPSERT).
- El log queda en `/var/log/kawii/mt_sync.log`. Rotar con logrotate (siguiente sección).

Alternativa managed: correr como job de Render Cron, Fly.io Machines, o Kubernetes CronJob si es un deploy en contenedor.

## 7. Backup automático diario

**Script en `/opt/kawii/scripts/backup.sh`:**

```bash
#!/bin/bash
set -e

BACKUP_DIR=/opt/kawii/backups
mkdir -p "$BACKUP_DIR"

FECHA=$(date +%Y-%m-%d_%H-%M)
FILE="$BACKUP_DIR/kawii_mt_$FECHA.dump"

# Backup con formato custom (comprimido)
sudo -u postgres pg_dump -d kawii_mt --format=custom -f "$FILE"

# Retener últimos 30 días
find "$BACKUP_DIR" -name "kawii_mt_*.dump" -mtime +30 -delete

# Opcional: subir a S3
# aws s3 cp "$FILE" s3://mi-bucket/kawii/
```

Cron a las 2 AM (una hora antes del sync):

```
0 2 * * * /opt/kawii/scripts/backup.sh >> /var/log/kawii/backup.log 2>&1
```

**Rotación de logs con logrotate**, en `/etc/logrotate.d/kawii`:

```
/var/log/kawii/*.log {
    daily
    rotate 30
    compress
    missingok
    notifempty
    copytruncate
}
```

## 8. Actualizar código (deploy de una versión nueva)

**Regla:** hacer backup ANTES de cualquier deploy con migraciones.

```bash
# 1. Backup preventivo
sudo -u postgres pg_dump -d kawii_mt --format=custom -f /opt/kawii/backups/pre_deploy_$(date +%Y-%m-%d).dump

# 2. Bajar el código nuevo
cd /opt/kawii
sudo -u kawii git pull origin main

# 3. Backend: actualizar dependencias si cambiaron
cd backend_hudec
sudo -u kawii ./.venv/bin/pip install -r requirements.txt

# 4. Aplicar migraciones nuevas (en orden)
ls docs/migrations/  # ver qué archivos hay
# Aplicar los que sean nuevos:
sudo -u postgres psql -d kawii_mt -f docs/migrations/2026-XX-XX_nueva_migracion.sql

# 5. Frontend: rebuild
cd ../frontend_hudec
sudo -u kawii npm install         # solo si package.json cambió
sudo -u kawii npm run build

# 6. Reiniciar servicios
sudo systemctl restart kawii-backend
sudo systemctl restart kawii-frontend

# 7. Verificar
curl -s https://api.kawii.tu-dominio.com/health   # debe dar 200
```

**Zero-downtime (opcional):**

Para deploys sin downtime, usar dos instancias del backend detrás de nginx (upstream con `least_conn`), y hacer rolling restart.

## 9. Rollback

Si el deploy sale mal:

**Backend/frontend (código):**

```bash
cd /opt/kawii
sudo -u kawii git log --oneline -5  # ver commits recientes
sudo -u kawii git checkout <commit-anterior>
cd backend_hudec
sudo -u kawii ./.venv/bin/pip install -r requirements.txt
cd ../frontend_hudec
sudo -u kawii npm install && sudo -u kawii npm run build
sudo systemctl restart kawii-backend kawii-frontend
```

**Base de datos (si aplicaste una migración destructiva):**

```bash
# Restore desde el backup pre-deploy
sudo systemctl stop kawii-backend kawii-frontend
sudo -u postgres dropdb kawii_mt
sudo -u postgres createdb kawii_mt
sudo -u postgres pg_restore -d kawii_mt /opt/kawii/backups/pre_deploy_YYYY-MM-DD.dump
sudo systemctl start kawii-backend kawii-frontend
```

**IMPORTANTE:** después de un restore, la `TOKEN_ENCRYPTION_KEY` en `.env` DEBE ser la misma que cuando se hizo el backup. Si no, los tokens no se pueden descifrar. Si perdiste la key, hay que reintroducir cada token desde BSale con `set_company_token.py`.

## 10. Monitoreo y alertas

**Mínimo indispensable:**

1. **Health check periódico** — cada 5 min pegarle a `https://api.kawii.tu-dominio.com/health`. Alerta si devuelve != 200.

2. **Disk space** — alerta cuando > 80%. Los backups y logs pueden llenar el disco.

3. **Postgres slow queries** — revisar `/var/log/postgresql/postgresql-17-main.log` con `log_min_duration_statement=1000`.

4. **Sync log** — el harvester debería completar en < 1h para syncs incrementales. Alerta si `sync_log.finished_at IS NULL` después de 2h.

    ```sql
    SELECT * FROM sync_log
    WHERE status = 'RUNNING' AND started_at < NOW() - INTERVAL '2 hours';
    ```

**Herramientas recomendadas:**
- Uptime: UptimeRobot, Better Uptime, Healthchecks.io (free tier)
- Métricas: Prometheus + Grafana (o managed: Datadog, New Relic)
- Logs: Loki + Grafana (self-hosted) o CloudWatch Logs

## 11. Checklist pre-producción

Antes del primer deploy a un ambiente que usen usuarios reales:

- [ ] `DEBUG=false` en `.env`
- [ ] `JWT_SECRET` y `TOKEN_ENCRYPTION_KEY` generados con `secrets.token_urlsafe(64)` (NO reutilizar valores de dev)
- [ ] `TOKEN_ENCRYPTION_KEY` guardado en password manager
- [ ] `COOKIE_SECURE=true` y HTTPS funcionando (certificado válido)
- [ ] `CORS_ORIGINS` restrictivo (NO `["*"]`)
- [ ] `DB_USER=hudec_app` (NO `postgres`) para que RLS aplique
- [ ] `ADMIN_PASSWORD` fuerte (no `admin` ni `admin15-L`)
- [ ] Cambiar la password del admin desde la UI después del primer login
- [ ] Backup diario automatizado y VERIFICAR que se pueden restaurar (hacer un restore de prueba en staging)
- [ ] Firewall: solo 80/443 público. 5432 y 8000 son de localhost
- [ ] Cron del harvester configurado y probado manualmente
- [ ] Monitoreo básico (uptime + disk) configurado
- [ ] Alerta cuando `sync_log` tenga status='FAILED'
- [ ] Documentado quién tiene las credenciales de emergencia (superuser postgres + TOKEN_ENCRYPTION_KEY)
- [ ] Runbook para incidentes comunes (backend caído, sync colgado, restore desde backup)
