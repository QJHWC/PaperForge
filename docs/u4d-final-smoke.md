# FinalSmokeReport

Schema: `paperforge.final-smoke-report/v1` (durable Markdown report).
Run date: 2026-09-30. Environment role: **synthetic_test**.
Overall transport result: **PASSED**. Publication/release authorization: **NOT_GRANTED**.

## Executed path

`tests/test_u4d_final_smoke.py::test_final_evidence_to_release_status_smoke`
executes the following in one disposable workspace, without patching production
functions or injecting a hand-made metric Evidence row:

1. Create clearly labeled synthetic source bytes; external import stage/validate.
2. Explicit fixture-only approval test double; materialize the source Evidence.
   This is not a real device attestation and must never be imported into a paper.
3. Approved source bytes -> registered metric adapter -> normalized observation ->
   frozen ledger. Preserve original lexical value, source hash, locator and identity.
4. Generate Fact/Table/Figure preview data from that same ledger; compare actual
   member hashes against the manifests. Outputs remain `publication_ready=false`.
5. Validate/render a fact-bound narrative, freeze scientific review and reviewer
   matrix, and verify historical/current artifact integrity.
6. Build a real one-page fixture PDF from the fixture text using PyMuPDF, bind
   source/PDF hashes and toolchain in build provenance, render it using real
   Poppler, run PDF QA. No HumanPageReview is fabricated.
7. Evaluate release with the default policy, actual ledger/review/matrix/PDF inputs.
   Verify zero database mutation, preserve missing gates, refuse eligibility,
   freeze the assessment, recompute currentness, and retain `NOT_RELEASED`.
8. Create a writing-only workflow and inspect UnifiedRunStatus. Missing process
   memory remains `UNKNOWN_NOT_ATTACHED`; original source and imported bytes are
   unchanged.

The test emits `FinalSmokeReport.json` inside its temporary workspace, including
the actual import/ledger/artifact/review/assessment identifiers. Temporary database,
PDF, raster and source fixtures are not repository delivery artifacts.

## Negative boundaries and reliability

- `tests/test_u4d_import_ledger.py`: fixture/synthetic imports cannot be approved
  or materialized as scientific Evidence; manuscript-existing transcription
  cannot bootstrap Evidence; stale source, semantic override and ambiguous measured
  FPS/energy are rejected. Existing frozen ledgers become stale when their Evidence
  semantics, source association or excerpt is damaged. Read-only release-context
  verification leaves the database unchanged.
- U4C's four reliability modules rerun without exclusions: RU-1..8, six actual
  SIGKILL/reopen checkpoints, import reopen and immutable split-brain/stress.
- Combined import/integration/reliability run: **86 passed**, zero failed/errors
  or skips; five dependency SWIG deprecation warnings disclosed.

```sh
.venv/bin/python3.11 -m pytest tests/test_u4d_import_ledger.py tests/test_u4d_final_smoke.py tests/test_u4c_result_recovery.py tests/test_u4c_subprocess.py tests/test_u4c_artifact_recovery.py tests/test_u4c_reopen_and_stress.py -q --tb=short
```

## Scope limits

This is a transport/invariant gate, not scientific validation of any manuscript.
It does not certify figure publication layout, reviewer acceptance, real device
measurement or human visual review. The dependent x/y preview in the fixture is
deliberately not treated as independent evidence. Physical power-loss and
concurrent-writer reliability remain unproven. The complete local security review,
authorized push and both remote reviews are separate required gates.
