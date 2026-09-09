# -*- coding: utf-8 -*-
"""Tests de la lógica pura del ecosistema de compras (app/routers/compras.py).

El SQL se valida contra la DB en integración; acá se fija el clasificador de
veredicto y el mapeo de niveles, que son la parte con reglas de negocio.
"""

from app.routers.compras import _NIVEL_COLS, _f, _veredicto


# ── _veredicto ───────────────────────────────────────────────────────────────


def test_sin_reposicion_vende_sin_comprar():
    # compra=0 pero hubo venta → nadie repuso en la ventana
    assert _veredicto(mult=None, cobertura=None, compra=0, venta=5000) == "SIN_REPOSICION"


def test_sin_movimiento_ni_compra_ni_venta():
    assert _veredicto(mult=None, cobertura=None, compra=0, venta=0) == "SIN_MOVIMIENTO"


def test_riesgo_quiebre_gana_a_sobrecompra():
    # Cobertura muy baja manda aunque el multiplicador también sea bajo.
    assert _veredicto(mult=1.2, cobertura=0.3, compra=1000, venta=1200) == "RIESGO_QUIEBRE"


def test_sobrecompra_por_multiplicador_bajo():
    # Multiplicador < 1.5 con cobertura sana → se compró de más.
    assert _veredicto(mult=1.2, cobertura=1.5, compra=1000, venta=1200) == "SOBRECOMPRA"


def test_exceso_stock_por_cobertura_alta():
    # Multiplicador ok pero mucho stock dormido.
    assert _veredicto(mult=2.0, cobertura=4.0, compra=1000, venta=2000) == "EXCESO_STOCK"


def test_equilibrado():
    assert _veredicto(mult=2.0, cobertura=1.5, compra=1000, venta=2000) == "EQUILIBRADO"


def test_umbral_multiplicador_es_estricto_en_1_5():
    # Exactamente 1.5 NO es sobrecompra (la regla es < 1.5).
    assert _veredicto(mult=1.5, cobertura=1.5, compra=1000, venta=1500) == "EQUILIBRADO"


def test_cobertura_none_no_dispara_riesgo_ni_exceso():
    # Sin venta no hay cobertura; con compra y sin venta cae a sobrecompra si
    # el multiplicador es None → pero mult None no es < 1.5, así que EQUILIBRADO.
    assert _veredicto(mult=None, cobertura=None, compra=1000, venta=0) == "EQUILIBRADO"


# ── _f (coerción defensiva) ──────────────────────────────────────────────────


def test_f_convierte_none_a_cero():
    assert _f(None) == 0.0


def test_f_convierte_decimal_y_str():
    from decimal import Decimal
    assert _f(Decimal("12.50")) == 12.5
    assert _f("3.14") == 3.14
    assert _f("basura") == 0.0


# ── _NIVEL_COLS ──────────────────────────────────────────────────────────────


def test_niveles_son_jerarquicos():
    assert _NIVEL_COLS["departamento"] == ["department"]
    assert _NIVEL_COLS["categoria"] == ["department", "category"]
    assert _NIVEL_COLS["subcategoria"] == ["department", "category", "subcategory"]
