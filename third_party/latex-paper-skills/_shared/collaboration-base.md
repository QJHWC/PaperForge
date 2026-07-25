# Collaboration Base Rules

Shared rules and patterns for Claude and Gemini co-pilot skills.
Each collaborator SKILL.md imports these rules and adds tool-specific
sections (model selection, auth, advanced flags).

## Core Rules

- The collaborator (Claude or Gemini) is a **co-pilot**; you own the final
  result and must verify changes locally.
- Do **not** invoke `claude` or `gemini` directly; always use the bridge
  script for the relevant skill so output/session handling stays consistent.
- Prefer file/line references over pasting code snippets.  Run the bridge
  with `--cd` set to the repo root.
- For code changes, request **Unified Diff Patch ONLY** and forbid direct
  file modification.
- Always capture `SESSION_ID` and reuse it for follow-ups to keep the
  conversation aware.
- Keep a short **Collaboration State Capsule** updated while this skill is
  active (see format below).
- Default timeout: set `timeout_ms` to **600000 (10 minutes)** unless a
  shorter/longer timeout is explicitly required.

## Candidates-Only Hard Constraint (Paper Mode)

All paper-mode outputs are **unverified candidates**, not citable facts.
They must pass through the downstream citation verification pipeline
(`arxiv_registry.py` → `source_ranker.py` → `citation_policy.py`) before
entering `ref.bib` or artifact final fields.  See template-level constraints
in `assets/prompt-template.md` for output format rules.

## Fallback: Code Collaboration Mode

For generic code tasks:

1. **Ask the model to open files itself** — provide entry file(s), line
   numbers, objective, constraints, and output format (diff vs analysis).
   Avoid pasting large code blocks.
2. **Enforce safe output for code changes** — append:
   `OUTPUT: Unified Diff Patch ONLY. Strictly prohibit any actual modifications.`
3. **Use the model for what it's good at** — alternative solution paths,
   edge cases, UI/UX feedback, patch review.

## Unavailability

Collaboration hooks are non-blocking.  If the CLI is unreachable, the
orchestrator decides the fallback (skip, self-execute, or ask user).
Candidates-Only constraint applies regardless of who executes the template.

## Safety & Guardrails

- Never paste secrets (private keys, API keys, seed phrases) into prompts.
- For code changes, request **Unified Diff Patch ONLY** and apply changes
  yourself.
- Treat collaborator output as suggestions; verify locally (tests, lint,
  build) before merging.

## Collaboration State Capsule

### Paper Mode Capsule (default)

Keep this block updated near the end of your reply while collaborating:

```text
[Paper Collaboration Capsule]
Topic:
Current_Mode_Hypothesis: review | empirical | undecided
Current_Artifact: topic-brief | contribution-map | evidence-matrix | outline-contract | router-decision
Artifact_Version: draft-N
Open_Gaps:
Rejected_Framings:
Evidence_Risks:
Next_Validation_Step:
SESSION_ID:
```

### Legacy Capsule (Code Mode)

```text
[Collaboration Capsule]
Goal:
SESSION_ID:
Files/lines handed off:
Last ask:
Summary:
Next ask:
```
