"""Resolución del scope de sucursales POR EMPRESA.

Antes, el conjunto de sucursales "que venden" vivía hardcodeado como
`OFFICE_IDS = (1, 3)` en `analytics/core/config.py` y se interpolaba en cada
query. Funcionaba de casualidad porque Kawii y Coya usan los mismos
`bsale_office_id` de tienda; una tercera empresa con otros IDs habría dado
números equivocados en silencio.

La fuente de verdad correcta es `app_config.company.offices_tienda`, editable
por empresa desde la UI. Este módulo la resuelve, con fallback a `OFFICE_IDS`
para empresas que todavía no la configuraron (comportamiento idéntico al viejo).

Dos formas de uso según lo que el llamador ya tiene a mano:

  - `office_scope_ids(company_config, office_id)` — SIN tocar la DB, cuando el
    endpoint ya cargó `company_config = await get_company(db, cid)` (el caso de
    casi todos los endpoints de analytics.py, que lo cargan para `tipos_venta`).
  - `resolve_office_scope(db, cid, office_id)` — hace la lectura por vos cuando
    no tenés el config cargado.

Los IDs se interpolan como enteros en el SQL (igual que antes). Vienen de
`Query(int)` de FastAPI o de la config validada, nunca de texto libre, así que
`office_filter()` es seguro frente a inyección; aun así fuerza `int()` como
segunda barrera.
"""

from __future__ import annotations

from contextvars import ContextVar

from sqlalchemy.ext.asyncio import AsyncSession

from analytics.core.config import OFFICE_IDS


# Scope de sucursales del request en curso. Lo usan los helpers que arman SQL en
# funciones "profundas" (sub-funciones de diagnosis/pulse) donde enhebrar el
# scope por parámetro a través de ~10 firmas sería ruidoso y fácil de olvidar.
# Es un ContextVar, no una global: cada request HTTP corre en su propia task
# async, así que dos empresas concurrentes no se pisan. El endpoint lo setea una
# vez con `set_request_office_scope`; si algún camino no lo setea, el getter cae
# a `OFFICE_IDS` (comportamiento histórico).
_current_office_scope: ContextVar[list[int] | None] = ContextVar(
    "current_office_scope", default=None
)


def set_request_office_scope(scope: list[int]) -> None:
    """Fija el scope de sucursales para el resto de este request."""
    _current_office_scope.set([int(i) for i in scope])


def request_office_scope() -> list[int]:
    """Scope seteado por el endpoint, o `OFFICE_IDS` si ninguno lo fijó."""
    val = _current_office_scope.get()
    return list(val) if val is not None else list(OFFICE_IDS)


def office_scope_ids(
    company_config: dict, office_id: int | None = None
) -> list[int]:
    """Sucursales que venden para esta empresa.

    Si `office_id` viene, gana (el usuario pidió una sucursal puntual). Si no,
    devuelve `offices_tienda` de la config; si la empresa no lo tiene cargado,
    cae a `OFFICE_IDS` para no cambiar el comportamiento histórico.
    """
    if office_id is not None:
        return [int(office_id)]
    ids = company_config.get("offices_tienda") or []
    return [int(i) for i in ids] if ids else list(OFFICE_IDS)


async def resolve_office_scope(
    db: AsyncSession, company_id: int, office_id: int | None = None
) -> list[int]:
    """Igual que `office_scope_ids` pero leyendo `company_config` por vos."""
    # Import local para evitar el ciclo con config_admin (que importa routers).
    from app.routers.config_admin import get_company

    company_config = await get_company(db, company_id)
    return office_scope_ids(company_config, office_id)


def office_filter(scope: list[int], *, alias: str = "") -> str:
    """Fragmento SQL `<alias.>bsale_office_id IN (id, id, …)`.

    `alias` es el prefijo de tabla (ej. 'doc' → 'doc.bsale_office_id IN (…)').
    Sin `alias`, la columna va pelada. Los IDs se fuerzan a int.
    """
    ids_sql = ", ".join(str(int(i)) for i in scope)
    prefix = f"{alias}." if alias else ""
    return f"{prefix}bsale_office_id IN ({ids_sql})"
