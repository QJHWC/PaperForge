from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Sequence

from engine.mvp_workflow import slugify
from engine.workspace_config import load_workspace_config, save_workspace_config

SOURCE_DRAFTS_DIRNAME = "source_drafts"
TEMPLATE_LIBRARY_DIRNAME = "template_library"
MIGRATIONS_DIRNAME = "migrations"
RECYCLE_BIN_DIRNAME = "recycle_bin"
WORKSPACE_HISTORY_FILENAME = "workspace_history.json"
PAPERFORGE_TEMPLATE_MANIFEST = "paperforge_template.json"
MARKER_PREFIX = "% PAPERFORGE_FIELD:"


@dataclass
class MigrationContent:
    source_profile: str
    title: str
    authors: str
    abstract: str
    keywords: str
    body: str
    bibliography_kind: str
    bibliography_payload: str


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_relpath(path_text: str) -> PurePosixPath:
    candidate = PurePosixPath(str(path_text).replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"unsafe path: {path_text}")
    return candidate


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _next_id(prefix: str, name: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{slugify(name, default=prefix)}_{uuid.uuid4().hex[:6]}"


def source_drafts_dir(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve() / SOURCE_DRAFTS_DIRNAME


def template_library_dir(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve() / TEMPLATE_LIBRARY_DIRNAME


def migrations_dir(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve() / MIGRATIONS_DIRNAME


def recycle_bin_dir(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve() / RECYCLE_BIN_DIRNAME


def workspace_history_path(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve() / WORKSPACE_HISTORY_FILENAME


def _manifest_list(parent: Path) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not parent.exists():
        return items
    for child in sorted(parent.iterdir()):
        manifest = child / "manifest.json"
        if not manifest.exists():
            continue
        try:
            payload = _read_json(manifest)
        except Exception:
            continue
        payload["path"] = str(child)
        items.append(payload)
    items.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return items


def refresh_workspace_history(workspace: str | Path) -> Dict[str, Any]:
    workspace_path = Path(workspace).expanduser().resolve()
    frontend_runs = []
    frontend_dir = workspace_path / "artifacts" / "frontend_runs"
    if frontend_dir.exists():
        for path in sorted(frontend_dir.glob("*.json"), reverse=True):
            try:
                frontend_runs.append(_read_json(path))
            except Exception:
                continue
    payload = {
        "updated_at": now_iso(),
        "source_drafts": _manifest_list(source_drafts_dir(workspace_path)),
        "templates": _manifest_list(template_library_dir(workspace_path)),
        "migrations": _manifest_list(migrations_dir(workspace_path)),
        "deleted_items": _manifest_list(recycle_bin_dir(workspace_path)),
        "frontend_runs": frontend_runs,
    }
    _write_json(workspace_history_path(workspace_path), payload)
    return payload


def load_workspace_history(workspace: str | Path) -> Dict[str, Any]:
    path = workspace_history_path(workspace)
    if not path.exists():
        return refresh_workspace_history(workspace)
    try:
        return _read_json(path)
    except Exception:
        return refresh_workspace_history(workspace)


def _normalize_uploaded_files(
    files: Sequence[Dict[str, Any]],
    *,
    strip_common_root: bool,
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    relpaths = [_safe_relpath(item["filename"]) for item in files if item.get("filename")]
    common_root: str | None = None
    if strip_common_root and relpaths:
        first_parts = [parts.parts[0] for parts in relpaths if len(parts.parts) > 1]
        if first_parts and len(first_parts) == len(relpaths) and len(set(first_parts)) == 1:
            common_root = first_parts[0]
    for item in files:
        rel = _safe_relpath(item["filename"])
        if common_root and len(rel.parts) > 1 and rel.parts[0] == common_root:
            rel = PurePosixPath(*rel.parts[1:])
        normalized.append({"filename": rel.as_posix(), "content": item["content"]})
    return normalized


def _detect_source_profile(tex_text: str) -> str:
    lowered = tex_text.lower()
    if "paperforge_field:title_start" in lowered and "cjc" in lowered:
        return "cjc"
    if "paperforge_field:title_start" in lowered and ("ieeecolor" in lowered or "ieeetran" in lowered):
        return "ieee_tii"
    if "cjc" in tex_text or r"\documentclass[10.5pt,compsoc]{CjC}" in tex_text:
        return "cjc"
    if "ieeecolor" in lowered or "ieeetran" in lowered or "ieeekeywords" in lowered:
        return "ieee_tii"
    return "generic"


def _extract_marker_block(tex_text: str, field_name: str) -> str | None:
    pattern = re.compile(
        rf"{re.escape(MARKER_PREFIX + field_name + '_start')}\s*(.*?)\s*{re.escape(MARKER_PREFIX + field_name + '_end')}",
        re.DOTALL,
    )
    match = pattern.search(tex_text)
    return match.group(1).strip() if match else None


def _extract_command_body(tex_text: str, command: str) -> str | None:
    match = re.search(rf"\\{command}\{{(.*?)\}}", tex_text, re.DOTALL)
    return match.group(1).strip() if match else None


def _extract_environment_body(tex_text: str, env_name: str) -> str | None:
    match = re.search(rf"\\begin\{{{env_name}\}}(.*?)\\end\{{{env_name}\}}", tex_text, re.DOTALL)
    return match.group(1).strip() if match else None


def _extract_body(tex_text: str) -> str:
    marker = _extract_marker_block(tex_text, "body")
    if marker is not None:
        return marker
    start = 0
    for token in (r"\end{IEEEkeywords}", r"\end{abstract}", r"\maketitle", r"\begin{document}"):
        idx = tex_text.find(token)
        if idx != -1:
            start = max(start, idx + len(token))
    end_candidates = [
        idx
        for idx in [
            tex_text.find(r"\begin{thebibliography}"),
            tex_text.find(r"\bibliographystyle"),
            tex_text.find(r"\section*{Disclosure}"),
            tex_text.find(r"\end{document}"),
        ]
        if idx != -1
    ]
    end = min(end_candidates) if end_candidates else len(tex_text)
    return tex_text[start:end].strip()


def _extract_thebibliography(tex_text: str) -> str | None:
    match = re.search(r"(\\begin\{thebibliography\}\{.*?\\end\{thebibliography\})", tex_text, re.DOTALL)
    return match.group(1).strip() if match else None


def _extract_bibtex_from_source(source_dir: Path, tex_text: str) -> str | None:
    embedded = re.search(
        r"\\begin\{filecontents\*?\}\{references\.bib\}(.*?)\\end\{filecontents\*?\}",
        tex_text,
        re.DOTALL,
    )
    if embedded:
        return embedded.group(1).strip()
    bibliography_targets = re.findall(r"\\bibliography\{([^}]*)\}", tex_text)
    if not bibliography_targets:
        return None
    candidates = []
    for group in bibliography_targets:
        for item in group.split(","):
            name = item.strip()
            if not name:
                continue
            filename = name if name.endswith(".bib") else f"{name}.bib"
            candidates.extend(
                [
                    source_dir / filename,
                    source_dir / "assets" / filename,
                    *list((source_dir / "assets").rglob(filename)),
                ]
            )
    contents = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen or not candidate.exists():
            continue
        seen.add(candidate)
        contents.append(candidate.read_text(encoding="utf-8"))
    if not contents:
        return None
    return "\n".join(contents).strip()


def extract_migration_content(source_draft_dir: str | Path) -> MigrationContent:
    source_dir = Path(source_draft_dir).expanduser().resolve()
    tex_path = source_dir / "source.tex"
    tex_text = tex_path.read_text(encoding="utf-8")
    source_profile = _detect_source_profile(tex_text)
    title = _extract_marker_block(tex_text, "title") or _extract_command_body(tex_text, "title") or "Untitled Draft"
    authors = _extract_marker_block(tex_text, "author") or _extract_command_body(tex_text, "author") or "Anonymous Authors"
    abstract = _extract_marker_block(tex_text, "abstract") or _extract_environment_body(tex_text, "abstract") or ""
    keywords = (
        _extract_marker_block(tex_text, "keywords")
        or _extract_environment_body(tex_text, "IEEEkeywords")
        or ""
    )
    bibliography_block = _extract_thebibliography(tex_text)
    if bibliography_block:
        bibliography_kind = "thebibliography"
        bibliography_payload = bibliography_block
    else:
        bibliography_payload = _extract_bibtex_from_source(source_dir, tex_text) or ""
        bibliography_kind = "bibtex" if bibliography_payload else "none"
    return MigrationContent(
        source_profile=source_profile,
        title=title.strip(),
        authors=authors.strip(),
        abstract=abstract.strip(),
        keywords=keywords.strip(),
        body=_extract_body(tex_text),
        bibliography_kind=bibliography_kind,
        bibliography_payload=bibliography_payload,
    )


def detect_template_profile(template_files_dir: str | Path) -> Dict[str, Any]:
    files_dir = Path(template_files_dir).expanduser().resolve()
    custom_manifest_path = files_dir / PAPERFORGE_TEMPLATE_MANIFEST
    tex_candidates = sorted(files_dir.rglob("*.tex"))
    main_tex = None
    profile = "unknown"
    migration_ready = False
    placeholders = None

    if custom_manifest_path.exists():
        payload = _read_json(custom_manifest_path)
        profile = str(payload.get("profile") or "custom")
        main_tex_value = payload.get("main_tex")
        if isinstance(main_tex_value, str) and (files_dir / main_tex_value).exists():
            main_tex = main_tex_value
        placeholders = payload.get("placeholders")
        required_keys = {"title", "author", "abstract", "keywords", "body", "bibliography"}
        migration_ready = (
            main_tex is not None
            and isinstance(placeholders, dict)
            and required_keys.issubset(set(placeholders.keys()))
        )
    elif (files_dir / "CjC.cls").exists():
        profile = "cjc"
        preferred = [p for p in tex_candidates if p.name.lower() == "cjc_template_tex.tex"]
        main_tex = (preferred[0] if preferred else (tex_candidates[0] if tex_candidates else None))
        if main_tex is not None:
            main_tex = main_tex.relative_to(files_dir).as_posix()
            migration_ready = True
    elif (files_dir / "ieeecolor.cls").exists() and (files_dir / "generic.sty").exists():
        profile = "ieee_tii"
        preferred = [p for p in tex_candidates if "template" in p.name.lower() or "tii" in p.name.lower()]
        main_tex = (preferred[0] if preferred else (tex_candidates[0] if tex_candidates else None))
        if main_tex is not None:
            main_tex = main_tex.relative_to(files_dir).as_posix()
            migration_ready = True
    elif tex_candidates:
        main_tex = tex_candidates[0].relative_to(files_dir).as_posix()

    return {
        "main_tex": main_tex,
        "profile": profile,
        "migration_ready": migration_ready,
        "placeholders": placeholders,
    }


def import_source_draft(workspace: str | Path, files: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    workspace_path = Path(workspace).expanduser().resolve()
    normalized = _normalize_uploaded_files(files, strip_common_root=False)
    tex_files = [item for item in normalized if item["filename"].lower().endswith(".tex")]
    pdf_files = [item for item in normalized if item["filename"].lower().endswith(".pdf")]
    if len(tex_files) != 1:
        raise ValueError("source draft import requires exactly one .tex file")
    if len(pdf_files) > 1:
        raise ValueError("source draft import accepts at most one preview .pdf")

    source_name = Path(tex_files[0]["filename"]).stem
    draft_id = _next_id("draft", source_name)
    target_dir = source_drafts_dir(workspace_path) / draft_id
    assets_dir = target_dir / "assets"
    target_dir.mkdir(parents=True, exist_ok=True)

    source_path = target_dir / "source.tex"
    source_path.write_bytes(tex_files[0]["content"])

    preview_name = None
    if pdf_files:
        preview_path = target_dir / "preview.pdf"
        preview_path.write_bytes(pdf_files[0]["content"])
        preview_name = preview_path.name

    asset_paths: List[str] = []
    for item in normalized:
        if item in tex_files or item in pdf_files:
            continue
        rel = _safe_relpath(item["filename"])
        asset_target = assets_dir / rel
        asset_target.parent.mkdir(parents=True, exist_ok=True)
        asset_target.write_bytes(item["content"])
        asset_paths.append(rel.as_posix())

    content = extract_migration_content(target_dir)
    manifest = {
        "draft_id": draft_id,
        "draft_name": source_name,
        "source_tex": "source.tex",
        "preview_pdf": preview_name,
        "assets": sorted(asset_paths),
        "source_profile": content.source_profile,
        "created_at": now_iso(),
    }
    _write_json(target_dir / "manifest.json", manifest)
    config = load_workspace_config(workspace_path)
    config["active_source_draft_id"] = draft_id
    save_workspace_config(workspace_path, config)
    refresh_workspace_history(workspace_path)
    return manifest


def import_template_directory(workspace: str | Path, files: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    workspace_path = Path(workspace).expanduser().resolve()
    normalized = _normalize_uploaded_files(files, strip_common_root=True)
    if not normalized:
        raise ValueError("template import requires at least one file")
    root_names = [PurePosixPath(item["filename"]).parts[0] for item in normalized if PurePosixPath(item["filename"]).parts]
    template_name = slugify(root_names[0] if root_names else "template", default="template")
    template_id = _next_id("template", template_name)
    target_dir = template_library_dir(workspace_path) / template_id
    files_dir = target_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    for item in normalized:
        rel = _safe_relpath(item["filename"])
        target = files_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item["content"])

    profile_info = detect_template_profile(files_dir)
    manifest = {
        "template_id": template_id,
        "template_name": template_name,
        "main_tex": profile_info["main_tex"],
        "profile": profile_info["profile"],
        "migration_ready": profile_info["migration_ready"],
        "created_at": now_iso(),
    }
    _write_json(target_dir / "manifest.json", manifest)
    config = load_workspace_config(workspace_path)
    config["active_template_id"] = template_id
    save_workspace_config(workspace_path, config)
    refresh_workspace_history(workspace_path)
    return manifest


def _copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _copy_source_assets_to_output(source_draft_dir: Path, output_dir: Path) -> None:
    assets_dir = source_draft_dir / "assets"
    if not assets_dir.exists():
        return
    for file_path in assets_dir.rglob("*"):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(assets_dir)
        target = output_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, target)


def _field_block(name: str, payload: str) -> str:
    return f"{MARKER_PREFIX}{name}_start\n{payload.strip()}\n{MARKER_PREFIX}{name}_end"


def _render_bibliography(content: MigrationContent) -> str:
    if content.bibliography_kind == "thebibliography" and content.bibliography_payload:
        return content.bibliography_payload.strip()
    if content.bibliography_kind == "bibtex" and content.bibliography_payload:
        return (
            "\\begin{filecontents*}{references.bib}\n"
            + content.bibliography_payload.strip()
            + "\n\\end{filecontents*}\n\n\\bibliographystyle{plain}\n\\bibliography{references}"
        )
    return ""


def render_ieee_tii_output(content: MigrationContent) -> str:
    keywords = content.keywords or "模板迁移, LaTeX, PaperForge"
    bibliography = _render_bibliography(content)
    title_block = _field_block("title", f"\\title{{{content.title}}}")
    author_block = _field_block("author", f"\\author{{{content.authors}}}")
    abstract_block = _field_block("abstract", content.abstract)
    keyword_block = _field_block("keywords", keywords)
    body_block = _field_block("body", content.body)
    return (
        r"""\documentclass[journal,twoside,web]{{ieeecolor}}
\usepackage{{generic}}
\usepackage{{cite}}
\usepackage{{amsmath}}
\usepackage{{newtxmath}}
\usepackage{{algorithmic}}
\usepackage{{graphicx}}
\usepackage{{CJKutf8}}
\usepackage{{textcomp}}
\usepackage{{booktabs}}
\usepackage{{multirow}}
\usepackage{{url}}
\usepackage{{tabularx}}
\graphicspath{{{{./}}{{figures/}}}}

\begin{{document}}
\begin{{CJK*}}{{UTF8}}{{gbsn}}
{title_block}
{author_block}
\maketitle
\begin{{abstract}}
{abstract_block}
\end{{abstract}}
\begin{{IEEEkeywords}}
{keyword_block}
\end{{IEEEkeywords}}
{body_block}
{bibliography}
\end{{CJK*}}
\end{{document}}
""".format(
            title_block=title_block,
            author_block=author_block,
            abstract_block=abstract_block,
            keyword_block=keyword_block,
            body_block=body_block,
            bibliography=bibliography,
        ).strip()
        + "\n"
    )


def render_cjc_output(content: MigrationContent) -> str:
    keywords = content.keywords or "模板迁移；LaTeX；PaperForge"
    bibliography = _render_bibliography(content)
    title_marker = _field_block("title", content.title)
    author_marker = _field_block("author", content.authors)
    abstract_marker = _field_block("abstract", content.abstract)
    keyword_marker = _field_block("keywords", keywords)
    body_marker = _field_block("body", content.body)
    return (
        r"""\documentclass[10.5pt,compsoc]{{CjC}}
\usepackage{{CJKutf8}}
\usepackage{{graphicx}}
\usepackage{{footmisc}}
\usepackage{{subfigure}}
\usepackage{{url}}
\usepackage{{multirow}}
\usepackage[noadjust]{{cite}}
\usepackage{{amsmath,amssymb,amsfonts}}
\usepackage{{booktabs}}
\usepackage{{color}}
\usepackage{{caption}}
\usepackage{{xcolor,stfloats}}
\usepackage{{comment}}
\usepackage{{captionhack}}
\usepackage{{epstopdf}}
\graphicspath{{{{./}}{{figures/}}}}
\begin{{document}}
\begin{{CJK*}}{{UTF8}}{{gbsn}}
\begin{{center}}
{{\zihao{{2}}\textbf{{{title}}}}}
\vskip 5mm
{{\zihao{{4}} {authors}}}
\end{{center}}
\vskip 4mm
\noindent\textbf{{摘\quad 要}}\quad {abstract}
\par\vskip 2mm
\noindent\textbf{{关键词}}\quad {keywords}
\par\vskip 6mm
{title_marker}
{author_marker}
{abstract_marker}
{keyword_marker}
{body_marker}
{bibliography}
\end{{CJK*}}
\end{{document}}
""".format(
            title=content.title,
            authors=content.authors,
            abstract=content.abstract,
            keywords=keywords,
            title_marker=title_marker,
            author_marker=author_marker,
            abstract_marker=abstract_marker,
            keyword_marker=keyword_marker,
            body_marker=body_marker,
            bibliography=bibliography,
        ).strip()
        + "\n"
    )


def render_custom_template_output(template_dir: Path, content: MigrationContent) -> str:
    files_dir = template_dir / "files"
    custom_manifest = _read_json(files_dir / PAPERFORGE_TEMPLATE_MANIFEST)
    placeholders = custom_manifest["placeholders"]
    main_tex = files_dir / str(custom_manifest["main_tex"])
    text = main_tex.read_text(encoding="utf-8")
    replacements = {
        placeholders["title"]: content.title,
        placeholders["author"]: content.authors,
        placeholders["abstract"]: content.abstract,
        placeholders["keywords"]: content.keywords,
        placeholders["body"]: content.body,
        placeholders["bibliography"]: _render_bibliography(content),
    }
    for needle, value in replacements.items():
        text = text.replace(str(needle), str(value))
    return text


def render_latex_project(output_dir: Path, tex_name: str = "main.tex", pdf_name: str = "main.pdf") -> Path:
    compile_log = output_dir / "compile_output.log"
    tex_path = output_dir / tex_name
    tex_text = tex_path.read_text(encoding="utf-8")
    commands = [["pdflatex", "-interaction=nonstopmode", tex_name]]
    if r"\bibliography{" in tex_text:
        commands.append(["bibtex", Path(tex_name).stem])
    commands.extend([["pdflatex", "-interaction=nonstopmode", tex_name], ["pdflatex", "-interaction=nonstopmode", tex_name]])
    chunks: List[str] = []
    for command in commands:
        result = subprocess.run(
            command,
            cwd=str(output_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        chunks.append(
            "## Command: {cmd}\n\n### STDOUT\n{stdout}\n\n### STDERR\n{stderr}\n".format(
                cmd=" ".join(command),
                stdout=result.stdout,
                stderr=result.stderr,
            )
        )
    compile_log.write_text("\n".join(chunks), encoding="utf-8")
    pdf_path = output_dir / pdf_name
    built_pdf = output_dir / f"{Path(tex_name).stem}.pdf"
    if not built_pdf.exists():
        raise RuntimeError(f"rendered PDF missing: {built_pdf}")
    if built_pdf != pdf_path:
        shutil.move(str(built_pdf), str(pdf_path))
    return pdf_path


def _move_to_recycle_bin(workspace: Path, item_path: Path, *, kind: str, item_id: str, original_relpath: str) -> Dict[str, Any]:
    recycle_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{kind}_{slugify(item_id, default=kind)}"
    recycle_target = recycle_bin_dir(workspace) / recycle_id
    recycle_target.parent.mkdir(parents=True, exist_ok=True)
    recycle_target.mkdir(parents=True, exist_ok=True)
    payload_path = recycle_target / item_path.name
    shutil.move(str(item_path), str(payload_path))
    manifest = {
        "recycle_id": recycle_id,
        "original_kind": kind,
        "original_item_id": item_id,
        "original_relpath": original_relpath,
        "payload_name": item_path.name,
        "is_directory": payload_path.is_dir(),
        "created_at": now_iso(),
    }
    _write_json(recycle_target / "manifest.json", manifest)
    refresh_workspace_history(workspace)
    return manifest


def recycle_workspace_artifact(workspace: str | Path, kind: str, item_id: str) -> Dict[str, Any]:
    workspace_path = Path(workspace).expanduser().resolve()
    if kind == "source_drafts":
        item_path = source_drafts_dir(workspace_path) / item_id
        rel = f"{SOURCE_DRAFTS_DIRNAME}/{item_id}"
    elif kind == "templates":
        item_path = template_library_dir(workspace_path) / item_id
        rel = f"{TEMPLATE_LIBRARY_DIRNAME}/{item_id}"
    elif kind == "migrations":
        item_path = migrations_dir(workspace_path) / item_id
        rel = f"{MIGRATIONS_DIRNAME}/{item_id}"
    else:
        raise ValueError(f"unsupported artifact kind: {kind}")
    if not item_path.exists():
        raise FileNotFoundError(item_id)
    return _move_to_recycle_bin(workspace_path, item_path, kind=kind, item_id=item_id, original_relpath=rel)


def restore_recycled_artifact(workspace: str | Path, recycle_id: str) -> Dict[str, Any]:
    workspace_path = Path(workspace).expanduser().resolve()
    recycle_path = recycle_bin_dir(workspace_path) / recycle_id
    manifest_path = recycle_path / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(recycle_id)
    manifest = _read_json(manifest_path)
    original_relpath = str(manifest["original_relpath"])
    destination = workspace_path / original_relpath
    payload_path = recycle_path / str(manifest.get("payload_name") or "")
    candidate = destination
    counter = 1
    while candidate.exists():
        suffix = destination.suffix if destination.is_file() or destination.suffix else ""
        candidate = destination.with_name(f"{destination.stem}_restored_{counter}{suffix}")
        counter += 1
    candidate.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(payload_path), str(candidate))
    shutil.rmtree(recycle_path, ignore_errors=True)
    refresh_workspace_history(workspace_path)
    return {
        "restored_to": str(candidate),
        "original_kind": manifest.get("original_kind"),
        "original_item_id": manifest.get("original_item_id"),
    }


def _copy_generated_outputs_to_workspace(workspace: Path, template_id: str, output_tex: Path, output_pdf: Path) -> Dict[str, str]:
    target_tex = workspace / f"migrated_{template_id}.tex"
    target_pdf = workspace / f"migrated_{template_id}.pdf"
    for target in (target_tex, target_pdf):
        if target.exists():
            _move_to_recycle_bin(
                workspace,
                target,
                kind="generated_files",
                item_id=target.name,
                original_relpath=target.name,
            )
    shutil.copy2(output_tex, target_tex)
    shutil.copy2(output_pdf, target_pdf)
    return {"tex": str(target_tex), "pdf": str(target_pdf)}


def run_template_migration(
    workspace: str | Path,
    source_draft_id: str,
    template_id: str,
    output_name: str,
) -> Dict[str, Any]:
    workspace_path = Path(workspace).expanduser().resolve()
    source_dir = source_drafts_dir(workspace_path) / source_draft_id
    template_dir = template_library_dir(workspace_path) / template_id
    if not (source_dir / "manifest.json").exists():
        raise FileNotFoundError(f"source draft not found: {source_draft_id}")
    if not (template_dir / "manifest.json").exists():
        raise FileNotFoundError(f"template not found: {template_id}")

    source_manifest = _read_json(source_dir / "manifest.json")
    template_manifest = _read_json(template_dir / "manifest.json")
    if not template_manifest.get("migration_ready"):
        raise ValueError(f"template not ready for migration: {template_id}")

    content = extract_migration_content(source_dir)
    migration_id = _next_id("migration", output_name or f"{source_draft_id}_{template_id}")
    migration_root = migrations_dir(workspace_path) / migration_id
    source_snapshot = migration_root / "source_snapshot"
    template_snapshot = migration_root / "template_snapshot"
    output_dir = migration_root / "output"
    _copy_tree(source_dir, source_snapshot)
    _copy_tree(template_dir, template_snapshot)
    output_dir.mkdir(parents=True, exist_ok=True)
    _copy_tree(template_dir / "files", output_dir)
    _copy_source_assets_to_output(source_dir, output_dir)

    profile = str(template_manifest.get("profile") or "unknown")
    if profile == "ieee_tii":
        rendered = render_ieee_tii_output(content)
    elif profile == "cjc":
        rendered = render_cjc_output(content)
    elif profile == "custom":
        rendered = render_custom_template_output(template_dir, content)
    else:
        raise ValueError(f"unsupported template profile: {profile}")

    output_tex = output_dir / "main.tex"
    output_tex.write_text(rendered, encoding="utf-8")
    output_pdf = render_latex_project(output_dir, tex_name="main.tex", pdf_name="main.pdf")
    copied = _copy_generated_outputs_to_workspace(workspace_path, template_id, output_tex, output_pdf)

    manifest = {
        "migration_id": migration_id,
        "source_draft_id": source_draft_id,
        "template_id": template_id,
        "output_name": output_name,
        "source_profile": content.source_profile,
        "target_profile": profile,
        "created_at": now_iso(),
        "status": "completed",
        "output": {
            "main_tex": "output/main.tex",
            "main_pdf": "output/main.pdf",
            "workspace_tex": Path(copied["tex"]).resolve().relative_to(workspace_path).as_posix(),
            "workspace_pdf": Path(copied["pdf"]).resolve().relative_to(workspace_path).as_posix(),
        },
    }
    _write_json(migration_root / "manifest.json", manifest)
    config = load_workspace_config(workspace_path)
    config["active_source_draft_id"] = source_manifest["draft_id"]
    config["active_template_id"] = template_manifest["template_id"]
    config["latest_migration_id"] = migration_id
    save_workspace_config(workspace_path, config)
    refresh_workspace_history(workspace_path)
    return manifest
