from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperforge.models import (
    ClaimRelation,
    ClaimStatus,
    ClaimType,
    CompletionGate,
    ExecutionProfile,
    WorkflowStatus,
)
from paperforge.policy import Action, ExecutionPolicy, PolicyViolation
from paperforge.provider import (
    CredentialResolver,
    ProviderConfigurationError,
    ProviderRegistry,
    ProviderRequestBuilder,
    preflight_openai_compatible,
)
from paperforge.scientific_memory import ScientificMemory
from paperforge.workflow import InvalidTransition, WorkflowEngine


def _passing_gate() -> CompletionGate:
    return CompletionGate(
        claim_gate_passed=True,
        required_artifacts_present=True,
        latex_clean_compile=True,
        all_pdf_pages_inspected=True,
        protected_hashes_unchanged=True,
        secret_scan_clean=True,
        release_manifest_verified=True,
    )


def test_writing_only_policy_denies_experiment_and_run_artifacts() -> None:
    policy = ExecutionPolicy(ExecutionProfile.WRITING_ONLY)
    policy.require(Action.DRAFT_EDIT)

    with pytest.raises(PolicyViolation):
        policy.require(Action.EXPERIMENT_MINI)
    with pytest.raises(PolicyViolation):
        policy.validate_command(["python", "experiment.py"], Action.LOCAL_EXECUTE)
    with pytest.raises(PolicyViolation):
        policy.validate_context_paths(["workspace/run_0/final_info.json"])


def test_research_policy_allows_proposals_but_not_execution() -> None:
    policy = ExecutionPolicy(ExecutionProfile.RESEARCH)
    policy.require(Action.PROPOSAL_CREATE)
    with pytest.raises(PolicyViolation):
        policy.require(Action.LOCAL_EXECUTE)


def test_bailu_provider_keeps_exact_base_url_and_filters_payload() -> None:
    registry = ProviderRegistry(
        env={
            "OPENAI_BASE_URL": "https://example.invalid/openapi/v1",
            "OPENAI_API_KEY": "same",
            "OPENAI_WRITEUP_API_KEY": "same",
        }
    )
    config = registry.resolve("bailu-turing", stage="writeup")

    assert config.base_url == "https://example.invalid/openapi/v1"
    assert registry.openai_client_kwargs(config)["base_url"] == config.base_url
    assert registry.filter_payload(
        config,
        {
            "model": config.model,
            "reasoning_effort": "high",
            "seed": 7,
            "n": 2,
            "stop": ["END"],
            "stream": True,
        },
    ) == {"model": "bailu-turing", "stream": False}


def test_provider_rejects_conflicting_legacy_base_urls() -> None:
    registry = ProviderRegistry(
        env={
            "OPENAI_BASE_URL": "https://one.invalid/v1",
            "OPENAI_API_BASE": "https://two.invalid/v1",
        }
    )
    with pytest.raises(ProviderConfigurationError):
        registry.resolve("bailu-turing")


def test_provider_request_builder_filters_every_bailu_request_path() -> None:
    registry = ProviderRegistry(env={})
    payload = ProviderRequestBuilder(registry).chat_completion(
        "bailu-turing",
        messages=[{"role": "user", "content": "hello"}],
        reasoning_effort="high",
        seed=3,
        n=2,
        stop=["END"],
        stream=True,
        max_tokens=16,
    )

    assert payload == {
        "model": "bailu-turing",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
        "max_tokens": 16,
    }


def test_primary_and_writeup_credentials_may_be_distinct() -> None:
    registry = ProviderRegistry(
        env={
            "OPENAI_API_KEY": "primary-secret",
            "OPENAI_WRITEUP_API_KEY": "writeup-secret",
        }
    )

    assert registry.credential(
        registry.resolve("gpt-4o", stage="default")
    ) == "primary-secret"
    assert registry.credential(
        registry.resolve("gpt-4o", stage="writeup")
    ) == "writeup-secret"


def test_credential_file_requires_private_permissions(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"bailu_primary": "secret"}), encoding="utf-8")
    credentials.chmod(0o644)

    with pytest.raises(ProviderConfigurationError):
        CredentialResolver(env={}, config_dir=tmp_path).resolve("bailu_primary")

    credentials.chmod(0o600)
    assert CredentialResolver(env={}, config_dir=tmp_path).resolve("bailu_primary") == "secret"


def test_provider_preflight_classifies_auth_failure() -> None:
    class Unauthorized(Exception):
        status_code = 401

    class Completions:
        @staticmethod
        def create(**kwargs):
            raise Unauthorized("invalid key")

    client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": Completions()})()},
    )()
    config = ProviderRegistry(env={}).resolve("bailu-turing")

    report = preflight_openai_compatible(config, client=client)

    assert report.status == "AUTH_BLOCKED"
    assert not report.response_received


def test_scientific_memory_blocks_unsupported_and_contradicted_claims(tmp_path: Path) -> None:
    memory = ScientificMemory(tmp_path / "paperforge.db")
    source_id = memory.add_source(
        kind="SOURCE_SNAPSHOT",
        uri="https://example.invalid/repo",
        commit_sha="abc",
        path="model.py",
        blob_sha256="blob",
    )
    evidence_id = memory.add_evidence(
        evidence_type="SOURCE_CODE",
        source_id=source_id,
        path="model.py",
        line_start=1,
        line_end=2,
        excerpt="return value",
    )
    supported = memory.add_claim(
        claim_type=ClaimType.STATIC_IMPLEMENTATION,
        text="The implementation returns one value.",
        status=ClaimStatus.SUPPORTED_STATIC,
    )
    memory.link_claim(supported, evidence_id, ClaimRelation.SUPPORTS)

    assert memory.claim_gate()["passed"]

    blocked = memory.add_claim(
        claim_type=ClaimType.EXPERIMENT_RESULT,
        text="The method improves performance.",
        status=ClaimStatus.BLOCKED,
    )
    memory.link_claim(blocked, evidence_id, ClaimRelation.SUPPORTS)
    gate = memory.claim_gate()
    assert not gate["passed"]
    assert any(item["claim_id"] == blocked for item in gate["failures"])


def test_claim_manifest_maps_tex_spans_to_evidence(tmp_path: Path) -> None:
    memory = ScientificMemory(tmp_path / "paperforge.db")
    source_id = memory.add_source(kind="LITERATURE", uri="doi:example")
    evidence_id = memory.add_evidence(
        evidence_type="LITERATURE",
        source_id=source_id,
        excerpt="Published evidence.",
    )
    claim_id = memory.add_claim(
        claim_type=ClaimType.LITERATURE,
        text="Prior work reported the method.",
        status=ClaimStatus.SUPPORTED_STATIC,
    )
    memory.link_claim(claim_id, evidence_id)

    manifest = memory.claim_manifest({claim_id: {"file": "main.tex", "start": 10, "end": 10}})

    assert manifest["claims"][0]["claim_id"] == claim_id
    assert manifest["claims"][0]["tex_span"]["start"] == 10
    assert manifest["claims"][0]["evidence"][0]["evidence_id"] == evidence_id


def test_workflow_requires_all_completion_gates(tmp_path: Path) -> None:
    engine = WorkflowEngine(tmp_path)
    workflow_id = engine.create(ExecutionProfile.WRITING_ONLY)
    engine.transition(workflow_id, WorkflowStatus.RUNNING)

    with pytest.raises(InvalidTransition):
        engine.transition(workflow_id, WorkflowStatus.COMPLETED, gate=CompletionGate())

    state = engine.transition(
        workflow_id,
        WorkflowStatus.COMPLETED,
        gate=_passing_gate(),
        checkpoint="release",
    )
    assert state["status"] == WorkflowStatus.COMPLETED.value


def test_full_profile_requires_approved_proposal(tmp_path: Path) -> None:
    engine = WorkflowEngine(tmp_path)
    with pytest.raises(PermissionError):
        engine.require_approval("proposal-1")

    engine.approve("proposal-1", approved_by="owner", scope={"stage": "mini"})
    approval = engine.require_approval("proposal-1")
    assert approval["scope"] == {"stage": "mini"}
