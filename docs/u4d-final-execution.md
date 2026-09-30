# U4D Final execution and downstream handoff

Batch started 2026-09-30 from `530d2f50dfbefec05b760c5e0f032cb09b781905`,
branch `feature/u4c-reliability`. This is a running evidence log, not a completion
certificate. The full batch includes the subsequent PCB paper revision; completing
U4D alone must not close the overall goal.

## Authorization and order

The batch owner authorizes isolated Python 3.11 dependency installation, source and
test repairs, local commits, and feature-branch push only after every required local
gate passes. Every push must be followed by inspection of both automatic code and
security review findings for the exact commit. No automatic merge.

After tool freeze, work in a separate paper revision branch/copy. Preserve original
source and experimental artifacts. Never promote manuscript-only numbers, fixtures,
simulated/remapped values or fixed-power proxies into measured Evidence. Human-only
gates remain pending; continue other independent tasks. No new third-party upload
of unpublished materials, paid resources, sudo, or system Python 3.14.

## Requirement coverage tracker

| Requirements | Evidence required | Current state |
|---|---|---|
| A1 environment | Interpreter/venv/pip and declared dependency inspection | IN_PROGRESS |
| A2 full tests | Full pytest, counts and failure classification | PENDING |
| A3-A4 static | Existing Ruff whole-repository and mypy paperforge scope pass | PENDING |
| A5 integration | End-to-end synthetic evidence-to-status FinalSmokeReport and negative boundaries | PENDING |
| A6 reliability | ResultUpdate, actual kill/reopen, import reopen, split-brain | PENDING |
| A7-A10 local readiness | Documentation, hygiene, security review, commits | PENDING |
| A11-A13 remote/freeze | Required local gates, authorized push, both review findings, fix/retest loop | PENDING |
| B0-B4 / P0 | Unique paper workspace, revision copy, baseline hashes, inventory, legal imports/ledger | PENDING_TOOL_FREEZE |
| C1-C7 / P1 | Layer guidance, bounded attribution, external validity, citation/PLC audit, layout, controlled patches | PENDING_TOOL_FREEZE |
| D1-D10 / P2-P3 / H0-H4 | Hardware and artifact inventory, frozen protocols, real feasible measurements/analyses | PENDING_TOOL_FREEZE |
| E1-E4 / V0-V3 | Split, provenance, hardware and scientific-scope audits | PENDING_TOOL_FREEZE |
| F1-F8 / P4 / V4 | Verified ledger, deterministic artifacts, grounded text, review, controlled update, consistency | PENDING_TOOL_FREEZE |
| F9-F12 | Bound compile, every-page render/QA, real human requests, response draft, revision package | PENDING_TOOL_FREEZE |
| G1-G3 | Separate paper commits, conditional push, itemized P/H/V/R statuses and blockers | PENDING_TOOL_FREEZE |
| H-I | Continuous independent progress; final 40-item consolidated delivery | PENDING |

## Preflight facts

- Initial tracked worktree clean; existing untracked `.paperforge/` retained.
- Python support remains `>=3.10,<3.13`; no metadata change required for the three
  known missing dependencies.
- `pymupdf==1.27.1` is an optional writing dependency, also installed by the official
  dev requirements through `requirements-writeup.txt`.
- `ruff==0.12.2` and `mypy==1.16.1` are dev-only dependencies.
- Created repository-local ignored `.venv` with `/opt/homebrew/bin/python3.11`.
  Installation uses `.venv/bin/python3.11 -m pip install -r requirements-dev.txt`;
  no global environment is modified.
- Official CI scope: `python -m pytest -q --tb=short`, `python -m ruff check .`,
  `python -m mypy paperforge`. Pytest discovers `tests`; Ruff retains the existing
  exclusions/rules; mypy retains the configured `paperforge` scope.
- No push, paper edits, or release authorization has occurred in this batch.

## Limitations retained

U4C remains PASSED_WITH_LIMITATIONS: controlled termination under a single-writer,
quiesced-repair model. Physical power loss and concurrent writers are not proven.
The earlier U4C/U4D-Prep reports are historical evidence, not current gate results.

## First verified checkpoint

- Official dev requirements installed successfully into `.venv`; `pip check`
  reports no broken requirements. No dependency metadata changes were needed.
- Initial isolated full run: 703 passed, 5 skipped, 3 failed, 0 collection errors.
  Classification: TEST_EXPECTATION / non-hermetic fixture. Three LocalBackend
  tests implicitly copied the repository cwd, including the new venv symlinks.
  They now explicitly use their own `tmp_path` as workdir; the production symlink
  rejection policy and all assertions remain intact.
- After repairs: **711 collected, 706 passed, 5 skipped, 0 failed, 0 errors**.
  All five skips are opt-in real Docker/SSH/Slurm/Kubernetes integration cases.
  Five PyMuPDF SWIG deprecation warnings remain visible.
- Ruff official scope PASS after import/unused-code/style cleanup; no configuration
  relaxation. A vacuous `... or True` assertion was strengthened to require the
  persisted audit timestamp, and three broad exception assertions were narrowed.
- Mypy official scope PASS (80 source files), with explicit mapping narrowing,
  manifest annotations and immutable-adapter Protocol properties. No blanket
  `ignore_errors` or scope reduction.
- Independent Python review found no new HIGH/CRITICAL in these repairs. It did
  reproduce a **pre-existing HIGH** in publication artifact fixed-name temporary
  writes: symlink traversal can overwrite an outside sentinel before registration
  rejects the output. This blocks push; repair and regression are next.
- End-to-end smoke inspection also found the import-to-ledger adapter connection
  requires verification: imported source Evidence lacks inline metric metadata,
  while ledger currently consumes only inline metadata. Do not fake this connection
  with hand-added scientific Evidence. A dedicated regression is required.
- Integration smoke, final security, remote review, tool freeze and all paper
  revision requirements remain open. No push is authorized by this checkpoint alone.
