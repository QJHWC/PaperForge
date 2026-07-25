# Citation Workflow (Concise)

Purpose: ensure every claim is supported and every BibTeX entry is real, accurate, and verified.

## During writing (iterative)
Trigger additional citations when:
- Starting a new section
- Introducing or comparing concepts
- Making quantitative or factual claims
- Noticing a paragraph with no citations
- Adding/adapting a figure/table/plot that includes externally sourced content (nodes/labels/data/results)

Visual citation rule of thumb:
- Prefer citing sources in the figure/table caption (keeps diagrams readable) or in the immediately surrounding text.
- Put `\cite{}` inside TikZ/node labels only when necessary (e.g., many distinct sourced items and caption would be ambiguous).

Protocol:
1. Identify the exact claim needing support
2. Search by title/author/year
3. Open the source page (venue/arXiv/DOI) and confirm metadata
4. Add to `ref.bib` only after verification
5. Cite immediately in text
   - For arXiv entries, use `arxiv_registry.py export-bibtex` to populate `ref.bib` (stable keys, dedup)

Citation density:
- Never 3 consecutive sentences without citations
- Meet per-section targets in the issues CSV

## Standalone validation (existing project)
1. Extract all `\cite{}` keys from the LaTeX source
2. Compare against `ref.bib` to find missing/orphaned entries
3. Verify every BibTeX entry online and correct metadata
4. Fix or remove invalid entries; summarize changes in chat if requested

## BibTeX hygiene
- Required fields present for entry type
- Special characters escaped; accented names use LaTeX commands
- No duplicates; consistent key format
