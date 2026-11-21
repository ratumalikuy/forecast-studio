"""Verified Intermittent Demand Forecasting Engine (Excel Reference Parity).

Implements the exact mathematical methodology audited against the 8-material
Excel baseline:
- ADI & CV^2 demand pattern classification (sample variance)
- Croston, Syntetos-Boylan Approximation (SBA), Teunter-Syntetos-Babai (TSB)
  * Croston & SBA update Z_t and P_t ONLY on non-zero demand periods, with
    first-demand initialization (Z = v, P = pq + 1), matching Excel exactly.
  * TSB updates demand probability every period and multiplies by cumulative
    average non-zero demand size, matching Excel exactly.
- Grid search over [0.0001, 1.0000] with step 0.0001 (10,000 candidates),
  minimizing one-step-ahead in-sample MSE (x[t] vs F[t-1]); ties select the
  smallest parameter.
- One-step-ahead error metrics: MAD, MSE, MAPE, SMAPE, WAPE, MASE, RMSSE.
- Best method selection: lowest in-sample MASE (matching Excel baseline),
  while also recording bestMse and bestMape.
- Forward projection over horizon = N historical months, repeating every
  round_half_up(interval) months from the last actual data point.
- Evaluation rule (strictly zero data leakage):
  * N > 60 months (> 5 years): chronological 50:50 Train/Test split.
  * N <= 60 months (<= 5 years): train from start up to month before last
    non-zero actual demand, evaluate against last actual demand.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

EVAL_LIMIT_MONTHS = 60
GRID_STEP = 0.0001
GRID_COUNT = 10000
_ALPHAS = np.arange(1, GRID_COUNT + 1, dtype=np.float64) / float(GRID_COUNT)


def _clean_float(val: Optional[float]) -> Optional[float]:
    if val is None:
        return None
    if not math.isfinite(val):
        return None
    return float(val)


def _round_half_up(val: float) -> int:
    """Match JavaScript Math.round / Excel ROUND for positive numbers."""
    return int(math.floor(val + 0.5))


def month_to_index(month_str: str) -> int:
    year_s, mon_s = month_str.split("-")
    return int(year_s) * 12 + (int(mon_s) - 1)


def index_to_month(idx: int) -> str:
    year = idx // 12
    mon = (idx % 12) + 1
    return f"{year:04d}-{mon:02d}"


def count_nonzero(series: Sequence[Tuple[str, float]]) -> int:
    return sum(1 for _, x in series if x > 0)


def compute_adi_cv2(series: Sequence[Tuple[str, float]]) -> Dict[str, Any]:
    """Compute ADI, CV^2 (sample variance), and Syntetos-Boylan classification."""
    n_total = len(series)
    nz = [float(x) for _, x in series if x > 0]
    n_nz = len(nz)

    if n_nz == 0:
        return {
            "adi": None,
            "cv2": None,
            "classification": "Tidak ada demand",
            "N": n_total,
            "n": 0,
        }

    adi = n_total / n_nz
    if n_nz == 1:
        classification = "Intermittent" if adi > 1.32 else "Smooth"
        return {
            "adi": float(adi),
            "cv2": 0.0,
            "classification": classification,
            "N": n_total,
            "n": 1,
        }

    mean = sum(nz) / n_nz
    variance = sum((x - mean) ** 2 for x in nz) / (n_nz - 1)
    std = math.sqrt(variance)
    cv2 = (std / mean) ** 2 if mean > 0 else 0.0

    if adi <= 1.32 and cv2 <= 0.49:
        classification = "Smooth"
    elif adi > 1.32 and cv2 <= 0.49:
        classification = "Intermittent"
    elif adi <= 1.32 and cv2 > 0.49:
        classification = "Erratic"
    else:
        classification = "Lumpy"

    return {
        "adi": float(adi),
        "cv2": float(cv2),
        "classification": classification,
        "N": n_total,
        "n": n_nz,
    }


def run_excel_states(
    x: Sequence[float],
    alpha_c: float,
    alpha_s: float,
    beta_t: float,
) -> Dict[str, Any]:
    """Run exact Excel-verified Croston, SBA, and TSB recursive state equations."""
    qt = 0
    zc = 0.0
    pc = 0.0
    zs = 0.0
    ps = 0.0
    pr = 0.0
    cs = 0.0
    cc = 0

    fc: List[float] = []
    fs: List[float] = []
    ft: List[float] = []

    for v_raw in x:
        v = float(v_raw)
        ind = 1 if v > 0 else 0
        pq = qt
        qt = 1 if v > 0 else qt + 1

        if v > 0:
            zc = v if zc == 0.0 else alpha_c * v + (1.0 - alpha_c) * zc
            pc = float(pq + 1) if pc == 0.0 else alpha_c * pq + (1.0 - alpha_c) * pc
            zs = v if zs == 0.0 else alpha_s * v + (1.0 - alpha_s) * zs
            ps = float(pq + 1) if ps == 0.0 else alpha_s * pq + (1.0 - alpha_s) * ps

        fc.append(0.0 if pc == 0.0 else zc / pc)
        fs.append(0.0 if ps == 0.0 else (1.0 - alpha_s / 2.0) * (zs / ps))

        pr = (1.0 - beta_t) * pr + beta_t * ind
        cs += v
        cc += ind
        rata = 0.0 if cc == 0 else cs / cc
        ft.append(rata * pr)

    final_rata = 0.0 if cc == 0 else cs / cc
    return {
        "Fc": fc,
        "Fs": fs,
        "Ft": ft,
        "final": {
            "ZtC": zc,
            "PtC": pc,
            "Fc": fc[-1] if fc else 0.0,
            "ZtS": zs,
            "PtS": ps,
            "Fs": fs[-1] if fs else 0.0,
            "ProbT": pr,
            "rata": final_rata,
            "Ft": ft[-1] if ft else 0.0,
        },
    }


def grid_search_excel(x: Sequence[float], method: str) -> Dict[str, Any]:
    """Vectorized 0.0001-step grid search on [0.0001, 1.0000] minimizing one-step-ahead MSE.

    Produces IEEE-754 double-precision parity with the verified Excel baseline.
    """
    n_obs = len(x)
    if n_obs < 2:
        return {
            "optimal_param": 0.0001,
            "minimum_mse": None,
            "n_candidates": GRID_COUNT,
            "n_candidates_at_minimum": 0,
            "step": GRID_STEP,
        }

    alphas = _ALPHAS
    one_minus = 1.0 - alphas
    sq_sum = np.zeros(GRID_COUNT, dtype=np.float64)
    prev_f = np.zeros(GRID_COUNT, dtype=np.float64)

    if method == "TSB":
        pr = np.zeros(GRID_COUNT, dtype=np.float64)
        cs = 0.0
        cc = 0
        for j, v_raw in enumerate(x):
            v = float(v_raw)
            if j > 0:
                err = v - prev_f
                sq_sum += err * err
            ind = 1.0 if v > 0 else 0.0
            pr = one_minus * pr + alphas * ind
            cs += v
            cc += 1 if v > 0 else 0
            rata = 0.0 if cc == 0 else cs / cc
            prev_f = rata * pr
    elif method in ("Croston", "SBA"):
        z_arr = np.zeros(GRID_COUNT, dtype=np.float64)
        p_arr = np.zeros(GRID_COUNT, dtype=np.float64)
        sba_factor = 1.0 - alphas / 2.0
        qt = 0
        first_seen = False
        for j, v_raw in enumerate(x):
            v = float(v_raw)
            if j > 0:
                err = v - prev_f
                sq_sum += err * err
            pq = qt
            qt = 1 if v > 0 else qt + 1
            if v > 0:
                if not first_seen:
                    z_arr.fill(v)
                    p_arr.fill(float(pq + 1))
                    first_seen = True
                else:
                    z_arr = alphas * v + one_minus * z_arr
                    p_arr = alphas * float(pq) + one_minus * p_arr
                if method == "SBA":
                    prev_f = sba_factor * (z_arr / p_arr)
                else:
                    prev_f = z_arr / p_arr
    else:
        raise ValueError(f"Metode tidak dikenali: {method}")

    mses = sq_sum / float(n_obs - 1)
    best_idx = int(np.argmin(mses))
    best_param = float(alphas[best_idx])
    best_mse = float(mses[best_idx])
    n_at_min = int(np.sum(np.isclose(mses, best_mse, rtol=1e-12, atol=1e-15)))

    return {
        "optimal_param": round(best_param, 4),
        "minimum_mse": _clean_float(best_mse),
        "n_candidates": GRID_COUNT,
        "n_candidates_at_minimum": n_at_min,
        "step": GRID_STEP,
    }


def compute_metrics_window(
    x: Sequence[float],
    forecasts: Sequence[float],
    start_idx: int,
    naive_series: Sequence[float],
) -> Dict[str, Optional[float]]:
    """Compute one-step-ahead error metrics for x[i] vs forecasts[i-1], i >= start_idx.

    Naive denominator for MASE and RMSSE is computed strictly from `naive_series`
    (full series for in-sample; train-only series for Train/Test split to prevent leakage).
    """
    n = 0
    sum_abs = 0.0
    sum_sq = 0.0
    sum_mape = 0.0
    n_mape = 0
    sum_smape = 0.0
    n_smape = 0
    sum_act = 0.0

    for i in range(max(1, start_idx), len(x)):
        actual = float(x[i])
        pred = float(forecasts[i - 1])
        err = actual - pred
        n += 1
        sum_abs += abs(err)
        sum_sq += err * err
        sum_act += abs(actual)
        if actual != 0.0:
            sum_mape += abs(err / actual)
            n_mape += 1
        denom = (abs(actual) + abs(pred)) / 2.0
        if denom != 0.0:
            sum_smape += abs(err) / denom
            n_smape += 1

    naive_abs = 0.0
    naive_sq = 0.0
    for i in range(1, len(naive_series)):
        diff = float(naive_series[i]) - float(naive_series[i - 1])
        naive_abs += abs(diff)
        naive_sq += diff * diff

    naive_n = len(naive_series) - 1
    naive_mae = (naive_abs / naive_n) if naive_n > 0 else 0.0
    naive_mse = (naive_sq / naive_n) if naive_n > 0 else 0.0

    mad = (sum_abs / n) if n > 0 else None
    mse = (sum_sq / n) if n > 0 else None
    mape = ((sum_mape / n_mape) * 100.0) if n_mape > 0 else None
    smape = ((sum_smape / n_smape) * 100.0) if n_smape > 0 else None
    wape = ((sum_abs / sum_act) * 100.0) if sum_act != 0.0 else None
    mase = (mad / naive_mae) if (mad is not None and naive_mae > 0.0) else None
    rmsse = math.sqrt(mse / naive_mse) if (mse is not None and naive_mse > 0.0) else None

    return {
        "n": n,
        "mad": _clean_float(mad),
        "mse": _clean_float(mse),
        "mape": _clean_float(mape),
        "smape": _clean_float(smape),
        "wape": _clean_float(wape),
        "mase": _clean_float(mase),
        "rmsse": _clean_float(rmsse),
    }


def generate_projection(
    series: Sequence[Tuple[str, float]],
    final_state: Dict[str, float],
    best_method: str,
    horizon_months: int,
) -> Dict[str, Any]:
    """Generate forward demand event predictions matching Excel projection logic."""
    if best_method == "Croston":
        interval = final_state["PtC"]
        qty = final_state["ZtC"]
        next_forecast = final_state["Fc"]
    elif best_method == "SBA":
        interval = final_state["PtS"]
        qty = final_state["ZtS"]
        next_forecast = final_state["Fs"]
    else:
        prob = final_state["ProbT"]
        interval = (1.0 / prob) if prob > 0.0 else 0.0
        qty = final_state["rata"]
        next_forecast = final_state["Ft"]

    interval_rounded = max(1, _round_half_up(interval)) if interval and math.isfinite(interval) and interval > 0 else 0
    predictions: List[List[Any]] = []
    next_month: Optional[str] = None

    if 0 < interval_rounded <= 12000 and series:
        last_idx = month_to_index(series[-1][0])
        idx = last_idx + interval_rounded
        end_idx = last_idx + horizon_months
        next_month = index_to_month(idx)
        while idx <= end_idx:
            predictions.append([index_to_month(idx), float(qty)])
            idx += interval_rounded

    return {
        "nextForecast": _clean_float(next_forecast),
        "interval": _clean_float(interval),
        "intervalRounded": interval_rounded,
        "nextMonth": next_month,
        "nextQty": _clean_float(qty),
        "predictions": predictions,
    }


def _pick_best_by(metrics: Dict[str, Dict[str, Optional[float]]], key: str) -> str:
    best_name: Optional[str] = None
    best_val = float("inf")
    for name in ("Croston", "SBA", "TSB"):
        v = metrics[name].get(key)
        if v is not None and math.isfinite(v) and v < best_val:
            best_val = v
            best_name = name
    if best_name is not None:
        return best_name
    if key != "mad":
        return _pick_best_by(metrics, "mad")
    return "Croston"


def evaluate_train_test_50_50(series: Sequence[Tuple[str, float]]) -> Dict[str, Any]:
    """Chronological 50:50 Train/Test split for series > 60 months (> 5 years).

    Parameters are optimized ONLY on Train; zero data leakage.
    """
    x = [float(v) for _, v in series]
    n_total = len(x)
    train_size = math.ceil(n_total / 2)
    test_size = n_total - train_size
    warnings: List[str] = []

    if test_size < 1 or train_size < 3:
        return {
            "mode": "traintest",
            "rule": ">60_months_50_50",
            "insufficient": True,
            "N": n_total,
            "warnings": ["Data historis terlalu pendek untuk evaluasi Train/Test 50:50 (minimal 3 bulan Train)."],
        }

    train_x = x[:train_size]
    if sum(1 for v in train_x if v > 0) == 0:
        warnings.append("Tidak ada kejadian demand (>0) pada periode Train; parameter hasil optimasi menjadi batas minimum.")

    g_c = grid_search_excel(train_x, "Croston")
    g_s = grid_search_excel(train_x, "SBA")
    g_t = grid_search_excel(train_x, "TSB")

    a_c = g_c["optimal_param"]
    a_s = g_s["optimal_param"]
    b_t = g_t["optimal_param"]

    states = run_excel_states(x, a_c, a_s, b_t)
    metrics = {
        "Croston": compute_metrics_window(x, states["Fc"], train_size, train_x),
        "SBA": compute_metrics_window(x, states["Fs"], train_size, train_x),
        "TSB": compute_metrics_window(x, states["Ft"], train_size, train_x),
    }

    best_test: Optional[str] = None
    best_mase = float("inf")
    for k in ("Croston", "SBA", "TSB"):
        m_val = metrics[k]["mase"]
        if m_val is not None and m_val < best_mase:
            best_mase = m_val
            best_test = k

    rows = [
        {
            "m": series[i][0],
            "actual": float(x[i]),
            "Croston": float(states["Fc"][i - 1]),
            "SBA": float(states["Fs"][i - 1]),
            "TSB": float(states["Ft"][i - 1]),
        }
        for i in range(train_size, n_total)
    ]

    nz_train = count_nonzero(series[:train_size])
    nz_test = count_nonzero(series[train_size:])
    if nz_test < 3:
        warnings.append(
            f"Periode Test hanya memiliki {nz_test} kejadian demand (>0); perlakukan metrik uji sebagai indikasi awal."
        )

    return {
        "mode": "traintest",
        "rule": ">60_months_50_50",
        "insufficient": False,
        "N": n_total,
        "trainSize": train_size,
        "testSize": test_size,
        "trainRange": [series[0][0], series[train_size - 1][0]],
        "testRange": [series[train_size][0], series[-1][0]],
        "nzTrain": nz_train,
        "nzTest": nz_test,
        "zerosTrain": sum(1 for v in train_x if v == 0.0),
        "params": {"Croston": a_c, "SBA": a_s, "TSB": b_t},
        "gridSearchTrain": {"croston": g_c, "sba": g_s, "tsb": g_t},
        "metrics": metrics,
        "rows": rows,
        "bestTest": best_test,
        "warnings": warnings,
    }


def evaluate_last_actual_demand(series: Sequence[Tuple[str, float]]) -> Dict[str, Any]:
    """Last actual demand evaluation for series <= 60 months (<= 5 years).

    Trains on all months from start (e.g. 2020) strictly before the last non-zero
    demand period L, keeping all zero-demand months, and compares forecast vs actual[L].
    """
    x = [float(v) for _, v in series]
    n_total = len(x)
    warnings: List[str] = []

    if series and not series[0][0].startswith("2020-"):
        warnings.append(
            f"Data dimulai pada {series[0][0]} (bukan tahun 2020). Aturan evaluasi <=5 tahun tetap menggunakan awal data tersedia hingga bulan sebelum demand aktual terakhir tanpa mengubah aturan."
        )

    last_nz_idx = -1
    for i in range(n_total - 1, -1, -1):
        if x[i] > 0.0:
            last_nz_idx = i
            break

    if last_nz_idx < 3:
        return {
            "mode": "lastdemand",
            "rule": "<=60_months_last_actual_demand",
            "insufficient": True,
            "N": n_total,
            "warnings": warnings + [
                "Periode pelatihan sebelum demand aktual terakhir kurang dari 3 bulan sehingga evaluasi tidak dapat dijalankan."
            ],
        }

    train_x = x[:last_nz_idx]
    nz_train = sum(1 for v in train_x if v > 0.0)
    if nz_train == 0:
        warnings.append(
            "Tidak ada kejadian demand (>0) pada periode pelatihan sebelum demand aktual terakhir."
        )

    g_c = grid_search_excel(train_x, "Croston")
    g_s = grid_search_excel(train_x, "SBA")
    g_t = grid_search_excel(train_x, "TSB")

    a_c = g_c["optimal_param"]
    a_s = g_s["optimal_param"]
    b_t = g_t["optimal_param"]

    states = run_excel_states(train_x, a_c, a_s, b_t)
    actual = x[last_nz_idx]

    results: Dict[str, Dict[str, Optional[float]]] = {}
    for name, f_arr in (("Croston", states["Fc"]), ("SBA", states["Fs"]), ("TSB", states["Ft"])):
        f_val = float(f_arr[last_nz_idx - 1])
        diff = actual - f_val
        abs_err = abs(diff)
        pct = (abs_err / actual * 100.0) if actual > 0.0 else None
        results[name] = {
            "forecast": _clean_float(f_val),
            "diff": _clean_float(diff),
            "absErr": _clean_float(abs_err),
            "pct": _clean_float(pct),
        }

    best_abs = min(("Croston", "SBA", "TSB"), key=lambda k: results[k]["absErr"] if results[k]["absErr"] is not None else float("inf"))

    return {
        "mode": "lastdemand",
        "rule": "<=60_months_last_actual_demand",
        "insufficient": False,
        "N": n_total,
        "trainSize": last_nz_idx,
        "trainRange": [series[0][0], series[last_nz_idx - 1][0]],
        "targetMonth": series[last_nz_idx][0],
        "actual": float(actual),
        "nzTrain": nz_train,
        "zerosTrain": sum(1 for v in train_x if v == 0.0),
        "trailingZeros": n_total - 1 - last_nz_idx,
        "params": {"Croston": a_c, "SBA": a_s, "TSB": b_t},
        "gridSearchTrain": {"croston": g_c, "sba": g_s, "tsb": g_t},
        "results": results,
        "bestAbs": best_abs,
        "warnings": warnings,
    }


def analyze_material(stock_code: str, name: str, series: Sequence[Tuple[str, float]]) -> Dict[str, Any]:
    """Run the complete verified forecasting pipeline for a single material."""
    ordered = sorted(((str(m), float(v)) for m, v in series), key=lambda item: item[0])
    x = [v for _, v in ordered]
    n_months = len(ordered)

    cls = compute_adi_cv2(ordered)

    g_c = grid_search_excel(x, "Croston")
    g_s = grid_search_excel(x, "SBA")
    g_t = grid_search_excel(x, "TSB")

    alpha_c = g_c["optimal_param"]
    alpha_s = g_s["optimal_param"]
    beta_t = g_t["optimal_param"]

    states = run_excel_states(x, alpha_c, alpha_s, beta_t)
    metrics = {
        "Croston": compute_metrics_window(x, states["Fc"], 1, x),
        "SBA": compute_metrics_window(x, states["Fs"], 1, x),
        "TSB": compute_metrics_window(x, states["Ft"], 1, x),
    }

    best_method = _pick_best_by(metrics, "mase")
    best_mse = _pick_best_by(metrics, "mse")
    best_mape = _pick_best_by(metrics, "mape")

    horizon_months = n_months
    proj = generate_projection(ordered, states["final"], best_method, horizon_months)

    if n_months > EVAL_LIMIT_MONTHS:
        evaluation = evaluate_train_test_50_50(ordered)
    else:
        evaluation = evaluate_last_actual_demand(ordered)

    range_str = f"{ordered[0][0]} s.d. {ordered[-1][0]} (aktual)" if ordered else "-"

    result = {
        "stockCode": stock_code,
        "name": name,
        "classification": cls["classification"],
        "adi": _clean_float(cls["adi"]),
        "cv2": _clean_float(cls["cv2"]),
        "range": range_str,
        "alphaC": alpha_c,
        "alphaS": alpha_s,
        "betaT": beta_t,
        "bestMethod": best_method,
        "bestMse": best_mse,
        "bestMape": best_mape,
        "selectionBasis": "mase_in_sample",
        "source": "backend_python",
        "formulaVersion": "excel-verified-v1",
        "gridStep": GRID_STEP,
        "horizonMonths": horizon_months,
        "nextForecast": proj["nextForecast"],
        "interval": proj["interval"],
        "nextMonth": proj["nextMonth"],
        "nextQty": proj["nextQty"],
        "metrics": metrics,
        "data": [[m, v] for m, v in ordered],
        "predictions": proj["predictions"],
        "evaluation": evaluation,
        "custom": True,
    }

    return {
        "stock_code": stock_code,
        "result": result,
        "grid_search": {"croston": g_c, "sba": g_s, "tsb": g_t},
        "grid_search_train": evaluation.get("gridSearchTrain"),
    }
