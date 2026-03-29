from __future__ import annotations

import json
import re
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Sequence

from engine.run_lock import run_lock


LIBRARY_VERSION = "1.0"
LIBRARY_RELATIVE_PATH = Path("artifacts") / "bibliography" / "library.json"


def _library_path(workspace: str | Path) -> Path:
    root = Path(workspace).expanduser().resolve()
    return root / LIBRARY_RELATIVE_PATH


def _default_library() -> Dict[str, Any]:
    return {
        "version": LIBRARY_VERSION,
        "updated_at": None,
        "entries": [],
        "section_map": {},
    }


@contextmanager
def _library_write_context(workspace: str | Path, acquire_lock: bool) -> Iterator[None]:
    workspace_path = Path(workspace).expanduser().resolve()
    ctx = run_lock(workspace_path, timeout=30, poll_interval=0.2, verbose=True) if acquire_lock else nullcontext()
    with ctx:
        yield


def _normalize_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _normalize_authors(author_text: str) -> List[str]:
    if not author_text.strip():
        return []
    return [part.strip() for part in author_text.replace("\n", " ").split(" and ") if part.strip()]


def _load_library(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return _default_library()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_library()
    if not isinstance(payload, dict):
        return _default_library()

    library = _default_library()
    library["version"] = str(payload.get("version") or LIBRARY_VERSION)
    library["updated_at"] = payload.get("updated_at")
    entries = payload.get("entries")
    if isinstance(entries, list):
        library["entries"] = [entry for entry in entries if isinstance(entry, dict)]
    section_map = payload.get("section_map")
    if isinstance(section_map, dict):
        normalized_section_map: Dict[str, List[str]] = {}
        for key, value in section_map.items():
            if not isinstance(key, str) or not isinstance(value, list):
                continue
            normalized_section_map[key] = [item for item in value if isinstance(item, str)]
        library["section_map"] = normalized_section_map
    return library


def _save_library(path: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": payload.get("version", LIBRARY_VERSION),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "entries": payload.get("entries", []),
        "section_map": payload.get("section_map", {}),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def load_workspace_library(workspace: str | Path) -> Dict[str, Any]:
    return _load_library(_library_path(workspace))


def list_entries(workspace: str | Path) -> List[Dict[str, Any]]:
    payload = load_workspace_library(workspace)
    entries = payload.get("entries", [])
    return list(entries) if isinstance(entries, list) else []


def _extract_wrapped_value(text: str, start: int) -> tuple[str, int]:
    opener = text[start]
    if opener == "{":
        depth = 0
        cursor = start
        while cursor < len(text):
            char = text[cursor]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start + 1 : cursor], cursor + 1
            cursor += 1
        return text[start + 1 :], len(text)
    if opener == '"':
        cursor = start + 1
        escaped = False
        parts: List[str] = []
        while cursor < len(text):
            char = text[cursor]
            if escaped:
                parts.append(char)
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                return "".join(parts), cursor + 1
            else:
                parts.append(char)
            cursor += 1
        return "".join(parts), len(text)

    cursor = start
    while cursor < len(text) and text[cursor] not in ",\n":
        cursor += 1
    return text[start:cursor].strip(), cursor


def _extract_field(body: str, field_name: str) -> str:
    match = re.search(rf"\b{re.escape(field_name)}\s*=\s*", body, re.IGNORECASE)
    if match is None:
        return ""
    cursor = match.end()
    while cursor < len(body) and body[cursor].isspace():
        cursor += 1
    if cursor >= len(body):
        return ""
    value, _ = _extract_wrapped_value(body, cursor)
    return re.sub(r"\s+", " ", value).strip()


def _iter_bibtex_entries(bibtex_text: str) -> Iterator[Dict[str, str]]:
    cursor = 0
    while True:
        at_index = bibtex_text.find("@", cursor)
        if at_index < 0:
            return
        brace_index = bibtex_text.find("{", at_index)
        if brace_index < 0:
            return
        entry_type = bibtex_text[at_index + 1 : brace_index].strip()
        comma_index = bibtex_text.find(",", brace_index)
        if comma_index < 0:
            return
        citation_key = bibtex_text[brace_index + 1 : comma_index].strip()
        depth = 1
        body_start = comma_index + 1
        body_cursor = body_start
        while body_cursor < len(bibtex_text) and depth > 0:
            char = bibtex_text[body_cursor]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            body_cursor += 1
        raw_entry = bibtex_text[at_index:body_cursor].strip()
        body = bibtex_text[body_start : body_cursor - 1].strip()
        cursor = body_cursor
        if entry_type and citation_key:
            yield {
                "entry_type": entry_type,
                "citation_key": citation_key,
                "body": body,
                "raw_bibtex": raw_entry,
            }


def _entry_fingerprint(entry: Dict[str, Any]) -> str:
    doi = str(entry.get("doi") or "").strip().lower()
    if doi:
        return f"doi:{doi}"
    title = _normalize_text(str(entry.get("title") or ""))
    year = str(entry.get("year") or "").strip()
    return f"title:{title}|year:{year}"


def _entry_from_bibtex(raw_entry: Dict[str, str], source: str) -> Dict[str, Any]:
    title = _extract_field(raw_entry["body"], "title")
    authors = _normalize_authors(_extract_field(raw_entry["body"], "author"))
    venue = (
        _extract_field(raw_entry["body"], "journal")
        or _extract_field(raw_entry["body"], "booktitle")
        or _extract_field(raw_entry["body"], "venue")
    )
    year = _extract_field(raw_entry["body"], "year")
    doi = _extract_field(raw_entry["body"], "doi")
    url = _extract_field(raw_entry["body"], "url")
    abstract = _extract_field(raw_entry["body"], "abstract")
    entry = {
        "id": raw_entry["citation_key"],
        "citation_key": raw_entry["citation_key"],
        "entry_type": raw_entry["entry_type"].lower(),
        "title": title,
        "authors": authors,
        "venue": venue,
        "year": year,
        "doi": doi,
        "url": url,
        "abstract": abstract,
        "source": source,
        "raw_bibtex": raw_entry["raw_bibtex"],
    }
    entry["fingerprint"] = _entry_fingerprint(entry)
    return entry


def _sort_entries(entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        entries,
        key=lambda item: (
            str(item.get("year") or ""),
            str(item.get("title") or "").lower(),
            str(item.get("citation_key") or "").lower(),
        ),
    )


def import_bibtex(
    workspace: str | Path,
    bibtex_text: str,
    *,
    source: str = "manual",
    acquire_lock: bool = False,
) -> Dict[str, Any]:
    with _library_write_context(workspace, acquire_lock):
        path = _library_path(workspace)
        payload = _load_library(path)
        entries: List[Dict[str, Any]] = list(payload.get("entries", []))
        existing_by_fingerprint = {str(entry.get("fingerprint") or _entry_fingerprint(entry)): entry for entry in entries}

        imported: List[Dict[str, Any]] = []
        duplicates: List[str] = []
        for raw_entry in _iter_bibtex_entries(bibtex_text):
            entry = _entry_from_bibtex(raw_entry, source=source)
            fingerprint = str(entry["fingerprint"])
            if fingerprint in existing_by_fingerprint:
                duplicates.append(entry["citation_key"])
                continue
            existing_by_fingerprint[fingerprint] = entry
            entries.append(entry)
            imported.append(entry)

        payload["entries"] = _sort_entries(entries)
        saved = _save_library(path, payload)
        return {
            "workspace": str(Path(workspace).expanduser().resolve()),
            "imported_count": len(imported),
            "duplicate_count": len(duplicates),
            "imported_keys": [entry["citation_key"] for entry in imported],
            "duplicate_keys": duplicates,
            "library": saved,
        }


def deduplicate_library(
    workspace: str | Path,
    *,
    acquire_lock: bool = False,
) -> Dict[str, Any]:
    with _library_write_context(workspace, acquire_lock):
        path = _library_path(workspace)
        payload = _load_library(path)
        entries = list(payload.get("entries", []))
        deduped: List[Dict[str, Any]] = []
        seen: set[str] = set()
        removed: List[str] = []
        for entry in entries:
            fingerprint = str(entry.get("fingerprint") or _entry_fingerprint(entry))
            if fingerprint in seen:
                removed.append(str(entry.get("citation_key") or entry.get("id") or ""))
                continue
            seen.add(fingerprint)
            entry["fingerprint"] = fingerprint
            deduped.append(entry)
        payload["entries"] = _sort_entries(deduped)
        saved = _save_library(path, payload)
        return {
            "before": len(entries),
            "after": len(deduped),
            "removed_keys": [item for item in removed if item],
            "library": saved,
        }


def export_bibtex(
    workspace: str | Path,
    *,
    entry_ids: Sequence[str] | None = None,
) -> str:
    payload = load_workspace_library(workspace)
    entries = list(payload.get("entries", []))
    if entry_ids is not None:
        wanted = set(entry_ids)
        entries = [entry for entry in entries if str(entry.get("id")) in wanted or str(entry.get("citation_key")) in wanted]
    return "\n\n".join(
        str(entry.get("raw_bibtex") or "").strip()
        for entry in entries
        if str(entry.get("raw_bibtex") or "").strip()
    ).strip()


def update_section_mapping(
    workspace: str | Path,
    section_name: str,
    citation_keys: Sequence[str],
    *,
    acquire_lock: bool = False,
) -> Dict[str, Any]:
    normalized_section = section_name.strip()
    if not normalized_section:
        raise ValueError("section_name is required")

    keys = [str(key).strip() for key in citation_keys if str(key).strip()]
    with _library_write_context(workspace, acquire_lock):
        path = _library_path(workspace)
        payload = _load_library(path)
        section_map = dict(payload.get("section_map", {}))
        section_map[normalized_section] = sorted(set(keys))
        payload["section_map"] = section_map
        saved = _save_library(path, payload)
        return {
            "section": normalized_section,
            "citation_keys": section_map[normalized_section],
            "library": saved,
        }


def record_writeup_citations(
    workspace: str | Path,
    section_name: str,
    citation_keys: Sequence[str],
    *,
    acquire_lock: bool = False,
) -> Dict[str, Any]:
    return update_section_mapping(
        workspace=workspace,
        section_name=section_name,
        citation_keys=citation_keys,
        acquire_lock=acquire_lock,
    )


def section_entries(workspace: str | Path, section_name: str) -> List[Dict[str, Any]]:
    payload = load_workspace_library(workspace)
    entry_map = {
        str(entry.get("citation_key") or entry.get("id")): entry
        for entry in payload.get("entries", [])
        if isinstance(entry, dict)
    }
    section_map = payload.get("section_map", {})
    citation_keys = section_map.get(section_name, []) if isinstance(section_map, dict) else []
    if not isinstance(citation_keys, list):
        return []
    return [entry_map[key] for key in citation_keys if key in entry_map]


def bibliography_summary(workspace: str | Path) -> Dict[str, Any]:
    payload = load_workspace_library(workspace)
    entries = list(payload.get("entries", []))
    section_map = payload.get("section_map", {})
    return {
        "workspace": str(Path(workspace).expanduser().resolve()),
        "version": payload.get("version", LIBRARY_VERSION),
        "updated_at": payload.get("updated_at"),
        "entry_count": len(entries),
        "sections": sorted(section_map.keys()) if isinstance(section_map, dict) else [],
        "citation_keys": [str(entry.get("citation_key") or entry.get("id")) for entry in entries],
    }
