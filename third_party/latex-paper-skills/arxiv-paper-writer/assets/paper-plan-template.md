# Paper Plan: <paper topic>

## Goal
- Produce a review paper on <topic> using the IEEEtran LaTeX template
- Target 60-80 verified citations (70%+ from the last 3 years)
- Include 5+ diverse visualizations

## Project Config
- Workflow mode: <workflow mode>
- Target venue: <target venue>
- Preferred venues: <preferred venues>
- Style mode: <style mode>
- Use `paper.config.yaml` as the machine-readable source policy and venue preference file

## Scope
- In: Literature review, synthesis, visualizations, verified citations
- Out: Novel experiments, primary research, non-review formats

## Kickoff Gate (must be confirmed before writing)
- **STOP**: Do not write prose into `main.tex` until this gate is confirmed and the issues CSV exists.
- [ ] User confirmed scope + outline in chat
- Venue/template: IEEEtran (arXiv)
- Target length: 6-10 pages of main text (references excluded)
- "Latest" definition: as-of date = <YYYY-MM-DD_HH-mm-ss> (update if user wants another cutoff)
- Scope boundaries: in-scope vs out-of-scope (TBD)

## Clarification Q&A (record answers)
| Question | Answer |
|---|---|
| What venue + page limit should we target? | TBD |
| Which subtopics MUST be covered? | TBD |
| What are the main emphasis areas? | TBD |
| Any required datasets/baselines? | TBD |
| Expected figures/tables (min 5 types): any must-haves? | TBD |

## Confirmed Outline (edit to match the user-approved outline)
1. Section 1: <title> - 2-4 bullet goals
2. Section 2: <title> - 2-4 bullet goals
3. Section 3: <title> - 2-4 bullet goals
4. Section 4: <title> - 2-4 bullet goals
5. Section 5: <title> - 2-4 bullet goals
6. Section 6: <title> - 2-4 bullet goals
7. Section 7: <title> - 2-4 bullet goals

## Plan Notes
- Keywords used for discovery:
- Candidate titles proposed:
- Sections/subsections plan:
- Visualization plan (types + placement):
- Source policy notes (`core` / `standard` / `frontier` by section):
- Style anchor papers / venue cues:

## Issue CSV
- Path: <paper_dir>/issues/<YYYY-MM-DD_HH-mm-ss>-<slug>.csv
- Must share the same timestamp/slug as this plan
- This CSV is the execution contract: update issue status as you write and QA
- Writing rows must include `Section_Path` and `Source_Policy`
- Issues may be added/split/inserted during execution; re-validate after edits and keep going until all issues are `DONE`/`SKIP` (when feasible, in the same run).
