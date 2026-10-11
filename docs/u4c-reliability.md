# U4C reliability maintenance

Current follow-up (2026-10-09): the U4D isolated Python 3.11 environment has
resolved the missing-dependency/static-tool debt recorded below. Latest full
suite: 778 passed, 5 opt-in compute skips, 0 failures/errors; Ruff/mypy pass.
All 69 U4C tests remain passing. This does not expand the reliability guarantees:
single-writer, quiesced repair and controlled termination only. See the
[live U4D execution/handoff](u4d-final-execution.md) for current readiness;
the original U4C results below are retained as historical evidence.

Source baseline: `7bc397ba3ed8a76383c7834a4bfd2b20856f0b80`.
Scope: crash/reopen/retry/reconcile only; no new scientific or release semantics.
No dependency installation, real manuscript changes, or push.

## ReliabilityOperationInventory (pre-change inspection)

| operation_id / subsystem | mutation type / DB writes | filesystem writes | atomic boundary | journal/state machine | idempotency strategy | reconcile function | known crash windows | existing tests | uncovered risk | planned U4C coverage |
|---|---|---|---|---|---|---|---|---|---|---|
| EI-stage / import | create imports row | copies sources, manifest | file and DB separate | staged | import identity | reconcile_imports | copied source without row | U21/U22 staging tests | incomplete staging must not materialize | inventory; incomplete approval remains blocked |
| EI-validate / import | validation/import state | validation.json | file and DB separate | validated/needs_human | canonical digest | reconcile_imports | report/state mismatch | U22 digest tests | unknown mismatch needs manual handling | retain fail-closed; no approval creation |
| EI-approve / import | approval + import state | reads validation/sources | SQLite commit | approved | bound hashes | approval_valid | before/after commit | U22 approval tests | currentness on recovery | revoked/stale binding tests |
| EI-materialize / import | Evidence + materialization + import | none | one SQLite transaction | materialized | materialization key | reconcile_imports | before/after COMMIT | exception rollback, ordinary subprocess reopen | no actual killed writer test; repair deletes orphan Evidence | SIGKILL before/after commit; orphan/missing Evidence; repeat |
| AF-ledger / bundle | ArtifactStore rows | query/ledger/manifest | per-file rename, then individual registry writes | implicit completion by artifacts | digest-derived ID | retry freeze_ledger | missing registry/sidecar | U23 ledger tests | retry trusts embedded digest; verifier ignores registry | common immutable fixture, registry failure/tamper/retry |
| AF-review / bundle | ArtifactStore rows | review/manifest | per-file rename then registration | none | digest-derived ID | retry freeze_review | files/registry split | U3A tests | retries rewrite audit time | immutable retry audit |
| AF-matrix / bundle | ArtifactStore rows | matrix/manifest | per-file rename then registration | none | digest-derived ID | retry freeze_review_matrix | files/registry split | U3B1 tests | retries rewrite history | common integrity audit |
| AF-PDFQA / bundle | ArtifactStore rows | spec/QA/manifest | per-file rename then registration | none | digest-derived ID | retry run_pdf_qa | partial file set | U3B2 tests | digest verifier/registry gap | common integrity audit; no new PDF rules |
| AF-release / bundle | ArtifactStore rows | target/report/manifest | fsynced temp + rename, then registration | none | assessment digest | retry freeze_assessment | report exists but registration absent | U4B tests | retry returns before restoring registry | real kill, incomplete freeze, retry |
| RU-proposal / result update | result_updates row | spec/proposal/validation | files then DB | VALIDATED/NEEDS_HUMAN | proposal digest | reconcile_result_updates | files without row | U24A tests | old proposal lacks expected whole-file result | freeze expected source hash in approved proposal |
| RU-approve / result update | approval + APPROVED | approval.json | DB and file separate | APPROVED | explicit approval | reconcile_result_updates | approval row/file gap | U24A tests | approval_id absent from file | validate DB/file approval before recovery |
| RU-apply / result update | APPLYING, baseline, APPLIED | target replace, applied.json | target rename separate from DB | APPLYING/APPLIED | source/block identity | reconcile_result_updates | RU-1..RU-8 | limited matrix/exception tests | new block presence mistaken for whole-file provenance; stale approval bypass | exact whole-file binding; full matrix and real SIGKILL |
| WF-status / workflow-process | workflow DB transitions | process memory only | SQLite commit | WorkflowStatus, independent process state | DB workflow identity | reopen + UnifiedRunStatus | process memory loss | U4A status tests | exact aliases vs persisted enums | four real DB reopen cases; status read zero writes |
| RE-evaluate / release | none | none | no recovery transaction | none | deterministic assessment | recompute currentness | evaluator termination | U4B readonly tests | killed evaluator not tested | SIGKILL + unchanged domain/history |

## Pre-change ReliabilityDefects

- RU expected-new hash is derived from the current file if it contains the new block: unknown outside-block edits can be misclassified CLEAN/FORWARD.
- RU forward recovery does not validate the approval; APPLIED with missing baseline only rewrites applied.json and never rebuilds baseline.
- EI repair deletes orphan Evidence and claim links without proof that removal is safe; missing Evidence can be deleted from the materialization index rather than retained for investigation.
- Release freeze retry returns an existing report before registry repair; ledger retry trusts its embedded digest and cannot complete a missing manifest.

These defects are addressed before widening stress coverage. Physical power-loss
durability is not claimed. Final executed coverage and limitations are recorded
below after verification.

## ReliabilityReport

Schema: `paperforge.reliability-report/v1` (Markdown representation).

Overall result: **PASSED_WITH_LIMITATIONS** under a single-writer, quiesced-repair
crash model. Controlled process death is tested; sudden physical power loss and
concurrent writers are not proven. No SQLite durability PRAGMA was changed.

### RecoveryOutcome

The test/report vocabulary is `CLEAN`, `RECOVERABLE_FORWARD`,
`RECOVERABLE_ROLLBACK`, `MANUAL_INTERVENTION_REQUIRED`, `UNRECOVERABLE`.
Existing subsystem APIs are retained. Result Update findings map directly;
Import reports add `recovery_outcome` without creating a new domain state machine.
Unreadable/corrupt database recovery is not attempted; retain the database and
escalate rather than declaring it clean or recreating scientific content.

### Executed crash matrix

| Case | Observed result and invariant | Evidence |
|---|---|---|
| RU-1 APPROVED + old | CLEAN; ordinary apply succeeds | `test_ru_crash_matrix[1]` |
| RU-2 APPLYING + old | RECOVERABLE_ROLLBACK; restores APPROVED only with existing valid approval, no target write | matrix case 2 + real pre-replace SIGKILL |
| RU-3 APPLYING + expected new | RECOVERABLE_FORWARD; restores full audit and baseline with approval ID | matrix case 3 + real post-replace SIGKILL |
| RU-4 APPLIED + new, applied.json missing | RECOVERABLE_FORWARD; fills audit without replacing manuscript | matrix case 4 |
| RU-5 APPLIED + new, baseline missing | RECOVERABLE_FORWARD; actually restores baseline | matrix case 5 |
| RU-6 APPLIED + unknown whole-file bytes | MANUAL_INTERVENTION_REQUIRED, even if expected new block is present | matrix case 6; outside-block/suffix edits |
| RU-7 new bytes but stale approval/artifact/ledger | MANUAL_INTERVENTION_REQUIRED, no forward repair | matrix case 7; binding and stale-ledger tests |
| RU-8 repeated reconcile | CLEAN after repair; byte/DB-stable subsequent calls | matrix case 8 and 20 fresh-workspace stress iterations |
| EI-1 killed before materialization COMMIT | zero partial Evidence/materialization rows; import not materialized | real precommit SIGKILL after an INSERT |
| EI-2 killed just after COMMIT | Evidence, materialization and import state agree after reopen | real postcommit SIGKILL |
| EI-3 orphan Evidence | detected and retained; repair refuses deletion | orphan tests and updated U22 regression |
| EI-4 materialization without Evidence | structured manual finding; retry refuses fabrication | missing Evidence / malformed IDs / wrong key tests |
| EI-5 repeated materialize/reconcile | same IDs, no new rows, logical DB dump unchanged | 20 reopens |
| AF-1 partial registration | not current until explicit same-content retry completes registry | all five bundle types; real release-freeze SIGKILL |
| AF-2 registry with missing member | integrity failure; only reconstructible identical member can be restored | missing manifest tests for all five types |
| AF-3 bytes/hash/status/kind disagreement | not current; retry does not overwrite conflicting registry/files | split-brain and tamper tests |
| AF-4 repeated same digest | same authoritative IDs, unchanged files and DB rows | 20 retries per bundle type |
| WF-1 waiting for human after restart | AWAITING_APPROVAL preserved, never COMPLETED | DB reopen tests + existing U4A exited-process relation tests |
| WF-2 approval survives lost process memory | actual APPROVED approval record unchanged; process UNKNOWN_NOT_ATTACHED | approved fixture + new ProcessManager instance |
| WF-3 RUNNING process disappears | RUNNING preserved, process_unknown relation | DB reopen test |
| WF-4 repeated status reads | full logical DB dump unchanged | 20 reads per workflow scenario |
| RE-evaluator killed | no domain mutation, prior complete report unchanged/current | real evaluator SIGKILL |
| RE-partial freeze/retry | no partial authority, complete history preserved, deterministic retry | real registry-point kill + partial JSON tests |

`NEEDS_HUMAN` is the task's conceptual label: this repository persists
`WorkflowStatus.AWAITING_APPROVAL`. `APPROVED` belongs to domain approval/experiment
records, not WorkflowStatus. Tests use the actual model and preserve both records;
no new workflow status or automatic resume was introduced.

### Real process termination

`tests/u4c_crash_child.py` is test-only instrumentation. The child performs the
real mutation, announces a controlled checkpoint over stdout, and waits. The
parent kills the child, waits for termination, asserts return code `-SIGKILL`,
opens a fresh service/DB handle and checks invariants. Six paths are covered:

1. Import after first Evidence INSERT, before COMMIT.
2. Import after COMMIT.
3. Result Update after APPLYING, before target replace.
4. Result Update immediately after target replace.
5. Read-only release evaluator during snapshot computation.
6. Release freeze after file writes, before registry registration.

Every child is reaped, including timeout/failure cleanup. Tests explicitly skip
when SIGKILL is unavailable; the recorded local run had no such skips. This is
not instruction-level timing enumeration or hardware power-loss simulation.

### Recovery safety

- Proposal/spec/approval files must match their DB bindings and canonical hashes.
- New proposals bind `expected_new_source_sha256` before approval. Legacy proposals
  are accepted only if reversing the proposed block reconstructs the exact old
  whole-file hash. Presence of a new block is never sufficient.
- Forward recovery verifies current ledger/artifact bindings and approval. It only
  writes technical audit/baseline/state records and never target manuscript bytes.
- Failure after target replacement leaves an incomplete operation, not a false
  ROLLED_BACK state. Existing conflicting baselines or audit records require manual
  intervention, including apparently plausible but mismatched approval IDs/times.
- Import reconciliation validates content/provenance and bidirectional references.
  It does not delete Evidence/claim links or materialization rows, fabricate missing
  Evidence, or set an import back to an apparently approved state. Unknown mismatch
  conservatively blocks materialization retry, including unrelated imported corruption.
- No reconcile operation creates approval, changes reviewer/claim status, completes
  human review or publishes anything. Tests compare the complete logical DB dump on
  manual cases and preserve the unknown target/audit bytes.

### Immutable file/registry boundary

`paperforge/immutable_artifacts.py` prechecks existing content before filling
absent members, never rewrites a complete matching file, and refuses registry
hash/status/kind conflicts. Registry-only retry does not refresh timestamps or IDs.
Ledger, ScientificReview, ReviewerMatrix, PDFQA and Release reports all use it.
Their verifiers now require the corresponding complete registry records.

PDFQA's verifier now excludes the bundle ID from the semantic digest exactly as
its producer does; this is an integrity-contract correction, not a new PDF rule.
U4B's release adapter additionally checks bundle registration. No checker or
release policy semantics were added.

Missing deterministic sidecars can be reconstructed by re-running the original
freeze inputs. A corrupt/truncated existing file is retained and rejected, not
overwritten. If exact registered bytes cannot be recovered, stop for manual
investigation. This is not a general concurrent publication protocol.

### SQLite and filesystem audit

Observed on the actual fixture connections: `journal_mode=wal`, `synchronous=2`
(FULL), `foreign_keys=1`, `integrity_check=ok`. Ordinary exception rollback and
actual killed-writer rollback/reopen pass. External materialization still inserts
Evidence, the materialization row and import state in one transaction. Result
Update intentionally has separate file and DB commit boundaries covered above.

| Write path | Durability level after this change |
|---|---|
| Ledger / Review / Matrix / PDFQA / Release immutable JSON | atomic_rename_plus_fsync via shared helper |
| Result Update target, approval.json, applied.json | atomic_rename_plus_fsync |
| ArtifactStore write_bytes | existing atomic_rename_plus_fsync |
| Result Update proposal/spec/validation | existing atomic_rename (no explicit file fsync) |
| External stage/validation artifacts | existing per-operation writes; no global multi-file transaction |

Fsync is on the temporary file before rename; there is no new parent-directory
fsync guarantee. Atomic rename is not proof of physical power-loss durability.

### Stress and orphan inventory

- Five immutable bundle types: one interrupted registration followed by 20
  idempotent retries each.
- External Import: 20 materialize/reconcile/reopen iterations.
- Result Update: 20 independent create/interrupt-state/reopen/reconcile workspaces.
- No unexplained duplicate IDs/rows, non-deterministic IDs, corruption or leaked
  helper temp files in those runs. Deliberately injected orphans were detected and
  either exactly repaired or retained with a manual outcome as specified.
- Filename-only scan of the existing repository `.paperforge` tree found no
  `*.tmp`, `*.partial`, `.*.json.*`, `*-journal`, `*-wal` or `*-shm` files. Existing
  `shadow/v0-shadow-001` and `shadow/v0-shadow-002` were not recovered, changed or
  submitted. This scan is not a claim that private shadow domain artifacts passed
  scientific or release validation.

Cleanup policy: never automatically delete unknown orphans, database WAL/SHM/journal
files, partial scientific artifacts or shadow data. Only a positively identified
abandoned temporary file, with its writer stopped and completed authoritative
artifact verified, may be considered for explicit operator cleanup. No cleanup
job or broad `.paperforge/` ignore rule was added.

### Operator APIs and manual intervention

With all writers stopped and a backup retained:

```python
from paperforge.api import PaperForgeService
from paperforge.external_import import reconcile_imports
from paperforge.result_update import reconcile_result_updates

service = PaperForgeService(workspace)
report = reconcile_result_updates(workspace, service.memory)  # inspect first
# Only known old/new states with valid provenance are repaired:
reconcile_result_updates(workspace, service.memory, repair=True)
# Import inconsistencies are retained for manual inspection, not auto-deleted:
reconcile_imports(workspace, service.memory, repair=True)
```

Before repair, preserve the DB and all operation/approval/source files. Unknown
third states, stale approvals/ledgers, conflicting baselines, corrupted JSON and
unreadable DBs require investigation, not a blind overwrite or forged approval.
The operator may restore known original artifacts from an independently verified
backup, then rerun inspection. No automatic rollback of manuscript bytes exists.

### Remaining limitations and U4D boundary

- Concurrent writers/freeze calls and concurrent manual file edits are outside the
  tested single-writer contract; quiesce writers before repair/retry.
- Physical power-loss, network filesystems, filesystem-specific directory durability,
  disk corruption recovery and exhaustive kill timing are not proven.
- Conflicting historical baselines and unreconstructible legacy partial audit files
  intentionally require manual intervention.
- Existing stage/validate/proposal file-to-DB gaps remain fail-closed/manual; U4C
  does not invent approvals to complete them.
- Static tools and missing optional writing dependencies remain U4D environment
  gates. Local review does not replace future push-triggered platform reviews.

## Final verification (2026-09-27)

Runtime: Python 3.11.15, pytest 9.1.1, macOS. No dependencies were installed.

| Run / gate | Result |
|---|---|
| Four new U4C test modules | 69 passed, no skips/failures/errors |
| All currently executable regression tests | 628 passed, 6 skipped, 10 deselected; two known blocked collection modules explicitly ignored |
| Full repository, `pytest tests --continue-on-collection-errors --tb=short` | 644 collected, 628 passed, 6 skipped, 10 failed, 2 collection errors |
| Environment-blocked tests | All 10 failures and both collection errors fail importing `pymupdf`; unchanged from baseline |
| Skips | Five opt-in live compute backend cases; one visualization case missing `fitz` |
| ruff / mypy | Both unavailable in this interpreter; NOT_RUN_TOOL_UNAVAILABLE |
| Local Python and security-focused review | Blocking findings repaired; final review found no HIGH/CRITICAL findings |
| Remote automatic code/security review | NOT_RUN_NO_PUSH; not replaced by local review |

The two collection errors are `tests/test_agent_bridges.py` and
`tests/test_legacy_security.py`. The ten failures are in
`tests/test_research_partner_pipeline.py`; every traceback ends in the existing
`engine/perform_review.py` import of `pymupdf`. The baseline was 559 passed with
the same 6 skips, 10 failures and 2 errors. The additional 69 passing tests are
the U4C suite. This is **not** a full-green repository result.

Reproduce targeted coverage:

```sh
python3.11 -m pytest tests/test_u4c_result_recovery.py tests/test_u4c_subprocess.py tests/test_u4c_artifact_recovery.py tests/test_u4c_reopen_and_stress.py -q --tb=short
python3.11 -m pytest tests --continue-on-collection-errors --tb=short
```

### Changed-file inventory

- `paperforge/immutable_artifacts.py`: shared immutable write/registration integrity.
- `paperforge/result_update.py`: exact recovery binding, approval/audit/baseline repair.
- `paperforge/external_import.py`: non-destructive consistency inspection and retry guard.
- `paperforge/result_ledger.py`, `scientific_review.py`, `reviewer_matrix.py`,
  `pdf_qa.py`, `release_eligibility.py`: immutable retry and registry verification.
- Four `tests/test_u4c_*.py` modules and `tests/u4c_crash_child.py`: matrices,
  actual process death, reopen, integrity and stress coverage.
- `tests/test_u22_finalization.py`, `tests/test_u4b_finalization.py`: align fixtures
  with retained-orphan safety and complete artifact registration.
- This report; merge-readiness inventory is a separate U4D-Prep document.

No real manuscript, shadow artifact, provider configuration, dependency declaration,
scientific checker, reviewer rule or publication authorization was changed.
