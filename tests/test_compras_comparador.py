# -*- coding: utf-8 -*-
"""Tests de la lógica pura del comparador temporal (app/routers/compras.py).

El SQL se valida contra la DB en integración; acá se fijan la resolución de
ventanas (presets, defaults, los 400), el veredicto de recompra, el enriquecido
por SKU y la agregación por jerarquía.
"""

from datetime import date

import pytest
from fastapi import HTTPException

from app.routers.compras import (
    _agrupar_comparador,
    _enrich_sku_comparador,
    _preset_ventana_a,
    _resolver_ventanas_comparador,
    _veredicto_comparador,
)


# ── presets ──────────────────────────────────────────────────────────────────


def test_preset_mes_anterior():
    # Un miércoles de mediados de agosto → mes anterior = julio completo.
    a0, a1 = _preset_ventana_a("mes_anterior", date(2026, 8, 19))
    assert a0 == date(2026, 7, 1)
    assert a1 == date(2026, 7, 31)


def test_preset_mismo_mes_ano_pasado():
    a0, a1 = _preset_ventana_a("mismo_mes_ano_pasado", date(2026, 8, 19))
    assert a0 == date(2025, 8, 1)
    assert a1 == date(2025, 8, 31)


def test_preset_mismo_mes_diciembre_cruza_anio():
    # Diciembre del año pasado, con el rollover a enero para el fin de mes.
    a0, a1 = _preset_ventana_a("mismo_mes_ano_pasado", date(2026, 12, 10))
    assert a0 == date(2025, 12, 1)
    assert a1 == date(2025, 12, 31)


def test_preset_semana_anterior_es_lun_a_dom():
    # 2026-08-19 es miércoles → semana pasada = lun 10 .. dom 16 de agosto.
    a0, a1 = _preset_ventana_a("semana_anterior", date(2026, 8, 19))
    assert a0 == date(2026, 8, 10)
    assert a1 == date(2026, 8, 16)
    assert a0.weekday() == 0 and a1.weekday() == 6


def test_preset_desconocido_es_400():
    with pytest.raises(HTTPException) as e:
        _preset_ventana_a("trimestre", date(2026, 8, 19))
    assert e.value.status_code == 400


# ── resolución de ventanas ───────────────────────────────────────────────────


def test_ventana_b_default_mismo_largo_cierra_ayer():
    # A = 10 días; sin B → B = 10 días terminando ayer (hoy = 2026-08-19).
    a0, a1, b0, b1 = _resolver_ventanas_comparador(
        "2025-05-01", "2025-05-10", None, None, None, date(2026, 8, 19)
    )
    assert (a0, a1) == (date(2025, 5, 1), date(2025, 5, 10))
    assert b1 == date(2026, 8, 18)                    # ayer
    assert (b1 - b0).days == (a1 - a0).days            # mismo largo


def test_ventana_ambas_explicitas():
    a0, a1, b0, b1 = _resolver_ventanas_comparador(
        "2025-05-01", "2025-05-31", "2026-05-01", "2026-05-31", None, date(2026, 8, 19)
    )
    assert (b0, b1) == (date(2026, 5, 1), date(2026, 5, 31))


def test_ventana_preset_llena_A():
    a0, a1, _, _ = _resolver_ventanas_comparador(
        None, None, None, None, "mes_anterior", date(2026, 8, 19)
    )
    assert (a0, a1) == (date(2026, 7, 1), date(2026, 7, 31))


def test_ventana_sin_A_ni_preset_es_400():
    with pytest.raises(HTTPException) as e:
        _resolver_ventanas_comparador(None, None, None, None, None, date(2026, 8, 19))
    assert e.value.status_code == 400


def test_ventana_A_invertida_es_400():
    with pytest.raises(HTTPException):
        _resolver_ventanas_comparador(
            "2025-05-31", "2025-05-01", None, None, None, date(2026, 8, 19)
        )


def test_ventana_B_una_sola_fecha_es_400():
    with pytest.raises(HTTPException):
        _resolver_ventanas_comparador(
            "2025-05-01", "2025-05-31", "2026-05-01", None, None, date(2026, 8, 19)
        )


# ── veredicto ────────────────────────────────────────────────────────────────


def test_veredicto_sin_movimiento():
    assert _veredicto_comparador(0, 0, 100, 0, 30) == "SIN_MOVIMIENTO"


def test_veredicto_reponer_demanda_persiste():
    # Vendía (vel_a=5) y sigue (vel_b=5), falta stock → REPONER.
    assert _veredicto_comparador(5.0, 5.0, 2, sugerencia=148, cob=30) == "REPONER"


def test_veredicto_revisar_cuando_demanda_cayo():
    # Vendía bien (vel_a=10) pero ahora casi nada (vel_b=1), falta stock → REVISAR.
    assert _veredicto_comparador(10.0, 1.0, 0, sugerencia=300, cob=30) == "REVISAR_DEMANDA"


def test_veredicto_en_alza():
    # No vendía casi (vel_a=1) y ahora sí (vel_b=5), con stock de sobra → EN_ALZA.
    assert _veredicto_comparador(1.0, 5.0, 1000, sugerencia=0, cob=30) == "EN_ALZA"


def test_veredicto_exceso_stock():
    # Rota poco hoy y hay muchísimo stock (cobertura > 2.5×cob) → EXCESO_STOCK.
    # vel_a≈vel_b para no caer en EN_ALZA; sugerencia 0.
    assert _veredicto_comparador(1.0, 1.0, 200, sugerencia=0, cob=30) == "EXCESO_STOCK"


def test_veredicto_ok():
    assert _veredicto_comparador(1.0, 1.0, 30, sugerencia=0, cob=30) == "OK"


# ── enriquecido por SKU ──────────────────────────────────────────────────────


def test_enrich_calcula_velocidad_y_sugerencia():
    row = {
        "departamento": "Tecnología", "categoria": "Audio", "subcategoria": "Audífonos",
        "sku": "AUD-1", "producto": "Audífono X",
        "unds_a": 60, "soles_a": 3000, "unds_b": 30, "soles_b": 1500,
        "stock_now_unds": 5, "stock_now_valor": 50, "stock_a_unds": None,
    }
    out = _enrich_sku_comparador(row, dias_a=30, dias_b=30, cob=30)
    assert out["velocidad_a"] == 2.0                 # 60/30
    assert out["velocidad_b"] == 1.0                 # 30/30
    assert out["delta_velocidad_pct"] == -50.0
    # sugerencia = vel_a*cob - stock = 2*30 - 5 = 55
    assert out["sugerencia_compra"] == 55
    assert out["stock_ventana_a_unds"] is None       # sin histórico
    # vel cayó de 2 a 1 (-50%): falta stock pero la demanda bajó → REVISAR, no REPONER.
    assert out["veredicto"] == "REVISAR_DEMANDA"


def test_enrich_stock_a_historico_se_redondea():
    row = {
        "unds_a": 10, "soles_a": 100, "unds_b": 0, "soles_b": 0,
        "stock_now_unds": 0, "stock_now_valor": 0, "stock_a_unds": 42.7,
    }
    out = _enrich_sku_comparador(row, dias_a=10, dias_b=10, cob=30)
    assert out["stock_ventana_a_unds"] == 43


# ── agregación por jerarquía ─────────────────────────────────────────────────


def test_agrupar_suma_y_calcula_delta():
    skus = [
        {"departamento": "Hogar", "categoria": "Cocina", "subcategoria": "X",
         "venta_a_pen": 100.0, "venta_b_pen": 150.0, "unds_a": 10, "unds_b": 15,
         "stock_actual_unds": 5, "stock_actual_valor": 50.0, "sugerencia_compra": 3},
        {"departamento": "Hogar", "categoria": "Cocina", "subcategoria": "Y",
         "venta_a_pen": 100.0, "venta_b_pen": 50.0, "unds_a": 10, "unds_b": 5,
         "stock_actual_unds": 5, "stock_actual_valor": 50.0, "sugerencia_compra": 7},
    ]
    out = _agrupar_comparador(skus, ["departamento", "categoria"])
    assert len(out) == 1
    g = out[0]
    assert g["skus"] == 2
    assert g["venta_a_pen"] == 200.0
    assert g["venta_b_pen"] == 200.0
    assert g["delta_venta_pct"] == 0.0
    assert g["sugerencia_compra"] == 10
