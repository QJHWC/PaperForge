import pytest

from paperforge.grounded_narrative import (
    GroundedNarrativeSpec,
    NarrativeGroundingError,
    NarrativeSentence,
    validate_narrative,
)


@pytest.mark.parametrize("subject", ["ModelB", "ModelAB", "The model"])
def test_narrative_rejects_wrong_or_unresolved_model_subject(subject):
    fact = {"fact_id": "f", "value": "20", "qualifiers": {"model_id": "ModelA"}}
    sentence = NarrativeSentence("s", f"{subject} measured 20 ms.", "metric_report", fact_ids=("f",))
    with pytest.raises(NarrativeGroundingError, match="GROUNDING_MISMATCH model_id"):
        validate_narrative(GroundedNarrativeSpec(allowed_fact_ids=("f",)), [[sentence]], facts=[fact])


def test_narrative_accepts_correct_model_from_source_identity():
    fact = {"fact_id": "f", "value": "20", "unit": "ms", "run_identity": {"model_id": "ModelA"}}
    sentence = NarrativeSentence("s", "ModelA measured 20 ms.", "metric_report", fact_ids=("f",))
    assert validate_narrative(GroundedNarrativeSpec(allowed_fact_ids=("f",)), [[sentence]],
                              facts=[fact])["overall_status"] == "passed"


@pytest.mark.parametrize("text", ["ModelB measured 20 ms, unlike ModelA.",
                                  "ModelA was the reference; ModelB measured 20 ms."])
def test_mentioning_expected_model_does_not_bind_wrong_subject(text):
    fact = {"fact_id": "f", "value": "20", "qualifiers": {"model_id": "ModelA"}}
    sentence = NarrativeSentence("s", text, "metric_report", fact_ids=("f",))
    assert validate_narrative(GroundedNarrativeSpec(allowed_fact_ids=("f",)), [[sentence]],
                              facts=[fact])["overall_status"] == "needs_human"
