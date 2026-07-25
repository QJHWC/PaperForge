# Research Workflow (Concise)

Purpose: build a verified, scoped literature base without over-collecting or drifting off-topic.

## Scope definition
- Clarify topic, intended contribution (survey/benchmark/application), and constraints
- Define the time window (default: 70%+ from the last 3 years relative to the plan as-of date)
- Identify the evaluation focus (metrics, datasets, baselines) only if relevant

## Phases

### Phase 0: Research snapshot (before outline)
**Target: 10-20 key papers**
- Identify core sub-areas and representative approaches
- Build a small “spine” set (foundational + recent flagships)
- Use this snapshot to propose outline, visuals, and clarification questions
- Do not expand to a large pool until scope is approved

### Phase 1: Initial discovery (before writing)
**Target: ~60-120 candidates; expect to cite ~60-80**
- Start from recent surveys and highly cited anchors
- Expand using keywords from the approved outline
- Verify and add to `ref.bib` immediately
- For arXiv candidates, use `arxiv_registry.py` to cache searches/BibTeX and avoid duplicates
- Avoid bulk dumps; prefer incremental, section-driven additions

### Phase 2: Per-section discovery (during writing)
**Target: 8-12 additional papers per section**
- Identify gaps or unsupported claims in the section
- Search, verify, and integrate immediately
- Repeat if gaps remain after drafting

## Keyword expansion (lightweight)
- Start from outline terms, then expand with synonyms and adjacent terms
- Prefer query refinement over broad, low-signal harvesting

## Relevance filter
**Keep** papers that directly support scope, provide essential context, or enable meaningful comparison.
**Reject** papers that are unrelated, unverifiable, redundant, or from low-quality venues.

## Verification protocol (mandatory)
- Search by exact title + first author + year
- Open the source page (venue/arXiv/DOI); confirm title, authors, year, venue
- Only then add to `ref.bib`

## Evidence discipline
- Never fabricate citations or results
- Mark uncertainty as TODO and ask the user
- Use citations for all non-trivial claims

## Quality targets
- 100% verification rate
- 70%+ from the last 3 years
- Zero duplicates or hallucinated citations
- High relevance: every paper supports specific claims
