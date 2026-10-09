import pytest
from test_u24a_finalization import _fact, _spec_with_table

from paperforge.result_update import (
    ResultUpdateApprovalError,
    ResultUpdateValidationError,
    apply_proposal,
    approve_proposal,
    build_proposal,
)


@pytest.mark.parametrize("phase", ["approve", "apply"])
def test_missing_bound_artifact_member_refuses_update(tmp_path, phase):
    service, spec, old, table = _spec_with_table(tmp_path, "Old result 20.0 ms.")
    prop = build_proposal(tmp_path, service.memory, spec,
                          proposed_block=old.replace("20.0", "22.4"), facts_used=[_fact()])
    if phase == "apply":
        approve_proposal(tmp_path, service.memory, prop["proposal_id"], approver="fixture-owner")
    source = (tmp_path / "paper.tex").read_bytes()
    (tmp_path / "artifacts/tables" / table / "table.tex").unlink()
    with pytest.raises(ResultUpdateApprovalError, match="bound artifact"):
        if phase == "approve":
            approve_proposal(tmp_path, service.memory, prop["proposal_id"], approver="fixture-owner")
        else:
            apply_proposal(tmp_path, service.memory, prop["proposal_id"])
    assert (tmp_path / "paper.tex").read_bytes() == source


@pytest.mark.parametrize("claim,error", [
    ("Latency was 20.0 ms.", "UNBOUND_SCIENTIFIC_VALUE"),
    ("Training used 300 epochs.", "UNBOUND_PROTOCOL_VALUE"),
])
def test_duplicated_old_value_requires_new_binding(tmp_path, claim, error):
    service, spec, old, _ = _spec_with_table(tmp_path, claim)
    with pytest.raises(ResultUpdateValidationError, match=error):
        build_proposal(tmp_path, service.memory, spec,
                       proposed_block=old.replace(claim, claim + " Another device: " + claim), facts_used=[])
