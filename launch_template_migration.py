from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.template_migration import run_template_migration


def _require_virtualenv(script_name: str) -> None:
    import os

    allow_system = os.getenv("PAPERFORGE_ALLOW_SYSTEM_PYTHON", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    in_virtualenv = bool(getattr(sys, "base_prefix", sys.prefix) != sys.prefix)
    if allow_system or in_virtualenv:
        return
    raise SystemExit(
        "环境错误: 检测到正在使用系统 Python 运行 PaperForge。\n"
        f"当前解释器: {sys.executable}\n"
        f"请先激活虚拟环境后再运行，例如:\n  source .venv311/bin/activate && python {script_name} --help\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run PaperForge template migration")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--source-draft-id", required=True)
    parser.add_argument("--template-id", required=True)
    parser.add_argument("--output-name", required=True)
    return parser


def main() -> None:
    _require_virtualenv("launch_template_migration.py")
    args = build_parser().parse_args()
    result = run_template_migration(
        workspace=args.workspace,
        source_draft_id=args.source_draft_id,
        template_id=args.template_id,
        output_name=args.output_name,
    )
    print(f"[migration] workspace={Path(args.workspace).expanduser().resolve()}")
    print(f"[migration] id={result['migration_id']}")
    print(f"[done] workspace={Path(args.workspace).expanduser().resolve()}")


if __name__ == "__main__":
    main()
