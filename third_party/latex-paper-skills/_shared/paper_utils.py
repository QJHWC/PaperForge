#!/usr/bin/env python3
"""Shared helpers for IEEE paper plan scripts.

This module lives in ``_shared/`` so that both ``arxiv-paper-writer`` and
``empirical-paper-writer`` can import it without fragile cross-skill
``sys.path`` hacks.  Skill-specific path helpers (``get_assets_dir``, etc.)
belong in the consuming script, not here.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
_SECTION_RE = re.compile(r"\\(?P<level>section|subsection|subsubsection)\*?\{(?P<title>[^{}]+)\}")
_CITE_COMMAND_RE = re.compile(r"\\(?P<command>[Cc]ite[a-zA-Z]*)")

DEFAULT_SOURCE_POLICY = {
    "require_published_version_if_available": True,
    "min_tier_ab_ratio_core": 0.8,
    "min_tier_ab_ratio_standard": 0.6,
    "max_tier_c_ratio_core": 0.2,
    "preferred_venue_boost": "soft",
}

DEFAULT_SECTION_ORDER = [
    "Introduction",
    "Background and Preliminaries",
    "Building Blocks",
    "Frontier Models and Systems",
    "Evaluation and Benchmarks",
    "Safety and Provenance",
    "Open Challenges and Conclusion",
]


def slugify(text: str) -> str:
    """Convert text to a slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:60] or "paper"


def validate_slug(slug: str) -> None:
    """Validate a slug format."""
    if not slug or not _SLUG_RE.match(slug):
        raise ValueError(
            "Invalid slug. Use lower-case, hyphen-delimited names (e.g., transformer-vision-review)."
        )


def validate_timestamp(timestamp: str) -> None:
    """Validate a timestamp format."""
    if not _TIMESTAMP_RE.match(timestamp):
        raise ValueError("Timestamp must be in YYYY-MM-DD_HH-mm-ss format.")


def now_timestamp() -> str:
    """Get current timestamp in plan format."""
    return datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")


def now_iso() -> str:
    """Get current timestamp in ISO format."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def build_plan_filename(timestamp: str, slug: str) -> str:
    """Build a plan filename from timestamp and slug."""
    validate_timestamp(timestamp)
    validate_slug(slug)
    return f"{timestamp}-{slug}.md"


def build_issues_filename(timestamp: str, slug: str) -> str:
    """Build an issues filename from timestamp and slug."""
    validate_timestamp(timestamp)
    validate_slug(slug)
    return f"{timestamp}-{slug}.csv"


def format_yaml_value(value: str) -> str:
    """Format a value for YAML frontmatter."""
    if value is None:
        return ""
    needs_quotes = (
        not value
        or value.strip() != value
        or "\n" in value
        or any(ch in value for ch in (":", "#", "{", "}", "[", "]", ","))
    )
    if needs_quotes:
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def check_latex_available() -> dict:
    """Check if LaTeX tools are available on the system."""
    tools = {
        "pdflatex": shutil.which("pdflatex"),
        "bibtex": shutil.which("bibtex"),
        "latexmk": shutil.which("latexmk"),
    }

    available = tools["pdflatex"] is not None and tools["bibtex"] is not None

    return {
        "available": available,
        "pdflatex": tools["pdflatex"],
        "bibtex": tools["bibtex"],
        "latexmk": tools["latexmk"],
        "recommended": "latexmk" if tools["latexmk"] else "pdflatex+bibtex" if available else None,
    }


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    text = str(value)
    if (
        not text
        or text.strip() != text
        or text.lower() in {"true", "false", "null", "none"}
        or any(ch in text for ch in [":", "#", "[", "]", "{", "}"])
    ):
        return _yaml_quote(text)
    return text


def dump_simple_yaml(data: Any, indent: int = 0) -> str:
    """Dump a small subset of YAML for config-like data."""

    lines: list[str] = []

    def emit(value: Any, level: int, key: str | None = None) -> None:
        prefix = " " * level
        if isinstance(value, dict):
            if key is not None:
                lines.append(f"{prefix}{key}:")
                prefix = " " * (level + 2)
            for nested_key, nested_value in value.items():
                if isinstance(nested_value, (dict, list)):
                    emit(nested_value, level + (2 if key is not None else 0), str(nested_key))
                else:
                    lines.append(
                        f"{prefix}{nested_key}: {_yaml_scalar(nested_value)}"
                    )
            if not value and key is not None:
                lines[-1] = f"{prefix[:-2]}{key}: {{}}"
            return

        if isinstance(value, list):
            if key is not None:
                lines.append(f"{prefix}{key}:")
                prefix = " " * (level + 2)
            if not value:
                lines[-1] = f"{prefix[:-2]}{key}: []" if key is not None else f"{prefix}[]"
                return
            for item in value:
                if isinstance(item, (dict, list)):
                    lines.append(f"{prefix}-")
                    emit(item, level + (4 if key is not None else 2))
                else:
                    lines.append(f"{prefix}- {_yaml_scalar(item)}")
            return

        if key is None:
            lines.append(f"{prefix}{_yaml_scalar(value)}")
        else:
            lines.append(f"{prefix}{key}: {_yaml_scalar(value)}")

    if isinstance(data, dict):
        for root_key, root_value in data.items():
            emit(root_value, indent, str(root_key))
    else:
        emit(data, indent)
    return "\n".join(lines).rstrip() + "\n"


def _parse_yaml_scalar(value: str) -> Any:
    value = value.strip()
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none", "~"}:
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def parse_simple_yaml(text: str) -> Any:
    """Parse a small YAML subset produced by dump_simple_yaml."""

    raw_lines = [line.rstrip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not raw_lines:
        return {}

    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def parse_block(index: int, indent: int, container_type: str) -> tuple[Any, int]:
        if container_type == "dict":
            container: Any = {}
        else:
            container = []

        while index < len(raw_lines):
            line = raw_lines[index]
            current_indent = indent_of(line)
            if current_indent < indent:
                break
            if current_indent > indent:
                raise ValueError(f"Unexpected indentation at line: {line}")

            stripped = line[current_indent:]
            if container_type == "list":
                if not stripped.startswith("-"):
                    break
                item_text = stripped[1:].strip()
                if item_text:
                    container.append(_parse_yaml_scalar(item_text))
                    index += 1
                    continue

                next_index = index + 1
                if next_index >= len(raw_lines) or indent_of(raw_lines[next_index]) <= current_indent:
                    container.append(None)
                    index += 1
                    continue

                next_stripped = raw_lines[next_index][indent_of(raw_lines[next_index]):]
                nested_type = "list" if next_stripped.startswith("-") else "dict"
                parsed, index = parse_block(next_index, current_indent + 2, nested_type)
                container.append(parsed)
                continue

            if stripped.startswith("-"):
                raise ValueError(f"Unexpected list item at line: {line}")

            key, separator, value_text = stripped.partition(":")
            if separator != ":":
                raise ValueError(f"Invalid YAML line: {line}")

            key = key.strip()
            value_text = value_text.strip()
            if value_text:
                container[key] = _parse_yaml_scalar(value_text)
                index += 1
                continue

            next_index = index + 1
            if next_index >= len(raw_lines) or indent_of(raw_lines[next_index]) <= current_indent:
                container[key] = {}
                index += 1
                continue

            next_stripped = raw_lines[next_index][indent_of(raw_lines[next_index]):]
            nested_type = "list" if next_stripped.startswith("-") else "dict"
            parsed, index = parse_block(next_index, current_indent + 2, nested_type)
            container[key] = parsed

        return container, index

    parsed, next_index = parse_block(0, 0, "dict")
    if next_index != len(raw_lines):
        raise ValueError("Unparsed trailing YAML content")
    return parsed


def read_simple_yaml_file(path: Path) -> dict[str, Any]:
    """Read a small YAML config file."""
    if not path.exists():
        return {}
    parsed = parse_simple_yaml(path.read_text(encoding="utf-8"))
    if isinstance(parsed, dict):
        return parsed
    raise ValueError(f"Expected mapping in YAML file: {path}")


def write_simple_yaml_file(path: Path, data: dict[str, Any]) -> None:
    """Write a small YAML config file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_simple_yaml(data), encoding="utf-8")


def deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge dictionaries, preferring override values."""
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def get_project_config_path(project_dir: Path) -> Path:
    """Return the project config path."""
    return project_dir / "paper.config.yaml"


def get_style_profile_path(project_dir: Path) -> Path:
    """Return the style profile path."""
    return project_dir / "notes" / "style-profile.yaml"


def build_default_paper_config(
    *,
    topic: str,
    workflow_mode: str = "standard",
    target_venue: str = "",
    preferred_venues: list[str] | None = None,
    style_mode: str = "neutral",
    style_anchor_papers: list[str] | None = None,
) -> dict[str, Any]:
    """Build the default project config."""
    preferred = [venue.strip() for venue in (preferred_venues or []) if venue.strip()]
    if target_venue.strip() and target_venue.strip() not in preferred:
        preferred.insert(0, target_venue.strip())

    return {
        "topic": topic.strip(),
        "workflow_mode": workflow_mode.strip() or "standard",
        "target_venue": target_venue.strip(),
        "preferred_venues": preferred,
        "style_mode": style_mode.strip() or "neutral",
        "style_anchor_papers": [paper.strip() for paper in (style_anchor_papers or []) if paper.strip()],
        "runtime": {
            "conda_env": "",
            "python": "",
        },
        "source_policy": dict(DEFAULT_SOURCE_POLICY),
    }


def ensure_paper_config(
    *,
    project_dir: Path,
    topic: str,
    workflow_mode: str = "standard",
    target_venue: str = "",
    preferred_venues: list[str] | None = None,
    style_mode: str = "neutral",
    style_anchor_papers: list[str] | None = None,
) -> tuple[Path, dict[str, Any], bool]:
    """Create or normalize paper.config.yaml for a project."""
    path = get_project_config_path(project_dir)
    defaults = build_default_paper_config(
        topic=topic,
        workflow_mode=workflow_mode,
        target_venue=target_venue,
        preferred_venues=preferred_venues,
        style_mode=style_mode,
        style_anchor_papers=style_anchor_papers,
    )

    existed = path.exists()
    if existed:
        current = read_simple_yaml_file(path)
        config = deep_merge_dicts(defaults, current)
    else:
        config = defaults
    write_simple_yaml_file(path, config)
    return path, config, existed


def load_paper_config(project_dir: Path) -> dict[str, Any]:
    """Load paper.config.yaml if present, with defaults filled in."""
    path = get_project_config_path(project_dir)
    current = read_simple_yaml_file(path)
    defaults = build_default_paper_config(topic=str(current.get("topic") or ""))
    return deep_merge_dicts(defaults, current)


def build_default_style_profile(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the default venue style profile."""
    config = config or {}
    venue = str(config.get("target_venue") or "")
    anchors = config.get("style_anchor_papers") or []
    return {
        "venue": venue or "TBD",
        "anchor_papers": [str(item) for item in anchors],
        "canonical_section_order": list(DEFAULT_SECTION_ORDER),
        "abstract_word_range": {"min": 150, "max": 250},
        "citation_density_target": {"min_per_section": 8, "max_uncited_sentence_run": 2},
        "figure_table_density_target": {
            "min_total": 5,
            "prefer_double_column_only_when_needed": True,
        },
        "caption_style": "Evidence-first captions with citations when source content is adapted.",
        "tone_rules": [
            "Expert, precise, evidence-first",
            "Separate facts from open questions",
        ],
        "forbidden_patterns": [
            "Uncited factual claims",
            "Venue-specific marketing language",
            "Copied sentences from anchor papers",
        ],
    }


def strip_latex_markup(text: str) -> str:
    """Strip lightweight LaTeX markup from a title-like string."""
    cleaned = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?\{([^{}]*)\}", r"\1", text)
    cleaned = re.sub(r"\\[a-zA-Z]+\*?", "", cleaned)
    cleaned = cleaned.replace("{", "").replace("}", "")
    return " ".join(cleaned.split()).strip()


def normalize_text(text: str) -> str:
    """Normalize free-form text for comparisons."""
    return re.sub(r"\s+", " ", strip_latex_markup(text or "")).strip().lower()


def normalize_text_tokens(text: str) -> list[str]:
    """Normalize text into comparable tokens."""
    return re.findall(r"[a-z0-9]+", normalize_text(text))


def read_balanced_group(text: str, start: int, opener: str, closer: str) -> tuple[str, int] | None:
    """Read a balanced bracket group starting at start."""
    if start >= len(text) or text[start] != opener:
        return None
    depth = 0
    index = start + 1
    group_start = index
    while index < len(text):
        char = text[index]
        if char == opener:
            depth += 1
        elif char == closer:
            if depth == 0:
                return text[group_start:index], index + 1
            depth -= 1
        index += 1
    return None


def extract_citation_commands(content: str) -> list[dict[str, Any]]:
    """Extract LaTeX citation commands with positions and keys."""
    commands: list[dict[str, Any]] = []
    for match in _CITE_COMMAND_RE.finditer(content):
        command = match.group("command")
        index = match.end()
        while index < len(content) and content[index].isspace():
            index += 1
        if index < len(content) and content[index] == "*":
            index += 1
        while index < len(content) and content[index].isspace():
            index += 1
        while index < len(content) and content[index] == "[":
            optional = read_balanced_group(content, index, "[", "]")
            if optional is None:
                break
            _, index = optional
            while index < len(content) and content[index].isspace():
                index += 1
        if index >= len(content) or content[index] != "{":
            continue
        required = read_balanced_group(content, index, "{", "}")
        if required is None:
            continue
        raw_keys, end = required
        keys = [key.strip() for key in raw_keys.split(",") if key.strip()]
        if not keys:
            continue
        commands.append(
            {
                "command": command,
                "keys": keys,
                "start": match.start(),
                "end": end,
            }
        )
    return commands


def count_citations(tex_path: Path) -> dict:
    """Count citations in a LaTeX file."""
    if not tex_path.exists():
        return {"total": 0, "unique": 0, "keys": []}

    content = tex_path.read_text(encoding="utf-8")
    all_keys: list[str] = []
    for command in extract_citation_commands(content):
        all_keys.extend(command["keys"])

    unique_keys = list(set(all_keys))
    return {
        "total": len(all_keys),
        "unique": len(unique_keys),
        "keys": sorted(unique_keys),
    }


def count_bibtex_entries(bib_path: Path) -> dict:
    """Count entries in a BibTeX file."""
    if not bib_path.exists():
        return {"total": 0, "by_year": {}, "keys": []}

    content = bib_path.read_text(encoding="utf-8")
    entry_matches = re.findall(r"@\w+\{([^,]+),", content)
    keys = [key.strip() for key in entry_matches]
    year_matches = re.findall(r"year\s*=\s*\{?(\d{4})\}?", content, re.IGNORECASE)
    by_year: dict[str, int] = {}
    for year in year_matches:
        by_year[year] = by_year.get(year, 0) + 1

    return {
        "total": len(keys),
        "by_year": by_year,
        "keys": sorted(keys),
    }


def extract_section_events(content: str) -> list[dict[str, Any]]:
    """Extract section hierarchy events with normalized paths."""
    events: list[dict[str, Any]] = []
    current: dict[str, str] = {"section": "", "subsection": "", "subsubsection": ""}
    for match in _SECTION_RE.finditer(content):
        level = match.group("level")
        title = strip_latex_markup(match.group("title"))
        if level == "section":
            current = {"section": title, "subsection": "", "subsubsection": ""}
        elif level == "subsection":
            current["subsection"] = title
            current["subsubsection"] = ""
        else:
            current["subsubsection"] = title

        path_parts = [part for part in [current["section"], current["subsection"], current["subsubsection"]] if part]
        path = " > ".join(path_parts)
        events.append(
            {
                "level": level,
                "title": title,
                "path": path,
                "normalized_path": normalize_text(path),
                "start": match.start(),
            }
        )
    return events


def find_section_path_for_position(section_events: list[dict[str, Any]], position: int) -> str:
    """Find the nearest section path before a given position."""
    current_path = ""
    for event in section_events:
        if event["start"] > position:
            break
        current_path = str(event["path"])
    return current_path


def parse_bibtex_entries(content: str) -> list[dict[str, Any]]:
    """Parse BibTeX entries into key/body pairs."""
    entries: list[dict[str, Any]] = []
    pattern = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", re.MULTILINE)
    matches = list(pattern.finditer(content))
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        entry_text = content[start:end].strip()
        entries.append(
            {
                "entry_type": match.group(1),
                "key": match.group(2).strip(),
                "text": entry_text,
            }
        )
    return entries


def extract_bibtex_field(entry_text: str, field_name: str) -> str | None:
    """Extract a field value from a BibTeX entry's raw text.

    Handles ``field = {value}``, ``field = "value"``, and bare values
    like ``year = 2023``, including nested braces.
    """
    pattern = re.compile(rf"{re.escape(field_name)}\s*=\s*", re.IGNORECASE)
    match = pattern.search(entry_text)
    if match is None:
        return None
    rest = entry_text[match.end():].lstrip()
    if rest.startswith("{"):
        depth = 0
        for i, ch in enumerate(rest):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return rest[1:i].strip() or None
    elif rest.startswith('"'):
        end = rest.find('"', 1)
        if end != -1:
            return rest[1:end].strip() or None
    else:
        end = rest.find(",")
        if end == -1:
            end = rest.find("}")
        if end == -1:
            end = len(rest)
        return rest[:end].strip() or None
    return None


def extract_cite_context(content: str, cite_start: int, cite_end: int, window: int = 1) -> str:
    """Extract ±*window* sentences around a citation position.

    Uses sentence-ending punctuation (``.``, ``?``, ``!``) followed by
    whitespace as boundaries.
    """
    breaks = [0]
    for m in re.finditer(r"[.!?]\s+", content):
        breaks.append(m.end())
    breaks.append(len(content))

    seg = 0
    for i in range(len(breaks) - 1):
        if breaks[i] <= cite_start < breaks[i + 1]:
            seg = i
            break

    lo = max(0, seg - window)
    hi = min(len(breaks) - 1, seg + 1 + window)
    return content[breaks[lo]:breaks[hi]].strip()
