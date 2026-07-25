from __future__ import annotations

import glob
import os
import platform
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


class ToolchainDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Toolchain:
    tex: Path | None = None
    latexmk: Path | None = None
    pdflatex: Path | None = None
    bibtex: Path | None = None
    pdftoppm: Path | None = None
    pdfinfo: Path | None = None
    pdftotext: Path | None = None

    @property
    def compile_backend(self) -> str | None:
        if self.latexmk is not None:
            return "latexmk"
        if self.pdflatex is not None:
            return "pdflatex"
        return None

    @property
    def poppler_available(self) -> bool:
        return self.pdftoppm is not None

    def require_compile(self, *, use_bibtex: bool) -> None:
        if self.latexmk is not None:
            return
        missing = []
        if self.pdflatex is None:
            missing.append("pdflatex")
        if use_bibtex and self.bibtex is None:
            missing.append("bibtex")
        if missing:
            raise ToolchainDiscoveryError(
                "missing TeX compile tools: " + ", ".join(missing)
            )

    def require_render(self) -> None:
        if self.pdftoppm is None:
            raise ToolchainDiscoveryError(
                "missing Poppler renderer: pdftoppm"
            )

    def as_dict(self) -> dict[str, str | None]:
        return {
            field: str(getattr(self, field)) if getattr(self, field) is not None else None
            for field in (
                "tex",
                "latexmk",
                "pdflatex",
                "bibtex",
                "pdftoppm",
                "pdfinfo",
                "pdftotext",
            )
        }


def _unique_paths(paths: list[Path]) -> tuple[Path, ...]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path.expanduser())
        if key in seen:
            continue
        seen.add(key)
        result.append(path.expanduser())
    return tuple(result)


def _split_path_list(value: str | None) -> list[Path]:
    if not value:
        return []
    return [Path(item) for item in value.split(os.pathsep) if item]


def _known_bin_dirs(platform_name: str, env: Mapping[str, str]) -> tuple[Path, ...]:
    system = platform_name.casefold()
    paths: list[Path] = []
    paths.extend(_split_path_list(env.get("PAPERFORGE_TOOL_PATH")))
    paths.extend(_split_path_list(env.get("PAPERFORGE_TEX_BIN")))
    paths.extend(_split_path_list(env.get("PAPERFORGE_POPPLER_BIN")))
    for root_name in (
        "PAPERFORGE_TEX_ROOT",
        "TEXLIVE_ROOT",
        "PAPERFORGE_POPPLER_ROOT",
        "POPPLER_ROOT",
    ):
        root_text = env.get(root_name)
        if not root_text:
            continue
        root = Path(root_text).expanduser()
        paths.extend((root, root / "bin", root / "Library" / "bin"))
        paths.extend(Path(item) for item in sorted(glob.glob(str(root / "bin" / "*"))))

    if system.startswith("darwin") or system.startswith("mac"):
        paths.extend(
            (
                Path("/Library/TeX/texbin"),
                Path("/opt/homebrew/bin"),
                Path("/usr/local/bin"),
                Path("/opt/local/bin"),
            )
        )
    elif system.startswith("win"):
        roots = (
            env.get("ProgramFiles"),
            env.get("ProgramFiles(x86)"),
            env.get("LOCALAPPDATA"),
        )
        for root_text in roots:
            if not root_text:
                continue
            root = Path(root_text)
            paths.extend(
                (
                    root / "MiKTeX" / "miktex" / "bin" / "x64",
                    root / "MiKTeX" / "miktex" / "bin",
                    root / "poppler" / "Library" / "bin",
                    root / "poppler" / "bin",
                )
            )
        for pattern in (
            "C:/texlive/*/bin/windows",
            "C:/Program Files/texlive/*/bin/windows",
            "C:/Program Files/poppler*/Library/bin",
        ):
            paths.extend(Path(item) for item in sorted(glob.glob(pattern)))
    else:
        paths.extend((Path("/usr/bin"), Path("/usr/local/bin"), Path("/snap/bin")))
        for pattern in (
            "/usr/local/texlive/*/bin/*",
            "/opt/texlive/*/bin/*",
        ):
            paths.extend(Path(item) for item in sorted(glob.glob(pattern)))

    paths.extend(_split_path_list(env.get("PATH")))
    return _unique_paths(paths)


def _candidate_names(name: str, platform_name: str) -> tuple[str, ...]:
    if platform_name.casefold().startswith("win"):
        return (f"{name}.exe", f"{name}.bat", name)
    return (name,)


def _find_tool(
    name: str,
    *,
    env: Mapping[str, str],
    platform_name: str,
    bin_dirs: tuple[Path, ...],
    which: Callable[..., str | None],
) -> Path | None:
    override = env.get(f"PAPERFORGE_{name.upper()}")
    if not override:
        override = env.get(name.upper())
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return candidate.resolve()

    found = which(name, path=env.get("PATH", ""))
    if found:
        return Path(found).expanduser().resolve()

    for directory in bin_dirs:
        for executable_name in _candidate_names(name, platform_name):
            candidate = directory / executable_name
            if candidate.is_file():
                return candidate.resolve()
    return None


def discover_toolchain(
    *,
    env: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    which: Callable[..., str | None] = shutil.which,
) -> Toolchain:
    environment = dict(os.environ if env is None else env)
    system = platform_name or platform.system()
    bin_dirs = _known_bin_dirs(system, environment)
    discovered = {
        name: _find_tool(
            name,
            env=environment,
            platform_name=system,
            bin_dirs=bin_dirs,
            which=which,
        )
        for name in (
            "tex",
            "latexmk",
            "pdflatex",
            "bibtex",
            "pdftoppm",
            "pdfinfo",
            "pdftotext",
        )
    }
    return Toolchain(**discovered)
