"""Reglas de la verificación post-sync (tools/maintenance/audit_sync.py)."""

from __future__ import annotations

import os

os.environ.setdefault("DB_NAME", "test_db")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "5432")
os.environ.setdefault("DB_USER", "test_user")
os.environ.setdefault("DB_PASSWORD", "test_pass")

from tools.maintenance import audit_sync as a  # noqa: E402


def test_conteo_igual_ok_distinto_falla():
    assert a.compare_counts("g", "n", 10, 10).estado == a.OK
    c = a.compare_counts("g", "n", 10, 8)
    assert c.estado == a.FALLA and "-2" in c.detalle


def test_conteo_suave_solo_avisa():
    # el mes en curso puede tener ventas posteriores al sync
    assert a.compare_counts("g", "n", 10, 9, soft=True).estado == a.AVISO


def test_bsale_sin_respuesta_es_aviso_no_falla():
    assert a.compare_counts("g", "n", "sin respuesta", 5).estado == a.AVISO


def test_threshold():
    assert a.threshold("g", "n", 0, falla_si_mayor=0).estado == a.OK
    assert a.threshold("g", "n", 3, falla_si_mayor=0).estado == a.FALLA
    assert a.threshold("g", "n", 3).estado == a.AVISO
    assert a.threshold("g", "n", 24, aviso_si_mayor=50).estado == a.OK


def test_cost_matches_por_origen():
    avg = {"cost_source": "AVERAGE", "average_cost": 1.71}
    assert a.cost_matches(avg, {"averageCost": 1.71})
    assert not a.cost_matches(avg, {"averageCost": 3.06})
    rec = {"cost_source": "RECEPTION_AVG", "average_cost": 2.1}
    assert a.cost_matches(rec, {"averageCost": 0})          # BSale no da promedio → se usa recepciones
    assert not a.cost_matches(rec, {"averageCost": 2.5})    # BSale ya tiene promedio y no se tomó
    assert a.cost_matches(avg, {})                          # sin respuesta no se acusa


def test_overall_toma_el_peor():
    mk = lambda e: a.Check("g", "n", e)  # noqa: E731
    assert a.overall([mk(a.OK), mk(a.AVISO)]) == a.AVISO
    assert a.overall([mk(a.OK), mk(a.FALLA), mk(a.AVISO)]) == a.FALLA
    assert a.overall([]) == a.OK


def test_meses_terminan_en_el_actual():
    ms = a._months(3)
    assert len(ms) == 3 and ms[-1][3] is True and not any(m[3] for m in ms[:-1])
    assert all(m[1] < m[2] for m in ms) and ms[0][2] < ms[1][1]
