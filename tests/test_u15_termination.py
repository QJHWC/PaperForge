from __future__ import annotations

from pathlib import Path

import pytest

from paperforge.api import PaperForgeService
from paperforge.provider import (
    GenerationResult,
    _parse_generation_response,
    chat_completion_structured,
    chat_completion_text,
    coerce_generation_result,
)
from paperforge.writing import WritingEngine, WritingSafetyError


class _Choice:
    def __init__(self, content: str, finish_reason: str | None) -> None:
        self.message = type("M", (), {"content": content})()
        self.finish_reason = finish_reason


class _Response:
    def __init__(self, content: str, finish_reason: str | None) -> None:
        self.choices = [_Choice(content, finish_reason)]
        self.usage = type("U", (), {"completion_tokens": 12})()


def _document(prose: str) -> str:
    return (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        f"{prose}\n"
        "% PAPERFORGE-PROTECTED-EXPERIMENT-START\n"
        "Author-owned results.\n"
        "% PAPERFORGE-PROTECTED-EXPERIMENT-END\n"
        "\\end{document}\n"
    )


def test_parse_length_is_truncated_even_when_text_looks_complete() -> None:
    parsed = _parse_generation_response(
        _Response("A complete sentence with a full stop.", "length")
    )
    assert parsed.truncated is True
    assert parsed.text.endswith(".")


def test_parse_stop_is_not_truncated() -> None:
    parsed = _parse_generation_response(_Response("Done.", "stop"))
    assert parsed.truncated is False
    assert parsed.termination_unknown is False


def test_legacy_string_coerced_to_unknown() -> None:
    coerced = coerce_generation_result("legacy text")
    assert coerced.text == "legacy text"
    assert coerced.termination_unknown is True
    assert coerced.truncated is False


def test_structured_length_rejects_write_even_if_complete(tmp_path: Path) -> None:
    tex = tmp_path / "paper.tex"
    tex.write_text(_document("Original prose."), encoding="utf-8")
    candidate = _document("Changed prose.")  # syntactically perfect
    service = PaperForgeService(tmp_path)
    engine = WritingEngine(
        tmp_path,
        memory=service.memory,
        request_text=lambda *a, **k: GenerationResult(
            text=candidate, finish_reason="length", truncated=True
        ),
    )
    with pytest.raises(WritingSafetyError, match="GENERATION_TRUNCATED"):
        engine.write(
            run_id="wf-1",
            payload={"main_tex": "paper.tex", "instructions": "x"},
            model="bailu-turing",
        )
    assert tex.read_text(encoding="utf-8") == _document("Original prose.")


def test_structured_stop_allows_write_and_audits_termination(tmp_path: Path) -> None:
    tex = tmp_path / "paper.tex"
    tex.write_text(_document("Original prose."), encoding="utf-8")
    candidate = _document("Changed prose.")
    service = PaperForgeService(tmp_path)
    engine = WritingEngine(
        tmp_path,
        memory=service.memory,
        request_text=lambda *a, **k: GenerationResult(
            text=candidate, finish_reason="stop", truncated=False
        ),
    )
    result = engine.write(
        run_id="wf-1",
        payload={"main_tex": "paper.tex", "instructions": "x"},
        model="bailu-turing",
    )
    metadata = result.artifact.to_dict()["metadata"]
    assert metadata["generation_termination"]["finish_reason"] == "stop"
    assert metadata["generation_termination"]["truncated"] is False


def test_old_string_call_still_writes_with_unknown_audit(tmp_path: Path) -> None:
    tex = tmp_path / "paper.tex"
    tex.write_text(_document("Original prose."), encoding="utf-8")
    candidate = _document("Changed prose.")
    service = PaperForgeService(tmp_path)
    engine = WritingEngine(
        tmp_path, memory=service.memory, request_text=lambda *a, **k: candidate
    )
    result = engine.write(
        run_id="wf-1",
        payload={"main_tex": "paper.tex", "instructions": "x"},
        model="bailu-turing",
    )
    metadata = result.artifact.to_dict()["metadata"]
    assert metadata["generation_termination"]["termination_unknown"] is True


def test_structured_wrappers_share_provider_contract(monkeypatch) -> None:
    class _Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    assert kwargs["stream"] is False
                    return _Response("OK.", "stop")

    import paperforge.provider as provider

    monkeypatch.setattr(
        provider.ProviderRegistry, "credential", lambda self, config: "k"
    )
    monkeypatch.setattr(
        provider.ProviderPreflightCache, "load", lambda self, c, k: type(
            "R", (), {"status": "EXTERNAL_SERVICE_VERIFIED"}
        )()
    )
    structured = chat_completion_structured("bailu-turing", messages=[{"role": "user", "content": "hi"}], client=_Client())
    assert structured.text == "OK."
    text = chat_completion_text("bailu-turing", messages=[{"role": "user", "content": "hi"}], client=_Client())
    assert text == "OK."
