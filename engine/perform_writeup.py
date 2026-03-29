import argparse
import json
import os
import os.path as osp
import re
import runpy
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from engine.generate_ideas import search_for_papers
from engine.llm import get_response_from_llm, extract_json_between_markers, create_client, AVAILABLE_LLMS
from engine.run_lock import run_lock
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


def _default_writeup_checkpoint() -> Dict:
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


def _load_writeup_checkpoint(folder_name: str) -> Dict:
    path = _checkpoint_path(folder_name)
    if not osp.exists(path):
        return _default_writeup_checkpoint()
    try:
        with open(path, "r", encoding="utf-8") as f:
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
) -> Dict:
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


def _restore_writeup_tex_from_checkpoint(folder_name: str, writeup_tex_file: str, state: Dict) -> bool:
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


PRACTICAL_PROMPT_KEYS = [
    "论文评审专家",
    "写英文摘要",
    "SCI论文润色",
    "润色英文段落结构和句子逻辑",
    "语法检查/查找语法错误",
    "直接润色段落",
    "逻辑论证辅助",
]


def _candidate_prompt_library_paths() -> List[str]:
    paths: List[str] = []

    env_path = os.getenv("PAPERFORGE_PROMPT_LIBRARY_PATH", "").strip()
    if env_path:
        paths.append(osp.abspath(osp.expanduser(env_path)))

    # Default prompt library location inside project root.
    paths.append(osp.abspath(osp.join(osp.dirname(__file__), "..", "prompt_library.py")))
    # Backward-compatible fallback for legacy filename.
    paths.append(osp.abspath(osp.join(osp.dirname(__file__), "..", "提示词.py")))

    deduped: List[str] = []
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


def _extract_theme_text(idea: Dict) -> str:
    if not isinstance(idea, dict):
        return ""
    fields: List[str] = []
    for key in ("Title", "Experiment", "Name", "Topic", "Keywords"):
        value = idea.get(key)
        if isinstance(value, str) and value.strip():
            fields.append(value.strip())
    return "\n".join(fields)


def _select_theme_matched_prompt_cues(
    prompt_library: Dict,
    theme_text: str,
    top_k: int = 5,
) -> List[str]:
    if not isinstance(prompt_library, dict):
        return []
    theme_tokens = _tokenize_theme_text(theme_text)
    if not theme_tokens:
        return []

    scored: List[Tuple[int, str, str]] = []
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
    selected: List[str] = []
    for overlap, key, cue in scored[:top_k]:
        selected.append(f"- {key} (match={overlap}): {cue}")
    return selected


def _load_external_prompt_library() -> Dict:
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


def _extract_bibtex_key(bibtex: str) -> Optional[str]:
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


_DISCLOSURE_BLOCK = r"""
%% ── AI Disclosure ──
%% DO NOT REMOVE THIS SECTION.
\section*{Disclosure}
\label{sec:disclosure}
Portions of this manuscript, including drafting, iterative refinement, and
formatting, were conducted with the assistance of PaperForge, an AI-powered
academic writing pipeline. All experimental results, analysis, and scientific
claims have been reviewed and validated by the authors.
""".strip()


def _ensure_disclosure(tex_text: str) -> str:
    if r"\section*{Disclosure}" in tex_text:
        return tex_text
    anchor = r"\bibliographystyle"
    idx = tex_text.find(anchor)
    if idx != -1:
        return tex_text[:idx] + _DISCLOSURE_BLOCK + "\n\n" + tex_text[idx:]
    anchor2 = r"\end{document}"
    idx2 = tex_text.find(anchor2)
    if idx2 != -1:
        return tex_text[:idx2] + _DISCLOSURE_BLOCK + "\n\n" + tex_text[idx2:]
    return tex_text


def _sanitize_template_tex_file(path: str) -> None:
    with open(path, "r", encoding="utf-8") as f:
        tex_text = f.read()
    sanitized = _sanitize_template_tex_contents(tex_text)
    sanitized = _ensure_disclosure(sanitized)
    if sanitized != tex_text:
        with open(path, "w", encoding="utf-8") as f:
            f.write(sanitized)


# GENERATE LATEX
def generate_latex(
    coder,
    folder_name,
    pdf_file,
    timeout=30,
    num_error_corrections=PAPERFORGE_DEFAULT_NUM_ERROR_CORRECTIONS,
    checkpoint_enabled: bool = False,
    checkpoint_resume_round: int = 0,
):
    folder = osp.abspath(folder_name)
    cwd = osp.join(folder, "latex")  # Fixed potential issue with path
    writeup_file = osp.join(cwd, "template.tex")
    _sanitize_template_tex_file(writeup_file)

    # Check all references are valid and in the references.bib file
    with open(writeup_file, "r") as f:
        tex_text = f.read()
    cites = re.findall(r"\\cite[a-z]*{([^}]*)}", tex_text)
    references_bib = re.search(
        r"\\begin{filecontents}{references.bib}(.*?)\\end{filecontents}",
        tex_text,
        re.DOTALL,
    )
    if references_bib is None:
        print("No references.bib found in template.tex")
        return
    bib_text = references_bib.group(1)
    cites = [cite.strip() for item in cites for cite in item.split(",")]
    for cite in cites:
        if cite not in bib_text:
            print(f"Reference {cite} not found in references.")
            prompt = f"""Reference {cite} not found in references.bib. Is this included under a different name?
If so, please modify the citation in template.tex to match the name in references.bib at the top. Otherwise, remove the cite."""
            coder.run(prompt)

    # Check all included figures are actually in the directory.
    with open(writeup_file, "r") as f:
        tex_text = f.read()
    referenced_figs = re.findall(r"\\includegraphics.*?{(.*?)}", tex_text)
    all_figs = [f for f in os.listdir(folder) if f.endswith(".png")]
    for figure in referenced_figs:
        if figure not in all_figs:
            print(f"Figure {figure} not found in directory.")
            prompt = f"""The image {figure} not found in the directory. The images in the directory are: {all_figs}.
Please ensure that the figure is in the directory and that the filename is correct. Check the notes to see what each figure contains."""
            coder.run(prompt)

    # Remove duplicate figures.
    with open(writeup_file, "r") as f:
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
    with open(writeup_file, "r") as f:
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
    for i in range(num_error_corrections):
        round_idx = i + 1
        if checkpoint_enabled and round_idx <= max(0, checkpoint_resume_round):
            print(f"[writeup][checkpoint] skipping latex_fix round {round_idx}")
            continue
        # Filter trivial bugs in chktex
        chktex_proc = subprocess.run(
            ["chktex", writeup_file, "-q", "-n2", "-n24", "-n13", "-n1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        check_output = chktex_proc.stdout
        if check_output:
            prompt = f"""Please fix the following LaTeX errors in `template.tex` guided by the output of `chktex`:
{check_output}.

Make the minimal fix required and do not remove or change any packages.
Pay attention to any accidental uses of HTML syntax, e.g. </end instead of \\end.
"""
            coder.run(prompt)
            _sanitize_template_tex_file(writeup_file)
            if checkpoint_enabled:
                _save_writeup_checkpoint(
                    folder,
                    stage="latex_fix",
                    current_round=round_idx,
                    writeup_tex_file=writeup_file,
                    snapshot_name=f"template_latex_{round_idx}.tex",
                )
        else:
            break
    return compile_latex(cwd, pdf_file, timeout=timeout)


def compile_latex(cwd, pdf_file, timeout=30):
    print("GENERATING LATEX")

    commands = [
        ["pdflatex", "-interaction=nonstopmode", "template.tex"],
        ["bibtex", "template"],
        ["pdflatex", "-interaction=nonstopmode", "template.tex"],
        ["pdflatex", "-interaction=nonstopmode", "template.tex"],
    ]

    for command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
            print("Standard Output:\n", result.stdout)
            print("Standard Error:\n", result.stderr)
        except subprocess.TimeoutExpired:
            print(f"Latex timed out after {timeout} seconds")
        except subprocess.CalledProcessError as e:
            print(f"Error running command {' '.join(command)}: {e}")

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
) -> Tuple[Optional[str], bool]:
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
    writeup_tex_file = osp.join(folder_name, "latex", "template.tex")
    final_pdf_file = f"{folder_name}/{idea['Name']}.pdf"

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
    _sanitize_template_tex_file(writeup_tex_file)

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
        for section in [
            "Introduction",
            "Background",
            "Method",
            "Experimental Setup",
            "Results",
            "Conclusion",
        ]:
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

            with open(writeup_tex_file, "r") as f:
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

    latex_ok = generate_latex(
        coder,
        folder_name,
        final_pdf_file,
        num_error_corrections=num_error_corrections,
        checkpoint_enabled=checkpoint_enabled,
        checkpoint_resume_round=latex_resume_round,
    )
    _sanitize_template_tex_file(writeup_tex_file)
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
    from aider.coders import Coder
    from aider.models import Model
    from aider.io import InputOutput
    import json

    parser = argparse.ArgumentParser(description="Perform writeup for a project")
    parser.add_argument("--folder", type=str)
    parser.add_argument("--no-writing", action="store_true", help="Only generate")
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-4o-2024-05-13",
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
    args = parser.parse_args()
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
    with open(ideas_file, "r") as f:
        ideas = json.load(f)
    for idea in ideas:
        if idea["Name"] in idea_name:
            print(f"Found idea: {idea['Name']}")
            break
    if idea["Name"] not in idea_name:
        raise ValueError(f"Idea {idea_name} not found")
    fnames = [exp_file, writeup_file, notes]
    io = InputOutput(yes=True, chat_history_file=f"{folder_name}/{idea_name}_aider.txt")
    if args.model == "deepseek-coder-v2-0724":
        main_model = Model("deepseek/deepseek-coder")
    elif args.model == "llama3.1-405b":
        main_model = Model("openrouter/meta-llama/llama-3.1-405b-instruct")
    else:
        main_model = Model(model)
    coder = Coder.create(
        main_model=main_model,
        fnames=fnames,
        io=io,
        stream=False,
        use_git=False,
        edit_format="diff",
    )
    with run_lock(Path(folder_name).resolve(), timeout=30, poll_interval=0.2, verbose=True):
        if args.no_writing:
            generate_latex(coder, args.folder, f"{args.folder}/test.pdf")
        else:
            try:
                perform_writeup(idea, folder_name, coder, client, client_model, engine=args.engine)
            except Exception as e:
                print(f"Failed to perform writeup: {e}")
