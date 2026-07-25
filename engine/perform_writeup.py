import argparse
import json
import os
import os.path as osp
import re
import runpy
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from engine.generate_ideas import search_for_papers
from engine.llm import (
    AVAILABLE_LLMS,
    create_client,
    extract_json_between_markers,
    gateway_profile_env_overrides,
    get_response_from_llm,
)
from engine.run_lock import run_lock
from paperforge.protected_blocks import (
    ProtectedCoder,
    ProtectedEditTransaction,
    extract_protected_blocks,
)

ORIGINAL_NUM_CITE_ROUNDS = 20
ORIGINAL_NUM_ERROR_CORRECTIONS = 5
ORIGINAL_SECOND_REFINEMENT_ENABLED = True

PAPERFORGE_DEFAULT_NUM_CITE_ROUNDS = 3
PAPERFORGE_DEFAULT_NUM_ERROR_CORRECTIONS = 2
PAPERFORGE_DEFAULT_SECOND_REFINEMENT_ENABLED = False
WRITEUP_CHECKPOINT_FILENAME = "writeup_checkpoint.json"
WRITEUP_CHECKPOINT_STAGE_ORDER = {
    "start": 0,
    "init": 1,
    "cite": 2,
    "refine": 3,
    "latex_fix": 4,
    "done": 5,
}
WRITEUP_CHECKPOINT_STAGE_DEFAULT = "start"


def _atomic_write_text(path: str, content: str) -> None:
    tmp_path = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    finally:
        try:
            if osp.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _checkpoint_path(folder_name: str) -> str:
    return osp.join(folder_name, WRITEUP_CHECKPOINT_FILENAME)


def _checkpoint_snapshot_dir(folder_name: str) -> str:
    return osp.join(folder_name, "latex", "checkpoints")


def _default_writeup_checkpoint() -> dict:
    return {
        "stage": WRITEUP_CHECKPOINT_STAGE_DEFAULT,
        "current_round": 0,
        "latest_tex_file": None,
        "updated_at": None,
    }


def _normalize_checkpoint_stage(stage: object) -> str:
    if isinstance(stage, str) and stage in WRITEUP_CHECKPOINT_STAGE_ORDER:
        return stage
    return WRITEUP_CHECKPOINT_STAGE_DEFAULT


def _checkpoint_stage_rank(stage: str) -> int:
    return WRITEUP_CHECKPOINT_STAGE_ORDER.get(stage, 0)


def _load_writeup_checkpoint(folder_name: str) -> dict:
    path = _checkpoint_path(folder_name)
    if not osp.exists(path):
        return _default_writeup_checkpoint()
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        print(f"[writeup][checkpoint] invalid checkpoint ignored: {exc}")
        return _default_writeup_checkpoint()

    state = _default_writeup_checkpoint()
    if isinstance(payload, dict):
        state["stage"] = _normalize_checkpoint_stage(payload.get("stage"))
        current_round = payload.get("current_round")
        if isinstance(current_round, int) and current_round >= 0:
            state["current_round"] = current_round
        latest_tex_file = payload.get("latest_tex_file")
        if isinstance(latest_tex_file, str) and latest_tex_file.strip():
            state["latest_tex_file"] = latest_tex_file.strip()
        updated_at = payload.get("updated_at")
        if isinstance(updated_at, str) and updated_at.strip():
            state["updated_at"] = updated_at.strip()
    return state


def _save_writeup_checkpoint(
    folder_name: str,
    stage: str,
    current_round: int,
    writeup_tex_file: str,
    snapshot_name: str,
) -> dict:
    normalized_stage = _normalize_checkpoint_stage(stage)
    normalized_round = max(0, int(current_round))
    snapshot_dir = _checkpoint_snapshot_dir(folder_name)
    os.makedirs(snapshot_dir, exist_ok=True)
    snapshot_path = osp.join(snapshot_dir, snapshot_name)
    shutil.copy2(writeup_tex_file, snapshot_path)
    relative_snapshot = osp.relpath(snapshot_path, folder_name)
    payload = {
        "stage": normalized_stage,
        "current_round": normalized_round,
        "latest_tex_file": relative_snapshot,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _atomic_write_text(
        _checkpoint_path(folder_name),
        json.dumps(payload, ensure_ascii=False, indent=2),
    )
    return payload


def _restore_writeup_tex_from_checkpoint(folder_name: str, writeup_tex_file: str, state: dict) -> bool:
    latest_tex_file = state.get("latest_tex_file")
    if not isinstance(latest_tex_file, str) or not latest_tex_file.strip():
        return False
    snapshot_path = osp.join(folder_name, latest_tex_file)
    if not osp.exists(snapshot_path):
        print(f"[writeup][checkpoint] snapshot missing: {snapshot_path}")
        return False
    shutil.copy2(snapshot_path, writeup_tex_file)
    return True


def _remove_writeup_checkpoint(folder_name: str) -> None:
    path = _checkpoint_path(folder_name)
    if osp.exists(path):
        os.remove(path)


def _env_bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _deprecated_env_override(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    print(f"[deprecated] {name} is deprecated; use CLI arguments or workspace_config.json instead.")
    return raw


PRACTICAL_PROMPT_KEYS = [
    "论文评审专家",
    "写英文摘要",
    "SCI论文润色",
    "润色英文段落结构和句子逻辑",
    "语法检查/查找语法错误",
    "直接润色段落",
    "逻辑论证辅助",
]


def _candidate_prompt_library_paths() -> list[str]:
    paths: list[str] = []

    env_path = os.getenv("PAPERFORGE_PROMPT_LIBRARY_PATH", "").strip()
    if env_path:
        paths.append(osp.abspath(osp.expanduser(env_path)))

    # Default prompt library location inside project root.
    paths.append(osp.abspath(osp.join(osp.dirname(__file__), "..", "prompt_library.py")))
    # Backward-compatible fallback for legacy filename.
    paths.append(osp.abspath(osp.join(osp.dirname(__file__), "..", "提示词.py")))

    deduped: list[str] = []
    seen = set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            deduped.append(path)
    return deduped


def _one_line(text: str, max_chars: int = 220) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            line = re.sub(r"\s+", " ", line)
            return line[:max_chars]
    return ""


def _tokenize_theme_text(text: str) -> set[str]:
    if not text:
        return set()
    out = set()
    # English-style tokens
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_+\-]{2,}", text):
        out.add(token.lower())

    # Chinese tokens: keep short chunks and add 2/3-gram slices for robust matching.
    for seq in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(seq) >= 2:
            if len(seq) <= 4:
                out.add(seq)
            for n in (2, 3):
                if len(seq) >= n:
                    for i in range(len(seq) - n + 1):
                        out.add(seq[i : i + n])
    return out


def _extract_theme_text(idea: dict) -> str:
    if not isinstance(idea, dict):
        return ""
    fields: list[str] = []
    for key in ("Title", "Experiment", "Name", "Topic", "Keywords"):
        value = idea.get(key)
        if isinstance(value, str) and value.strip():
            fields.append(value.strip())
    return "\n".join(fields)


def _select_theme_matched_prompt_cues(
    prompt_library: dict,
    theme_text: str,
    top_k: int = 5,
) -> list[str]:
    if not isinstance(prompt_library, dict):
        return []
    theme_tokens = _tokenize_theme_text(theme_text)
    if not theme_tokens:
        return []

    scored: list[tuple[int, str, str]] = []
    for key, item in prompt_library.items():
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        item_tokens = _tokenize_theme_text(f"{key}\n{content[:1200]}")
        overlap = len(theme_tokens.intersection(item_tokens))
        if overlap <= 0:
            continue
        cue = _one_line(content)
        if cue:
            scored.append((overlap, str(key), cue))

    scored.sort(key=lambda x: (-x[0], x[1]))
    selected: list[str] = []
    for overlap, key, cue in scored[:top_k]:
        selected.append(f"- {key} (match={overlap}): {cue}")
    return selected


def _load_external_prompt_library() -> dict:
    for prompt_file in _candidate_prompt_library_paths():
        if not osp.exists(prompt_file):
            continue
        try:
            namespace = runpy.run_path(prompt_file)
            get_prompts_content = namespace.get("get_prompts_content")
            if callable(get_prompts_content):
                data = get_prompts_content()
                if isinstance(data, dict):
                    print(f"[INFO] Loaded prompt library: {prompt_file}")
                    return data
        except Exception as exc:
            print(f"[WARN] Failed to load external prompt library `{prompt_file}`: {exc}")
    return {}


def _build_style_guidelines(theme_text: str = "") -> str:
    lines = [
        "Writing policy (must follow):",
        "- Use neutral, evidence-based academic language.",
        "- Avoid self-referential AI wording (e.g., generated by AI, as an AI model).",
        "- Do not mention the writing process, prompt design, or model/tool behavior in the manuscript body.",
        "- Replace generic claims with concrete numbers, settings, and observed results from notes/logs.",
        "- Anchor each quantitative claim to explicit evidence (run index, table row, figure filename, or logged metric).",
        "- State limitations, failure cases, and deployment constraints explicitly.",
        "- Keep sentences concise and avoid hype words (e.g., groundbreaking, revolutionary).",
        "- Avoid repetitive template phrases and repeated sentence openers across adjacent paragraphs.",
        "- Prefer domain-specific wording over generic filler (e.g., robust framework, seamless integration).",
    ]

    prompt_library = _load_external_prompt_library()
    selected = []
    for key in PRACTICAL_PROMPT_KEYS:
        item = prompt_library.get(key, {})
        if isinstance(item, dict):
            content = _one_line(str(item.get("content", "")).strip())
            if content:
                selected.append(f"- {key}: {content}")

    if selected:
        lines.append("Useful prompt cues from prompt_library.py:")
        lines.extend(selected)

    if theme_text:
        lines.append(f"Theme signal: {_one_line(theme_text, max_chars=180)}")
        theme_cues = _select_theme_matched_prompt_cues(
            prompt_library=prompt_library,
            theme_text=theme_text,
            top_k=5,
        )
        if theme_cues:
            lines.append("Theme-matched prompt cues:")
            lines.extend(theme_cues)

    return "\n".join(lines)


def _append_style(prompt: str, style_guidelines: str) -> str:
    return f"{prompt}\n\n{style_guidelines}\n"


def _blocked_citation_keys() -> set[str]:
    raw = os.getenv("WRITEUP_BLOCKED_CITATION_KEYS", "").strip()
    if not raw:
        return set()
    return {key.strip() for key in raw.split(",") if key.strip()}


def _extract_bibtex_key(bibtex: str) -> str | None:
    match = re.search(r"@\w+\s*{\s*([^,\s]+)\s*,", bibtex)
    if match is None:
        return None
    return match.group(1).strip()


def _sanitize_author_block(tex_text: str) -> str:
    """Normalize author / header lines that LLMs sometimes inject."""
    direct_replacements = {
        r"\lhead{Research Preprint}": r"\lhead{Research Preprint}",
        r"\author{GPT-4o \& Claude\\": r"\author{Anonymous Authors\\",
        r"\author{LLM\\": r"\author{Anonymous Authors\\",
        r"Department of Computer Science\\": "",
        r"University of LLMs\\": r"Affiliation withheld for review\\",
    }
    for src, dst in direct_replacements.items():
        tex_text = tex_text.replace(src, dst)

    tex_text = re.sub(
        r"\n?This work was generated by\s*\\textsc\{[^}]+\}(?:\s*\\citep\{[^}]+\})?\.?\n?",
        "\n",
        tex_text,
        flags=re.IGNORECASE,
    )
    tex_text = re.sub(
        r"(Affiliation withheld for review\\\\)\s*\n\s*(Affiliation withheld for review\\\\)",
        r"\1",
        tex_text,
    )
    return tex_text


def _sanitize_template_tex_contents(tex_text: str) -> str:
    tex_text = _sanitize_author_block(tex_text)
    blocked_keys = _blocked_citation_keys()
    if not blocked_keys:
        return tex_text

    for key in blocked_keys:
        tex_text = re.sub(
            rf"@\w+\s*\{{\s*{re.escape(key)}\s*,.*?\n\}}\s*",
            "",
            tex_text,
            flags=re.DOTALL,
        )

    def _rewrite_cites(match: re.Match) -> str:
        command = match.group(1)
        keys = [key.strip() for key in match.group(2).split(",")]
        keys = [key for key in keys if key and key not in blocked_keys]
        if not keys:
            return ""
        return f"{command}{{{', '.join(keys)}}}"

    return re.sub(r"(\\cite[a-zA-Z*]*)\{([^}]*)\}", _rewrite_cites, tex_text)


def _sanitize_template_tex_file(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        tex_text = f.read()
    sanitized = _sanitize_template_tex_contents(tex_text)
    if sanitized != tex_text:
        with open(path, "w", encoding="utf-8") as f:
            f.write(sanitized)


_LATEX_FIGURE_EXTENSIONS = (".png", ".pdf", ".jpg", ".jpeg", ".webp", ".svg", ".eps")
_REPO_ROOT = Path(__file__).resolve().parent.parent
_TII_TEMPLATE_DIR = _REPO_ROOT / "论文写作" / ".archive_20260326" / "TII-Articles-LaTeX-template"
_DEFAULT_LATEX_TEMPLATE_DIR = _REPO_ROOT / "templates" / "paper_writer" / "latex"
_LATEX_SUPPORT_FILES = ("ieeecolor.cls", "generic.sty", "TII.eps")


def _extract_external_bibliography_targets(tex_text: str) -> list[str]:
    matches = re.findall(r"\\bibliography\{([^}]*)\}", tex_text)
    targets: list[str] = []
    for item in matches:
        for name in item.split(","):
            cleaned = name.strip()
            if cleaned:
                targets.append(cleaned)
    return targets


def _resolve_external_bib_files(cwd: str, tex_text: str) -> list[str]:
    resolved: list[str] = []
    seen = set()
    for target in _extract_external_bibliography_targets(tex_text):
        candidate = target if target.endswith(".bib") else f"{target}.bib"
        path = candidate if osp.isabs(candidate) else osp.join(cwd, candidate)
        normalized = osp.abspath(path)
        if osp.exists(normalized) and normalized not in seen:
            seen.add(normalized)
            resolved.append(normalized)
    return resolved


def _load_available_bibliography_text(cwd: str, tex_text: str) -> tuple[str | None, bool]:
    embedded = _extract_embedded_references_bib(tex_text)
    if embedded is not None:
        return embedded, True
    if _has_inline_thebibliography(tex_text):
        return None, False
    external_files = _resolve_external_bib_files(cwd, tex_text)
    if external_files:
        contents: list[str] = []
        for path in external_files:
            with open(path, encoding="utf-8") as f:
                contents.append(f.read())
        return "\n".join(contents), True
    return None, False


def _extract_embedded_references_bib(tex_text: str) -> str | None:
    match = re.search(
        r"\\begin{filecontents}{references.bib}(.*?)\\end{filecontents}",
        tex_text,
        re.DOTALL,
    )
    if match is None:
        return None
    return match.group(1)


def _has_inline_thebibliography(tex_text: str) -> bool:
    return re.search(r"\\begin{thebibliography}", tex_text) is not None


def _list_available_figures(cwd: str) -> list[str]:
    available: list[str] = []
    for root, _, files in os.walk(cwd):
        for filename in files:
            if Path(filename).suffix.lower() not in _LATEX_FIGURE_EXTENSIONS:
                continue
            available.append(osp.relpath(osp.join(root, filename), cwd))
    return sorted(available)


def _figure_exists(cwd: str, figure: str) -> bool:
    candidate = figure.strip()
    if not candidate:
        return False

    candidates = [candidate]
    _, ext = osp.splitext(candidate)
    if not ext:
        candidates.extend(f"{candidate}{suffix}" for suffix in _LATEX_FIGURE_EXTENSIONS)

    for item in candidates:
        path = item if osp.isabs(item) else osp.join(cwd, item)
        if osp.exists(path):
            return True
    return False


def _ensure_latex_support_files(cwd: str) -> None:
    target_dir = Path(cwd)
    for filename in _LATEX_SUPPORT_FILES:
        target = target_dir / filename
        if target.exists():
            continue
        for source_dir in (_TII_TEMPLATE_DIR, _DEFAULT_LATEX_TEMPLATE_DIR):
            source = source_dir / filename
            if source.exists():
                shutil.copy2(source, target)
                break


@dataclass
class ChktexResult:
    errors: list[str]
    warnings: list[str]
    raw_output: str


def run_chktex(latex_dir: str, tex_file: str) -> ChktexResult:
    if shutil.which("chktex") is None:
        return ChktexResult(
            errors=[],
            warnings=[],
            raw_output="chktex is not installed; lint step skipped",
        )
    proc = subprocess.run(
        ["chktex", tex_file, "-q", "-v0", "-f", "%k|%n|%l|%c|%m\n"],
        cwd=latex_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    raw_output = proc.stdout or ""
    errors: list[str] = []
    warnings: list[str] = []
    for line in raw_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split("|", 4)
        if len(parts) == 5:
            kind, msg_id, row, col, message = parts
            rendered = f"{kind} {msg_id} at {row}:{col}: {message}".strip()
            lowered = kind.lower()
            if lowered.startswith("error"):
                errors.append(rendered)
            else:
                warnings.append(rendered)
            continue
        lowered = stripped.lower()
        if lowered.startswith("error"):
            errors.append(stripped)
        else:
            warnings.append(stripped)
    return ChktexResult(errors=errors, warnings=warnings, raw_output=raw_output)


# GENERATE LATEX
def generate_latex(
    coder,
    folder_name,
    pdf_file,
    timeout=30,
    num_error_corrections=PAPERFORGE_DEFAULT_NUM_ERROR_CORRECTIONS,
    checkpoint_enabled: bool = False,
    checkpoint_resume_round: int = 0,
    skip_chktex_fix: bool = False,
):
    folder = osp.abspath(folder_name)
    cwd = osp.join(folder, "latex")  # Fixed potential issue with path
    _ensure_latex_support_files(cwd)
    writeup_file = osp.join(cwd, "template.tex")
    _sanitize_template_tex_file(writeup_file)

    # Check all references are valid and in the references.bib file
    with open(writeup_file) as f:
        tex_text = f.read()
    cites = re.findall(r"\\cite[a-z]*{([^}]*)}", tex_text)
    bib_text, use_bibtex = _load_available_bibliography_text(cwd, tex_text)
    if bib_text is None and not _has_inline_thebibliography(tex_text):
        print("No references.bib, external bibliography, or inline thebibliography found in template.tex")
        return False
    if bib_text is not None:
        cites = [cite.strip() for item in cites for cite in item.split(",")]
        for cite in cites:
            if cite not in bib_text:
                print(f"Reference {cite} not found in references.")
                prompt = f"""Reference {cite} not found in references.bib. Is this included under a different name?
If so, please modify the citation in template.tex to match the name in references.bib at the top. Otherwise, remove the cite."""
                coder.run(prompt)

    # Check all included figures are actually in the directory.
    with open(writeup_file) as f:
        tex_text = f.read()
    referenced_figs = re.findall(r"\\includegraphics.*?{(.*?)}", tex_text)
    available_figs = _list_available_figures(cwd)
    for figure in referenced_figs:
        if not _figure_exists(cwd, figure):
            print(f"Figure {figure} not found in directory.")
            prompt = f"""The image {figure} not found in the directory. The images in the directory are: {available_figs}.
Please ensure that the figure is in the directory and that the filename is correct. Check the notes to see what each figure contains."""
            coder.run(prompt)

    # Remove duplicate figures.
    with open(writeup_file) as f:
        tex_text = f.read()
    referenced_figs = re.findall(r"\\includegraphics.*?{(.*?)}", tex_text)
    duplicates = {x for x in referenced_figs if referenced_figs.count(x) > 1}
    if duplicates:
        for dup in duplicates:
            print(f"Duplicate figure found: {dup}.")
            prompt = f"""Duplicate figures found: {dup}. Ensure any figure is only included once.
If duplicated, identify the best location for the figure and remove any other."""
            coder.run(prompt)

    # Remove duplicate section headers.
    with open(writeup_file) as f:
        tex_text = f.read()
    sections = re.findall(r"\\section{([^}]*)}", tex_text)
    duplicates = {x for x in sections if sections.count(x) > 1}
    if duplicates:
        for dup in duplicates:
            print(f"Duplicate section header found: {dup}")
            prompt = f"""Duplicate section header found: {dup}. Ensure any section header is declared once.
If duplicated, identify the best location for the section header and remove any other."""
            coder.run(prompt)

    # Iteratively fix any LaTeX bugs
    if skip_chktex_fix:
        print("[writeup] skipping chktex auto-fix in existing manuscript mode.")
        chktex_result = run_chktex(cwd, writeup_file)
        if chktex_result.errors:
            error_path = Path(cwd) / "chktex_errors.txt"
            error_path.write_text("\n".join(chktex_result.errors) + "\n", encoding="utf-8")
            raise RuntimeError(f"chktex blocking errors: {error_path}")
        if chktex_result.warnings:
            warning_path = Path(cwd) / "chktex_warnings.txt"
            warning_path.write_text("\n".join(chktex_result.warnings) + "\n", encoding="utf-8")
    else:
        for i in range(num_error_corrections):
            round_idx = i + 1
            if checkpoint_enabled and round_idx <= max(0, checkpoint_resume_round):
                print(f"[writeup][checkpoint] skipping latex_fix round {round_idx}")
                continue
            chktex_result = run_chktex(cwd, writeup_file)
            if chktex_result.errors:
                error_path = Path(cwd) / "chktex_errors.txt"
                error_path.write_text("\n".join(chktex_result.errors) + "\n", encoding="utf-8")
                raise RuntimeError(f"chktex blocking errors: {error_path}")
            if chktex_result.warnings:
                warning_path = Path(cwd) / "chktex_warnings.txt"
                warning_path.write_text("\n".join(chktex_result.warnings) + "\n", encoding="utf-8")
            break
    with open(writeup_file) as f:
        tex_text = f.read()
    use_bibtex = _load_available_bibliography_text(cwd, tex_text)[1]
    return compile_latex(cwd, pdf_file, timeout=timeout, use_bibtex=use_bibtex)


def compile_latex(cwd, pdf_file, timeout=30, use_bibtex: bool = True):
    print("GENERATING LATEX")
    compile_log_path = Path(cwd) / "compile_output.log"
    compile_chunks: list[str] = []

    commands = [["pdflatex", "-interaction=nonstopmode", "template.tex"]]
    if use_bibtex:
        commands.append(["bibtex", "template"])
    commands.extend(
        [
            ["pdflatex", "-interaction=nonstopmode", "template.tex"],
            ["pdflatex", "-interaction=nonstopmode", "template.tex"],
        ]
    )

    for command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            compile_chunks.append(
                "## Command: {cmd}\n\n### STDOUT\n{stdout}\n\n### STDERR\n{stderr}\n".format(
                    cmd=" ".join(command),
                    stdout=result.stdout,
                    stderr=result.stderr,
                )
            )
            print("Standard Output:\n", result.stdout)
            print("Standard Error:\n", result.stderr)
        except subprocess.TimeoutExpired:
            print(f"Latex timed out after {timeout} seconds")
            compile_chunks.append(f"## Command timed out after {timeout}s: {' '.join(command)}\n")
        except subprocess.CalledProcessError as e:
            print(f"Error running command {' '.join(command)}: {e}")
            compile_chunks.append(f"## Command error: {' '.join(command)} -> {e}\n")

    compile_log_path.write_text("\n".join(compile_chunks), encoding="utf-8")

    print("FINISHED GENERATING LATEX")

    # Attempt to move the PDF to the desired location
    try:
        shutil.move(osp.join(cwd, "template.pdf"), pdf_file)
        return True
    except FileNotFoundError:
        print("Failed to rename PDF.")
        return False


per_section_tips = {
    "Abstract": """
- TL;DR of the paper
- What are we trying to do and why is it relevant?
- Why is this hard? 
- How do we solve it (i.e. our contribution!)
- How do we verify that we solved it (e.g. Experiments and results)

Please make sure the abstract reads smoothly and is well-motivated. This should be one continuous paragraph with no breaks between the lines.
""",
    "Introduction": """
- Longer version of the Abstract, i.e. of the entire paper
- What are we trying to do and why is it relevant?
- Why is this hard? 
- How do we solve it (i.e. our contribution!)
- How do we verify that we solved it (e.g. Experiments and results)
- New trend: specifically list your contributions as bullet points
- Extra space? Future work!
""",
    "Related Work": """
- Academic siblings of our work, i.e. alternative attempts in literature at trying to solve the same problem. 
- Goal is to “Compare and contrast” - how does their approach differ in either assumptions or method? If their method is applicable to our Problem Setting I expect a comparison in the experimental section. If not, there needs to be a clear statement why a given method is not applicable. 
- Note: Just describing what another paper is doing is not enough. We need to compare and contrast.
""",
    "Background": """
- Academic Ancestors of our work, i.e. all concepts and prior work that are required for understanding our method. 
- Usually includes a subsection, Problem Setting, which formally introduces the problem setting and notation (Formalism) for our method. Highlights any specific assumptions that are made that are unusual. 
- Note: If our paper introduces a novel problem setting as part of its contributions, it's best to have a separate Section.
""",
    "Method": """
- What we do. Why we do it. All described using the general Formalism introduced in the Problem Setting and building on top of the concepts / foundations introduced in Background.
""",
    "Experimental Setup": """
- How do we test that our stuff works? Introduces a specific instantiation of the Problem Setting and specific implementation details of our Method for this Problem Setting.
- Do not imagine unknown hardware details.
- Includes a description of the dataset, evaluation metrics, important hyperparameters, and implementation details.
""",
    "Results": """
- Shows the results of running Method on our problem described in Experimental Setup.
- Includes statements on hyperparameters and other potential issues of fairness.
- Only includes results that have actually been run and saved in the logs. Do not hallucinate results that don't exist.
- If results exist: compares to baselines and includes statistics and confidence intervals. 
- If results exist: includes ablation studies to show that specific parts of the method are relevant.
- Discusses limitations of the method.
- Make sure to include all the results from the experiments, and include all relevant figures.
""",
    "Conclusion": """
- Brief recap of the entire paper.
- To keep going with the analogy, you can think of future work as (potential) academic offspring.
""",
}

error_list = """- Unenclosed math symbols
- Only reference figures that exist in our directory
- LaTeX syntax errors
- Numerical results that do not come from explicit experiments and logs
- Repeatedly defined figure labels
- References to papers that are not in the .bib file, DO NOT ADD ANY NEW CITATIONS!
- Unnecessary verbosity or repetition, unclear text
- Results or insights in the `notes.txt` that have not yet need included
- Any relevant figures that have not yet been included in the text
- Closing any \\begin{{figure}} with a \\end{{figure}} and \\begin{{table}} with a \\end{{table}}, etc.
- Duplicate headers, e.g. duplicated \\section{{Introduction}} or \\end{{document}}
- Unescaped symbols, e.g. shakespeare_char should be shakespeare\\_char in text
- Incorrect closing of environments, e.g. </end{{figure}}> instead of \\end{{figure}}
"""

refinement_prompt = (
    """Great job! Now criticize and refine only the {section} that you just wrote.
Make this complete in this pass, do not leave any placeholders.

Pay particular attention to fixing any errors such as:
"""
    + error_list
)

second_refinement_prompt = (
    """Criticize and refine the {section} only. Recall the advice:
{tips}
Make this complete in this pass, do not leave any placeholders.

Pay attention to how it fits in with the rest of the paper.
Identify any redundancies (e.g. repeated figures or repeated text), if there are any, decide where in the paper things should be cut.
Identify where we can save space, and be more concise without weakening the message of the text.
Fix any remaining errors as before:
"""
    + error_list
)

# CITATION HELPERS
citation_system_msg = """You are a rigorous PhD researcher who is looking to publish a paper that will contribute significantly to the field.
You have already written an initial draft of the paper and now you are looking to add missing citations to related papers throughout the paper.
The related work section already has some initial comments on which papers to add and discuss.

Focus on completing the existing write-up and do not add entirely new elements unless necessary.
Ensure every point in the paper is substantiated with sufficient evidence.
Feel free to add more cites to a particular point if there is only one or two references.
Ensure no paper is cited without a corresponding reference in the `references.bib` file.
Ensure each paragraph of the related work has sufficient background, e.g. a few papers cited.
You will be given access to the Semantic Scholar API, only add citations that you have found using the API.
Aim to discuss a broad range of relevant papers, not just the most popular ones.
Make sure not to copy verbatim from prior literature to avoid plagiarism.

You will be prompted to give a precise description of where and how to add the cite, and a search query for the paper to be cited.
Finally, you will select the most relevant cite from the search results (top 10 results will be shown).
You will have {total_rounds} rounds to add to the references, but do not need to use them all.

DO NOT ADD A CITATION THAT ALREADY EXISTS!"""

citation_first_prompt = '''Round {current_round}/{total_rounds}:

You have written this LaTeX draft so far:

"""
{draft}
"""

Identify the most important citation that you still need to add, and the query to find the paper.

Respond in the following format:

THOUGHT:
<THOUGHT>

RESPONSE:
```json
<JSON>
```

In <THOUGHT>, first briefly reason over the paper and identify where citations should be added.
If no more citations are needed, add "No more citations needed" to your thoughts.
Do not add "No more citations needed" if you are adding citations this round.

In <JSON>, respond in JSON format with the following fields:
- "Description": A precise description of the required edit, along with the proposed text and location where it should be made.
- "Query": The search query to find the paper (e.g. attention is all you need).

Ensure the description is sufficient to make the change without further context. Someone else will make the change.
The query will work best if you are able to recall the exact name of the paper you are looking for, or the authors.
This JSON will be automatically parsed, so ensure the format is precise.'''

citation_second_prompt = """Search has recovered the following articles:

{papers}

Respond in the following format:

THOUGHT:
<THOUGHT>

RESPONSE:
```json
<JSON>
```

In <THOUGHT>, first briefly reason over the search results and identify which citation best fits your paper and the location is to be added at.
If none are appropriate, add "Do not add any" to your thoughts.

In <JSON>, respond in JSON format with the following fields:
- "Selected": A list of the indices of the selected papers to be cited, e.g. "[0, 1]". Can be "[]" if no papers are selected. This must be a string.
- "Description": Update the previous description of the required edit if needed. Ensure that any cites precisely match the name in the bibtex!!!

Do not select papers that are already in the `references.bib` file at the top of the draft, or if the same citation exists under a different name.
This JSON will be automatically parsed, so ensure the format is precise."""


def get_citation_aider_prompt(
        client, model, draft, current_round, total_rounds, engine="semanticscholar"
) -> tuple[str | None, bool]:
    msg_history = []
    try:
        text, msg_history = get_response_from_llm(
            citation_first_prompt.format(
                draft=draft, current_round=current_round, total_rounds=total_rounds
            ),
            client=client,
            model=model,
            system_message=citation_system_msg.format(total_rounds=total_rounds),
            msg_history=msg_history,
        )
        if "No more citations needed" in text:
            print("No more citations needed.")
            return None, True

        ## PARSE OUTPUT
        json_output = extract_json_between_markers(text)
        assert json_output is not None, "Failed to extract JSON from LLM output"
        query = json_output["Query"]
        papers = search_for_papers(query, engine=engine)
    except Exception as e:
        print(f"Error: {e}")
        return None, False

    if papers is None:
        print("No papers found.")
        return None, False

    paper_strings = []
    for i, paper in enumerate(papers):
        paper_strings.append(
            """{i}: {title}. {authors}. {venue}, {year}.\nAbstract: {abstract}""".format(
                i=i,
                title=paper["title"],
                authors=paper["authors"],
                venue=paper["venue"],
                year=paper["year"],
                abstract=paper["abstract"],
            )
        )
    papers_str = "\n\n".join(paper_strings)

    try:
        text, msg_history = get_response_from_llm(
            citation_second_prompt.format(
                papers=papers_str,
                current_round=current_round,
                total_rounds=total_rounds,
            ),
            client=client,
            model=model,
            system_message=citation_system_msg.format(total_rounds=total_rounds),
            msg_history=msg_history,
        )
        if "Do not add any" in text:
            print("Do not add any.")
            return None, False
        ## PARSE OUTPUT
        json_output = extract_json_between_markers(text)
        assert json_output is not None, "Failed to extract JSON from LLM output"
        desc = json_output["Description"]
        selected_papers = json_output["Selected"]
        selected_papers = str(selected_papers)

        # convert to list
        if selected_papers != "[]":
            selected_papers = list(map(int, selected_papers.strip("[]").split(",")))
            assert all(
                [0 <= i < len(papers) for i in selected_papers]
            ), "Invalid paper index"
            bibtexs = [papers[i]["citationStyles"]["bibtex"] for i in selected_papers]
            blocked_keys = _blocked_citation_keys()
            bibtexs = [
                bibtex
                for bibtex in bibtexs
                if _extract_bibtex_key(bibtex) not in blocked_keys
            ]
            if not bibtexs:
                return None, False
            bibtex_string = "\n".join(bibtexs)
        else:
            return None, False

    except Exception as e:
        print(f"Error: {e}")
        return None, False

    # Add citation to draft
    aider_format = '''The following citations have just been added to the end of the `references.bib` file definition at the top of the file:
"""
{bibtex}
"""
You do not need to add them yourself.
ABSOLUTELY DO NOT ADD IT AGAIN!!!

Make the proposed change to the draft incorporating these new cites:
{description}

Use your judgment for whether these should be cited anywhere else.
Make sure that any citation precisely matches the name in `references.bib`. Change its name to the correct name in the bibtex if needed.
Ensure the citation is well-integrated into the text.'''

    aider_prompt = (
            aider_format.format(bibtex=bibtex_string, description=desc)
            + """\n You must use \\cite or \\citet to reference papers, do not manually type out author names."""
    )
    return aider_prompt, False


# PERFORM WRITEUP
def perform_writeup(
        idea,
        folder_name,
        coder,
        cite_client,
        cite_model,
        num_cite_rounds=PAPERFORGE_DEFAULT_NUM_CITE_ROUNDS,
        engine="semanticscholar",
        existing_draft_path: str | None = None,
        skip_chktex_fix: bool | None = None,
):
    num_cite_rounds = _env_int("WRITEUP_CITE_ROUNDS", num_cite_rounds)
    second_refinement_enabled = _env_bool(
        "WRITEUP_SECOND_REFINEMENT",
        "1" if PAPERFORGE_DEFAULT_SECOND_REFINEMENT_ENABLED else "0",
    )
    num_error_corrections = _env_int(
        "WRITEUP_LATEX_FIX_ROUNDS",
        PAPERFORGE_DEFAULT_NUM_ERROR_CORRECTIONS,
    )
    checkpoint_enabled = _env_bool("WRITEUP_ENABLE_CHECKPOINT", "1")
    reset_checkpoint = _env_bool("WRITEUP_RESET_CHECKPOINT", "0")
    skip_experimental_sections = _env_bool(
        "WRITEUP_SKIP_EXPERIMENTAL_SECTIONS",
        "0",
    )
    use_existing_draft = existing_draft_path is not None
    if not use_existing_draft and _deprecated_env_override("WRITEUP_USE_EXISTING_DRAFT") is not None:
        use_existing_draft = _env_bool("WRITEUP_USE_EXISTING_DRAFT", "0")
    if skip_chktex_fix is None:
        deprecated = _deprecated_env_override("WRITEUP_SKIP_CHKTEX_FIX")
        if deprecated is not None:
            skip_chktex_fix = _env_bool("WRITEUP_SKIP_CHKTEX_FIX", "0")
        else:
            skip_chktex_fix = use_existing_draft
    writeup_tex_file = osp.join(folder_name, "latex", "template.tex")
    final_pdf_file = f"{folder_name}/{idea['Name']}.pdf"
    if existing_draft_path:
        source_draft = Path(existing_draft_path).expanduser().resolve()
        target_draft = Path(writeup_tex_file).resolve()
        if not source_draft.exists():
            raise FileNotFoundError(f"existing draft not found: {source_draft}")
        if source_draft != target_draft:
            shutil.copy2(source_draft, target_draft)

    checkpoint_state = _default_writeup_checkpoint()
    if checkpoint_enabled:
        if reset_checkpoint:
            _remove_writeup_checkpoint(folder_name)
            print("[writeup][checkpoint] reset requested; previous checkpoint cleared.")
        checkpoint_state = _load_writeup_checkpoint(folder_name)
        if _checkpoint_stage_rank(checkpoint_state["stage"]) > _checkpoint_stage_rank("start"):
            restored = _restore_writeup_tex_from_checkpoint(folder_name, writeup_tex_file, checkpoint_state)
            if restored:
                print(
                    "[writeup][checkpoint] restored stage={stage} round={round}".format(
                        stage=checkpoint_state["stage"],
                        round=checkpoint_state["current_round"],
                    )
                )
        if checkpoint_state["stage"] == "done" and osp.exists(final_pdf_file):
            print(f"[writeup][checkpoint] already done, skip writeup: {final_pdf_file}")
            return
        if checkpoint_state["stage"] == "done" and not osp.exists(final_pdf_file):
            print("[writeup][checkpoint] stage=done but final PDF missing, retrying latex stage.")
            checkpoint_state["stage"] = "latex_fix"

    style_guidelines = _build_style_guidelines(_extract_theme_text(idea))
    if skip_experimental_sections:
        style_guidelines += """

Non-experimental writing pass (must follow):
- Do not create, rewrite, expand, or remove Experimental Setup or Results sections.
- Preserve any author-owned experimental material and reserved insertion markers verbatim.
- Do not invent metrics, datasets, hardware, baselines, ablations, tables, figures, or performance comparisons.
- In the abstract, introduction, and conclusion, describe the method and scope without claiming measured superiority.
- Treat empirical validation as author-supplied material outside this writing pass.
"""
        print("[writeup] non-experimental writing pass enabled")
    _sanitize_template_tex_file(writeup_tex_file)
    protection_transaction = None
    if skip_experimental_sections:
        protected_text = Path(writeup_tex_file).read_text(encoding="utf-8")
        if not extract_protected_blocks(protected_text):
            raise ValueError(
                "writing-only mode requires a protected experiment block in template.tex"
            )
        protection_transaction = ProtectedEditTransaction(
            Path(writeup_tex_file),
            require_markers=True,
        )
        coder = ProtectedCoder(coder, writeup_tex_file, require_markers=True)

    if use_existing_draft and _checkpoint_stage_rank(checkpoint_state["stage"]) < _checkpoint_stage_rank("latex_fix"):
        print("[writeup] existing manuscript mode enabled; skipping init/cite/refine stages.")
        checkpoint_state["stage"] = "latex_fix"
        checkpoint_state["current_round"] = 0

    if _checkpoint_stage_rank(checkpoint_state["stage"]) < _checkpoint_stage_rank("init"):
        # CURRENTLY ASSUMES LATEX
        abstract_prompt = f"""We've provided the `latex/template.tex` file to the project. We will be filling it in section by section.

First, please fill in the "Title" and "Abstract" sections of the writeup.

Some tips are provided below:
{per_section_tips["Abstract"]}

Be sure to first name the file and use *SEARCH/REPLACE* blocks to perform these edits.
"""
        coder.run(_append_style(abstract_prompt, style_guidelines))
        coder.run(
            _append_style(
                refinement_prompt.format(section="Abstract")
                .replace(r"{{", "{")
                .replace(r"}}", "}"),
                style_guidelines,
            )
        )
        draft_sections = [
            "Introduction",
            "Background",
            "Method",
            "Experimental Setup",
            "Results",
            "Conclusion",
        ]
        if skip_experimental_sections:
            draft_sections = [
                section
                for section in draft_sections
                if section not in {"Experimental Setup", "Results"}
            ]
        for section in draft_sections:
            section_prompt = f"""Please fill in the {section} of the writeup. Some tips are provided below:
{per_section_tips[section]}

Be sure to use \\cite or \\citet where relevant, referring to the works provided in the file.
Do not cite anything that is not already in `references.bib`. Do not add any new entries to this.

Keep the experimental results (figures and tables) only in the Results section, and make sure that any captions are filled in.
In this pass, do not reference anything in later sections of the paper.

Be sure to first name the file and use *SEARCH/REPLACE* blocks to perform these edits.
"""
            coder.run(_append_style(section_prompt, style_guidelines))
            coder.run(
                _append_style(
                    refinement_prompt.format(section=section)
                    .replace(r"{{", "{")
                    .replace(r"}}", "}"),
                    style_guidelines,
                )
            )

        # SKETCH THE RELATED WORK
        section_prompt = f"""Please fill in the Related Work of the writeup. Some tips are provided below:

{per_section_tips["Related Work"]}

For this section, very briefly sketch out the structure of the section, and clearly indicate what papers you intend to include.
Do this all in LaTeX comments using %.
The related work should be concise, only plan to discuss the most relevant work.
Do not modify `references.bib` to add any new citations, this will be filled in at a later stage.

Be sure to first name the file and use *SEARCH/REPLACE* blocks to perform these edits.
"""
        coder.run(_append_style(section_prompt, style_guidelines))
        _sanitize_template_tex_file(writeup_tex_file)
        if protection_transaction is not None:
            protection_transaction.verify()
        if checkpoint_enabled:
            checkpoint_state = _save_writeup_checkpoint(
                folder_name,
                stage="init",
                current_round=0,
                writeup_tex_file=writeup_tex_file,
                snapshot_name="template_init.tex",
            )
    else:
        print("[writeup][checkpoint] skipping init stage")

    cite_stage_rank = _checkpoint_stage_rank("cite")
    if _checkpoint_stage_rank(checkpoint_state["stage"]) <= cite_stage_rank:
        cite_resume_round = 0
        if checkpoint_state["stage"] == "cite":
            cite_resume_round = max(0, int(checkpoint_state.get("current_round", 0)))

        for cite_round in range(1, num_cite_rounds + 1):
            if checkpoint_enabled and cite_round <= min(cite_resume_round, num_cite_rounds):
                print(f"[writeup][checkpoint] skipping cite round {cite_round}")
                continue

            with open(writeup_tex_file) as f:
                draft = f.read()
            prompt, done = get_citation_aider_prompt(
                cite_client,
                cite_model,
                draft,
                cite_round,
                num_cite_rounds,
                engine=engine,
            )
            if prompt is not None:
                # extract bibtex string
                bibtex_string = prompt.split('"""')[1]
                # insert this into draft before the "\end{filecontents}" line
                search_str = r"\end{filecontents}"
                draft = draft.replace(search_str, f"{bibtex_string}{search_str}")
                with open(writeup_tex_file, "w") as f:
                    f.write(draft)
                coder.run(_append_style(prompt, style_guidelines))
            _sanitize_template_tex_file(writeup_tex_file)
            if protection_transaction is not None:
                protection_transaction.verify()
            if checkpoint_enabled:
                checkpoint_state = _save_writeup_checkpoint(
                    folder_name,
                    stage="cite",
                    current_round=cite_round,
                    writeup_tex_file=writeup_tex_file,
                    snapshot_name=f"template_cite_{cite_round}.tex",
                )
            if done:
                break

        if checkpoint_enabled and int(checkpoint_state.get("current_round", 0)) >= (num_cite_rounds + 1):
            print("[writeup][checkpoint] skipping related-work refinement after cite rounds")
        else:
            coder.run(
                _append_style(
                    refinement_prompt.format(section="Related Work")
                    .replace(r"{{", "{")
                    .replace(r"}}", "}"),
                    style_guidelines,
                )
            )
            _sanitize_template_tex_file(writeup_tex_file)
            if checkpoint_enabled:
                checkpoint_state = _save_writeup_checkpoint(
                    folder_name,
                    stage="cite",
                    current_round=num_cite_rounds + 1,
                    writeup_tex_file=writeup_tex_file,
                    snapshot_name="template_cite_refined.tex",
                )
    else:
        print("[writeup][checkpoint] skipping cite stage")

    refine_stage_rank = _checkpoint_stage_rank("refine")
    if second_refinement_enabled and _checkpoint_stage_rank(checkpoint_state["stage"]) <= refine_stage_rank:
        refine_sections = [
            "Abstract",
            "Related Work",
            "Introduction",
            "Background",
            "Method",
            "Experimental Setup",
            "Results",
            "Conclusion",
        ]
        if skip_experimental_sections:
            refine_sections = [
                section
                for section in refine_sections
                if section not in {"Experimental Setup", "Results"}
            ]
        refine_resume_round = 0
        if checkpoint_state["stage"] == "refine":
            refine_resume_round = max(0, int(checkpoint_state.get("current_round", 0)))

        if checkpoint_enabled and refine_resume_round > 0:
            print("[writeup][checkpoint] skipping refine title rethink")
        else:
            coder.run(
                _append_style(
                    """Great job! Now that there is a complete draft of the entire paper, let's refine each section again.
First, re-think the Title if necessary. Keep this concise and descriptive of the paper's concept, but try by creative with it.""",
                    style_guidelines,
                )
            )
            _sanitize_template_tex_file(writeup_tex_file)
            if checkpoint_enabled:
                checkpoint_state = _save_writeup_checkpoint(
                    folder_name,
                    stage="refine",
                    current_round=0,
                    writeup_tex_file=writeup_tex_file,
                    snapshot_name="template_refine_0.tex",
                )

        for idx, section in enumerate(refine_sections, start=1):
            if checkpoint_enabled and idx <= refine_resume_round:
                print(f"[writeup][checkpoint] skipping refine section {idx}: {section}")
                continue
            coder.run(
                _append_style(
                    second_refinement_prompt.format(
                        section=section, tips=per_section_tips[section]
                    )
                    .replace(r"{{", "{")
                    .replace(r"}}", "}"),
                    style_guidelines,
                )
            )
            _sanitize_template_tex_file(writeup_tex_file)
            if checkpoint_enabled:
                checkpoint_state = _save_writeup_checkpoint(
                    folder_name,
                    stage="refine",
                    current_round=idx,
                    writeup_tex_file=writeup_tex_file,
                    snapshot_name=f"template_refine_{idx}.tex",
                )
    elif second_refinement_enabled:
        print("[writeup][checkpoint] skipping refine stage")
    else:
        print("[writeup] second refinement disabled")

    latex_resume_round = 0
    if checkpoint_state["stage"] == "latex_fix":
        latex_resume_round = max(0, int(checkpoint_state.get("current_round", 0)))
    elif _checkpoint_stage_rank(checkpoint_state["stage"]) > _checkpoint_stage_rank("latex_fix"):
        latex_resume_round = num_error_corrections

    if protection_transaction is not None:
        protection_transaction.verify()
    latex_ok = generate_latex(
        coder,
        folder_name,
        final_pdf_file,
        num_error_corrections=num_error_corrections,
        checkpoint_enabled=checkpoint_enabled,
        checkpoint_resume_round=latex_resume_round,
        skip_chktex_fix=bool(skip_chktex_fix),
    )
    _sanitize_template_tex_file(writeup_tex_file)
    if protection_transaction is not None:
        protection_transaction.verify()
    if checkpoint_enabled and latex_ok:
        _save_writeup_checkpoint(
            folder_name,
            stage="done",
            current_round=0,
            writeup_tex_file=writeup_tex_file,
            snapshot_name="template_done.tex",
        )
    elif checkpoint_enabled:
        print("[writeup][checkpoint] latex generation incomplete, checkpoint remains resumable.")


if __name__ == "__main__":
    import json

    from aider.coders import Coder
    from aider.io import InputOutput

    from paperforge.provider import build_aider_model

    parser = argparse.ArgumentParser(description="Perform writeup for a project")
    parser.add_argument("--folder", type=str)
    parser.add_argument("--no-writing", action="store_true", help="Only generate")
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-5.4-xhigh",
        choices=AVAILABLE_LLMS,
        help="Model to use for PaperForge.",
    )
    parser.add_argument(
        "--engine",
        type=str,
        default="semanticscholar",
        choices=["semanticscholar", "openalex"],
        help="Scholar engine to use.",
    )
    parser.add_argument("--gateway-profile", choices=["safe", "full"], default=None)
    parser.add_argument("--existing-draft", default=None)
    parser.add_argument("--skip-chktex-fix", dest="skip_chktex_fix", action="store_true")
    parser.add_argument("--no-skip-chktex-fix", dest="skip_chktex_fix", action="store_false")
    parser.set_defaults(skip_chktex_fix=None)
    args = parser.parse_args()
    for key, value in gateway_profile_env_overrides(args.gateway_profile).items():
        os.environ[key] = value
    client, client_model = create_client(args.model)
    print("Make sure you cleaned the Aider logs if re-generating the writeup!")
    folder_name = args.folder
    idea_name = osp.basename(folder_name)
    exp_file = osp.join(folder_name, "experiment.py")
    vis_file = osp.join(folder_name, "plot.py")
    notes = osp.join(folder_name, "notes.txt")
    model = args.model
    writeup_file = osp.join(folder_name, "latex", "template.tex")
    ideas_file = osp.join(folder_name, "ideas.json")
    with open(ideas_file) as f:
        ideas = json.load(f)
    for idea in ideas:
        if idea["Name"] in idea_name:
            print(f"Found idea: {idea['Name']}")
            break
    if idea["Name"] not in idea_name:
        raise ValueError(f"Idea {idea_name} not found")
    fnames = [exp_file, writeup_file, notes]
    io = InputOutput(yes=True, chat_history_file=f"{folder_name}/{idea_name}_aider.txt")
    main_model = build_aider_model(
        model,
        generation_profile=args.gateway_profile or "safe",
        stage="writeup",
    )
    coder = Coder.create(
        main_model=main_model,
        fnames=fnames,
        io=io,
        stream=_env_bool("PAPERFORGE_AIDER_STREAM", "1"),
        use_git=False,
        edit_format="diff",
    )
    with run_lock(Path(folder_name).resolve(), timeout=30, poll_interval=0.2, verbose=True):
        if args.no_writing:
            generate_latex(
                coder,
                args.folder,
                f"{args.folder}/test.pdf",
                skip_chktex_fix=bool(args.skip_chktex_fix),
            )
        else:
            try:
                perform_writeup(
                    idea,
                    folder_name,
                    coder,
                    client,
                    client_model,
                    engine=args.engine,
                    existing_draft_path=args.existing_draft,
                    skip_chktex_fix=args.skip_chktex_fix,
                )
            except Exception as e:
                print(f"Failed to perform writeup: {e}")
