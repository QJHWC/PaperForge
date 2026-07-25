from __future__ import annotations

import math

import pytest

from paperforge.plugins import (
    BioPlugin,
    CVPlugin,
    DataValidationError,
    DomainPlugin,
    DomainPluginRegistry,
    NLPPlugin,
    PhysicsMaterialPlugin,
    RLPlugin,
    RoboticsPlugin,
    builtin_registry,
)


@pytest.mark.parametrize(
    ("name", "plugin_type"),
    [
        ("cv", CVPlugin),
        ("nlp", NLPPlugin),
        ("rl", RLPlugin),
        ("bio", BioPlugin),
        ("physics-material", PhysicsMaterialPlugin),
        ("robotics", RoboticsPlugin),
    ],
)
def test_builtin_registry_exposes_six_runnable_plugins(
    name: str,
    plugin_type: type[DomainPlugin],
) -> None:
    plugin = builtin_registry.get(name)
    assert isinstance(plugin, plugin_type)
    assert plugin.name == name
    assert name in builtin_registry.names()


def test_registry_rejects_duplicate_names_and_supports_aliases() -> None:
    registry = DomainPluginRegistry()
    plugin = CVPlugin()
    registry.register(plugin, aliases=("computer-vision",))

    assert registry.get("CV") is plugin
    assert registry.get("computer_vision") is plugin
    with pytest.raises(ValueError, match="already registered"):
        registry.register(CVPlugin())


def test_cv_metrics_evidence_and_visualization_use_observed_rows() -> None:
    rows = [
        {"sample_id": "a", "target": "cat", "prediction": "cat"},
        {"sample_id": "b", "target": "cat", "prediction": "dog"},
        {"sample_id": "c", "target": "dog", "prediction": "dog"},
        {"sample_id": "d", "target": "dog", "prediction": "dog"},
    ]
    plugin = CVPlugin()
    result = plugin.run(rows)

    assert result.metrics["accuracy"] == pytest.approx(0.75)
    assert result.metrics["sample_count"] == 4
    assert result.evidence
    assert all(record.source == "observed_rows" for record in result.evidence)
    assert all(not record.simulated for record in result.evidence)
    assert all(record.dataset_sha256 for record in result.evidence)
    assert all(not record.eligible_for_claims for record in result.evidence)
    assert result.visualizations[0].data
    assert result.to_dict()["metrics"]["accuracy"] == pytest.approx(0.75)
    assert plugin.evidence_records(rows)[0].source == "observed_rows"
    assert plugin.visualization_spec(rows).data


def test_plugin_validation_reports_row_and_field_without_partial_metrics() -> None:
    with pytest.raises(DataValidationError) as error:
        NLPPlugin().run(
            [
                {"sample_id": "ok", "reference": "paper forge", "prediction": "paper"},
                {"sample_id": "bad", "reference": "missing prediction"},
            ]
        )

    assert error.value.issues[0].row_index == 1
    assert error.value.issues[0].field == "prediction"


@pytest.mark.parametrize(
    ("plugin", "rows", "required_metrics"),
    [
        (
            NLPPlugin(),
            [
                {"reference": "a b", "prediction": "a b"},
                {"reference": "a c", "prediction": "a"},
            ],
            {"exact_match", "token_f1", "sample_count"},
        ),
        (
            RLPlugin(),
            [
                {"episode_id": "one", "reward": 1.0, "done": False},
                {"episode_id": "one", "reward": 2.0, "done": True, "success": True},
                {"episode_id": "two", "reward": -1.0, "done": True, "success": False},
            ],
            {"episode_count", "mean_episode_return", "mean_episode_length"},
        ),
        (
            BioPlugin(),
            [
                {"target": 1.0, "prediction": 1.2},
                {"target": 2.0, "prediction": 1.8},
            ],
            {"mae", "rmse", "sample_count"},
        ),
        (
            PhysicsMaterialPlugin(),
            [
                {"measured_value": 10.0, "predicted_value": 9.0},
                {"measured_value": 12.0, "predicted_value": 13.0},
            ],
            {"mae", "rmse", "mean_relative_error"},
        ),
        (
            RoboticsPlugin(),
            [
                {
                    "success": True,
                    "collision": False,
                    "position_error": 0.01,
                    "orientation_error": 0.02,
                },
                {
                    "success": False,
                    "collision": True,
                    "position_error": 0.05,
                    "orientation_error": 0.08,
                },
            ],
            {
                "success_rate",
                "collision_rate",
                "mean_position_error",
                "mean_orientation_error",
            },
        ),
    ],
)
def test_domain_plugins_compute_only_real_finite_metrics(
    plugin: DomainPlugin,
    rows: list[dict[str, object]],
    required_metrics: set[str],
) -> None:
    result = plugin.run(rows)

    assert required_metrics <= result.metrics.keys()
    assert all(
        not isinstance(value, float) or math.isfinite(value) for value in result.metrics.values()
    )
    assert "simulated_accuracy" not in result.metrics
    assert {record.metric for record in result.evidence} <= result.metrics.keys()
    assert result.visualizations
