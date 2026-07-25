from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def verify_wheel(wheel: Path) -> None:
    resolved = wheel.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)

    with tempfile.TemporaryDirectory(prefix="paperforge-wheel-smoke-") as temp:
        root = Path(temp)
        target = root / "site-packages"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-deps",
                "--target",
                str(target),
                str(resolved),
            ],
            check=True,
        )
        smoke = """
import sys
sys.path.insert(0, sys.argv[1])

from importlib.metadata import files
from pathlib import Path
from agents.scientist_workflow_agent import ScientistWorkflowAgent
from agents.writeup_agent import WriteupAgent
from engine.perform_review import get_review_fewshot_examples
from engine.research_partner.rubric_loader import load_rubric_profile

entries = {str(path) for path in files("paperforge-research-os") or ()}
required = {
    "agents/schemas/coordinator.schema.json",
    "engine/fewshot_examples/132_automated_relational.json",
    "engine/fewshot_examples/132_automated_relational.pdf",
    "engine/fewshot_examples/132_automated_relational.txt",
    "engine/research_partner/rubrics/cvpr.yaml",
    "frontend/app.js",
    "frontend/index.html",
    "skills/write-section/SKILL.md",
    "skills/write-section/schema.json",
    "templates/paper_writer/latex/template.tex",
}
missing = sorted(required - entries)
if missing:
    raise SystemExit(f"wheel is missing package data: {missing}")
if load_rubric_profile("cvpr").name != "cvpr":
    raise SystemExit("packaged rubric smoke failed")
if "Paper:" not in get_review_fewshot_examples(1):
    raise SystemExit("packaged few-shot smoke failed")
writeup_command = WriteupAgent().build_command(
    WriteupAgent().build_config(workflow_kind="mvp")
)
scientist_command = ScientistWorkflowAgent().build_command(
    ScientistWorkflowAgent().build_config()
)
for command in (writeup_command, scientist_command):
    if not Path(command[1]).is_file():
        raise SystemExit(f"packaged launcher is missing: {command[1]}")
"""
        clean_env = dict(os.environ)
        clean_env.pop("PYTHONPATH", None)
        subprocess.run(
            [sys.executable, "-I", "-c", smoke, str(target)],
            cwd=root,
            env=clean_env,
            check=True,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args(argv)
    verify_wheel(args.wheel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
