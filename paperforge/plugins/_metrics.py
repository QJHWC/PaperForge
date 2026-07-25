from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Hashable, Iterable, Sequence


def mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("mean requires at least one value")
    return math.fsum(values) / len(values)


def median(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("median requires at least one value")
    return float(statistics.median(values))


def mae(targets: Sequence[float], predictions: Sequence[float]) -> float:
    return mean(
        [abs(prediction - target) for target, prediction in zip(targets, predictions, strict=True)]
    )


def rmse(targets: Sequence[float], predictions: Sequence[float]) -> float:
    return math.sqrt(
        mean(
            [
                (prediction - target) ** 2
                for target, prediction in zip(targets, predictions, strict=True)
            ]
        )
    )


def pearson_r(targets: Sequence[float], predictions: Sequence[float]) -> float | None:
    if len(targets) < 2:
        return None
    target_mean = mean(list(targets))
    prediction_mean = mean(list(predictions))
    numerator = math.fsum(
        (target - target_mean) * (prediction - prediction_mean)
        for target, prediction in zip(targets, predictions, strict=True)
    )
    target_scale = math.sqrt(math.fsum((target - target_mean) ** 2 for target in targets))
    prediction_scale = math.sqrt(
        math.fsum((prediction - prediction_mean) ** 2 for prediction in predictions)
    )
    denominator = target_scale * prediction_scale
    if denominator == 0:
        return None
    return numerator / denominator


def r_squared(targets: Sequence[float], predictions: Sequence[float]) -> float | None:
    target_mean = mean(list(targets))
    total = math.fsum((target - target_mean) ** 2 for target in targets)
    if total == 0:
        return None
    residual = math.fsum(
        (target - prediction) ** 2 for target, prediction in zip(targets, predictions, strict=True)
    )
    return 1.0 - residual / total


def regression_metrics(
    targets: Sequence[float],
    predictions: Sequence[float],
) -> dict[str, int | float]:
    if len(targets) != len(predictions) or not targets:
        raise ValueError("regression metrics require aligned non-empty rows")
    metrics: dict[str, int | float] = {
        "sample_count": len(targets),
        "mae": mae(targets, predictions),
        "rmse": rmse(targets, predictions),
    }
    correlation = pearson_r(targets, predictions)
    if correlation is not None:
        metrics["pearson_r"] = correlation
    coefficient = r_squared(targets, predictions)
    if coefficient is not None:
        metrics["r_squared"] = coefficient
    return metrics


def classification_metrics(
    targets: Sequence[Hashable],
    predictions: Sequence[Hashable],
) -> tuple[dict[str, int | float], list[dict[str, object]]]:
    if len(targets) != len(predictions) or not targets:
        raise ValueError("classification metrics require aligned non-empty rows")
    labels = sorted(set(targets) | set(predictions), key=lambda value: str(value))
    correct = sum(
        target == prediction for target, prediction in zip(targets, predictions, strict=True)
    )
    precisions: list[float] = []
    recalls: list[float] = []
    f1_values: list[float] = []
    confusion: list[dict[str, object]] = []
    for target_label in labels:
        for predicted_label in labels:
            count = sum(
                target == target_label and prediction == predicted_label
                for target, prediction in zip(targets, predictions, strict=True)
            )
            confusion.append(
                {
                    "target": str(target_label),
                    "prediction": str(predicted_label),
                    "count": count,
                }
            )
    for label in labels:
        true_positive = sum(
            target == label and prediction == label
            for target, prediction in zip(targets, predictions, strict=True)
        )
        false_positive = sum(
            target != label and prediction == label
            for target, prediction in zip(targets, predictions, strict=True)
        )
        false_negative = sum(
            target == label and prediction != label
            for target, prediction in zip(targets, predictions, strict=True)
        )
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 0.0
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative
            else 0.0
        )
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        precisions.append(precision)
        recalls.append(recall)
        f1_values.append(f1)
    return (
        {
            "sample_count": len(targets),
            "class_count": len(labels),
            "accuracy": correct / len(targets),
            "precision_macro": mean(precisions),
            "recall_macro": mean(recalls),
            "f1_macro": mean(f1_values),
        },
        confusion,
    )


def multiset_overlap(
    reference: Iterable[str],
    prediction: Iterable[str],
) -> tuple[float, float, float]:
    reference_counts = Counter(reference)
    prediction_counts = Counter(prediction)
    overlap = sum((reference_counts & prediction_counts).values())
    reference_total = sum(reference_counts.values())
    prediction_total = sum(prediction_counts.values())
    precision = overlap / prediction_total if prediction_total else float(reference_total == 0)
    recall = overlap / reference_total if reference_total else float(prediction_total == 0)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1
