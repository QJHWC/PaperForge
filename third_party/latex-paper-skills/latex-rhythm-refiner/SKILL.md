---
name: latex-rhythm-refiner
description: >
  Post-process LaTeX project prose to improve readability through varied
  sentence and paragraph lengths. Removes filler phrases and unnecessary
  transitions while preserving all citations and semantic meaning.
---

# LaTeX Rhythm Refiner

## When to Use
- After LaTeX content generation is complete
- To improve prose flow and readability in academic documents
- When sections feel monotonous or blocky

## When NOT to Use
- During initial content drafting
- For citation verification or addition
- For technical/structural LaTeX fixes

## Core Principles

### 1. Preserve Citations Exactly
- Every `\cite{...}` must remain in place
- Citations stay attached to their original semantic context
- Never move a citation to a different claim or sentence meaning

### 2. Vary Rhythm Stochastically
**Sentence length**: Mix short (5-12 words), medium (13-22 words), and long (23-35 words) sentences. Avoid consecutive sentences of similar length.

**Paragraph length**: Alternate between:
- Short (2-3 sentences) for emphasis or transitions
- Medium (4-5 sentences) for standard exposition
- Long (6-8 sentences) for complex arguments

### 3. Remove Fillers
Eliminate or replace:
- "in order to" → "to"
- "it is worth noting that" → (delete or rephrase directly)
- "due to the fact that" → "because"
- "in the context of" → "in" or "for"
- "a large number of" → "many"
- "in spite of the fact that" → "although"
- "at the present time" → "now" or "currently"
- "for the purpose of" → "to" or "for"

### 4. Minimize Transitions
Remove when structure already implies the relationship:
- "However," / "Therefore," / "Moreover," / "Furthermore,"
- "As mentioned above," / "As previously discussed,"
- "It should be noted that" / "In this regard,"

### 5. Tighten Prose
- Prefer active voice; replace vague verbs ("shows", "does", "works") with concrete ones
- Avoid repeated sentence openings across adjacent sentences ("This", "In practice", etc.)
- Replace hedge stacks ("may potentially") with one qualifier
- Each paragraph: one main idea, clear first sentence
- Ensure figures/tables are referenced and explained in text

## Processing Workflow

### Per-Section Refinement
Process one section at a time:

1. **Read** the section fully to understand context and argument flow
2. **Identify** all `\cite{...}` locations and their attached claims
3. **Map** current sentence/paragraph lengths
4. **Refine**:
   - Vary sentence lengths (break long chains, combine choppy sequences)
   - Adjust paragraph boundaries for rhythm
   - Strip fillers and unnecessary transitions
5. **Verify** all citations remain with their original semantic claims
6. **Output** the refined section

### Verification Checklist
Before finalizing each section:
- [ ] Citation count unchanged
- [ ] Each citation still supports its original claim
- [ ] No 3+ consecutive sentences of similar length
- [ ] Paragraph lengths vary
- [ ] Filler phrases removed
- [ ] Unnecessary transitions eliminated
- [ ] Technical accuracy preserved

## Constraints
- **Do not** add, remove, or relocate citations
- **Do not** change technical claims or data
- **Do not** alter LaTeX commands, environments, or structure
- **Do not** modify figure/table references or captions
- **Do not** expand abbreviations or change terminology
