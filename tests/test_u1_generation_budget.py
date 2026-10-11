from __future__ import annotations

from paperforge.provider import GenerationBudget, get_generation_budget


def test_unknown_model_is_fail_safe() -> None:
    budget = get_generation_budget("some-unknown-openai-compatible-model")
    assert isinstance(budget, GenerationBudget)
    assert budget.context_window is None
    assert budget.max_output_tokens == 4096


def test_bailu_context_window_unknown_not_invented() -> None:
    budget = get_generation_budget("bailu-turing")
    assert budget.context_window is None
    # 4096 is output compat default, not context capability.
    assert budget.max_output_tokens == 4096
    assert budget.reserved_system_tokens > 0
    assert budget.safety_margin > 0
