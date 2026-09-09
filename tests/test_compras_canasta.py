# -*- coding: utf-8 -*-
"""Tests de la lógica pura de la canasta (app/routers/compras.py).

El SQL de afinidad se valida contra la DB en integración; acá se fijan el
clasificador de lift, la resolución de ventana (incluye los 400) y los bullets
del resumen.
"""

from datetime import date, timedelta

import pytest
from fastapi import HTTPException

from app.routers.compras import (
    _interpretar_lift,
    _resolver_ventana,
    _resumen_canasta,
)


# ── _interpretar_lift ────────────────────────────────────────────────────────


def test_lift_muy_fuerte():
    assert _interpretar_lift(2.0) == "afinidad_muy_fuerte"
    assert _interpretar_lift(9.6) == "afinidad_muy_fuerte"


def test_lift_fuerte():
    assert _interpretar_lift(1.5) == "afinidad_fuerte"
    assert _interpretar_lift(1.99) == "afinidad_fuerte"


def test_lift_moderada():
    assert _interpretar_lift(1.2) == "afinidad_moderada"
    assert _interpretar_lift(1.49) == "afinidad_moderada"


# ── _resolver_ventana ────────────────────────────────────────────────────────


def test_ventana_por_dias_cierra_ayer():
    # Sin fechas → [hoy - dias, hoy): hoy queda excluido (día parcial).
    desde, hasta = _resolver_ventana(None, None, 30)
    hoy = date.today()
    assert hasta == hoy
    assert desde == hoy - timedelta(days=30)


def test_ventana_por_fechas_es_inclusiva():
    # hasta se devuelve +1 día (exclusivo) para que el rango sea inclusivo.
    desde, hasta = _resolver_ventana("2026-05-01", "2026-05-31", 90)
    assert desde == date(2026, 5, 1)
    assert hasta == date(2026, 6, 1)


def test_ventana_una_sola_fecha_es_400():
    with pytest.raises(HTTPException) as e:
        _resolver_ventana("2026-05-01", None, 90)
    assert e.value.status_code == 400
    with pytest.raises(HTTPException):
        _resolver_ventana(None, "2026-05-31", 90)


def test_ventana_desde_mayor_que_hasta_es_400():
    with pytest.raises(HTTPException) as e:
        _resolver_ventana("2026-06-01", "2026-05-01", 90)
    assert e.value.status_code == 400


def test_ventana_fecha_invalida_es_400():
    with pytest.raises(HTTPException) as e:
        _resolver_ventana("no-es-fecha", "2026-05-31", 90)
    assert e.value.status_code == 400


def test_ventana_mismo_dia_es_valida():
    # desde == hasta representa un solo día (hasta+1 exclusivo).
    desde, hasta = _resolver_ventana("2026-05-10", "2026-05-10", 90)
    assert desde == date(2026, 5, 10)
    assert hasta == date(2026, 5, 11)


# ── _resumen_canasta ─────────────────────────────────────────────────────────


def test_resumen_sin_pares_sugiere_ampliar():
    out = _resumen_canasta([], [], tickets=1000, multi=400, nivel="categoria")
    assert any("No se detectaron" in line for line in out)


def test_resumen_con_pares_menciona_el_top():
    pares = [
        {"a": "Piñatas", "b": "Cumpleaños", "juntos": 260, "lift": 9.6,
         "confianza_a_b": 55.0, "confianza_b_a": 11.0, "soporte_pct": 1.0,
         "interpretacion": "afinidad_muy_fuerte"},
    ]
    out = _resumen_canasta(pares, [], tickets=33000, multi=18000, nivel="categoria")
    joined = " ".join(out)
    assert "Piñatas" in joined and "Cumpleaños" in joined
    # Debe reportar el % multiproducto.
    assert "54%" in joined or "55%" in joined


def test_resumen_cuenta_afinidades_fuertes():
    pares = [
        {"a": "A", "b": "B", "juntos": 100, "lift": 2.0, "confianza_a_b": 30.0,
         "confianza_b_a": 20.0, "soporte_pct": 1.0, "interpretacion": "afinidad_muy_fuerte"},
        {"a": "C", "b": "D", "juntos": 80, "lift": 1.6, "confianza_a_b": 25.0,
         "confianza_b_a": 15.0, "soporte_pct": 0.8, "interpretacion": "afinidad_fuerte"},
        {"a": "E", "b": "F", "juntos": 50, "lift": 1.3, "confianza_a_b": 12.0,
         "confianza_b_a": 10.0, "soporte_pct": 0.5, "interpretacion": "afinidad_moderada"},
    ]
    out = _resumen_canasta(pares, [], tickets=10000, multi=5000, nivel="categoria")
    # 2 pares con lift >= 1.5.
    assert any("2 par" in line for line in out)
