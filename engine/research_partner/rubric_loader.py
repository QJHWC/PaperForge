from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class SchemaValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ScoringDimension:
    name: str
    weight: float
    description: str


@dataclass(frozen=True)
class RubricProfile:
    name: str
    approval_threshold: float
    max_rounds: int
    dimensions: tuple[ScoringDimension, ...]


def _default_rubrics_dir() -> Path:
    return Path(__file__).resolve().parent / "rubrics"


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SchemaValidationError(f"Invalid YAML: {path}") from exc
    if not isinstance(payload, dict):
        raise SchemaValidationError(f"Rubric file must contain a mapping: {path}")
    return payload


def _parse_dimensions(raw_dimensions: Any) -> tuple[ScoringDimension, ...]:
    if not isinstance(raw_dimensions, list) or not raw_dimensions:
        raise SchemaValidationError("Rubric dimensions must be a non-empty list")

    dimensions: list[ScoringDimension] = []
    weight_sum = 0.0
    seen_names: set[str] = set()
    for item in raw_dimensions:
        if not isinstance(item, dict):
            raise SchemaValidationError("Each rubric dimension must be a mapping")
        name = str(item.get("name") or "").strip()
        description = str(item.get("description") or "").strip()
        try:
            weight = float(item.get("weight"))
        except (TypeError, ValueError) as exc:
            raise SchemaValidationError("Rubric dimension weight must be numeric") from exc
        if not name or not description:
            raise SchemaValidationError("Rubric dimension requires name and description")
        if name in seen_names:
            raise SchemaValidationError("Rubric dimension names must be unique")
        seen_names.add(name)
        dimensions.append(ScoringDimension(name=name, weight=weight, description=description))
        weight_sum += weight

    if abs(weight_sum - 1.0) > 1e-6:
        raise SchemaValidationError("Rubric dimension weights must sum to 1.0")
    return tuple(dimensions)


def _build_profile(payload: dict[str, Any]) -> RubricProfile:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise SchemaValidationError("Rubric profile requires name")
    try:
        approval_threshold = float(payload.get("approval_threshold"))
        max_rounds = int(payload.get("max_rounds"))
    except (TypeError, ValueError) as exc:
        raise SchemaValidationError("Rubric profile requires numeric approval_threshold and max_rounds") from exc
    if not 0.0 <= approval_threshold <= 1.0:
        raise SchemaValidationError("approval_threshold must be between 0.0 and 1.0")
    if max_rounds < 1:
        raise SchemaValidationError("max_rounds must be >= 1")
    dimensions = _parse_dimensions(payload.get("dimensions"))
    return RubricProfile(
        name=name,
        approval_threshold=approval_threshold,
        max_rounds=max_rounds,
        dimensions=dimensions,
    )


def load_rubric_profile(venue: str, *, rubrics_dir: Path | None = None) -> RubricProfile:
    base_dir = (rubrics_dir or _default_rubrics_dir()).expanduser().resolve()
    requested = (venue or "default").strip() or "default"
    requested_path = base_dir / f"{requested}.yaml"
    target_path = requested_path if requested_path.exists() else base_dir / "default.yaml"
    if not target_path.exists():
        raise SchemaValidationError(f"Rubric profile not found: {requested}")
    return _build_profile(_load_yaml(target_path))
