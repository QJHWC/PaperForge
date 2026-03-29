"""Metric-based gate logic for experiment pipelines.

Automatically decides whether training results meet a quality threshold
and should proceed to the next phase, or if corrective action is needed.
Outputs a structured decision as JSON and Markdown for auditability.

Usage:
    from engine.gate import GateConfig, evaluate_gate

    cfg = GateConfig(
        metric_key="mAP50_95",
        target_mean_min=0.885,
        target_any_seed_min=0.90,
        seed_std_max=0.010,
    )
    decision = evaluate_gate(results, cfg)
    decision.save(output_dir)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np


@dataclass
class GateConfig:
    """Configurable thresholds for a quality gate."""
    metric_key: str = "primary_metric"
    target_mean_min: float = 0.0
    target_any_seed_min: float = 0.0
    seed_std_max: float = float("inf")
    tail_gain_last_n: int = 20
    tail_gain_max: float = float("inf")


@dataclass
class GateDecision:
    """Outcome of a gate evaluation."""
    action: str  # CONTINUE, STOP_BELOW_TARGET, STOP_HIGH_VARIANCE, STOP_STILL_IMPROVING, STOP_NO_DATA
    passed: bool
    metric_key: str
    observed_mean: float
    observed_max: float
    observed_std: float
    n_seeds: int
    thresholds: Dict[str, Any] = field(default_factory=dict)
    details: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    def to_markdown(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [
            f"# Gate Decision: **{status}**",
            "",
            f"- Action: `{self.action}`",
            f"- Metric: `{self.metric_key}`",
            f"- Seeds: {self.n_seeds}",
            f"- Mean: {self.observed_mean:.6f}",
            f"- Max: {self.observed_max:.6f}",
            f"- Std: {self.observed_std:.6f}",
            "",
            "## Thresholds",
            "",
        ]
        for k, v in self.thresholds.items():
            lines.append(f"- `{k}`: {v}")
        if self.details:
            lines += ["", "## Details", "", self.details]
        return "\n".join(lines) + "\n"

    def save(self, output_dir: str | Path) -> None:
        d = Path(output_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / "gate_decision.json").write_text(self.to_json(), encoding="utf-8")
        (d / "gate_decision.md").write_text(self.to_markdown(), encoding="utf-8")


def evaluate_gate(
    seed_values: List[float],
    config: GateConfig,
    epoch_curve: Optional[List[float]] = None,
) -> GateDecision:
    """Evaluate whether *seed_values* pass the quality gate.

    Parameters
    ----------
    seed_values : list of float
        One metric value per seed (e.g. mAP across 5 seeds).
    config : GateConfig
        Threshold configuration.
    epoch_curve : list of float, optional
        Per-epoch metric values for the best seed, used to detect whether
        training is still improving (tail-gain check).

    Returns
    -------
    GateDecision
    """
    thresholds = {
        "target_mean_min": config.target_mean_min,
        "target_any_seed_min": config.target_any_seed_min,
        "seed_std_max": config.seed_std_max,
    }

    if not seed_values:
        return GateDecision(
            action="STOP_NO_DATA",
            passed=False,
            metric_key=config.metric_key,
            observed_mean=float("nan"),
            observed_max=float("nan"),
            observed_std=float("nan"),
            n_seeds=0,
            thresholds=thresholds,
            details="No seed values provided.",
        )

    arr = np.array(seed_values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return GateDecision(
            action="STOP_NO_DATA",
            passed=False,
            metric_key=config.metric_key,
            observed_mean=float("nan"),
            observed_max=float("nan"),
            observed_std=float("nan"),
            n_seeds=0,
            thresholds=thresholds,
            details="All seed values are NaN.",
        )

    mean_val = float(np.mean(arr))
    max_val = float(np.max(arr))
    std_val = float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0

    if std_val > config.seed_std_max:
        return GateDecision(
            action="STOP_HIGH_VARIANCE",
            passed=False,
            metric_key=config.metric_key,
            observed_mean=mean_val,
            observed_max=max_val,
            observed_std=std_val,
            n_seeds=int(arr.size),
            thresholds=thresholds,
            details=f"Seed std {std_val:.6f} exceeds threshold {config.seed_std_max:.6f}.",
        )

    if epoch_curve and len(epoch_curve) >= config.tail_gain_last_n:
        tail = np.array(epoch_curve[-config.tail_gain_last_n :], dtype=float)
        tail_gain = float(tail[-1] - tail[0])
        if tail_gain > config.tail_gain_max:
            thresholds["tail_gain_max"] = config.tail_gain_max
            return GateDecision(
                action="STOP_STILL_IMPROVING",
                passed=False,
                metric_key=config.metric_key,
                observed_mean=mean_val,
                observed_max=max_val,
                observed_std=std_val,
                n_seeds=int(arr.size),
                thresholds=thresholds,
                details=(
                    f"Tail gain {tail_gain:.6f} over last {config.tail_gain_last_n} epochs "
                    f"exceeds {config.tail_gain_max:.6f}; training may benefit from more epochs."
                ),
            )

    mean_ok = mean_val >= config.target_mean_min
    any_ok = max_val >= config.target_any_seed_min

    if mean_ok or any_ok:
        return GateDecision(
            action="CONTINUE",
            passed=True,
            metric_key=config.metric_key,
            observed_mean=mean_val,
            observed_max=max_val,
            observed_std=std_val,
            n_seeds=int(arr.size),
            thresholds=thresholds,
            details="Gate passed.",
        )

    return GateDecision(
        action="STOP_BELOW_TARGET",
        passed=False,
        metric_key=config.metric_key,
        observed_mean=mean_val,
        observed_max=max_val,
        observed_std=std_val,
        n_seeds=int(arr.size),
        thresholds=thresholds,
        details=(
            f"Mean {mean_val:.6f} < {config.target_mean_min:.6f} and "
            f"max {max_val:.6f} < {config.target_any_seed_min:.6f}."
        ),
    )
