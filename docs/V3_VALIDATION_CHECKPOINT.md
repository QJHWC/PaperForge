# PaperForge v3 Validation Checkpoint

Date: 2026-07-25

## Release decision

Status: `RELEASE_CANDIDATE`

The v3 source is code-complete and the macOS, Linux, real-compute, publication,
security, and review gates pass. On 2026-07-26 the release scope was explicitly
changed to defer Windows validation, so the missing Windows runtime no longer
blocks the current macOS/Linux release. The source ZIP, MambaIR workspace ZIP,
delivery report, and `v3.0.0` tag are produced only by the final release step.

## Verified results

- `CODE_VERIFIED`
  - macOS 15.7.2 arm64
  - Final macOS full suite: 333 passed, 5 real-compute gates skipped
  - Python 3.10.20, 3.11.15, and 3.12.13 passed clean full-suite validation;
    the final Docker/GitHub delta passed 12 tests on each version
  - Linux arm64 Python 3.12.3 full suite: 325 passed, 6 platform/runtime gates
    skipped; the final delta passed 11 tests with one case-insensitive-filesystem
    test skipped
  - Ruff passed
  - Mypy passed for 64 PaperForge source files
  - Node syntax check passed for `frontend/app.js`
  - Wheel built; clean wheel installation, dependency check,
    preflight, console entry point, metadata, launchers, templates, few-shot
    examples, rubrics, and packaged assets passed
  - Current verified wheel SHA-256:
    `667c5c7cd212590c55fdfcff6ecef2d17578e1622e2f96ffc247a566af931f11`
  - Source and reachable Git history secret scan passed: 262 worktree files
    and 257 Git blobs scanned, zero findings
  - Initial independent review blockers were fixed; final consolidated review
    verdict: `APPROVE`, with zero critical, high, medium, or low findings

- `LOCAL_E2E_VERIFIED`
  - Local, Docker, OpenSSH, Cloud-SSH, Slurm 23.11.4, and Kind/Kubernetes
    backends exercised against real local runtimes
  - Submit, status, logs, artifact synchronization, cancellation, and resume
    paths exercised
  - Frontend loaded through Playwright with no browser console errors
  - Generic, CVPR, IEEE, and Elsevier publication profiles compiled, rendered,
    and passed authoritative release revalidation in both empty-Bib and
    existing-Bib states (eight combinations)
  - CVPR author-kit was validated at locked commit
    `291758547e923160eb4d37079b7b9f0dfce82355`, tree
    `bada7af3a66da84fd610948fd72ce5dd01fb3cc2`, and git-archive SHA-256
    `72df21fe120ab08c59980bc9461c6cafc427149e6400684749e731086efce5d6`;
    the kit is not redistributed
  - Release verification now extracts the locked source bundle into a system
    temporary directory, requires byte-identical internal/external source
    locks, recompiles, rerenders, recomputes Claim/source invariants, and
    requires rebuilt page hashes to match both the official PDF render and the
    inspected page hashes
  - Every validation render passed decoded-pixel blank-page, saturation, and
    border-cropping checks in addition to TeX layout diagnostics
  - MambaIR-GPPNN writing-only candidate: 93/93 public claims mapped, five PDF
    pages freshly rebuilt, rendered, and inspected; protected hashes, source
    locks, bundle checksums, official page hashes, and release manifest passed
  - MambaIR-GPPNN source pinned to commit
    `5e24e22c0f726fa73fa924afb1d1d186ca677b7b`
  - MambaIR-GPPNN source tree SHA-256:
    `6129f9393b89bfc91bd29acd978fedae9dd3c987128ff3b9ae802b9fb4c61181`

- `EXTERNAL_SERVICE_AUTH_BLOCKED`
  - The one permitted Bailu preflight returned HTTP 401.
  - The runtime entered `AUTH_BLOCKED`; no long-generation retry was issued.

- Original repository protection
  - `/Users/qjhwc/Desktop/PaperForge/` pre-upgrade archive SHA-256:
    `16524ab87999a1b442d37cda4dfd047fa0f33e3e63a55fdad2b49e64e3762634`
  - All 7,681 files captured by the pre-upgrade content manifest still match
    their SHA-256 values; zero files have modification times after the
    baseline.
  - A prior read-only Git validation changed only the nested `.git` directory
    mtime, so a byte-identical tar stream cannot be claimed. No original file
    content was modified.

## Deferred Windows validation

`Windows fresh-install and E2E` has not been executed. QEMU, VirtualBox, Parallels,
UTM, Wine, and Windows PowerShell are unavailable on this host.

The Windows ACL implementation is fail-closed and has platform-independent
payload tests for trusted owners, untrusted readers/writers, missing DACLs, and
NULL DACLs. These tests do not replace the required execution on real Windows.

Run the complete gate from an elevated or standard PowerShell session with
Python 3.10-3.12, Git, Node, TeX Live/MiKTeX, and Poppler already available:

```powershell
powershell -ExecutionPolicy Bypass -File tools/run_windows_gate.ps1 `
  -OutputFile "$env:TEMP\paperforge-windows-gate.json"
```

The script builds one wheel, installs that wheel into fresh Python 3.10, 3.11,
and 3.12 environments, runs isolated import and dependency checks, then runs
the full test suite, Ruff, Mypy, Node syntax validation, the pinned CVPR lock,
and all eight publication profile/Bib combinations. It fails unless the host
is real Windows.

This remains the required gate before claiming Windows support. It is not part
of the current macOS/Linux release scope and must not be reported as passed.
