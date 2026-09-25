from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


class EvidenceSemanticsError(ValueError):
    pass


MEASUREMENT_KINDS = frozenset(
    {"measured", "simulated", "remapped", "derived", "transcribed"}
)
ACQUISITION_ORIGINS = frozenset(
    {"paperforge_run", "external_import", "manuscript_existing", "manual_entry"}
)
ENVIRONMENT_ROLES = frozenset({"scientific", "fixture", "synthetic_test"})

# Publishable measurement kinds (transcribed alone is never physical proof).
_PUBLISHABLE_KINDS = frozenset({"measured", "simulated", "remapped", "derived"})


@dataclass(frozen=True)
class EvidenceSemantics:
    """Three orthogonal fields. Never a flat seven-value enum.

    measurement_kind describes what the data is;
    claim eligibility describes whether it may support a paper claim.
    """

    measurement_kind: str
    acquisition_origin: str
    environment_role: str
    parent_evidence_ids: tuple[str, ...] = ()
    derivation_method: str | None = None
    derivation_version: str | None = None

    def __post_init__(self) -> None:
        if self.measurement_kind not in MEASUREMENT_KINDS:
            raise EvidenceSemanticsError(
                f"unknown measurement_kind: {self.measurement_kind!r}"
            )
        if self.acquisition_origin not in ACQUISITION_ORIGINS:
            raise EvidenceSemanticsError(
                f"unknown acquisition_origin: {self.acquisition_origin!r}"
            )
        if self.environment_role not in ENVIRONMENT_ROLES:
            raise EvidenceSemanticsError(
                f"unknown environment_role: {self.environment_role!r}"
            )
        parents = tuple(str(p) for p in (self.parent_evidence_ids or ()))
        object.__setattr__(self, "parent_evidence_ids", parents)

    def validate(self) -> None:
        """Syntax + lineage requirements. Does not grant claim eligibility."""
        # __post_init__ already checked vocabularies.
        if self.measurement_kind in {"derived", "remapped"}:
            if not self.parent_evidence_ids:
                raise EvidenceSemanticsError(
                    f"{self.measurement_kind} requires parent_evidence_ids"
                )
            if not (self.derivation_method or "").strip():
                raise EvidenceSemanticsError(
                    f"{self.measurement_kind} requires derivation_method"
                )
            if not (self.derivation_version or "").strip():
                raise EvidenceSemanticsError(
                    f"{self.measurement_kind} requires derivation_version"
                )

    def to_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "measurement_kind": self.measurement_kind,
            "acquisition_origin": self.acquisition_origin,
            "environment_role": self.environment_role,
        }
        if self.parent_evidence_ids:
            payload["parent_evidence_ids"] = list(self.parent_evidence_ids)
        if self.derivation_method:
            payload["derivation_method"] = self.derivation_method
        if self.derivation_version:
            payload["derivation_version"] = self.derivation_version
        return payload

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any]) -> EvidenceSemantics:
        try:
            return cls(
                measurement_kind=str(metadata["measurement_kind"]),
                acquisition_origin=str(metadata["acquisition_origin"]),
                environment_role=str(metadata["environment_role"]),
                parent_evidence_ids=tuple(metadata.get("parent_evidence_ids") or ()),
                derivation_method=(
                    str(metadata["derivation_method"])
                    if metadata.get("derivation_method") is not None
                    else None
                ),
                derivation_version=(
                    str(metadata["derivation_version"])
                    if metadata.get("derivation_version") is not None
                    else None
                ),
            )
        except KeyError as exc:
            raise EvidenceSemanticsError(
                f"evidence semantics missing field: {exc}"
            ) from exc

    def is_measured(self) -> bool:
        return self.measurement_kind == "measured"

    def is_proxy(self) -> bool:
        return self.measurement_kind in {"simulated", "remapped"}

    def is_semantically_admissible(self) -> bool:
        """Whether semantics allow this data as a scientific candidate.

        measured only describes what the data is. It does not prove
        validation passed, approval passed, claim eligibility,
        scientific verification, or publication eligibility.
        Fixture/synthetic_test are never admissible; transcribed alone is
        never physical-measurement proof. Hash integrity never implies
        physical truth (enforced by requiring validation+approval in U2.2).
        """
        if self.environment_role != "scientific":
            return False
        if self.measurement_kind not in _PUBLISHABLE_KINDS:
            return False
        if self.measurement_kind in {"derived", "remapped"}:
            try:
                self.validate()
            except EvidenceSemanticsError:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "ACQUISITION_ORIGINS",
    "ENVIRONMENT_ROLES",
    "EvidenceSemantics",
    "EvidenceSemanticsError",
    "MEASUREMENT_KINDS",
]
