from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass


class UnknownTemplateProfile(KeyError):
    pass


@dataclass(frozen=True, slots=True)
class TemplateProfile:
    name: str
    document_classes: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    signature_patterns: tuple[str, ...] = ()
    two_column: bool = False
    page_limit: int | None = None
    bibliography_file: str = "references.bib"
    dependency_policy: str = "vendored-only"

    def __post_init__(self) -> None:
        if self.bibliography_file != "references.bib":
            raise ValueError("all template profiles must use references.bib")
        if self.dependency_policy != "vendored-only":
            raise ValueError("template profiles must use vendored-only dependencies")
        if self.page_limit is not None and self.page_limit < 1:
            raise ValueError("template profile page_limit must be positive")

    def matches(self, tex_text: str) -> bool:
        document_class = _document_class(tex_text)
        if document_class is not None and any(
            document_class.casefold() == candidate.casefold()
            for candidate in self.document_classes
        ):
            return True
        return any(
            re.search(pattern, tex_text, flags=re.IGNORECASE | re.MULTILINE)
            for pattern in self.signature_patterns
        )


def _document_class(tex_text: str) -> str | None:
    match = re.search(
        r"\\documentclass(?:\s*\[[^\]]*\])?\s*\{([^}]+)\}",
        tex_text,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else None


class TemplateProfileRegistry:
    def __init__(self, profiles: tuple[TemplateProfile, ...] = ()) -> None:
        self._profiles: dict[str, TemplateProfile] = {}
        self._aliases: dict[str, str] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: TemplateProfile, *, replace: bool = False) -> None:
        canonical = profile.name.strip().casefold()
        if not canonical:
            raise ValueError("template profile name must not be empty")
        if canonical in self._profiles and not replace:
            raise ValueError(f"template profile already registered: {profile.name}")

        identifiers = (profile.name, *profile.aliases, *profile.document_classes)
        if not replace:
            collisions = [
                identifier
                for identifier in identifiers
                if identifier.casefold() in self._aliases
                and self._aliases[identifier.casefold()] != canonical
            ]
            if collisions:
                raise ValueError(f"template profile aliases already registered: {collisions}")

        self._profiles[canonical] = profile
        for identifier in identifiers:
            self._aliases[identifier.casefold()] = canonical

    def get(
        self,
        name: str,
        default: TemplateProfile | None = None,
    ) -> TemplateProfile:
        canonical = self._aliases.get(str(name).strip().casefold())
        if canonical is None:
            if default is not None:
                return default
            raise UnknownTemplateProfile(name)
        return self._profiles[canonical]

    def resolve(
        self,
        profile: str | TemplateProfile | None,
        *,
        tex_text: str | None = None,
    ) -> TemplateProfile:
        if isinstance(profile, TemplateProfile):
            return profile
        if profile is not None:
            return self.get(profile)
        if tex_text is not None:
            return self.detect(tex_text)
        return self.get("generic")

    def detect(self, tex_text: str) -> TemplateProfile:
        for name in ("cvpr", "ieee", "elsevier"):
            profile = self._profiles.get(name)
            if profile is not None and profile.matches(tex_text):
                return profile
        return self.get("generic")

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    def __getitem__(self, key: str) -> TemplateProfile:
        return self.get(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self.names())

    def __len__(self) -> int:
        return len(self._profiles)


DEFAULT_TEMPLATE_REGISTRY = TemplateProfileRegistry(
    (
        TemplateProfile(
            name="generic",
            document_classes=("article", "report", "book", "scrartcl"),
            aliases=("default",),
        ),
        TemplateProfile(
            name="cvpr",
            document_classes=("cvpr",),
            aliases=("cvpr-template",),
            signature_patterns=(
                r"\\usepackage(?:\s*\[[^\]]*\])?\s*\{cvpr\}",
                r"\\def\\cvprPaperID\b",
            ),
            two_column=True,
        ),
        TemplateProfile(
            name="ieee",
            document_classes=("IEEEtran", "ieeecolor"),
            aliases=("IEEE", "IEEEtran"),
            signature_patterns=(
                r"\\begin\{IEEEkeywords\}",
                r"\\IEEEPARstart\b",
            ),
            two_column=True,
        ),
        TemplateProfile(
            name="elsevier",
            document_classes=("elsarticle", "cas-sc", "cas-dc"),
            aliases=("elsarticle", "cas"),
            signature_patterns=(
                r"\\begin\{frontmatter\}",
                r"\\journal\s*\{",
            ),
        ),
    )
)
