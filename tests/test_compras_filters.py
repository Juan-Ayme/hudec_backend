# -*- coding: utf-8 -*-
"""Tests del filtro compartido de compras (app/routers/_compras_filters.py).

El filtro decide qué línea de `receptions`/`reception_details` cuenta como una
compra real. Es lógica delicada y de error silencioso (un WHERE mal armado da
un número plausible pero equivocado), así que se testea:

  - El predicado de notas usa `NOT (… ILIKE ANY(…))`, no `NOT ILIKE ANY`, que
    para más de un prefijo es siempre verdadero.
  - Los flags (importaciones / traslados) prenden y apagan las cláusulas justas.
  - Los límites de sanidad salen de `app_config.thresholds` con fallback.
  - Los scopes: compras incluye el almacén; ventas no.
  - `merge()` detecta colisiones de parámetros.
"""

import asyncio

import pytest

from app.config_defaults import DEFAULT_THRESHOLDS
from app.routers._compras_filters import (
    ComprasFilter,
    build_compras_filter,
    scope_compras,
    scope_ventas,
)


# ── Fake AsyncSession ────────────────────────────────────────────────────────
# get_thresholds / get_company solo llaman `await db.scalar(text(sql), params)`
# y esperan el JSON crudo de app_config. Un fake que devuelve por `key` alcanza.


class _FakeSession:
    def __init__(self, by_key: dict[str, str | None]):
        self._by_key = by_key

    async def scalar(self, _stmt, params):
        return self._by_key.get(params.get("k"))


def _run(coro):
    return asyncio.run(coro)


# ── _note_not_like (vía el WHERE generado) ───────────────────────────────────


def test_notas_usa_not_envolviendo_any():
    """El bug clásico: `NOT x ILIKE ANY(arr)` en vez de `NOT (x ILIKE ANY(arr))`.

    El primero, con ≥2 patrones, es siempre TRUE y no filtra nada.
    """
    cf = _run(build_compras_filter(_FakeSession({}), 1))
    assert "NOT (COALESCE(r.note, '') ILIKE ANY(:cf_notas))" in cf.where
    # No debe existir la forma peligrosa sin paréntesis.
    assert "NOT COALESCE(r.note, '') ILIKE ANY" not in cf.where


def test_default_excluye_solo_ajustes_en_notas():
    """Por default las importaciones SÍ son compra: solo se excluyen ajustes."""
    cf = _run(build_compras_filter(_FakeSession({}), 1))
    assert cf.params["cf_notas"] == ["Ajuste de Stock%"]
    assert cf.incluir_importaciones is True


def test_sin_importaciones_agrega_prefijos():
    cf = _run(
        build_compras_filter(_FakeSession({}), 1, incluir_importaciones=False)
    )
    assert cf.params["cf_notas"] == [
        "Ajuste de Stock%",
        "Importar Stock%",
        "Stock importado%",
    ]
    assert cf.incluir_importaciones is False


# ── Traslados ────────────────────────────────────────────────────────────────


def test_default_excluye_traslados():
    cf = _run(build_compras_filter(_FakeSession({}), 1))
    assert "NOT r.is_transfer" in cf.where


def test_incluir_traslados_omite_la_clausula():
    cf = _run(build_compras_filter(_FakeSession({}), 1, incluir_traslados=True))
    assert "is_transfer" not in cf.where


# ── Sanidad qty/cost ─────────────────────────────────────────────────────────


def test_sanity_usa_defaults_sin_config():
    cf = _run(build_compras_filter(_FakeSession({}), 1))
    assert cf.qty_limit == float(DEFAULT_THRESHOLDS["reception_qty_sanity_limit"])
    assert cf.cost_limit == float(DEFAULT_THRESHOLDS["reception_cost_sanity_limit"])
    assert cf.params["cf_qty_max"] == cf.qty_limit
    assert cf.params["cf_cost_max"] == cf.cost_limit


def test_sanity_respeta_config_de_empresa():
    session = _FakeSession(
        {"thresholds": '{"reception_qty_sanity_limit": 8000, '
                       '"reception_cost_sanity_limit": 2500}'}
    )
    cf = _run(build_compras_filter(session, 1))
    assert cf.qty_limit == 8000.0
    assert cf.cost_limit == 2500.0


def test_sin_rd_omite_sanity():
    """Si la consulta no abre las líneas (`rd=None`), no hay qty/cost que filtrar."""
    cf = _run(build_compras_filter(_FakeSession({}), 1, rd=None))
    assert "qty_max" not in cf.where
    assert "cf_qty_max" not in cf.params
    # Pero las notas y el traslado sí siguen.
    assert "is_transfer" in cf.where


def test_where_nunca_vacio():
    """Peor caso (todo incluido, sin líneas) debe seguir siendo un WHERE válido."""
    cf = _run(
        build_compras_filter(
            _FakeSession({}), 1, rd=None, incluir_traslados=True
        )
    )
    assert cf.where.strip()  # no vacío
    # Con solo el filtro de notas activo, el WHERE es esa cláusula.
    assert "ILIKE ANY" in cf.where


# ── Alias configurables ──────────────────────────────────────────────────────


def test_alias_y_prefijo_personalizados():
    cf = _run(
        build_compras_filter(
            _FakeSession({}), 1, r="rec", rd="det", param_prefix="x_"
        )
    )
    assert "NOT rec.is_transfer" in cf.where
    assert "det.quantity" in cf.where
    assert "x_qty_max" in cf.params
    assert "cf_qty_max" not in cf.params


# ── Scopes ───────────────────────────────────────────────────────────────────


def test_scope_compras_incluye_almacen():
    session = _FakeSession(
        {"company": '{"offices_tienda": [1, 3], "office_almacen": 4}'}
    )
    assert _run(scope_compras(session, 1)) == [1, 3, 4]


def test_scope_ventas_excluye_almacen():
    session = _FakeSession(
        {"company": '{"offices_tienda": [1, 3], "office_almacen": 4}'}
    )
    assert _run(scope_ventas(session, 1)) == [1, 3]


def test_scope_office_puntual_manda():
    session = _FakeSession(
        {"company": '{"offices_tienda": [1, 3], "office_almacen": 4}'}
    )
    assert _run(scope_compras(session, 1, office_id=3)) == [3]
    assert _run(scope_ventas(session, 1, office_id=99)) == [99]


def test_scope_sin_almacen_configurado():
    session = _FakeSession({"company": '{"offices_tienda": [1, 3]}'})
    assert _run(scope_compras(session, 1)) == [1, 3]


def test_scope_almacen_ya_en_tiendas_no_duplica():
    session = _FakeSession(
        {"company": '{"offices_tienda": [1, 3, 4], "office_almacen": 4}'}
    )
    assert _run(scope_compras(session, 1)) == [1, 3, 4]


def test_scope_vacio_sin_config():
    """Empresa sin configurar: lista vacía, el llamador la trata como 'sin filtro'."""
    assert _run(scope_compras(_FakeSession({}), 1)) == []
    assert _run(scope_ventas(_FakeSession({}), 1)) == []


# ── merge() ──────────────────────────────────────────────────────────────────


def test_merge_combina_params():
    cf = _run(build_compras_filter(_FakeSession({}), 1))
    merged = cf.merge({"cid": 1, "desde": "2026-01-01"})
    assert merged["cid"] == 1
    assert merged["cf_qty_max"] == cf.qty_limit


def test_merge_detecta_colision():
    cf = _run(build_compras_filter(_FakeSession({}), 1))
    with pytest.raises(ValueError, match="chocan"):
        cf.merge({"cf_notas": "otra cosa"})


# ── ComprasFilter es inmutable ───────────────────────────────────────────────


def test_filter_es_frozen():
    cf = ComprasFilter(
        where="TRUE", params={}, qty_limit=1, cost_limit=1,
        incluir_importaciones=True,
    )
    with pytest.raises(Exception):
        cf.where = "x"  # type: ignore[misc]
