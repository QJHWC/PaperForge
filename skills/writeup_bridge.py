from __future__ import annotations

from skills.runtime import available_skill_ids


def resolve_writeup_phase_skills() -> dict[str, list[str]]:
    available = set(available_skill_ids())
    phase_map = {
        "init": ["write-section", "refine-section"],
        "cite": ["citation-gap", "citation-select"],
        "refine": ["refine-section", "de-aigc-rewrite"],
        "latex_fix": ["latex-fix"],
    }
    return {
        phase: [skill_id for skill_id in skill_ids if skill_id in available]
        for phase, skill_ids in phase_map.items()
    }
