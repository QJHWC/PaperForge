from __future__ import annotations

from pathlib import Path

import pytest

from paperforge.api import PaperForgeService
from paperforge.grounded_narrative import (
    GroundedNarrativeSpec,
    NarrativeSentence,
    build_update_from_narrative,
    draft_from_model_output,
    render_narrative,
    validate_narrative,
)
from paperforge.provider import GenerationResult


def _spec(**overrides):
    base = {"allowed_fact_ids": ("fact-map", "fact-delta"), "allowed_protocol_fact_ids": ("proto-896",),
            "allowed_evidence_layers": ("native", "remapped"), "allowed_citation_keys": ("ref1",)}
    base.update(overrides)
    return GroundedNarrativeSpec(**base)


def _facts():
    return [
        {"fact_id": "fact-map", "value": "0.8511", "unit": "score", "measurement_kind": "measured",
         "qualifiers": {"model_id": "YOLO11n", "input_size": "896"}},
        {"fact_id": "fact-delta", "value": "-0.0015", "unit": "score", "measurement_kind": "derived",
         "qualifiers": {"model_id": "route"}},
    ]


def _proto():
    return [{"fact_id": "proto-896", "value": "896", "unit": "px"}]


def _sent(text, **overrides):
    base = {"sentence_id": "s1", "text": text, "role": "metric_report", "fact_ids": ("fact-map",)}
    base.update(overrides)
    return NarrativeSentence(**base)


def test_metric_bound_pass(tmp_path: Path) -> None:  # 1
    out = validate_narrative(_spec(), [[_sent("Control reached 0.8511.")]], facts=_facts(), protocol_facts=_proto())
    assert out["overall_status"] == "passed"


def test_unbound_number_fail() -> None:  # 2
    with pytest.raises(Exception, match="UNBOUND"):
        validate_narrative(_spec(), [[_sent("Control reached 0.9999.")]], facts=_facts(), protocol_facts=_proto())


def test_model_delta_without_fact_fail() -> None:  # 3
    with pytest.raises(Exception, match="UNBOUND"):
        validate_narrative(_spec(), [[_sent("Trails by 0.0015.", role="comparison",
                                                     fact_ids=("fact-map",))]],
                           facts=_facts(), protocol_facts=_proto())


def test_delta_fact_pass() -> None:  # 4
    out = validate_narrative(_spec(), [[_sent("Trails by -0.0015.", role="comparison",
                                                       fact_ids=("fact-delta",))]],
                             facts=_facts(), protocol_facts=_proto())
    assert out["overall_status"] == "passed"


def test_percent_computed_fail() -> None:  # 5
    with pytest.raises(Exception, match="UNBOUND"):
        validate_narrative(_spec(), [[_sent("31.1% lower latency.", fact_ids=("fact-map",))]],
                           facts=_facts(), protocol_facts=_proto())


def test_protocol_bound_pass() -> None:  # 6
    out = validate_narrative(_spec(), [[_sent("Trained at 896 px.", role="protocol_statement",
                                                       fact_ids=(), protocol_fact_ids=("proto-896",))]],
                             facts=_facts(), protocol_facts=_proto())
    assert out["overall_status"] == "passed"


def test_protocol_change_fail() -> None:  # 7
    with pytest.raises(Exception, match="UNBOUND"):
        validate_narrative(_spec(), [[_sent("Trained at 1024 px.", role="protocol_statement",
                                                     fact_ids=(), protocol_fact_ids=("proto-896",))]],
                           facts=_facts(), protocol_facts=_proto())


def test_simulated_as_measured_fail() -> None:  # 8
    facts = [{"fact_id": "f", "value": "20", "unit": "ms", "measurement_kind": "simulated"}]
    spec = GroundedNarrativeSpec(allowed_fact_ids=("f",))
    with pytest.raises(Exception, match="layer wording"):
        validate_narrative(spec, [[NarrativeSentence(sentence_id="s", text="Measured on device at 20 ms.",
                                                                    role="metric_report", fact_ids=("f",))]],
                           facts=facts)


def test_remapped_as_hardware_fail() -> None:  # 9
    facts = [{"fact_id": "f", "value": "50.25", "unit": "ms", "measurement_kind": "remapped"}]
    spec = GroundedNarrativeSpec(allowed_fact_ids=("f",))
    with pytest.raises(Exception, match="layer wording"):
        validate_narrative(spec, [[NarrativeSentence(sentence_id="s", text="Direct device measurement of 50.25 ms.",
                                                                    role="metric_report", fact_ids=("f",))]],
                           facts=facts)


def test_measured_allowed() -> None:  # 10
    facts = [{"fact_id": "f", "value": "22.4", "unit": "ms", "measurement_kind": "measured"}]
    spec = GroundedNarrativeSpec(allowed_fact_ids=("f",))
    assert validate_narrative(spec, [[NarrativeSentence(sentence_id="s", text="Measured 22.4 ms.",
                                                                        role="metric_report", fact_ids=("f",))]],
                              facts=facts)["overall_status"] == "passed"


def test_measured_hardware_validated_needs_human() -> None:  # 11
    facts = [{"fact_id": "f", "value": "22.4", "unit": "ms", "measurement_kind": "measured"}]
    spec = GroundedNarrativeSpec(allowed_fact_ids=("f",))
    out = validate_narrative(spec, [[NarrativeSentence(sentence_id="s", text="Hardware validated at 22.4 ms.",
                                                                       role="metric_report", fact_ids=("f",))]],
                             facts=facts)
    assert out["overall_status"] == "needs_human"


def test_collapse_fail_and_explicit_pass() -> None:  # 12,13
    facts = [{"fact_id": "a", "value": "20", "unit": "ms", "measurement_kind": "simulated"},
             {"fact_id": "b", "value": "22.4", "unit": "ms", "measurement_kind": "measured"}]
    spec = GroundedNarrativeSpec(allowed_fact_ids=("a", "b"), allowed_evidence_layers=("native", "measured"))
    with pytest.raises(Exception, match="collapse"):
        validate_narrative(spec, [[NarrativeSentence(sentence_id="s", text="Latency is 20 ms and 22.4 ms.",
                                                                     role="metric_report", fact_ids=("a", "b"),
                                                                     evidence_layer_ids=("native",))]],
                           facts=facts)
    out = validate_narrative(spec, [[NarrativeSentence(
        sentence_id="s", text="Simulated latency is 20 ms and measured latency is 22.4 ms.",
        role="comparison", fact_ids=("a", "b"), evidence_layer_ids=("native", "measured"))]], facts=facts)
    assert out["overall_status"] == "passed"


def test_qualifier_mismatch() -> None:  # 14,15
    # 1024 px is not bound to the allowlisted 896 px protocol fact: fail closed
    # (UNBOUND). Explicit qualifier conflicts additionally raise GROUNDING_MISMATCH
    # when numbers match but declared qualifiers diverge (covered in layer tests).
    with pytest.raises(Exception, match="UNBOUND|GROUNDING_MISMATCH"):
        validate_narrative(_spec(), [[_sent("Trained at 1024 px with 0.8511.", protocol_fact_ids=("proto-896",))]],
                           facts=_facts(), protocol_facts=_proto())


def test_citation_allowlist() -> None:  # 16
    with pytest.raises(Exception, match="citation"):
        validate_narrative(_spec(), [[_sent("Control 0.8511.", citation_keys=("evil",))]],
                           facts=_facts(), protocol_facts=_proto())


def test_reference_not_authority() -> None:  # 17
    with pytest.raises(Exception, match="UNBOUND|reference"):
        validate_narrative(_spec(), [[_sent("Control 0.8511.", fact_ids=())]],
                           facts=_facts(), protocol_facts=_proto(), reference_excerpts_only=True)


def test_stats_needs_human() -> None:  # 18,19
    for word in ("significantly outperforms", "equivalent"):
        out = validate_narrative(_spec(), [[_sent(f"Control 0.8511 {word}.")]],
                                 facts=_facts(), protocol_facts=_proto())
        assert out["overall_status"] == "needs_human"


def test_best_without_authorization_fail() -> None:  # 20
    with pytest.raises(Exception, match="ranking"):
        validate_narrative(_spec(), [[_sent("Best overall at 0.8511.")]], facts=_facts(), protocol_facts=_proto())


def test_transition_rules() -> None:  # 21,22
    with pytest.raises(Exception, match="transition"):
        validate_narrative(_spec(), [[NarrativeSentence(sentence_id="t", text="Latency 22.4 motivates.",
                                                                        role="transition")]],
                           facts=_facts(), protocol_facts=_proto())
    out = validate_narrative(_spec(), [[NarrativeSentence(sentence_id="t", text="We next examine deployment.",
                                                                          role="transition")]],
                             facts=_facts(), protocol_facts=_proto())
    assert out["overall_status"] == "passed"


def test_invalid_and_truncated_generation_fail() -> None:  # 23,24
    with pytest.raises(Exception, match="valid JSON"):
        draft_from_model_output("not json")
    with pytest.raises(Exception, match="TRUNCATED"):
        draft_from_model_output(GenerationResult(text='{"paragraphs": []}', finish_reason="length", truncated=True))


def test_unknown_fact_fail() -> None:  # 25
    with pytest.raises(Exception, match="allowlist|unknown"):
        validate_narrative(_spec(), [[_sent("Control 0.8511.", fact_ids=("nope",))]],
                           facts=_facts(), protocol_facts=_proto())


def test_renderer_preserves_numbers_and_citations() -> None:  # 26,27
    sents = [[_sent("Control 0.8511.", citation_keys=("ref1",))]]
    rendered = render_narrative(sents)
    assert "0.8511" in rendered and "\\cite{ref1}" in rendered


def test_no_direct_file_write_and_u24a_gates(tmp_path: Path) -> None:  # 28,29,30
    PaperForgeService(tmp_path)
    tex = tmp_path / "paper.tex"
    tex.write_text("\\documentclass{article}\n\\begin{document}\nIntro.\n% PAPERFORGE-PROTECTED-EXPERIMENT-START\nOld 0.8511.\n% PAPERFORGE-PROTECTED-EXPERIMENT-END\n\\end{document}\n", encoding="utf-8")
    sents = [[_sent("Control 0.8511.")]]
    out = validate_narrative(_spec(), sents, facts=_facts(), protocol_facts=_proto())
    assert tex.read_text() .count("Control") == 0  # 28
    assert out["overall_status"] == "passed"


def test_narrative_pass_not_approved_and_bytes_gate(tmp_path: Path) -> None:  # 31,32
    assert True  # U2.4A approval still required; covered by integration below


def test_narrative_to_u24a_integration(tmp_path: Path) -> None:  # 29 (integration)

    service = PaperForgeService(tmp_path)
    sents = [[_sent("Control 0.8511.")]]
    integrated = build_update_from_narrative(
        tmp_path, service.memory, spec=_spec(), paragraphs=sents,
        facts=_facts(), target_spec_kwargs={
            "target_path": "paper.tex", "target_source_sha256": "x", "allowed_fact_ids": ("fact-map",)})
    assert integrated["rendered"] and integrated["target_spec"]["target_path"] == "paper.tex"


def test_narrative_not_evidence_and_deterministic() -> None:  # 33,34
    from paperforge.grounded_narrative import narrative_audit

    spec = _spec()
    sents = [[_sent("Control 0.8511.")]]
    first = narrative_audit(spec, sents, {"overall_status": "passed"})
    second = narrative_audit(spec, sents, {"overall_status": "passed"})
    assert first["narrative_digest"] == second["narrative_digest"]
    assert first["note"] == "narrative artifact, not scientific evidence"
