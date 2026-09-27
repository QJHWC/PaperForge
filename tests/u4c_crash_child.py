"""Subprocess-only fault injector. Never imported by production code."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paperforge.api import PaperForgeService


def crash_point():
    print("U4C_READY_TO_KILL", flush=True)
    while True:
        time.sleep(60)


def main():
    root, mode, identity = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    service = PaperForgeService(root)
    if mode.startswith("import"):
        import paperforge.external_import as module

        original = module._insert_evidence_row
        if mode == "import_precommit":
            def insert(*args, **kwargs):
                result = original(*args, **kwargs)
                crash_point()
                return result
            module._insert_evidence_row = insert
        module.materialize_import(root, service.memory, identity)
        crash_point()
    elif mode.startswith("result"):
        import paperforge.result_update as module

        original = module.atomic_write_text
        def write(workspace, relative, content, **kwargs):
            if str(relative) == "paper.tex" and mode == "result_pre_replace":
                crash_point()
            result = original(workspace, relative, content, **kwargs)
            if str(relative) == "paper.tex":
                crash_point()
            return result
        module.atomic_write_text = write
        module.apply_proposal(root, service.memory, identity)
    else:
        import paperforge.release_eligibility as module

        params = json.loads((root / "test-target.json").read_bytes())
        target = module.ReleaseTarget(**params)
        policy = module.ReleasePolicy(required_gates=("source_integrity",))
        if mode == "evaluator":
            original = module._snapshot
            def snapshot(*args, **kwargs):
                crash_point()
                return original(*args, **kwargs)
            module._snapshot = snapshot
        report = module.evaluate_release(root, service.memory, target, policy)
        if mode == "release_registry":
            from paperforge.artifacts import ArtifactStore
            original_register = ArtifactStore.register
            def register(self, *args, **kwargs):
                crash_point()
                return original_register(self, *args, **kwargs)
            ArtifactStore.register = register
            module.freeze_assessment(root, service.memory, report)


if __name__ == "__main__":
    main()
