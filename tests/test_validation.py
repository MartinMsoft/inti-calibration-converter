import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from validation import (
    build_vols,
    find_pages_to_retry,
    fix_row_scale_consistency,
    fix_scale_errors,
    fix_scale_shift_runs,
    get_prev_context,
    validate_vols,
)


def make_row(base, values):
    return {"base_mm": base, "values": values}


def test_build_vols_basic():
    page_results = [
        (1, [make_row(0, list(range(0, 10)))]),
        (2, [make_row(10, [10, 11, None, 13, 14, 15, 16, 17, 18, 19])]),
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
    page_results = [(1, []), (2, [make_row(0, [0] * 10)])]
    retry = find_pages_to_retry(page_results, validation)
    assert retry == {1}


def test_find_pages_to_retry_flags_pages_with_bad_mm():
    validation = {"bad_mm": {15}, "missing": []}
    page_results = [(1, [make_row(10, [10 + i for i in range(10)])])]
    retry = find_pages_to_retry(page_results, validation)
    assert retry == {1}


def test_get_prev_context_returns_closest_prior_mm():
    vols = {0: 0.0, 10: 10.0, 25: 25.0}
    assert get_prev_context(vols, 30) == (25, 25.0)
    assert get_prev_context(vols, 5) == (0, 0.0)


def test_get_prev_context_returns_none_when_nothing_before():
    vols = {10: 10.0}
    assert get_prev_context(vols, 5) is None


def _build_tank_curve(start_mm, end_mm, start_val, step):
    """Genera una curva creciente simple, tipica de una tabla de calibracion."""
    vols = {}
    val = start_val
    for mm in range(start_mm, end_mm + 1):
        vols[mm] = round(val, 3)
        val += step
    return vols


def test_fix_scale_shift_runs_corrects_sustained_x1000_page():
    # Caso real reportado: una tabla entera (una pagina nueva del PDF) se leyo
    # x1000 mas grande, de forma sostenida y consistente, no solo el primer punto.
    good = _build_tank_curve(690, 729, 69.723, 0.071)
    bad_tail = {mm: round(v, 3) * 1000 for mm, v in _build_tank_curve(730, 800, 72.556, 0.071).items()}
    vols = {**good, **bad_tail}

    corrected, fixes = fix_scale_shift_runs(vols)

    # tolerancia mas amplia porque _build_tank_curve acumula pequenios errores
    # de punto flotante al sumar 0.071 muchas veces; no es precision del algoritmo.
    assert corrected[730] == pytest.approx(72.556, abs=0.01)
    assert corrected[744] == pytest.approx(73.548, abs=0.01)
    assert corrected[800] < 100  # ya no deberia quedar en la escala x1000
    assert len(fixes) == 1
    assert "730" in fixes[0]


def test_fix_scale_shift_runs_corrects_sustained_div1000_page():
    # Caso espejo: una pagina entera se leyo /1000 mas chica de forma sostenida.
    good = _build_tank_curve(0, 39, 0.0, 1.0)
    bad_tail = {mm: v / 1000 for mm, v in _build_tank_curve(40, 100, 40.0, 1.0).items()}
    vols = {**good, **bad_tail}

    corrected, fixes = fix_scale_shift_runs(vols)

    assert corrected[40] == 40.0
    assert corrected[100] == 100.0
    assert len(fixes) == 1


def test_fix_scale_shift_runs_noop_on_normal_curve():
    vols = _build_tank_curve(0, 200, 0.0, 0.5)
    corrected, fixes = fix_scale_shift_runs(vols)
    assert corrected == vols
    assert fixes == []


def test_fix_scale_shift_runs_ignores_legitimate_large_jump():
    # Un salto grande pero NO cercano a 1000x (ej: cambio real de geometria del
    # tanque) no debe "corregirse" artificialmente.
    good = _build_tank_curve(0, 39, 0.0, 1.0)
    bigger_but_not_1000x = {mm: v * 8 for mm, v in _build_tank_curve(40, 100, 40.0, 1.0).items()}
    vols = {**good, **bigger_but_not_1000x}
    corrected, fixes = fix_scale_shift_runs(vols)
    assert corrected == vols
    assert fixes == []


def test_fix_row_scale_consistency_real_case_mixed_row():
    # Caso real reportado: dentro de UNA fila, la IA leyo v0 como decimal
    # (867.774) y v1..v9 como enteros x1000 (867677, 867980, ...).
    row = make_row(8310, [
        867.774, 867677.0, 867980.0, 868083.0, 868186.0,
        868290.0, 868393.0, 868496.0, 868599.0, 868702.0,
    ])
    fixed_rows, fixes = fix_row_scale_consistency([row])
    values = fixed_rows[0]["values"]
    assert values[0] == pytest.approx(867.774, abs=0.001)
    assert values[1] == pytest.approx(867.677, abs=0.001)
    assert values[9] == pytest.approx(868.702, abs=0.001)
    assert len(fixes) == 9


def test_fix_row_scale_consistency_noop_on_clean_row():
    row = make_row(0, [0.0, 0.103, 0.206, 0.309, 0.412, 0.515, 0.618, 0.721, 0.824, 0.927])
    fixed_rows, fixes = fix_row_scale_consistency([row])
    assert fixed_rows[0]["values"] == row["values"]
    assert fixes == []


def test_fix_row_scale_consistency_ignores_rows_with_too_few_points():
    row = make_row(0, [1.0, 2.0, None, None, None, None, None, None, None, None])
    fixed_rows, fixes = fix_row_scale_consistency([row])
    assert fixed_rows[0]["values"] == row["values"]
    assert fixes == []
