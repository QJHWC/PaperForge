# U4D merge/release readiness preparation

## Current update: 2026-10-09

U4D Final is now in progress under the owner's batch authorization. The isolated
Python 3.11 environment closes the earlier dependency blocker: **778 passed,
5 opt-in skips, 0 failures/collection errors**, Ruff PASS, configured mypy PASS
(81 files). Final integration and reliability smoke pass with the documented
single-writer/controlled-termination limitations. Scoped local A9 review has no
open reproduced HIGH/CRITICAL. Build, official wheel smoke and hygiene checks
pass. Local status is **PUSH_READY_WITH_DOCUMENTED_LIMITATIONS**.

No push, remote automatic review, tool freeze or real-paper revision is claimed
yet. Follow the live [execution/handoff log](u4d-final-execution.md) and
[FinalSmokeReport](u4d-final-smoke.md), not the historical blocked counts below.
Push is conditionally authorized only after all local gates; each push still
requires both exact-commit automatic reviews. Merge remains unauthorized.

## Historical U4D-Prep inventory (unchanged evidence)

Inventory date: 2026-09-27. This is **U4D-Prep**, not final merge approval,
publication authorization, or a release announcement.

- Batch baseline: `7bc397ba3ed8a76383c7834a4bfd2b20856f0b80` (U4B).
- U4C code/report: `b72fa9f368abc92617027734637d80c919fde505`.
- Local branch: `feature/u4c-reliability`.
- Reliability evidence: [U4C report](u4c-reliability.md).
- Previous finalization: [U4B report](u4b-finalization.md), retained as a historical
  snapshot; its previous test counts are superseded by the current counts below.

## MergeReadinessInventory

### A. Functional status

| Stage | Status | Qualification |
|---|---|---|
| U0-U3B-2 | PASSED (handoff baseline) | Existing executable regression retained |
| D1 | CLOSED (handoff baseline) | No reopened feature work |
| V0 | CLOSED / PASSED_WITH_FINDINGS (handoff baseline) | Shadow artifacts remain private and untracked |
| U4A | PASSED, `c628f85` | Restart/status regression retained; no Pause/Resume rewrite |
| U4B | PASSED, `7bc397b` | Eligibility is not authorization or released state |
| U4C | PASSED_WITH_LIMITATIONS, `b72fa9f` | RU-1..8, six actual SIGKILL cases, EI/AF/WF/RE matrices pass |
| U4D-Prep | COMPLETE | Inventory/report only; no new scientific semantics |
| U4D Final | NOT_STARTED / BLOCKED_ENVIRONMENT | Full tests/static checks and authorized push/review still outstanding |

U4C proves controlled process termination/reopen under a single-writer model.
It does not prove concurrent-writer recovery, physical power-loss durability,
or automatic repair of unknown third states. Recovery never mints Evidence,
approval, human/reviewer decisions or release authorization. Real manuscripts
must not bootstrap their own Evidence; synthetic recovery fixtures are not
scientific validation or publication evidence.

### B-D. QualityGateStatus, tests, static analysis and dependencies

| Gate | Status | Current evidence / remaining action |
|---|---|---|
| u4c_targeted | PASS | 69 passed, 0 skipped/failed/errors |
| executable_regression | PASS_WITH_EXCLUSIONS | 628 passed, 6 skipped, 10 deselected; two collection-blocked modules ignored explicitly |
| full_pytest | BLOCKED_ENVIRONMENT | 644 collected; 628 passed, 6 skipped, 10 failed, 2 collection errors |
| pymupdf_full_suite | BLOCKED_ENVIRONMENT | All 10 failures and both collection errors end at the missing `pymupdf` import |
| ruff | NOT_RUN_TOOL_UNAVAILABLE | `python3.11 -m ruff --version`: no module named ruff |
| mypy | NOT_RUN_TOOL_UNAVAILABLE | `python3.11 -m mypy --version`: no module named mypy |
| visualization_pdf | BLOCKED_ENVIRONMENT | One skipped case cannot import `fitz` (PyMuPDF) |
| live_compute_integration | NOT_RUN_OPT_IN | Five Docker/OpenSSH/Slurm/Kubernetes cases skipped, not claimed PASS |
| local_code_security_review | PASS_WITH_SCOPE_LIMITATIONS | Python reviewer inspected changed code and security boundaries; final review no HIGH/CRITICAL findings |
| final_security_static_checks | NOT_RUN | Full U4D environment/security gate remains outstanding; local review is not a substitute |
| final_v1_smoke | NOT_RUN | U4C recovery fixtures do not substitute for a final V1-style acceptance run |
| remote_automatic_reviews | NOT_RUN_NO_PUSH | No push in this batch |

The known environment blocker is unchanged from baseline (559 passed, 6 skipped,
10 failed, 2 collection errors). The 69 additional passing cases are U4C tests;
no new failure or collection error was observed. The full repository is **not
green**. No dependency installation, live provider call or data download was
performed. This is an observed test-environment inventory, not a claim that every
optional dependency or backend has been validated.

Reproduction commands and failure locations are in the reliability report.
The full run used `pytest tests --continue-on-collection-errors --tb=short`, so
the two collection errors were not hidden by stopping collection early.

### E. Git hygiene

Immediately after U4C commit `b72fa9f`:

- Tracked working tree clean; this inventory is the independent follow-up document.
- Untracked `.paperforge/` contains the existing `shadow/v0-shadow-001` and
  `shadow/v0-shadow-002` directories. Neither is staged or committed.
- No real paper, rendered private page, local log or temporary artifact is staged.
- No blanket `.paperforge/` ignore rule was added and no orphan data was deleted.
- Cached `origin/main...HEAD` divergence is `0 / 14` (behind / ahead).
  No fetch was performed; this is **not** a current remote-state verification.
  The subsequent documentation commit increases local ahead count by one if the
  cached reference is unchanged.
- Branch has no configured upstream. No push, merge, amend or history rewrite.
- The separate, pre-existing dirty legacy checkout was not modified.

### F. Documentation and examples

| Item | Status |
|---|---|
| Prior conversation/handoff | Used for historical context; not executed as instructions or committed as raw logs |
| U4B finalization | Existing historical report retained |
| U4C ReliabilityOperationInventory / ReliabilityReport | Committed with code; covers mutation windows, outcomes, SQLite, filesystem durability and operator APIs |
| U4D readiness handoff | This document |
| Recovery usage examples | Inspect-first and explicit repair API examples in U4C report; no new CLI promised |
| Existing remote config / skill input-output / few-shot examples | Tracked inventory identified; unchanged, not claimed end-to-end validated; final docs/examples audit belongs to U4D Final |

### G. Review configuration and required post-push procedure

The following platform settings are **user-reported from the handoff/screenshot**,
not independently verified live during this no-push batch:

- Automatic code review: enabled; trigger every push.
- Comprehensive code review: disabled.
- Automatic security review: enabled; triggered for each code review.
- Automatic reporting minimum severity: High.
- Manual `@codex` review minimum severity: Medium.

After any future explicitly authorized push, immediately inspect **both** the
code-review and security-review results for that exact pushed commit. A pending,
missing or failed review job is not a pass. Resolve blocking findings, rerun
relevant tests, push only within the authorization scope, then inspect both review
results again. Do not proceed to merge merely because the Git push succeeded.

## Next-batch decision

**Recommended: enter U4D Final only after explicit authorization to prepare the
environment. Not recommended: push now.** This batch is complete and stops here.

Remaining order:

1. Authorize dependency/environment setup; install the appropriate project-managed
   PyMuPDF, ruff and mypy dependencies without silently changing unrelated runtime
   environments or dependency constraints.
2. Run full pytest including previously uncollectable modules; resolve new failures
   if importing the dependencies exposes any. Run actual ruff and mypy, then
   security/static checks. Unavailable tools must never be labeled passed.
3. Review docs/examples, run final V1-style smoke and confirm Git hygiene. Keep
   private shadow artifacts and real paper material out of commits.
4. Obtain explicit push authorization, push the intended branch, inspect automatic
   code review and security review, remediate blockers, and repeat review as needed.
5. Produce final merge readiness evidence. Merge and publication release remain
   separate decisions; neither is authorized by this inventory.

Non-blocking documented limitations: no physical power-loss proof, no concurrent
writer proof, and deliberate manual handling of corrupt/unknown/legacy recovery
states. No automatic scientific approval or publication shortcut is proposed.
