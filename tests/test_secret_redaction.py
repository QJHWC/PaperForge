from __future__ import annotations

from pathlib import Path

import pytest

from engine.secret_redaction import contains_secret, redact_secrets
from paperforge.release import scan_workspace_secrets
from paperforge.scientific_memory import ScientificMemory


def test_redacts_common_secret_forms(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "env-secret-value")
    provider_token = "sk-" + "exampleToken123456789"
    named_token = "named-" + "token-value"
    text = "\n".join(
        (
            "raw=env-secret-value",
            f"token {provider_token}",
            "Authorization: Bearer bearer-token-value",
            f'api_key="{named_token}"',
            "--openai-api-key cli-token-value",
        )
    )

    sanitized = redact_secrets(text)

    for secret in (
        "env-secret-value",
        provider_token,
        "bearer-token-value",
        named_token,
        "cli-token-value",
    ):
        assert secret not in sanitized
    assert sanitized.count("***redacted***") == 5


def test_preserves_non_secret_diagnostics(monkeypatch) -> None:
    for key in (
        "OPENAI_API_KEY",
        "OPENAI_WRITEUP_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)

    message = "[done] workspace=paper_writer/example"
    assert redact_secrets(message) == message
    assert not contains_secret({"token_count": 42, "command": ["python", "-V"]})


def test_detects_secret_fields_and_secret_argv() -> None:
    assert contains_secret({"api_key": "fixture"})
    assert contains_secret(
        {"argv": ["tool", "--access-token", "secret-fixture"]}
    )


def test_scientific_memory_rejects_secrets_before_persistence(
    tmp_path: Path,
) -> None:
    memory = ScientificMemory(tmp_path / "memory.db")
    secret = "sk-" + "scientificMemoryCanary123"

    with pytest.raises(ValueError, match="must not contain credentials"):
        memory.add_source(kind="REMOTE", uri=f"https://example.test/?key={secret}")
    with pytest.raises(ValueError, match="must not contain credentials"):
        memory.add_evidence(
            evidence_type="SOURCE_CODE",
            excerpt=f"api_key={secret}",
        )
    with pytest.raises(ValueError, match="must not contain credentials"):
        memory.add_claim(
            claim_type="STATIC_IMPLEMENTATION",
            status="SUPPORTED_STATIC",
            text=f"The configured token is {secret}.",
        )

    assert secret.encode() not in (tmp_path / "memory.db").read_bytes()


def test_source_hashes_are_strict_and_binary_secret_scan_finds_adjacent_token(
    tmp_path: Path,
) -> None:
    memory = ScientificMemory(tmp_path / "memory.db")
    secret = "sk-" + ("x" * 24)
    for field in ("blob_sha256", "content_sha256", "notice_sha256"):
        with pytest.raises(ValueError, match="64-character hexadecimal"):
            memory.add_source(
                kind="SOURCE",
                uri="fixture://source",
                **{field: secret},
            )
    binary = tmp_path / "fixture.db"
    binary.write_bytes(b"adjacent-prefixTx" + secret.encode() + b"suffix")

    result = scan_workspace_secrets(tmp_path)

    assert not result["clean"]
    assert any(finding["path"] == "fixture.db" for finding in result["findings"])
