"""Statistical significance testing for experiment comparison.

Provides Welch's t-test, Wilcoxon signed-rank test (with sign-test
fallback), and a convenience function to compare multiple groups against
a baseline — suitable for ablation studies in academic papers.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import comb, sqrt
from typing import Dict, Iterable, List, Sequence, Tuple

import numpy as np


@dataclass
class TestResult:
    test_name: str
    statistic: float
    p_value: float
    n: int


def mean_std(values: Sequence[float]) -> Tuple[float, float]:
    arr = np.asarray(list(values), dtype=float)
    if arr.size == 0:
        return float("nan"), float("nan")
    return float(np.mean(arr)), float(np.std(arr, ddof=1) if arr.size > 1 else 0.0)


def _normal_cdf(x: float) -> float:
    return float(0.5 * (1.0 + np.math.erf(x / sqrt(2.0))))


def welch_t_test(x: Sequence[float], y: Sequence[float]) -> TestResult:
    """Two-sample Welch's t-test (unequal variance)."""
    x_arr = np.asarray(list(x), dtype=float)
    y_arr = np.asarray(list(y), dtype=float)
    x_arr = x_arr[np.isfinite(x_arr)]
    y_arr = y_arr[np.isfinite(y_arr)]

    if x_arr.size < 2 or y_arr.size < 2:
        return TestResult("welch_ttest", float("nan"), float("nan"), int(min(x_arr.size, y_arr.size)))

    try:
        from scipy import stats
        stat, p = stats.ttest_ind(x_arr, y_arr, equal_var=False)
        return TestResult("welch_ttest", float(stat), float(p), int(min(x_arr.size, y_arr.size)))
    except Exception:
        mx, my = float(np.mean(x_arr)), float(np.mean(y_arr))
        vx, vy = float(np.var(x_arr, ddof=1)), float(np.var(y_arr, ddof=1))
        denom = sqrt(vx / x_arr.size + vy / y_arr.size)
        if denom <= 0:
            return TestResult("welch_ttest_fallback", 0.0, 1.0, int(min(x_arr.size, y_arr.size)))
        t_stat = (mx - my) / denom
        p_val = float(2.0 * (1.0 - _normal_cdf(abs(t_stat))))
        return TestResult("welch_ttest_fallback", float(t_stat), p_val, int(min(x_arr.size, y_arr.size)))


def _sign_test_p_value(x: Sequence[float], y: Sequence[float]) -> float:
    diffs = np.asarray(list(x), dtype=float) - np.asarray(list(y), dtype=float)
    diffs = diffs[np.isfinite(diffs)]
    diffs = diffs[diffs != 0]
    n = int(diffs.size)
    if n == 0:
        return 1.0
    k = int(np.sum(diffs > 0))
    tail = sum(comb(n, i) for i in range(0, min(k, n - k) + 1))
    p = 2.0 * tail / (2**n)
    return float(min(1.0, p))


def wilcoxon_or_sign_test(x: Sequence[float], y: Sequence[float]) -> TestResult:
    """Wilcoxon signed-rank test, falling back to exact sign test."""
    x_arr = np.asarray(list(x), dtype=float)
    y_arr = np.asarray(list(y), dtype=float)
    n = int(min(x_arr.size, y_arr.size))
    if n == 0:
        return TestResult("wilcoxon", float("nan"), float("nan"), 0)
    x_arr, y_arr = x_arr[:n], y_arr[:n]

    try:
        from scipy import stats
        stat, p = stats.wilcoxon(x_arr, y_arr, zero_method="wilcox", correction=False, alternative="two-sided")
        return TestResult("wilcoxon", float(stat), float(p), n)
    except Exception:
        p_val = _sign_test_p_value(x_arr, y_arr)
        return TestResult("sign_test_fallback", float("nan"), float(p_val), n)


def compare_to_baseline(
    rows: Iterable[Dict[str, object]],
    metric_key: str,
    baseline_name: str,
    group_key: str = "ablation",
    alpha: float = 0.05,
) -> List[Dict[str, object]]:
    """Compare every group against *baseline_name* on *metric_key*.

    Returns one row per group with mean, std, delta, test name, statistic,
    p-value, and significance flag.
    """
    grouped: Dict[str, List[float]] = {}
    for row in rows:
        g = str(row.get(group_key, ""))
        try:
            v = float(row.get(metric_key, float("nan")))  # type: ignore[arg-type]
        except Exception:
            v = float("nan")
        if not np.isfinite(v):
            continue
        grouped.setdefault(g, []).append(v)

    baseline_vals = grouped.get(baseline_name, [])
    output: List[Dict[str, object]] = []

    for group_name, values in sorted(grouped.items()):
        mu, sd = mean_std(values)
        entry: Dict[str, object] = {
            "group": group_name,
            "metric": metric_key,
            "n": len(values),
            "mean": mu,
            "std": sd,
            "baseline": baseline_name,
            "delta_vs_baseline": float("nan"),
            "test": "",
            "statistic": float("nan"),
            "p_value": float("nan"),
            "significant": False,
        }
        if group_name != baseline_name and baseline_vals and values:
            baseline_mu, _ = mean_std(baseline_vals)
            entry["delta_vs_baseline"] = float(mu - baseline_mu)
            test_res = welch_t_test(values, baseline_vals)
            if not np.isfinite(test_res.p_value):
                test_res = wilcoxon_or_sign_test(
                    values[: len(baseline_vals)], baseline_vals[: len(values)]
                )
            entry["test"] = test_res.test_name
            entry["statistic"] = test_res.statistic
            entry["p_value"] = test_res.p_value
            entry["significant"] = bool(np.isfinite(test_res.p_value) and test_res.p_value < alpha)
        output.append(entry)

    return output
