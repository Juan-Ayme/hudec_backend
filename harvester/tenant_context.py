"""
Contexto de tenant activo para el harvester (multi-tenant).

Antes de correr cualquier sync, el orquestador (run_daily_sync.py) llama a
`set_current_tenant(company_id, token, slug)` con los datos de la empresa
que se va a sincronizar. Después:

  - `bsale_client.fetch()` construye los headers con `current_token()`.
  - Los `INSERT INTO ...` del harvester incluyen `current_company_id()`.
  - `sync_taxonomy()` carga el JSON `Nueva_estructura/{current_slug()}.json`.

IMPLEMENTACIÓN: variable de módulo (no contextvars) para que los threads
del ThreadPoolExecutor la lean sin problema. El harvester procesa UNA
empresa a la vez, así que no hay riesgo de contaminación entre tenants.
Si en el futuro quisiéramos correr syncs de empresas en paralelo, habría
que migrar a contextvars + ctx.run() en cada pool.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tenant:
    company_id: int
    token: str
    slug: str


_current: Tenant | None = None


class NoTenantSetError(RuntimeError):
    """Se intentó usar el tenant activo sin haberlo seteado antes."""


def set_current_tenant(company_id: int, token: str, slug: str) -> Tenant:
    """Establece el tenant activo. Debe llamarse ANTES de cualquier operación
    del harvester (fetch a BSale o INSERT en la DB) para una empresa dada.

    Ejemplo:
        for company in active_companies:
            set_current_tenant(company.id, company.token, company.slug)
            sync_masters()
            sync_transactions()
    """
    global _current
    _current = Tenant(company_id=company_id, token=token, slug=slug)
    return _current


def clear_current_tenant() -> None:
    """Limpia el tenant activo (útil entre empresas en el mismo proceso)."""
    global _current
    _current = None


def current_tenant() -> Tenant:
    """Devuelve el tenant activo o levanta si no hay uno seteado."""
    if _current is None:
        raise NoTenantSetError(
            "No hay tenant activo. Llamar set_current_tenant(...) antes de "
            "usar el harvester. Esto suele significar que un script legacy "
            "está corriendo sin pasar por run_daily_sync.py."
        )
    return _current


def current_company_id() -> int:
    return current_tenant().company_id


def current_token() -> str:
    return current_tenant().token


def current_slug() -> str:
    return current_tenant().slug
