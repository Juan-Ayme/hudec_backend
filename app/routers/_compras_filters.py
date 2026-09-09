"""Filtros compartidos para el análisis de COMPRAS (tabla `receptions`).

`receptions` NO es una tabla de compras. Mezcla cuatro cosas distintas y solo
dos son mercadería que la empresa efectivamente pagó:

  1. Recepción manual        → COMPRA. El almacenero carga línea por línea.
  2. "Importar Stock"        → COMPRA. Carga masiva por CSV; es el flujo normal
                               de recepción en Kawii, NO una carga inicial de
                               catálogo (ver nota de validación abajo).
  3. "Ajuste de Stock"       → NO es compra. Corrección contable de inventario.
  4. Traslado interno        → NO es compra. Mercadería que ya se había comprado
                               antes y solo cambia de sucursal (`is_transfer`).

Encima de eso hay líneas con valores imposibles: códigos de barras tipeados en
el campo cantidad. En Kawii hay dos casos que solos representan S/ 4.3 billones
—más que toda la venta histórica— y contaminan cualquier agregado que no los
filtre. Ver `tools/maintenance/flag_reception_outliers.py`.

Este módulo es el ÚNICO lugar donde vive ese criterio. Cualquier consulta nueva
sobre compras debe usarlo en vez de reescribir el predicado, porque el filtro
es fácil de olvidar y el error es silencioso: da un número plausible pero mal.

──────────────────────────────────────────────────────────────────────────────
Nota de validación — por qué "Importar Stock" SÍ cuenta como compra
──────────────────────────────────────────────────────────────────────────────
Es tentador excluirlo por el nombre. Los datos dicen que no (Kawii, ago-2026):

  · Aparece en los 15 meses de historia, entre 101 y 748 recepciones por mes.
    Una carga inicial de catálogo sería un pico único al principio.
  · Aparece en las 3 sucursales, incluido el almacén central desde el día que
    abrió (03-feb-2026) hasta hoy.
  · Los SKUs se importan repetidamente: 1 001 SKUs dos veces, 550 tres veces,
    hasta 11 veces. Una carga inicial sería 1 vez por SKU.
  · De 782 SKUs importados en los últimos 3 meses, 781 también vendieron.

Excluirlo subestimaría la compra en ~75 % (S/ 670 k en vez de S/ 1.96 M en
12 meses). Si en otra empresa el patrón fuera distinto, cambiar
`incluir_importaciones=False` en la llamada, no el default de este módulo.

──────────────────────────────────────────────────────────────────────────────
Asimetría de sucursales — importante
──────────────────────────────────────────────────────────────────────────────
Las COMPRAS entran por el almacén central; las VENTAS salen por las tiendas.
Filtrar las compras con `offices_tienda` pierde la mercadería que llega al
almacén — en Kawii son S/ 865 k de S/ 1.96 M, casi la mitad. Por eso
`scope_compras()` incluye el almacén por default y `scope_ventas()` no.

Uso típico:

    from app.routers._compras_filters import build_compras_filter, scope_compras

    cf = await build_compras_filter(db, company.company_id)
    rows = await db.execute(text(f'''
        SELECT rd.bsale_variant_id, SUM(rd.quantity * rd.cost) AS costo
          FROM reception_details rd
          JOIN receptions r ON r.company_id = rd.company_id
                           AND r.bsale_reception_id = rd.bsale_reception_id
         WHERE rd.company_id = :cid
           AND r.admission_date >= :desde
           AND {cf.where}
         GROUP BY 1
    '''), {**cf.params, "cid": cid, "desde": desde})
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config_defaults import DEFAULT_THRESHOLDS
from app.routers.config_admin import get_company, get_thresholds

# Prefijos de `receptions.note` que BSale escribe cuando la "recepción" es un
# ajuste contable de inventario y no mercadería que llegó. Se comparan con
# ILIKE '<prefijo>%', así que el texto que sigue (nombre del usuario) no importa.
AJUSTE_NOTE_PREFIXES: tuple[str, ...] = ("Ajuste de Stock",)

# Ídem para las cargas masivas. NO se excluyen por default — ver nota arriba.
IMPORT_NOTE_PREFIXES: tuple[str, ...] = ("Importar Stock", "Stock importado")


@dataclass(frozen=True)
class ComprasFilter:
    """Predicado SQL + sus parámetros, listo para concatenar en un WHERE.

    `where` nunca viene vacío (peor caso devuelve 'TRUE'), así que se puede
    interpolar sin chequear. Los parámetros van namespaced con `param_prefix`
    para no chocar con los del llamador.
    """

    where: str
    params: dict[str, Any]
    qty_limit: float
    cost_limit: float
    incluir_importaciones: bool

    def merge(self, extra: dict[str, Any]) -> dict[str, Any]:
        """Azúcar para `{**cf.params, **extra}` sin pisar los del filtro."""
        colisiones = set(extra) & set(self.params)
        if colisiones:
            raise ValueError(
                f"Los parámetros {sorted(colisiones)} chocan con los del filtro "
                f"de compras. Usá otro nombre o cambiá `param_prefix`."
            )
        return {**self.params, **extra}


def _note_not_like(alias: str, prefixes: tuple[str, ...], param: str) -> str:
    """`NOT (note ILIKE ANY(...))` — ojo que `NOT ILIKE ANY` significa otra cosa.

    `x NOT ILIKE ANY(arr)` es "existe un patrón que NO matchea", que para más de
    un prefijo es siempre verdadero. Lo correcto es negar el ANY completo.
    """
    if not prefixes:
        return "TRUE"
    return f"NOT (COALESCE({alias}.note, '') ILIKE ANY(:{param}))"


async def build_compras_filter(
    db: AsyncSession,
    company_id: int,
    *,
    r: str = "r",
    rd: str = "rd",
    incluir_importaciones: bool = True,
    incluir_traslados: bool = False,
    param_prefix: str = "cf_",
) -> ComprasFilter:
    """Arma el predicado de "esta línea de recepción es una compra real".

    Args:
        r:  alias de `receptions` en la consulta del llamador.
        rd: alias de `reception_details`. Si la consulta no lo joinea (por
            ejemplo, cuenta recepciones sin abrir sus líneas), pasar `rd=None`
            y el filtro de sanidad se omite.
        incluir_importaciones: dejar en True salvo que la empresa use "Importar
            Stock" solo para carga inicial de catálogo — validar con datos antes
            de cambiarlo, ver la nota del módulo.
        incluir_traslados: True solo para análisis de movimiento interno, nunca
            para gasto de compra (duplicaría mercadería ya comprada).
        param_prefix: namespace de los parámetros generados.

    Los límites de sanidad salen de `app_config.thresholds` por empresa, con
    fallback a `DEFAULT_THRESHOLDS`.
    """
    thresholds = await get_thresholds(db, company_id)
    qty_limit = float(
        thresholds.get(
            "reception_qty_sanity_limit",
            DEFAULT_THRESHOLDS["reception_qty_sanity_limit"],
        )
    )
    cost_limit = float(
        thresholds.get(
            "reception_cost_sanity_limit",
            DEFAULT_THRESHOLDS["reception_cost_sanity_limit"],
        )
    )

    p = param_prefix
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if not incluir_traslados:
        clauses.append(f"NOT {r}.is_transfer")

    excluir_notas: list[str] = [f"{pref}%" for pref in AJUSTE_NOTE_PREFIXES]
    if not incluir_importaciones:
        excluir_notas += [f"{pref}%" for pref in IMPORT_NOTE_PREFIXES]
    clauses.append(_note_not_like(r, tuple(excluir_notas), f"{p}notas"))
    params[f"{p}notas"] = excluir_notas

    # Sanidad: solo aplica si el llamador tiene las líneas a la vista.
    if rd:
        clauses.append(
            f"{rd}.quantity > 0 AND {rd}.quantity <= :{p}qty_max "
            f"AND {rd}.cost >= 0 AND {rd}.cost <= :{p}cost_max"
        )
        params[f"{p}qty_max"] = qty_limit
        params[f"{p}cost_max"] = cost_limit

    return ComprasFilter(
        where=" AND ".join(f"({c})" for c in clauses) or "TRUE",
        params=params,
        qty_limit=qty_limit,
        cost_limit=cost_limit,
        incluir_importaciones=incluir_importaciones,
    )


async def scope_compras(
    db: AsyncSession, company_id: int, office_id: int | None = None
) -> list[int]:
    """Sucursales donde ENTRA mercadería: tiendas + almacén central.

    Si `office_id` viene, se respeta tal cual (el usuario pidió una sucursal
    puntual). Si la empresa no tiene `offices_tienda` configurado, devuelve
    lista vacía — el llamador debe tratarlo como "sin filtro de sucursal"
    en vez de generar un `IN ()` inválido.
    """
    if office_id is not None:
        return [office_id]
    company = await get_company(db, company_id)
    ids = list(company.get("offices_tienda") or [])
    almacen = company.get("office_almacen")
    if almacen is not None and almacen not in ids:
        ids.append(almacen)
    return sorted(ids)


async def scope_ventas(
    db: AsyncSession, company_id: int, office_id: int | None = None
) -> list[int]:
    """Sucursales donde SALE mercadería: solo las que venden al público.

    Excluye el almacén central a propósito: sus documentos son mayoreo o
    movimiento interno facturado, no venta retail, y mezclarlos distorsiona
    ticket promedio, mix por categoría y comparaciones YoY.
    """
    if office_id is not None:
        return [office_id]
    company = await get_company(db, company_id)
    return sorted(company.get("offices_tienda") or [])
