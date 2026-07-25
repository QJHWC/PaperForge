---
name: arxiv-paper-writer
description: >
  Writes ML/AI review and survey papers for arXiv using the IEEEtran LaTeX
  template with verified BibTeX citations. Triggers when the deliverable is
  a review paper, literature survey, or when an existing LaTeX project needs
  citation validation or repair.
---

# ML/AI Review Paper Workflow (IEEEtran template)

## Prerequisites
- Python 3.8+ (for scripts)
- Web browsing/search capability (for citation verification)
- LaTeX recommended (pdflatex + bibtex or latexmk) for compilation

## When to Use
- ML/AI review papers for arXiv (main text ~6-10 pages; references excluded)
- LaTeX + BibTeX workflow with verified citations
- Citation validation/repair on existing LaTeX projects

## When NOT to Use
- Novel experimental research papers (this is a review workflow)
- Non-academic documents

## Role in the Paper Stack
- This skill is the **review-paper executor** in the broader paper stack.
- Use ../paper-from-zero/SKILL.md as the default front door when the topic is known but the paper mode is not yet decided.
- Use this skill directly only when the deliverable is clearly a review/survey-style paper or when an existing LaTeX review project needs citation validation/repair.

## Inputs
- Topic description (required)
- Constraints: venue, page limit, author/affiliations (optional)
- Existing project path for citation validation (optional)

## Outputs
- `main.tex` (LaTeX source)
- `ref.bib` (verified BibTeX entries)
- `IEEEtran.cls`
- `paper.config.yaml` (machine-readable venue/style/source policy config)
- `plan/<timestamp>-<slug>.md`, `issues/<timestamp>-<slug>.csv`
- Figures/tables; `main.pdf`
- `notes/literature-notes.md` (optional per-citation notes)
- `notes/arxiv-registry.sqlite3` (arXiv metadata/BibTeX cache)
- `notes/style-profile.yaml` (optional target-venue style profile)

**Conventions**: run `python3 scripts/...` from this skill folder (where `scripts/` lives); `<paper_dir>` is the paper/project root (contains `main.tex`, `ref.bib`, `paper.config.yaml`, `plan/`, `issues/`, `notes/`). Paths like `plan/...` are under `<paper_dir>`. For arXiv discovery/metadata/BibTeX, use `scripts/arxiv_registry.py` (no ad-hoc curl/wget). Use `scripts/source_ranker.py`, `scripts/citation_policy.py`, and `scripts/style_profile.py` for source-quality, venue-policy, and style-profile workflows.

---

## Gated Workflow

> Tip: Run `python3 scripts/<script>.py --help` before use.
> Open reference files only when a step calls them out.

### Non-Negotiable Rules
1. **No prose in `main.tex`** until plan approved AND issues CSV exists.
2. First deliverable: research snapshot + outline + clarification questions + draft plan.
3. **Use plan + issues tracking for all new papers; do not opt out.**
4. Issues CSV is the execution contract; update `Status` and `Verified_Citations` per issue, and add/split/insert issue rows when scope grows (do not do untracked work).
5. **Template is fixed**: use IEEEtran two-column layout (`assets/template/IEEEtran.cls`).
   Treat two-column width as a layout constraint (use two-column floats when needed).

### Gate 0: Research Snapshot + Draft Plan
1. Confirm constraints (venue, page limit, author block, date range).
2. Translate the topic into search keywords and run a light discovery pass:
   10-20 key papers (see `references/research-workflow.md`). After step 4 (once `<paper_dir>` exists), cache arXiv discovery with `arxiv_registry.py search`.
3. Propose 2-4 candidate titles aligned to the topic.
4. Scaffold the project folder and draft plan:
   ```bash
   python3 scripts/bootstrap_ieee_review_paper.py --stage kickoff --topic "<topic>"
   ```
   For a lighter entrypoint that still creates `paper.config.yaml` and a draft plan, use:
   ```bash
   python3 scripts/bootstrap_ieee_review_paper.py --stage outline --topic "<topic>"
   ```
   This copies LaTeX templates from `assets/template/`; plan/issues are generated from templates in `assets/`.
   Initialize arXiv registry (once): `python3 scripts/arxiv_registry.py --project-dir <paper_dir> init`.
5. Create a **framework skeleton** in `main.tex`
   (section headings + 2-4 bullets per section + seed citations; **no prose**).
6. Update the plan file to reflect the framework, proposed titles, and section/subsection plan.
7. Compile early: `python3 scripts/compile_paper.py --project-dir <paper_dir> --check-warnings`
   Fix any `Overfull \hbox` warnings (see Layout Hygiene below).
8. Return to user:
    - Proposed outline (5-8 sections, 2-4 bullets each)
    - Planned visualizations (5+) mapped to sections (see `references/visual-templates.md`)
    - Clarification questions
9. **STOP** until user approves.

### Gate 1: Create Issues CSV (after approval)
1. Check kickoff gate in plan: `- [x] User confirmed scope + outline in chat`.
2. Create issues CSV (script refuses if gate unchecked):
   ```bash
   python3 scripts/bootstrap_ieee_review_paper.py --stage issues --topic "<topic>" --with-literature-notes
   ```
3. Validate:
   ```bash
   python3 scripts/validate_paper_issues.py <paper_dir>/issues/<timestamp>-<slug>.csv
   ```
4. If literature notes are enabled, keep short summaries and (optional) abstract snippets to avoid re-search.
5. The plan may evolve; add/split/insert issues as needed, re閳ユ唺alidate after edits, and keep going until all issues (including inserted ones) are `DONE` or `SKIP` (when feasible, in the same run).

### Phase 1.5: Literature Enrichment Gate

Before starting the writing loop, ensure citation coverage is adequate for a
review/survey paper. Review papers require substantially more references than
empirical papers.

1. **Gather candidates**: collect papers from initial discovery, spine set,
   and sub-area searches. Aim for 50-80 candidate entries in `ref.bib`.
2. **Sub-area coverage**: for each Related Work subsection (from the outline
   contract or framework skeleton), verify ≥ 5 cited papers. If any sub-area
   has fewer, search for additional papers in that cluster.
3. **Seminal work**: for each major research thread in the taxonomy, verify
   at least 1 foundational/seminal paper (pre-2020) is cited alongside
   recent work. Review papers that cite only recent work look shallow.
4. **Key method citations**: every method, algorithm, or benchmark mentioned
   by name in the outline must have a corresponding `ref.bib` entry.
5. **Quality screening**: verify and filter all citations via the standard
   verification pipeline (`arxiv_registry.py` → `source_ranker.py` →
   `citation_policy.py`). Remove entries that fail verification, are
   duplicates, or are Tier C (low-quality) sources unless no better
   alternative exists for that claim.
6. **Gate (post-screening)**: do not start W1 until `ref.bib` has
   **≥ 30 verified entries after screening**. Target 60-80 for the
   finished paper (additional citations are added during Phase 2 writing).
   Every Related Work sub-area must still have ≥ 5 verified citations
   after screening. If the gate fails, return to enrichment (repeat
   steps 2-5) before proceeding.

### Phase 2: Per-Issue Writing Loop
For each writing issue in the CSV:
- If an issue balloons (new figure, new subsection, new benchmark set, or a large QA fix), split/insert new issue row(s) (e.g., `W6a`, `Q5`) before proceeding; re-run `python3 scripts/validate_paper_issues.py <issues.csv>`; keep going until all issues are `DONE`/`SKIP`.
1. **Research**: 8-12 section-specific papers.
2. **Write**: Never 3 sentences without citations; varied paragraph rhythm
   (see `references/writing-style.md`).
   For section intent and structure, use `references/template-usage.md`.
3. **Visualize**: Match content triggers (see `references/visual-templates.md`).
   Prioritize single-column sizing; use double-column spans only when necessary (see Layout Hygiene).
   Cite externally sourced figure content.
4. **Verify**: Web search + open source page (and PDF if available) before adding to `ref.bib`.
   For arXiv entries, append BibTeX via `python3 scripts/arxiv_registry.py --project-dir <paper_dir> export-bibtex <arxiv_id> --out-bib <paper_dir>/ref.bib`.
   For a per-issue shortcut with stable cite keys, use `python3 scripts/issue_workflow.py --project-dir <paper_dir> append-bibtex --issue-id <Wx> <arxiv_id> [<arxiv_id> ...]`.
5. **Update**: Mark issue `DONE` with `Verified_Citations` count.
   To sync `Verified_Citations` from actual section citations, run `python3 scripts/issue_workflow.py --project-dir <paper_dir> sync-verified --issues <issues.csv>`.
6. Compile after meaningful changes; fix `Overfull \hbox` before marking `DONE`.

### Phase 2.2: Issue Execution Helpers
- Use `python3 scripts/issue_workflow.py --project-dir <paper_dir> render-skeleton --issues <issues.csv> --issue-id <Wx>` to render a LaTeX section skeleton for a Writing issue.
- Add `--apply-if-missing` only when the full section path is entirely absent from `main.tex`; nested insertion under an existing parent stays manual.
- Before QA or after a batch of edits, run `python3 scripts/issue_workflow.py --project-dir <paper_dir> audit --issues <issues.csv>` to check section-path consistency, citation counts, placeholders, and lightweight figure/page signals.

### Phase 2.5: Rhythm Refinement
After all writing issues are `DONE`, refine prose section-by-section using the `latex-rhythm-refiner` skill. This step varies sentence/paragraph lengths and removes filler phrases while preserving all citations.

### Phase 3: QA Gate
1. Run internal QA checklist (see `references/quality-report.md`).
2. Audit source quality and venue policy:
   - `python3 scripts/issue_workflow.py --project-dir <paper_dir> audit --issues <issues.csv> --fail-on-issues`
   - `python3 scripts/source_ranker.py --project-dir <paper_dir> rank`
   - `python3 scripts/citation_policy.py --project-dir <paper_dir> audit-bib`
   - `python3 scripts/citation_policy.py --project-dir <paper_dir> audit-tex --issues <issues.csv>`
   - `python3 scripts/style_profile.py --project-dir <paper_dir> check-draft` (if using `style_mode=target_venue`)
   - `python3 scripts/compile_paper.py --project-dir <paper_dir> --check-warnings --fail-on-warnings`
   - `python3 scripts/citation_policy.py --project-dir <paper_dir> lint-bib --fail-on-lint`
2. Compile; ensure no `Overfull \hbox` warnings in `main.log`.
3. Deliver `main.tex`, `ref.bib`, figures, and `main.pdf`.

---

## Existing Paper Workflow (No Re-Scaffold)
If a paper folder already exists, do NOT rerun scaffold:
```bash
# Create plan
python3 scripts/create_paper_plan.py --topic "<topic>" --stage plan --output-dir <paper_dir>
# STOP for approval, then check kickoff gate box
# Create issues (use timestamp/slug from plan filename/frontmatter)
python3 scripts/create_paper_plan.py --topic "<topic>" --stage issues --timestamp "<TS>" --slug "<slug>" --output-dir <paper_dir> --with-literature-notes
```

## Citation-Validation Variant
1. Treat provided path as LaTeX project root.
2. Follow `references/citation-workflow.md`.
3. Use `references/bibtex-guide.md` for BibTeX rules if entries need repair.
4. Deliver validation report and corrected `ref.bib` if requested.

---

## Success Criteria

**Compilation**: `python3 scripts/compile_paper.py --project-dir <paper_dir> --check-warnings --fail-on-warnings` (exit 0). Use `--report-page-counts` for main-text page count.

**Quality Metrics**:
- 6-10 pages of main text (references excluded)
- 60-80 total citations (8+ per section)
- Every Related Work sub-area has ≥ 5 citations
- At least 1 seminal/foundational citation per major research thread
- 100% citation verification rate
- 70%+ citations from last 3 years
- 5+ visualization types
- All issues `DONE` or `SKIP`

---

## Safety & Guardrails
- **Never fabricate** citations or results; add TODO and ask user if evidence missing.
- **Verify every citation** via web search + source page (and PDF if available) before adding to `ref.bib`.
- **Confirm before** large literature searches.
- **Do not overwrite** user files without confirmation.
- **Issues CSV** is the contract; mark `DONE` only when criteria met.
- **No submission bundles** unless user requests.

## Layout Hygiene
Fix `Overfull \hbox` warnings before marking issues `DONE`:
- Figures: start with `figure` + `\columnwidth`; switch to `figure*` + `\textwidth` if needed
- Tables: prefer `p{...}` column widths / `\tabcolsep` over `\resizebox`
- Equations: use `split`, `multline`, `aligned`, or `IEEEeqnarray` for line-breaking

---

## Issues CSV Schema
| Phase | Issues |
|-------|--------|
| Research | Rx: discovery, scaffolding, framework, viz planning |
| Writing | Wx: each section with `Section_Path`, `Source_Policy`, target citations, and visualization |
| Refinement | RFx: apply `latex-rhythm-refiner` skill (after all Wx DONE) |
| QA | Qx: citation verification, QA checklist, compilation, final review |

Status: `TODO` -> `DOING` -> `DONE`. Schema validated by `validate_paper_issues.py`.


