from types import SimpleNamespace

from engine.llm import (
    AVAILABLE_LLMS,
    create_client,
    get_batch_responses_from_llm,
    get_response_from_llm,
)


class _FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content="ok")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_bailu_model_is_available():
    assert "bailu-turing" in AVAILABLE_LLMS


def test_bailu_client_uses_openai_compatible_configuration(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/openapi")

    client, model = create_client("bailu-turing")

    assert model == "bailu-turing"
    assert str(client.base_url) == "https://example.invalid/openapi/"


def test_bailu_chat_uses_portable_openai_parameters():
    completions = _FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    text, history = get_response_from_llm(
        "hello",
        client=client,
        model="bailu-turing",
        system_message="system",
        temperature=0.2,
    )

    assert text == "ok"
    assert history[-1] == {"role": "assistant", "content": "ok"}
    kwargs = completions.calls[0]
    assert kwargs["model"] == "bailu-turing"
    assert kwargs["temperature"] == 0.2
    assert "reasoning_effort" not in kwargs
    assert "seed" not in kwargs


def test_bailu_batch_reuses_portable_single_response_requests():
    completions = _FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    texts, histories = get_batch_responses_from_llm(
        "hello",
        client=client,
        model="bailu-turing",
        system_message="system",
        n_responses=2,
    )

    assert texts == ["ok", "ok"]
    assert len(histories) == 2
    assert len(completions.calls) == 2
    assert all("n" not in kwargs for kwargs in completions.calls)
    assert all("seed" not in kwargs for kwargs in completions.calls)
    assert all("stop" not in kwargs for kwargs in completions.calls)
