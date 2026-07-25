#!/usr/bin/env python3
"""Rank literature sources by publication quality and venue preference."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

from arxiv_registry import connect, ensure_initialized, init_schema  # noqa: E402
from source_policy_utils import assess_work, ensure_policy_schema, resolve_work_ids  # noqa: E402
from paper_utils import load_paper_config  # noqa: E402


def cmd_rank(args: argparse.Namespace) -> int:
    project_dir = args.project_dir
    config = load_paper_config(project_dir) if project_dir else {"preferred_venues": [], "target_venue": ""}

    with connect(args.db) as conn:
        init_schema(conn)
        ensure_initialized(conn)
        ensure_policy_schema(conn)

        work_ids = resolve_work_ids(
            conn,
            arxiv_ids=args.arxiv_id,
            search_id=args.search_id,
        )
        if not work_ids:
            print("warning: no works available to rank", file=sys.stderr)
            return 0

        ranked_rows: list[dict] = []
        for work_id in work_ids[: args.limit if args.limit > 0 else None]:
            ranked_rows.append(
                assess_work(
                    conn,
                    work_id=work_id,
                    config=config,
                    timeout_s=args.timeout_s,
                    refresh=args.refresh,
                )
            )

        ranked_rows.sort(key=lambda row: (int(row["quality_score"]), row["source_tier"]), reverse=True)
        print(f"Ranked {len(ranked_rows)} works.")
        for row in ranked_rows[: args.print_count]:
            print(
                f"- work_id={row['work_id']} tier={row['source_tier']} score={row['quality_score']} "
                f"venue={row['canonical_venue'] or 'N/A'} reason={row['assessment_reason']}"
            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rank literature sources by quality tier.")
    parser.add_argument(
        "--project-dir",
        help="Paper/project directory containing paper.config.yaml and notes/arxiv-registry.sqlite3.",
    )
    parser.add_argument(
        "--db",
        help="Registry sqlite path. Defaults to <project-dir>/notes/arxiv-registry.sqlite3.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_rank = sub.add_parser("rank", help="Enrich metadata and assess source quality.")
    p_rank.add_argument("--arxiv-id", nargs="*", default=[], help="Specific arXiv IDs to assess.")
    p_rank.add_argument("--search-id", type=int, help="Assess all works attached to a stored search.")
    p_rank.add_argument("--limit", type=int, default=0, help="Maximum works to assess; 0 means all.")
    p_rank.add_argument("--print-count", type=int, default=10, help="Number of ranked rows to print.")
    p_rank.add_argument("--timeout-s", type=int, default=20, help="Network timeout in seconds.")
    p_rank.add_argument("--refresh", action="store_true", help="Refresh external metadata even if cached.")
    p_rank.set_defaults(fn=cmd_rank)
    return parser


def resolve_db_arg(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db).expanduser().resolve()
    if not args.project_dir:
        return Path("notes/arxiv-registry.sqlite3").resolve()
    return (args.project_dir / "notes" / "arxiv-registry.sqlite3").resolve()


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.project_dir:
        args.project_dir = Path(args.project_dir).expanduser().resolve()
    args.db = resolve_db_arg(args)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
