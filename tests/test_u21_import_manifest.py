from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperforge.api import PaperForgeService
from paperforge.evidence_semantics import EvidenceSemantics, EvidenceSemanticsError
from paperforge.external_import import IMPORT_SCHEMA, get_import, stage_import


def _service(tmp_path: Path) -> PaperForgeService:
    return PaperForgeService(tmp_path)


def test_fixture_never_publishable() -> None:
    sem = EvidenceSemantics(
        measurement_kind="measured",
        acquisition_origin="external_import",
        environment_role="fixture",
    )
    sem.validate()
    assert sem.is_semantically_admissible() is False
    assert sem.is_measured() is True


def test_transcribed_never_physical_proof() -> None:
    sem = EvidenceSemantics(
        measurement_kind="transcribed",
        acquisition_origin="manuscript_existing",
        environment_role="scientific",
    )
    assert sem.is_semantically_admissible() is False


def test_derived_requires_parents() -> None:
    with pytest.raises(EvidenceSemanticsError, match="parent_evidence_ids"):
        EvidenceSemantics(
            measurement_kind="derived",
            acquisition_origin="paperforge_run",
            environment_role="scientific",
        ).validate()


def test_remapped_requires_method_version() -> None:
    with pytest.raises(EvidenceSemanticsError):
        EvidenceSemantics(
            measurement_kind="remapped",
            acquisition_origin="manuscript_existing",
            environment_role="scientific",
            parent_evidence_ids=("ev_1",),
        ).validate()


def test_measured_external_scientific_allowed_but_not_auto_verified() -> None:
    sem = EvidenceSemantics(
        measurement_kind="measured",
        acquisition_origin="external_import",
        environment_role="scientific",
    )
    sem.validate()
    # Allowed as a data source; claim eligibility still requires U2.2
    # validation + approval, never automatic here.
    assert sem.is_semantically_admissible() is True


def test_stage_copies_absolute_source_and_does_not_trust_system_fields(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    outside = tmp_path.parent / "outside-power.csv"
    outside.write_text("t,power\n0,6.1\n", encoding="utf-8")
    sem = EvidenceSemantics(
        measurement_kind="measured",
        acquisition_origin="external_import",
        environment_role="scientific",
    )
    record = stage_import(
        tmp_path,
        service.memory,
        semantics=sem,
        subject={"model_id": "m", "dataset_id": "d", "seed": 0},
        execution={"device": "rpi5", "timing_scope": "inference_only"},
        source_files=[str(outside)],
        approval_scope="scientific_metric_import",
    )
    assert record.state == "staged"
    # Canonical copy exists; original can disappear without breaking the import.
    canonical = tmp_path / ".paperforge" / "imports" / record.import_id / "sources"
    assert any(canonical.iterdir())
    outside.unlink()
    fetched = get_import(tmp_path, service.memory, record.import_id)
    assert fetched["manifest"]["schema"] == IMPORT_SCHEMA
    assert fetched["record"]["validation_status"] == "pending"
    assert json.loads(fetched["record"]["evidence_ids_json"]) == []
    # System fields are generated, never taken from caller subject.
    with pytest.raises(Exception, match="system field"):
        stage_import(
            tmp_path,
            service.memory,
            semantics=sem,
            subject={"model_id": "m", "manifest_sha256": "0" * 64},
            execution={"device": "rpi5"},
            source_files=[str(next(canonical.iterdir()))],
        )


def test_semantics_roundtrip_metadata() -> None:
    sem = EvidenceSemantics(
        measurement_kind="remapped",
        acquisition_origin="manuscript_existing",
        environment_role="scientific",
        parent_evidence_ids=("ev_a",),
        derivation_method="host-to-edge",
        derivation_version="v1",
    )
    sem.validate()
    assert EvidenceSemantics.from_metadata(sem.to_metadata()).to_metadata() == sem.to_metadata()
    assert sem.is_semantically_admissible() is True
