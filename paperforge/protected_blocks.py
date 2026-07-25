from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROTECTED_START = "% PAPERFORGE-PROTECTED-EXPERIMENT-START"
PROTECTED_END = "% PAPERFORGE-PROTECTED-EXPERIMENT-END"
_PROTECTED_TOKEN_PATTERN = re.compile(
    rf"{re.escape(PROTECTED_START)}|{re.escape(PROTECTED_END)}"
)


class ProtectedBlockViolation(RuntimeError):
    pass


def extract_protected_blocks(text: str) -> tuple[str, ...]:
    blocks: list[str] = []
    block_start: int | None = None
    for match in _PROTECTED_TOKEN_PATTERN.finditer(text):
        token = match.group(0)
        if token == PROTECTED_START:
            if block_start is not None:
                raise ProtectedBlockViolation("nested protected experiment markers")
            block_start = match.start()
            continue
        if block_start is None:
            raise ProtectedBlockViolation("protected experiment end marker precedes start")
        blocks.append(text[block_start : match.end()])
        block_start = None
    if block_start is not None:
        raise ProtectedBlockViolation("protected experiment start marker has no end")
    return tuple(blocks)


def protected_blocks_sha256(text: str) -> str:
    blocks = extract_protected_blocks(text)
    joined = "\x1e".join(blocks)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass
class ProtectedEditTransaction:
    path: Path
    require_markers: bool = False

    def __post_init__(self) -> None:
        self.path = self.path.expanduser().resolve()
        self.before_text = self.path.read_text(encoding="utf-8")
        self.before_blocks = extract_protected_blocks(self.before_text)
        if self.require_markers and not self.before_blocks:
            raise ProtectedBlockViolation("protected experiment markers are required")
        self.before_sha256 = protected_blocks_sha256(self.before_text)

    def verify(self) -> str:
        after_text = self.path.read_text(encoding="utf-8")
        try:
            after_blocks = extract_protected_blocks(after_text)
        except ProtectedBlockViolation:
            self.path.write_text(self.before_text, encoding="utf-8")
            raise
        if after_blocks != self.before_blocks:
            self.path.write_text(self.before_text, encoding="utf-8")
            raise ProtectedBlockViolation(
                "protected experiment content changed; edit was rolled back"
            )
        return protected_blocks_sha256(after_text)

    def rollback(self) -> None:
        self.path.write_text(self.before_text, encoding="utf-8")


class ProtectedCoder:
    """Wrap an Aider-like coder and make each edit transaction fail closed."""

    def __init__(self, coder: Any, tex_path: str | Path, *, require_markers: bool = False) -> None:
        self._coder = coder
        self._tex_path = Path(tex_path)
        self._require_markers = require_markers

    def __getattr__(self, name: str) -> Any:
        return getattr(self._coder, name)

    def run(self, *args: Any, **kwargs: Any) -> Any:
        transaction = ProtectedEditTransaction(
            self._tex_path,
            require_markers=self._require_markers,
        )
        try:
            result = self._coder.run(*args, **kwargs)
        except BaseException:
            transaction.rollback()
            raise
        transaction.verify()
        return result
