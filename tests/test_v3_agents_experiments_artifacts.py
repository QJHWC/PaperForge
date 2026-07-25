from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from paperforge.agents import (
    AgentRegistry,
    AgentRequest,
    AgentResult,
    AgentResultStatus,
    AgentRole,
    AgentTraceIntegrityError,
    PersistentTraceStore,
)
from paperforge.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotAllowedError,
    ArtifactPathError,
    ArtifactStore,
)
from paperforge.experiments import (
    ExperimentManager,
    ExperimentTransitionError,
    ProvenanceKind,
    ProvenanceViolation,
)
from paperforge.models import (
    ClaimStatus,
    ClaimType,
    ExecutionProfile,
    ExperimentStage,
    ExperimentStatus,
)
from paperforge.policy import PolicyViolation


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_registry_covers_v3_roles_and_dispatches_typed_trace_inputs(
    tmp_path: Path,
) -> None:
    trace_store = PersistentTraceStore(tmp_path)
    trace = trace_store.persist(
        {"source": "evidence/source.json", "decision": "inspect"},
        name="research-input",
    )
    registry = AgentRegistry.with_defaults(trace_store=trace_store)

    assert registry.roles == frozenset(AgentRole)
    assert AgentRegistry().roles == frozenset(AgentRole)

    request = AgentRequest(
        role=AgentRole.RESEARCH,
        task="Inspect the persisted evidence trace.",
        payload={"claim_id": "claim-1"},
        trace_inputs=(trace,),
    )
    result = registry.dispatch(request)

    assert isinstance(result, AgentResult)
    assert result.request_id == request.request_id
    assert result.role is AgentRole.RESEARCH
    assert result.status is AgentResultStatus.BLOCKED
    assert result.output["trace_input_count"] == 1
    assert trace_store.load(trace)["decision"] == "inspect"

    trace_path = tmp_path / trace.path
    trace_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(AgentTraceIntegrityError):
        registry.dispatch(request)


def test_artifact_store_restricts_paths_and_verifies_manifest(tmp_path: Path) -> None:
    store = ArtifactStore(
        tmp_path,
        allowed_roots=("artifacts",),
        allowed_suffixes=(".json",),
        allowed_kinds=("metrics", "manifest"),
    )
    record = store.write_json(
        "artifacts/metrics.json",
        {"accuracy": 0.75},
        kind="metrics",
    )

    assert record.path == "artifacts/metrics.json"
    assert record.sha256 == _sha256(tmp_path / record.path)
    assert not Path(record.path).is_absolute()

    manifest_record = store.write_manifest(
        "artifacts/manifest.json",
        kind="manifest",
    )
    manifest = json.loads((tmp_path / manifest_record.path).read_text(encoding="utf-8"))
    assert manifest["artifacts"][0]["path"] == record.path
    assert store.verify_manifest(manifest)["verified"]

    with pytest.raises(ArtifactPathError):
        store.write_json("/tmp/outside.json", {}, kind="metrics")
    with pytest.raises(ArtifactPathError):
        store.write_json("../outside.json", {}, kind="metrics")
    with pytest.raises(ArtifactNotAllowedError):
        store.write_text("artifacts/result.txt", "no", kind="metrics")
    with pytest.raises(ArtifactNotAllowedError):
        store.write_json("artifacts/other.json", {}, kind="paper")

    outside = tmp_path / "outside.json"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "artifacts" / "link.json"
    link.symlink_to(outside)
    with pytest.raises(ArtifactPathError):
        store.register("artifacts/link.json", kind="metrics")

    (tmp_path / record.path).write_text('{"accuracy": 1}', encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError):
        store.verify_manifest(manifest)


def test_experiment_state_machine_records_hashes_and_claim_eligibility(
    tmp_path: Path,
) -> None:
    code = tmp_path / "src" / "train.py"
    config = tmp_path / "configs" / "run.json"
    data = tmp_path / "data" / "sample.csv"
    checkpoint = tmp_path / "artifacts" / "model.ckpt"
    metrics = tmp_path / "artifacts" / "metrics.json"
    for path, content in (
        (code, "print('train')\n"),
        (config, '{"seed": 7}\n'),
        (data, "x,y\n1,2\n"),
        (checkpoint, "weights"),
        (metrics, '{"accuracy": 0.8}\n'),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    manager = ExperimentManager(tmp_path, profile=ExecutionProfile.FULL)
    proposal = manager.propose(
        title="Measured validation",
        command=("python", "src/train.py"),
        code_paths=("src/train.py",),
        config_paths=("configs/run.json",),
        data_paths=("data/sample.csv",),
    )

    with pytest.raises(PermissionError):
        manager.record_stage(
            proposal.proposal_id,
            ExperimentStage.STATIC_CHECK,
            status=ExperimentStatus.PASSED,
            provenance_kind=ProvenanceKind.MEASURED,
        )

    manager.approve(
        proposal.proposal_id,
        approved_by="owner",
        scope={"stage": "full"},
    )
    with pytest.raises(ExperimentTransitionError):
        manager.record_stage(
            proposal.proposal_id,
            ExperimentStage.FULL_EXPERIMENT,
            status=ExperimentStatus.PASSED,
            provenance_kind=ProvenanceKind.MEASURED,
        )

    static_run = manager.record_stage(
        proposal.proposal_id,
        ExperimentStage.STATIC_CHECK,
        status=ExperimentStatus.PASSED,
        provenance_kind=ProvenanceKind.MEASURED,
    )
    assert not static_run.eligible_for_claims

    with pytest.raises(ProvenanceViolation):
        manager.record_stage(
            proposal.proposal_id,
            ExperimentStage.MINI_EXPERIMENT,
            status=ExperimentStatus.PASSED,
            provenance_kind=ProvenanceKind.SIMULATED,
            metadata={"performance_conclusion": "The method is better."},
        )

    mini_run = manager.record_stage(
        proposal.proposal_id,
        ExperimentStage.MINI_EXPERIMENT,
        status=ExperimentStatus.PASSED,
        provenance_kind=ProvenanceKind.SIMULATED,
    )
    assert not mini_run.eligible_for_claims

    full_run = manager.execute_stage(
        proposal.proposal_id,
        ExperimentStage.FULL_EXPERIMENT,
        provenance_kind=ProvenanceKind.MEASURED,
        executor=lambda *_: {
            "returncode": 0,
            "metrics_paths": ("artifacts/metrics.json",),
            "checkpoint_paths": ("artifacts/model.ckpt",),
        },
        checkpoint_paths=("artifacts/model.ckpt",),
        metrics_paths=("artifacts/metrics.json",),
    )

    assert full_run.eligible_for_claims
    assert full_run.code_sha256 == _sha256(code)
    assert full_run.config_sha256 == _sha256(config)
    assert full_run.data_sha256 == _sha256(data)
    assert full_run.checkpoint_sha256 == _sha256(checkpoint)
    assert full_run.metrics_sha256 == _sha256(metrics)
    assert manager.get_proposal(proposal.proposal_id).status is ExperimentStatus.PASSED

    claim_id = manager.memory.add_claim(
        claim_type=ClaimType.EXPERIMENT_RESULT,
        text="Measured fixture accuracy is 0.8.",
        status=ClaimStatus.VERIFIED_EXPERIMENT,
    )
    source_only = manager.memory.add_evidence(
        evidence_type="SOURCE_CODE",
        excerpt="accuracy fixture",
    )
    manager.memory.link_claim(claim_id, source_only)
    assert not manager.memory.claim_gate()["passed"]

    metric_evidence = manager.import_metric_evidence(
        full_run.run_id,
        excerpt="accuracy=0.8",
        path="artifacts/metrics.json",
        line_start=1,
        line_end=1,
    )
    manager.memory.link_claim(claim_id, metric_evidence)
    assert manager.memory.claim_gate()["passed"]


def test_experiment_execution_requires_full_profile_even_when_approved(
    tmp_path: Path,
) -> None:
    manager = ExperimentManager(tmp_path, profile=ExecutionProfile.RESEARCH)
    proposal = manager.propose(title="Research proposal")
    manager.approve(proposal.proposal_id, approved_by="owner")

    with pytest.raises(PolicyViolation):
        manager.record_stage(
            proposal.proposal_id,
            ExperimentStage.STATIC_CHECK,
            status=ExperimentStatus.PASSED,
            provenance_kind=ProvenanceKind.MEASURED,
        )


def test_experiment_approval_scope_is_a_maximum_stage(tmp_path: Path) -> None:
    manager = ExperimentManager(tmp_path, profile=ExecutionProfile.FULL)
    proposal = manager.propose(title="Mini only")
    manager.approve(
        proposal.proposal_id,
        approved_by="owner",
        scope={"maximum_stage": "mini"},
    )
    manager.record_stage(
        proposal.proposal_id,
        ExperimentStage.STATIC_CHECK,
        status=ExperimentStatus.PASSED,
        provenance_kind=ProvenanceKind.MEASURED,
    )
    manager.record_stage(
        proposal.proposal_id,
        ExperimentStage.MINI_EXPERIMENT,
        status=ExperimentStatus.PASSED,
        provenance_kind=ProvenanceKind.MEASURED,
    )

    with pytest.raises(PermissionError):
        manager.record_stage(
            proposal.proposal_id,
            ExperimentStage.FULL_EXPERIMENT,
            status=ExperimentStatus.PASSED,
            provenance_kind=ProvenanceKind.MEASURED,
        )
