"""Regression tests verifying exact mathematical parity with the 8-material Excel baseline."""

import math
import pytest

from app.baseline import BASELINE_CODES, BASELINE_EXCEL_RESULTS, EXCLUDED_CODES
from app.forecasting import EVAL_LIMIT_MONTHS, analyze_material


@pytest.mark.parametrize("code", BASELINE_CODES)
def test_baseline_exact_excel_parity(code: str) -> None:
    ref = BASELINE_EXCEL_RESULTS[code]
    series = [(m, float(v)) for m, v in ref["data"]]
    out = analyze_material(code, ref["name"], series)
    res = out["result"]

    assert res["classification"] == ref["classification"]
    assert res["bestMethod"] == ref["bestMethod"]
    assert res["alphaC"] == pytest.approx(ref["alphaC"], abs=1e-12)
    assert res["alphaS"] == pytest.approx(ref["alphaS"], abs=1e-12)
    assert res["betaT"] == pytest.approx(ref["betaT"], abs=1e-12)

    for method in ("Croston", "SBA", "TSB"):
        assert res["metrics"][method]["mase"] == pytest.approx(ref["mase"][method], rel=1e-11, abs=1e-12)

    assert res["nextForecast"] == pytest.approx(ref["nextForecast"], rel=1e-10)
    assert res["interval"] == pytest.approx(ref["interval"], rel=1e-10)
    assert res["nextMonth"] == ref["nextMonth"]
    assert res["nextQty"] == pytest.approx(ref["nextQty"], rel=1e-10)


def test_excluded_materials_absent_from_baseline() -> None:
    for code in EXCLUDED_CODES:
        assert code not in BASELINE_EXCEL_RESULTS


@pytest.mark.parametrize("code", BASELINE_CODES)
def test_evaluation_rule_no_leakage(code: str) -> None:
    ref = BASELINE_EXCEL_RESULTS[code]
    series = [(m, float(v)) for m, v in ref["data"]]
    out = analyze_material(code, ref["name"], series)
    ev = out["result"]["evaluation"]

    if len(series) > EVAL_LIMIT_MONTHS:
        assert ev["mode"] == "traintest"
        assert ev["trainSize"] == math.ceil(len(series) / 2)
        assert ev["testSize"] == len(series) - ev["trainSize"]
        assert ev["trainRange"][1] < ev["testRange"][0]
    else:
        assert ev["mode"] == "lastdemand"
        assert ev["trainRange"][1] < ev["targetMonth"]
        assert ev["actual"] > 0
