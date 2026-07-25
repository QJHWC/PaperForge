# PaperForge v3 Validation Checkpoint

Date: 2026-07-25

## Release decision

Status: `RELEASE_BLOCKED`

The v3 source is code-complete and the available local end-to-end gates pass, but
the official `v3.0.0` release is intentionally withheld. The approved release
plan requires a real Windows VM run, and no Windows runtime is available on this
host. Consequently there is no `v3.0.0` Git tag, official source ZIP, MambaIR
workspace ZIP, or final delivery report.

## Verified results

- `CODE_VERIFIED`
  - macOS 15.7.2 arm64
  - Python 3.10.20: 252 tests passed
  - Python 3.11.15: 252 tests passed
  - Python 3.12.13: 252 tests passed
  - Linux x86_64 Python 3.11: 250 non-Git tests passed with the exact
    writing dependency set under QEMU; the two real Git tests passed in the
    native Linux arm64 VM
  - Linux arm64 Python 3.12 clean wheel install: 110 core, security, backend,
    publication, and real Git tests passed
  - Ruff passed
  - Mypy passed for 55 PaperForge source files
  - Node syntax check passed for `frontend/app.js`
  - Wheel and sdist built; clean wheel installation, dependency check,
    preflight, console entry point, metadata, launchers, templates, few-shot
    examples, rubrics, and packaged assets passed
  - Final wheel SHA-256:
    `3aadce7756b81bcc6fdcad50639589fd957063d8a88900d0f6dc414f51c3e63c`
  - Final sdist SHA-256:
    `5c82f525f269964b04675e71c6069da36489f97ff97dac0bd2e980e88113ee4a`
  - Source secret scan passed: 221 files scanned, zero findings
  - Independent final code and Python reviews found no Critical, High, or
    Medium findings

- `LOCAL_E2E_VERIFIED`
  - Local, Docker, OpenSSH, Cloud-SSH, Slurm 23.11.4, and Kind/Kubernetes
    backends exercised against real local runtimes
  - Submit, status, logs, artifact synchronization, cancellation, and resume
    paths exercised
  - Frontend loaded through Playwright with no browser console errors
  - Generic, CVPR, IEEE, and Elsevier publication profiles compiled and rendered
  - MambaIR-GPPNN writing-only candidate: 94/94 claims mapped, five PDF pages
    rendered and inspected, protected hashes unchanged, and release manifest
    verified
  - MambaIR-GPPNN source pinned to commit
    `5e24e22c0f726fa73fa924afb1d1d186ca677b7b`

- `EXTERNAL_SERVICE_AUTH_BLOCKED`
  - The one permitted Bailu preflight returned HTTP 401.
  - The runtime entered `AUTH_BLOCKED`; no long-generation retry was issued.

- Original repository protection
  - `/Users/qjhwc/Desktop/PaperForge/` pre/post archive SHA-256:
    `16524ab87999a1b442d37cda4dfd047fa0f33e3e63a55fdad2b49e64e3762634`
  - The original directory remained unchanged.

## Open release gate

`Windows fresh-install and E2E`: not executed. QEMU, VirtualBox, Parallels,
UTM, Wine, and Windows PowerShell are unavailable on this host.

The Windows ACL implementation is fail-closed and has platform-independent
payload tests for trusted owners, untrusted readers/writers, missing DACLs, and
NULL DACLs. These tests do not replace the required execution on real Windows.

This is a verification-environment blocker, not permission to downgrade the
gate. Official release artifacts must remain absent until the Windows run
passes. GitHub publication also remains local-only because no explicit remote
and approval were provided.
