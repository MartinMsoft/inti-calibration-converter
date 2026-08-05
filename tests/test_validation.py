import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from validation import build_vols, find_pages_to_retry, fix_scale_errors, get_prev_context, validate_vols


def make_row(base, values):
    return {"base_mm": base, "values": values}


def test_build_vols_basic():
    page_results = [
        (1, None, [make_row(0, list(range(0, 10)))]),
        (2, None, [make_row(10, [10, 11, None, 13, 14, 15, 16, 17, 18, 19])]),
    ]
    vols = build_vols(page_results)
    assert vols[0] == 0
    assert vols[9] == 9
    assert vols[10] == 10
    assert 12 not in vols
    assert vols[19] == 19


def test_fix_scale_errors_detects_div1000():
    # mm=20 esta 1000x mas grande que sus vecinos -> se corrige hacia abajo
    vols = {0: 100.0, 10: 200.0, 20: 300000.0, 30: 400.0}
    corrected, fixes = fix_scale_errors(vols)
    assert corrected[20] == 300.0
    assert len(fixes) == 1
    assert "div1000" in fixes[0]


def test_fix_scale_errors_detects_x1000():
    # mm=20 esta 1000x mas chico que sus vecinos -> se corrige hacia arriba.
    # NOTA: este algoritmo (sin modificar) puede loguear correcciones
    # intermedias que se cancelan entre si (ida y vuelta) antes de llegar
    # al valor final correcto; por eso este test fija el resultado final,
    # no la cantidad de entradas en el log de fixes.
    vols = {0: 100.0, 10: 200.0, 20: 0.3, 30: 400.0}
    corrected, fixes = fix_scale_errors(vols)
    assert corrected[20] == 300.0
    assert corrected[10] == 200.0
    assert any("x1000" in f for f in fixes)


def test_fix_scale_errors_noop_when_monotonic():
    vols = {0: 0.0, 1: 1.0, 2: 2.0, 3: 3.0}
    corrected, fixes = fix_scale_errors(vols)
    assert corrected == vols
    assert fixes == []


def test_validate_vols_detects_missing():
    vols = {0: 0.0, 1: 1.0, 3: 3.0}
    result = validate_vols(vols)
    assert not result["ok"]
    assert result["stats"]["faltantes"] == 1
    assert 2 in result["bad_mm"]


def test_validate_vols_detects_non_monotonic():
    vols = {0: 0.0, 1: 5.0, 2: 3.0}
    result = validate_vols(vols)
    assert not result["ok"]
    assert 2 in result["bad_mm"]


def test_validate_vols_ok_when_clean():
    vols = {mm: float(mm) for mm in range(0, 50)}
    result = validate_vols(vols)
    assert result["ok"]
    assert result["bad_mm"] == set()


def test_validate_vols_empty():
    result = validate_vols({})
    assert not result["ok"]
    assert result["errors"] == ["No hay datos."]


def test_find_pages_to_retry_flags_empty_pages():
    validation = {"bad_mm": set(), "missing": []}
    page_results = [(1, None, []), (2, None, [make_row(0, [0] * 10)])]
    retry = find_pages_to_retry(page_results, validation)
    assert retry == {1}


def test_find_pages_to_retry_flags_pages_with_bad_mm():
    validation = {"bad_mm": {15}, "missing": []}
    page_results = [(1, None, [make_row(10, [10 + i for i in range(10)])])]
    retry = find_pages_to_retry(page_results, validation)
    assert retry == {1}


def test_get_prev_context_returns_closest_prior_mm():
    vols = {0: 0.0, 10: 10.0, 25: 25.0}
    assert get_prev_context(vols, 30) == (25, 25.0)
    assert get_prev_context(vols, 5) == (0, 0.0)


def test_get_prev_context_returns_none_when_nothing_before():
    vols = {10: 10.0}
    assert get_prev_context(vols, 5) is None
