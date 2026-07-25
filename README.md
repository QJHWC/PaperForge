# PaperForge Research OS v3

PaperForge v3 is an evidence-first runtime for research planning, controlled
experiments, paper production, review, and release. All public entry points use
the same workflow database, policy engine, agent registry, provider registry,
artifact store, and release verifier.

## Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,writing]"
paperforge preflight
```

Python 3.10-3.12 is supported. LaTeX publication additionally requires
`latexmk` or `pdflatex`, BibTeX, and Poppler.

## Execution profiles

| Profile | Permitted behavior |
|---|---|
| `writing-only` | Evidence reading, protected draft editing, compilation, rendering, review |
| `research` | Writing-only plus literature/research planning and experiment proposals |
| `full` | Approved experiment, compute, code patch, analysis, visualization, and release flows |

`writing-only` rejects experiment execution, inference, training, SSH,
datasets, weights, run artifacts, and executable context at the policy,
tool-registration, command, and context layers.

## CLI

```text
paperforge preflight
paperforge run --profile writing-only|research|full
paperforge approve --proposal-id <id>
paperforge resume
paperforge publish --template generic|cvpr|ieee|elsevier
paperforge release
```

Compatibility commands route to the same runtime:

```bash
paperforge writeup --workspace <path>
paperforge research_partner --workspace <path>
paperforge mvp --workspace <path>
paperforge scientist --workspace <path>
```

## Provider configuration

Credentials are never accepted as CLI arguments and are not stored in a
workspace. Put provider settings in the user configuration directory:

```text
~/.config/paperforge/config.json
~/.config/paperforge/credentials.json
```

On POSIX, `credentials.json` must have mode `0600`. Bailu uses the pinned
OpenAI-compatible endpoint, model `bailu-turing`, and one shared request
constructor that removes unsupported fields from single, batch, review,
citation, streaming, and Aider paths. A `401` or `403` preflight becomes
`AUTH_BLOCKED` without a long-generation retry.

## Evidence and publication

Every publication sentence is represented by a `claim_id`. The SQLite
Scientific Memory links claims to source, literature, runtime, experiment, or
license evidence. Final publication fails closed when:

- any claim is missing evidence, blocked, or contradicted;
- LaTeX sentence coverage is below 100%;
- compilation, bibliography, rendering, page inspection, protected hashes, or
  source lock verification fails;
- a secret scan or artifact digest check fails.

Publication profiles are `generic`, `cvpr`, `ieee`, and `elsevier`. External
skills and template sources are pinned in `third_party/source-lock.json`.
`references.bib` is the only bibliography file accepted.

## Experiment and compute

The experiment state machine is:

```text
Proposal -> Static Check -> Mini Experiment -> Full Experiment
```

Execution requires the `full` profile and an approved proposal. Provenance
records code, config, dataset, checkpoint, metric, and artifact hashes.
Backends expose the same submit/status/cancel/resume/log/sync contract:

- Local
- Docker
- SSH
- Slurm
- Kubernetes
- Cloud SSH

All backends default to dry-run plans. SSH requires pinned `known_hosts`,
`RejectPolicy`, a non-root user, and upload denylisting.

## MambaIR-GPPNN writing-only workspace

The release builder imports upstream commit
`5e24e22c0f726fa73fa924afb1d1d186ca677b7b`, retains license and notice files,
imports the five-page seed as `IMPORTED_SEED`, maps all manuscript claims, and
publishes without training, inference, metric recomputation, data, or weights:

```bash
python -m paperforge.mambair_workspace /path/to/assembled-workspace
```

## Quality gates

```bash
python -m pytest -q
python -m ruff check paperforge engine agents frontend tests
python -m mypy paperforge
```

Core repository layers:

- `paperforge/`: v3 runtime, policy, memory, experiments, compute, publication, release
- `agents/`: compatibility bridges into the unified runtime
- `frontend/`: local control plane and artifact preview
- `mcp_servers/`: policy-checked MCP process adapters
- `skills/`: runtime skill bridges
- `tests/`: security, migration, unit, integration, and release-gate coverage

The release report records local verification separately from external
services and unavailable infrastructure. It never represents an unavailable
GPU, cloud account, container runtime, or provider credential as verified.

See `docs/V3_ARCHITECTURE.md` and `docs/SECURITY.md`.
