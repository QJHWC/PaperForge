from __future__ import annotations

from engine.secret_redaction import redact_secrets


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
