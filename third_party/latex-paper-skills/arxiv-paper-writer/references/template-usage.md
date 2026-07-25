# Template Usage Notes (Concise)

## Structure
- Preserve the template’s section order unless the user explicitly requests changes
- Keep the abstract, keywords, and references sections intact
- Replace all placeholders and remove instructional comments before delivery

## Figures, tables, and math
- Prefer vector/TikZ for figures when feasible
- Use `booktabs` for tables with clear headers
- Define symbols and variables before use

## Formatting
- Do not override fonts or margins; `IEEEtran` controls layout
- Keep sources in one directory; avoid absolute paths
- Use two-column floats only when needed to avoid overflow

## Bibliography
- Keep `\bibliographystyle{ieeetr}` and `\bibliography{ref}` if using `ref.bib`
