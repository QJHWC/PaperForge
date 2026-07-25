# Third-Party Notices

PaperForge v3 pins every external publication input in
`third_party/source-lock.json`. Runtime publication does not fetch an upstream
default branch.

## latex-paper-skills

A selected subset is vendored under `third_party/latex-paper-skills` from
commit `d0f106108cb09e448604a56ce973d35b340cf497`. It is licensed under the MIT
License; the upstream license and a per-file checksum manifest are included.

## CVPR author kit

CVPR template compatibility is implemented as a profile and validator.
PaperForge does not redistribute the author-kit files because the pinned
repository does not include a license file. Users provide the official kit;
PaperForge verifies the selected profile without following upstream `main`.

## PaperFit

The bounded compile-render-diagnose-repair-verify loop is an independent
implementation informed by PaperFit, arXiv:2605.10341v1. The paper itself is
not redistributed.

## MambaIR-GPPNN

The separately packaged first-paper workspace uses fixed upstream commit
`5e24e22c0f726fa73fa924afb1d1d186ca677b7b`. Its own MIT license, Apache-2.0
license for adapted MambaIR material, and third-party notices remain in that
workspace.
