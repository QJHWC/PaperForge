import json
import os
import os.path as osp
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from engine.generate_ideas import search_for_papers

RUN_DIR_PATTERN = re.compile(r"run_(\d+)$")
FIGURE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".svg"}
DEFAULT_TEMPLATE_REQUIRED_FILES = [
    "experiment.py",
    "plot.py",
    "latex/template.tex",
]


def slugify(text: str, default: str = "project") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return slug or default


def validate_template_integrity(
    experiment: str,
    required_files: Optional[List[str]] = None,
) -> str:
    template_dir = osp.join("templates", experiment)
    if not osp.isdir(template_dir):
        raise FileNotFoundError(f"Template not found: {template_dir}")

    requirements = list(required_files or DEFAULT_TEMPLATE_REQUIRED_FILES)
    missing = [rel for rel in requirements if not osp.exists(osp.join(template_dir, rel))]
    if missing:
        missing_text = ", ".join(missing)
        raise FileNotFoundError(
            f"Template `{experiment}` is missing required files: {missing_text}\n"
            f"Please restore them under `{template_dir}` "
            f"or switch template with `--experiment paper_writer`."
        )
    return template_dir


def create_workspace_from_template(experiment: str, idea_name: str) -> str:
    template_dir = validate_template_integrity(experiment)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_name = f"{timestamp}_{slugify(idea_name, default=experiment)}"
    workspace = osp.join("results", experiment, run_name)
    if osp.exists(workspace):
        raise FileExistsError(f"Workspace already exists: {workspace}")

    os.makedirs(osp.dirname(workspace), exist_ok=True)
    shutil.copytree(template_dir, workspace)
    return workspace


def load_baseline_results(workspace: str) -> Dict:
    baseline_path = osp.join(workspace, "run_0", "final_info.json")
    if not osp.exists(baseline_path):
        return {}

    with open(baseline_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        collapsed = {}
        for dataset_name, dataset_obj in data.items():
            means = dataset_obj.get("means", {}) if isinstance(dataset_obj, dict) else {}
            collapsed[dataset_name] = means
        return collapsed
    return {}


def write_idea_metadata(workspace: str, idea_name: str, title: str, description: str) -> None:
    payload = {
        "Name": slugify(idea_name, default="idea"),
        "Title": title,
        "Experiment": description,
    }
    path = osp.join(workspace, "workflow_idea.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_idea_metadata(workspace: str) -> Dict:
    path = osp.join(workspace, "workflow_idea.json")
    if osp.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
            payload.setdefault("Name", slugify(osp.basename(workspace), default="idea"))
            payload.setdefault("Title", payload.get("Name", "Untitled"))
            payload.setdefault("Experiment", "")
            return payload

    return {
        "Name": slugify(osp.basename(workspace), default="idea"),
        "Title": osp.basename(workspace),
        "Experiment": "",
    }


def initialize_notes(workspace: str, title: str, description: str, overwrite: bool = False) -> str:
    notes_path = osp.join(workspace, "notes.txt")
    if osp.exists(notes_path) and not overwrite:
        return notes_path

    baseline = load_baseline_results(workspace)
    content = [
        f"# Title: {title}",
        f"# Experiment description: {description}",
        "",
        "## Workflow mode",
        "This run uses staged generation: draft -> MVP -> upload feedback -> optimization -> refine.",
        "",
        "## Run 0: Baseline",
        f"Results: {baseline}",
        "Description: Baseline from template.",
        "",
        "## Authoring policy",
        "- Early stage: keep draft concise, avoid heavy iterative refinement.",
        "- Only after MVP and upload feedback, run optimization and deeper writeup passes.",
        "",
    ]
    with open(notes_path, "w", encoding="utf-8") as f:
        f.write("\n".join(content))
    return notes_path


def ensure_upload_interface(workspace: str) -> Dict[str, str]:
    uploads_dir = osp.join(workspace, "uploads")
    code_dir = osp.join(uploads_dir, "code")
    figures_dir = osp.join(uploads_dir, "figures")
    artifacts_dir = osp.join(workspace, "artifacts")
    os.makedirs(code_dir, exist_ok=True)
    os.makedirs(figures_dir, exist_ok=True)
    os.makedirs(artifacts_dir, exist_ok=True)

    readme_path = osp.join(uploads_dir, "README_UPLOAD.md")
    if not osp.exists(readme_path):
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(
                "# Upload Interface\n\n"
                "Drop files before continuing writeup:\n"
                "- `uploads/code/`: revised experiment code files\n"
                "- `uploads/figures/`: screenshots/figures for paper\n"
                "- `uploads/notes.md`: optional plain-language notes\n\n"
                "After upload, run phase `feedback` to ingest and reflect them into notes/writeup.\n"
            )

    notes_md = osp.join(uploads_dir, "notes.md")
    if not osp.exists(notes_md):
        with open(notes_md, "w", encoding="utf-8") as f:
            f.write("# User Upload Notes\n\n- Add key observations, caveats, and figure explanations.\n")

    return {
        "uploads_dir": uploads_dir,
        "code_dir": code_dir,
        "figures_dir": figures_dir,
        "notes_md": notes_md,
    }


def _upsert_notes_block(notes_path: str, block_key: str, body: str) -> None:
    start = f"<!-- AUTO:{block_key}:START -->"
    end = f"<!-- AUTO:{block_key}:END -->"
    block = f"{start}\n{body.strip()}\n{end}"

    if osp.exists(notes_path):
        with open(notes_path, "r", encoding="utf-8") as f:
            text = f.read()
    else:
        text = ""

    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if pattern.search(text):
        text = pattern.sub(block, text)
    else:
        text = (text.rstrip() + "\n\n" + block + "\n").strip() + "\n"

    with open(notes_path, "w", encoding="utf-8") as f:
        f.write(text)


def list_run_indices(workspace: str) -> List[int]:
    indices: List[int] = []
    for entry in os.listdir(workspace):
        match = RUN_DIR_PATTERN.fullmatch(entry)
        if match:
            indices.append(int(match.group(1)))
    return sorted(indices)


def next_run_index(workspace: str) -> int:
    indices = list_run_indices(workspace)
    if not indices:
        return 1
    return max(indices) + 1


def run_python_command(workspace: str, args: List[str], timeout: int = 7200) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=workspace,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def run_experiment_once(workspace: str, python_bin: str = "python", run_index: Optional[int] = None) -> Dict:
    if run_index is None:
        run_index = next_run_index(workspace)
    out_dir = f"run_{run_index}"
    result = run_python_command(workspace, [python_bin, "experiment.py", f"--out_dir={out_dir}"])

    return {
        "run_index": run_index,
        "out_dir": out_dir,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def run_plotting(workspace: str, python_bin: str = "python") -> Dict:
    result = run_python_command(workspace, [python_bin, "plot.py"], timeout=1200)
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def collect_run_summaries(workspace: str) -> List[Dict]:
    summaries: List[Dict] = []
    for idx in list_run_indices(workspace):
        path = osp.join(workspace, f"run_{idx}", "final_info.json")
        if not osp.exists(path):
            continue

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            dataset_name, dataset_obj = next(iter(data.items()))
            means = dataset_obj.get("means", {})
            if not isinstance(means, dict):
                means = {}
        except Exception:
            continue

        metric_keys = sorted(
            [
                k
                for k, v in means.items()
                if isinstance(v, (int, float)) and k != "total_train_time_mean"
            ]
        )
        metrics = {k: means[k] for k in metric_keys}

        summaries.append(
            {
                "run": idx,
                "dataset": dataset_name,
                "metrics": metrics,
                "total_train_time_mean": means.get("total_train_time_mean"),
            }
        )
    return summaries


def _fmt(value: Optional[float], digits: int = 4) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return "-"


def format_run_summary_markdown(summaries: List[Dict]) -> str:
    if not summaries:
        return "## Experiment Feedback\nNo run summary found yet."

    preferred_cols = [
        "overall_score_mean",
        "draft_quality_mean",
        "citation_coverage_mean",
        "coherence_mean",
        "readability_mean",
        "revision_efficiency_mean",
    ]
    discovered_cols: List[str] = []
    discovered_set = set()
    for row in summaries:
        for key in row.get("metrics", {}).keys():
            if key not in discovered_set:
                discovered_set.add(key)
                discovered_cols.append(key)

    metric_cols = [k for k in preferred_cols if k in discovered_set]
    metric_cols.extend([k for k in discovered_cols if k not in metric_cols])
    metric_cols = metric_cols[:8]

    lines = [
        "## Experiment Feedback",
        "| Run | Dataset | "
        + (" | ".join(metric_cols) if metric_cols else "No numeric metrics")
        + " |",
        "| --- | --- | "
        + (" | ".join(["---"] * len(metric_cols)) if metric_cols else "---")
        + " |",
    ]

    for row in sorted(summaries, key=lambda x: x["run"]):
        metric_values = [
            _fmt(row.get("metrics", {}).get(col), digits=4) for col in metric_cols
        ]
        lines.append(
            "| run_{run} | {dataset} | {metrics} |".format(
                run=row["run"],
                dataset=row.get("dataset", "-"),
                metrics=" | ".join(metric_values) if metric_values else "-",
            ),
        )

    if metric_cols:
        primary = metric_cols[0]
        best = max(
            [x for x in summaries if x.get("metrics", {}).get(primary) is not None],
            key=lambda x: x["metrics"][primary],
            default=None,
        )
        if best is not None:
            lines.append("")
            lines.append(
                f"- Best `{primary}`: run_{best['run']} ({_fmt(best['metrics'].get(primary), digits=4)})."
            )

    return "\n".join(lines)


def refresh_notes_with_run_feedback(workspace: str, notes_path: str) -> List[Dict]:
    summaries = collect_run_summaries(workspace)
    block = format_run_summary_markdown(summaries)
    _upsert_notes_block(notes_path, "RUN_FEEDBACK", block)
    return summaries


def refresh_notes_with_literature(
    notes_path: str,
    query: str,
    engine: str = "openalex",
    top_k: int = 5,
) -> None:
    try:
        papers = search_for_papers(query=query, result_limit=top_k, engine=engine) or []
    except Exception as exc:
        block = f"## Literature Snapshot\nQuery: `{query}`\nSearch failed: {exc}"
        _upsert_notes_block(notes_path, "LITERATURE", block)
        return

    if not papers:
        block = f"## Literature Snapshot\nQuery: `{query}`\nNo papers found."
        _upsert_notes_block(notes_path, "LITERATURE", block)
        return

    lines = ["## Literature Snapshot", f"Query: `{query}`", ""]
    for i, paper in enumerate(papers, start=1):
        title = paper.get("title", "Untitled")
        year = paper.get("year", "?")
        venue = paper.get("venue", "Unknown")
        authors = paper.get("authors", "Unknown")
        lines.append(f"{i}. {title} ({year}, {venue}) - {authors}")
    _upsert_notes_block(notes_path, "LITERATURE", "\n".join(lines))


def _copy_file_unique(src: Path, dst_dir: Path, preferred_name: Optional[str] = None) -> str:
    dst_dir.mkdir(parents=True, exist_ok=True)
    if preferred_name:
        base_name = preferred_name
    else:
        base_name = src.name

    stem = Path(base_name).stem
    suffix = Path(base_name).suffix
    candidate = dst_dir / base_name
    counter = 1
    while candidate.exists():
        candidate = dst_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    shutil.copy2(src, candidate)
    return candidate.name


def ingest_user_uploads(workspace: str) -> Dict:
    ensure_upload_interface(workspace)
    uploads_dir = Path(workspace) / "uploads"
    code_dir = uploads_dir / "code"
    fig_dir = uploads_dir / "figures"
    notes_md = uploads_dir / "notes.md"

    artifacts_code = Path(workspace) / "artifacts" / "user_code"
    artifacts_figs = Path(workspace) / "artifacts" / "user_figures"

    code_files: List[str] = []
    figure_files: List[str] = []
    paper_figure_files: List[str] = []

    for file_path in code_dir.rglob("*"):
        if file_path.is_file():
            rel = file_path.relative_to(code_dir)
            dst_path = artifacts_code / rel
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, dst_path)
            code_files.append(str(rel))

    for file_path in fig_dir.rglob("*"):
        if not file_path.is_file() or file_path.suffix.lower() not in FIGURE_EXTENSIONS:
            continue
        rel = file_path.relative_to(fig_dir)
        dst_path = artifacts_figs / rel
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, dst_path)
        figure_files.append(str(rel))

        safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", file_path.name)
        preferred_name = f"upload_{safe_name}"
        copied_name = _copy_file_unique(file_path, Path(workspace), preferred_name=preferred_name)
        paper_figure_files.append(copied_name)

    user_notes = ""
    if notes_md.exists():
        user_notes = notes_md.read_text(encoding="utf-8").strip()

    manifest = {
        "timestamp": datetime.now().isoformat(),
        "code_files": sorted(code_files),
        "figure_files": sorted(figure_files),
        "paper_figure_files": sorted(paper_figure_files),
        "user_notes": user_notes,
    }

    manifest_path = Path(workspace) / "artifacts" / "upload_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def append_upload_feedback_to_notes(notes_path: str, manifest: Dict) -> None:
    lines = ["## User Upload Feedback"]
    code_files = manifest.get("code_files", [])
    figure_files = manifest.get("figure_files", [])
    paper_figure_files = manifest.get("paper_figure_files", [])

    lines.append(f"- Uploaded code files: {len(code_files)}")
    for path in code_files:
        lines.append(f"  - {path}")

    lines.append(f"- Uploaded figure files: {len(figure_files)}")
    for path in figure_files:
        lines.append(f"  - {path}")

    if paper_figure_files:
        lines.append("- Figure filenames available for LaTeX includegraphics:")
        for fname in paper_figure_files:
            lines.append(f"  - {fname}")

    user_notes = (manifest.get("user_notes") or "").strip()
    if user_notes:
        lines.append("")
        lines.append("### User notes")
        lines.append(user_notes)

    _upsert_notes_block(notes_path, "UPLOAD_FEEDBACK", "\n".join(lines))


def save_workflow_state(workspace: str, state: Dict) -> None:
    path = osp.join(workspace, "workflow_state.json")
    payload = {"updated_at": datetime.now().isoformat(), **state}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_workflow_state(workspace: str) -> Dict:
    path = osp.join(workspace, "workflow_state.json")
    if not osp.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
