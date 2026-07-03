"""
KAWII — Wrapper del ETL diario multi-tenant.

Punto de entrada estable para cron jobs (Render Cron, Windows Task Scheduler,
systemd timer). Loopea todas las empresas activas y sincroniza cada una.

    python run_daily_sync.py [--days N] [--company-id X] [--skip-documents]

Toda la lógica vive en `tools.maintenance.mt_sync`; este archivo solo expone
esa lógica desde la raíz del repo para que la cron line sea siempre la misma
si algún día el módulo se mueve.
"""

from __future__ import annotations

import sys

from tools.maintenance.mt_sync import main


if __name__ == "__main__":
    sys.exit(main())
