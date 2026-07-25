from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore, sha256_file
from .claim_manifest import LatexClaimUnit, import_latex_claims
from .models import ArtifactTier, ClaimStatus, ClaimType
from .protected_blocks import protected_blocks_sha256
from .publication import PublicationEngine
from .release import ReleaseVerifier, write_page_inspection
from .scientific_memory import ScientificMemory

PINNED_COMMIT = "5e24e22c0f726fa73fa924afb1d1d186ca677b7b"
UPSTREAM_URL = "https://github.com/QJHWC/MambaIR-GPPNN"


def _tree_digest(root: Path) -> tuple[str, list[dict[str, Any]]]:
    records = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    ]
    digest = hashlib.sha256(
        json.dumps(records, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return digest, records


def _source_evidence(
    memory: ScientificMemory,
    source_root: Path,
    relative: str,
    *,
    evidence_type: str = "SOURCE_CODE",
) -> str:
    path = source_root / relative
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    source_id = memory.add_source(
        kind="SOURCE_SNAPSHOT",
        uri=UPSTREAM_URL,
        commit_sha=PINNED_COMMIT,
        path=relative,
        blob_sha256=sha256_file(path),
        content_sha256=sha256_file(path),
        license_id="Apache-2.0" if relative.startswith(("models/", "LICENSE")) else None,
    )
    return memory.add_evidence(
        evidence_type=evidence_type,
        source_id=source_id,
        path=f"source/{relative}",
        line_start=1,
        line_end=len(lines),
        excerpt=text,
        config_scope=f"commit:{PINNED_COMMIT}",
        metadata={
            "commit_sha": PINNED_COMMIT,
            "file_sha256": sha256_file(path),
        },
    )


def build_workspace(workspace: str | Path) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve()
    paper = root / "paper"
    source = root / "source"
    tex = paper / "main.tex"
    references = paper / "references.bib"
    imported_seed = root / "provenance" / "imported-seed.pdf"
    required = (
        tex,
        references,
        imported_seed,
        source / "LICENSE",
        source / "LICENSES" / "Apache-2.0.txt",
        source / "THIRD_PARTY_NOTICES.md",
        source / "models" / "mambair_gppnn.py",
        source / "models" / "dual_modal_assm.py",
        source / "models" / "cross_modal_attention.py",
        source / "data" / "photo_dataloader.py",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("workspace inputs are incomplete: " + ", ".join(missing))

    memory = ScientificMemory(root / ".paperforge" / "paperforge.db")
    model_evidence = _source_evidence(memory, source, "models/mambair_gppnn.py")
    routing_evidence = _source_evidence(memory, source, "models/dual_modal_assm.py")
    fusion_evidence = _source_evidence(memory, source, "models/cross_modal_attention.py")
    loader_evidence = _source_evidence(memory, source, "data/photo_dataloader.py")
    readme_evidence = _source_evidence(memory, source, "README.md")
    license_evidence = _source_evidence(
        memory,
        source,
        "THIRD_PARTY_NOTICES.md",
        evidence_type="PROVENANCE_LICENSE",
    )
    bibliography_source = memory.add_source(
        kind="BIBLIOGRAPHY",
        uri="paper/references.bib",
        path="paper/references.bib",
        blob_sha256=sha256_file(references),
        content_sha256=sha256_file(references),
    )
    bibliography_evidence = memory.add_evidence(
        evidence_type="LITERATURE",
        source_id=bibliography_source,
        path="paper/references.bib",
        line_start=1,
        line_end=len(references.read_text(encoding="utf-8").splitlines()),
        excerpt=references.read_text(encoding="utf-8"),
        metadata={"scope": "bibliographic metadata; no empirical values imported"},
    )

    def resolve(unit: LatexClaimUnit) -> tuple[ClaimType, ClaimStatus, Sequence[str]]:
        lowered = unit.text.lower()
        if "\\cite" in unit.text or any(
            word in lowered for word in ("mambair adapts", "gppnn derives")
        ):
            return ClaimType.LITERATURE, ClaimStatus.SUPPORTED_STATIC, (
                bibliography_evidence,
            )
        if any(
            word in lowered
            for word in ("apache", "license", "provenance", "official reproduction")
        ):
            return ClaimType.PROVENANCE_LICENSE, ClaimStatus.SUPPORTED_STATIC, (
                license_evidence,
                routing_evidence,
            )
        if any(
            word in lowered
            for word in (
                "loader",
                "resample",
                "random tensors",
                "three-channel",
                "one-channel",
            )
        ):
            return ClaimType.LIMITATION, ClaimStatus.SUPPORTED_STATIC, (
                loader_evidence,
                readme_evidence,
            )
        if any(
            word in lowered
            for word in ("gumbel", "semantic", "selective scan", "state recurrence")
        ):
            return ClaimType.STATIC_IMPLEMENTATION, ClaimStatus.SUPPORTED_STATIC, (
                routing_evidence,
            )
        if any(word in lowered for word in ("attention", "chunk", "query", "key")):
            return ClaimType.STATIC_IMPLEMENTATION, ClaimStatus.SUPPORTED_STATIC, (
                fusion_evidence,
            )
        return ClaimType.STATIC_IMPLEMENTATION, ClaimStatus.SUPPORTED_STATIC, (
            model_evidence,
            readme_evidence,
        )

    claim_manifest_path = root / "artifacts" / "claim_manifest.json"
    manifest = import_latex_claims(
        memory,
        tex,
        evidence_resolver=resolve,
        relative_file="main.tex",
        manifest_path=claim_manifest_path,
    )
    spans = {
        claim["claim_id"]: claim["tex_span"]
        for claim in manifest["claims"]
    }

    store = ArtifactStore(
        root,
        allowed_roots=("artifacts", "dist", "paper", "provenance"),
        allowed_suffixes=(".json", ".pdf", ".png", ".tex", ".bib", ".zip"),
        memory=memory,
    )
    seed_record = store.register(
        "provenance/imported-seed.pdf",
        kind="paper",
        tier=ArtifactTier.IMPORTED_SEED,
        status="VERIFIED",
        media_type="application/pdf",
        metadata={
            "source": "20260724 writing-only seed",
            "sha256": sha256_file(imported_seed),
        },
    )
    store.register(
        "artifacts/claim_manifest.json",
        kind="manifest",
        status="VERIFIED",
        media_type="application/json",
    )

    protected_before = protected_blocks_sha256(tex.read_text(encoding="utf-8"))
    final_pdf = root / "dist" / "MambaIR-GPPNN-v3.pdf"
    publication = PublicationEngine(max_rounds=3).publish(
        paper,
        template="generic",
        main_tex="main.tex",
        output_pdf=final_pdf,
        scientific_memory=memory,
        claim_spans=spans,
        artifact_dir=root / "dist",
        release_root=root,
    )
    if not publication.success or publication.final_pdf is None:
        raise RuntimeError("publication engine did not produce a verified PDF")
    protected_after = protected_blocks_sha256(tex.read_text(encoding="utf-8"))
    if protected_before != protected_after:
        raise RuntimeError("protected experiment block changed during publication")

    final_record = store.register(
        "dist/MambaIR-GPPNN-v3.pdf",
        kind="paper",
        tier=ArtifactTier.FINAL_PUBLICATION,
        status="VERIFIED",
        media_type="application/pdf",
        metadata={
            "publication_manifest": "dist/publication.manifest.json",
            "claim_coverage_percent": manifest["coverage"]["percent"],
        },
    )
    final_render = publication.rounds[-1].render_result
    if final_render is None or not final_render.success:
        raise RuntimeError("publication has no verified rendered pages")
    write_page_inspection(
        root,
        pdf_path=final_pdf,
        rendered_pages=final_render.pages,
        reviewer="PaperForge publication renderer and layout diagnostician",
    )
    store.register(
        "artifacts/page-inspection.json",
        kind="manifest",
        status="VERIFIED",
        media_type="application/json",
    )

    tree_sha256, tree_records = _tree_digest(source)
    provenance = {
        "schema": "paperforge.mambair.provenance/v1",
        "upstream": UPSTREAM_URL,
        "commit": PINNED_COMMIT,
        "source_tree_sha256": tree_sha256,
        "source_files": tree_records,
        "licenses": {
            "repository": {
                "path": "source/LICENSE",
                "sha256": sha256_file(source / "LICENSE"),
            },
            "apache_2_0": {
                "path": "source/LICENSES/Apache-2.0.txt",
                "sha256": sha256_file(source / "LICENSES" / "Apache-2.0.txt"),
            },
            "third_party_notice": {
                "path": "source/THIRD_PARTY_NOTICES.md",
                "sha256": sha256_file(source / "THIRD_PARTY_NOTICES.md"),
            },
        },
        "artifacts": {
            "imported_seed": seed_record.to_dict(),
            "final_publication": final_record.to_dict(),
        },
        "execution_policy": {
            "profile": "writing-only",
            "training": False,
            "inference": False,
            "metric_recomputation": False,
        },
    }
    provenance_path = root / "artifacts" / "provenance.json"
    provenance_path.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    store.register(
        "artifacts/provenance.json",
        kind="manifest",
        status="VERIFIED",
        media_type="application/json",
    )
    store.write_manifest(
        "artifacts/workspace.manifest.json",
        metadata={
            "profile": "writing-only",
            "commit": PINNED_COMMIT,
            "claim_coverage_percent": manifest["coverage"]["percent"],
        },
    )

    release_gate = ReleaseVerifier(root, memory=memory).verify()
    ReleaseVerifier(root, memory=memory).write_report(release_gate)
    if not release_gate.passed:
        raise RuntimeError(
            "release verification failed: "
            + json.dumps(release_gate.to_dict(), ensure_ascii=False)
        )
    return {
        "workspace": str(root),
        "claim_count": len(manifest["claims"]),
        "claim_coverage_percent": manifest["coverage"]["percent"],
        "source_tree_sha256": tree_sha256,
        "final_pdf": str(final_pdf),
        "release_gate": release_gate.to_dict(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the pinned MambaIR-GPPNN writing-only publication workspace."
    )
    parser.add_argument("workspace")
    args = parser.parse_args(argv)
    print(json.dumps(build_workspace(args.workspace), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
